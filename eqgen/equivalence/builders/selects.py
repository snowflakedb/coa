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

"""``SELECT * FROM <source> [WHERE <filter>]``. Looks like a placeholder; does two real jobs.

It is the only builder that turns a requested filter into an actual ``WHERE``::

    asked for:  RowFilterConstraint(MOD(c_int, 2) = 0)
    emits:      SELECT * FROM t WHERE MOD(c_int, 2) = 0

The builders that split rows never write a ``WHERE`` themselves — they pass the filter down and
ask for a relation — so without this one they would have nothing to hand their branches to.

It also asks for its own source, so this is where a chain of rewrites gets one link longer.
"""

from __future__ import annotations

from typing import Optional, Type

from eqgen.builder.constraint_set import Constraint, ConstraintSet
from eqgen.core.types import IntegerType
from eqgen.equivalence.ast import (
    CreateView,
    CteQuery,
    EqNode,
    LateralReprojectQuery,
    ProjectionItem,
    SelectQuery,
)
from eqgen.equivalence.builders.base import EquivalenceBuilder
from eqgen.equivalence.constraints import ColumnRewriteConstraint, RowFilterConstraint, SingleSourceConstraint
from eqgen.equivalence.context import EquivalenceContext
from eqgen.ir import expr


class SelectStarQueryBuilder(EquivalenceBuilder[SelectQuery]):
    """``SELECT * FROM <source> [WHERE <filter>]``.

    Uses ``*`` rather than listing columns, so it keeps working when the table's columns change.
    """

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return [RowFilterConstraint, SingleSourceConstraint]

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[SelectQuery]:
        source = self._dispatch_source(context, constraint_set)
        if source is None:
            return None
        return SelectQuery(source, None, self._current_filter(constraint_set))


class ExplicitProjectionQueryBuilder(EquivalenceBuilder[SelectQuery]):
    """The same rows, with the columns written out instead of ``*``::

        SELECT c_int, c_big, c_txt FROM t          -- rather than SELECT * FROM t

    An identity projection, so the rows and their types are unchanged. Worth having because it is
    the only portable builder that puts a real ``SELECT`` list in front of the engine — ``*`` and a
    column list are not the same path through name resolution.
    """

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return [RowFilterConstraint, SingleSourceConstraint]

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[SelectQuery]:
        items = self._passthrough_items(context)
        if not items:
            return None
        source = self._dispatch_source(context, constraint_set)
        if source is None:
            return None
        return SelectQuery(source, items, self._current_filter(constraint_set))


class CteQueryBuilder(EquivalenceBuilder[CteQuery]):
    """Read the source through a common table expression::

        WITH t_cte_1 AS (SELECT * FROM t__base WHERE MOD(c_int, 2) = 0) SELECT * FROM t_cte_1

    The filter goes inside the CTE, which is where it would go by hand, and leaves the engine to
    decide whether to fold the CTE in or compute it once.

    Portable: this is a plain CTE. Forced ``MATERIALIZED`` / ``NOT MATERIALIZED`` hints are
    :class:`MaterializedCteQueryBuilder` (Postgres and DuckDB).
    """

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return [RowFilterConstraint, SingleSourceConstraint]

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[CteQuery]:
        source = self._dispatch_source(context, constraint_set)
        if source is None:
            return None
        name = context.names.generate_object_name(f"{context.base_table.table_name}_cte")
        return CteQuery(source, name, self._current_filter(constraint_set))


class MaterializedCteQueryBuilder(EquivalenceBuilder[CteQuery]):
    """``WITH … AS MATERIALIZED (…)`` — force a snapshot CTE (Postgres / DuckDB / SQLite).

    DuckDB has no ``CREATE MATERIALIZED VIEW``; this is the planner-side substitute that still
    exercises materialize-once plans. Weight 0 on engines without the hint.
    """

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return [RowFilterConstraint, SingleSourceConstraint]

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[CteQuery]:
        source = self._dispatch_source(context, constraint_set)
        if source is None:
            return None
        name = context.names.generate_object_name(f"{context.base_table.table_name}_mcte")
        return CteQuery(source, name, self._current_filter(constraint_set), materialize=True)


class NotMaterializedCteQueryBuilder(EquivalenceBuilder[CteQuery]):
    """``WITH … AS NOT MATERIALIZED (…)`` — force inlining (Postgres / DuckDB / SQLite)."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return [RowFilterConstraint, SingleSourceConstraint]

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[CteQuery]:
        source = self._dispatch_source(context, constraint_set)
        if source is None:
            return None
        name = context.names.generate_object_name(f"{context.base_table.table_name}_nmcte")
        return CteQuery(source, name, self._current_filter(constraint_set), materialize=False)


class LateralReprojectQueryBuilder(EquivalenceBuilder[LateralReprojectQuery]):
    """Send every row through a one-row lateral subquery and take the same columns back::

        SELECT l.c_int AS c_int FROM t__base s, LATERAL (SELECT s.c_int AS c_int) AS l

    One row in, one row out, nothing selected that was not already there. The engine runs a
    correlated subquery per row to arrive at the rows it started with.

    Does not honour a row filter: the lateral is the whole point, and there is nowhere sensible to
    put a ``WHERE`` without changing what is being exercised. Not listing the constraint is how a
    builder says so — the factory then never routes one here.
    """

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return []

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[LateralReprojectQuery]:
        del constraint_set
        source = self._dispatch_source(context)
        if source is None or not source.get_signature():
            return None
        return LateralReprojectQuery(source)


class AddDropColumnQueryBuilder(EquivalenceBuilder[SelectQuery]):
    """Add a column, then drop it again by not selecting it::

        CREATE VIEW t_view_1 AS SELECT c_int, c_txt, CAST(NULL AS INTEGER) AS eq_tmp_col_1
                                FROM t__base
        SELECT c_int, c_txt FROM t_view_1                 -- the extra column projected away

    The intermediate view is built here rather than asked for, because it deliberately does **not**
    have the base table's columns — it has one more. Anything you ask the factory for comes back with
    the base's columns, so a child of a different shape is yours to make and yours to know the shape
    of.
    """

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return []

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[SelectQuery]:
        del constraint_set
        base_items = self._passthrough_items(context)
        if not base_items:
            return None
        source = self._dispatch_source(context)
        if source is None:
            return None
        extra_name = context.names.generate_column_name("eq_tmp_col")
        extra_type = IntegerType()
        extra = ProjectionItem(extra_name, expr.typed_null(extra_type), extra_type)
        wider = CreateView.build(context.namer, SelectQuery(source, (*base_items, extra)))
        return SelectQuery(wider, base_items)


class QualifyQueryBuilder(EquivalenceBuilder[SelectQuery]):
    """``QUALIFY ROW_NUMBER() OVER (ORDER BY c0) >= 1`` — identity filter over every row.

    Ranks start at 1, so the predicate keeps the full multiset while exercising QUALIFY
    (DuckDB) or its PostgreSQL subquery lowering.
    """

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return []

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[SelectQuery]:
        del constraint_set
        items = self._passthrough_items(context)
        if not items:
            return None
        source = self._dispatch_source(context)
        if source is None:
            return None
        order_col = expr.col(items[0].alias, items[0].data_type)
        keep_all = expr.ge(expr.row_number((order_col,)), expr.int_lit(1))
        return SelectQuery(source, items, qualify=keep_all)


class OrderedScanQueryBuilder(EquivalenceBuilder[SelectQuery]):
    """Inner ``ORDER BY`` forcing a sort — multiset unchanged (Def 1.1). Coverage-only."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return []

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[SelectQuery]:
        del constraint_set
        items = self._passthrough_items(context)
        if not items:
            return None
        source = self._dispatch_source(context)
        if source is None:
            return None
        return SelectQuery(source, items, order_by=(expr.col(items[0].alias, items[0].data_type),))


class DistinctAsGroupByQueryBuilder(EquivalenceBuilder[SelectQuery]):
    """``GROUP BY`` every column of a uniquely keyed relation — bag-preserving DISTINCT dual::

        CREATE TABLE keyed AS SELECT …, ROW_NUMBER() OVER (…) AS eq_k FROM …
        SELECT c0, c1, … FROM keyed GROUP BY c0, c1, …, eq_k

    The synthetic key makes each group size 1, so the multiset matches the base. Exercises the
    aggregate / distinct planner path without collapsing genuine duplicates.
    """

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return []

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[SelectQuery]:
        del constraint_set
        key_name = context.names.generate_column_name("eq_dg")
        keyed = self._materialize_row_key(context, key_name)
        if keyed is None or not keyed.base_items:
            return None
        group_keys = tuple(expr.col(item.alias, item.data_type) for item in keyed.base_items) + (
            expr.col(key_name, IntegerType()),
        )
        return SelectQuery(keyed.relation, keyed.base_items, group_by=group_keys)


class ProjectionQueryBuilder(EquivalenceBuilder[SelectQuery]):
    """``SELECT <exactly the columns that were asked for> FROM <source> [WHERE <filter>]``.

    The only builder that honours :class:`ColumnRewriteConstraint`, and therefore the only reason
    anything can *ask* for a relation of a particular shape::

        asked for:  (c_int, c_txt, ROW_NUMBER() OVER (ORDER BY c_int) AS eq_key_1)
        emits:      SELECT c_int, c_txt, ROW_NUMBER() OVER (ORDER BY c_int) AS eq_key_1 FROM t__base

    That is what lets a builder needing an oddly-shaped child — one with a key column added, say —
    ask for it instead of building it, so the child is drawn from the whole pool rather than frozen
    to one object kind. See ``_materialize_row_key``.

    Requires the constraint rather than merely supporting it: with no projection to reproduce there
    is nothing here that ``ExplicitProjectionQueryBuilder`` does not already do.
    """

    def required_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return [ColumnRewriteConstraint]

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return [ColumnRewriteConstraint, RowFilterConstraint, SingleSourceConstraint]

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[SelectQuery]:
        source = self._dispatch_source(context, constraint_set)
        if source is None:
            return None
        rewrite = constraint_set.get_constraint(ColumnRewriteConstraint)
        return SelectQuery(source, rewrite.projection, self._current_filter(constraint_set))
