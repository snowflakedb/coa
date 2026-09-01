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

"""What was found, the running totals, and the ``.sql`` file someone can actually run.

Each finding gets a self-contained script — both databases rebuilt from nothing, then the query::

    -- ERROR
    -- engine: duckdb 1.5.0 (python wheel, in-memory)
    -- seed: 1785390952
    -- EQUIVALENT error: Binder Error: ...
    -- ============ database 1: the base table ============
    CREATE TABLE t (c_int BIGINT, ...);
    INSERT INTO t VALUES (NULL, ...);
    CREATE TABLE t0 AS SELECT * FROM t;   -- same-base forks only
    CREATE TABLE t1 AS SELECT * FROM t;
    -- ============ database 2: the equivalent ============
    ...
    -- ============ the query, run against each ============
    SELECT c_int FROM t0, t1 WHERE ...;
    -- ============ mismatch results ============
    -- only in base (1 distinct row(s)):
    --   ×1 (42,)
    -- only in equivalent (1 distinct row(s)):
    --   ×1 (None,)

The header lines are not decoration. "MISMATCH on DuckDB" gives whoever picks this up nothing; the
version and the settings that decide how values compare are what make it actionable. For
mismatches, the result multiset diff belongs in the same file — otherwise the reader has to
re-run both sides just to see *what* disagreed. Fork rounds must emit the base-side
``CREATE TABLE t_i AS SELECT * FROM t`` copies too: the equivalent DDL already installs those
names, but the base half of the file otherwise only has ``t`` and the query (``FROM t0, t1``)
fails with a catalog error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from eqgen.core.catalog import Table
from eqgen.fuzz.adapter import DialectAdapter
from eqgen.fuzz.database import (
    FLOAT_ABS_TOL,
    FLOAT_REL_TOL,
    Row,
    base_setup_statements,
    fork_copy_statements,
    hidden_base_name,
)
from eqgen.fuzz.round import RoundOutcome

#: Cap how many distinct differing rows are written into a mismatch repro / findings index.
#: Enough to diagnose; not a dump of a cartesian-product blow-up.
_MISMATCH_ROW_LOG_LIMIT = 32


@dataclass(frozen=True)
class Finding:
    """One reportable outcome: a mismatch, a one-sided error, or a crash."""

    kind: str
    round_number: int
    seed: int
    query: str
    only_in_base: list[tuple[Row, int]] = field(default_factory=list)
    only_in_equivalent: list[tuple[Row, int]] = field(default_factory=list)
    base_error: Optional[str] = None
    equivalent_error: Optional[str] = None
    note: Optional[str] = None


@dataclass
class FuzzReport:
    """Running totals for a fuzz session."""

    rounds: int = 0
    queries_run: int = 0
    passed: int = 0
    skipped: int = 0
    discarded_unbuildable: int = 0
    discarded_not_equivalent: int = 0
    findings: list[Finding] = field(default_factory=list)
    skipped_known_issues: dict[str, int] = field(default_factory=dict)
    #: Queries that passed only because of the float tolerance, and the rows it absorbed.
    tolerated_queries: int = 0
    tolerated_rows: int = 0

    def summary(self) -> str:
        lines = [
            f"rounds {self.rounds}, queries {self.queries_run}, pass {self.passed}, skipped {self.skipped}",
            f"findings {len(self.findings)}"
            + (f" ({', '.join(sorted({f.kind for f in self.findings}))})" if self.findings else ""),
        ]
        if self.discarded_unbuildable:
            lines.append(f"discarded (would not build) {self.discarded_unbuildable}")
        if self.discarded_not_equivalent:
            # A generator defect, and reported separately for that reason: it must never be
            # silently folded in with engine findings.
            lines.append(f"DISCARDED (generator produced a non-equivalent object) {self.discarded_not_equivalent}")
        if self.tolerated_queries:
            lines.append(
                f"float tolerance reconciled {self.tolerated_rows} row(s) across "
                f"{self.tolerated_queries} otherwise-passing query/queries "
                f"(rel={FLOAT_REL_TOL:g} abs={FLOAT_ABS_TOL:g})"
            )
        for label, count in sorted(self.skipped_known_issues.items()):
            lines.append(f"known issue {label}: {count}")
        return "\n".join(lines)


def _is_worker_timeout_noise(note: Optional[str]) -> bool:
    """Wall-clock worker kills (hang / OOM SIGKILL) are harness noise, not oracle bugs."""
    if not note:
        return False
    lower = note.lower()
    return "did not respond within" in lower or "sigkill" in lower


def record_round(report: FuzzReport, outcome: RoundOutcome, round_number: int) -> list[Finding]:
    """Fold one round's outcome into *report* and return the findings it produced."""
    if outcome.setup_error is not None:
        report.discarded_unbuildable += 1
        return []
    if outcome.object_diverged:
        # The generator produced something that is not equivalent. Every query result from this round
        # is untrustworthy, because a divergence would be blamed on the engine.
        report.discarded_not_equivalent += 1
        return []

    found: list[Finding] = []
    if outcome.setup_crash is not None and not _is_worker_timeout_noise(outcome.crash_note or outcome.setup_crash):
        # A crash with no query to blame: the engine died while building the object. Reported rather
        # than swallowed, because "this DDL kills the engine" is at least as interesting as "this query
        # does" — and the repro file carries the statements that did it.
        found.append(
            Finding(
                kind="crash",
                round_number=round_number,
                seed=outcome.seed,
                query="-- no query ran: the engine died while building the equivalence in this file",
                note=outcome.crash_note,
            )
        )
    elif outcome.setup_crash is not None:
        report.skipped += 1
        report.skipped_known_issues["worker-timeout"] = report.skipped_known_issues.get("worker-timeout", 0) + 1

    if outcome.crashed_query is not None and not _is_worker_timeout_noise(outcome.crash_note):
        found.append(
            Finding(
                kind="crash",
                round_number=round_number,
                seed=outcome.seed,
                query=outcome.crashed_query,
                note=outcome.crash_note,
            )
        )
    elif outcome.crashed_query is not None:
        report.skipped += 1
        report.skipped_known_issues["worker-timeout"] = report.skipped_known_issues.get("worker-timeout", 0) + 1

    report.rounds += 1
    report.queries_run += len(outcome.results)
    report.passed += sum(1 for result in outcome.results if result.is_pass)
    tolerated = [result for result in outcome.results if result.is_pass and result.reconciled]
    report.tolerated_queries += len(tolerated)
    report.tolerated_rows += sum(result.reconciled for result in tolerated)
    known = [result for result in outcome.results if result.is_known_issue]
    report.skipped += sum(1 for result in outcome.results if result.is_uncomparable) + len(known)
    for result in known:
        label = result.equivalent_known_issue or "unlabelled"
        report.skipped_known_issues[label] = report.skipped_known_issues.get(label, 0) + 1

    for result in outcome.findings:
        found.append(
            Finding(
                kind="mismatch" if result.is_mismatch else "error",
                round_number=round_number,
                seed=outcome.seed,
                query=result.query,
                only_in_base=result.only_in_base,
                only_in_equivalent=result.only_in_equivalent,
                base_error=result.base_error,
                equivalent_error=result.equivalent_error,
            )
        )
    report.findings.extend(found)
    return found


def format_mismatch_results(
    only_in_base: Sequence[tuple[Row, int]],
    only_in_equivalent: Sequence[tuple[Row, int]],
    *,
    limit: int = _MISMATCH_ROW_LOG_LIMIT,
    prefix: str = "-- ",
) -> list[str]:
    """Comment lines describing the multiset diff that made a mismatch reportable.

    Counts are always logged; individual rows are capped at *limit* so a cartesian blow-up
    cannot turn every finding into a multi-megabyte file.
    """

    def _block(label: str, entries: Sequence[tuple[Row, int]]) -> list[str]:
        distinct = len(entries)
        total = sum(count for _, count in entries)
        lines = [f"{prefix}{label} ({distinct} distinct row(s), {total} row(s) counting multiplicity):"]
        if not entries:
            lines.append(f"{prefix}  (none)")
            return lines
        shown = entries[:limit]
        for row, count in shown:
            lines.append(f"{prefix}  ×{count} {row!r}")
        omitted = distinct - len(shown)
        if omitted:
            lines.append(f"{prefix}  … {omitted} more distinct row(s) omitted")
        return lines

    return (
        [f"{prefix}============ mismatch results ============"]
        + _block("only in base", only_in_base)
        + _block("only in equivalent", only_in_equivalent)
    )


def repro_script(
    adapter: DialectAdapter,
    table: Table,
    rows: Sequence[Row],
    outcome: RoundOutcome,
    query: str,
    *,
    kind: str,
    session: Sequence[tuple[str, str]] = (),
    base_error: Optional[str] = None,
    equivalent_error: Optional[str] = None,
    only_in_base: Sequence[tuple[Row, int]] = (),
    only_in_equivalent: Sequence[tuple[Row, int]] = (),
) -> str:
    """A self-contained script that rebuilds both sides and runs *query*.

    Both databases in one file, clearly separated, so the reader can run each half independently — the
    first question about any finding is "does the base really do something different", and answering it
    should not require reconstructing anything. When both sides returned rows that disagreed, the
    multiset diff is appended as comments so the disagreement is visible without re-running.
    """
    header = [
        f"-- {kind}",
        f"-- engine: {adapter.engine_banner()}",
        f"-- seed: {outcome.seed}",
    ]
    header += [f"-- {label}: {value}" for label, value in session]
    if outcome.crash_note:
        header.append(f"-- crash: {outcome.crash_note}")
    if base_error is not None:
        header.append(f"-- BASE error: {base_error}")
    if equivalent_error is not None:
        header.append(f"-- EQUIVALENT error: {equivalent_error}")
    if only_in_base or only_in_equivalent:
        header.append(
            f"-- mismatch: {len(only_in_base)} distinct only in base, "
            f"{len(only_in_equivalent)} distinct only in equivalent"
        )

    base_side = ["", "-- ============ database 1: the base table ============"]
    base_side += [f"{statement};" for statement in base_setup_statements(adapter, table, rows)]
    # Same-base forks: build_base copies ``t`` → ``t0``/``t1``/…; the equivalent side gets those
    # names from the generated DDL, but the base half of this file must recreate them too or the
    # workload (``FROM t0, t1, …``) fails with "table t0 does not exist".
    names = outcome.exposed_names or (table.get_sql_name(),)
    base_side += [f"{statement};" for statement in fork_copy_statements(table.get_sql_name(), names, adapter=adapter)]

    equivalent_side = ["", "-- ============ database 2: the equivalent ============"]
    equivalent_side += [f"{statement};" for statement in base_setup_statements(adapter, table, rows)]
    equivalent_side.append(f"{adapter.rename_aside_sql(table.get_sql_name(), hidden_base_name(table))};")
    equivalent_side += [f"{statement};" for statement in outcome.equivalent_statements]

    query_section = ["", "-- ============ the query, run against each ============", f"{query.rstrip().rstrip(';')};"]
    results_section: list[str] = []
    if only_in_base or only_in_equivalent:
        results_section = [""] + format_mismatch_results(only_in_base, only_in_equivalent)
    return "\n".join(header + base_side + equivalent_side + query_section + results_section) + "\n"


def write_finding(
    directory: Path,
    adapter: DialectAdapter,
    table: Table,
    rows: Sequence[Row],
    outcome: RoundOutcome,
    finding: Finding,
    index: int,
    *,
    session: Sequence[tuple[str, str]] = (),
) -> Path:
    """Write one finding's repro and append it to the findings index."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{finding.kind}_round{finding.round_number}_{index}.sql"
    label = finding.kind.upper() + (f" ({finding.note})" if finding.note else "")
    path.write_text(
        repro_script(
            adapter,
            table,
            rows,
            outcome,
            finding.query,
            kind=label,
            session=session,
            base_error=finding.base_error,
            equivalent_error=finding.equivalent_error,
            only_in_base=finding.only_in_base,
            only_in_equivalent=finding.only_in_equivalent,
        )
    )
    with (directory / "findings.txt").open("a") as index_file:
        index_file.write(f"round {finding.round_number} seed {finding.seed}: {path.name}\n    {finding.query}\n")
        if finding.equivalent_error:
            index_file.write(f"    EQUIVALENT error: {finding.equivalent_error}\n")
        if finding.base_error:
            index_file.write(f"    BASE error: {finding.base_error}\n")
        if finding.only_in_base or finding.only_in_equivalent:
            for line in format_mismatch_results(
                finding.only_in_base,
                finding.only_in_equivalent,
                prefix="    ",
            ):
                index_file.write(f"{line}\n")
    return path
