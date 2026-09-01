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

"""Joins that give back exactly the rows they started with.

Both take the base rows, write them out once with a key that tells each row apart, split or copy
them, and join them back together. The key is what makes the join one-to-one and complete: every row
matches exactly once, so nothing is dropped, nothing is duplicated, and nothing is null-padded.

    key       ROW_NUMBER() OVER (ORDER BY c_int)   unique, so the join cannot fan out
    complete  every key on the left has one on the right, so no row is lost

``FlagTableJoinQueryBuilder`` joins the rows against a table holding one flag row per key, and keeps
the flagged ones — which is all of them. Because that holds whatever the join type is, the join type
is drawn from config: `INNER`, `LEFT OUTER`, `RIGHT OUTER` and `FULL OUTER` all return the base rows
here, so one implementation covers four operators.

``SequenceOuterJoinQueryBuilder`` splits the columns in half instead, then puts the row back together
with a ``FULL OUTER JOIN`` on the key.

Both build their children directly rather than asking for them, and for the reason set out in
``EXTENDING.md``: a keyed relation and a half-width relation deliberately do **not** have the base
table's columns, and anything you ask the factory for does.
"""

from __future__ import annotations

from typing import Optional, Type

from eqgen.builder.constraint_set import Constraint, ConstraintSet
from eqgen.core.types import IntegerType
from eqgen.equivalence.ast import CreateTable, CreateView, EqNode, EquivalentRelation, JoinQuery, ProjectionItem, SelectQuery
from eqgen.equivalence.builders.base import EquivalenceBuilder
from eqgen.equivalence.constraints import RowFilterConstraint
from eqgen.equivalence.context import EquivalenceContext
from eqgen.ir import expr

_LEFT, _RIGHT = "l", "r"


def _qualified(alias: str, items: tuple[ProjectionItem, ...]) -> tuple[ProjectionItem, ...]:
    """The same columns, read from one side of the join: ``l.c_int AS c_int``."""
    return tuple(
        ProjectionItem(item.alias, expr.qualified_col(alias, item.alias, item.data_type), item.data_type) for item in items
    )


class LeftJoinEmptyQueryBuilder(EquivalenceBuilder[JoinQuery]):
    """``LEFT OUTER JOIN`` an empty relation on ``TRUE`` — every left row survives once.

    Portable anti-semantics without the ``ANTI`` keyword (algebra **(LeftEmpty)**)::

        SELECT l.c_int AS c_int FROM left l LEFT OUTER JOIN empty r ON TRUE

    The empty side is asked for as ``RowFilterConstraint(1 = 0)``, so any filter-honouring
    builder can serve it.
    """

    _EMPTY = expr.eq(expr.int_lit(1), expr.int_lit(0))

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return []

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[JoinQuery]:
        del constraint_set
        base_items = self._passthrough_items(context)
        if not base_items:
            return None
        left = self.builder_factory.build_subtree(
            EquivalentRelation,  # type: ignore[type-abstract]
            ConstraintSet([]),
            context,
        )
        right = self.builder_factory.build_subtree(
            EquivalentRelation,  # type: ignore[type-abstract]
            ConstraintSet([RowFilterConstraint(self._EMPTY)]),
            context,
        )
        if left is None or right is None:
            return None
        return JoinQuery(
            left,
            right,
            expr.eq(expr.int_lit(1), expr.int_lit(1)),
            "LEFT OUTER",
            _qualified(_LEFT, base_items),
            _LEFT,
            _RIGHT,
        )


class SemiJoinFlagRoundTripBuilder(EquivalenceBuilder[JoinQuery]):
    """``SEMI JOIN`` a complete flag table of synthetic keys — every keyed row matches once.

    Algebra **(SemiFlag)**. ``SEMI`` is DuckDB-native; weight it 0 on PostgreSQL.
    """

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return []

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[JoinQuery]:
        del constraint_set
        uid = context.names.generate_column_name("eq_sj")
        keyed = self._materialize_row_key(context, uid)
        if keyed is None or not keyed.base_items:
            return None
        flag_items = (ProjectionItem(uid, expr.col(uid, IntegerType()), IntegerType()),)
        flag_table = CreateTable.build(context.namer, SelectQuery(keyed.relation, flag_items))
        condition = expr.eq(
            expr.qualified_col(_LEFT, uid, IntegerType()),
            expr.qualified_col(_RIGHT, uid, IntegerType()),
        )
        return JoinQuery(
            keyed.relation,
            flag_table,
            condition,
            "SEMI",
            _qualified(_LEFT, keyed.base_items),
            _LEFT,
            _RIGHT,
        )


class FlagTableJoinQueryBuilder(EquivalenceBuilder[JoinQuery]):
    """Join the keyed rows against one flag row per key, and keep the flagged ones::

        CREATE TABLE t_table_1 AS SELECT c_int, c_txt, ROW_NUMBER() OVER (...) AS eq_uid_1 FROM t__base
        CREATE TABLE t_table_2 AS SELECT eq_uid_1 AS eq_uid_1, 1 AS eq_flag_1 FROM t_table_1

        SELECT l.c_int AS c_int, l.c_txt AS c_txt
        FROM t_table_1 l INNER JOIN t_table_2 r ON l.eq_uid_1 = r.eq_uid_1
        WHERE r.eq_flag_1 = 1

    Every key appears exactly once on each side, so the join matches every row exactly once and the
    flag keeps all of them. That holds for any join type, which is why the join type is a weight in
    the config rather than a fixed keyword: four operators, one identity, one implementation.
    """

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return []

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[JoinQuery]:
        del constraint_set
        uid = context.names.generate_column_name("eq_uid")
        flag = context.names.generate_column_name("eq_flag")
        keyed = self._materialize_row_key(context, uid)
        if keyed is None:
            return None

        # One (key, 1) row per keyed row. Built here, not asked for: no constraint can request
        # "one flag row per key", and its columns are not the base table's.
        flag_items = (
            ProjectionItem(uid, expr.col(uid, IntegerType()), IntegerType()),
            ProjectionItem(flag, expr.int_lit(1), IntegerType()),
        )
        flag_table = CreateTable.build(context.namer, SelectQuery(keyed.relation, flag_items))

        condition = expr.eq(expr.qualified_col(_LEFT, uid, IntegerType()), expr.qualified_col(_RIGHT, uid, IntegerType()))
        keep = expr.eq(expr.qualified_col(_RIGHT, flag, IntegerType()), expr.int_lit(1))
        join_type = context.config.join_type_weights.choose_one()
        return JoinQuery(
            keyed.relation,
            flag_table,
            condition,
            join_type.value,
            _qualified(_LEFT, keyed.base_items),
            _LEFT,
            _RIGHT,
            keep,
        )


class SequenceOuterJoinQueryBuilder(EquivalenceBuilder[JoinQuery]):
    """Split the columns in half, then join the halves back on the key::

        CREATE TABLE t_table_1 AS SELECT c_int, c_txt, ROW_NUMBER() OVER (...) AS eq_seq_1 FROM t__base
        CREATE VIEW  t_view_1  AS SELECT c_int, eq_seq_1 FROM t_table_1     -- the first half
        CREATE VIEW  t_view_2  AS SELECT c_txt, eq_seq_1 FROM t_table_1     -- the second

        SELECT l.c_int AS c_int, r.c_txt AS c_txt
        FROM t_view_1 l FULL OUTER JOIN t_view_2 r ON l.eq_seq_1 = r.eq_seq_1

    Both halves come from the same keyed table, so they hold exactly the same keys — the outer join
    therefore never null-pads, and taking each column from the side that has it rebuilds the row.

    Declines on a single-column table: there would be nothing to split.
    """

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return []

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[JoinQuery]:
        del constraint_set
        key = context.names.generate_column_name("eq_seq")
        keyed = self._materialize_row_key(context, key)
        if keyed is None or len(keyed.base_items) < 2:
            return None

        split = len(keyed.base_items) // 2
        left_cols, right_cols = keyed.base_items[:split], keyed.base_items[split:]
        key_item = ProjectionItem(key, expr.col(key, IntegerType()), IntegerType())
        left = CreateView.build(context.namer, SelectQuery(keyed.relation, (*left_cols, key_item)))
        right = CreateView.build(context.namer, SelectQuery(keyed.relation, (*right_cols, key_item)))

        condition = expr.eq(expr.qualified_col(_LEFT, key, IntegerType()), expr.qualified_col(_RIGHT, key, IntegerType()))
        projection = _qualified(_LEFT, left_cols) + _qualified(_RIGHT, right_cols)
        return JoinQuery(left, right, condition, "FULL OUTER", projection, _LEFT, _RIGHT)


class CrossJoinFilterAsInnerBuilder(EquivalenceBuilder[JoinQuery]):
    """``CROSS JOIN`` a complete flag table, filter on the key — equals ``INNER JOIN`` on that key::

        CREATE TABLE keyed AS SELECT …, ROW_NUMBER() OVER (…) AS eq_uid FROM …
        CREATE TABLE flag  AS SELECT eq_uid FROM keyed
        SELECT l.c0, … FROM keyed l CROSS JOIN flag r WHERE l.eq_uid = r.eq_uid

    Every key appears once on each side, so the filter keeps every left row exactly once.
    """

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return []

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[JoinQuery]:
        del constraint_set
        uid = context.names.generate_column_name("eq_xj")
        keyed = self._materialize_row_key(context, uid)
        if keyed is None or not keyed.base_items:
            return None
        flag_items = (ProjectionItem(uid, expr.col(uid, IntegerType()), IntegerType()),)
        flag_table = CreateTable.build(context.namer, SelectQuery(keyed.relation, flag_items))
        keep = expr.eq(
            expr.qualified_col(_LEFT, uid, IntegerType()),
            expr.qualified_col(_RIGHT, uid, IntegerType()),
        )
        return JoinQuery(
            keyed.relation,
            flag_table,
            None,  # CROSS JOIN — no ON
            "CROSS",
            _qualified(_LEFT, keyed.base_items),
            _LEFT,
            _RIGHT,
            keep,
        )
