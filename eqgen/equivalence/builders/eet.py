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

"""Wrap every column in a ``CASE`` whose condition is always TRUE.

Take any predicate ``p``. Then ``p OR NOT p OR p IS NULL`` is TRUE for every row, whatever ``p``
is, so::

    SELECT CASE WHEN (p OR NOT p OR p IS NULL) THEN c_txt ELSE NULL END AS c_txt,
           CASE WHEN (p OR NOT p OR p IS NULL) THEN c_int ELSE NULL END AS c_int
    FROM t

returns the same values as ``SELECT c_txt, c_int FROM t``, while making the engine evaluate a
freely generated expression per column on the way.

Useful because it works on **every** column type. Other column rewrites have to ask whether the
type can be encoded as bytes, packed into an array, compared, aggregated — and decline for the
rest. A redundant ``CASE`` is valid for anything, so whatever the table holds, this fires.

The ``ELSE`` branch never runs but still needs the column's type, or the engine rejects the
``CASE`` for mismatched arms.

From Jiang & Su, OSDI'24, where it is called EET.
"""

from __future__ import annotations

from typing import Callable, Optional, Type

from eqgen.builder.constraint_set import Constraint, ConstraintSet
from eqgen.core.types import SqlType, TypeProperty
from eqgen.equivalence.ast import EqNode, SelectQuery
from eqgen.equivalence.builders.base import ColumnRewriteQueryBuilder, EquivalenceBuilder
from eqgen.equivalence.constraints import RowFilterConstraint, SingleSourceConstraint
from eqgen.equivalence.context import EquivalenceContext
from eqgen.ir import expr
from eqgen.ir.expr import ExpressionNode


def _free_predicate(context: EquivalenceContext) -> Optional[ExpressionNode]:
    """A predicate to feed the ``CASE`` condition — from the plugin, or a built-in fallback.

    The fallback keeps this builder working with no plugin configured::

        c_int >= c_int      -- TRUE for a value, NULL for a NULL, so the third arm still matters
        c_txt IS NULL       -- when the column cannot be ordered

    Only an empty table yields nothing.
    """
    generated = EquivalenceBuilder._generated_predicate(context)
    if generated is not None:
        return generated
    columns = context.base_table.get_column_list()
    if not columns:
        return None
    context.predicate_origin["builtin-fallback"] += 1
    column = EquivalenceBuilder._select_key(columns, context)
    reference = expr.col(column.get_column_name(), column.get_data_type())
    if column.get_data_type().get_properties() & TypeProperty.ORDERABLE:
        return expr.ge(reference, reference)
    return expr.is_null(reference)


class EetCaseColumnQueryBuilder(ColumnRewriteQueryBuilder):
    """Wrap each column in the always-true ``CASE`` above. Declines only on an empty table.

    **A fresh predicate per column**, not one shared across the projection. Each column's ``CASE`` is
    independently always-true, so correctness does not require them to agree — and sharing turned out to
    cost more than it saved:

    * it is not shorter. Nine copies of one predicate and nine different predicates are the same length.
    * it is not more readable. The emitted statement is ~2,900 characters either way; nine repetitions of
      one 176-character expression is arguably the harder of the two to read.
    * it is measurably weaker. An identical subexpression repeated nine times is what PostgreSQL's
      common-subexpression handling exists to collapse, so the engine plausibly evaluates it *once*.
      Nine distinct predicates force nine evaluation paths, which is the whole point of the rewrite.

    Contrast the three-way repetition *inside* one condition (``p``, ``NOT p``, ``p IS NULL``): that one
    is mandatory, because the three arms must share a predicate or they stop covering every row.
    """

    def _column_rewriter(self, context: EquivalenceContext) -> Callable[[str, SqlType], Optional[ExpressionNode]]:
        if _free_predicate(context) is None:
            return lambda name, data_type: None  # no columns -> nothing to rewrite -> decline

        def rewrite(name: str, data_type: SqlType) -> Optional[ExpressionNode]:
            predicate = _free_predicate(context)
            if predicate is None:
                return None
            condition = expr.determined_true(predicate)
            return expr.case_when(condition, expr.col(name, data_type), expr.typed_null(data_type), data_type)

        return rewrite


class EetDeterminedFilterQueryBuilder(EquivalenceBuilder[SelectQuery]):
    """Put the always-true condition in the ``WHERE`` instead of around each column::

        SELECT * FROM t WHERE ((c_big <= 1000) OR (NOT (c_big <= 1000)))
                              OR ((c_big <= 1000) IS NULL)

    True for every row, so the rows are the ones the filter above asked for and no others. The
    engine still has to evaluate a freely generated predicate to find that out.

    ``conjoin`` rather than assignment: listing ``RowFilterConstraint`` promises to honour the
    filter this build was asked for, and replacing it would widen the rows of every branch
    dispatched into here.

    Declines when no predicate source is configured and the base table has no columns to build a
    fallback from.
    """

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return [RowFilterConstraint, SingleSourceConstraint]

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[SelectQuery]:
        predicate = _free_predicate(context)
        if predicate is None:
            return None
        source = self._dispatch_source(context, constraint_set)
        if source is None:
            return None
        where = expr.conjoin(self._current_filter(constraint_set), expr.determined_true(predicate))
        return SelectQuery(source, None, where)
