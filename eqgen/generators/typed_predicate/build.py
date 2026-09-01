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

"""Build a typed predicate AST over a table's columns.

Fork of the toy's ``_atom`` / ``_predicate`` control flow, but returning :class:`ExpressionNode`
trees instead of SQL strings. No casts: comparisons stay inside a type family. Dialect
scalar functions come from :mod:`pg_func` / :mod:`duckdb_func` via :func:`func_spec.catalog_for`.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Optional, Sequence

from eqgen.core.catalog import Column, Table
from eqgen.core.types import (
    BooleanType,
    DateType,
    DoubleType,
    Int4RangeType,
    IntegerType,
    JsonbType,
    NumericType,
    SqlType,
    TimestampType,
    TypeProperty,
    UuidType,
    VarcharType,
)
from eqgen.generators.typed_predicate.func_spec import FuncSpec, catalog_for
from eqgen.ir import expr
from eqgen.ir.expr import Comparison, ExpressionNode

_COMPARISONS = ("=", "<>", "<", "<=", ">", ">=")
_STRINGS = ("", "a", "abc", "Zed", "o'brien", "trailing ")
_LIKE_PATTERNS = ("%", "a%", "%c", "%a%", "___", "o'brien")
_MAX_PREDICATE_DEPTH = 2


@dataclass(frozen=True)
class PortableLiteral(ExpressionNode):
    """A literal whose SQL spelling is already fixed and portable across our engines."""

    sql: str
    result_type: SqlType

    def __post_init__(self) -> None:
        assert self.sql.strip(), "a portable literal must not be empty"

    @property
    def data_type(self) -> SqlType:
        return self.result_type


@dataclass(frozen=True)
class FuncCall(ExpressionNode):
    """A scalar function application. *sql_name* is taken from the dialect catalog at build time."""

    sql_name: str
    args: tuple[ExpressionNode, ...]
    result_type: SqlType

    def __post_init__(self) -> None:
        assert self.args, "a function call needs at least one argument"

    @property
    def data_type(self) -> SqlType:
        return self.result_type


@dataclass(frozen=True)
class IsDistinctFrom(ExpressionNode):
    left: ExpressionNode
    right: ExpressionNode
    negated: bool = False

    @property
    def data_type(self) -> SqlType:
        return BooleanType()


@dataclass(frozen=True)
class Between(ExpressionNode):
    value: ExpressionNode
    low: ExpressionNode
    high: ExpressionNode
    negated: bool = False

    @property
    def data_type(self) -> SqlType:
        return BooleanType()


@dataclass(frozen=True)
class InList(ExpressionNode):
    value: ExpressionNode
    items: tuple[ExpressionNode, ...]
    negated: bool = False

    def __post_init__(self) -> None:
        assert self.items, "IN needs at least one item"

    @property
    def data_type(self) -> SqlType:
        return BooleanType()


@dataclass(frozen=True)
class Like(ExpressionNode):
    value: ExpressionNode
    pattern: ExpressionNode
    negated: bool = False

    @property
    def data_type(self) -> SqlType:
        return BooleanType()


def _quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _same_family(left: SqlType, right: SqlType, *, dialect: str = "") -> bool:
    # ClickHouse has no Int↔Float / Decimal↔Float supertype (Code 386). Keep peers exact.
    if dialect == "clickhouse":
        if isinstance(left, IntegerType) and isinstance(right, IntegerType):
            return True
        if (
            isinstance(left, NumericType)
            and isinstance(right, NumericType)
            and not isinstance(left, IntegerType)
            and not isinstance(right, IntegerType)
        ):
            return (left.get_scale() or 0) == (right.get_scale() or 0)
        for family in (DoubleType, VarcharType, BooleanType, DateType, TimestampType):
            if isinstance(left, family) and isinstance(right, family):
                return True
        return False
    for family in (
        NumericType,
        DoubleType,
        VarcharType,
        BooleanType,
        DateType,
        TimestampType,
        JsonbType,
        UuidType,
        Int4RangeType,
    ):
        if isinstance(left, family) and isinstance(right, family):
            return True
    return False


def _literal(dtype: SqlType, rng: random.Random) -> ExpressionNode:
    if isinstance(dtype, BooleanType):
        return PortableLiteral(rng.choice(("TRUE", "FALSE")), dtype)
    if isinstance(dtype, DoubleType):
        return PortableLiteral(rng.choice(("0.0", "1.5", "-0.25", "1000.125")), dtype)
    if isinstance(dtype, NumericType):
        scale = dtype.get_scale()
        if scale:
            return PortableLiteral(rng.choice(("0.00", "12.34", "-5.50", "999.99")), dtype)
        return expr.int_lit(rng.choice((-7, -1, 0, 1, 2, 3, 42, 1000)))
    if isinstance(dtype, DateType):
        return PortableLiteral(_quote(rng.choice(("2024-01-15", "1999-12-31", "2030-06-01"))), dtype)
    if isinstance(dtype, TimestampType):
        return PortableLiteral(
            _quote(rng.choice(("2024-01-15 12:34:56", "1999-12-31 23:59:59"))),
            dtype,
        )
    if isinstance(dtype, JsonbType):
        return PortableLiteral(
            rng.choice(("'{}'::jsonb", "'[]'::jsonb", '\'{"a": 1}\'::jsonb')),
            dtype,
        )
    if isinstance(dtype, UuidType):
        return PortableLiteral(
            rng.choice(
                (
                    "'00000000-0000-0000-0000-000000000000'::uuid",
                    "'550e8400-e29b-41d4-a716-446655440000'::uuid",
                )
            ),
            dtype,
        )
    if isinstance(dtype, Int4RangeType):
        return PortableLiteral(
            rng.choice(("'empty'::int4range", "'[1,10)'::int4range", "'[0,100]'::int4range")),
            dtype,
        )
    return PortableLiteral(_quote(rng.choice(_STRINGS)), dtype)


def _compare(left: ExpressionNode, right: ExpressionNode, rng: random.Random) -> Comparison:
    return Comparison(rng.choice(_COMPARISONS), left, right)


def _peer_or_literal(
    columns: Sequence[Column], name: str, dtype: SqlType, rng: random.Random, *, dialect: str = ""
) -> ExpressionNode:
    peers = [
        c
        for c in columns
        if c.get_column_name() != name and _same_family(dtype, c.get_data_type(), dialect=dialect)
    ]
    if peers and rng.random() < 0.4:
        other = rng.choice(peers)
        return expr.col(other.get_column_name(), other.get_data_type())
    return _literal(dtype, rng)


def _pick_func_args(
    spec: FuncSpec, columns: Sequence[Column], rng: random.Random
) -> Optional[tuple[ExpressionNode, ...]]:
    """Bind each argument to a matching column, or a same-family literal for trailing args."""
    args: list[ExpressionNode] = []
    for i, family in enumerate(spec.arg_families):
        candidates = [c for c in columns if isinstance(c.get_data_type(), family)]
        if not candidates:
            return None
        # Prefer a column for the first arg; later args may be literals so binary ops vary.
        if i == 0 or rng.random() < 0.6:
            col = rng.choice(candidates)
            args.append(expr.col(col.get_column_name(), col.get_data_type()))
        else:
            args.append(_literal(rng.choice(candidates).get_data_type(), rng))
    return tuple(args)


def _func_atom(columns: Sequence[Column], rng: random.Random, *, dialect: str) -> Optional[ExpressionNode]:
    """A catalog function as a boolean atom, or ``None`` if nothing matches the table."""
    catalog = catalog_for(dialect)
    applicable = [s for s in catalog if all(
        any(isinstance(c.get_data_type(), fam) for c in columns) for fam in s.arg_families
    )]
    if not applicable:
        return None
    spec = rng.choice(applicable)
    args = _pick_func_args(spec, columns, rng)
    if args is None:
        return None
    arg_types = tuple(a.data_type for a in args)
    result = spec.result_type_for(arg_types)
    call = FuncCall(spec.sql_name, args, result)
    if isinstance(result, BooleanType):
        return call
    return _compare(call, _literal(result, rng), rng)


def _atom(columns: Sequence[Column], rng: random.Random, *, dialect: str) -> ExpressionNode:
    """One comparison / null / BETWEEN / IN / LIKE / function atom. No casts."""
    column = rng.choice(list(columns))
    name, dtype = column.get_column_name(), column.get_data_type()
    ref = expr.col(name, dtype)
    # jsonb / int4range: no ORDERABLE — only null checks and equality against typed literals.
    if not (dtype.get_properties() & TypeProperty.ORDERABLE):
        choice = rng.random()
        if choice < 0.4:
            return expr.is_null(ref)
        if choice < 0.7:
            return expr.is_not_null(ref)
        return Comparison("=", ref, _literal(dtype, rng))

    choice = rng.random()

    if choice < 0.10:
        return expr.is_null(ref)
    if choice < 0.16:
        return expr.is_not_null(ref)

    if choice < 0.26:
        # ClickHouse isDistinctFrom is strict on Decimal↔Float and Date↔String
        # (Code: 43) when the portable printer emits a floaty/string literal.
        if dialect != "clickhouse":
            return IsDistinctFrom(
                ref, _peer_or_literal(columns, name, dtype, rng, dialect=dialect), negated=rng.random() < 0.5
            )

    if choice < 0.36:
        return Between(ref, _literal(dtype, rng), _literal(dtype, rng), negated=rng.random() < 0.25)

    if choice < 0.46:
        # ClickHouse types bare `12.34` as Float64, so `c_dec IN (12.34)` is Code 386.
        decimalish = (
            dialect == "clickhouse"
            and isinstance(dtype, NumericType)
            and not isinstance(dtype, IntegerType)
            and bool(dtype.get_scale())
        )
        if not decimalish:
            n = rng.randint(1, 3)
            return InList(ref, tuple(_literal(dtype, rng) for _ in range(n)), negated=rng.random() < 0.25)

    if choice < 0.54 and isinstance(dtype, VarcharType):
        return Like(ref, PortableLiteral(_quote(rng.choice(_LIKE_PATTERNS)), dtype), negated=rng.random() < 0.25)

    # Dialect catalog functions — gated by signature lists in pg_func / duckdb_func.
    if choice < 0.72:
        built = _func_atom(columns, rng, dialect=dialect)
        if built is not None:
            return built

    if choice < 0.82:
        peers = [
            c
            for c in columns
            if c.get_column_name() != name and _same_family(dtype, c.get_data_type(), dialect=dialect)
        ]
        if peers:
            other = rng.choice(peers)
            return _compare(ref, expr.col(other.get_column_name(), other.get_data_type()), rng)

    if (
        choice < 0.88
        and isinstance(dtype, NumericType)
        and not isinstance(dtype, DoubleType)
        and not dtype.get_scale()
    ):
        return _compare(expr.mod(ref, 2), expr.int_lit(rng.choice((0, 1))), rng)

    return _compare(ref, _literal(dtype, rng), rng)


def _predicate(
    columns: Sequence[Column], rng: random.Random, *, dialect: str, depth: int = 0
) -> ExpressionNode:
    if depth >= _MAX_PREDICATE_DEPTH or rng.random() < 0.45:
        return _atom(columns, rng, dialect=dialect)
    if rng.random() < 0.2:
        return expr.not_(_predicate(columns, rng, dialect=dialect, depth=depth + 1))
    left = _predicate(columns, rng, dialect=dialect, depth=depth + 1)
    right = _predicate(columns, rng, dialect=dialect, depth=depth + 1)
    return expr.and_(left, right) if rng.random() < 0.5 else expr.or_(left, right)


def build_predicate(table: Table, *, seed: int, dialect: str = "postgres") -> Optional[ExpressionNode]:
    """A deterministic typed predicate over *table*, or ``None`` if it has no columns.

    *dialect* selects the function catalog (:mod:`pg_func` / :mod:`duckdb_func`).
    """
    columns = table.get_column_list()
    if not columns:
        return None
    return _predicate(columns, random.Random(seed), dialect=dialect)
