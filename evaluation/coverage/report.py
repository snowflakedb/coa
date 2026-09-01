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

"""Turn snapshots into line and branch coverage::

    python -m evaluation.coverage.report eqgen/log/coverage_postgres_20260805-120000

Prints one row per snapshot — covered lines and branches against a denominator fixed across the whole
run — and writes ``coverage.csv`` next to the snapshots.

Two details that decide whether the numbers mean anything:

**The denominator is fixed across the run, not read from each snapshot.** gcov only writes a .gcda for a
translation unit once something has loaded it, and gcovr can only report on files that have one. So a
module loaded on demand joins the denominator partway through, and the percentage can *fall* while
coverage has only grown. :func:`~evaluation.coverage.fixed_denominator` takes the largest total
seen for each file and uses that throughout; a file missing from an early snapshot is then zero-covered,
which is what it was.

**Reporting overwrites the live counters.** A report needs .gcda and .gcno side by side, and gcovr cannot
be told they live in different trees, so each snapshot is copied back over the build tree in turn. That
costs nothing — the last snapshot of a campaign is the live state — but it does mean this is something to
run after a campaign rather than during one.

**DuckDB whole-engine** uses ``lcov`` (``--no-external`` + ``lcov_exclude`` + ``lcovrc`` when present),
not bare gcovr over ``src/``: gcovr over-counts ``src/include/`` headers and roughly halves the
percentage on the same counters.

**Branch rule (lcov reporter, DuckDB and Postgres):** never-evaluated ``BRDA`` arms (``taken=-``) are
omitted from both numerator and denominator (:data:`~evaluation.coverage.gcov.BRANCH_COUNTING_LCOV`).
Any DuckDB v1.0.0 CSV with ``branches_total≈370110`` was produced under the old lcov-2.x-style rule —
re-run this module to refresh it (~220k denom, paper-comparable).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from evaluation.coverage import gcov as coverage


def _load_manifest(run_dir: Path) -> coverage.JsonDict:
    path = run_dir / "manifest.json"
    if not path.is_file():
        raise SystemExit(f"no manifest.json in {run_dir} — is that a coverage run directory?")
    with open(path, encoding="utf-8") as handle:
        manifest: coverage.JsonDict = json.load(handle)
    return manifest


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Report line and branch coverage from a campaign's snapshots.")
    parser.add_argument("run_dir", type=Path, help="a directory written by evaluation.coverage.campaign")
    parser.add_argument("--source", type=Path, default=None, help="override the instrumented source tree")
    parser.add_argument("--jobs", type=int, default=None, help="gcovr parallelism (default: all cores)")
    parser.add_argument(
        "--reporter",
        choices=("auto", "lcov", "gcovr"),
        default=None,
        help="override manifest reporter (e.g. lcov to re-report a fair Postgres run with artifact capture)",
    )
    args = parser.parse_args(argv)

    run_dir = args.run_dir.expanduser()
    manifest = _load_manifest(run_dir)
    # ``source`` is where .gcda/.gcno live (the build tree). DuckDB's cmake out-of-tree build puts them
    # under ``build/coverage`` while the checkout that ``--filter`` is relative to is ``source_root``.
    source = (args.source or Path(str(manifest["source"]))).expanduser()
    if not source.is_dir():
        raise SystemExit(f"instrumented source tree {source} is gone; pass --source")
    source_root = Path(str(manifest["source_root"])).expanduser() if manifest.get("source_root") else source

    snapshots = list(manifest.get("snapshots", []) or [])
    if not snapshots:
        raise SystemExit("the manifest lists no snapshots")

    filters = tuple(str(pattern) for pattern in (manifest.get("filters") or coverage.DEFAULT_FILTERS))
    dialect = str(manifest.get("dialect") or "")
    if not dialect:
        # Older manifests only stamped a banner into ``engine``.
        banner = str(manifest.get("engine") or "").lower()
        dialect = "duckdb" if "duckdb" in banner else "postgres"
    reporter = args.reporter or str(manifest.get("reporter") or "auto")
    branch_rule = (
        coverage.BRANCH_COUNTING_LCOV if reporter == "lcov" else coverage.BRANCH_COUNTING_GCOVR
    )
    print(f"engine   : {manifest.get('engine')}")
    print(f"source   : {source}")
    if source_root != source:
        print(f"root     : {source_root}")
    print(f"filters  : {' '.join(filters)}")
    print(f"reporter : {reporter}" + (f" (dialect={dialect})" if dialect else ""))
    print(f"branches : {branch_rule}")
    print(f"snapshots: {len(snapshots)}   (lcov/gcovr takes tens of seconds each)")
    print()

    reports: list[coverage.JsonDict] = []
    for index, entry in enumerate(snapshots, start=1):
        snapshot_dir = run_dir / str(entry["path"])
        restored = coverage.restore_snapshot(snapshot_dir, source)
        print(f"  [{index}/{len(snapshots)}] {entry['label']}: {restored} counter files ...", end="", flush=True)
        report = coverage.run_coverage_report(
            source,
            dialect=dialect if dialect in ("duckdb", "postgres") else "postgres",
            filters=filters,
            jobs=args.jobs,
            root=source_root,
            object_directory=source if source_root != source else None,
            reporter=reporter,
        )
        reports.append(report)
        print(" done")

    denominator = coverage.fixed_denominator(reports)
    print()
    header = f"{'snapshot':>14}  {'round':>6}  {'queries':>8}  {'seconds':>8}  {'lines':>19}  {'branches':>19}"
    print(header)
    print("-" * len(header))

    rows: list[coverage.JsonDict] = []
    for entry, report in zip(snapshots, reports):
        totals = coverage.totals_against(report, denominator)
        print(
            f"{str(entry['label']):>14}  {str(entry['round'] if entry['round'] is not None else '-'):>6}  "
            f"{str(entry['queries']):>8}  {str(entry['elapsed_seconds']):>8}  "
            f"{totals.lines_covered:>8}/{totals.lines_total:<7} {totals.line_percent:>5.2f}%  "
            f"{totals.branches_covered:>8}/{totals.branches_total:<7} {totals.branch_percent:>5.2f}%"
        )
        rows.append(
            {
                "label": entry["label"],
                "round": entry["round"],
                "queries": entry["queries"],
                "elapsed_seconds": entry["elapsed_seconds"],
                "lines_covered": totals.lines_covered,
                "lines_total": totals.lines_total,
                "line_percent": round(totals.line_percent, 4),
                "branches_covered": totals.branches_covered,
                "branches_total": totals.branches_total,
                "branch_percent": round(totals.branch_percent, 4),
                "functions_covered": totals.functions_covered,
                "functions_total": totals.functions_total,
                "function_percent": round(totals.function_percent, 4),
            }
        )

    destination = run_dir / "coverage.csv"
    with open(destination, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    # Refresh methodology stamps on older manifests so the CSV and manifest agree.
    manifest["reporter"] = reporter
    manifest["branch_counting"] = branch_rule
    with open(run_dir / "manifest.json", "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")

    baseline, final = rows[0], rows[-1]
    print()
    print(f"baseline (server start, no generated query): {baseline['line_percent']}% lines, {baseline['branch_percent']}% branches")
    print(f"final                                      : {final['line_percent']}% lines, {final['branch_percent']}% branches")
    print(
        "workload contribution                      : "
        f"+{round(float(str(final['line_percent'])) - float(str(baseline['line_percent'])), 2)} points of lines, "
        f"+{round(float(str(final['branch_percent'])) - float(str(baseline['branch_percent'])), 2)} of branches"
    )
    print()
    print(f"wrote {destination}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
