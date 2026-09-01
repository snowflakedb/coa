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

"""Turn a predicate AST into dialect SQL.

This is the generator's own printer — not :mod:`eqgen.ir.render` and not anything under
``dialects/``. Shared shapes use portable spelling; dialect subclasses are hooks for later
function overrides, not a per-function allowlist.
"""

from __future__ import annotations

from eqgen.generators.typed_predicate.build import (
    Between,
    FuncCall,
    InList,
    IsDistinctFrom,
    Like,
    PortableLiteral,
)
from eqgen.ir.expr import (
    BoolOp,
    ColumnRef,
    Comparison,
    ExpressionNode,
    IntLiteral,
    IsNull,
    Mod,
    Not,
)

#: Node kinds that need no parentheses as operands.
_ATOMIC = (ColumnRef, IntLiteral, PortableLiteral, Mod, FuncCall)


class UnsupportedPrint(RuntimeError):
    """This printer does not know how to write *node* — extend it rather than guess."""


class Printer:
    """Portable predicate printing. Dialects subclass and override function forms."""

    def expr(self, node: ExpressionNode) -> str:
        if isinstance(node, ColumnRef):
            if node.relation_alias is not None:
                return f"{node.relation_alias}.{node.name}"
            return node.name
        if isinstance(node, IntLiteral):
            return str(node.value)
        if isinstance(node, PortableLiteral):
            return node.sql
        if isinstance(node, Comparison):
            return f"{self._operand(node.left)} {node.operator} {self._operand(node.right)}"
        if isinstance(node, BoolOp):
            return f"{self._operand(node.left)} {node.operator} {self._operand(node.right)}"
        if isinstance(node, Not):
            return f"NOT {self._operand(node.operand)}"
        if isinstance(node, IsNull):
            return f"{self._operand(node.operand)} IS {'NOT ' if node.negated else ''}NULL"
        if isinstance(node, IsDistinctFrom):
            op = "IS NOT DISTINCT FROM" if node.negated else "IS DISTINCT FROM"
            return f"{self._operand(node.left)} {op} {self._operand(node.right)}"
        if isinstance(node, Between):
            op = "NOT BETWEEN" if node.negated else "BETWEEN"
            return (
                f"{self._operand(node.value)} {op} {self._operand(node.low)}"
                f" AND {self._operand(node.high)}"
            )
        if isinstance(node, InList):
            op = "NOT IN" if node.negated else "IN"
            items = ", ".join(self.expr(item) for item in node.items)
            return f"{self._operand(node.value)} {op} ({items})"
        if isinstance(node, Like):
            op = "NOT LIKE" if node.negated else "LIKE"
            return f"{self._operand(node.value)} {op} {self._operand(node.pattern)}"
        if isinstance(node, FuncCall):
            args = ", ".join(self.expr(arg) for arg in node.args)
            return f"{node.sql_name}({args})"
        if isinstance(node, Mod):
            return self.mod_sql(node)
        raise UnsupportedPrint(f"no printing for {type(node).__name__}")

    def _operand(self, node: ExpressionNode) -> str:
        """Bracket compound operands so ``NOT`` / ``AND`` / ``OR`` cannot rebind."""
        rendered = self.expr(node)
        return rendered if isinstance(node, _ATOMIC) else f"({rendered})"

    def mod_sql(self, node: Mod) -> str:
        """``MOD(x, n)``. An engine that prefers ``x % n`` overrides this."""
        return f"MOD({self.expr(node.operand)}, {node.modulus})"


class PostgresPrinter(Printer):
    """PostgreSQL — portable forms; override methods when a function differs."""


class DuckDBPrinter(Printer):
    """DuckDB — inherits portable forms; override methods when a function differs."""


class SqlitePrinter(Printer):
    """SQLite — ``%`` for remainder; ``IS [NOT] DISTINCT FROM`` needs ≥3.39 (eqgen pins 3.53)."""

    def mod_sql(self, node: Mod) -> str:
        return f"({self.expr(node.operand)} % {node.modulus})"


class MysqlPrinter(Printer):
    """MySQL / MariaDB / TiDB / Dolt — null-safe equals via ``<=>``."""

    def expr(self, node: ExpressionNode) -> str:
        if isinstance(node, IsDistinctFrom):
            left = self._operand(node.left)
            right = self._operand(node.right)
            # ``<=>`` is IS NOT DISTINCT FROM; invert for the positive DISTINCT form.
            if node.negated:
                return f"({left} <=> {right})"
            return f"(NOT ({left} <=> {right}))"
        return super().expr(node)


class ClickHousePrinter(Printer):
    """ClickHouse — portable forms (``MOD``, ``IS [NOT] DISTINCT FROM``, ``LIKE``)."""


#: Dialects that share the MySQL-family printer + function catalog.
_MYSQL_FAMILY = frozenset({"mysql", "mariadb", "tidb", "dolt"})


def printer_for(dialect: str) -> Printer:
    """The printer for *dialect*."""
    if dialect == "duckdb":
        return DuckDBPrinter()
    if dialect == "postgres" or dialect == "cratedb":
        return PostgresPrinter()
    if dialect == "sqlite":
        return SqlitePrinter()
    if dialect == "clickhouse":
        return ClickHousePrinter()
    if dialect in _MYSQL_FAMILY:
        return MysqlPrinter()
    raise ValueError(f"unknown dialect for typed predicates: {dialect!r}")


def print_predicate(node: ExpressionNode, *, dialect: str) -> str:
    """SQL text for *node* under *dialect*."""
    return printer_for(dialect).expr(node)
