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

"""The SQLite dialect — pinned 3.53.4 amalgamation; offline gcl/types + live rounds."""

from __future__ import annotations

# Bind the pinned lib before any sqlite3 import in this module.
from eqgen.dialects.sqlite.ensure import PINNED_VERSION, bootstrap

bootstrap()

import sqlite3

import pytest

from eqgen.core.types import BooleanType, DoubleType, IntegerType, NumericType, TextType
from eqgen.dialects.sqlite.types_sql import sqlite_literal, sqlite_type
from eqgen.equivalence.config import default_equivalence_config

pytestmark = pytest.mark.unit

_SQLITE_NATIVE = {
    "SqliteCreateIndexBuilder",
    "SqliteUniqueIndexMatBuilder",
    "SqlitePartialIndexBuilder",
    "SqliteTruthyPartialIndexBuilder",
    "SqliteConstantPartialIndexBuilder",
    "SqliteAttachRoundTripBuilder",
    "SqliteWithoutRowidTableBuilder",
    "SqliteGeneratedColumnRoundTripBuilder",
    "SqliteStoredGeneratedColumnRoundTripBuilder",
    "SqliteStrictTableBuilder",
    "SqliteExpressionIndexMatBuilder",
    "SqliteWithoutRowidIndexedBuilder",
    "SqliteRecursiveCteIdentityBuilder",
    "SqliteNestedMaterializedCteBuilder",
    "SqliteAnalyzeIndexMatBuilder",
}

@pytest.mark.parametrize(
    ("data_type", "expected"),
    [
        (IntegerType(), "INTEGER"),
        (BooleanType(), "INTEGER"),
        (DoubleType(), "REAL"),
        (NumericType(10, 2), "NUMERIC"),
        (TextType(), "TEXT"),
    ],
)
def test_sqlite_type(data_type: object, expected: str) -> None:
    assert sqlite_type(data_type) == expected  # type: ignore[arg-type]


def test_sqlite_literal() -> None:
    assert sqlite_literal(True) == "1"
    assert sqlite_literal(None) == "NULL"
    assert sqlite_literal("a'b") == "'a''b'"


def test_the_dialect_declares_its_builder_set_in_gcl() -> None:
    from eqgen.dialects.sqlite.adapter import sqlite_equivalence_config

    portable = set(default_equivalence_config().builder_weights)
    sqlite_cfg = set(sqlite_equivalence_config().builder_weights)
    assert portable <= sqlite_cfg, portable - sqlite_cfg
    assert sqlite_cfg - portable == _SQLITE_NATIVE, sqlite_cfg - portable


def test_engine_banner_is_pinned_353() -> None:
    from eqgen.dialects.sqlite.adapter import SqliteAdapter

    banner = SqliteAdapter().engine_banner()
    assert sqlite3.sqlite_version.startswith("3.53"), sqlite3.sqlite_version
    assert sqlite3.sqlite_version in banner
    assert "eqgen cache" in banner
    assert PINNED_VERSION.startswith("3.53")


def test_extra_builders_are_registered() -> None:
    from eqgen.dialects.sqlite.adapter import SqliteAdapter
    from eqgen.equivalence.generator import EquivalenceGenerator

    adapter = SqliteAdapter()
    names = {cls.__name__ for cls in adapter.extra_builders()}
    assert names == _SQLITE_NATIVE
    gen = EquivalenceGenerator(adapter.equivalence_config(), extra_builders=adapter.extra_builders())
    registered = set(gen.factory.registered_builder_names)
    assert _SQLITE_NATIVE <= registered


def test_native_mats_emit_expected_sql() -> None:
    from eqgen.core.catalog import Column, Table
    from eqgen.core.types import IntegerType, TextType
    from eqgen.dialects.sqlite.ast import (
        SqliteCreateAttach,
        SqliteCreateGenerated,
        SqliteCreateIndex,
        SqliteCreateStrict,
        SqliteCreateWithoutRowid,
    )
    from eqgen.dialects.sqlite.emitter import SqliteEmitter
    from eqgen.equivalence.ast import BaseTableSource, CreateTable, CreateView, SelectQuery
    from eqgen.equivalence.context import NameGenerator, ObjectNamer
    from eqgen.equivalence.emitter import emit_equivalence

    table = Table("t", [Column("c_pk", IntegerType(), nullable=False), Column("c_txt", TextType())])
    namer = ObjectNamer("t", NameGenerator())
    body_tbl = CreateTable.build(namer, SelectQuery(BaseTableSource(table)))
    body_view = CreateView.build(namer, SelectQuery(BaseTableSource(table)))
    out = ["c_pk", "c_txt"]
    defs = ["c_pk INTEGER NOT NULL", "c_txt TEXT"]

    cases = [
        (
            SqliteCreateIndex.build(namer, body_tbl, target="c_txt", out_cols=out),
            ("CREATE INDEX", "CREATE VIEW"),
        ),
        (
            SqliteCreateIndex.build(
                namer, body_tbl, target="c_pk", out_cols=out, unique=True, where_sql="c_txt IS NOT NULL"
            ),
            ("CREATE UNIQUE INDEX", "WHERE c_txt IS NOT NULL", "CREATE VIEW"),
        ),
        (
            SqliteCreateAttach.build(namer, body_view),
            ("ATTACH DATABASE", "CREATE TABLE", "DETACH DATABASE"),
        ),
        (
            SqliteCreateWithoutRowid.build(namer, body_view, col_defs=defs, out_cols=out),
            ("WITHOUT ROWID", "INSERT INTO", "CREATE VIEW"),
        ),
        (
            SqliteCreateGenerated.build(namer, body_view, col_defs=defs, out_cols=out),
            ("GENERATED ALWAYS AS", "VIRTUAL", "CREATE VIEW"),
        ),
        (
            SqliteCreateStrict.build(namer, body_view, col_defs=defs, out_cols=out),
            ("STRICT", "INSERT INTO", "CREATE VIEW"),
        ),
    ]
    for node, needles in cases:
        texts = "\n".join(s.statement_text for s in emit_equivalence(node, SqliteEmitter()))
        for needle in needles:
            assert needle in texts, (needle, texts)
        if isinstance(node, SqliteCreateAttach):
            assert f"CREATE TABLE {node.exposed.name} AS" in texts, texts
            assert f"CREATE VIEW {node.exposed.name} AS" not in texts, texts


def test_sqlite_emitter_keeps_native_right_and_full_outer_joins() -> None:
    from eqgen.core.catalog import Column, Table
    from eqgen.core.types import IntegerType, TextType
    from eqgen.dialects.sqlite.emitter import SqliteEmitter
    from eqgen.equivalence.ast import (
        BaseTableSource,
        CreateView,
        JoinQuery,
        ProjectionItem,
        SelectQuery,
    )
    from eqgen.equivalence.context import NameGenerator, ObjectNamer
    from eqgen.equivalence.emitter import emit_equivalence
    from eqgen.ir import expr

    table = Table("t", [Column("c_pk", IntegerType(), nullable=False), Column("c_txt", TextType())])
    namer = ObjectNamer("t", NameGenerator())
    left = CreateView.build(namer, SelectQuery(BaseTableSource(table)))
    right = CreateView.build(namer, SelectQuery(BaseTableSource(table)))
    items = (
        ProjectionItem("c_pk", expr.qualified_col("l", "c_pk", IntegerType()), IntegerType()),
        ProjectionItem("c_txt", expr.qualified_col("l", "c_txt", TextType()), TextType()),
    )
    cond = expr.eq(expr.qualified_col("l", "c_pk", IntegerType()), expr.qualified_col("r", "c_pk", IntegerType()))
    # Pinned amalgamation ≥3.39: emit native RIGHT/FULL (do not collapse to LEFT).
    for join_type in ("RIGHT OUTER", "FULL OUTER"):
        node = CreateView.build(namer, JoinQuery(left, right, cond, join_type, items, "l", "r"))
        sql = emit_equivalence(node, SqliteEmitter())[-1].statement_text
        assert f"{join_type} JOIN" in sql, sql
        assert "LEFT OUTER JOIN" not in sql, sql


def test_sqlite_emitter_casts_projections_for_affinity() -> None:
    """Views/windows/unions must re-declare affinity via CAST (sqlite datatype3 §3.2–3.3.1)."""
    from eqgen.core.catalog import Column, Table
    from eqgen.core.types import IntegerType, TextType
    from eqgen.dialects.sqlite.emitter import SqliteEmitter
    from eqgen.equivalence.ast import (
        BaseTableSource,
        CreateView,
        ProjectionItem,
        SelectQuery,
        UnionAllQuery,
    )
    from eqgen.equivalence.context import NameGenerator, ObjectNamer
    from eqgen.equivalence.emitter import emit_equivalence
    from eqgen.ir import expr

    table = Table("t", [Column("c_pk", IntegerType(), nullable=False), Column("c_txt", TextType())])
    source = BaseTableSource(table)
    namer = ObjectNamer("t", NameGenerator())
    items = (
        ProjectionItem("c_pk", expr.col("c_pk", IntegerType()), IntegerType()),
        ProjectionItem("c_txt", expr.col("c_txt", TextType()), TextType()),
    )
    view = CreateView.build(namer, SelectQuery(source, items))
    sql = emit_equivalence(view, SqliteEmitter())[-1].statement_text
    assert "CAST(" in sql and "AS TEXT)" in sql and "AS INTEGER)" in sql, sql

    left = CreateView.build(namer, SelectQuery(source, items))
    right = CreateView.build(namer, SelectQuery(source, items))
    unioned = CreateView.build(namer, UnionAllQuery(left, right))
    union_sql = emit_equivalence(unioned, SqliteEmitter())[-1].statement_text
    assert "UNION ALL" in union_sql and "CAST(" in union_sql, union_sql


def test_sqlite_affinity_mismatch_agrees_after_cast() -> None:
    """The known false positive: int > TEXT agrees once the view column has TEXT affinity."""
    con = sqlite3.connect(":memory:")
    c = con.cursor()
    c.executescript(
        """
        CREATE TABLE t (c_pk INTEGER NOT NULL, c_txt TEXT);
        INSERT INTO t VALUES (3, '');
        CREATE TABLE t0 AS SELECT * FROM t;
        CREATE VIEW v_raw AS
          SELECT LAST_VALUE(c_txt) OVER (PARTITION BY c_txt ORDER BY c_txt) AS c_txt FROM t;
        CREATE VIEW v_cast AS
          SELECT CAST(LAST_VALUE(c_txt) OVER (PARTITION BY c_txt ORDER BY c_txt) AS TEXT) AS c_txt FROM t;
        """
    )
    q = "SELECT c_txt FROM {} WHERE ((1462169534)>(c_txt))"
    base = c.execute(q.format("t0")).fetchall()
    assert base == [("",)]
    assert c.execute(q.format("v_raw")).fetchall() == []
    assert c.execute(q.format("v_cast")).fetchall() == base


# ---------------------------------------------------------------------------
# Live rounds
# ---------------------------------------------------------------------------


def test_a_round_runs_end_to_end_on_sqlite() -> None:
    """The broad gate: setup applies, the equivalent holds the base's rows, no finding fires.

    Cheap enough to keep (SQLite is embedded, no server), and it is the only test that exercises
    the generator, emitter and comparison together on this dialect.
    """
    from eqgen.dialects.sqlite.adapter import SqliteAdapter
    from eqgen.equivalence.generator import EquivalenceGenerator
    from eqgen.fuzz.journal import sample_rows
    from eqgen.fuzz.round import run_round
    from eqgen.generators.example_generator import RandomPredicateSource, RandomSelectSource

    adapter = SqliteAdapter()
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


@pytest.mark.parametrize("builder_name", sorted(_SQLITE_NATIVE))
def test_each_native_builder_runs_and_preserves_rows_on_sqlite(builder_name: str) -> None:
    """Per-builder gate: with this builder heavily favoured, every round must apply cleanly and
    hold the base's rows.

    ``test_native_mats_emit_expected_sql`` asserts on SQL *text* for a subset of node kinds, which
    cannot catch a statement live SQLite rejects — and several builders had no coverage of either
    kind. Weighting one builder up at a time is what makes a failure name its own cause.

    The weights are *copied* rather than replaced: a name absent from the dict is eligible, so
    dropping the dialect's explicit ``weight = 0`` entries (``CREATE MATERIALIZED VIEW``, which
    SQLite does not have) would reintroduce them.
    """
    from dataclasses import replace

    from eqgen.dialects.sqlite.adapter import SqliteAdapter
    from eqgen.equivalence.generator import EquivalenceGenerator
    from eqgen.fuzz.journal import sample_rows
    from eqgen.fuzz.round import run_round
    from eqgen.generators.example_generator import RandomPredicateSource, RandomSelectSource

    adapter = SqliteAdapter()
    base = adapter.equivalence_config()
    weights = dict(base.builder_weights)
    assert builder_name in weights, builder_name
    weights[builder_name] = 200.0
    config = replace(base, builder_weights=weights)

    table = adapter.simple_catalog("t")
    rows = sample_rows(table, 8, seed=3)
    queries = list(RandomSelectSource().iter_queries(table, seed=5, limit=4))
    for seed in range(8):
        generator = EquivalenceGenerator(
            config,
            predicate_source=RandomPredicateSource(),
            emitter=adapter.emitter(),
            extra_builders=adapter.extra_builders(),
        )
        outcome = run_round(adapter, generator, table, rows, queries, seed=seed)
        assert outcome.setup_error is None, f"{builder_name} seed={seed}: {outcome.setup_error}"
        assert outcome.object_comparison is not None
        assert outcome.object_comparison.equal, f"{builder_name} seed={seed} lost rows"
        assert not outcome.findings, [f.query for f in outcome.findings]


def test_any_value_is_written_as_max_and_sqlite_accepts_it() -> None:
    """SQLite has no ``ANY_VALUE``, but the semantics of the builder that emits it do hold here.

    The key-channel reducers group *copies of one base row*, so every row in a group carries the same
    value and ``MAX`` returns exactly what ``ANY_VALUE`` would. Spelling it in the emitter is what
    keeps the builder enabled; before this, the four ``Key*Reduce`` builders were enabled while both
    their expanders sat at weight 0, so they declined every draw and the mis-spelling never surfaced.
    """
    from eqgen.core.catalog import Column, Table
    from eqgen.dialects.sqlite.emitter import SqliteEmitter
    from eqgen.equivalence.ast import BaseTableSource, CreateView, ProjectionItem, SelectQuery
    from eqgen.equivalence.context import NameGenerator, ObjectNamer
    from eqgen.equivalence.emitter import emit_equivalence
    from eqgen.ir import expr

    column = Column("c_txt", TextType(), 1)
    source = BaseTableSource(Table("t__base", [column]))
    items = [ProjectionItem("c_txt", expr.any_value(expr.col("c_txt", TextType()), TextType()), TextType())]
    view = CreateView.build(ObjectNamer("t", NameGenerator()), SelectQuery(source, items))
    sql = emit_equivalence(view, SqliteEmitter())[-1].statement_text

    assert "MAX(" in sql, sql
    assert "ANY_VALUE" not in sql, sql

    # And the engine agrees: ANY_VALUE is genuinely absent, MAX is genuinely there.
    connection = sqlite3.connect(":memory:")
    try:
        with pytest.raises(sqlite3.OperationalError, match="no such function: ANY_VALUE"):
            connection.execute("SELECT ANY_VALUE(1)")
        assert connection.execute("SELECT MAX(x) FROM (SELECT 'a' x UNION ALL SELECT 'a')").fetchone() == ("a",)
    finally:
        connection.close()
