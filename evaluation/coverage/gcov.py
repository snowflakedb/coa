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

"""Reading line and branch coverage out of a gcov-instrumented engine.

How gcov works, because three details decide everything here::

    compile   one .gcno per translation unit    the control-flow graph: every line, every branch
    run       counters in process memory        arcs increment as they are taken
    exit      one .gcda per translation unit    written out, MERGED with what is already on disk

1. **The write happens at process exit**, so "when does a process end" is the sampling grain. Nothing
   needs zeroing between samples: because .gcda writes *add*, coverage accumulates on its own.
2. **A process killed by a signal writes nothing.** SIGQUIT, SIGKILL, SIGSEGV, SIGABRT all lose that
   process's counters.
3. **Only translation units that have written a .gcda appear in a report at all.** A file compiled but
   never loaded is invisible — not zero, absent — which is why the denominator here comes from the last
   snapshot rather than from each one. See :func:`fixed_denominator`.

For PostgreSQL that lands as: one backend per connection, flushing when the connection closes. eqgen
opens and closes both connections every round, so counters reach disk once per round for free.

A snapshot is a **copy of the .gcda files** (a few MB) rather than a coverage report, because building
the report takes ~20 seconds and a campaign should not stop for it. The reports are built afterwards,
from the snapshots, by :mod:`evaluation.coverage.report`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

#: A parsed gcovr report. ``Any`` rather than a precise shape on purpose: this is JSON from another
#: program, and the two keys used from it are read defensively below. gcovr stamps its own
#: ``gcovr/summary_format_version`` (currently 0.6) into every report if the shape ever needs checking.
JsonDict = dict[str, Any]

#: The subtrees a number is reported over. Anything outside them is not the engine: ``src/bin`` is
#: client programs, ``src/interfaces`` is libpq, ``src/timezone`` is mostly the build-time zone
#: compiler, and ``src/test`` is the test suite. Fixing this list once is what makes two runs
#: comparable — a coverage percentage means nothing without the denominator it was taken over.
DEFAULT_FILTERS: tuple[str, ...] = ("src/backend/", "src/common/")

#: DuckDB's whole engine under ``src/`` — Metamorphic Coverage paper (Ba/Jiang/Rigger) gcovr path.
#: Campaign / Argus-style *code* coverage still uses :func:`run_duckdb_lcov` (``lcov_exclude``):
#: gcovr over ``src/`` attributes ~200k lines of ``src/include/`` headers that DuckDB's own
#: ``lcov --no-external`` + ``lcov_exclude`` pipeline does not (measured: gcovr 15.4% of 366k vs
#: lcov 30.0% of 109k).
DUCKDB_FILTERS: tuple[str, ...] = ("src/",)

#: Directory excludes from the MC paper's ``targets/duckdb/gcovr.cfg`` (nus-test/metamorphic_coverage).
#: Applied as gcovr ``--exclude-directories`` so MC denominators match their DuckDB setup.
DUCKDB_MC_EXCLUDE_DIRECTORIES: tuple[str, ...] = (
    "third_party/",
    "extension/",
    "tools/",
    "test/",
    "benchmark/",
    "data/",
    "examples/",
    "CMakeFiles/",
)

#: Filename of DuckDB's upstream exclude list (relative to the checkout). Same file SQLancer++ /
#: DuckDB's coverage CI use.
DUCKDB_LCOV_EXCLUDE = Path(".github/workflows/lcov_exclude")

#: Parse → rewrite → plan. The Postgres "query compiler" in the usual sense: not storage, not the
#: executor. Useful for metamorphic coverage when the question is how differently equivalent inputs
#: exercise planning, rather than whole-engine line share (Argus Table 3 is whole-engine on DuckDB).
COMPILER_FILTERS: tuple[str, ...] = (
    "src/backend/parser/",
    "src/backend/nodes/",
    "src/backend/rewrite/",
    "src/backend/optimizer/",
)

#: DuckDB's analogue of the Postgres compiler scope: parse → bind/plan → optimize.
DUCKDB_COMPILER_FILTERS: tuple[str, ...] = (
    "src/parser/",
    "src/planner/",
    "src/optimizer/",
)

#: Planner only — the narrowest scope people sometimes mean by "compiler" in DBMS testing papers.
OPTIMIZER_FILTERS: tuple[str, ...] = ("src/backend/optimizer/",)
DUCKDB_OPTIMIZER_FILTERS: tuple[str, ...] = ("src/optimizer/",)

SCOPE_FILTERS: dict[str, tuple[str, ...]] = {
    "all": DEFAULT_FILTERS,
    "compiler": COMPILER_FILTERS,
    "optimizer": OPTIMIZER_FILTERS,
}

DUCKDB_SCOPE_FILTERS: dict[str, tuple[str, ...]] = {
    "all": DUCKDB_FILTERS,
    "compiler": DUCKDB_COMPILER_FILTERS,
    "optimizer": DUCKDB_OPTIMIZER_FILTERS,
}


def filters_for(dialect: str, scope: str = "all") -> tuple[str, ...]:
    """gcovr ``--filter`` patterns for *dialect* at the named *scope*."""
    table = DUCKDB_SCOPE_FILTERS if dialect == "duckdb" else SCOPE_FILTERS
    try:
        return table[scope]
    except KeyError as exc:
        raise ValueError(f"unknown coverage scope {scope!r} for {dialect}") from exc

#: PostgreSQL compiles ``src/common`` more than once — a server build, a shared-library build and a
#: frontend build — so the same inlined header function appears at different lines in different
#: objects. gcovr refuses to merge that by default and aborts the whole report.
_MERGE_MODE = "merge-use-line-min"

#: gcovr aborts when a hit count exceeds 2**32, on the assumption that it is the counter corruption of
#: GCC bug 68080. On a long campaign it is not: after 6.7 million queries ``MemSetAligned`` in
#: ``mcxt.c`` had genuinely been executed 7,116,195,248 times, and the whole report failed on it. Only
#: *whether* a line was hit matters here, never how often, so a warning is the right response to a
#: number too large to be plausible-looking. DuckDB's C++ instrumentation also produces *negative*
#: branch hits under the same GCC bug; ``all`` covers both.
_IGNORE_PARSE_ERRORS = "all"

#: DuckDB's bison-generated ``*.y`` parser TUs make gcov try to write ``select.y##….gcov`` files and
#: then fail to infer a working directory. Ignoring *only* that error keeps the report usable; the
#: generated grammar is not what we filter for anyway (``src/`` still covers the hand-written code).
#: Do **not** pass ``all``: gcovr 8.6 with ``--gcov-ignore-errors=all`` can emit empty 0/0 summaries
#: while still exiting successfully (measured on PostgreSQL 18.4).
_IGNORE_GCOV_ERRORS: str | None = "no_working_dir_found"



@dataclass(frozen=True)
class CoverageTotals:
    """What one snapshot covered. Percentages are computed, never read from the report, so that they
    can be recomputed against a denominator the snapshot itself did not know about."""

    lines_covered: int
    lines_total: int
    branches_covered: int
    branches_total: int
    functions_covered: int
    functions_total: int

    @staticmethod
    def _percent(covered: int, total: int) -> float:
        return 100.0 * covered / total if total else 0.0

    @property
    def line_percent(self) -> float:
        return self._percent(self.lines_covered, self.lines_total)

    @property
    def branch_percent(self) -> float:
        return self._percent(self.branches_covered, self.branches_total)

    @property
    def function_percent(self) -> float:
        return self._percent(self.functions_covered, self.functions_total)


def gcda_paths(source_dir: Path) -> list[Path]:
    """Every counter file currently on disk under *source_dir*."""
    return sorted(source_dir.rglob("*.gcda"))


def zero_counters(source_dir: Path) -> int:
    """Delete every .gcda, returning how many there were.

    Needed once after building, and it is not obvious why: ``make`` compiles and *runs* instrumented
    helper programs, so a freshly built tree already carries counts for hundreds of files. Skip this
    and the first report includes the build.
    """
    paths = gcda_paths(source_dir)
    for path in paths:
        path.unlink(missing_ok=True)
    return len(paths)


def take_snapshot(source_dir: Path, destination: Path) -> int:
    """Copy the counter files into *destination*, keeping their paths relative to *source_dir*.

    Only .gcda are copied. The .gcno they pair with are ~10x larger and never change, so they stay
    where they are and :func:`restore_snapshot` puts the counters back beside them.
    """
    destination.mkdir(parents=True, exist_ok=True)
    paths = gcda_paths(source_dir)
    for path in paths:
        target = destination / path.relative_to(source_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    return len(paths)


def restore_snapshot(snapshot: Path, source_dir: Path) -> int:
    """Put a snapshot's counters back into the build tree, replacing whatever is there.

    **This overwrites live counters**, so it is a reporting-time operation, not something to do while a
    campaign is running. Nothing is lost by it: the last snapshot of a campaign *is* the live state.

    It exists because a report needs the .gcda and .gcno side by side, and gcovr has no way to be told
    that the two halves live in different trees.
    """
    for stale in gcda_paths(source_dir):
        stale.unlink(missing_ok=True)
    count = 0
    for path in sorted(snapshot.rglob("*.gcda")):
        target = source_dir / path.relative_to(snapshot)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        count += 1
    return count


def find_lcov() -> str:
    """Locate ``lcov``. PATH first, then a nix store install (common on this fleet)."""
    found = shutil.which("lcov")
    if found:
        return found
    nix = Path("/nix/store")
    if nix.is_dir():
        matches = sorted(nix.glob("*-lcov-*/bin/lcov"))
        if matches:
            return str(matches[-1])
    raise RuntimeError(
        "lcov not found on PATH (and no nixpkgs lcov under /nix/store). "
        "DuckDB/Postgres paper-comparable coverage needs it — e.g. "
        "`nix-shell -p lcov` or put lcov on PATH."
    )


def _lcov_version(lcov_bin: str) -> tuple[int, int]:
    """Major.minor from ``lcov --version``, or ``(1, 0)`` if unparseable."""
    import re

    result = subprocess.run([lcov_bin, "--version"], capture_output=True, text=True, check=False)
    text = (result.stdout or "") + (result.stderr or "")
    match = re.search(r"LCOV version\s+(\d+)\.(\d+)", text)
    if not match:
        return (1, 0)
    return int(match.group(1)), int(match.group(2))


#: DuckDB CI / SQLancer++ artifact ``lcov`` config (``lcov_excl_br_line`` for exception paths).
DUCKDB_LCOVRC = Path(".github/workflows/lcovrc")

#: How :func:`_parse_lcov_info` counts branches. Stamped into JSON summaries and campaign manifests
#: so a CSV cannot silently mix pre-fix (lcov-2.x-style, count ``taken=-``) with paper numbers.
BRANCH_COUNTING_LCOV = "omit_unevaluated_brda"
BRANCH_COUNTING_GCOVR = "gcovr"


def _lcov_base_args(lcov_bin: str, *, config_file: Optional[Path] = None) -> list[str]:
    """Branch coverage + ignore flags that keep GCC C++ dumps usable on lcov 1.x and 2.x.

    GCC coverage on DuckDB produces negative branch hits (same class of bug gcovr's
    ``--gcov-ignore-parse-errors=all`` papers over). lcov 2.x treats those as hard errors unless
    ``--ignore-errors negative`` is set; 1.14 only warns.

    *config_file* is DuckDB's ``lcovrc`` when present (paper Dockerfile passes
    ``--config-file .github/workflows/lcovrc``).
    """
    major, _minor = _lcov_version(lcov_bin)
    args = [lcov_bin]
    if config_file is not None and config_file.is_file():
        args += ["--config-file", str(config_file)]
    if major >= 2:
        args += [
            "--rc",
            "branch_coverage=1",
            "--ignore-errors",
            "negative,inconsistent,corrupt,mismatch,unsupported,unused,deprecated,empty,source,count",
        ]
    else:
        args += ["--rc", "lcov_branch_coverage=1"]
    return args


def lcov_parallel_args(lcov_bin: str, jobs: Optional[int]) -> list[str]:
    """``-j N`` when the lcov on PATH understands it, else nothing.

    ``geninfo`` forks ``gcov`` once per translation unit and parses its text output in Perl, so a
    capture over PostgreSQL's 1,307 units costs ~52s single-threaded -- and metamorphic coverage pays
    that twice per pair, which is where essentially all of its runtime went. lcov 2.x parallelises
    the per-unit work: measured on this tree, ``-j 16`` takes 6.1s and its ``coverage.info`` is
    bit-identical (16,640 hit lines of 360,752 either way). Same binary, same flags, same numbers --
    so this is a pure speedup and not a change of measurement.

    Gated on the major version because 1.x has no ``--parallel`` and would fail on the flag.
    """
    if not jobs or jobs < 2:
        return []
    major, _minor = _lcov_version(lcov_bin)
    return ["-j", str(int(jobs))] if major >= 2 else []


def _parse_lcov_info(path: Path) -> JsonDict:
    """Turn an ``lcov.info`` into a gcovr-shaped JSON summary (aggregates + per-file rows).

    Branch totals follow the SQLancer++ / older-lcov paper convention (used for **both** DuckDB
    and Postgres lcov reporters): a ``BRDA`` whose *taken* field is ``-`` (basic block never
    entered — branch never evaluated) is **omitted** from both numerator and denominator. Only
    evaluated arms (``0`` = not taken, ``>0`` = taken) count.

    Counting ``-`` as missed (lcov 2.x ``--summary`` default) inflates the DuckDB v1.0.0
    denominator from ~220k to ~370k and is not comparable to the paper (~226k). Any
    ``coverage.csv`` with DuckDB ``branches_total≈370110`` on that tree was reported under the
    old rule and must be re-run through :mod:`evaluation.coverage.report`.

    Each file row also carries ``lines``: ``[{"line_number": N, "count": H}, ...]`` for every
    ``DA:`` record — the hit set metamorphic coverage needs. Aggregates stay the source of truth
    for campaign CSVs; ``lines`` is ignored by :func:`totals_from`.
    """
    files: list[JsonDict] = []
    line_covered = line_total = 0
    branch_covered = branch_total = 0
    function_covered = function_total = 0

    cur: Optional[str] = None
    f_line_c = f_line_t = 0
    f_br_c = f_br_t = 0
    f_fn_c = f_fn_t = 0
    f_lines: list[JsonDict] = []

    def flush() -> None:
        nonlocal line_covered, line_total, branch_covered, branch_total
        nonlocal function_covered, function_total
        nonlocal f_line_c, f_line_t, f_br_c, f_br_t, f_fn_c, f_fn_t, cur, f_lines
        if cur is None:
            return
        files.append(
            {
                "filename": cur,
                "line_covered": f_line_c,
                "line_total": f_line_t,
                "branch_covered": f_br_c,
                "branch_total": f_br_t,
                "function_covered": f_fn_c,
                "function_total": f_fn_t,
                "lines": f_lines,
            }
        )
        line_covered += f_line_c
        line_total += f_line_t
        branch_covered += f_br_c
        branch_total += f_br_t
        function_covered += f_fn_c
        function_total += f_fn_t
        cur = None
        f_line_c = f_line_t = f_br_c = f_br_t = f_fn_c = f_fn_t = 0
        f_lines = []

    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if raw.startswith("SF:"):
            flush()
            cur = raw[3:]
        elif raw.startswith("DA:") and cur is not None:
            # DA:<line>,<hits>
            try:
                line_s, hits_s = raw[3:].split(",", 1)
                line_no = int(line_s)
                hits = int(hits_s.split(",")[0])
            except ValueError:
                continue
            f_lines.append({"line_number": line_no, "count": hits})
            f_line_t += 1
            if hits > 0:
                f_line_c += 1
        elif raw.startswith("BRDA:") and cur is not None:
            # BRDA:<line>,<block>,<branch>,<taken>  taken is '-' or an int
            parts = raw[5:].split(",")
            if len(parts) < 4:
                continue
            taken = parts[3]
            # Paper rule: never-evaluated branches do not enter the denominator.
            if taken == "-":
                continue
            f_br_t += 1
            if taken != "0":
                f_br_c += 1
        elif raw.startswith("FNDA:") and cur is not None:
            # FNDA:<hits>,<name>
            try:
                hits = int(raw[5:].split(",", 1)[0])
            except ValueError:
                continue
            f_fn_t += 1
            if hits > 0:
                f_fn_c += 1
        elif raw == "end_of_record":
            flush()
    flush()

    def pct(covered: int, total: int) -> float:
        return round(100.0 * covered / total, 1) if total else 0.0

    return {
        "line_covered": line_covered,
        "line_total": line_total,
        "line_percent": pct(line_covered, line_total),
        "branch_covered": branch_covered,
        "branch_total": branch_total,
        "branch_percent": pct(branch_covered, branch_total),
        "function_covered": function_covered,
        "function_total": function_total,
        "function_percent": pct(function_covered, function_total),
        "files": files,
        "reporter": "lcov",
        "branch_counting": BRANCH_COUNTING_LCOV,
    }


def instrumented_by_file(report: JsonDict, *, root: Path) -> dict[str, int]:
    """``{file key: instrumented line count}`` from an lcov-shaped report.

    Metamorphic coverage needs this for the same reason a campaign curve does (see COVERAGE_NOTES.md
    §5): ``lcov --capture`` can only report translation units that have written a ``.gcda``, so a module
    first touched by pair 40 *joins* the denominator at pair 40. Taking a scalar ``max`` over
    per-measurement totals -- what MC did -- lets the denominator drift within a run (measured:
    141,461 -> 164,330 across one 2-pair suite) and differ between arms of a sweep, which makes their
    percentages incomparable.

    Per-file counts let :func:`fixed_denominator` pin one denominator across every measurement, which
    is what the campaign already does. Note this counts only files lcov actually reported, matching
    the SQLancer++ artifact's ``lcov --capture --directory .``; it is deliberately *not*
    ``lcov --capture --initial``, which would add units the artifact's recipe never counts.
    """
    root = root.resolve()
    totals: dict[str, int] = {}
    for entry in report.get("files", []):
        raw_name = entry.get("filename") or entry.get("file")
        if not raw_name:
            continue
        path = Path(str(raw_name))
        if path.is_absolute():
            try:
                key = str(path.resolve().relative_to(root))
            except ValueError:
                key = str(path.resolve())
        else:
            key = str(raw_name)
        count = len(entry.get("lines") or [])
        totals[key] = max(totals.get(key, 0), count)
    return totals


def hit_lines_from_lcov_report(
    report: JsonDict,
    *,
    root: Path,
) -> tuple[set[tuple[str, int]], int]:
    """``(hit lines, instrumented lines)`` from a :func:`_parse_lcov_info` / :func:`run_duckdb_lcov` report.

    Paths under *root* are keyed root-relative so two staged snapshots of the same build compare
    equal. Used by metamorphic coverage when DuckDB ``scope=all`` takes the paper lcov path.
    """
    root = root.resolve()
    hit: set[tuple[str, int]] = set()
    instrumented = 0
    for entry in report.get("files", []):
        raw_name = entry.get("filename") or entry.get("file")
        if not raw_name:
            continue
        path = Path(str(raw_name))
        if path.is_absolute():
            try:
                file_key = str(path.resolve().relative_to(root))
            except ValueError:
                file_key = str(path.resolve())
        else:
            file_key = str(raw_name)
        lines = entry.get("lines") or []
        instrumented += len(lines)
        for line in lines:
            if line.get("count", 0):
                hit.add((file_key, int(line["line_number"])))
    return hit, instrumented


def run_duckdb_lcov(
    source_dir: Path,
    *,
    root: Optional[Path] = None,
    object_directory: Optional[Path] = None,
    exclude_file: Optional[Path] = None,
    work_dir: Optional[Path] = None,
    json_out: Optional[Path] = None,
    jobs: Optional[int] = None,
) -> JsonDict:
    """DuckDB coverage the way DuckDB CI / SQLancer++ measure it.

    ``lcov --capture --no-external`` from the build tree, then ``lcov --remove`` with the checkout's
    ``.github/workflows/lcov_exclude``. Returns a gcovr-shaped summary so :func:`totals_from` /
    :func:`fixed_denominator` keep working.

    *source_dir* is ignored when *object_directory* is set (kept for call-site symmetry with
    :func:`run_gcovr`); counters are read from the object/build directory.
    """
    report_root = (root or source_dir).resolve()
    search = (object_directory or source_dir).resolve()
    excl = exclude_file or (report_root / DUCKDB_LCOV_EXCLUDE)
    if not excl.is_file():
        raise RuntimeError(f"DuckDB lcov exclude list missing: {excl}")

    lcov_bin = find_lcov()
    work = work_dir or (search / ".lcov-eqgen")
    work.mkdir(parents=True, exist_ok=True)
    raw_info = work / "coverage.info"
    filtered = work / "lcov.info"
    # Match the SQLancer++ Dockerfile: ``lcov --config-file .github/workflows/lcovrc …``.
    base = _lcov_base_args(lcov_bin, config_file=report_root / DUCKDB_LCOVRC)

    capture = base + lcov_parallel_args(lcov_bin, jobs) + [
        "--directory",
        str(search),
        "--base-directory",
        str(report_root),
        "--no-external",
        "--capture",
        "--output-file",
        str(raw_info),
    ]
    def _empty_lcov_report() -> JsonDict:
        # Baseline (and any zeroed snapshot) has no .gcda: lcov 2.x exits 1 and leaves a
        # stale .info untouched. Treat as zero coverage; :func:`fixed_denominator` fills
        # totals from later snapshots the same way gcovr did for these runs.
        out: JsonDict = {
            "line_covered": 0,
            "line_total": 0,
            "branch_covered": 0,
            "branch_total": 0,
            "function_covered": 0,
            "function_total": 0,
            "files": [],
            "reporter": "lcov",
            "branch_counting": BRANCH_COUNTING_LCOV,
            "lcovrc_used": bool((report_root / DUCKDB_LCOVRC).is_file()),
            "empty_snapshot": True,
        }
        destination = json_out or (work / "summary.json")
        destination.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
        return out

    if not any(search.rglob("*.gcda")):
        return _empty_lcov_report()

    raw_info.unlink(missing_ok=True)
    cap = subprocess.run(capture, capture_output=True, text=True, check=False)
    err = (cap.stderr or "") + (cap.stdout or "")
    if cap.returncode != 0 or not raw_info.is_file() or raw_info.stat().st_size == 0:
        if "no .gcda" in err or "no data generated" in err:
            return _empty_lcov_report()
        raise RuntimeError(f"lcov capture failed ({cap.returncode}): {err[-2000:]}")

    patterns = excl.read_text(encoding="utf-8").split()
    remove = base + [
        "--remove",
        str(raw_info),
        *patterns,
        "-o",
        str(filtered),
    ]
    rem = subprocess.run(remove, capture_output=True, text=True, check=False)
    if rem.returncode != 0 or not filtered.is_file():
        raise RuntimeError(
            f"lcov --remove failed ({rem.returncode}): {(rem.stderr or rem.stdout)[-2000:]}"
        )

    report = _parse_lcov_info(filtered)
    report["lcovrc_used"] = bool((report_root / DUCKDB_LCOVRC).is_file())
    destination = json_out or (work / "summary.json")
    destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def run_postgres_lcov(
    source_dir: Path,
    *,
    work_dir: Optional[Path] = None,
    json_out: Optional[Path] = None,
    jobs: Optional[int] = None,
) -> JsonDict:
    """PostgreSQL coverage the way the SQLancer++ artifact measures it.

    ``lcov --capture --directory <source>`` with **no** ``--no-external`` and **no** exclude list
    (see ``scripts/run_postgres_coverage.sh`` in the artifact). Counters live in the in-tree build,
    so *source_dir* is both the checkout and the object directory.

    Branch totals use the same omit-``taken=-`` rule as DuckDB (:func:`_parse_lcov_info`) so
    numbers align with older lcov/genhtml, not lcov 2.x ``--summary``.
    """
    search = source_dir.resolve()
    lcov_bin = find_lcov()
    work = work_dir or (search / ".lcov-eqgen")
    work.mkdir(parents=True, exist_ok=True)
    info = work / "coverage.info"
    base = _lcov_base_args(lcov_bin)

    def _empty() -> JsonDict:
        out: JsonDict = {
            "line_covered": 0,
            "line_total": 0,
            "branch_covered": 0,
            "branch_total": 0,
            "function_covered": 0,
            "function_total": 0,
            "files": [],
            "reporter": "lcov",
            "branch_counting": BRANCH_COUNTING_LCOV,
            "empty_snapshot": True,
        }
        destination = json_out or (work / "summary.json")
        destination.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
        return out

    if not any(search.rglob("*.gcda")):
        return _empty()

    # Artifact: lcov --capture --directory . --output-file coverage.info
    info.unlink(missing_ok=True)
    capture = base + lcov_parallel_args(lcov_bin, jobs) + [
        "--directory",
        str(search),
        "--capture",
        "--output-file",
        str(info),
    ]
    cap = subprocess.run(capture, capture_output=True, text=True, check=False)
    err = (cap.stderr or "") + (cap.stdout or "")
    if cap.returncode != 0 or not info.is_file() or info.stat().st_size == 0:
        if "no .gcda" in err or "no data generated" in err:
            return _empty()
        raise RuntimeError(f"lcov capture failed ({cap.returncode}): {err[-2000:]}")

    report = _parse_lcov_info(info)
    destination = json_out or (work / "summary.json")
    destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def run_coverage_report(
    source_dir: Path,
    *,
    dialect: str = "postgres",
    filters: Sequence[str] = DEFAULT_FILTERS,
    jobs: Optional[int] = None,
    json_out: Optional[Path] = None,
    root: Optional[Path] = None,
    object_directory: Optional[Path] = None,
    reporter: str = "auto",
) -> JsonDict:
    """Dispatch coverage tooling.

    *reporter*: ``auto`` (default), ``lcov``, or ``gcovr``.

    - DuckDB whole-engine (``src/``): ``auto``/``lcov`` → artifact lcov + ``lcov_exclude``.
    - Postgres whole-engine: ``lcov`` (set by ``--artifact``) → artifact full-tree lcov;
      ``auto`` stays on gcovr ``backend/``+``common/`` so fair (zero-after-initdb) curves remain
      comparable to historical eqgen numbers.
    - Narrow scopes always use gcovr.
    """
    if dialect == "duckdb":
        use_duckdb_lcov = False
        if reporter == "lcov":
            use_duckdb_lcov = True
        elif reporter == "auto":
            norms = {str(f).rstrip("/") for f in filters}
            use_duckdb_lcov = norms == {"src"} or any(
                str(f) in ("src/", "src") or str(f).endswith("/src/") for f in filters
            )
        if use_duckdb_lcov:
            return run_duckdb_lcov(
                source_dir,
                root=root,
                object_directory=object_directory or source_dir,
                json_out=json_out,
            )
    elif dialect == "postgres" and reporter == "lcov":
        # Whole-tree capture; ignore gcovr filters (artifact does not filter).
        return run_postgres_lcov(object_directory or source_dir, json_out=json_out)

    return run_gcovr(
        source_dir,
        filters=filters,
        jobs=jobs,
        json_out=json_out,
        root=root,
        object_directory=object_directory,
    )


def run_gcovr(
    source_dir: Path,
    *,
    filters: Sequence[str] = DEFAULT_FILTERS,
    jobs: Optional[int] = None,
    json_out: Optional[Path] = None,
    root: Optional[Path] = None,
    object_directory: Optional[Path] = None,
) -> JsonDict:
    """Build a coverage report for whatever counters are currently in *source_dir*.

    Returns gcovr's JSON summary: aggregate totals plus one entry per file. The summary form is used
    rather than the full one because it is the aggregate that is being reported, and the full form
    carries every line of a million-line codebase.

    Takes ~20 seconds on PostgreSQL, which is why campaigns snapshot instead of reporting.

    *root* is the path gcovr strips prefixes against (and relative ``--filter`` patterns resolve under).
    Defaults to *source_dir*. DuckDB's cmake out-of-tree build puts ``.gcno``/``.gcda`` under a build
    directory while sources live in the checkout, so pass ``root=<checkout>`` and
    ``object_directory=<build>`` (or search *source_dir* when that *is* the build tree).
    """
    report_root = root or source_dir
    search = object_directory or source_dir
    destination = json_out or (search / ".gcovr-summary.json")
    # Invoked as a module of *this* interpreter rather than as a bare `gcovr`, so it is found whether
    # or not the virtualenv's bin directory happens to be on PATH -- which it is not when the
    # interpreter was started by absolute path, e.g. `.venv/bin/python -m evaluation.coverage.report`.
    command = [
        sys.executable, "-m", "gcovr",
        "--root", str(report_root),
        str(search),
        f"--merge-mode-functions={_MERGE_MODE}",
        f"--gcov-ignore-parse-errors={_IGNORE_PARSE_ERRORS}",
        "--json-summary", str(destination),
        "-j", str(jobs or os.cpu_count() or 4),
    ]  # fmt: skip
    if _IGNORE_GCOV_ERRORS:
        # Immediately after the parse-errors flag.
        command.insert(6, f"--gcov-ignore-errors={_IGNORE_GCOV_ERRORS}")
    for pattern in filters:
        command += ["--filter", pattern]
    # cwd matters: a relative --filter is resolved against it, so running from anywhere else silently
    # matches nothing and reports 0 out of 0.
    result = subprocess.run(command, cwd=report_root, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"gcovr failed ({result.returncode}): {result.stderr.strip()[-2000:]}")
    with open(destination, encoding="utf-8") as handle:
        report: JsonDict = json.load(handle)
    return report


def totals_from(report: JsonDict) -> CoverageTotals:
    """The aggregate numbers out of a gcovr summary."""

    def count(key: str) -> int:
        return int(report.get(key, 0) or 0)

    return CoverageTotals(
        lines_covered=count("line_covered"),
        lines_total=count("line_total"),
        branches_covered=count("branch_covered"),
        branches_total=count("branch_total"),
        functions_covered=count("function_covered"),
        functions_total=count("function_total"),
    )


def fixed_denominator(reports: Sequence[JsonDict]) -> dict[str, tuple[int, int, int]]:
    """One line/branch/function total per file, taken across *all* reports.

    Why a curve needs this: a report only contains translation units that have written a .gcda, and a
    module loaded on demand — a text-search dictionary, an encoding conversion — writes its first one
    the moment it is used. Its lines then *join* the denominator mid-run, and the percentage can fall
    while coverage has only grown::

        round 10    26,961 / 378,016 = 7.13%
        round 20    27,400 / 391,000 = 7.01%     <- more covered, lower percentage

    Taking the maximum per file across every snapshot gives one denominator for the whole run, against
    which a file absent from an early snapshot is simply zero-covered — which is what it was.
    """
    totals: dict[str, tuple[int, int, int]] = {}
    for report in reports:
        for entry in report.get("files") or []:
            name = str(entry["filename"])
            previous = totals.get(name, (0, 0, 0))
            totals[name] = (
                max(previous[0], int(entry.get("line_total", 0) or 0)),
                max(previous[1], int(entry.get("branch_total", 0) or 0)),
                max(previous[2], int(entry.get("function_total", 0) or 0)),
            )
    return totals


def totals_against(report: JsonDict, denominator: dict[str, tuple[int, int, int]]) -> CoverageTotals:
    """*report*'s covered counts, over a denominator fixed by :func:`fixed_denominator`."""
    covered = [0, 0, 0]
    for entry in report.get("files") or []:
        covered[0] += int(entry.get("line_covered", 0) or 0)
        covered[1] += int(entry.get("branch_covered", 0) or 0)
        covered[2] += int(entry.get("function_covered", 0) or 0)
    lines = sum(value[0] for value in denominator.values())
    branches = sum(value[1] for value in denominator.values())
    functions = sum(value[2] for value in denominator.values())
    return CoverageTotals(
        lines_covered=covered[0],
        lines_total=lines,
        branches_covered=covered[1],
        branches_total=branches,
        functions_covered=covered[2],
        functions_total=functions,
    )


class Sampler:
    """Copies the counter files on a cadence, and remembers what the run had done by each one.

    Shared by every campaign driver — the eqgen arms in :mod:`evaluation.coverage.campaign` and the external-tool
    arms in :mod:`evaluation.coverage.run_sqlancerpp` — so that all of them write the same ``manifest.json``
    and :mod:`evaluation.coverage.report` can read any of them without knowing which tool produced it.

    The cadence can be in rounds (*every*) or in seconds (*every_seconds*), and for a long campaign it has
    to be the latter. A six-hour eqgen run gets through ~223,000 rounds; at one snapshot every 10 rounds
    that would be 22,000 snapshots and days of reporting. Every ten minutes gives 37 and a better curve.

    *queries* is a count the driver maintains, recorded alongside elapsed time. Comparisons are made at
    equal wall clock — that is the convention, and it correctly prices a tool that spends time to reach
    more code — so the query count is a diagnostic for *why* a curve moved, not the axis of comparison.
    """

    def __init__(
        self,
        source: Path,
        out_dir: Path,
        *,
        every: int = 10,
        every_seconds: Optional[float] = None,
        max_seconds: Optional[float] = None,
        verbose: bool = True,
        flush: Optional[Callable[[], None]] = None,
    ) -> None:
        self.source = source
        self.out_dir = out_dir
        self.every = every
        self.every_seconds = every_seconds
        self.max_seconds = max_seconds
        self.verbose = verbose
        # Called immediately before each snapshot. For PostgreSQL it restarts the cluster, which is what
        # makes the long-lived processes (checkpointer, walwriter, autovacuum) write their counters --
        # otherwise their whole contribution lands as a jump in the final snapshot instead of accruing
        # along the curve. See PgCluster.restart.
        self.flush = flush
        self.started = time.monotonic()
        self.last_snapshot = self.started
        self.queries = 0
        self.entries: list[JsonDict] = []

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def out_of_time(self) -> bool:
        return self.max_seconds is not None and self.elapsed >= self.max_seconds

    def due(self) -> bool:
        """Whether a time-based snapshot is owed. Only meaningful when *every_seconds* is set."""
        return self.every_seconds is not None and (time.monotonic() - self.last_snapshot) >= self.every_seconds

    def record(self, label: str, *, round_number: Optional[int] = None, flush: bool = True) -> None:
        """Snapshot the counters. Pass ``flush=False`` for the final snapshot.

        The flush hook restarts the engine, and the final snapshot is deliberately taken *after* the
        cluster has been stopped and its data directory removed — stopping is itself the flush, and a
        stronger one, because it retires every process rather than recycling them. Flushing there tried
        to restart a cluster that no longer existed and took down an otherwise complete run.
        """
        if flush and self.flush is not None:
            self.flush()
        destination = self.out_dir / f"snapshot_{len(self.entries):04d}_{label}"
        files = take_snapshot(self.source, destination)
        entry: JsonDict = {
            "label": label,
            "round": round_number,
            "elapsed_seconds": round(self.elapsed, 3),
            "queries": self.queries,
            "gcda_files": files,
            "path": destination.name,
        }
        self.entries.append(entry)
        self.last_snapshot = time.monotonic()
        if self.verbose:
            print(
                f"  [coverage] {label}: {files} counter files, {entry['queries']} queries, {entry['elapsed_seconds']}s elapsed",
                flush=True,
            )

    def count_round(self, round_number: int, queries: int) -> None:
        """Note that a round ran *queries* queries, and snapshot if this is a sampling point."""
        self.queries += queries
        if self.due() if self.every_seconds is not None else (round_number + 1) % self.every == 0:
            self.record(f"round{round_number}", round_number=round_number)
