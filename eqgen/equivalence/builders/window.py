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

"""Rewrite each column to a window function over a partition of that same column.

Partition by the column itself and every row in a partition holds the same value, so the function
returns exactly the value it started with::

    SELECT MAX(c_int) OVER (PARTITION BY c_int) AS c_int,
           MAX(c_txt) OVER (PARTITION BY c_txt) AS c_txt
    FROM t

Same rows, and the engine has to build a window plan — sort or hash the partition, evaluate a frame
— to arrive at them. The function and the frame are drawn from config: over a partition where every
row shares the value they all return that value, so which one is coverage rather than correctness.

Two columns are skipped, both for reasons found by running it:

``MIN``/``MAX`` over a boolean, because PostgreSQL has no ``min(boolean)`` aggregate — DuckDB does.
The query would fail on one engine and be reported as a one-sided error.

Floating point, whatever the function. ``PARTITION BY`` groups values that compare equal, and
``-0.0 = 0.0`` is true, so a partition can hold both and the function may hand back the other one.
The rows would still compare equal on most drivers and then not on some, which is the worst kind of
intermittent.
"""

from __future__ import annotations

from typing import Callable, Optional

from eqgen.config.settings import WindowFrameChoice, WindowFunctionChoice
from eqgen.core.types import BooleanType, DoubleType, SqlType, TypeProperty
from eqgen.equivalence.builders.base import ColumnRewriteQueryBuilder
from eqgen.equivalence.context import EquivalenceContext
from eqgen.ir import expr
from eqgen.ir.expr import ExpressionNode, WindowFrame, WindowFrameKind, WindowFunction

#: The config choice, and the function it writes. Separate enums on purpose: the config lists what a
#: run may choose, the IR lists what can be rendered, and ``ROW_NUMBER`` is in the second only —
#: nothing should be able to configure it here, because it does not return the column.
_FUNCTIONS: dict[WindowFunctionChoice, WindowFunction] = {
    WindowFunctionChoice.MIN: WindowFunction.MIN,
    WindowFunctionChoice.MAX: WindowFunction.MAX,
    WindowFunctionChoice.FIRST_VALUE: WindowFunction.FIRST_VALUE,
    WindowFunctionChoice.LAST_VALUE: WindowFunction.LAST_VALUE,
}

#: ``FIRST_VALUE``/``LAST_VALUE`` are defined by position, so they need an ``ORDER BY``. ``MIN``/
#: ``MAX`` are aggregates and do not.
_NEEDS_ORDER = frozenset({WindowFunction.FIRST_VALUE, WindowFunction.LAST_VALUE})


def _frame_for(choice: WindowFrameChoice) -> Optional[WindowFrame]:
    if choice is WindowFrameChoice.ROWS:
        return expr.frame(WindowFrameKind.ROWS)
    if choice is WindowFrameChoice.RANGE:
        return expr.frame(WindowFrameKind.RANGE)
    return None


class WindowRewriteQueryBuilder(ColumnRewriteQueryBuilder):
    """``<fn>(c) OVER (PARTITION BY c …)`` per column. See the module docstring."""

    @staticmethod
    def _eligible(data_type: SqlType, function: WindowFunction, ordered: bool) -> bool:
        properties = data_type.get_properties()
        if not properties & TypeProperty.GROUPABLE:
            return False  # PARTITION BY <column>
        if ordered and not properties & TypeProperty.ORDERABLE:
            return False  # ORDER BY <column>
        # The aggregates are a separate question from grouping: a type can group and have no MAX.
        # PostgreSQL also has no min/max(boolean) specifically; DuckDB does.
        aggregate = function in (WindowFunction.MIN, WindowFunction.MAX)
        if aggregate and not properties & TypeProperty.AGGREGATABLE:
            return False
        if aggregate and isinstance(data_type, BooleanType):
            return False
        # -0.0 and 0.0 share a partition; see the module docstring.
        return not isinstance(data_type, DoubleType)

    def _column_rewriter(self, context: EquivalenceContext) -> Callable[[str, SqlType], Optional[ExpressionNode]]:
        function = _FUNCTIONS[context.config.window_function_weights.choose_one()]
        frame = _frame_for(context.config.window_frame_weights.choose_one())
        ordered = function in _NEEDS_ORDER or frame is not None

        def rewrite(name: str, data_type: SqlType) -> Optional[ExpressionNode]:
            if not self._eligible(data_type, function, ordered):
                return None
            column = expr.col(name, data_type)
            order_by = (column,) if ordered else ()
            return expr.window_over(function, column, (column,), data_type, order_by=order_by, frame_spec=frame)

        return rewrite
