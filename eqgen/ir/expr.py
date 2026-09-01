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

"""The expression nodes, and the constructors builders call.

Ten kinds, covering everything that can appear in a generated ``WHERE``, projection item or
``CASE`` condition::

    col("c_int", IntegerType())        ->  c_int
    int_lit(2)                         ->  2
    typed_null(VarcharType())          ->  CAST(NULL AS VARCHAR)
    generated_predicate("c_int > 3")   ->  (c_int > 3)
    eq(a, b) / ne / ge                 ->  a = b
    and_(a, b) / or_(a, b)             ->  a AND b
    not_(a)                            ->  NOT a
    is_null(a) / is_not_null(a)        ->  a IS NULL
    mod(a, 2)                          ->  MOD(a, 2)
    case_when(c, t, e, type)           ->  CASE WHEN c THEN t ELSE e END

There is no `FunctionCall(name, args)` catch-all. One class per shape means you can list
everything that reaches generated SQL by reading this file, and an engine that spells one
differently overrides one method. Aggregate calls (:class:`AggregateCall`) are the one
exception that share a class — the function name is an enum, not a free string.

Text from a plugin arrives as a :class:`GeneratedPredicate` leaf, so the core can wrap it
without understanding it. Turning nodes into SQL happens in :mod:`eqgen.ir.render`; nothing
here knows any engine.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from enum import StrEnum
from typing import Optional, Sequence

from eqgen.core.types import BooleanType, IntegerType, SqlType


class ExpressionNode(abc.ABC):
    """A value-producing expression. Frozen; rendered by a dialect's spelling object."""

    @property
    @abc.abstractmethod
    def data_type(self) -> SqlType:
        """The type this expression evaluates to."""


# ---------------------------------------------------------------------------
# Leaves
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnRef(ExpressionNode):
    """A column reference::

        col("c_int", ...)             ->  c_int          -- use this by default
        qualified_col("l", "c_int")   ->  l.c_int        -- only when two inputs share a name

    Bare names are the default because a predicate gets substituted into query bodies whose
    source may be a table, a view or a derived table. ``l.c_int`` resolves in none of those.
    """

    name: str
    column_type: SqlType
    relation_alias: Optional[str] = None

    @property
    def data_type(self) -> SqlType:
        return self.column_type


@dataclass(frozen=True)
class IntLiteral(ExpressionNode):
    """An integer constant."""

    value: int

    @property
    def data_type(self) -> SqlType:
        return IntegerType()


@dataclass(frozen=True)
class BooleanLiteral(ExpressionNode):
    """A boolean constant: ``TRUE`` or ``FALSE``."""

    value: bool

    @property
    def data_type(self) -> SqlType:
        return BooleanType()


@dataclass(frozen=True)
class RawExpr(ExpressionNode):
    """Opaque SQL of a known type — for dialect-specific identity expressions.

    Spelling renders the text as-is (parenthesised when nested). Prefer real nodes when the
    shape is portable; use this when the identity is a DuckDB-only function chain.
    """

    sql: str
    expr_type: SqlType

    def __post_init__(self) -> None:
        assert self.sql.strip(), "a raw expression must not be empty"

    @property
    def data_type(self) -> SqlType:
        return self.expr_type


@dataclass(frozen=True)
class TypedNull(ExpressionNode):
    """``CAST(NULL AS <type>)``.

    The type is required, not decoration::

        CASE WHEN <always true> THEN c_txt ELSE NULL END                 -- engine rejects it
        CASE WHEN <always true> THEN c_txt ELSE CAST(NULL AS VARCHAR) END -- accepted

    The ``ELSE`` branch is unreachable but still has to agree with the column's type.
    """

    null_type: SqlType

    @property
    def data_type(self) -> SqlType:
        return self.null_type


@dataclass(frozen=True)
class GeneratedPredicate(ExpressionNode):
    """Ready-made SQL text from a plugin. Never inspected here, only wrapped.

    Always rendered inside parentheses, because the text can contain ``AND``/``OR`` and gets
    negated. Given ``a = 1 OR b = 2``::

        NOT a = 1 OR b = 2       -- without: NOT applies to a = 1 only. Wrong rows.
        NOT (a = 1 OR b = 2)     -- with: correct

    This has been shipped wrong once.
    """

    value: str

    def __post_init__(self) -> None:
        assert self.value.strip(), "a generated predicate must not be empty"

    @property
    def data_type(self) -> SqlType:
        return BooleanType()


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Comparison(ExpressionNode):
    """``left <op> right`` for one of ``=``, ``<>``, ``<``, ``<=``, ``>``, ``>=``."""

    operator: str
    left: ExpressionNode
    right: ExpressionNode

    @property
    def data_type(self) -> SqlType:
        return BooleanType()


@dataclass(frozen=True)
class BoolOp(ExpressionNode):
    """``left AND right`` or ``left OR right``."""

    operator: str
    left: ExpressionNode
    right: ExpressionNode

    @property
    def data_type(self) -> SqlType:
        return BooleanType()


@dataclass(frozen=True)
class Not(ExpressionNode):
    """``NOT operand``.

    ``NOT NULL`` is NULL, not TRUE::

        c_int > 3          -- NULL when c_int IS NULL
        NOT (c_int > 3)    -- also NULL, so this row is in neither

    So splitting rows on a predicate needs a third branch, ``IS NULL``, to catch them.
    """

    operand: ExpressionNode

    @property
    def data_type(self) -> SqlType:
        return BooleanType()


@dataclass(frozen=True)
class IsNull(ExpressionNode):
    """``operand IS NULL`` / ``operand IS NOT NULL``. Always TRUE or FALSE, never NULL —
    which is what catches the rows :class:`Not` misses."""

    operand: ExpressionNode
    negated: bool = False

    @property
    def data_type(self) -> SqlType:
        return BooleanType()


@dataclass(frozen=True)
class Mod(ExpressionNode):
    """``MOD(operand, modulus)``. Result type follows the operand.

    Watch the sign — in several engines the result takes the dividend's::

        MOD(-1, 2)  =  -1        -- not 1

    So a test for odd numbers written as ``MOD(c, 2) = 1`` silently skips negatives; use
    ``MOD(c, 2) <> 0``.
    """

    operand: ExpressionNode
    modulus: int

    @property
    def data_type(self) -> SqlType:
        return self.operand.data_type


@dataclass(frozen=True)
class Case(ExpressionNode):
    """``CASE WHEN condition THEN then_expr ELSE else_expr END`` of a stated type."""

    condition: ExpressionNode
    then_expr: ExpressionNode
    else_expr: ExpressionNode
    result_type: SqlType

    @property
    def data_type(self) -> SqlType:
        return self.result_type


class AggregateFunction(StrEnum):
    """Aggregates a rewrite may put in a ``SELECT`` list (with ``GROUP BY``). Explicit, so what
    can appear in generated SQL is still answerable by reading this file."""

    MIN = "MIN"
    MAX = "MAX"
    ANY_VALUE = "ANY_VALUE"


@dataclass(frozen=True)
class AggregateCall(ExpressionNode):
    """``<fn>(<arg>)`` — a plain aggregate, not a window. Used by the key-channel GROUP reducer
    where every copy in a group holds the same value, so ``ANY_VALUE`` / ``MAX`` / ``MIN`` all
    recover that value."""

    function: AggregateFunction
    arg: ExpressionNode
    result_type: SqlType

    @property
    def data_type(self) -> SqlType:
        return self.result_type


class ValueCodec(StrEnum):
    """A lossless per-value round trip: ``decode(encode(c))`` gives ``c`` back.

    An enum rather than one node class per codec, for the reason ``join_type_weights`` is a weight
    and not four join classes (ARCHITECTURE §4): the *shape* is identical — encode a value, decode
    it, land on the same value — so the variant belongs in the config, and one builder covers all of
    them. Adding a codec is an entry here plus a line per engine that spells it.

    What can appear in generated SQL is still answerable by reading this file, which is why this is a
    closed enum and not a function-name string.
    """

    #: ``hex`` / ``unhex`` — the only one confirmed present on every engine tested.
    HEX = "HEX"
    #: ``to_base64`` / ``from_base64``. Absent on SQLite.
    BASE64 = "BASE64"
    #: One-key JSON object, then read the key back out.
    JSON_PACK = "JSON_PACK"


@dataclass(frozen=True)
class ValueCodecRoundTrip(ExpressionNode):
    """``decode(encode(<arg>))`` for one :class:`ValueCodec`, typed as *arg*.

    The engine spells it (:meth:`~eqgen.ir.render.Spelling.value_codec_sql`); the node only records
    which round trip was asked for. That is the difference between this and the ``RawExpr`` chains
    the dialects each wrote separately: the same node renders on every engine that has the codec, so
    the builder is portable and only the spelling is not.
    """

    codec: ValueCodec
    arg: ExpressionNode
    result_type: SqlType

    @property
    def data_type(self) -> SqlType:
        return self.result_type


class WindowFunction(StrEnum):
    """The window functions a rewrite may use. An explicit list, not a name string, so what can
    appear in generated SQL is still answerable by reading this file."""

    MIN = "MIN"
    MAX = "MAX"
    FIRST_VALUE = "FIRST_VALUE"
    LAST_VALUE = "LAST_VALUE"
    ROW_NUMBER = "ROW_NUMBER"


class WindowFrameKind(StrEnum):
    """``ROWS`` counts rows, ``RANGE`` counts peers of the ``ORDER BY`` value. Over a partition
    where every row shares that value the two differ, which is worth exercising."""

    ROWS = "ROWS"
    RANGE = "RANGE"


@dataclass(frozen=True)
class WindowFrame(ExpressionNode):
    """``<kind> BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING`` — the whole partition.

    Only the unbounded frame, because that is the one that leaves the answer independent of where
    in the partition the row sits. A narrower frame would make ``FIRST_VALUE`` depend on position,
    and the rewrite would stop returning the column it started with.
    """

    kind: WindowFrameKind

    @property
    def data_type(self) -> SqlType:
        raise TypeError("a window frame is a clause, not a value")


@dataclass(frozen=True)
class WindowCall(ExpressionNode):
    """``<fn>(<arg>) OVER (PARTITION BY … [ORDER BY …] [frame])``.

    With ``partition_by`` the same column as ``arg``, every row in a partition holds the same
    value, so all of MIN/MAX/FIRST_VALUE/LAST_VALUE return exactly that value::

        MAX(c_int) OVER (PARTITION BY c_int)                    ==  c_int
        FIRST_VALUE(c_int) OVER (PARTITION BY c_int ORDER BY c_int
                                 ROWS BETWEEN UNBOUNDED PRECEDING
                                          AND UNBOUNDED FOLLOWING)  ==  c_int

    ``arg`` is ``None`` for ``ROW_NUMBER``, which takes no argument.
    """

    function: WindowFunction
    arg: Optional[ExpressionNode]
    partition_by: tuple[ExpressionNode, ...]
    order_by: tuple[ExpressionNode, ...]
    frame: Optional[WindowFrame]
    result_type: SqlType

    @property
    def data_type(self) -> SqlType:
        return self.result_type


# ---------------------------------------------------------------------------
# Constructors — what builders actually call
# ---------------------------------------------------------------------------


def col(name: str, data_type: SqlType) -> ColumnRef:
    """A bare (unqualified) column reference."""
    return ColumnRef(name, data_type)


def qualified_col(relation_alias: str, name: str, data_type: SqlType) -> ColumnRef:
    """``<alias>.<name>`` — for a query whose inputs share column names."""
    return ColumnRef(name, data_type, relation_alias=relation_alias)


def int_lit(value: int) -> IntLiteral:
    return IntLiteral(value)


def bool_lit(value: bool) -> BooleanLiteral:
    return BooleanLiteral(value)


def raw_expr(sql: str, data_type: SqlType) -> RawExpr:
    """Dialect-specific SQL fragment typed as *data_type*."""
    return RawExpr(sql, data_type)


def typed_null(data_type: SqlType) -> TypedNull:
    return TypedNull(data_type)


def generated_predicate(value: str) -> GeneratedPredicate:
    """Wrap plugin-supplied predicate text as an opaque boolean leaf."""
    return GeneratedPredicate(value)


def eq(left: ExpressionNode, right: ExpressionNode) -> Comparison:
    return Comparison("=", left, right)


def ne(left: ExpressionNode, right: ExpressionNode) -> Comparison:
    return Comparison("<>", left, right)


def ge(left: ExpressionNode, right: ExpressionNode) -> Comparison:
    return Comparison(">=", left, right)


def mod(operand: ExpressionNode, modulus: int) -> Mod:
    return Mod(operand, modulus)


def and_(left: ExpressionNode, right: ExpressionNode) -> BoolOp:
    return BoolOp("AND", left, right)


def or_(left: ExpressionNode, right: ExpressionNode) -> BoolOp:
    return BoolOp("OR", left, right)


def not_(operand: ExpressionNode) -> Not:
    return Not(operand)


def is_null(operand: ExpressionNode) -> IsNull:
    return IsNull(operand)


def is_not_null(operand: ExpressionNode) -> IsNull:
    return IsNull(operand, negated=True)


def case_when(
    condition: ExpressionNode,
    then_expr: ExpressionNode,
    else_expr: ExpressionNode,
    data_type: SqlType,
) -> Case:
    return Case(condition, then_expr, else_expr, data_type)


def agg(function: AggregateFunction, arg: ExpressionNode, data_type: SqlType) -> AggregateCall:
    """``<function>(arg)`` as a plain aggregate (no ``OVER``)."""
    return AggregateCall(function, arg, data_type)


def any_value(arg: ExpressionNode, data_type: SqlType) -> AggregateCall:
    """``ANY_VALUE(arg)`` — any row in the group; safe when every row holds the same value."""
    return AggregateCall(AggregateFunction.ANY_VALUE, arg, data_type)


def value_codec_roundtrip(codec: ValueCodec, arg: ExpressionNode, data_type: SqlType) -> ValueCodecRoundTrip:
    """``decode(encode(arg))`` through *codec*, typed as *data_type*."""
    return ValueCodecRoundTrip(codec, arg, data_type)


# ---------------------------------------------------------------------------
# Built from the above: expressions with a known constant value
# ---------------------------------------------------------------------------
#
# These stay here and are never overridden per engine. An engine chooses how to spell ``OR``;
# it does not get to change which arms are present, because that is what makes the value
# constant.


def conjoin(existing: Optional[ExpressionNode], extra: ExpressionNode) -> ExpressionNode:
    """``AND`` *extra* onto *existing*, which may be absent."""
    return extra if existing is None else and_(existing, extra)


def determined_true(predicate: ExpressionNode) -> ExpressionNode:
    """``p OR NOT p OR p IS NULL`` — TRUE for every row, whatever ``p`` is.

    With ``p`` = ``c_int > 3`` and a row where ``c_int`` is NULL::

        c_int > 3                                    ->  NULL
        NOT (c_int > 3)                              ->  NULL
        (c_int > 3) OR NOT (c_int > 3)               ->  NULL      -- not TRUE
        (c_int > 3) OR NOT (c_int > 3) OR (c_int > 3) IS NULL -> TRUE

    So the third arm is required, not padding. Needs a ``p`` that gives the same answer each
    time it is evaluated: with ``random() < 0.5`` all three arms can come out false at once.
    From Jiang & Su, OSDI'24.
    """
    return or_(or_(predicate, not_(predicate)), is_null(predicate))


def determined_false(predicate: ExpressionNode) -> ExpressionNode:
    """``p AND NOT p AND p IS NOT NULL`` — FALSE for every row, whatever ``p`` is.

    Mirror of :func:`determined_true`. ``p AND NOT p`` comes out NULL when ``p`` is NULL, and
    the ``IS NOT NULL`` conjunct drops that row. ``OR``-ing this onto a filter leaves the
    filter's rows unchanged.
    """
    return and_(and_(predicate, not_(predicate)), is_not_null(predicate))


def frame(kind: WindowFrameKind) -> WindowFrame:
    """The whole-partition frame of *kind*."""
    return WindowFrame(kind)


def window_over(
    function: WindowFunction,
    arg: Optional[ExpressionNode],
    partition_by: Sequence[ExpressionNode],
    data_type: SqlType,
    *,
    order_by: Sequence[ExpressionNode] = (),
    frame_spec: Optional[WindowFrame] = None,
) -> WindowCall:
    """``<function>(arg) OVER (PARTITION BY … …)``. See :class:`WindowCall` for why this is an
    identity when ``partition_by`` is the same column as ``arg``."""
    return WindowCall(function, arg, tuple(partition_by), tuple(order_by), frame_spec, data_type)


def row_number(order_by: Sequence[ExpressionNode]) -> WindowCall:
    """``ROW_NUMBER() OVER (ORDER BY …)`` — a different whole number for every row, starting at 1.

    The order does not have to be a total one. The number is used to tell rows apart and is
    written down once, so ties change which row gets which number and nothing else.
    """
    return WindowCall(WindowFunction.ROW_NUMBER, None, (), tuple(order_by), None, IntegerType())
