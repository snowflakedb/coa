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

"""The MySQL dialect.

Offline tests always run. Live tests skip unless Docker is available (or fail clearly if
``EQGEN_MYSQL_BINDIR`` is set — local mysqld is not implemented in v1).
"""

from __future__ import annotations

import pytest

from eqgen.core.catalog import Column, Table
from eqgen.core.types import BooleanType, DoubleType, IntegerType, NumericType, TextType, VarcharType
from eqgen.dialects.mysql.cluster import MYSQL_COLLATION
from eqgen.dialects.mysql.types_sql import TEXT_LENGTH, mysql_cast_type, mysql_literal, mysql_type
from eqgen.equivalence.ast import BaseTableSource, ProjectionItem, SelectQuery
from eqgen.equivalence.config import default_equivalence_config
from eqgen.equivalence.emitter import emit_equivalence
from eqgen.equivalence.factory import EquivalenceBuilderFactory
from eqgen.equivalence.generator import EquivalenceGenerator
from eqgen.fuzz.compare import compare_objects
from eqgen.fuzz.database import Database, column_names, hidden_base_name
from eqgen.fuzz.journal import sample_rows
from eqgen.fuzz.round import run_round
from eqgen.generators.example_generator import RandomPredicateSource, RandomSelectSource
from eqgen.ir.expr import Comparison, IntLiteral, col

pytestmark = pytest.mark.unit


def _docker_ready() -> bool:
    from eqgen.dialects.mysql.cluster import docker_available

    return docker_available()


def _adapter():
    pytest.importorskip("pymysql")
    if not _docker_ready():
        pytest.skip("Docker not available for MySQL live tests")
    from eqgen.dialects.mysql.adapter import MySqlAdapter

    return MySqlAdapter()


# ---------------------------------------------------------------------------
# Offline
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data_type", "expected"),
    [
        (IntegerType(), "BIGINT"),
        (NumericType(10, 2), "DECIMAL(10, 2)"),
        (NumericType(38, 0), "BIGINT"),
        (DoubleType(), "DOUBLE"),
        (VarcharType(), f"VARCHAR({TEXT_LENGTH})"),
        (TextType(), f"VARCHAR({TEXT_LENGTH})"),
        (BooleanType(), "TINYINT(1)"),
    ],
)
def test_mysql_type_ddl(data_type: object, expected: str) -> None:
    assert mysql_type(data_type) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("data_type", "expected"),
    [
        (IntegerType(), "SIGNED"),
        (NumericType(10, 2), "DECIMAL(10, 2)"),
        (BooleanType(), "SIGNED"),
        (TextType(), f"CHAR({TEXT_LENGTH})"),
    ],
)
def test_mysql_cast_type_differs_from_ddl_where_needed(data_type: object, expected: str) -> None:
    assert mysql_cast_type(data_type) == expected  # type: ignore[arg-type]


def test_mysql_literal_bool_before_int() -> None:
    assert mysql_literal(True) == "1"
    assert mysql_literal(False) == "0"
    assert mysql_literal(None) == "NULL"
    assert mysql_literal("a'b") == "'a''b'"


def test_mysql_known_issue_labels_obrien_syntax() -> None:
    """Apostrophe-in-literal 1064s against UNION under NBE must not flood ERROR findings.

    Match the characteristic mid-literal cursor (``near '<rest>')'``, ``near '<rest>' when``,
    ``near '<rest>' >``, ``near ''…'' and/like``, ``near '')'``, ``near '' <>``, ``near '\\'…``),
    not every 1064.
    """
    from eqgen.dialects.mysql.adapter import MySqlAdapter

    adapter = MySqlAdapter.__new__(MySqlAdapter)  # no Docker — label logic is pure
    for cursor in (
        "near 'brien')' at line 1",
        "near 'rG')' at line 1",
        "near 'KF&' when `t0`.`c_txt` then false else false end))",
        "near 'H' > `t0`.`c_txt`)",
        "near ''p}[X[\\'' < `t1`.`c_txt`)",
        "near ''UZua|吓&S\\'' and `t1`.`c_txt`)",
        "near '')' at line 1",
        "near '\\'7L_렿㘧<' when `t0`.`c_chr` then true end))",
        "near ''ꔲMA\\'' like `t0`.`c_chr`)))",
        "near '' <> `t1`.`c_txt`)",
    ):
        exc = Exception(1064, f"You have an error in your SQL syntax; check the manual {cursor}")
        assert adapter.known_issue_label(exc) == "mysql-obrien-apostrophe-syntax", cursor
    other = Exception(1064, "You have an error in your SQL syntax; near 'SELECT' at line 1")
    assert adapter.known_issue_label(other) is None
    assert adapter.known_issue_label(Exception(1054, "near 'brien')' at line 1")) is None


def test_mysql_known_issue_labels_numeric_out_of_range() -> None:
    """STRICT-mode COT/LN domain errors (errno 1690) are one-sided under rewrite — demote."""
    from eqgen.dialects.mysql.adapter import MariaDbAdapter, MySqlAdapter

    for adapter_cls in (MySqlAdapter, MariaDbAdapter):
        adapter = adapter_cls.__new__(adapter_cls)
        exc = Exception(1690, "DOUBLE value is out of range in 'cot(`l`.`c_txt`)'")
        assert adapter.known_issue_label(exc) == "numeric-out-of-range (invalid argument)"
        assert adapter.known_issue_label(Exception(1064, "syntax")) != "numeric-out-of-range (invalid argument)"


def test_mysql_index_builders_emit_create_index_then_view() -> None:
    from eqgen.dialects.mysql.builders import MySqlPlainIndexBuilder, MySqlUniqueIndexBuilder
    from eqgen.dialects.mysql.emitter import MySqlEmitter
    from eqgen.equivalence.context import EquivalenceContext, NameGenerator

    table = Table(
        "t",
        [
            Column("c_pk", IntegerType(), 1, nullable=False),
            Column("c_txt", TextType(), 2),
        ],
    )
    factory = EquivalenceBuilderFactory()
    context = EquivalenceContext(
        default_equivalence_config(),
        base_table=table,
        predicate_source=None,
        name_generator=NameGenerator(),
    )
    query = SelectQuery(
        BaseTableSource(table),
        [
            ProjectionItem("c_pk", col("c_pk", IntegerType()), IntegerType()),
            ProjectionItem("c_txt", col("c_txt", TextType()), TextType()),
        ],
    )
    plain = MySqlPlainIndexBuilder(factory)._wrap(query, context, exposed_name="t")
    assert plain is not None
    statements = [s.statement_text for s in emit_equivalence(plain, MySqlEmitter())]
    assert any(s.startswith("CREATE INDEX") for s in statements), statements
    assert statements[-1].startswith("CREATE VIEW t AS SELECT")

    unique = MySqlUniqueIndexBuilder(factory)._wrap(query, context, exposed_name="t")
    assert unique is not None
    ustmts = [s.statement_text for s in emit_equivalence(unique, MySqlEmitter())]
    assert any("CREATE UNIQUE INDEX" in s for s in ustmts), ustmts


def test_mysql_json_pack_round_trip_builder_guards_json_null() -> None:
    from eqgen.dialects.mysql.builders import MySqlJsonPackRoundTripBuilder
    from eqgen.dialects.mysql.emitter import MySqlEmitter
    from eqgen.equivalence.context import EquivalenceContext, NameGenerator

    table = Table(
        "t",
        [
            Column("c_pk", IntegerType(), 1, nullable=False),
            Column("c_txt", TextType(), 2),
        ],
    )
    factory = EquivalenceBuilderFactory()
    context = EquivalenceContext(
        default_equivalence_config(),
        base_table=table,
        predicate_source=None,
        name_generator=NameGenerator(),
    )
    query = SelectQuery(
        BaseTableSource(table),
        [
            ProjectionItem("c_pk", col("c_pk", IntegerType()), IntegerType()),
            ProjectionItem("c_txt", col("c_txt", TextType()), TextType()),
        ],
    )
    node = MySqlJsonPackRoundTripBuilder(factory)._wrap(query, context, exposed_name="t")
    assert node is not None
    statements = [s.statement_text for s in emit_equivalence(node, MySqlEmitter())]
    view = statements[-1]
    assert any("JSON_OBJECT('c_pk', c_pk, 'c_txt', c_txt)" in s for s in statements), statements
    # JSON_UNQUOTE belongs on the text column and *only* the text column: applying it to a number
    # would be wrong, and asserting on a bare substring cannot tell the two apart.
    assert "CAST(JSON_UNQUOTE(JSON_EXTRACT(eq_json.j, '$.c_txt')) AS CHAR(255))" in view, view
    assert "JSON_UNQUOTE(JSON_EXTRACT(eq_json.j, '$.c_pk'))" not in view, view
    # The COLLATE is load-bearing: without it the CHAR cast falls back to the connection default
    # (case-insensitive) and silently flips string comparisons. See MySqlEmitter's docstring.
    assert f"AS CHAR(255)) COLLATE {MYSQL_COLLATION}" in view, view
    assert any("CAST('null' AS JSON)" in s for s in statements), statements
    assert view.startswith("CREATE VIEW t AS SELECT")


def test_mysql_json_pack_collation_follows_the_dialect_not_a_hardcoded_name() -> None:
    """MariaDB's NO PAD binary collation has a different name than MySQL's, so a hardcoded
    name would fail or change trailing-space comparison on the dialect that inherits this emitter."""
    from eqgen.dialects.mysql.builders import MySqlJsonPackRoundTripBuilder
    from eqgen.dialects.mysql.cluster import MARIADB_COLLATION
    from eqgen.dialects.mysql.emitter import MySqlEmitter
    from eqgen.equivalence.context import EquivalenceContext, NameGenerator

    assert MARIADB_COLLATION != MYSQL_COLLATION  # else this test proves nothing
    assert MARIADB_COLLATION == "utf8mb4_nopad_bin"  # PAD SPACE utf8mb4_bin is a false-mismatch trap
    table = Table(
        "t",
        [
            Column("c_pk", IntegerType(), 1, nullable=False),
            Column("c_txt", TextType(), 2),
        ],
    )
    factory = EquivalenceBuilderFactory()
    context = EquivalenceContext(
        default_equivalence_config(),
        base_table=table,
        predicate_source=None,
        name_generator=NameGenerator(),
    )
    query = SelectQuery(
        BaseTableSource(table),
        [
            ProjectionItem("c_pk", col("c_pk", IntegerType()), IntegerType()),
            ProjectionItem("c_txt", col("c_txt", TextType()), TextType()),
        ],
    )
    node = MySqlJsonPackRoundTripBuilder(factory)._wrap(query, context, exposed_name="t")
    assert node is not None
    view = [s.statement_text for s in emit_equivalence(node, MySqlEmitter(collation=MARIADB_COLLATION))][-1]
    assert f"COLLATE {MARIADB_COLLATION}" in view, view
    assert MYSQL_COLLATION not in view, view


def test_qualify_rewrite_needs_projection() -> None:
    from eqgen.dialects.mysql.emitter import MySqlQueryRenderer, MySqlSpelling

    table = Table("t", [Column("c_pk", IntegerType(), 1)])
    query = SelectQuery(
        BaseTableSource(table),
        [ProjectionItem("c_pk", col("c_pk", IntegerType()), IntegerType())],
        qualify=Comparison(">=", col("c_pk", IntegerType()), IntLiteral(1)),
    )
    sql = MySqlQueryRenderer(spelling=MySqlSpelling()).visit_select_query(query)
    assert "eq_qsrc" in sql
    assert "WHERE eq_q" in sql
    assert "QUALIFY" not in sql


def test_the_dialect_declares_its_builder_set_in_gcl() -> None:
    from eqgen.dialects.mysql.adapter import mysql_equivalence_config
    from eqgen.dialects.mysql.builders import (
        MySqlInnodbTableBuilder,
        MySqlInvisibleIndexBuilder,
        MySqlJsonPackRoundTripBuilder,
        MySqlPlainIndexBuilder,
        MySqlPrefixIndexBuilder,
        MySqlUniqueIndexBuilder,
    )

    portable = set(default_equivalence_config().builder_weights)
    mysql = set(mysql_equivalence_config().builder_weights)
    extras = {
        MySqlPlainIndexBuilder.__name__,
        MySqlUniqueIndexBuilder.__name__,
        MySqlInvisibleIndexBuilder.__name__,
        MySqlPrefixIndexBuilder.__name__,
        MySqlInnodbTableBuilder.__name__,
        MySqlJsonPackRoundTripBuilder.__name__,
    }
    assert portable <= mysql, portable - mysql
    assert mysql - portable == extras, (mysql - portable) ^ extras


# ---------------------------------------------------------------------------
# Live
# ---------------------------------------------------------------------------


def test_two_connections_cannot_see_each_others_databases() -> None:
    adapter = _adapter()
    a = adapter.connect()
    b = adapter.connect()
    try:
        a.execute("CREATE TABLE only_a (id INT)")
        with pytest.raises(adapter.db_error):
            b.execute("SELECT * FROM only_a")
    finally:
        a.close()
        b.close()


def test_a_failed_statement_does_not_poison_the_session() -> None:
    adapter = _adapter()
    conn = adapter.connect()
    try:
        with pytest.raises(adapter.db_error):
            conn.execute("SELECT this is not sql")
        rows = list(conn.execute("SELECT 1 AS x").fetchall())
        assert rows == [(1,)]
    finally:
        conn.close()


def test_a_round_runs_end_to_end_on_mysql() -> None:
    adapter = _adapter()
    table = adapter.simple_catalog("t")
    rows = sample_rows(table, 8, seed=3)
    generator = EquivalenceGenerator(
        adapter.equivalence_config(),
        predicate_source=RandomPredicateSource(),
        emitter=adapter.emitter(),
        extra_builders=adapter.extra_builders(),
    )
    queries = list(RandomSelectSource().iter_queries(table, seed=5, limit=8))
    outcome = run_round(adapter, generator, table, rows, queries, seed=11)
    assert outcome.setup_error is None
    assert outcome.object_comparison is not None and outcome.object_comparison.equal
    assert not outcome.findings, [f.query for f in outcome.findings]


def test_generated_objects_hold_the_same_rows_on_mysql() -> None:
    adapter = _adapter()
    table = adapter.simple_catalog("t")
    rows = sample_rows(table, 8, seed=3)
    hidden = Table(hidden_base_name(table), table.get_column_list())
    generator = EquivalenceGenerator(
        adapter.equivalence_config(),
        predicate_source=RandomPredicateSource(),
        emitter=adapter.emitter(),
        extra_builders=adapter.extra_builders(),
    )
    for seed in range(4):
        statements = [
            s.statement_text for s in generator.generate(hidden, seed=seed, exposed_name="t").setup_statements
        ]
        base = Database.build_base(adapter, table, rows)
        equivalent = Database.build_equivalent(adapter, table, rows, statements=statements)
        try:
            comparison = compare_objects(base, equivalent, table, column_names(table))
            assert comparison.equal, f"seed {seed}: {comparison.verdict}"
        finally:
            base.close()
            equivalent.close()


def test_mysql_json_pack_declines_on_duplicate_aliases() -> None:
    """``JSON_OBJECT`` keeps only the last value for a repeated key, so two identically-aliased
    columns would both read back the second one's value."""
    from eqgen.dialects.mysql.builders import MySqlJsonPackRoundTripBuilder
    from eqgen.equivalence.context import EquivalenceContext, NameGenerator

    table = Table("t", [Column("c_pk", IntegerType(), 1, nullable=False)])
    factory = EquivalenceBuilderFactory()
    context = EquivalenceContext(
        default_equivalence_config(),
        base_table=table,
        predicate_source=None,
        name_generator=NameGenerator(),
    )
    dupe = SelectQuery(
        BaseTableSource(table),
        [
            ProjectionItem("c_dup", col("c_pk", IntegerType()), IntegerType()),
            ProjectionItem("c_dup", col("c_pk", IntegerType()), IntegerType()),
        ],
    )
    assert MySqlJsonPackRoundTripBuilder(factory)._wrap(dupe, context, exposed_name="t") is None
    unique = SelectQuery(
        BaseTableSource(table),
        [ProjectionItem("c_pk", col("c_pk", IntegerType()), IntegerType())],
    )
    assert MySqlJsonPackRoundTripBuilder(factory)._wrap(unique, context, exposed_name="t") is not None
