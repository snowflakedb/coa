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

"""Run a campaign against an instrumented engine, snapshotting coverage as it goes::

    EQGEN_PG_BINDIR=$HOME/pgcov-18.4/bin EQGEN_PG_COVERAGE=1 \\
      python -m evaluation.coverage.campaign --dialect postgres --rich --rounds 200

    EQGEN_DUCKDB_CLI=$HOME/ducksrc-spp-paper/build/coverage/duckdb \\
    EQGEN_DUCKDB_COVERAGE_SRC=$HOME/ducksrc-spp-paper \\
      python -m evaluation.coverage.campaign --dialect duckdb --rich --rounds 50

Build first with ``build_postgres_coverage.sh`` / ``build_duckdb_coverage.sh`` (paper path by
default: PG REL_18_4, DuckDB v1.0.0). Measurement defaults match the SQLancer++ artifact:
Postgres keeps initdb and uses full-tree lcov; DuckDB uses lcov + ``lcov_exclude``. Pass
``--fair`` to opt into the older eqgen-only curves (zero initdb, gcovr backend+common on PG).

This does not compute coverage. It copies the counter files — a few MB — and records what the run had
done by that point. Turning the snapshots into line and branch percentages takes about 20 seconds each
and happens afterwards, in :mod:`evaluation.coverage.report`, so the campaign is not repeatedly paused by
its own measurement.

**Runs are compared at equal wall clock**, which is what the DBMS-testing literature does (Argus's
coverage figures are 24-hour runs) and is the right call rather than a concession: throughput is part
of what a tool is, so a rewrite that costs time and buys coverage should be judged on the trade, not
credited with the coverage and excused the cost. Each snapshot also records cumulative **queries**, but
as a diagnostic — it tells you *why* a curve moved, not whether the comparison was fair.

The first snapshot is a **baseline**, taken after the server is up and one connection has been opened
and closed but before any generated query has run. Measured on 18.4 it is about 1.2% of lines in
``src/backend`` — the floor every tool measured this way gets for free.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from evaluation.coverage import gcov as coverage
from eqgen.fuzz.adapter import DialectAdapter
from eqgen.fuzz.cli import (
    DEFAULT_LOG_DIR,
    DEFAULT_ROUND_SECONDS,
    _fixed_catalog,
    _round_catalog_and_rows,
    load_adapter,
    predicate_source_for,
    query_source,
    run_fuzz,
    scheduler_from_args,
)
from eqgen.fuzz.database import Database, exposed_fork_names
from eqgen.fuzz.round import RoundOutcome
from eqgen.plugins import QuerySource

#: Where PostgreSQL's instrumented build's .gcno/.gcda live. This is a *source* tree, not the install
#: prefix: gcov records the path of the object file it came from, so the counters land next to the
#: objects. Default: REL_18_4 tree from ``build_postgres_coverage.sh``.
PG_SOURCE_ENV = "EQGEN_PG_COVERAGE_SRC"
PG_DEFAULT_SOURCE = Path("~/pgsrc-cov-18.4")

#: DuckDB checkout (gcovr ``--root`` / ``--filter``) and the cmake build tree where .gcno/.gcda land.
#: Paper default: v1.0.0 tree from ``build_duckdb_coverage.sh``.
DUCKDB_SOURCE_ENV = "EQGEN_DUCKDB_COVERAGE_SRC"
DUCKDB_BUILD_ENV = "EQGEN_DUCKDB_COVERAGE_BUILD"
DUCKDB_DEFAULT_SOURCE = Path("~/ducksrc-spp-paper")

# Back-compat alias used by older docs / scripts.
SOURCE_ENV = PG_SOURCE_ENV
DEFAULT_SOURCE = PG_DEFAULT_SOURCE


class _Sampler(coverage.Sampler):
    """The shared sampler plus the one thing only the eqgen arms need: a deadline that stops the loop.

    ``run_fuzz`` already treats ``KeyboardInterrupt`` as "stop cleanly and return what you have", so
    raising it from the round hook ends a timed run with its report, manifest and final snapshot written.
    Killing the process from outside with ``timeout`` would lose all three.
    """

    def __init__(self, *args, plan_tracker=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.plan_tracker = plan_tracker

    def on_round(self, round_number: int, outcome: RoundOutcome) -> None:
        if self.plan_tracker is not None:
            self.plan_tracker.observe(outcome)
        self.count_round(round_number, len(outcome.results))
        if self.out_of_time():
            print(f"\nreached the {self.max_seconds:.0f}s limit after round {round_number}", flush=True)
            raise KeyboardInterrupt

    def record(self, label: str, *, round_number: Optional[int] = None, flush: bool = True) -> None:
        super().record(label, round_number=round_number, flush=flush)
        if self.plan_tracker is not None:
            entry = self.entries[-1]
            self.plan_tracker.write_row(
                label,
                round_number=round_number,
                queries=int(entry["queries"]),
                elapsed_seconds=float(entry["elapsed_seconds"]),
            )


def run_baseline(
    adapter: DialectAdapter,
    *,
    catalog: str,
    row_count: int,
    query_source: QuerySource,
    rounds: Optional[int],
    round_seconds: float,
    seed: Optional[int],
    sampler: _Sampler,
    verbose: bool = True,
    forks: int = 1,
) -> int:
    """The control: the same generated queries, against a plain table, with no equivalence built.

    This is what the query generator reaches on its own. Subtract it from a full run and what is left
    is what building a second row-equivalent object and reading it instead adds — which is the only
    part of the number that is about this project rather than about the query generator bolted to it.

    Three things are held identical to a full run, because each of them moves coverage on its own:

    * the **queries** — same source, same per-round seed sequence from the same master seed;
    * the **rows and catalog** (same *catalog* mode and per-round sampling as the equivalence arm);
    * the **connection lifecycle** — one database built and closed per round, because closing is what
      makes a backend write its counters, so a baseline that held one connection open for the whole run
      would flush once and look artificially low.

    What is *not* held identical is throughput, and deliberately so. This arm gets through several times
    more rounds per hour, because it neither generates nor builds an object. Both arms are given the same
    wall clock and the difference in what they reach at the end is the answer — buying coverage with
    slower rounds is a trade, and equal time is what prices it.

    Each baseline round spends up to *round_seconds* of query-phase wall clock (flat — there is no
    equivalence to score), matching the scheduled query-phase cap on the equivalence arm.
    """
    random.seed(seed if seed is not None else random.randrange(2**31))
    try:
        for round_number in itertools.count():
            if rounds is not None and round_number >= rounds:
                break
            round_seed = random.randrange(2**31)
            table, rows = _round_catalog_and_rows(
                adapter, catalog=catalog, fixed_table=None, row_count=row_count, seed=round_seed
            )
            names = exposed_fork_names(forks, seed_name=table.get_sql_name())
            base = Database.build_base(adapter, table, rows, exposed_names=names)
            executed = 0
            deadline = time.monotonic() + round_seconds
            try:
                for query in query_source.iter_queries(
                    table, seed=round_seed, limit=None, exposed_names=names
                ):
                    if time.monotonic() >= deadline:
                        break
                    base.query(query)  # never raises: an error is an outcome, and covers code too
                    executed += 1
            finally:
                base.close()  # <- the flush point: a closed connection is an exited backend
            sampler.count_round(round_number, executed)
            if verbose and (round_number + 1) % 100 == 0:
                print(f"round {round_number}: {sampler.queries} queries so far, {sampler.elapsed:.0f}s elapsed", flush=True)
            if sampler.out_of_time():
                print(f"\nreached the {sampler.max_seconds:.0f}s limit after round {round_number}", flush=True)
                break
    except KeyboardInterrupt:
        print("\ninterrupted")
    return sampler.queries


def _resolve_postgres_source(explicit: Optional[Path]) -> Path:
    # Note the empty-string check. `Path("")` is `Path(".")`, which is truthy, so an `or` chain over a
    # possibly-unset environment variable silently resolves to the working directory instead of the
    # default -- and then fails with "." having no .gcno files.
    from_env = os.environ.get(PG_SOURCE_ENV) or None
    source = Path(explicit or from_env or PG_DEFAULT_SOURCE).expanduser()
    if not source.is_dir():
        raise SystemExit(
            f"no instrumented source tree at {source}. Run evaluation/build_postgres_coverage.sh, or pass --source."
        )
    if not any(source.rglob("*.gcno")):
        raise SystemExit(f"{source} has no .gcno files, so it was not built with coverage instrumentation.")
    return source


def _resolve_duckdb_trees(explicit: Optional[Path]) -> tuple[Path, Path]:
    """Return ``(counter_dir, source_root)`` for an instrumented DuckDB build.

    Counters live in the cmake build tree; filters and gcovr ``--root`` need the checkout.
    """
    source_root = Path(
        explicit or (os.environ.get(DUCKDB_SOURCE_ENV) or None) or DUCKDB_DEFAULT_SOURCE
    ).expanduser()
    if not source_root.is_dir():
        raise SystemExit(
            f"no DuckDB checkout at {source_root}. Run evaluation/build_duckdb_coverage.sh, or pass --source."
        )
    build = Path(
        (os.environ.get(DUCKDB_BUILD_ENV) or None) or (source_root / "build" / "coverage")
    ).expanduser()
    if not build.is_dir():
        raise SystemExit(f"no DuckDB coverage build at {build}. Run evaluation/build_duckdb_coverage.sh.")
    if not any(build.rglob("*.gcno")):
        raise SystemExit(f"{build} has no .gcno files, so it was not built with coverage instrumentation.")
    cli = os.environ.get("EQGEN_DUCKDB_CLI")
    if not cli:
        candidate = build / "duckdb"
        if candidate.is_file():
            os.environ["EQGEN_DUCKDB_CLI"] = str(candidate)
        else:
            raise SystemExit(
                f"set EQGEN_DUCKDB_CLI to the instrumented CLI (expected at {candidate}). "
                "Do not use the nightly artifacts.duckdb.org binary for coverage — it has no counters."
            )
    return build, source_root


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run a campaign against an instrumented engine, sampling coverage.")
    parser.add_argument("--dialect", default="postgres", help="engine to test (default: postgres)")
    parser.add_argument("--rounds", type=int, default=100, help="rounds to run (default: 100; ignored when --hours is given)")
    parser.add_argument("--hours", type=float, default=None, help="run for this many hours instead of a fixed round count")
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
    parser.add_argument("--seed", type=int, default=None, help="master seed, to replay a run")
    parser.add_argument("--every", type=int, default=10, help="snapshot every N rounds (default: 10)")
    parser.add_argument(
        "--every-minutes",
        type=float,
        default=None,
        help="snapshot on a clock instead of a round count; use this for long runs (10 gives 36 snapshots over 6 hours)",
    )
    parser.add_argument("--source", type=Path, default=None, help="instrumented source tree (default depends on --dialect)")
    parser.add_argument("--out", type=Path, default=None, help="where to write snapshots (default: under eqgen/log)")
    parser.add_argument("--no-predicates", action="store_true", help="do not supply predicates")
    parser.add_argument("--generator", default="random", choices=("random", "sqlancerpp"), help="query generator (default: random)")
    parser.add_argument(
        "--predicates",
        default="typed",
        choices=("example", "typed", "sqlancerpp", "none"),
        help="source of the predicates embedded in generated objects (default: typed)",
    )
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="control run: the same generated queries against a plain table, no equivalence built",
    )
    parser.add_argument(
        "--include-setup",
        action="store_true",
        help="count cluster creation (initdb). Paper default already does this on Postgres; "
        "only needed with --fair if you want initdb back in.",
    )
    parser.add_argument(
        "--exclude-setup",
        action="store_true",
        help="zero counters after initdb (fair / non-paper). Implies measurement closer to a tool "
        "that connects to an already-running server.",
    )
    parser.add_argument(
        "--fair",
        action="store_true",
        help="opt out of paper measurement: on Postgres, zero initdb and report with gcovr "
        "backend+common (unless --include-setup / --reporter). DuckDB report stays lcov; use "
        "EQGEN_COVERAGE_FAIR=1 on the build script for the fair DuckDB tree.",
    )
    parser.add_argument(
        "--artifact",
        action="store_true",
        help="deprecated no-op: paper measurement is already the default. Use --fair to opt out.",
    )
    parser.add_argument(
        "--scope",
        default="all",
        choices=("all", "compiler", "optimizer"),
        help="denominator scope: all (engine), compiler (parse/plan/optimize), or optimizer only",
    )
    parser.add_argument(
        "--track-plans",
        action="store_true",
        help="count distinct Postgres query plans (EXPLAIN fingerprints) into plans.csv; off by default",
    )
    args = parser.parse_args(argv)

    paper = not args.fair
    # Postgres initdb: paper keeps it; --fair zeros it unless --include-setup overrides.
    if args.dialect == "postgres":
        if args.exclude_setup:
            args.include_setup = False
        elif args.include_setup:
            args.include_setup = True
        else:
            args.include_setup = paper
    else:
        args.include_setup = False

    if args.dialect == "duckdb":
        counter_dir, source_root = _resolve_duckdb_trees(args.source)
        filters = coverage.filters_for("duckdb", args.scope)
    elif args.dialect == "postgres":
        counter_dir = _resolve_postgres_source(args.source)
        source_root = counter_dir
        filters = coverage.filters_for("postgres", args.scope)
    else:
        raise SystemExit(f"coverage campaigns support duckdb and postgres, not {args.dialect!r}")

    # Paper whole-engine → lcov. Fair Postgres → gcovr filters. Narrow scopes always gcovr.
    if args.scope != "all" or (args.fair and args.dialect == "postgres"):
        reporter = "gcovr"
    else:
        reporter = "lcov"

    if args.dialect == "postgres" and not os.environ.get("EQGEN_PG_COVERAGE"):
        # Not fatal: the run still produces a usable curve, it just loses whatever the checkpointer,
        # walwriter and any still-open backend had measured when the server went down.
        print("warning: EQGEN_PG_COVERAGE is not set, so the cluster will shut down with SIGQUIT and", file=sys.stderr)
        print("         counters from aux processes will be lost. Set it to 1.", file=sys.stderr)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = (args.out or (DEFAULT_LOG_DIR / f"coverage_{args.dialect}_{stamp}")).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"source   : {counter_dir}")
    if source_root != counter_dir:
        print(f"root     : {source_root}")
    if reporter == "lcov" and args.dialect == "postgres":
        print("filters  : (full tree, lcov)")
    else:
        print(f"filters  : {' '.join(filters)}")
    print(f"reporter : {reporter}" + ("  (paper)" if paper else "  (fair)"))
    if args.dialect == "postgres":
        print("setup    : " + ("include initdb (paper)" if args.include_setup else "initdb zeroed (fair)"))
    print(f"out dir  : {out_dir}")
    print(f"zeroed   : {coverage.zero_counters(counter_dir)} counter file(s) from before this run")

    adapter = load_adapter(args.dialect)  # starts the server, for postgres

    # Zero again after cluster setup when *excluding* initdb (fair). Paper keeps initdb counts —
    # SQLancer++ artifact methodology. initdb is a separate exited process, so deleting its .gcda
    # removes it cleanly. Postmaster startup cannot be removed the same way.
    #
    # DuckDB has no initdb: each connection is a fresh CLI process, so the second zero is a no-op.
    if args.dialect == "postgres" and not args.include_setup:
        print(f"zeroed   : {coverage.zero_counters(counter_dir)} more after cluster setup (initdb excluded)")

    if args.rich and args.simple:
        raise SystemExit("use only one of --rich / --simple / --catalog")
    if args.catalog is not None:
        catalog = args.catalog
    elif args.rich:
        catalog = "rich"
    elif args.simple:
        catalog = "simple"
    else:
        catalog = "random"
    print(f"catalog  : {catalog}")

    max_seconds = args.hours * 3600 if args.hours else None
    rounds = None if max_seconds else args.rounds
    # Flushing before each snapshot is what turns the curve into a trend: without it every
    # long-lived process's coverage arrives at once in the final snapshot. DuckDB needs none of that —
    # each connection is a CLI process that writes .gcda on close, and eqgen closes both every round.
    flush = None
    if args.dialect == "postgres":
        from eqgen.dialects.postgres.cluster import shared_cluster

        flush = shared_cluster().restart

    if args.generator == "sqlancerpp" or args.predicates == "sqlancerpp":
        if catalog == "random":
            raise SystemExit(
                "sqlancerpp requires a fixed catalog; pass --catalog rich|simple (or --rich/--simple)"
            )
        from eqgen.fuzz.cli import _configure_sqlancerpp_schema

        fixed = _fixed_catalog(adapter, catalog)
        _configure_sqlancerpp_schema(args.forks, seed_name=fixed.get_sql_name(), dialect=args.dialect)

    plan_tracker = None
    plan_fingerprint = None
    if args.track_plans:
        if args.dialect != "postgres":
            raise SystemExit("--track-plans is Postgres-only in v1")
        from evaluation.plans.postgres import fingerprint_query
        from evaluation.plans.tracker import PlanTracker

        plan_tracker = PlanTracker(out_dir)
        plan_fingerprint = fingerprint_query
        print("plans    : tracking distinct EXPLAIN fingerprints → plans.csv")

    sampler = _Sampler(
        counter_dir,
        out_dir,
        every=max(1, args.every),
        every_seconds=args.every_minutes * 60 if args.every_minutes else None,
        max_seconds=max_seconds,
        flush=flush,
        plan_tracker=plan_tracker,
    )
    if max_seconds:
        cadence = f"{args.every_minutes} min" if args.every_minutes else f"{args.every} rounds"
        print(f"limit    : {args.hours}h wall clock, snapshot every {cadence}")
    # One connection opened and closed, so the baseline snapshot includes what a client costs before any
    # generated query: connect, authenticate, set search_path, disconnect.
    handle = adapter.connect()
    handle.close()
    sampler.record("baseline")

    findings = 0
    scheduler = scheduler_from_args(
        round_seconds=args.round_seconds,
        min_round_seconds=args.min_round_seconds,
        schedule=args.schedule,
    )
    if args.baseline:
        print("mode     : BASELINE — generated queries against a plain table, no equivalence\n", flush=True)
        run_baseline(
            adapter,
            catalog=catalog,
            row_count=args.rows,
            query_source=query_source(None, args.generator, dialect=args.dialect),
            rounds=rounds,
            round_seconds=args.round_seconds,
            seed=args.seed,
            sampler=sampler,
            forks=args.forks,
        )
    else:
        report = run_fuzz(
            adapter,
            catalog=catalog,
            row_count=args.rows,
            query_source=query_source(None, args.generator, dialect=args.dialect),
            predicate_source=predicate_source_for(
                "none" if args.no_predicates else args.predicates,
                dialect=args.dialect,
            ),
            rounds=rounds,
            seed=args.seed,
            round_hook=sampler.on_round,
            scheduler=scheduler,
            forks=args.forks,
            plan_fingerprint=plan_fingerprint,
            # Coverage campaigns must keep going through unbuildable rounds: the metric is what
            # executed over the wall-clock budget, and a streak of bad SQLancer++ predicates must
            # not abort the run after two minutes of a ten-minute budget.
            max_consecutive_unbuildable=10_000,
        )
        findings = len(report.findings)

    # The final snapshot has to come after the server is down: the postmaster and its aux processes
    # write their counters as they exit, so a snapshot taken while it is running misses them.
    if args.dialect == "postgres":
        from eqgen.dialects.postgres.cluster import shared_cluster

        shared_cluster().stop()
    sampler.record("final", flush=False)

    manifest = {
        "engine": adapter.engine_banner(),
        "dialect": args.dialect,
        "mode": "baseline" if args.baseline else "equivalence",
        "source": str(counter_dir),
        "source_root": str(source_root),
        "filters": list(filters),
        "reporter": reporter,
        "branch_counting": (
            coverage.BRANCH_COUNTING_LCOV if reporter == "lcov" else coverage.BRANCH_COUNTING_GCOVR
        ),
        "artifact_aligned": paper,
        "coverage_artifact_build": os.environ.get("EQGEN_COVERAGE_FAIR", "0") != "1"
        and os.environ.get("EQGEN_COVERAGE_ARTIFACT", "1") != "0",
        "duckdb_coverage_ref": os.environ.get("EQGEN_DUCKDB_COVERAGE_REF"),
        "pg_coverage_ref": os.environ.get("EQGEN_PG_COVERAGE_REF"),
        "scope": args.scope,
        "rounds_requested": rounds,
        "hours_limit": args.hours,
        "round_seconds": args.round_seconds,
        "min_round_seconds": args.min_round_seconds,
        "schedule": args.schedule,
        "generator": args.generator,
        "predicates": args.predicates,
        "catalog": catalog,
        "rows": args.rows,
        "seed": args.seed,
        "include_setup": bool(args.include_setup) if args.dialect == "postgres" else False,
        "findings": findings,
        "track_plans": bool(args.track_plans),
        "distinct_plans": None if plan_tracker is None else plan_tracker.distinct_plans,
        "snapshots": sampler.entries,
    }
    with open(out_dir / "manifest.json", "w", encoding="utf-8") as handle_out:
        json.dump(manifest, handle_out, indent=2)

    print()
    print(f"{len(sampler.entries)} snapshot(s) written. Turn them into line and branch coverage with:")
    print(f"  python -m evaluation.coverage.report {out_dir}")
    if plan_tracker is not None:
        print(f"distinct plans: {plan_tracker.distinct_plans}  (see {out_dir / 'plans.csv'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
