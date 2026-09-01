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

"""Build a table, then mutate it without changing the multiset of rows::

    CREATE TABLE t_table_1 AS SELECT * FROM t__base
    DELETE FROM t_table_1 WHERE MOD(c_int, 2) = 1
    INSERT INTO t_table_1 SELECT c_int, c_txt FROM t__base WHERE MOD(c_int, 2) = 1

or::

    CREATE TABLE t_table_1 AS SELECT * FROM t__base
    UPDATE t_table_1 SET c_int = c_int, c_txt = c_txt WHERE MOD(c_int, 2) = 1

Same rows at the end. The difference is that this table has been written to — tombstones, reused
space, statistics updated after creation, whatever the engine does on modification. Every other
rewrite here produces a table that was only ever created once, and a query can take a different
path over one that was not.

The ``DELETE`` and the ``INSERT`` share one predicate object, so they cannot drift apart. Put back
a different set than you removed and the rows change. The no-op ``UPDATE`` uses the same
predicate helper; identity assignments keep values unchanged.
"""

from __future__ import annotations

from typing import Optional, Type

from eqgen.builder.constraint_set import Constraint, ConstraintSet
from eqgen.core.types import NumericType, SqlType
from eqgen.equivalence.ast import DeleteReinsertTable, EqNode, NoopUpdateTable, QueryNode
from eqgen.equivalence.builders.base import EquivalenceBuilder
from eqgen.equivalence.constraints import AcceptsDmlConstraint, ExposedNameConstraint
from eqgen.equivalence.context import EquivalenceContext
from eqgen.ir import expr
from eqgen.ir.expr import ExpressionNode


def _pick_integer_key(context: EquivalenceContext) -> Optional[tuple[str, SqlType]]:
    eligible = [
        (column.get_column_name(), column.get_data_type())
        for column in context.base_table.get_column_list()
        if isinstance(column.get_data_type(), NumericType) and column.get_data_type().get_scale() in (None, 0)
    ]
    if not eligible:
        return None
    return EquivalenceBuilder._select_key(eligible, context)


def _mutation_predicate(context: EquivalenceContext) -> Optional[ExpressionNode]:
    """Predicate used by every write-then-restore rewrite.

    Prefer a plugin predicate when one is configured; otherwise ``MOD(key, 2) = 1`` on an integer
    column; otherwise ``None`` (mutate every row).
    """
    generated = EquivalenceBuilder._generated_predicate(context)
    if generated is not None:
        return generated
    key = _pick_integer_key(context)
    if key is None:
        return None
    name, data_type = key
    return expr.eq(expr.mod(expr.col(name, data_type), 2), expr.int_lit(1))


class DeleteReinsertTableBuilder(EquivalenceBuilder[DeleteReinsertTable]):
    """A copy table mutated by an ordered delete and re-insert from the base.

    When a predicate source is configured its text filters both steps; otherwise an integer key
    yields ``MOD(key, 2) = 1``; otherwise the predicate is absent, which degenerates to
    delete-all-then-reinsert-all — still row-preserving, just less interesting.

    The parity sign trap that bites the *partitioning* builder does not bite here, for a different
    reason worth noting: this deletes the rows matching a predicate and re-inserts the rows
    matching the *same* predicate, so whichever rows those are, the net effect is zero. Exhaustive
    coverage is not required — only agreement between the two steps.
    """

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return [AcceptsDmlConstraint, ExposedNameConstraint]

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[DeleteReinsertTable]:
        query = self.builder_factory.build_subtree(
            QueryNode,  # type: ignore[type-abstract]
            ConstraintSet([]),
            context,
        )
        if query is None:
            return None
        return DeleteReinsertTable.build(
            context.namer,
            query,
            context.base_table,
            _mutation_predicate(context),
            exposed_name=self._exposed_name(constraint_set),
        )


class NoopUpdateTableBuilder(EquivalenceBuilder[NoopUpdateTable]):
    """A copy table written with identity ``SET col = col`` assignments.

    Same "wrote the table" intent as delete/reinsert, without an inverse function to get wrong.
    The optional ``WHERE`` comes from :func:`_mutation_predicate`.
    """

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return [AcceptsDmlConstraint, ExposedNameConstraint]

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[NoopUpdateTable]:
        query = self.builder_factory.build_subtree(
            QueryNode,  # type: ignore[type-abstract]
            ConstraintSet([]),
            context,
        )
        if query is None:
            return None
        signature = query.get_signature()
        if not signature:
            return None
        assignments = tuple((named.alias, expr.col(named.alias, named.target)) for named in signature)
        return NoopUpdateTable.build(
            context.namer,
            query,
            assignments,
            _mutation_predicate(context),
            exposed_name=self._exposed_name(constraint_set),
        )
