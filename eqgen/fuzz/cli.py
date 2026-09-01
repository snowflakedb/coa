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

"""The loop, and the command line::

    python -m eqgen.fuzz.cli --dialect duckdb --rich --rounds 12

Nothing external is required to run it: ``pip install -r requirements.txt`` and go. A real
query generator can be selected with ``--generator`` (see ``eqgen.generators``); the default is the
bundled example.

The loop is short because :func:`~eqgen.fuzz.round.run_round` does the work. What is left here is
loop-shaped: pick a seed, make the run directory, open a log per round, add up the results, write
the repro files.

One guard worth naming. A round that will not build is normal — a generated predicate can compare
two types the engine refuses to compare — so one failure takes a new seed and moves on. Ten in a row
means *every* object is failing, which is our bug rather than bad luck, so the run stops loudly
instead of spinning and reporting nothing.
"""

from __future__ import annotations

import argparse
import itertools
import os
import random
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Sequence

from eqgen.core.catalog import Table
from eqgen.equivalence.generator import EquivalenceGenerator
from eqgen.generators.example_generator import RandomPredicateSource, RandomSelectSource
from eqgen.fuzz.adapter import DialectAdapter
from eqgen.fuzz.database import Row, exposed_fork_names
from eqgen.fuzz.journal import QueryJournal, sample_catalog, sample_rows
from eqgen.fuzz.report import FuzzReport, record_round, write_finding
from eqgen.fuzz.round import RoundOutcome, run_round
from eqgen.fuzz.schedule import RoundTimeScheduler, default_min_seconds, flat_scheduler
from eqgen.fuzz.sweep import sweep_all
from eqgen.plugins import CorpusSource, PredicateSource, QuerySource

#: Run directories live inside the package, at ``eqgen/log``, resolved from this file rather than the
#: working directory — so ``python -m eqgen.fuzz.cli`` writes to the same place from anywhere, and the
#: evidence sits next to the code that produced it instead of in a home directory nobody looks in.
DEFAULT_LOG_DIR = Path(__file__).resolve().parent.parent / "log"

DEFAULT_ROUND_SECONDS = 5.0


def load_adapter(dialect: str, *, duckdb_backend: str = "cli") -> DialectAdapter:
    """Instantiate a dialect adapter by name.

    *duckdb_backend* is ``"cli"`` (default fuzz path) or ``"wheel"`` (offline tests). Ignored for
    every other dialect.
    """
    if dialect == "duckdb":
        from eqgen.dialects.duckdb.adapter import DuckDBAdapter

        return DuckDBAdapter(execution_backend=duckdb_backend)
    if dialect == "postgres":
        from eqgen.dialects.postgres.adapter import PostgresAdapter

        return PostgresAdapter()
    if dialect == "mysql":
        from eqgen.dialects.mysql.adapter import MySqlAdapter

        return MySqlAdapter()
    if dialect == "mariadb":
        from eqgen.dialects.mysql.adapter import MariaDbAdapter

        return MariaDbAdapter()
    if dialect == "tidb":
        from eqgen.dialects.tidb.adapter import TiDbAdapter

        return TiDbAdapter()
    if dialect == "dolt":
        from eqgen.dialects.dolt.adapter import DoltAdapter

        return DoltAdapter()
    if dialect == "cratedb":
        from eqgen.dialects.cratedb.adapter import CrateDbAdapter

        return CrateDbAdapter()
    if dialect == "sqlite":
        from eqgen.dialects.sqlite.adapter import SqliteAdapter

        return SqliteAdapter()
    if dialect == "clickhouse":
        from eqgen.dialects.clickhouse.adapter import ClickHouseAdapter

        return ClickHouseAdapter()
    raise SystemExit(
        f"unknown dialect {dialect!r} (available: duckdb, postgres, mysql, mariadb, tidb, dolt, cratedb, sqlite, clickhouse)"
    )


def _ensure_duckdb_cli(*, download: bool) -> None:
    """Refresh or locate the DuckDB CLI before a fuzz run, matching dbfuzz's per-run fetch."""
    from eqgen.dialects.duckdb import cli

    if download:
        print("[duckdb] downloading latest main CLI from artifacts.duckdb.org ...", flush=True)
        path = cli.ensure_latest_duckdb_cli()
    else:
        path = cli.resolve_duckdb_cli()
    library_version, source_id = cli.engine_version(path)
    print(f"[duckdb] using {path} — version {library_version} ({source_id})", flush=True)


def _ensure_clickhouse(*, download: bool) -> None:
    """Refresh or locate the ClickHouse binary before a fuzz run."""
    from eqgen.dialects.clickhouse import cluster

    if download:
        print("[clickhouse] downloading latest master from builds.clickhouse.com ...", flush=True)
        path = cluster.ensure_latest_clickhouse()
    else:
        path = cluster.clickhouse_binary()
    version = cluster.engine_version(path)
    print(f"[clickhouse] using {path} — version {version}", flush=True)


def _run_directory(root: Path, dialect: str) -> Path:
    """A fresh directory per run, so concurrent runs cannot overwrite each other's evidence."""
    root = root.expanduser()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for suffix in itertools.count():
        candidate = root / (f"{dialect}_{stamp}" + (f"_{suffix}" if suffix else ""))
        if not candidate.exists():
            candidate.mkdir(parents=True)
            return candidate
    raise AssertionError("unreachable")


def _fixed_catalog(adapter: DialectAdapter, catalog: str, name: str = "t") -> Table:
    if catalog == "rich":
        return adapter.rich_catalog(name)
    if catalog == "simple":
        return adapter.simple_catalog(name)
    raise ValueError(f"fixed catalog mode expected 'simple' or 'rich', got {catalog!r}")


def _round_catalog_and_rows(
    adapter: DialectAdapter,
    *,
    catalog: str,
    fixed_table: Optional[Table],
    row_count: int,
    seed: int,
    seed_name: str = "t",
) -> tuple[Table, list[Row]]:
    """Resolve the seed table and awkward rows for one round.

    *fixed_table* (tests / explicit) wins. Otherwise ``catalog='random'`` samples a signature;
    ``simple`` / ``rich`` use the adapter's fixed catalogs. Rows are always freshly sampled.
    """
    if fixed_table is not None:
        table = fixed_table
    elif catalog == "random":
        table = sample_catalog(adapter, seed_name, seed=seed)
    else:
        table = _fixed_catalog(adapter, catalog, seed_name)
    return table, sample_rows(
        table, row_count, seed=seed, allow_inf=adapter.supports_float_inf
    )


def run_fuzz(
    adapter: DialectAdapter,
    table: Optional[Table] = None,
    rows: Optional[Sequence[Row]] = None,
    *,
    catalog: str = "random",
    row_count: int = 8,
    query_source: QuerySource,
    predicate_source: Optional[PredicateSource] = None,
    rounds: Optional[int] = None,
    seed: Optional[int] = None,
    workdir: Optional[Path] = None,
    max_consecutive_unbuildable: int = 10,
    verbose: bool = True,
    round_hook: Optional[Callable[[int, RoundOutcome], None]] = None,
    scheduler: Optional[RoundTimeScheduler] = None,
    forks: int = 1,
    plan_fingerprint: Optional[Callable[..., Optional[str]]] = None,
) -> FuzzReport:
    """Run the loop until *rounds* is reached, or until interrupted.

    *catalog* selects the seed signature each round: ``random`` (default), ``simple``, or ``rich``.
    Passing an explicit *table* pins that signature for every round (unit tests); *rows* only
    supplies the row count when given. Rows are always re-sampled from the round seed so payload
    values vary even with a fixed catalog.

    *scheduler* caps (and, when not flat, scales) the query-phase wall clock per round. The default
    construction in :func:`main` is a flat ``--round-seconds`` budget; pass ``--schedule`` for
    complexity scaling. ``None`` disables the deadline (consume the query source until it ends) —
    useful for unit tests that pass a finite query list via a custom source.

    *round_hook* is called once per round, with the round number and its outcome, after the round's
    journal is closed and before its results are classified. It exists so something outside can
    observe the loop without the loop knowing what it is — coverage measurement used it to sample the
    engine there. Passing a callable in keeps the dependency pointing that way round: nothing here
    imports the observer, which is why the observer could be moved out of the tree entirely.

    *plan_fingerprint* is the same pattern for distinct-plan counting: the caller supplies a
    fingerprinter and the harness never learns what a plan is. Supplying one is what turns plan
    collection on; there is no separate flag to keep in step with it.
    """
    if catalog not in ("random", "simple", "rich"):
        raise ValueError(f"catalog must be random|simple|rich, got {catalog!r}")
    run_dir = _run_directory(workdir or DEFAULT_LOG_DIR, adapter.name)
    master_seed = seed if seed is not None else random.randrange(2**31)
    random.seed(master_seed)
    n_rows = row_count if rows is None else max(len(rows), 3)
    seed_name = table.get_sql_name() if table is not None else "t"

    if verbose:
        print(f"engine   : {adapter.engine_banner()}")
        print(f"run dir  : {run_dir}")
        print(f"seed     : {master_seed}   (rerun with --seed {master_seed} to replay this sequence)")
        catalog_label = "fixed-table" if table is not None else catalog
        print(f"catalog  : {catalog_label}  (rows re-sampled each round, n={n_rows})")
        if forks != 1:
            print(f"forks    : {forks}  ({', '.join(exposed_fork_names(forks, seed_name=seed_name))})")
        if scheduler is not None:
            mode = "flat" if scheduler.flat else "scaled"
            print(
                f"schedule : {mode}, query-phase {scheduler.min_seconds:g}–{scheduler.max_seconds:g}s"
            )

    try:
        session = adapter.session_context()
    except Exception:  # - session context is a nicety; never let it abort a run
        session = []

    generator = EquivalenceGenerator(
        adapter.equivalence_config(),
        predicate_source=predicate_source,
        emitter=adapter.emitter(),
        extra_builders=adapter.extra_builders(),
    )

    names = exposed_fork_names(forks, seed_name=seed_name)
    report = FuzzReport()
    consecutive_unbuildable = 0
    try:
        for round_number in itertools.count():
            if rounds is not None and round_number >= rounds:
                break
            round_seed = random.randrange(2**31)
            # Owner-side generator watchdog: a streaming source (e.g. sqlancerpp) whose jar crashed
            # or wedged gets restarted here, before the round forks its worker — otherwise every
            # remaining round harvests 0 queries from a dead generator.
            heartbeat = getattr(query_source, "heartbeat", None)
            if callable(heartbeat):
                heartbeat()
            round_table, round_rows = _round_catalog_and_rows(
                adapter,
                catalog=catalog,
                fixed_table=table,
                row_count=n_rows,
                seed=round_seed,
                seed_name=seed_name,
            )
            journal = QueryJournal(
                run_dir / f"round{round_number}.log",
                header=f"round {round_number}, seed {round_seed}, source {query_source.name}",
            )
            try:
                col_desc = ", ".join(
                    f"{c.get_column_name()}:{c.get_data_type()}" for c in round_table.get_column_list()
                )
                journal.note(f"catalog: {round_table.get_sql_name()} ({col_desc})")
                # Stream until the round's time budget (or a finite source) stops consumption.
                queries = query_source.iter_queries(
                    round_table, seed=round_seed, limit=None, exposed_names=names
                )
                outcome = run_round(
                    adapter,
                    generator,
                    round_table,
                    round_rows,
                    queries,
                    seed=round_seed,
                    journal=journal,
                    scheduler=scheduler,
                    forks=forks,
                    plan_fingerprint=plan_fingerprint,
                )
            finally:
                journal.close()

            if round_hook is not None:
                round_hook(round_number, outcome)

            if outcome.setup_error is not None:
                consecutive_unbuildable += 1
                record_round(report, outcome, round_number)
                if verbose:
                    print(f"round {round_number} (seed {round_seed}): would not build ({outcome.setup_error}) -- retrying")
                if consecutive_unbuildable >= max_consecutive_unbuildable:
                    raise RuntimeError(
                        f"{consecutive_unbuildable} consecutive rounds failed to build an equivalence; "
                        f"last error: {outcome.setup_error}"
                    )
                continue
            consecutive_unbuildable = 0

            found = record_round(report, outcome, round_number)
            if outcome.object_diverged:
                # The generator, not the engine. Loud, because it invalidates the round and any
                # further round using the same rewrite.
                print(f"round {round_number} (seed {round_seed}): GENERATOR BUG -- object is not equivalent; discarding")
                continue

            for index, finding in enumerate(found):
                path = write_finding(
                    run_dir, adapter, round_table, round_rows, outcome, finding, index, session=session
                )
                detail = ""
                if finding.kind == "mismatch" and (finding.only_in_base or finding.only_in_equivalent):
                    detail = (
                        f" ({len(finding.only_in_base)} only in base, "
                        f"{len(finding.only_in_equivalent)} only in equivalent)"
                    )
                print(f"  -> {finding.kind.upper()}{detail}: repro written to {path}")

            if verbose:
                passed = sum(1 for result in outcome.results if result.is_pass)
                skipped = sum(1 for result in outcome.results if result.is_uncomparable or result.is_known_issue)
                budget = (
                    f"budget {outcome.round_budget_seconds:.1f}s, "
                    if outcome.round_budget_seconds is not None
                    else ""
                )
                # A pass bought with float tolerance is still a pass, but it is worth seeing as the
                # run goes rather than only in the closing summary.
                tolerated = sum(result.reconciled for result in outcome.results if result.is_pass)
                leniency = f", {tolerated} row(s) within float tolerance" if tolerated else ""
                print(
                    f"round {round_number} (seed {round_seed}): {budget}{len(outcome.results)} queries -> "
                    f"{passed} pass, {len(found)} finding(s), {skipped} skipped{leniency}"
                )
    except KeyboardInterrupt:
        print("\ninterrupted")
    if verbose:
        print()
        print(report.summary())
    return report


def _configure_sqlancerpp_schema(forks: int, *, seed_name: str = "t", dialect: str = "postgres") -> None:
    """Install fork relation names on the shared SQLancer++ engine before either plugin starts it.

    Generation (predicates) often touches the jar before the first ``iter_queries``, so the schema
    must be known at construction time — not only when queries arrive with ``exposed_names``.
    """
    from eqgen.fuzz.database import exposed_fork_names
    from eqgen.generators.sqlancerpp import shared_engine

    shared_engine(schema_names=exposed_fork_names(forks, seed_name=seed_name), dialect=dialect)


def _warm_sqlancerpp(table: Table, forks: int, *, dialect: str) -> None:
    """Start the throwaway jar/container before the first round so Docker boot is not on budget."""
    from eqgen.fuzz.database import exposed_fork_names
    from eqgen.generators.sqlancerpp import shared_engine

    names = exposed_fork_names(forks, seed_name=table.get_sql_name())
    eng = shared_engine(schema_names=names, dialect=dialect)
    eng.start(table, exposed_names=names)


def predicate_source_for(name: str, *, dialect: str = "postgres") -> Optional[PredicateSource]:
    """The predicate source named on the command line.

    Separate from ``--generator`` on purpose: a split predicate is embedded in the generated object's DDL
    while a workload query only reads it, so the two plugin points are worth choosing independently. When
    both name ``sqlancerpp`` they share one jar — see ``generators.sqlancerpp.shared_engine``.

    Useful mixes: ``--generator sqlancerpp --predicates typed`` (jar workload + typed embeds) and
    ``--generator sqlancerpp --predicates none`` (no embeds). The reverse (``typed`` queries aren't a
    generator; use ``random`` + ``sqlancerpp`` predicates) is also supported.

    *dialect* is forwarded to sources that print engine-specific SQL (``typed``); others ignore it.
    """
    if name == "none":
        return None
    if name == "sqlancerpp":
        from eqgen.generators.sqlancerpp import SqlancerppPredicateSource

        return SqlancerppPredicateSource(dialect=dialect)
    if name == "typed":
        from eqgen.generators.typed_predicate import TypedPredicateSource

        return TypedPredicateSource(dialect=dialect)
    if name == "example":
        return RandomPredicateSource()
    raise SystemExit(f"unknown predicate source {name!r} (available: example, typed, none)")


def query_source(
    corpus: Optional[Path],
    generator: str = "random",
    *,
    dialect: str = "postgres",
    shuffle_corpus: bool = False,
) -> QuerySource:
    """Replay a corpus if one was given, otherwise generate with the named generator.

    A corpus wins over ``--generator``: replaying a recorded round is how a finding from *any* generator
    is reproduced, so it has to be available regardless of which one produced it.
    """
    if corpus is not None:
        source = CorpusSource.from_path(corpus)
        if shuffle_corpus:
            return CorpusSource(source.queries, name=f"{source.name}+shuffle", shuffle=True)
        return source
    if generator == "sqlancerpp":
        from eqgen.generators.sqlancerpp import SqlancerppSource

        return SqlancerppSource(dialect=dialect)
    if generator == "random":
        return RandomSelectSource()
    raise SystemExit(f"unknown generator {generator!r} (available: random)")


def scheduler_from_args(
    *,
    round_seconds: float,
    min_round_seconds: Optional[float],
    schedule: bool = False,
) -> RoundTimeScheduler:
    """Build the round scheduler from CLI knobs. Complexity scaling is off unless *schedule*."""
    if not schedule:
        return flat_scheduler(round_seconds)
    minimum = default_min_seconds(round_seconds) if min_round_seconds is None else min_round_seconds
    return RoundTimeScheduler(max_seconds=round_seconds, min_seconds=minimum)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Differential fuzzing over generated row-equivalent objects.")
    parser.add_argument("--dialect", default="duckdb", help="engine to test (default: duckdb)")
    parser.add_argument("--rounds", type=int, default=None, help="stop after N rounds (default: run until interrupted)")
    parser.add_argument(
        "--round-seconds",
        type=float,
        default=DEFAULT_ROUND_SECONDS,
        help=f"max query-phase wall clock per round in seconds (default: {DEFAULT_ROUND_SECONDS:g})",
    )
    parser.add_argument(
        "--min-round-seconds",
        type=float,
        default=None,
        help="min query-phase seconds for simple equivalences (default: max(3, round-seconds/5))",
    )
    parser.add_argument(
        "--schedule",
        action="store_true",
        help="scale query-phase time by equivalence complexity (default: every round gets the full --round-seconds)",
    )
    parser.add_argument("--seed", type=int, default=None, help="master seed, to replay a run")
    parser.add_argument("--rows", type=int, default=8, help="rows in the base table (default: 8)")
    parser.add_argument(
        "--forks",
        type=int,
        default=1,
        help="same-base equivalents to expose (1=t; 2+=t0,t1,… with join workload) (default: 1)",
    )
    parser.add_argument(
        "--catalog",
        choices=("random", "simple", "rich"),
        default=None,
        help="seed table signature: random each round (default), or fixed simple/rich",
    )
    parser.add_argument("--rich", action="store_true", help="alias for --catalog rich")
    parser.add_argument("--simple", action="store_true", help="alias for --catalog simple")
    parser.add_argument("--corpus", type=Path, default=None, help="replay queries from a file instead of generating them")
    parser.add_argument(
        "--shuffle-corpus",
        action="store_true",
        help="shuffle --corpus with the round seed so a short time budget draws a random prefix "
        "instead of always the same first queries (default: replay in file order)",
    )
    parser.add_argument(
        "--generator",
        default="random",
        help="query stream source (default: random, the bundled example generator). "
        "--predicates follows this unless set explicitly — see --predicates",
    )
    parser.add_argument("--no-predicates", action="store_true", help="shorthand for --predicates none")
    parser.add_argument(
        "--predicates",
        default=None,
        help="predicates embedded in generated object DDL (default: typed, unless --generator "
        "names a valid source here too, in which case that wins). Bundled: example, typed, none",
    )
    parser.add_argument("--workdir", type=Path, default=None, help=f"where to write run directories (default: {DEFAULT_LOG_DIR})")
    parser.add_argument("--sweep", action="store_true", help="validate each builder in isolation and exit")
    parser.add_argument("--sweep-seeds", type=int, default=20, help="seeds per builder when sweeping (default: 20)")
    parser.add_argument(
        "--duckdb-cli",
        type=Path,
        default=None,
        help="path to a duckdb CLI binary (sets EQGEN_DUCKDB_CLI; implies --no-download-duckdb)",
    )
    parser.add_argument(
        "--no-download-duckdb",
        action="store_true",
        help="do not refresh the DuckDB CLI from artifacts.duckdb.org (use cache or --duckdb-cli)",
    )
    parser.add_argument(
        "--clickhouse-bin",
        type=Path,
        default=None,
        help="path to a clickhouse binary (sets EQGEN_CLICKHOUSE_BIN; implies --no-download-clickhouse)",
    )
    parser.add_argument(
        "--no-download-clickhouse",
        action="store_true",
        help="do not refresh ClickHouse from builds.clickhouse.com (use cache, dbfuzz cache, or --clickhouse-bin)",
    )
    args = parser.parse_args(argv)

    if args.duckdb_cli is not None:
        os.environ["EQGEN_DUCKDB_CLI"] = str(args.duckdb_cli.expanduser())
        args.no_download_duckdb = True

    if args.clickhouse_bin is not None:
        os.environ["EQGEN_CLICKHOUSE_BIN"] = str(args.clickhouse_bin.expanduser())
        args.no_download_clickhouse = True

    if args.dialect == "duckdb":
        _ensure_duckdb_cli(download=not args.no_download_duckdb)
    elif args.dialect == "clickhouse":
        _ensure_clickhouse(download=not args.no_download_clickhouse)

    adapter = load_adapter(args.dialect)
    if args.rich and args.simple:
        print("use only one of --rich / --simple / --catalog", file=sys.stderr)
        return 2
    if args.catalog is not None:
        catalog = args.catalog
    elif args.rich:
        catalog = "rich"
    elif args.simple:
        catalog = "simple"
    else:
        catalog = "random"

    if args.predicates is not None:
        predicates = args.predicates
    elif args.generator in ("example", "typed", "sqlancerpp", "none"):
        predicates = args.generator  # follow --generator when it names a valid predicate source too
    else:
        predicates = "typed"
    predicates = "none" if args.no_predicates else predicates

    if args.sweep:
        # Sweeps need a stable signature so each builder sees the same types.
        table = _fixed_catalog(adapter, "rich" if catalog == "random" else catalog)
        rows = sample_rows(
            table, args.rows, seed=args.seed, allow_inf=adapter.supports_float_inf
        )
        # The same predicate source a real run would use. Without it every builder that embeds a
        # generated predicate declines on every seed and reports not_exercised — see sweep.py.
        print(f"sweeping builders on {adapter.engine_banner()} (predicates: {predicates})\n")
        worst = 0
        for result in sweep_all(
            adapter,
            table,
            rows,
            seeds=args.sweep_seeds,
            predicate_source=predicate_source_for(predicates, dialect=args.dialect),
        ):
            detail = result.first_divergence or result.first_failure or ""
            print(
                f"  {result.builder:34} {result.verdict:16} ok={result.ok:3} "
                f"skip={result.not_exercised:3} {detail[:60]}"
            )
            if result.not_equivalent:
                worst = 1
        return worst

    if args.shuffle_corpus and args.corpus is None:
        print("--shuffle-corpus requires --corpus", file=sys.stderr)
        return 2
    source = query_source(
        args.corpus, args.generator, dialect=args.dialect, shuffle_corpus=args.shuffle_corpus
    )
    if args.forks > 1 and args.corpus is not None:
        print(
            "warning: --forks>1 with --corpus; corpus SQL must already use t0/t1/… names",
            file=sys.stderr,
        )
    if args.forks < 1:
        print("--forks must be >= 1", file=sys.stderr)
        return 2
    if args.generator == "sqlancerpp" or predicates == "sqlancerpp":
        if catalog == "random":
            print(
                "sqlancerpp requires a fixed catalog; pass --catalog rich|simple (or --rich/--simple)",
                file=sys.stderr,
            )
            return 2
        fixed = _fixed_catalog(adapter, catalog)
        _configure_sqlancerpp_schema(args.forks, seed_name=fixed.get_sql_name(), dialect=args.dialect)
        if args.generator == "sqlancerpp":
            _warm_sqlancerpp(fixed, args.forks, dialect=args.dialect)
    predicate_source = predicate_source_for(predicates, dialect=args.dialect)
    report = run_fuzz(
        adapter,
        catalog=catalog,
        row_count=args.rows,
        query_source=source,
        predicate_source=predicate_source,
        forks=args.forks,
        rounds=args.rounds,
        seed=args.seed,
        workdir=args.workdir,
        scheduler=scheduler_from_args(
            round_seconds=args.round_seconds,
            min_round_seconds=args.min_round_seconds,
            schedule=args.schedule,
        ),
    )
    return 1 if report.findings else 0


if __name__ == "__main__":
    sys.exit(main())
