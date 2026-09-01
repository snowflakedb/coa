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

"""Turning an expression or a type name into SQL text for one engine.

A :class:`Spelling` answers two questions::

    spelling.expr(node)                    ->  "MOD(c_int, 2) = 0"
    spelling.type_sql(NumericType(10, 2))  ->  "NUMERIC(10, 2)"    PostgreSQL
                                           ->  "DECIMAL(10, 2)"    DuckDB

Both live on one object because a column definition and a ``CAST`` have to agree::

    CREATE TABLE t (c DECIMAL(10, 2))    -- declared one way
    ... CAST(NULL AS NUMERIC(10, 2))     -- cast another: an error, and a bug this project
                                         -- has already had to pin with a test

:class:`PostgresSpelling` is the starting point for every engine. PostgreSQL rather than
"ANSI" because you can point it at a real server and check the output; nobody can run ANSI.
DuckDB overrides :meth:`type_sql` and nothing else.

An unrecognised node raises instead of rendering as something plausible.
"""

from __future__ import annotations

import abc

from eqgen.core.types import (
    BooleanType,
    CharType,
    DateType,
    DoubleType,
    Int4RangeType,
    IntegerType,
    JsonbType,
    NumericType,
    SqlType,
    TextType,
    TimestampType,
    UuidType,
    VarcharType,
)
from eqgen.ir.expr import (
    AggregateCall,
    BooleanLiteral,
    BoolOp,
    Case,
    ColumnRef,
    Comparison,
    ExpressionNode,
    GeneratedPredicate,
    IntLiteral,
    IsNull,
    Mod,
    Not,
    RawExpr,
    TypedNull,
    ValueCodec,
    ValueCodecRoundTrip,
    WindowCall,
)


class UnsupportedForDialect(RuntimeError):
    """No checked way to write this construct for this engine.

    Raise rather than guess. A dropped object is counted and reported; a guessed spelling
    becomes a mismatch that looks like an engine bug and is not.
    """


#: Node kinds that need no parentheses when they appear as an operand — either they are
#: atomic, or they already bracket themselves.
_ATOMIC = (ColumnRef, IntLiteral, BooleanLiteral, TypedNull, Mod, GeneratedPredicate, RawExpr)


class Spelling(abc.ABC):
    """A dialect's lexical choices: how to write an expression and a type name."""

    @abc.abstractmethod
    def type_sql(self, data_type: SqlType) -> str:
        """This dialect's name for *data_type*, parameters included."""

    # -- expressions ------------------------------------------------------

    def expr(self, node: ExpressionNode) -> str:
        """Render *node*. Dispatches on class; raises on anything unrecognised."""
        if isinstance(node, ColumnRef):
            return self.column_sql(node)
        if isinstance(node, IntLiteral):
            return str(node.value)
        if isinstance(node, BooleanLiteral):
            return "TRUE" if node.value else "FALSE"
        if isinstance(node, RawExpr):
            return node.sql
        if isinstance(node, TypedNull):
            return self.typed_null_sql(node)
        if isinstance(node, GeneratedPredicate):
            # Unconditionally parenthesised: see GeneratedPredicate's docstring.
            return f"({node.value})"
        if isinstance(node, Comparison):
            return f"{self._operand(node.left)} {node.operator} {self._operand(node.right)}"
        if isinstance(node, BoolOp):
            return f"{self._operand(node.left)} {node.operator} {self._operand(node.right)}"
        if isinstance(node, Not):
            return f"NOT {self._operand(node.operand)}"
        if isinstance(node, IsNull):
            return f"{self._operand(node.operand)} IS {'NOT ' if node.negated else ''}NULL"
        if isinstance(node, Mod):
            return self.mod_sql(node)
        if isinstance(node, Case):
            return self.case_sql(node)
        if isinstance(node, AggregateCall):
            return self.aggregate_sql(node)
        if isinstance(node, ValueCodecRoundTrip):
            return self.value_codec_sql(node)
        if isinstance(node, WindowCall):
            return self.window_sql(node)
        raise UnsupportedForDialect(f"no rendering for expression node {type(node).__name__}")

    def _operand(self, node: ExpressionNode) -> str:
        """Render *node*, bracketed unless it is a single value.

        Brackets go on anything compound, even where precedence made them unnecessary::

            a = 1 AND (b = 2 OR c = 3)     -- what we emit
            a = 1 AND b = 2 OR c = 3       -- unbracketed: parses as (a AND b) OR c,
                                           -- which selects different rows

        A precedence table got subtly wrong gives no syntax error, just different rows.
        """
        rendered = self.expr(node)
        return rendered if isinstance(node, _ATOMIC) else f"({rendered})"

    # -- per-node hooks a dialect may override ----------------------------

    def column_sql(self, node: ColumnRef) -> str:
        if node.relation_alias is not None:
            return f"{node.relation_alias}.{node.name}"
        return node.name

    def typed_null_sql(self, node: TypedNull) -> str:
        """``CAST(NULL AS <type>)``.

        ``CAST(NULL AS VARCHAR)``, not PostgreSQL's ``NULL::VARCHAR``. Every engine takes
        the first form, so the shorthand buys nothing.
        """
        return f"CAST(NULL AS {self.type_sql(node.null_type)})"

    def mod_sql(self, node: Mod) -> str:
        """``MOD(x, n)``. An engine that writes ``x % n`` overrides this."""
        return f"MOD({self.expr(node.operand)}, {node.modulus})"

    def aggregate_sql(self, node: AggregateCall) -> str:
        """``ANY_VALUE(c)`` / ``MAX(c)``. An engine that spells one differently overrides this."""
        return f"{node.function.value}({self.expr(node.arg)})"

    def value_codec_sql(self, node: ValueCodecRoundTrip) -> str:
        """``decode(encode(c))`` for one codec, cast back to the column's own type.

        PostgreSQL's spelling, which is the default for the same reason every other default here is
        (nobody can run "ANSI"). Every engine that has the codec overrides this and none of them
        needs a new builder for it — that is the whole point of the codec being an enum.

        The cast back is not decoration. ``HEX``/``BASE64`` go through a binary representation on
        several engines and MySQL hands back ``bytes`` rather than a string, which changes the
        *declared* column type and trips the type half of the equivalence pre-gate even though the
        rows match. So each spelling lands on the base type explicitly.
        """
        raise UnsupportedForDialect(
            f"{type(self).__name__} does not spell the {node.codec.value} codec; "
            "override value_codec_sql or leave that codec's builder at weight 0 for this engine"
        )

    def window_sql(self, node: WindowCall) -> str:
        """``MAX(c) OVER (PARTITION BY c ORDER BY c ROWS BETWEEN …)``.

        An engine that writes the frame or the function differently overrides this one method.
        """
        argument = "" if node.arg is None else self.expr(node.arg)
        clauses = []
        if node.partition_by:
            clauses.append("PARTITION BY " + ", ".join(self.expr(e) for e in node.partition_by))
        if node.order_by:
            clauses.append("ORDER BY " + ", ".join(self.expr(e) for e in node.order_by))
        if node.frame is not None:
            clauses.append(f"{node.frame.kind.value} BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING")
        return f"{node.function.value}({argument}) OVER ({' '.join(clauses)})"

    def case_sql(self, node: Case) -> str:
        condition = self.expr(node.condition)
        then_sql = self.expr(node.then_expr)
        else_sql = self.expr(node.else_expr)
        return f"CASE WHEN {condition} THEN {then_sql} ELSE {else_sql} END"


class PostgresSpelling(Spelling):
    """PostgreSQL. Every engine starts from this class and overrides what it writes
    differently."""

    def value_codec_sql(self, node: ValueCodecRoundTrip) -> str:
        """PostgreSQL's codec round trips. See :meth:`Spelling.value_codec_sql` on the cast back.

        ``encode``/``decode`` work on ``bytea`` and return ``text``, so both binary codecs are
        ``convert_from(decode(encode(x::bytea, fmt), fmt), 'UTF8')``. The JSON codec uses ``->>``,
        which already yields ``text``.

        **Unverified against a live server**: no PostgreSQL build was available when this was
        written (the tree ``campaign.py`` points at does not exist on this machine). The DuckDB,
        SQLite, MySQL and ClickHouse spellings below were each checked against a running engine;
        these three were not. Run ``--sweep`` on Postgres before trusting them.
        """
        inner = self.expr(node.arg)
        if node.codec is ValueCodec.JSON_PACK:
            return f"(jsonb_build_object('v', {inner}) ->> 'v')"
        fmt = "hex" if node.codec is ValueCodec.HEX else "base64"
        return f"convert_from(decode(encode(CAST({inner} AS BYTEA), '{fmt}'), '{fmt}'), 'UTF8')"

    def type_sql(self, data_type: SqlType) -> str:
        kind = data_type.get_type_kind()
        precision, scale = data_type.get_precision(), data_type.get_scale()

        if isinstance(data_type, IntegerType):
            return "INTEGER"
        if isinstance(data_type, DoubleType):
            return "DOUBLE PRECISION"
        if isinstance(data_type, NumericType):
            if precision is None:
                return "NUMERIC"
            return f"NUMERIC({precision})" if scale is None else f"NUMERIC({precision}, {scale})"
        if isinstance(data_type, TextType):
            return "TEXT"
        if isinstance(data_type, CharType):
            return "CHAR" if precision is None else f"CHAR({precision})"
        if isinstance(data_type, VarcharType):
            return "VARCHAR" if precision is None else f"VARCHAR({precision})"
        if isinstance(data_type, BooleanType):
            return "BOOLEAN"
        if isinstance(data_type, DateType):
            return "DATE"
        if isinstance(data_type, TimestampType):
            return "TIMESTAMP"
        if isinstance(data_type, JsonbType):
            return "JSONB"
        if isinstance(data_type, UuidType):
            return "UUID"
        if isinstance(data_type, Int4RangeType):
            return "INT4RANGE"
        raise UnsupportedForDialect(f"no PostgreSQL type name for {kind}")


#: Ordering matters in :meth:`PostgresSpelling.type_sql` — ``IntegerType`` is a
#: ``NumericType`` and ``CharType``/``TextType`` are ``VarcharType``, so subclasses must be
#: tested before their bases. This list is the reminder, and a test pins each mapping.
_SUBCLASS_BEFORE_BASE = (
    (IntegerType, NumericType),
    (CharType, VarcharType),
    (TextType, VarcharType),
)

#: The default spelling used wherever a dialect has not supplied one.
DEFAULT_SPELLING = PostgresSpelling()


def render(node: ExpressionNode, spelling: Spelling | None = None) -> str:
    """Render one expression with *spelling* (PostgreSQL by default). A convenience for
    tests and for call sites that have no dialect in scope."""
    return (spelling or DEFAULT_SPELLING).expr(node)
