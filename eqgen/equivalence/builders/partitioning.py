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

"""Split the rows into groups, then union the groups back together::

    SELECT * FROM even_rows UNION ALL SELECT * FROM odd_rows

Same rows as the base table, as long as the split puts **every** row in **exactly one** group.
Miss a row and the object is short one; put a row in two groups and it appears twice. Either way
the mismatch looks like an engine bug when it is ours, so the two builders below spend most of
their code on edge cases: negatives, and NULLs.

Neither partition builder writes the ``WHERE`` itself. Each hands its branches a
:class:`RowFilterConstraint` and asks for a relation, so a branch can be any object kind and can
itself be arbitrarily deep.

Also: set-algebra round trips (``R UNION ALL empty``, ``R EXCEPT ALL empty``) and a rank-modulus
partition (``UNION ALL`` over ``MOD(rank, N)`` residues).
"""

from __future__ import annotations

import random
from typing import Optional, Type

from eqgen.builder.constraint_set import Constraint, ConstraintSet
from eqgen.core.types import IntegerType, NumericType, SqlType, TypeProperty
from eqgen.equivalence.ast import (
    CreateTable,
    CreateView,
    EqNode,
    EquivalentRelation,
    ExceptAllQuery,
    IntersectAllQuery,
    SelectQuery,
    UnionAllQuery,
)
from eqgen.equivalence.builders.base import EquivalenceBuilder
from eqgen.equivalence.constraints import RowFilterConstraint
from eqgen.equivalence.context import EquivalenceContext
from eqgen.ir import expr

#: Residues for :class:`RankModUnionQueryBuilder`. A ``UNION ALL`` needs ``>= 2`` branches.
_RANK_MODULI = (2, 3, 4)

#: ``WHERE 1 = 0`` — empty relation for set-algebra round trips.
_EMPTY = expr.eq(expr.int_lit(1), expr.int_lit(0))


class PartitionUnionQueryBuilder(EquivalenceBuilder[UnionAllQuery]):
    """Even rows in one branch, odd rows in the other::

        SELECT * FROM t WHERE MOD(c_int, 2) = 0
        UNION ALL
        SELECT * FROM t WHERE MOD(c_int, 2) <> 0 OR c_int IS NULL

    Both details in the second branch were row-loss bugs once:

    ``<> 0`` rather than ``= 1``, because ``MOD`` takes the dividend's sign in several
    engines, so ``MOD(-1, 2)`` is ``-1``. With ``= 1``, the value ``-1`` matches neither branch
    and vanishes.

    ``OR c_int IS NULL``, because ``MOD(NULL, 2)`` is NULL, and ``NULL <> 0`` is NULL rather
    than TRUE. Without it a NULL key vanishes too.

    Needs no predicate plugin, so this one always works.
    """

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return [RowFilterConstraint]

    @staticmethod
    def _pick_integer_key(context: EquivalenceContext) -> Optional[tuple[str, SqlType]]:
        """A column whose values are whole numbers, or ``None``.

        A scaled decimal will not do::

            MOD(12.34, 2)  =  0.34      -- neither = 0 nor <> 0 in a useful sense

        ``NUMERIC(38, 0)`` is fine, ``NUMERIC(10, 2)`` is not.
        """
        eligible = [
            (column.get_column_name(), column.get_data_type())
            for column in context.base_table.get_column_list()
            if isinstance(column.get_data_type(), NumericType) and column.get_data_type().get_scale() in (None, 0)
        ]
        if not eligible:
            return None
        return EquivalenceBuilder._select_key(eligible, context)

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[UnionAllQuery]:
        key = self._pick_integer_key(context)
        if key is None:
            return None  # nothing to partition on; dispatch falls back to another builder
        name, data_type = key
        current = self._current_filter(constraint_set)
        key_ref = expr.col(name, data_type)
        even = expr.eq(expr.mod(key_ref, 2), expr.int_lit(0))
        odd = expr.or_(expr.ne(expr.mod(key_ref, 2), expr.int_lit(0)), expr.is_null(key_ref))

        branches: list[EquivalentRelation] = []
        for branch_predicate in (even, odd):
            branch = self.builder_factory.build_subtree(
                EquivalentRelation,  # type: ignore[type-abstract]
                ConstraintSet([RowFilterConstraint(expr.conjoin(current, branch_predicate))]),
                context,
            )
            if branch is None:
                return None
            branches.append(branch)
        return UnionAllQuery(*branches)


class TlpPartitionUnionQueryBuilder(EquivalenceBuilder[UnionAllQuery]):
    """Three branches from a plugin predicate ``p``::

        SELECT * FROM t WHERE (c_int > 3)
        UNION ALL
        SELECT * FROM t WHERE NOT (c_int > 3)
        UNION ALL
        SELECT * FROM t WHERE (c_int > 3) IS NULL

    A SQL comparison gives TRUE, FALSE or NULL, and there is one branch for each, so every row
    lands in exactly one — no extra arm needed for NULLs, unlike the parity split above.

    Works for any ``p`` that gives the same answer each time it is evaluated; ``random() < 0.5``
    would put a row in two branches or none. Declines when no predicate source is configured.

    From Rigger & Su, OOPSLA'20.
    """

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return [RowFilterConstraint]

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[UnionAllQuery]:
        predicate = self._generated_predicate(context)
        if predicate is None:
            return None  # no predicate source configured; another builder takes the slot
        current = self._current_filter(constraint_set)
        branch_predicates = (predicate, expr.not_(predicate), expr.is_null(predicate))

        branches: list[EquivalentRelation] = []
        for branch_predicate in branch_predicates:
            branch = self.builder_factory.build_subtree(
                EquivalentRelation,  # type: ignore[type-abstract]
                ConstraintSet([RowFilterConstraint(expr.conjoin(current, branch_predicate))]),
                context,
            )
            if branch is None:
                return None
            branches.append(branch)
        return UnionAllQuery(*branches)


class RankModUnionQueryBuilder(EquivalenceBuilder[UnionAllQuery]):
    """Split rows by ``MOD(rank, N)`` and ``UNION ALL`` the residues::

        CREATE TABLE keyed AS SELECT …, ROW_NUMBER() OVER (…) AS eq_rank FROM …
        SELECT * FROM keyed WHERE MOD(eq_rank, N) = 0
        UNION ALL
        SELECT * FROM keyed WHERE MOD(eq_rank, N) = 1
        …

    The rank is unique and non-null, so every row lands in exactly one residue — no NULL
    hazard, and no base integer key required. Uses a materialized ``ROW_NUMBER()`` key (same
    pattern as the join builders) rather than Snowflake's ``AUTOINCREMENT`` ranked table.

    Does not honour an inbound row filter: the rank table is built from the full base body.
    """

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return []

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[UnionAllQuery]:
        del constraint_set
        modulus = random.choice(_RANK_MODULI)
        rank_name = context.names.generate_column_name("eq_rank")
        keyed = self._materialize_row_key(context, rank_name)
        if keyed is None or not keyed.base_items:
            return None
        rank_ref = expr.col(rank_name, IntegerType())
        branches: list[EquivalentRelation] = []
        for residue in range(modulus):
            predicate = expr.eq(expr.mod(rank_ref, modulus), expr.int_lit(residue))
            branches.append(CreateView.build(context.namer, SelectQuery(keyed.relation, keyed.base_items, predicate)))
        return UnionAllQuery(*branches)


class UnionEmptyRoundTripBuilder(EquivalenceBuilder[UnionAllQuery]):
    """``R UNION ALL (R WHERE FALSE)`` — bag identity (Cosette / textbook set algebra)."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return []

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[UnionAllQuery]:
        del constraint_set
        left = self.builder_factory.build_subtree(
            EquivalentRelation,  # type: ignore[type-abstract]
            ConstraintSet([]),
            context,
        )
        right = self.builder_factory.build_subtree(
            EquivalentRelation,  # type: ignore[type-abstract]
            ConstraintSet([RowFilterConstraint(_EMPTY)]),
            context,
        )
        if left is None or right is None:
            return None
        return UnionAllQuery(left, right)


class ExceptEmptyRoundTripBuilder(EquivalenceBuilder[ExceptAllQuery]):
    """``R EXCEPT ALL (R WHERE FALSE)`` — bag identity.

    Requires ``EXCEPT ALL`` (plain ``EXCEPT`` is DISTINCT and collapses duplicates). Weight 0
    on engines that lack ``EXCEPT ALL`` (MySQL-family, SQLite, CrateDB).
    """

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return []

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[ExceptAllQuery]:
        del constraint_set
        left = self.builder_factory.build_subtree(
            EquivalentRelation,  # type: ignore[type-abstract]
            ConstraintSet([]),
            context,
        )
        right = self.builder_factory.build_subtree(
            EquivalentRelation,  # type: ignore[type-abstract]
            ConstraintSet([RowFilterConstraint(_EMPTY)]),
            context,
        )
        if left is None or right is None:
            return None
        return ExceptAllQuery(left, right)


class IntersectSelfRoundTripBuilder(EquivalenceBuilder[IntersectAllQuery]):
    """``R INTERSECT ALL R`` — bag identity when both sides are the same multiset.

    Requires ``INTERSECT ALL``. Weight 0 on engines that lack it or where intersect is not
    multiset-preserving.
    """

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return []

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[IntersectAllQuery]:
        del constraint_set
        left = self.builder_factory.build_subtree(
            EquivalentRelation,  # type: ignore[type-abstract]
            ConstraintSet([]),
            context,
        )
        right = self.builder_factory.build_subtree(
            EquivalentRelation,  # type: ignore[type-abstract]
            ConstraintSet([]),
            context,
        )
        if left is None or right is None:
            return None
        return IntersectAllQuery(left, right)


class DistinctUnionDuplicateQueryBuilder(EquivalenceBuilder[SelectQuery]):
    """``SELECT DISTINCT * FROM (R UNION ALL R)`` — bag identity, unconditionally.

    Plain ``SELECT DISTINCT *`` is only row-preserving when the source has no genuine
    duplicates, which nothing here guarantees. So a :meth:`_materialize_row_key` key rides
    along instead: doubling ``(base cols + key)`` and taking ``DISTINCT`` over *all* of it can
    never collapse two rows that were meant to stay two — each physical row keeps its own
    key — and only then does an outer projection drop the key back out. A synthetic key beats
    checking for a real one (e.g. ``c_pk``) because it needs no assumption about what the
    source's columns happen to be.
    """

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return []

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[SelectQuery]:
        del constraint_set
        items = self._passthrough_items(context)
        if not items:
            return None
        if not all(item.data_type.get_properties() & TypeProperty.GROUPABLE for item in items):
            return None
        key_name = context.names.generate_column_name("eq_dd")
        keyed = self._materialize_row_key(context, key_name)
        if keyed is None or not keyed.base_items:
            return None
        # Materialize the bag doubling: ``FROM (UNION ALL)`` is not a named source for SELECT.
        doubled = CreateTable.build(context.namer, UnionAllQuery(keyed.relation, keyed.relation))
        deduped = CreateTable.build(context.namer, SelectQuery(doubled, None, distinct=True))
        return SelectQuery(deduped, keyed.base_items)
