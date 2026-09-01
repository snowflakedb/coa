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

"""Run one query on both sides and decide what a difference means.

Two comparisons, catching different things::

    SELECT <all columns> FROM t         on both sides   -- checks *us*: is the object we
                                                        -- generated really the same rows?
    SELECT c_int FROM t WHERE ...       on both sides   -- checks the *engine*, and this is
                                                        -- where the real bugs come from

Five outcomes per query, and only two of them get reported::

    base ok, other ok, rows same        PASS
    base ok, other ok, rows differ      MISMATCH        <- reported
    base ok, other failed               ERROR           <- reported
    base ok, other failed, known cause  skipped, counted
    base failed                         skipped, whatever the other side did

That last line is the one to get right. If the base rejected the query, the query was already
invalid, so what the other side did tells us nothing. Reverse it and the run fills with findings
that are just bad generated SQL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Sequence

from eqgen.core.catalog import Table
from eqgen.fuzz.database import Database, MultisetDiff, Row, compare_multisets

#: Optional hook: structural plan id for one side of a comparison.
#: A callback rather than a method, so a caller can supply one without the harness importing it or
#: knowing what a plan is. Passing one is the only way plans get collected — there is deliberately no
#: hook on :class:`~eqgen.fuzz.database.Database`, because two mechanisms for one job need
#: reconciling and the reconciliation is where an unguarded call hid.
PlanFingerprinter = Callable[[Database, str], Optional[str]]


@dataclass(frozen=True)
class QueryComparison:
    """The outcome of running one query on the base and on the equivalent."""

    query: str
    equal: bool
    only_in_base: list[tuple[Row, int]] = field(default_factory=list)
    only_in_equivalent: list[tuple[Row, int]] = field(default_factory=list)
    base_error: Optional[str] = None
    equivalent_error: Optional[str] = None
    equivalent_known_issue: Optional[str] = None
    #: Rows that agreed only through the float tolerance (see :attr:`MultisetDiff.reconciled`).
    #: Non-zero on a PASS means the pass was bought with leniency, so it is journaled and tallied.
    reconciled: int = 0
    #: Structural plan fingerprints when a ``plan_fingerprint`` hook was supplied; else ``None``.
    base_plan: Optional[str] = None
    equivalent_plan: Optional[str] = None
    @property
    def is_uncomparable(self) -> bool:
        """The base failed, so the query was already invalid. Never reportable."""
        return self.base_error is not None

    @property
    def is_pass(self) -> bool:
        return self.base_error is None and self.equivalent_error is None and self.equal

    @property
    def is_known_issue(self) -> bool:
        """The base ran and the equivalent raised a known non-bug. Skipped, and counted per label."""
        return self.base_error is None and self.equivalent_known_issue is not None

    @property
    def is_error(self) -> bool:
        """The base ran and the equivalent raised a *real* error — the equivalent broke a valid query."""
        return self.base_error is None and self.equivalent_error is not None and self.equivalent_known_issue is None

    @property
    def is_mismatch(self) -> bool:
        """Both sides ran and disagreed on the rows. A semantic divergence."""
        return self.base_error is None and self.equivalent_error is None and not self.equal

    @property
    def is_reportable(self) -> bool:
        return self.is_error or self.is_mismatch

    @property
    def verdict(self) -> str:
        """A one-line label, for the journal and the round summary."""
        if self.is_mismatch:
            base_n = len(self.only_in_base)
            equiv_n = len(self.only_in_equivalent)
            # Keep the journal line informative but short; the repro file carries the rows.
            sample_base = self.only_in_base[0][0] if self.only_in_base else None
            sample_equiv = self.only_in_equivalent[0][0] if self.only_in_equivalent else None
            return (
                f"MISMATCH: {base_n} distinct only in base, {equiv_n} distinct only in equivalent"
                f"; e.g. base={sample_base!r} equiv={sample_equiv!r}"
            )
        if self.is_error:
            return f"ERROR (equivalent): {self.equivalent_error}"
        if self.is_known_issue:
            return f"known-issue ({self.equivalent_known_issue})"
        if self.is_uncomparable:
            return f"uncomparable (base rejected it): {self.base_error}"
        if self.reconciled:
            return f"PASS ({self.reconciled} row(s) reconciled by float tolerance)"
        return "PASS"


def _safe_plan(
    fingerprinter: PlanFingerprinter,
    database: Database,
    query: str,
) -> Optional[str]:
    """*fingerprinter* applied to one side, never raising.

    This runs inside the forked worker, where an escaping exception kills the child and comes back as
    "the engine died on this query" — a false crash finding. So every failure becomes ``None``.
    """
    try:
        return fingerprinter(database, query)
    except Exception:  # noqa: BLE001 — plan collection must never become a finding
        return None


def compare_one(
    base: Database,
    equivalent: Database,
    query: str,
    *,
    plan_fingerprint: Optional[PlanFingerprinter] = None,
) -> QueryComparison:
    """Run *query* against both sides independently and diff the results.

    Independently: each side gets its own attempt, and neither failure suppresses the other's. That
    is what makes the base-failed case distinguishable from the equivalent-failed case.

    When *plan_fingerprint* is given, each side's structural plan id is attached for the caller.
    Failures yield ``None`` fingerprints and never change the verdict.
    """
    base_outcome = base.query(query)
    equivalent_outcome = equivalent.query(query)

    base_plan = equivalent_plan = None
    if plan_fingerprint is not None:
        base_plan = _safe_plan(plan_fingerprint, base, query)
        equivalent_plan = _safe_plan(plan_fingerprint, equivalent, query)

    if base_outcome.rows is not None and equivalent_outcome.rows is not None:
        diff = compare_multisets(base_outcome.rows, equivalent_outcome.rows)
        return QueryComparison(
            query,
            equal=diff.equal,
            only_in_base=diff.only_in_base,
            only_in_equivalent=diff.only_in_other,
            reconciled=diff.reconciled,
            base_plan=base_plan,
            equivalent_plan=equivalent_plan,
        )
    return QueryComparison(
        query,
        equal=False,
        base_error=base_outcome.error,
        equivalent_error=equivalent_outcome.error,
        equivalent_known_issue=equivalent_outcome.known_issue,
        base_plan=base_plan,
        equivalent_plan=equivalent_plan,
    )


@dataclass(frozen=True)
class ObjectComparison:
    """Whether the equivalence itself is equivalent — checked before any query runs.

    This is the generator's own correctness gate. If it fails, the queries that follow would report
    the generator's bug as the engine's, so a round that fails here is discarded rather than
    reported.
    """

    rows: MultisetDiff
    base_types: tuple[str, ...]
    equivalent_types: tuple[str, ...]

    @property
    def types_agree(self) -> bool:
        return self.base_types == self.equivalent_types

    @property
    def equal(self) -> bool:
        return self.rows.equal and self.types_agree

    @property
    def verdict(self) -> str:
        """A one-line label, for the journal and the round summary."""
        if self.equal:
            if self.rows.reconciled:
                return (
                    f"OK (rows and declared types agree; {self.rows.reconciled} row(s) "
                    "reconciled by float tolerance)"
                )
            return "OK (rows and declared types agree)"
        reasons = []
        if not self.rows.equal:
            reasons.append(
                f"rows differ: {len(self.rows.only_in_base)} only in base, {len(self.rows.only_in_other)} only in equivalent"
            )
        if not self.types_agree:
            reasons.append(f"types differ: base {self.base_types} vs equivalent {self.equivalent_types}")
        return "NOT EQUIVALENT -- " + "; ".join(reasons)


def compare_objects(base: Database, equivalent: Database, table: Table, columns: Sequence[str]) -> ObjectComparison:
    """Compare the base relation with the exposed equivalent, rows and declared types."""
    name = table.get_sql_name()
    return ObjectComparison(
        rows=compare_multisets(base.multiset(name, columns), equivalent.multiset(name, columns)),
        base_types=base.column_types(name, columns),
        equivalent_types=equivalent.column_types(name, columns),
    )


def compare_env(
    base: Database,
    equivalent: Database,
    catalog: Table,
    columns: Sequence[str],
    exposed_names: Sequence[str],
) -> ObjectComparison:
    """Pointwise :func:`compare_objects` for every name in *exposed_names* (EqEnv gate)."""
    names = tuple(exposed_names) if exposed_names else (catalog.get_sql_name(),)
    comparisons = [
        compare_objects(base, equivalent, Table(name, catalog.get_column_list()), columns) for name in names
    ]
    for comparison in comparisons:
        if not comparison.equal:
            return comparison
    return comparisons[0]


def errors(results: Iterable[QueryComparison]) -> list[QueryComparison]:
    return [result for result in results if result.is_error]


def mismatches(results: Iterable[QueryComparison]) -> list[QueryComparison]:
    return [result for result in results if result.is_mismatch]


def findings(results: Iterable[QueryComparison]) -> list[QueryComparison]:
    return [result for result in results if result.is_reportable]
