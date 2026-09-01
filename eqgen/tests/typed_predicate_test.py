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

"""Contracts for the typed AST predicate generator.

Mirrors the example generator's predicate gates, plus: no CAST, same-family only, and every
printed predicate runs on DuckDB.
"""

from __future__ import annotations

import re

import pytest

from eqgen.core.catalog import Column, Table
from eqgen.core.types import (
    BooleanType,
    DateType,
    DoubleType,
    IntegerType,
    NumericType,
    TextType,
    TimestampType,
    VarcharType,
)
from eqgen.generators.typed_predicate import TypedPredicateSource, build_predicate, print_predicate
from eqgen.generators.typed_predicate.build import (
    Between,
    FuncCall,
    InList,
    IsDistinctFrom,
    Like,
    PortableLiteral,
)
from eqgen.ir.expr import ColumnRef, Comparison, ExpressionNode
from eqgen.plugins import PredicateSource

pytestmark = pytest.mark.unit

_SEEDS = range(120)

_NONDETERMINISTIC = (
    "random",
    "rand",
    "now",
    "current_timestamp",
    "current_date",
    "current_time",
    "localtime",
    "uuid",
    "nextval",
    "sysdate",
)


def _table() -> Table:
    return Table(
        "t",
        [
            Column("c_int", IntegerType(), 1),
            Column("c_big", NumericType(38, 0), 2),
            Column("c_dec", NumericType(10, 2), 3),
            Column("c_dbl", DoubleType(), 4),
            Column("c_txt", VarcharType(), 5),
            Column("c_chr", TextType(), 6),
            Column("c_flag", BooleanType(), 7),
            Column("c_date", DateType(), 8),
            Column("c_ts", TimestampType(), 9),
        ],
    )


def _all_predicates(*, dialect: str = "duckdb") -> list[str]:
    source = TypedPredicateSource(dialect=dialect)
    return [p for seed in _SEEDS if (p := source.boolean_predicate(_table(), seed=seed)) is not None]


def _walk(node: ExpressionNode):
    yield node
    if isinstance(node, Comparison):
        yield from _walk(node.left)
        yield from _walk(node.right)
        return
    if isinstance(node, IsDistinctFrom):
        yield from _walk(node.left)
        yield from _walk(node.right)
        return
    if isinstance(node, Between):
        yield from _walk(node.value)
        yield from _walk(node.low)
        yield from _walk(node.high)
        return
    if isinstance(node, InList):
        yield from _walk(node.value)
        for item in node.items:
            yield from _walk(item)
        return
    if isinstance(node, Like):
        yield from _walk(node.value)
        yield from _walk(node.pattern)
        return
    if isinstance(node, FuncCall):
        for arg in node.args:
            yield from _walk(arg)
        return
    for attr in ("left", "right", "operand"):
        child = getattr(node, attr, None)
        if isinstance(child, ExpressionNode):
            yield from _walk(child)


def test_typed_source_satisfies_the_protocol() -> None:
    assert isinstance(TypedPredicateSource(), PredicateSource)


def test_name_is_typed() -> None:
    assert TypedPredicateSource().name == "typed"


def test_declines_for_empty_table() -> None:
    assert TypedPredicateSource().boolean_predicate(Table("t", []), seed=1) is None
    assert build_predicate(Table("t", []), seed=1) is None


def test_generation_is_deterministic_per_seed() -> None:
    source = TypedPredicateSource(dialect="postgres")
    assert source.boolean_predicate(_table(), seed=5) == source.boolean_predicate(_table(), seed=5)


def test_predicates_are_row_local() -> None:
    for predicate in _all_predicates():
        assert not re.search(r"\b(COUNT|SUM|AVG|MIN|MAX)\s*\(", predicate, re.I), predicate
        assert "OVER" not in predicate.upper(), predicate
        assert "SELECT" not in predicate.upper(), predicate


def test_predicates_contain_no_nondeterministic_functions() -> None:
    for predicate in _all_predicates():
        lowered = predicate.lower()
        for name in _NONDETERMINISTIC:
            assert f"{name}(" not in lowered, f"{name} in {predicate}"


def test_predicates_use_bare_unqualified_column_names() -> None:
    known = {c.get_column_name() for c in _table().get_column_list()}
    for predicate in _all_predicates():
        for reference in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\.", predicate):
            pytest.fail(f"qualified reference {reference!r} in {predicate}")
        for name in re.findall(r"\bc_[a-z]+\b", predicate):
            assert name in known, f"unknown column {name!r} in {predicate}"


def test_no_type_casts() -> None:
    for predicate in _all_predicates():
        assert "CAST" not in predicate.upper(), predicate


def test_predicates_are_balanced() -> None:
    for predicate in _all_predicates():
        assert predicate.count("(") == predicate.count(")"), predicate


def test_predicates_escape_embedded_quotes() -> None:
    quoted = [p for p in _all_predicates() if "o''brien" in p]
    assert quoted, "expected the embedded-quote literal to be reached"
    for predicate in quoted:
        assert "'o''brien'" in predicate


def test_predicates_reach_null_tests() -> None:
    predicates = _all_predicates()
    assert any("IS NULL" in p for p in predicates)
    assert any("IS NOT NULL" in p for p in predicates)


def test_between_in_like_and_distinct_are_emitted() -> None:
    predicates = _all_predicates()
    assert any(" BETWEEN " in p for p in predicates)
    assert any(re.search(r"\bIN\s*\(", p) for p in predicates)
    assert any(" LIKE " in p for p in predicates)
    assert any("IS DISTINCT FROM" in p or "IS NOT DISTINCT FROM" in p for p in predicates)


def test_comparisons_stay_in_family() -> None:
    from eqgen.generators.typed_predicate.build import _same_family

    for seed in _SEEDS:
        for dialect in ("postgres", "duckdb"):
            node = build_predicate(_table(), seed=seed, dialect=dialect)
            assert node is not None
            for child in _walk(node):
                if isinstance(child, IsDistinctFrom):
                    assert _same_family(child.left.data_type, child.right.data_type), child
                elif isinstance(child, Between):
                    assert _same_family(child.value.data_type, child.low.data_type), child
                    assert _same_family(child.value.data_type, child.high.data_type), child
                elif isinstance(child, InList):
                    for item in child.items:
                        assert _same_family(child.value.data_type, item.data_type), child
                elif isinstance(child, Like):
                    assert isinstance(child.value.data_type, VarcharType), child
                    assert isinstance(child.pattern.data_type, VarcharType), child
                elif isinstance(child, Comparison):
                    assert _same_family(child.left.data_type, child.right.data_type), (
                        child.left,
                        child.right,
                    )


def test_dialect_catalogs_differ_on_boolean_greatest_and_string_length() -> None:
    """Signatures live in the catalogs: Postgres has no boolean GREATEST; length spellings differ."""
    from eqgen.generators.typed_predicate.func_spec import catalog_for

    pg = catalog_for("postgres")
    duck = catalog_for("duckdb")
    sqlite = catalog_for("sqlite")
    mysql = catalog_for("mysql")
    assert any(s.sql_name == "CHAR_LENGTH" for s in pg)
    assert not any(s.sql_name == "LENGTH" for s in pg)
    assert any(s.sql_name == "LENGTH" for s in duck)
    assert not any(s.sql_name == "CHAR_LENGTH" for s in duck)
    assert any(s.sql_name == "LENGTH" for s in sqlite)
    assert any(s.sql_name == "CHAR_LENGTH" for s in mysql)
    assert catalog_for("mariadb") is mysql or catalog_for("mariadb") == mysql
    assert catalog_for("cratedb") == pg
    assert not any(s.sql_name == "GREATEST" and BooleanType in s.arg_families for s in pg)
    assert any(s.sql_name == "GREATEST" and BooleanType in s.arg_families for s in duck)
    assert any(s.sql_name == "ends_with" for s in duck)
    assert not any(s.sql_name == "ends_with" for s in pg)


def test_mysql_printer_uses_null_safe_equals() -> None:
    from eqgen.generators.typed_predicate.print import MysqlPrinter, SqlitePrinter
    from eqgen.ir import expr as ir_expr

    left = ir_expr.col("a", IntegerType())
    right = ir_expr.col("b", IntegerType())
    mysql = MysqlPrinter()
    assert "<=>" in mysql.expr(IsDistinctFrom(left, right, negated=True))
    assert "NOT" in mysql.expr(IsDistinctFrom(left, right, negated=False))
    sqlite = SqlitePrinter()
    mod = ir_expr.mod(left, 2)
    assert "%" in sqlite.mod_sql(mod)
    assert "MOD(" not in sqlite.mod_sql(mod)


def test_dialect_functions_are_emitted() -> None:
    pg = _all_predicates(dialect="postgres")
    duck = _all_predicates(dialect="duckdb")
    assert any("ABS(" in p or "CHAR_LENGTH(" in p or "UPPER(" in p or "INITCAP(" in p for p in pg)
    assert any("ABS(" in p or "LENGTH(" in p or "ends_with(" in p or "UPPER(" in p for p in duck)
    assert not any("ends_with(" in p for p in pg)
    assert not any("INITCAP(" in p for p in duck)


def _duckdb_connection():
    duckdb = pytest.importorskip("duckdb")
    connection = duckdb.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE t (
            c_int INTEGER, c_big BIGINT, c_dec DECIMAL(10, 2), c_dbl DOUBLE,
            c_txt VARCHAR, c_chr VARCHAR, c_flag BOOLEAN, c_date DATE, c_ts TIMESTAMP
        )
        """
    )
    connection.execute(
        """
        INSERT INTO t VALUES
            (1, 2, 3.50, 1.5, 'a', 'b', TRUE, '2024-01-15', '2024-01-15 12:34:56'),
            (NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL),
            (-1, -2, -3.50, -1.5, '', 'trailing ', FALSE, '1999-12-31', '1999-12-31 23:59:59')
        """
    )
    return connection


def test_every_generated_predicate_runs_on_duckdb() -> None:
    connection = _duckdb_connection()
    failures: list[tuple[str, str]] = []
    for predicate in _all_predicates(dialect="duckdb"):
        try:
            connection.execute(f"SELECT 1 FROM t WHERE {predicate}").fetchall()
        except Exception as exc:
            failures.append((predicate, str(exc).splitlines()[0]))
    assert not failures, f"{len(failures)} predicate(s) rejected, e.g. {failures[:3]}"


def test_cli_wires_typed_source() -> None:
    from eqgen.fuzz.cli import predicate_source_for

    source = predicate_source_for("typed", dialect="duckdb")
    assert isinstance(source, TypedPredicateSource)
    assert source.dialect == "duckdb"
    assert source.boolean_predicate(_table(), seed=1)
