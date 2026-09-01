# Copyright 2026 Snowflake Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Metamorphic coverage: the code that only *one* side of an equivalent pair executes.

Ordinary coverage counts the union of what a test suite touches. Metamorphic coverage (Ba, Jiang and
Rigger, arXiv:2508.16307) counts, for a pair of inputs expected to agree, the **symmetric difference** —
lines reached by solely one side. The argument for it is that a metamorphic oracle can only detect a bug
in code the two sides execute *differently*; code both sides run identically is compared against itself
and proves nothing. Argus reports 3.256% line MC for SQLancer against 17.820% for itself, and calls it
"a more relevant metric for evaluating the effectiveness of test oracles" than plain coverage.

**DuckDB reporting matches the MC paper's gcovr pipeline** (``filter=src/`` + their
``exclude-directories``), not the Argus/SQLancer++ lcov campaign path. Postgres has no official MC
artifact target; ``scope=all`` there still uses paper full-tree lcov.

**eqgen is an unusually clean subject for it.** In TLP or NoREC the pair is two *different query texts*
over one database, so the difference mixes "different plan" with "different parse and analyse path".
Here the query text is byte-identical on both sides and only the object differs, so the symmetric
difference isolates exactly what the object's shape caused.

## No curve, by design

Both papers report MC as a per-suite aggregate, never a time series — Argus in a table averaged over ten
suites, and the source paper over 10 suites of 100 test cases. So none of the campaign machinery applies:
no sampler, no cadence, and in particular **no flush/restart**, because a long-lived process's coverage
is identical on both sides of a pair and cancels in the symmetric difference.

## What it costs, and why the suite is small

One measurement needs coverage for a single query on a single side, in isolation: zero the counters, run
it, make the backend exit so it writes them, then capture the line set. Campaigns already treat live
reports as too slow: they copy the ``.gcda`` files and report later. MC does the same per side, then
parses the two snapshots. Postgres (lcov) can parse both sides in parallel; DuckDB gcovr must stay
serial because shared-checkout ``.gcno`` race on intermediate ``.gcov`` files. That is still the
paper's "~1 minute per test input" order of magnitude, and why they used fixed suites of a hundred
rather than campaigns.

## Flushing per query

**PostgreSQL.** Counters reach disk only at process exit and ``__gcov_dump`` is not in the binary, so
the only way to flush after one query is to close the connection that ran it. Each measurement opens a
fresh connection, runs one query, and closes. ``_PgConnection.close()`` drops its schema, so the
connection that *built* a side is held open for the whole measurement. ``CREATE TEMPORARY`` objects are
session-scoped, so those builders are excluded (:data:`_EXCLUDED_BUILDERS`).

**DuckDB.** Each connection is a CLI process that writes ``.gcda`` on exit — the same reconnect-to-flush
pattern. Objects live in a *file-backed* database (not ``:memory:``) so a later process sees them after
the builder exits. Temporary builders are excluded for the same reason as on Postgres.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import gzip
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional, Protocol, Sequence, Union

from eqgen.core.catalog import Table
from eqgen.equivalence.config import EquivalenceConfig
from eqgen.equivalence.generator import EquivalenceGenerator
from evaluation.coverage import gcov as coverage
from evaluation.coverage.campaign import PG_DEFAULT_SOURCE, PG_SOURCE_ENV, _resolve_duckdb_trees
from eqgen.fuzz.adapter import DialectAdapter
from eqgen.fuzz.database import (
    Database,
    Row,
    exposed_fork_names,
    fork_copy_statements,
    hidden_base_name,
)
from eqgen.fuzz.journal import sample_rows
from eqgen.generators.example_generator import RandomPredicateSource, RandomSelectSource

#: Builders whose objects cannot survive a reconnect. A ``CREATE TEMPORARY`` object belongs to the
#: session that made it, so the fresh connection each measurement opens cannot see it — the query would
#: fail on the equivalent side only, which looks exactly like a bug and is not one.
_EXCLUDED_BUILDERS = ("CreateTemporaryViewBuilder", "CreateTemporaryTableBuilder")

#: DuckDB builders that need extensions the instrumented coverage CLI cannot load (no extension
#: download on the offline gcov build). Excluding them keeps suites from dying mid-build.
#:
#: The JSON codec is ``JsonPackCodecRoundTripBuilder`` -- portable, in
#: ``equivalence/builders/roundtrips.py``, not a DuckDB-native builder. This tuple used to name
#: ``DuckDBJsonPackRoundTripBuilder``, which exists nowhere in eqgen, so :func:`mc_config`'s
#: ``weights.pop`` silently did nothing and suites kept drawing it. A smoke run dies on
#: ``Scalar Function with name "json_extract_string" is not in the catalog, but it exists in the json
#: extension``, which is the likeliest reason both 10x100 DuckDB attempts ended truncated.
#:
#: ``MixedCodecRoundTripBuilder`` needs excluding for the same reason without naming JSON: it draws a
#: codec per column and JSON pack is in its mix, so dropping only the dedicated builder leaves the
#: dependency reachable.
_EXCLUDED_BUILDERS_DUCKDB = _EXCLUDED_BUILDERS + (
    "JsonPackCodecRoundTripBuilder",
    "MixedCodecRoundTripBuilder",
)


@dataclass(frozen=True)
class Divergence:
    """Lines exactly one side of an equivalent pair ran, on one measurement surface.

    Two surfaces are measured, because eqgen runs two comparisons and each has its own pair:

    * **setup** — the DDL+DML that produces the base table, against the DDL that produces the
      equivalence. Both sides run byte-identical ``base_setup_statements``
      (:meth:`Database.build_equivalent`), so that prefix cancels and what survives is the composed
      object. The oracle is the row check that gates every round.
    * **query** — one query text, run against each of the two objects. The oracle is the result
      comparison.

    Neither surface requires the two sides to be balanced. MC only asks that they execute
    *different* code, since code both sides run identically is compared against itself; a line only
    the equivalence ran is a line where a bug corrupts one side's data and a comparison catches it.
    The query surface is already strongly one-sided in practice (median 52 base-only against 376
    equivalent-only over the 1,000-pair Postgres suite), so asymmetry is the norm rather than a
    defect of the setup surface.
    """

    base_only: int
    equivalent_only: int
    both: int
    #: The lines exactly one side ran. Kept, not just counted, because the *reported* metric is the union
    #: of these across a suite -- Argus's 17.820% is a suite figure, not a per-pair one, so per-pair means
    #: are not comparable with it.
    divergent_lines: frozenset[tuple[str, int]] = frozenset()
    #: Instrumented lines **per file**, as lcov reported them for this measurement.
    #:
    #: Per file rather than a single total because ``lcov --capture`` only reports translation units
    #: that have written a ``.gcda``, so a module first touched midway through a run joins the
    #: denominator midway through it. A scalar total therefore drifts upward across a run and differs
    #: between arms of a sweep. :func:`~evaluation.coverage.gcov.fixed_denominator` pins one
    #: denominator from per-file maxima, which is what the campaign already does (COVERAGE_NOTES.md §5).
    instrumented_by_file: dict[str, int] = dataclasses.field(default_factory=dict)

    @property
    def instrumented(self) -> int:
        """This one measurement's denominator — diagnostic only.

        Not the figure to report: it counts only the files lcov saw for *this* pair. Report against the
        run-wide pinned denominator instead.
        """
        return sum(self.instrumented_by_file.values())

    @property
    def divergent(self) -> int:
        """Lines exactly one side executed — the metamorphic coverage of this pair."""
        return self.base_only + self.equivalent_only

    @property
    def union(self) -> int:
        return self.divergent + self.both

    @property
    def divergence_ratio(self) -> float:
        """Divergent lines as a share of everything the pair touched. Not the reported metric, but the
        useful one for reading a single pair: 0 means the two sides ran identical code."""
        return self.divergent / self.union if self.union else 0.0


@dataclass(frozen=True)
class PairCoverage(Divergence):
    """One query, measured on both sides. The *query* surface."""

    query: str = ""


@dataclass(frozen=True)
class SetupCoverage(Divergence):
    """One suite's two setups, measured. The *setup* surface.

    Measured once per suite rather than once per pair — the objects are built once and every query
    then probes them — so this whole surface costs about a tenth of what the query surface does at
    ``--pairs 10``.

    ``measured`` is False for the TLP and NoREC arms. Those run both halves of every pair against
    the *same* base table (:func:`iter_pairs`), so their two setups are not merely similar but
    identical, and the symmetric difference is analytically empty. Recording the zero rather than
    spending a build on it keeps the record shape uniform, and makes "query-rewrite oracles have no
    setup-phase divergence surface" a line in the data instead of a claim in the prose.
    """

    #: How many statements the equivalence side ran beyond the shared base setup.
    statements: int = 0
    #: False when the zero is analytic (TLP / NoREC) rather than measured (eqgen).
    measured: bool = True


class _ReplayPredicateSource:
    """A fixed pool of predicates, chosen by seed — deterministic where SQLancer++ is not.

    :class:`~eqgen.generators.sqlancerpp.SqlancerppPredicateSource` accepts a *seed* and ignores it:
    one shared jar stream hands each caller whatever came next. That is fine for bug hunting, where
    the journal records what ran, but it makes a *comparison* unsound — two arms of a sweep would
    differ in their embedded predicates as well as in the variable under test, and on the setup
    surface the predicate is a large part of what the DDL exercises.

    Harvest the jar's output once, replay it here, and both arms see byte-identical inputs.
    """

    name = "replay"

    def __init__(self, pool: Sequence[str]) -> None:
        if not pool:
            raise ValueError("predicate pool is empty")
        self._pool = tuple(pool)

    def boolean_predicate(self, table: Table, *, seed: int) -> Optional[str]:
        del table
        return self._pool[seed % len(self._pool)]


def harvest_generator_inputs(
    cache: Path,
    *,
    adapter: DialectAdapter,
    table: Table,
    generator: str,
    predicates: str,
    queries_wanted: int,
    predicates_wanted: int,
    exposed_names: Sequence[str] = (),
) -> tuple[object, object]:
    """``(query_source, predicate_source)`` replaying a cached harvest, filling the cache if empty.

    The first arm of a sweep pays for generation and writes ``queries.sql`` / ``predicates.txt``;
    every later arm reads them. That is what lets arms be compared pairwise rather than only on
    averages, and it is also why the jar is only booted once per sweep instead of once per arm.

    ``exposed_names`` only matters for the one-time harvest call below (it tells the live jar about
    ``t0``/``t1``/... so it can generate joins across them) -- a cache is keyed to the exposed names
    it was harvested under. Replaying a cache under a different fork count means the cached queries
    reference relations that don't exist under the new schema; that fails loudly on its own (every
    query gets rejected and dropped by ``_measure_queries``) so no separate guard is needed here.
    """
    from eqgen.fuzz.cli import predicate_source_for, query_source
    from eqgen.plugins import CorpusSource

    cache.mkdir(parents=True, exist_ok=True)
    query_file, predicate_file = cache / "queries.sql", cache / "predicates.txt"

    if not query_file.is_file() or not predicate_file.is_file():
        print(f"harvesting {queries_wanted} queries / {predicates_wanted} predicates into {cache}", flush=True)
        source = query_source(None, generator, dialect=adapter.name)
        harvested = list(source.iter_queries(table, seed=0, limit=queries_wanted, exposed_names=exposed_names))
        query_file.write_text(";\n".join(harvested) + ";\n", encoding="utf-8")
        pool: list[str] = []
        predicate_src = predicate_source_for(predicates, dialect=adapter.name)
        if predicate_src is not None:
            for index in range(predicates_wanted * 4):
                text = predicate_src.boolean_predicate(table, seed=index)
                if text and text not in pool:
                    pool.append(text)
                if len(pool) >= predicates_wanted:
                    break
        predicate_file.write_text("\n".join(pool) + "\n", encoding="utf-8")
        with contextlib.suppress(Exception):
            source.close()
        print(f"harvested {len(harvested)} queries, {len(pool)} distinct predicates", flush=True)

    # shuffle=True: each suite gets a different seeded permutation of the pool, so with a pool larger
    # than --pairs the suites see different queries while every arm still sees identical ones. Ordered
    # replay would hand all suites the same leading N.
    ordered = CorpusSource.from_path(query_file)
    replay_queries = CorpusSource(ordered.queries, name=f"{ordered.name}+shuffle", shuffle=True)
    pool = [line for line in predicate_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    print(f"replaying {len(replay_queries.queries)} queries / {len(pool)} predicates from {cache}", flush=True)
    return replay_queries, _ReplayPredicateSource(pool)


def mc_config(
    base: EquivalenceConfig,
    *,
    dialect: str = "postgres",
    also_exclude: Sequence[str] = (),
) -> EquivalenceConfig:
    """The dialect's config with reconnect-unsafe (and DuckDB extension-only) builders excluded.

    *also_exclude* is how ``--builders portable`` drops the dialect-native set: a portable-only
    configuration keeps the taxonomy and the arms dialect-independent, so a depth curve measured on
    Postgres means the same thing as one measured on DuckDB. It also removes the awkwardness of
    explaining a bundle of engine-specific builders that exist for bug-hunting rather than for the
    experiment.
    """
    excluded = set(_EXCLUDED_BUILDERS_DUCKDB if dialect == "duckdb" else _EXCLUDED_BUILDERS)
    excluded.update(also_exclude)
    # Set to 0.0 rather than deleting the key. `WeightedBuilderFactory._get_builder_weight` and
    # `_weighted_shuffle` both fall back to `.get(name, 1.0)`, so removing an entry does not exclude a
    # builder -- it *promotes* it to weight 1.0. This function used to `pop`, which is why the
    # reconnect-unsafe CREATE TEMPORARY builders kept appearing in `builders_used` despite the
    # docstring above. Weight 0 is the documented way to disable one (see
    # config/gcl/equivalence_generator_v3.gcl).
    weights = dict(base.builder_weights)
    roots = dict(base.root_builder_weights)
    for name in excluded:
        weights[name] = 0.0
        if roots:
            roots[name] = 0.0
    return dataclasses.replace(base, builder_weights=weights, root_builder_weights=roots)


def covered_lines(
    search: Path,
    *,
    root: Optional[Path] = None,
    filters: Sequence[str] = coverage.DEFAULT_FILTERS,
    exclude_directories: Sequence[str] = (),
    jobs: int = 12,
    json_out: Optional[Path] = None,
) -> tuple[set[tuple[str, int]], dict[str, int]]:
    """Every ``(file, line)`` with a non-zero count in whatever counters are under *search* (gcovr).

    Line-level rather than the aggregate ``--json-summary`` used elsewhere, because a symmetric
    difference needs line identity. File paths are keyed relative to *root* (default *search*) when
    they fall under it, so two staged snapshots of the same build compare equal even though each lives
    in its own temporary directory.

    *exclude_directories* maps to gcovr ``--exclude-directories`` (MC paper DuckDB ``gcovr.cfg``).
    """
    report_root = (root or search).resolve()
    destination = json_out or (search / ".gcovr-mc.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable, "-m", "gcovr",
        "--root", str(report_root), str(search),
        "--merge-mode-functions=merge-use-line-min",
        "--gcov-ignore-parse-errors=all",
        "--gcov-ignore-errors=all",
        "--json", str(destination), "-j", str(jobs),
    ]  # fmt: skip
    for pattern in filters:
        command += ["--filter", pattern]
    for pattern in exclude_directories:
        command += ["--exclude-directories", pattern]
    result = subprocess.run(command, cwd=search, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"gcovr failed ({result.returncode}): {result.stderr.strip()[-1500:]}")
    with open(destination, encoding="utf-8") as handle:
        report = json.load(handle)
    files = report.get("files", [])
    hit: set[tuple[str, int]] = set()
    instrumented: dict[str, int] = {}
    for entry in files:
        file_key = _file_key(entry["file"], report_root)
        lines = entry.get("lines", [])
        instrumented[file_key] = max(instrumented.get(file_key, 0), len(lines))
        for line in lines:
            if line.get("count", 0):
                hit.add((file_key, line["line_number"]))
    return hit, instrumented


def covered_lines_via_duckdb_lcov(
    search: Path,
    *,
    root: Path,
    json_out: Optional[Path] = None,
    jobs: Optional[int] = None,
) -> tuple[set[tuple[str, int]], dict[str, int]]:
    """Paper-aligned DuckDB line hits via :func:`~evaluation.coverage.gcov.run_duckdb_lcov`.

    Same hit-set shape as :func:`covered_lines`, but the denominator matches campaign / SQLancer++
    lcov (``lcov_exclude``, omit unevaluated BRDA) rather than gcovr-over-``src/``.
    """
    report_root = root.resolve()
    work_dir = None
    if json_out is not None:
        work_dir = json_out.parent / f"{json_out.stem}-lcov-work"
        work_dir.mkdir(parents=True, exist_ok=True)
    report = coverage.run_duckdb_lcov(
        report_root,
        root=report_root,
        object_directory=search,
        work_dir=work_dir,
        json_out=json_out,
        jobs=jobs,
    )
    hit, _ = coverage.hit_lines_from_lcov_report(report, root=report_root)
    return hit, coverage.instrumented_by_file(report, root=report_root)


def covered_lines_via_postgres_lcov(
    search: Path,
    *,
    root: Path,
    json_out: Optional[Path] = None,
    jobs: Optional[int] = None,
) -> tuple[set[tuple[str, int]], dict[str, int]]:
    """Paper-aligned Postgres line hits via :func:`~evaluation.coverage.gcov.run_postgres_lcov`.

    Full-tree capture, no ``--no-external`` / exclude list — same as the SQLancer++ artifact script.
    *search* is the staged in-tree mirror (``.gcno`` + snapshot ``.gcda``); *root* is the real
    checkout used to key paths after symlink resolve.
    """
    report_root = root.resolve()
    work_dir = None
    if json_out is not None:
        work_dir = json_out.parent / f"{json_out.stem}-lcov-work"
        work_dir.mkdir(parents=True, exist_ok=True)
    report = coverage.run_postgres_lcov(search, work_dir=work_dir, json_out=json_out, jobs=jobs)
    hit, _ = coverage.hit_lines_from_lcov_report(report, root=report_root)
    return hit, coverage.instrumented_by_file(report, root=report_root)


def _file_key(file_path: str, root: Path) -> str:
    """Stable identity for a report path across staged temporary trees.

    Paths under *root* become root-relative. Paths outside it (typical: compile-time paths baked into
    ``.gcno``) stay absolute, so two stagings that symlink the same ``.gcno`` still agree.
    """
    path = Path(file_path)
    if not path.is_absolute():
        return file_path
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(root))
    except ValueError:
        return str(resolved)


@contextmanager
def _staged_snapshot(snapshot: Path, counter_dir: Path, *, mirror_tree: bool) -> Iterator[Path]:
    """Temporary tree with *snapshot*'s ``.gcda`` overlaid on *counter_dir*'s ``.gcno``.

    PostgreSQL builds in-tree, so a full ``cp -as`` mirror keeps ``.c`` sources next to the counters.
    DuckDB's cmake out-of-tree build is tens of GB of ``.o`` files; only the ``.gcno`` are mirrored,
    and gcov resolves sources via the absolute paths baked into those notes (the real checkout).
    """
    with tempfile.TemporaryDirectory(prefix="eqgen-mc-") as tmp:
        work = Path(tmp)
        if mirror_tree:
            result = subprocess.run(
                ["cp", "-as", f"{counter_dir.resolve()}/.", str(work)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(f"cp -as failed ({result.returncode}): {result.stderr.strip()[-1500:]}")
            for stale in work.rglob("*.gcda"):
                stale.unlink(missing_ok=True)
        else:
            for gcno in counter_dir.rglob("*.gcno"):
                dest = work / gcno.relative_to(counter_dir)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.symlink_to(gcno.resolve())
        for gcda in snapshot.rglob("*.gcda"):
            dest = work / gcda.relative_to(snapshot)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(gcda, dest)
        yield work


def covered_lines_from_snapshot(
    snapshot: Path,
    counter_dir: Path,
    *,
    source_root: Path,
    filters: Sequence[str] = coverage.DEFAULT_FILTERS,
    exclude_directories: Sequence[str] = (),
    jobs: int = 12,
    json_out: Optional[Path] = None,
    mirror_tree: bool = True,
    reporter: str = "gcovr",
    dialect: str = "postgres",
) -> tuple[set[tuple[str, int]], dict[str, int]]:
    """Like :func:`covered_lines`, but for a ``.gcda`` snapshot rather than the live build tree.

    *reporter* ``gcovr`` (DuckDB MC paper path; default) or ``lcov`` (Postgres ``scope=all``).
    Relative gcovr ``--filter`` patterns are resolved against *source_root*, not the staging
    directory: gcovr realpath's sources back to the real tree, and a filter anchored at the staging
    root would exclude every file. The lcov path ignores *filters* / *exclude_directories*.
    """
    real = source_root.resolve()
    with _staged_snapshot(snapshot, counter_dir, mirror_tree=mirror_tree) as work:
        if reporter == "lcov":
            if dialect == "duckdb":
                return covered_lines_via_duckdb_lcov(work, root=real, json_out=json_out, jobs=jobs)
            if dialect == "postgres":
                return covered_lines_via_postgres_lcov(work, root=real, json_out=json_out, jobs=jobs)
            raise ValueError(f"lcov reporter unsupported for dialect {dialect!r}")
        if reporter != "gcovr":
            raise ValueError(f"unknown MC reporter {reporter!r} (available: gcovr, lcov)")
        abs_filters = tuple(
            pattern if Path(pattern).is_absolute() else str((real / pattern).resolve()) for pattern in filters
        )
        return covered_lines(
            work,
            root=real,
            filters=abs_filters,
            exclude_directories=exclude_directories,
            jobs=jobs,
            json_out=json_out,
        )


class Side(Protocol):
    """One half of a metamorphic pair: run a query in isolation, then tear down."""

    def run_isolated(self, query: str) -> None: ...

    def flush_builder(self) -> None:
        """Make whatever process ran the build write its counters, keeping the object usable."""
        ...

    def close(self) -> None: ...


class _PgSide:
    """Postgres side whose schema outlives the builder connection."""

    def __init__(self, adapter: DialectAdapter, database: Database) -> None:
        from eqgen.dialects.postgres.adapter import PostgresAdapter

        assert isinstance(adapter, PostgresAdapter)
        self._database = database
        self.schema = database.connection._schema  # type: ignore[attr-defined]
        self.dsn = adapter._cluster.dsn
        self._builder_flushed = False

    def flush_builder(self) -> None:
        """Exit the backend that ran the build, so it writes its counters — without dropping the schema.

        Counters reach disk only at process exit and ``__gcov_dump`` is not in the binary, so measuring
        what the *setup* executed means ending the backend that executed it. The obvious way to do that
        is :meth:`_PgConnection.close`, and it is the wrong one: it runs ``DROP SCHEMA … CASCADE``
        first (``postgres/adapter.py``), which would put teardown of the composed object — catalog
        scans, dependency walking, one ``DROP`` per generated relation — inside the setup snapshot and
        attribute it to the equivalence. Closing the underlying connection directly skips the drop.

        The schema survives, which is what :meth:`run_isolated` needs anyway, so nothing has to be
        rebuilt for the query surface. :meth:`close` picks the drop back up at teardown, by which point
        no snapshot is being taken and its coverage lands nowhere.
        """
        raw = self._database.connection._connection  # type: ignore[attr-defined]
        with contextlib.suppress(Exception):
            raw.close()
        self._builder_flushed = True

    def run_isolated(self, query: str) -> None:
        import psycopg

        connection = psycopg.connect(self.dsn, autocommit=True)
        try:
            connection.execute(f'SET search_path TO "{self.schema}"')
            cursor = connection.execute(query)
            with_rows = getattr(cursor, "description", None)
            if with_rows:
                cursor.fetchall()
        finally:
            connection.close()

    def close(self) -> None:
        """Drop the schema. Uses a fresh connection when the builder's has already been flushed away.

        ``_PgConnection.close`` skips its own ``DROP`` when the connection is already closed, so after
        :meth:`flush_builder` the schema would otherwise leak for the rest of the run.
        """
        if not self._builder_flushed:
            self._database.close()
            return
        import psycopg

        with contextlib.suppress(psycopg.Error):
            connection = psycopg.connect(self.dsn, autocommit=True)
            try:
                connection.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
            finally:
                connection.close()


class _DuckSide:
    """DuckDB side backed by a file: builder may exit; a later CLI process reopens the catalog."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def flush_builder(self) -> None:
        """Nothing to do: :func:`_build_duck_side` already exits the builder process.

        Every DuckDB connection is its own CLI process and writes ``.gcda`` when it goes, so the build's
        counters are on disk before this side is even returned. Until now they were then deleted by the
        first ``zero_counters`` in :func:`measure_pair` — the setup surface was being generated and
        discarded. :func:`measure_setup` snapshots ahead of that zero instead.
        """

    def run_isolated(self, query: str) -> None:
        import duckdb

        from eqgen.dialects.duckdb import cli

        connection = cli.connect_cli(self.path)
        try:
            cursor = connection.execute(query)
            try:
                # DDL (e.g. the fork-copy CREATE TABLE ... AS SELECT measure_setup runs here) has no
                # result set, but the CLI cursor's .description eagerly runs "DESCRIBE <query>" to find
                # out -- which itself fails to parse a non-SELECT statement.
                has_result_set = cursor.description
            except duckdb.Error:
                has_result_set = None
            if has_result_set:
                cursor.fetchall()
        finally:
            connection.close()

    def close(self) -> None:
        self.path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            self.path.with_suffix(self.path.suffix + ".wal").unlink(missing_ok=True)
        parent = self.path.parent
        if parent.name.startswith("eqgen-mc-duck-"):
            shutil.rmtree(parent, ignore_errors=True)


def _build_pg_side(
    adapter: DialectAdapter,
    table: Table,
    rows: Sequence[Row],
    *,
    statements: Optional[Sequence[str]] = None,
    exposed_names: Sequence[str] = (),
) -> _PgSide:
    if statements is None:
        database = Database.build_base(adapter, table, rows, exposed_names=exposed_names)
    else:
        database = Database.build_equivalent(
            adapter, table, rows, statements=statements, exposed_names=exposed_names
        )
    return _PgSide(adapter, database)


def _build_duck_side(
    adapter: DialectAdapter,
    table: Table,
    rows: Sequence[Row],
    *,
    statements: Optional[Sequence[str]] = None,
    exposed_names: Sequence[str] = (),
) -> _DuckSide:
    from eqgen.dialects.duckdb import cli

    work = Path(tempfile.mkdtemp(prefix="eqgen-mc-duck-"))
    path = work / "side.duckdb"
    connection = cli.connect_cli(path)
    try:
        if statements is None:
            Database.build_base(adapter, table, rows, connection=connection, exposed_names=exposed_names)
        else:
            Database.build_equivalent(
                adapter,
                table,
                rows,
                statements=statements,
                connection=connection,
                exposed_names=exposed_names,
            )
    finally:
        # Exit the builder process so the file is consistent and its .gcda are written (then zeroed
        # before the first measurement).
        connection.close()
    return _DuckSide(path)


def _diff_snapshots(
    counter_dir: Path,
    left_snap: Path,
    right_snap: Path,
    work: Path,
    *,
    source_root: Path,
    jobs: int,
    filters: Sequence[str],
    exclude_directories: Sequence[str],
    mirror_tree: bool,
    reporter: str,
    dialect: str,
) -> tuple[set[tuple[str, int]], set[tuple[str, int]], dict[str, int]]:
    """``(left hits, right hits, instrumented)`` for two already-taken snapshots.

    Shared by both surfaces: snapshotting counters is cheap, turning them into line sets is not, and the
    parallelism rule is the same either way. Postgres in-tree staging (*mirror_tree*) and lcov (unique
    work dirs) can report both sides at once; DuckDB gcovr over a shared checkout must stay serial,
    because concurrent gcovr races on the same ``*.gcov`` intermediates next to the sources.
    """
    common = dict(
        source_root=source_root,
        filters=filters,
        exclude_directories=exclude_directories,
        mirror_tree=mirror_tree,
        reporter=reporter,
        dialect=dialect,
    )
    if mirror_tree or reporter == "lcov":
        per_side = max(1, jobs // 2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            left_future = pool.submit(
                covered_lines_from_snapshot,
                left_snap,
                counter_dir,
                jobs=per_side,
                json_out=work / "left.json",
                **common,
            )
            right_future = pool.submit(
                covered_lines_from_snapshot,
                right_snap,
                counter_dir,
                jobs=per_side,
                json_out=work / "right.json",
                **common,
            )
            left, left_totals = left_future.result()
            right, right_totals = right_future.result()
        return left, right, _merge_denominator(left_totals, right_totals)
    left, left_totals = covered_lines_from_snapshot(
        left_snap, counter_dir, jobs=jobs, json_out=work / "left.json", **common
    )
    right, right_totals = covered_lines_from_snapshot(
        right_snap, counter_dir, jobs=jobs, json_out=work / "right.json", **common
    )
    return left, right, _merge_denominator(left_totals, right_totals)


def measure_pair(
    counter_dir: Path,
    left_side: Side,
    left_query: str,
    right_side: Side,
    right_query: str,
    *,
    source_root: Path,
    jobs: int = 12,
    filters: Sequence[str] = coverage.DEFAULT_FILTERS,
    exclude_directories: Sequence[str] = (),
    mirror_tree: bool = True,
    reporter: str = "gcovr",
    dialect: str = "postgres",
) -> PairCoverage:
    """Coverage of each half of a pair, reduced to a symmetric difference.

    Deliberately general in both axes, because the oracles being compared differ in *which* axis varies:

    * eqgen        same query text, two different objects   -> left_side != right_side
    * TLP / NoREC  two query texts, one table               -> left_query != right_query

    Holding the harness, engine, denominator and suite size identical across those is the only way the
    resulting percentages can be put in one table.

    Counters are snapshotted after each side (cheap). Line-level reports then run in parallel when safe:
    Postgres in-tree staging (``mirror_tree``) and lcov (unique work dirs). DuckDB gcovr over a shared
    checkout must stay serial — concurrent gcovr races on the same ``*.gcov`` intermediates next to
    sources.
    """
    with tempfile.TemporaryDirectory(prefix="eqgen-mc-pair-") as tmp:
        tmp_path = Path(tmp)
        left_snap = tmp_path / "left"
        right_snap = tmp_path / "right"

        coverage.zero_counters(counter_dir)
        left_side.run_isolated(left_query)
        coverage.take_snapshot(counter_dir, left_snap)

        coverage.zero_counters(counter_dir)
        right_side.run_isolated(right_query)
        coverage.take_snapshot(counter_dir, right_snap)

        left, right, instrumented = _diff_snapshots(
            counter_dir,
            left_snap,
            right_snap,
            tmp_path,
            source_root=source_root,
            jobs=jobs,
            filters=filters,
            exclude_directories=exclude_directories,
            mirror_tree=mirror_tree,
            reporter=reporter,
            dialect=dialect,
        )

    return PairCoverage(
        query=left_query,
        base_only=len(left - right),
        equivalent_only=len(right - left),
        both=len(left & right),
        divergent_lines=frozenset(left ^ right),
        instrumented_by_file=instrumented,
    )


def measure_setup(
    counter_dir: Path,
    build_base: "Callable[[], Side]",
    build_equivalent: "Callable[[], Side]",
    *,
    source_root: Path,
    jobs: int = 12,
    filters: Sequence[str] = coverage.DEFAULT_FILTERS,
    exclude_directories: Sequence[str] = (),
    mirror_tree: bool = True,
    reporter: str = "gcovr",
    dialect: str = "postgres",
    statements: int = 0,
    base_fork_copy_statements: Sequence[str] = (),
) -> tuple[SetupCoverage, Side, Side]:
    """Build both sides with the counters bracketed, and reduce the two builds to a symmetric difference.

    This is the *setup* surface: the DDL+DML that produces the base table against the DDL that produces
    the equivalence. It is a metamorphic pair in exactly the sense the query surface is — two inputs
    expected to yield the same data, and eqgen's row check is the oracle that says so — and it is the
    cleaner of the two, because :meth:`Database.build_equivalent` runs byte-identical
    ``base_setup_statements`` on both sides before the rename. That prefix cancels, so what survives is
    the composed object and nothing else.

    Both builds happen here rather than in the caller, because a snapshot is only meaningful if nothing
    else ran between the zero and the flush. The built sides are returned for the query surface to reuse:
    neither engine needs a rebuild, since :meth:`_PgSide.flush_builder` keeps the schema and DuckDB's
    objects live in a file that outlasts the builder process.

    Composition depth lands *here* whenever the root is a table or a materialized view. Those plan and
    execute the composed tree at DDL time and leave the query reading a finished heap, which is why the
    query surface alone showed no relationship between tree size and divergence across the 1,000-pair
    Postgres suite.

    ``base_fork_copy_statements`` (forks>1 only) creates the base side's ``t0``/``t1``/... copies for the
    *query* surface to reuse afterward. They run once the base snapshot above has already been taken, and
    the next ``zero_counters`` (right below) wipes whatever they touched before the equivalent side is
    built — so they cost nothing in either snapshot. They're deliberately not part of ``build_base``: the
    equivalent side has no per-fork "plain copy" counterpart to cancel them against, so counting the CTAS
    copies here would just add generic execution noise to the composition signal this function measures.
    """
    with tempfile.TemporaryDirectory(prefix="eqgen-mc-setup-") as tmp:
        tmp_path = Path(tmp)
        base_snap = tmp_path / "base"
        equivalent_snap = tmp_path / "equivalent"

        coverage.zero_counters(counter_dir)
        base = build_base()
        base.flush_builder()
        coverage.take_snapshot(counter_dir, base_snap)

        for stmt in base_fork_copy_statements:
            base.run_isolated(stmt)

        coverage.zero_counters(counter_dir)
        equivalent = build_equivalent()
        equivalent.flush_builder()
        coverage.take_snapshot(counter_dir, equivalent_snap)

        left, right, instrumented = _diff_snapshots(
            counter_dir,
            base_snap,
            equivalent_snap,
            tmp_path,
            source_root=source_root,
            jobs=jobs,
            filters=filters,
            exclude_directories=exclude_directories,
            mirror_tree=mirror_tree,
            reporter=reporter,
            dialect=dialect,
        )

    return (
        SetupCoverage(
            base_only=len(left - right),
            equivalent_only=len(right - left),
            both=len(left & right),
            divergent_lines=frozenset(left ^ right),
            instrumented_by_file=instrumented,
            statements=statements,
        ),
        base,
        equivalent,
    )


def tlp_pair(table: str, predicate: str) -> tuple[str, str]:
    """Ternary Logic Partitioning (Rigger and Su): a query, and the union of its three partitions.

    Under three-valued logic every row satisfies exactly one of ``p``, ``NOT p``, ``p IS NULL``, so the
    union returns the same multiset as the unpartitioned query. The predicate is parenthesised here for
    the same reason eqgen's renderer does it: an unparenthesised top-level ``OR`` would bind ``NOT`` to
    the wrong operand and the partition would stop being exhaustive.
    """
    original = f"SELECT * FROM {table}"
    partitioned = (
        f"SELECT * FROM {table} WHERE ({predicate})"
        f" UNION ALL SELECT * FROM {table} WHERE NOT ({predicate})"
        f" UNION ALL SELECT * FROM {table} WHERE ({predicate}) IS NULL"
    )
    return original, partitioned


def norec_pair(table: str, predicate: str) -> tuple[str, str]:
    """Non-optimising Reference Engine Construction (Rigger and Su): a filter, and a count that cannot
    be optimised into one.

    The first form lets the planner use indexes, pushdown and short-circuiting; the second forces the
    predicate to be evaluated once per row inside a projection. Both should agree on how many rows
    satisfy it, and the plans are deliberately dissimilar — which is exactly what makes it a metamorphic
    pair worth measuring.
    """
    optimised = f"SELECT * FROM {table} WHERE ({predicate})"
    unoptimised = f"SELECT SUM(CASE WHEN ({predicate}) THEN 1 ELSE 0 END) FROM {table}"
    return optimised, unoptimised


def iter_pairs(
    adapter: DialectAdapter,
    counter_dir: Path,
    *,
    source_root: Path,
    oracle: str,
    pairs: int,
    rows: int = 8,
    seed: int = 1,
    rich: bool = True,
    jobs: int = 12,
    filters: Sequence[str] = coverage.DEFAULT_FILTERS,
    exclude_directories: Sequence[str] = (),
    mirror_tree: bool = True,
    reporter: str = "gcovr",
    query_source: object = None,
    predicate_source: object = None,
    portable_only: bool = False,
    forks: int = 1,
) -> Iterator[tuple[Union[SetupCoverage, PairCoverage], dict[str, int]]]:
    """Measure one suite of the chosen *oracle*: its setup surface, then *pairs* query pairs.

    The first item yielded is always the :class:`SetupCoverage`, so a consumer can dispatch on type and
    a killed run still has the setup number for every suite it finished building.

    ``eqgen`` builds one object per suite and runs the same query against it and the base table. One
    object per suite on purpose: it makes the suite's divergence attributable to a known builder set,
    which is what turns MC into a per-builder ranking rather than a single aggregate.

    ``tlp`` and ``norec`` build no object at all — both halves run against the base table, and what
    differs is the query text. They exist so eqgen's figure has something to be compared against on the
    same engine, denominator, harness and suite size. Because both halves *are* the same table, their
    setup divergence is analytically empty rather than merely small, and is reported unmeasured.
    """
    table = adapter.rich_catalog("t") if rich else adapter.simple_catalog("t")
    seed_rows: Sequence[Row] = sample_rows(
        table, rows, seed=seed, allow_inf=adapter.supports_float_inf
    )
    name = table.get_sql_name()
    names = exposed_fork_names(forks, seed_name=name)
    build_side = _build_duck_side if adapter.name == "duckdb" else _build_pg_side
    queries = query_source if query_source is not None else RandomSelectSource()
    predicates_src = predicate_source if predicate_source is not None else RandomPredicateSource()
    surfaces = dict(
        source_root=source_root,
        jobs=jobs,
        filters=filters,
        exclude_directories=exclude_directories,
        mirror_tree=mirror_tree,
        reporter=reporter,
        dialect=adapter.name,
    )

    base: Optional[Side] = None
    other: Optional[Side] = None
    builders: dict[str, int] = {}
    try:
        if oracle == "eqgen":
            hidden = Table(hidden_base_name(table), table.get_column_list())
            # extra_builders() *is* the dialect-native set, so portable-only means both withholding
            # the classes and zeroing their weights -- the weighted shuffle would otherwise keep
            # drawing names that no longer resolve to a builder.
            natives = tuple(adapter.extra_builders())
            native_names = tuple(cls.__name__ for cls in natives)
            generator = EquivalenceGenerator(
                mc_config(
                    adapter.equivalence_config(),
                    dialect=adapter.name,
                    also_exclude=native_names if portable_only else (),
                ),
                predicate_source=predicates_src,
                emitter=adapter.emitter(),
                extra_builders=() if portable_only else natives,
            )
            equivalence = generator.generate_forks(hidden, seed=seed, exposed_names=names)
            statements = [statement.statement_text for statement in equivalence.setup_statements]
            builders = dict(generator.builders_used)
            # forks>1: the base side only gets the plain table for the setup measurement (see
            # measure_setup's docstring for why) -- its t0/t1/... copies are created afterward,
            # unmeasured, via base_fork_copy_statements, purely so the query surface below has them.
            fork_copy_stmts = fork_copy_statements(name, names, adapter=adapter) if forks > 1 else ()
            # Both sides are built inside measure_setup, so nothing runs between its zero and its
            # snapshot. They come back live and the query surface reuses them un-rebuilt.
            setup, base, other = measure_setup(
                counter_dir,
                lambda: build_side(adapter, table, seed_rows),
                lambda: build_side(adapter, table, seed_rows, statements=statements, exposed_names=names),
                statements=len(statements),
                base_fork_copy_statements=fork_copy_stmts,
                **surfaces,
            )
            yield (setup, builders)
            yield from _measure_queries(
                counter_dir, base, other, queries, table, seed=seed, pairs=pairs,
                builders=builders, surfaces=surfaces,
            )
        else:
            base = build_side(adapter, table, seed_rows)
            # Not measured: both halves of every pair run against this one object, so the two setups
            # are the same statements in the same schema and the symmetric difference is empty by
            # construction. Yielded anyway to keep the record shape uniform across oracles.
            yield (SetupCoverage(base_only=0, equivalent_only=0, both=0, measured=False), builders)
            build = tlp_pair if oracle == "tlp" else norec_pair
            predicates = _predicates(table, pairs, seed, source=predicates_src)
            measured = 0
            for predicate in predicates:
                left, right = build(name, predicate)
                try:
                    pair = measure_pair(counter_dir, base, left, base, right, **surfaces)
                except Exception as exc:  # a rejected query is not a measurement -- see _measure_queries
                    print(f"    dropped query ({type(exc).__name__}: {str(exc).splitlines()[0][:90]})")
                    continue
                measured += 1
                yield (pair, builders)
                if measured >= pairs:
                    return
    finally:
        if base is not None:
            base.close()
        if other is not None:
            other.close()


def _measure_queries(
    counter_dir: Path,
    base: Side,
    other: Side,
    queries: object,
    table: Table,
    *,
    seed: int,
    pairs: int,
    builders: dict[str, int],
    surfaces: dict,
) -> Iterator[tuple[PairCoverage, dict[str, int]]]:
    """Yield *pairs* successfully measured query pairs, drawing replacements for rejected queries.

    A query the engine refuses is not a measurement, so it must not consume one of the *pairs* slots
    and must not abort the suite. Both used to happen: ``run_isolated`` executes raw psycopg and lets
    the error propagate, so a single query Postgres rejected -- ``function max(uuid) does not exist``
    is a real example from a SQLancer++ stream -- discarded every pair already measured in that suite
    and biased the surviving suites toward those that happened to draw only valid SQL.

    Dropping and redrawing keeps the sample size honest: the arm still reports *pairs* measurements per
    suite, all of them real. The count of drops is printed rather than hidden, because a stream that is
    mostly invalid is a fact about the generator worth seeing.

    Note the query source is asked for an unbounded stream rather than exactly *pairs* queries, since
    replacements have to come from somewhere; a source that runs dry before *pairs* succeed raises, and
    the suite is then skipped like any other failure.
    """
    measured = dropped = 0
    for query in queries.iter_queries(table, seed=seed, limit=None):
        try:
            pair = measure_pair(counter_dir, base, query, other, query, **surfaces)
        except Exception as exc:
            dropped += 1
            print(f"    dropped query ({type(exc).__name__}: {str(exc).splitlines()[0][:90]})")
            continue
        measured += 1
        yield pair, builders
        if measured >= pairs:
            if dropped:
                print(f"    ({dropped} query/queries dropped as engine-rejected, not counted)")
            return
    raise RuntimeError(
        f"query source ran dry after {measured}/{pairs} measured ({dropped} dropped)"
    )


def _predicates(table: Table, count: int, seed: int, *, source: object = None) -> list[str]:
    """Distinct predicates for the TLP and NoREC arms, from the same source eqgen's builders use.

    Using eqgen's predicate source rather than SQLancer++'s own keeps the *only* difference between the
    three arms the oracle itself. Feeding TLP better predicates than eqgen's builders get would measure
    the predicate generator, not the oracle.
    """
    source = source if source is not None else RandomPredicateSource()
    found: list[str] = []
    for offset in range(count * 20):
        text = source.boolean_predicate(table, seed=seed * 1000 + offset)
        if text and text not in found:
            found.append(text)
        if len(found) >= count:
            break
    return found


def _atomic_write_json(path: Path, payload: object) -> None:
    """Write JSON via a temp file + replace so a kill mid-write leaves the previous file intact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def _dump_lines(path: Path, lines: frozenset[tuple[str, int]] | set[tuple[str, int]]) -> None:
    """Write a divergent line set as gzipped ``file:line``, one per row.

    Kept because the reported metric is a *union* and nothing downstream can reconstruct one from
    per-pair counts. Every question that comes after a run — per-component MC, whether one arm's lines
    are a subset of another's, which categories contribute uniquely divergent lines — is a set operation
    on these files. Roughly 100 KB gzipped per suite against ~4 minutes to re-measure one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for file_key, line in sorted(lines):
            handle.write(f"{file_key}:{line}\n")


def _merge_denominator(*totals: dict[str, int]) -> dict[str, int]:
    """Per-file maxima across several reports — :func:`gcov.fixed_denominator`'s rule, incrementally.

    A file missing from one report was simply zero-covered there, so the max is its real size.
    """
    merged: dict[str, int] = {}
    for mapping in totals:
        for name, count in mapping.items():
            if count > merged.get(name, 0):
                merged[name] = count
    return merged


def _line_mc(union: set[tuple[str, int]], instrumented: int) -> float:
    return 100.0 * len(union) / max(1, instrumented)


def _flush_mc_checkpoint(
    *,
    out: Optional[Path],
    records: list[coverage.JsonDict],
    suite_records: list[coverage.JsonDict],
    query_union: set[tuple[str, int]],
    setup_union: set[tuple[str, int]],
    instrumented: int,
    successful: int,
    skipped: int,
    started: float,
) -> None:
    """Persist running line MC for both surfaces so a killed run still has the numbers.

    Cheap relative to a pair (~ms): both unions are already maintained in memory.
    """
    measured = records + suite_records
    pairs = sum(1 for r in measured if r["phase"] == "query")
    query_mc = _line_mc(query_union, instrumented)
    setup_mc = _line_mc(setup_union, instrumented)
    print(
        f"  running line MC       : query {query_mc:.3f}%  setup {setup_mc:.3f}%  "
        f"({len(query_union):,} / {len(setup_union):,} of {instrumented:,}, {pairs} pairs)"
    )
    if out is None:
        return
    _atomic_write_json(out, measured)
    checkpoint = {
        "pairs": pairs,
        "successful_suites": successful,
        "skipped_suites": skipped,
        "instrumented": instrumented,
        "query_union_divergent": len(query_union),
        "query_line_mc_percent": round(query_mc, 6),
        "setup_union_divergent": len(setup_union),
        "setup_line_mc_percent": round(setup_mc, 6),
        "elapsed_min": round((time.monotonic() - started) / 60.0, 3),
    }
    _atomic_write_json(out.with_name("checkpoint.json"), checkpoint)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Metamorphic coverage of eqgen's equivalent pairs.")
    parser.add_argument("--dialect", default="postgres", choices=("postgres", "duckdb"))
    parser.add_argument(
        "--oracle",
        default="eqgen",
        choices=("eqgen", "tlp", "norec"),
        help="which metamorphic relation to measure (default: eqgen's object equivalence)",
    )
    parser.add_argument("--pairs", type=int, default=10, help="queries per suite (default: 10; ~25–100s each)")
    parser.add_argument(
        "--suites",
        type=int,
        default=1,
        help="number of *successful* suites to measure (failed builds are skipped/retried; default: 1)",
    )
    parser.add_argument("--seed", type=int, default=1, help="master seed")
    parser.add_argument("--rows", type=int, default=8)
    parser.add_argument("--rich", action="store_true", help="use the multi-type catalog")
    parser.add_argument("--source", type=Path, default=None, help="instrumented source tree / DuckDB checkout")
    parser.add_argument("--jobs", type=int, default=12)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="write per-pair JSON here (flushed after every pair; also writes sibling checkpoint.json "
        "with running line MC)",
    )
    parser.add_argument(
        "--builders",
        default="full",
        choices=("full", "portable"),
        help="portable drops the dialect-native builders, keeping arms dialect-independent",
    )
    parser.add_argument(
        "--generator",
        default="example",
        choices=("example", "sqlancerpp"),
        help="workload query source (default: eqgen's bundled example generator)",
    )
    parser.add_argument(
        "--predicates",
        default="example",
        choices=("example", "typed", "sqlancerpp", "none"),
        help="source of the predicates embedded in generated objects (default: example)",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=None,
        help="harvest queries/predicates once into this directory and replay them. Required for "
        "sweeps: the SQLancer++ sources ignore their seed, so without a cache two arms differ in "
        "their inputs as well as in the variable under test",
    )
    parser.add_argument(
        "--scope",
        default="all",
        choices=("all", "compiler", "optimizer"),
        help="denominator scope: all (DuckDB: MC-paper gcovr src/; Postgres: paper lcov), "
        "compiler (parse/plan/optimize via gcovr), or optimizer only",
    )
    parser.add_argument(
        "--forks",
        type=int,
        default=1,
        help="same-base equivalence trees to expose as t0,t1,... instead of one t (default: 1). "
        "Requires --cache: the live sqlancerpp path is not fork-aware",
    )
    args = parser.parse_args(argv)

    if args.forks < 1:
        raise SystemExit("--forks must be >= 1")
    if args.forks > 1 and args.cache is None:
        raise SystemExit("--forks > 1 requires --cache (live sqlancerpp generation isn't fork-aware)")

    filters = coverage.filters_for(args.dialect, args.scope)
    # DuckDB MC matches nus-test/metamorphic_coverage (gcovr + src/ + their excludes).
    # Postgres has no official MC target; scope=all stays on paper full-tree lcov.
    if args.dialect == "duckdb":
        reporter = "gcovr"
        exclude_directories: tuple[str, ...] = (
            coverage.DUCKDB_MC_EXCLUDE_DIRECTORIES if args.scope == "all" else ()
        )
    else:
        reporter = "lcov" if args.scope == "all" else "gcovr"
        exclude_directories = ()
    if args.dialect == "duckdb":
        counter_dir, source_root = _resolve_duckdb_trees(args.source)
        mirror_tree = False
        from eqgen.dialects.duckdb.adapter import DuckDBAdapter

        adapter: DialectAdapter = DuckDBAdapter(execution_backend="cli")
        # Budget: gcovr on coverage-artifact; refine after smoke.
        seconds_per_pair = 90
    else:
        counter_dir = Path(args.source or os.environ.get(PG_SOURCE_ENV) or PG_DEFAULT_SOURCE).expanduser()
        source_root = counter_dir
        mirror_tree = True
        if not any(counter_dir.rglob("*.gcno")):
            raise SystemExit(f"{counter_dir} is not an instrumented source tree.")
        from eqgen.dialects.postgres.adapter import PostgresAdapter

        adapter = PostgresAdapter()
        seconds_per_pair = 25 if reporter == "gcovr" else 40

    print(f"engine   : {adapter.engine_banner()}")
    print(f"source   : {counter_dir}")
    if source_root != counter_dir:
        print(f"root     : {source_root}")
    print(f"oracle   : {args.oracle}")
    print(f"scope    : {args.scope}  ({', '.join(filters)})")
    print(f"reporter : {reporter}")
    if exclude_directories:
        print(f"excludes : {', '.join(exclude_directories)}  (MC-paper gcovr.cfg)")
    excluded = _EXCLUDED_BUILDERS_DUCKDB if args.dialect == "duckdb" else _EXCLUDED_BUILDERS
    print(f"excluded : {', '.join(excluded)}")
    print(
        f"budget   : {args.suites} successful suite(s) x {args.pairs} pairs x ~{seconds_per_pair}s "
        f"= ~{args.suites * args.pairs * seconds_per_pair / 60:.0f} min "
        f"(failed builds are skipped; retries until {args.suites} succeed)\n"
    )

    mc_table = adapter.rich_catalog("t") if args.rich else adapter.simple_catalog("t")
    if "sqlancerpp" in (args.generator, args.predicates):
        # Must happen before either plugin constructs the shared engine, so the jar installs eqgen's
        # fixed schema rather than generating its own random one. The jar drives a throwaway Docker
        # Postgres, never the instrumented cluster -- its own probing and deliberate error-triggering
        # would otherwise land in the counters we are about to zero.
        from eqgen.fuzz.cli import _configure_sqlancerpp_schema

        _configure_sqlancerpp_schema(args.forks, seed_name=mc_table.get_sql_name(), dialect=args.dialect)
    print(f"generator: {args.generator}   predicates: {args.predicates}   builders: {args.builders}")
    fork_names = exposed_fork_names(args.forks, seed_name=mc_table.get_sql_name())
    if args.forks != 1:
        print(f"forks    : {args.forks}  ({', '.join(fork_names)})")

    mc_queries: object = None
    mc_predicates: object = None
    if args.cache is not None:
        mc_queries, mc_predicates = harvest_generator_inputs(
            args.cache,
            adapter=adapter,
            table=mc_table,
            generator=args.generator,
            predicates=args.predicates,
            queries_wanted=max(args.pairs * args.suites, args.pairs) * 2,
            predicates_wanted=500,
            exposed_names=fork_names,
        )
    elif "sqlancerpp" in (args.generator, args.predicates):
        from eqgen.fuzz.cli import predicate_source_for, query_source

        mc_queries = query_source(None, args.generator, dialect=args.dialect)
        mc_predicates = predicate_source_for(args.predicates, dialect=args.dialect)
        print("warning: no --cache, so this run's queries/predicates are not reproducible", file=sys.stderr)
    elif args.predicates != "example":
        from eqgen.fuzz.cli import predicate_source_for

        mc_predicates = predicate_source_for(args.predicates, dialect=args.dialect)

    started = time.monotonic()
    records: list[coverage.JsonDict] = []
    query_union: set[tuple[str, int]] = set()
    setup_union: set[tuple[str, int]] = set()
    # Pinned per-file, merged across every measurement, then summed once at report time. See
    # Divergence.instrumented_by_file for why a scalar max here drifts.
    denominator: dict[str, int] = {}
    successful = 0
    attempt = 0
    # Cap retries so a systemic failure cannot spin forever.
    max_attempts = max(args.suites * 20, args.suites + 5)
    skipped = 0
    lines_dir = args.out.with_name("lines") if args.out else None
    while successful < args.suites and attempt < max_attempts:
        suite_seed = args.seed + attempt
        attempt += 1
        print(f"suite {successful} (seed {suite_seed}, attempt {attempt})")
        suite_records: list[coverage.JsonDict] = []
        # Accumulated per suite and merged into the run-wide unions only once the suite completes.
        # A suite that dies halfway has its records dropped, so folding its lines into the union
        # would report a numerator drawn from more suites than the stated pair count covers.
        suite_query_union: set[tuple[str, int]] = set()
        suite_setup_union: set[tuple[str, int]] = set()
        pair_index = 0
        try:
            for item, builders in iter_pairs(
                adapter,
                counter_dir,
                source_root=source_root,
                oracle=args.oracle,
                pairs=args.pairs,
                rows=args.rows,
                seed=suite_seed,
                rich=args.rich,
                jobs=args.jobs,
                filters=filters,
                exclude_directories=exclude_directories,
                mirror_tree=mirror_tree,
                reporter=reporter,
                query_source=mc_queries,
                predicate_source=mc_predicates,
                portable_only=args.builders == "portable",
                forks=args.forks,
            ):
                common = {
                    "suite": successful,
                    "seed": suite_seed,
                    "dialect": args.dialect,
                    "oracle": args.oracle,
                    "base_only": item.base_only,
                    "equivalent_only": item.equivalent_only,
                    "both": item.both,
                    "divergent": item.divergent,
                    "builders": builders,
                    "scope": args.scope,
                    "reporter": reporter,
                }
                if isinstance(item, SetupCoverage):
                    if builders:
                        print(f"  builders: {', '.join(f'{k} {v}' for k, v in sorted(builders.items()))}")
                    if item.measured:
                        suite_setup_union |= item.divergent_lines
                        denominator = _merge_denominator(denominator, item.instrumented_by_file)
                        print(
                            f"  setup       divergent {item.divergent:6}  both {item.both:6}  "
                            f"ratio {100 * item.divergence_ratio:5.2f}%   "
                            f"{item.statements} equivalence statement(s)"
                        )
                    else:
                        print("  setup       divergent      0  (both sides are the same object — analytic)")
                    suite_records.append(
                        {**common, "phase": "setup", "statements": item.statements, "measured": item.measured}
                    )
                else:
                    suite_query_union |= item.divergent_lines
                    denominator = _merge_denominator(denominator, item.instrumented_by_file)
                    print(
                        f"  pair {pair_index:3}   divergent {item.divergent:6}  both {item.both:6}  "
                        f"ratio {100 * item.divergence_ratio:5.2f}%   {item.query[:52]}"
                    )
                    suite_records.append({**common, "phase": "query", "query": item.query})
                    pair_index += 1
                _flush_mc_checkpoint(
                    out=args.out,
                    records=records,
                    suite_records=suite_records,
                    query_union=query_union | suite_query_union,
                    setup_union=setup_union | suite_setup_union,
                    instrumented=sum(denominator.values()),
                    successful=successful,
                    skipped=skipped,
                    started=started,
                )
            if pair_index < args.pairs:
                raise RuntimeError(f"suite produced only {pair_index}/{args.pairs} pairs")
            records.extend(suite_records)
            query_union |= suite_query_union
            setup_union |= suite_setup_union
            if lines_dir is not None:
                _dump_lines(lines_dir / f"suite{successful:03d}_setup.txt.gz", suite_setup_union)
                _dump_lines(lines_dir / f"suite{successful:03d}_query.txt.gz", suite_query_union)
            successful += 1
        except Exception as exc:  # failed builds do not count toward --suites
            skipped += 1
            print(f"  skipped ({type(exc).__name__}: {exc})")

    if successful < args.suites:
        print(
            f"warning: only {successful}/{args.suites} successful suites "
            f"after {attempt} attempts ({skipped} skipped)"
        )

    if not records:
        raise SystemExit("no pairs measured")

    queries = [r for r in records if r["phase"] == "query"]
    setups = [r for r in records if r["phase"] == "setup" and r["measured"]]
    divergent = [int(r["divergent"]) for r in queries]
    union = [int(r["divergent"]) + int(r["both"]) for r in queries]
    print()
    print(
        f"pairs measured        : {len(queries)} "
        f"({successful} suites x {args.pairs} pairs, {skipped} skipped) "
        f"in {(time.monotonic() - started) / 60:.1f} min"
    )
    print(f"divergent lines/pair  : mean {sum(divergent) / len(divergent):.0f}, min {min(divergent)}, max {max(divergent)}")
    print(f"divergence ratio      : mean {100 * sum(divergent) / max(1, sum(union)):.2f}% of lines the pair touched")
    if setups:
        setup_divergent = [int(r["divergent"]) for r in setups]
        print(
            f"divergent lines/setup : mean {sum(setup_divergent) / len(setup_divergent):.0f}, "
            f"min {min(setup_divergent)}, max {max(setup_divergent)}  ({len(setups)} suites)"
        )
    print()
    print(f"  --- the reported metric: metamorphic coverage of the SUITE (scope={args.scope}) ---")
    instrumented = sum(denominator.values())
    print(f"  instrumented lines       : {instrumented:,}   (pinned per-file across {len(denominator):,} files)")
    print(f"  query  union / line MC   : {len(query_union):>9,}   {_line_mc(query_union, instrumented):7.3f}%")
    if setups:
        print(f"  setup  union / line MC   : {len(setup_union):>9,}   {_line_mc(setup_union, instrumented):7.3f}%")
        combined = query_union | setup_union
        print(f"  either union / line MC   : {len(combined):>9,}   {_line_mc(combined, instrumented):7.3f}%")
        only_setup = setup_union - query_union
        print(
            f"  setup-only               : {len(only_setup):>9,}   {_line_mc(only_setup, instrumented):7.3f}%"
            "   (divergence the query surface cannot see)"
        )
    else:
        print("  setup  union / line MC   :         0     0.000%   (both sides one object — analytic)")
    if args.dialect == "duckdb" and args.scope == "all":
        print("  (denominator: MC-paper gcovr src/ + exclude-directories; DuckDB 1.5.5)")
        print("  (Argus Table 3 uses a different lcov denom — not directly comparable)")
    elif args.dialect == "postgres" and args.scope == "all" and reporter == "lcov":
        print("  (denominator: paper full-tree lcov — SQLancer++ artifact capture, not gcovr filters)")
    elif args.scope != "all":
        print(f"  (scope={args.scope} is narrower than Argus Table 3's whole-engine DuckDB denominator)")
    if args.out:
        _atomic_write_json(args.out, records)
        _atomic_write_json(
            args.out.with_name("checkpoint.json"),
            {
                "pairs": len(queries),
                "successful_suites": successful,
                "skipped_suites": skipped,
                "instrumented": instrumented,
                "query_union_divergent": len(query_union),
                "query_line_mc_percent": round(_line_mc(query_union, instrumented), 6),
                "setup_union_divergent": len(setup_union),
                "setup_line_mc_percent": round(_line_mc(setup_union, instrumented), 6),
                "either_union_divergent": len(query_union | setup_union),
                "either_line_mc_percent": round(_line_mc(query_union | setup_union, instrumented), 6),
                "setup_only_divergent": len(setup_union - query_union),
                "elapsed_min": round((time.monotonic() - started) / 60.0, 3),
                "final": True,
            },
        )
        print(f"wrote {args.out} and {args.out.with_name('checkpoint.json')}")
        if lines_dir is not None:
            print(f"wrote per-suite divergent line sets to {lines_dir}")
    return 0


if __name__ == "__main__":
    random.seed(0)
    sys.exit(main())
