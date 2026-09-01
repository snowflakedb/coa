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

"""The PostgreSQL dialect, against a real server.

Skipped when no server is available, because these tests are about behaviour no mock can stand in
for: whether a failed statement poisons the session, and whether two connections can see each
other's objects. Build one with ``dbfuzz``'s ``build_postgres_main.sh`` or point ``EQGEN_PG_BINDIR``
at an existing ``bin`` directory.
"""

from __future__ import annotations

import pytest

from eqgen.core.catalog import Table
from eqgen.core.types import (
    BooleanType,
    DoubleType,
    Int4RangeType,
    IntegerType,
    JsonbType,
    NumericType,
    TextType,
    UuidType,
    VarcharType,
)
from eqgen.equivalence.config import default_equivalence_config
from eqgen.equivalence.generator import EquivalenceGenerator
from eqgen.generators.example_generator import RandomPredicateSource, RandomSelectSource
from eqgen.fuzz.compare import compare_objects
from eqgen.fuzz.database import Database, column_names, hidden_base_name
from eqgen.fuzz.journal import sample_rows
from eqgen.fuzz.round import run_round

pytestmark = pytest.mark.unit


def test_postgres_known_issue_labels_domain_errors() -> None:
    """Plan-dependent math domain errors are generator noise, not engine bugs."""
    from eqgen.dialects.postgres.adapter import PostgresAdapter

    adapter = PostgresAdapter.__new__(PostgresAdapter)
    assert (
        adapter.known_issue_label(Exception("cannot take square root of a negative number"))
        == "postgres-math-domain-error"
    )
    assert adapter.known_issue_label(Exception("cannot take logarithm of zero")) == "postgres-math-domain-error"
    assert adapter.known_issue_label(Exception("input is out of range")) == "postgres-math-domain-error"
    assert adapter.known_issue_label(Exception("division by zero")) == "postgres-division-by-zero"
    assert adapter.known_issue_label(Exception("character number must be positive")) == "postgres-chr-domain-error"
    assert adapter.known_issue_label(Exception("integer out of range")) == "postgres-integer-out-of-range"
    assert (
        adapter.known_issue_label(Exception('invalid input syntax for type integer: "o\'brien"'))
        == "postgres-invalid-numeric-cast"
    )
    assert adapter.known_issue_label(Exception("canceling statement due to statement timeout")) == (
        "postgres-statement-timeout"
    )
    assert adapter.known_issue_label(Exception("field position must not be zero")) == (
        "postgres-field-position-zero"
    )
    assert adapter.known_issue_label(Exception("negative substring length not allowed")) == (
        "postgres-negative-substring-length"
    )
    assert adapter.known_issue_label(
        Exception("FULL JOIN is only supported with merge-joinable or hash-joinable join conditions")
    ) == "postgres-full-join-not-equijoin"
    assert adapter.known_issue_label(
        Exception("string buffer exceeds maximum allowed length (1073741823 bytes)")
    ) == "postgres-string-buffer-exceeded"
    assert adapter.known_issue_label(Exception("syntax error at or near SELECT")) is None


def test_sample_rows_plants_inf_for_postgres() -> None:
    """Postgres supports IEEE Inf, so sample_rows always plants ±Inf in the first DOUBLE."""
    from eqgen.dialects.postgres.adapter import rich_catalog
    from eqgen.fuzz.journal import sample_rows

    table = rich_catalog("t")
    rows = sample_rows(table, 8, seed=99, allow_inf=True)
    cols = [c.get_column_name() for c in table.get_column_list()]
    di = cols.index("c_dbl")
    vals = [r[di] for r in rows]
    assert float("inf") in vals
    assert float("-inf") in vals


def test_sample_rows_skips_inf_by_default() -> None:
    """Without allow_inf (MySQL-family default), DOUBLE seeds stay finite."""
    from eqgen.dialects.postgres.adapter import rich_catalog
    from eqgen.fuzz.journal import sample_rows

    table = rich_catalog("t")
    rows = sample_rows(table, 8, seed=99, allow_inf=False)
    cols = [c.get_column_name() for c in table.get_column_list()]
    di = cols.index("c_dbl")
    assert all(v is None or (isinstance(v, float) and v == v and abs(v) != float("inf")) for v in (r[di] for r in rows))


def _adapter() -> object:
    pytest.importorskip("psycopg")
    from eqgen.dialects.postgres.cluster import pg_bindir

    try:
        pg_bindir()
    except RuntimeError as exc:
        pytest.skip(str(exc))
    from eqgen.dialects.postgres.adapter import PostgresAdapter

    return PostgresAdapter()


# ---------------------------------------------------------------------------
# Type names and the catalogs — no server needed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data_type", "expected"),
    [
        (IntegerType(), "INTEGER"),
        (NumericType(10, 2), "NUMERIC(10, 2)"),
        (NumericType(38, 0), "NUMERIC(38, 0)"),
        (DoubleType(), "DOUBLE PRECISION"),
        (VarcharType(), "VARCHAR"),
        (TextType(), "TEXT"),
        (BooleanType(), "BOOLEAN"),
        (JsonbType(), "JSONB"),
        (UuidType(), "UUID"),
        (Int4RangeType(), "INT4RANGE"),
    ],
)
def test_column_types_come_from_the_portable_spelling(data_type: object, expected: str) -> None:
    """This dialect writes no type names of its own. A column declared one way and cast another is
    a bug this project has already had to pin, so both go through ``PostgresSpelling``."""
    pytest.importorskip("psycopg")
    from eqgen.dialects.postgres.adapter import postgres_type

    assert postgres_type(data_type) == expected  # type: ignore[arg-type]


def test_postgres_index_builders_emit_create_index_then_view() -> None:
    """Plan-only κ: table body, index, ANALYZE, exposing view — rows unchanged, access path different."""
    from eqgen.builder.constraint_set import ConstraintSet
    from eqgen.core.catalog import Column, Table
    from eqgen.dialects.postgres.builders import (
        PostgresBrinIndexBuilder,
        PostgresBtreeIndexBuilder,
        PostgresCoveringIndexBuilder,
        PostgresExpressionIndexBuilder,
        PostgresHashIndexBuilder,
        PostgresPartialCoveringIndexBuilder,
        PostgresPartialIndexBuilder,
    )
    from eqgen.dialects.postgres.emitter import PostgresEmitter
    from eqgen.equivalence.ast import BaseTableSource, SelectQuery
    from eqgen.equivalence.context import EquivalenceContext, NameGenerator
    from eqgen.equivalence.emitter import emit_equivalence
    from eqgen.equivalence.factory import EquivalenceBuilderFactory

    table = Table(
        "t",
        [
            Column("c_pk", IntegerType(), 1, nullable=False),
            Column("c_int", IntegerType(), 2),
            Column("c_txt", TextType(), 3),
        ],
    )
    factory = EquivalenceBuilderFactory()
    context = EquivalenceContext(
        default_equivalence_config(),
        base_table=table,
        predicate_source=None,
        name_generator=NameGenerator(),
    )
    original = factory.build_subtree

    def force_select_star(result_type: object, constraints: object, ctx: object) -> object:
        from eqgen.equivalence.ast import QueryNode

        if result_type is QueryNode:
            return SelectQuery(BaseTableSource(table), None)
        return original(result_type, constraints, ctx)  # type: ignore[arg-type]

    factory.build_subtree = force_select_star  # type: ignore[method-assign]
    cases: list[tuple[type, str]] = [
        (PostgresBtreeIndexBuilder, "USING btree (c_pk)"),
        (PostgresHashIndexBuilder, "USING hash (c_pk)"),
        (PostgresBrinIndexBuilder, "USING brin (c_pk)"),
        (PostgresPartialIndexBuilder, "WHERE c_txt IS NOT NULL"),
        (PostgresExpressionIndexBuilder, "(lower(c_txt))"),
        (PostgresCoveringIndexBuilder, "INCLUDE (c_int, c_txt)"),
        (
            PostgresPartialCoveringIndexBuilder,
            "INCLUDE (c_int, c_txt) WHERE c_txt IS NOT NULL",
        ),
    ]
    for builder_cls, needle in cases:
        node = builder_cls(factory)._build(ConstraintSet([]), context)
        assert node is not None, builder_cls.__name__
        statements = [s.statement_text for s in emit_equivalence(node, PostgresEmitter())]
        assert statements[0].startswith("CREATE TABLE"), statements
        assert any(needle in s and s.startswith("CREATE INDEX") for s in statements), (needle, statements)
        assert any(s.startswith("ANALYZE ") for s in statements), statements
        assert statements[-1].startswith("CREATE VIEW"), statements


def test_postgres_merge_upsert_builder_emits_noop_self_merge() -> None:
    """Every row's ``c_pk`` exists in the copy too, so only ``WHEN MATCHED`` can ever fire."""
    from eqgen.builder.constraint_set import ConstraintSet
    from eqgen.core.catalog import Column, Table
    from eqgen.dialects.postgres.builders import PostgresMergeUpsertBuilder
    from eqgen.dialects.postgres.emitter import PostgresEmitter
    from eqgen.equivalence.ast import BaseTableSource, SelectQuery
    from eqgen.equivalence.context import EquivalenceContext, NameGenerator
    from eqgen.equivalence.emitter import emit_equivalence
    from eqgen.equivalence.factory import EquivalenceBuilderFactory

    table = Table(
        "t",
        [
            Column("c_pk", IntegerType(), 1, nullable=False),
            Column("c_int", IntegerType(), 2),
            Column("c_txt", TextType(), 3),
        ],
    )
    factory = EquivalenceBuilderFactory()
    context = EquivalenceContext(
        default_equivalence_config(),
        base_table=table,
        predicate_source=None,
        name_generator=NameGenerator(),
    )
    original = factory.build_subtree

    def force_select_star(result_type: object, constraints: object, ctx: object) -> object:
        from eqgen.equivalence.ast import QueryNode

        if result_type is QueryNode:
            return SelectQuery(BaseTableSource(table), None)
        return original(result_type, constraints, ctx)  # type: ignore[arg-type]

    factory.build_subtree = force_select_star  # type: ignore[method-assign]

    node = PostgresMergeUpsertBuilder(factory)._build(ConstraintSet([]), context)
    assert node is not None
    statements = [s.statement_text for s in emit_equivalence(node, PostgresEmitter())]
    assert statements[0].startswith("CREATE TABLE"), statements
    assert any(s.startswith("CREATE TABLE") and " AS SELECT * FROM " in s for s in statements[1:]), statements
    merge = next((s for s in statements if s.startswith("MERGE INTO")), None)
    assert merge is not None, statements
    assert "WHEN MATCHED THEN UPDATE SET" in merge, merge
    # DO NOTHING, not INSERT: the ON condition is UNKNOWN for a NULL pk, so an INSERT branch
    # would add a duplicate row instead of updating. Every src row came from body.
    assert "WHEN NOT MATCHED THEN DO NOTHING" in merge, merge
    assert "INSERT" not in merge, merge
    assert "ON tgt.c_pk = src.c_pk" in merge, merge
    assert statements[-1].startswith("CREATE VIEW"), statements


def test_postgres_generated_column_builder_exposes_the_generated_twin() -> None:
    """The view must expose the generated column under the shadowed column's own name."""
    from eqgen.builder.constraint_set import ConstraintSet
    from eqgen.core.catalog import Column, Table
    from eqgen.dialects.postgres.builders import PostgresGeneratedColumnBuilder
    from eqgen.dialects.postgres.emitter import PostgresEmitter
    from eqgen.equivalence.ast import BaseTableSource, SelectQuery
    from eqgen.equivalence.context import EquivalenceContext, NameGenerator
    from eqgen.equivalence.emitter import emit_equivalence
    from eqgen.equivalence.factory import EquivalenceBuilderFactory

    table = Table(
        "t",
        [
            Column("c_pk", IntegerType(), 1, nullable=False),
            Column("c_int", IntegerType(), 2),
            Column("c_txt", TextType(), 3),
        ],
    )
    factory = EquivalenceBuilderFactory()
    context = EquivalenceContext(
        default_equivalence_config(),
        base_table=table,
        predicate_source=None,
        name_generator=NameGenerator(),
    )
    original = factory.build_subtree

    def force_select_star(result_type: object, constraints: object, ctx: object) -> object:
        from eqgen.equivalence.ast import QueryNode

        if result_type is QueryNode:
            return SelectQuery(BaseTableSource(table), None)
        return original(result_type, constraints, ctx)  # type: ignore[arg-type]

    factory.build_subtree = force_select_star  # type: ignore[method-assign]

    node = PostgresGeneratedColumnBuilder(factory)._build(ConstraintSet([]), context)
    assert node is not None
    statements = [s.statement_text for s in emit_equivalence(node, PostgresEmitter())]
    assert statements[0].startswith("CREATE TABLE"), statements
    alter = next((s for s in statements if s.startswith("ALTER TABLE")), None)
    assert alter is not None, statements
    # The expression is CAST to the declared type rather than relying on an implicit assignment
    # cast: the declared type comes from the IR signature, the body is a CTAS, and this dialect
    # does not cast projections, so the two can otherwise disagree.
    assert "GENERATED ALWAYS AS (CAST(c_int AS INTEGER)) STORED" in alter, alter
    view = statements[-1]
    assert view.startswith("CREATE VIEW"), statements
    assert "c_int_gen AS c_int" in view, view
    assert "c_pk" in view and "c_txt" in view, view


def test_postgres_legacy_inheritance_builder_exposes_a_view_over_the_parent() -> None:
    """The exposing view must query the empty parent, not the child, so inheritance is load-bearing."""
    from eqgen.builder.constraint_set import ConstraintSet
    from eqgen.core.catalog import Column, Table
    from eqgen.dialects.postgres.builders import PostgresLegacyInheritanceBuilder
    from eqgen.dialects.postgres.emitter import PostgresEmitter
    from eqgen.equivalence.ast import BaseTableSource, SelectQuery
    from eqgen.equivalence.context import EquivalenceContext, NameGenerator
    from eqgen.equivalence.emitter import emit_equivalence
    from eqgen.equivalence.factory import EquivalenceBuilderFactory

    table = Table(
        "t",
        [
            Column("c_pk", IntegerType(), 1, nullable=False),
            Column("c_int", IntegerType(), 2),
            Column("c_txt", TextType(), 3),
        ],
    )
    factory = EquivalenceBuilderFactory()
    context = EquivalenceContext(
        default_equivalence_config(),
        base_table=table,
        predicate_source=None,
        name_generator=NameGenerator(),
    )
    original = factory.build_subtree

    def force_select_star(result_type: object, constraints: object, ctx: object) -> object:
        from eqgen.equivalence.ast import QueryNode

        if result_type is QueryNode:
            return SelectQuery(BaseTableSource(table), None)
        return original(result_type, constraints, ctx)  # type: ignore[arg-type]

    factory.build_subtree = force_select_star  # type: ignore[method-assign]

    node = PostgresLegacyInheritanceBuilder(factory)._build(ConstraintSet([]), context)
    assert node is not None
    statements = [s.statement_text for s in emit_equivalence(node, PostgresEmitter())]
    assert statements[0].startswith("CREATE TABLE"), statements
    body_ref = statements[0].split()[2]
    like_stmt = next((s for s in statements if s.startswith("CREATE TABLE") and "LIKE" in s), None)
    assert like_stmt is not None, statements
    assert f"LIKE {body_ref}" in like_stmt, like_stmt
    parent_ref = like_stmt.split()[2]
    inherit = next((s for s in statements if s.startswith("ALTER TABLE")), None)
    assert inherit is not None, statements
    assert inherit == f"ALTER TABLE {body_ref} INHERIT {parent_ref}", inherit
    view = statements[-1]
    assert view.startswith("CREATE VIEW"), statements
    assert f"FROM {parent_ref}" in view, view


def test_postgres_domain_column_builder_retypes_one_column_via_domain() -> None:
    """The view exposes the domain-typed column under its own name; the domain has no CHECK."""
    from eqgen.builder.constraint_set import ConstraintSet
    from eqgen.core.catalog import Column, Table
    from eqgen.dialects.postgres.builders import PostgresDomainColumnBuilder
    from eqgen.dialects.postgres.emitter import PostgresEmitter
    from eqgen.equivalence.ast import BaseTableSource, SelectQuery
    from eqgen.equivalence.context import EquivalenceContext, NameGenerator
    from eqgen.equivalence.emitter import emit_equivalence
    from eqgen.equivalence.factory import EquivalenceBuilderFactory

    table = Table(
        "t",
        [
            Column("c_pk", IntegerType(), 1, nullable=False),
            Column("c_int", IntegerType(), 2),
            Column("c_txt", TextType(), 3),
        ],
    )
    factory = EquivalenceBuilderFactory()
    context = EquivalenceContext(
        default_equivalence_config(),
        base_table=table,
        predicate_source=None,
        name_generator=NameGenerator(),
    )
    original = factory.build_subtree

    def force_select_star(result_type: object, constraints: object, ctx: object) -> object:
        from eqgen.equivalence.ast import QueryNode

        if result_type is QueryNode:
            return SelectQuery(BaseTableSource(table), None)
        return original(result_type, constraints, ctx)  # type: ignore[arg-type]

    factory.build_subtree = force_select_star  # type: ignore[method-assign]

    node = PostgresDomainColumnBuilder(factory)._build(ConstraintSet([]), context)
    assert node is not None
    statements = [s.statement_text for s in emit_equivalence(node, PostgresEmitter())]
    assert statements[0].startswith("CREATE TABLE"), statements
    domain_stmt = next((s for s in statements if s.startswith("CREATE DOMAIN")), None)
    assert domain_stmt is not None, statements
    domain_name = domain_stmt.split()[2]
    assert "CHECK" not in domain_stmt, domain_stmt
    alter = next((s for s in statements if s.startswith("ALTER TABLE")), None)
    assert alter is not None, statements
    # USING spells the conversion to the domain's base type, for the same reason.
    assert f"ALTER COLUMN c_int TYPE {domain_name} USING CAST(c_int AS INTEGER)" in alter, alter
    view = statements[-1]
    assert view.startswith("CREATE VIEW"), statements
    assert "c_int" in view and "c_pk" in view and "c_txt" in view, view


def test_postgres_surface_mats_emit_gin_gist_partition_parallel() -> None:
    """GIN/GiST/partition/parallel Mats emit the planner-path SQL without changing the view."""
    from eqgen.builder.constraint_set import ConstraintSet
    from eqgen.core.catalog import Column, Table
    from eqgen.dialects.postgres.builders import (
        PostgresGinJsonbIndexBuilder,
        PostgresGistRangeIndexBuilder,
        PostgresParallelToggleMatBuilder,
        PostgresPartitionedTableMatBuilder,
    )
    from eqgen.dialects.postgres.emitter import PostgresEmitter
    from eqgen.equivalence.ast import BaseTableSource, SelectQuery
    from eqgen.equivalence.context import EquivalenceContext, NameGenerator
    from eqgen.equivalence.emitter import emit_equivalence
    from eqgen.equivalence.factory import EquivalenceBuilderFactory

    table = Table(
        "t",
        [
            Column("c_pk", IntegerType(), 1, nullable=False),
            Column("c_json", JsonbType(), 2),
            Column("c_range", Int4RangeType(), 3),
            Column("c_txt", TextType(), 4),
        ],
    )
    factory = EquivalenceBuilderFactory()
    context = EquivalenceContext(
        default_equivalence_config(),
        base_table=table,
        predicate_source=None,
        name_generator=NameGenerator(),
    )
    original = factory.build_subtree

    def force_select_star(result_type: object, constraints: object, ctx: object) -> object:
        from eqgen.equivalence.ast import QueryNode

        if result_type is QueryNode:
            return SelectQuery(BaseTableSource(table), None)
        return original(result_type, constraints, ctx)  # type: ignore[arg-type]

    factory.build_subtree = force_select_star  # type: ignore[method-assign]

    gin = PostgresGinJsonbIndexBuilder(factory)._build(ConstraintSet([]), context)
    assert gin is not None
    gin_sql = [s.statement_text for s in emit_equivalence(gin, PostgresEmitter())]
    assert any("USING gin (c_json)" in s for s in gin_sql), gin_sql

    gist = PostgresGistRangeIndexBuilder(factory)._build(ConstraintSet([]), context)
    assert gist is not None
    gist_sql = [s.statement_text for s in emit_equivalence(gist, PostgresEmitter())]
    assert any("USING gist (c_range)" in s for s in gist_sql), gist_sql

    part = PostgresPartitionedTableMatBuilder(factory)._build(ConstraintSet([]), context)
    assert part is not None
    part_sql = [s.statement_text for s in emit_equivalence(part, PostgresEmitter())]
    assert any("PARTITION BY RANGE (c_pk)" in s for s in part_sql), part_sql
    assert any("PARTITION OF" in s for s in part_sql), part_sql
    assert any(s.startswith("INSERT INTO") for s in part_sql), part_sql

    par = PostgresParallelToggleMatBuilder(factory)._build(ConstraintSet([]), context)
    assert par is not None
    par_sql = [s.statement_text for s in emit_equivalence(par, PostgresEmitter())]
    # The pinned name, not "either spelling": a server accepts only its own, so accepting both
    # means this test passes against a server the statement would fail on.
    assert any("SET debug_parallel_query = on" in s for s in par_sql), par_sql
    assert par_sql[-1].startswith("CREATE VIEW"), par_sql
    assert any("LIKE " in s and "PARTITION BY RANGE (c_pk)" in s for s in part_sql), part_sql


def test_covering_index_keys_on_c_pk_even_when_not_first_column() -> None:
    """Projection order must not demote the unique key off the index."""
    from eqgen.builder.constraint_set import ConstraintSet
    from eqgen.core.catalog import Column, Table
    from eqgen.dialects.postgres.builders import PostgresCoveringIndexBuilder
    from eqgen.dialects.postgres.emitter import PostgresEmitter
    from eqgen.equivalence.ast import BaseTableSource, SelectQuery
    from eqgen.equivalence.context import EquivalenceContext, NameGenerator
    from eqgen.equivalence.emitter import emit_equivalence
    from eqgen.equivalence.factory import EquivalenceBuilderFactory

    table = Table(
        "t",
        [
            Column("c_int", IntegerType(), 1),
            Column("c_pk", IntegerType(), 2, nullable=False),
            Column("c_txt", TextType(), 3),
        ],
    )
    factory = EquivalenceBuilderFactory()
    context = EquivalenceContext(
        default_equivalence_config(),
        base_table=table,
        predicate_source=None,
        name_generator=NameGenerator(),
    )
    original = factory.build_subtree

    def force_select_star(result_type: object, constraints: object, ctx: object) -> object:
        from eqgen.equivalence.ast import QueryNode

        if result_type is QueryNode:
            return SelectQuery(BaseTableSource(table), None)
        return original(result_type, constraints, ctx)  # type: ignore[arg-type]

    factory.build_subtree = force_select_star  # type: ignore[method-assign]
    node = PostgresCoveringIndexBuilder(factory)._build(ConstraintSet([]), context)
    assert node is not None
    statements = [s.statement_text for s in emit_equivalence(node, PostgresEmitter())]
    index_sql = next(s for s in statements if s.startswith("CREATE INDEX"))
    assert "USING btree (c_pk) INCLUDE (c_int, c_txt)" in index_sql, index_sql


def test_postgres_distinct_on_and_stats_and_security_barrier_emit() -> None:
    """DISTINCT ON / extended stats / security_barrier — PG-only plan or rewrite payloads."""
    from eqgen.builder.constraint_set import ConstraintSet
    from eqgen.core.catalog import Column, Table
    from eqgen.dialects.postgres.builders import (
        PostgresDistinctOnQueryBuilder,
        PostgresExtendedStatisticsBuilder,
        PostgresSecurityBarrierViewBuilder,
    )
    from eqgen.dialects.postgres.emitter import PostgresEmitter
    from eqgen.equivalence.ast import BaseTableSource, SelectQuery
    from eqgen.equivalence.context import EquivalenceContext, NameGenerator
    from eqgen.equivalence.emitter import emit_equivalence
    from eqgen.equivalence.factory import EquivalenceBuilderFactory

    table = Table(
        "t",
        [
            Column("c_pk", IntegerType(), 1, nullable=False),
            Column("c_int", IntegerType(), 2),
            Column("c_txt", TextType(), 3),
        ],
    )
    factory = EquivalenceBuilderFactory()
    context = EquivalenceContext(
        default_equivalence_config(),
        base_table=table,
        predicate_source=None,
        name_generator=NameGenerator(),
    )
    original = factory.build_subtree

    def force_base(result_type: object, constraints: object, ctx: object) -> object:
        from eqgen.equivalence.ast import EquivalentSource, QueryNode

        if result_type is QueryNode:
            return SelectQuery(BaseTableSource(table), None)
        if result_type is EquivalentSource:
            return BaseTableSource(table)
        return original(result_type, constraints, ctx)  # type: ignore[arg-type]

    factory.build_subtree = force_base  # type: ignore[method-assign]

    don = PostgresDistinctOnQueryBuilder(factory)._build(ConstraintSet([]), context)
    assert don is not None
    don_sql = PostgresEmitter()._render_query(don)  # type: ignore[attr-defined]
    assert "DISTINCT ON (c_pk)" in don_sql and "ORDER BY c_pk" in don_sql

    barrier = PostgresSecurityBarrierViewBuilder(factory)._build(ConstraintSet([]), context)
    assert barrier is not None
    barrier_stmts = [s.statement_text for s in emit_equivalence(barrier, PostgresEmitter())]
    assert any("security_barrier = true" in s for s in barrier_stmts), barrier_stmts

    stats = PostgresExtendedStatisticsBuilder(factory)._build(ConstraintSet([]), context)
    assert stats is not None
    stats_stmts = [s.statement_text for s in emit_equivalence(stats, PostgresEmitter())]
    assert any(s.startswith("CREATE STATISTICS") for s in stats_stmts), stats_stmts
    assert any(s.startswith("ANALYZE ") for s in stats_stmts), stats_stmts
    assert stats_stmts[-1].startswith("CREATE VIEW"), stats_stmts


def test_materialized_view_reads_a_permanent_table_not_a_temp() -> None:
    """PostgreSQL refuses ``CREATE MATERIALIZED VIEW … FROM <temporary>``.

    The builder CTAS's the defining query into a permanent table first, so a temporary view can
    still appear *under* the CTAS (rows are copied) while the matview itself only names the table.
    """
    from eqgen.core.catalog import Column, Table
    from eqgen.dialects.postgres.emitter import PostgresEmitter
    from eqgen.equivalence.ast import BaseTableSource, CreateTemporaryView, SelectQuery
    from eqgen.equivalence.builders.creates import CreateMaterializedViewBuilder
    from eqgen.equivalence.context import EquivalenceContext, NameGenerator
    from eqgen.equivalence.emitter import emit_equivalence
    from eqgen.equivalence.factory import EquivalenceBuilderFactory

    table = Table("t", [Column("c_int", IntegerType(), 1), Column("c_txt", TextType(), 2)])
    factory = EquivalenceBuilderFactory()
    context = EquivalenceContext(
        default_equivalence_config(),
        base_table=table,
        predicate_source=None,
        name_generator=NameGenerator(),
    )
    temp = CreateTemporaryView.build(context.namer, SelectQuery(BaseTableSource(table), None))
    node = CreateMaterializedViewBuilder(factory)._wrap(SelectQuery(temp, None), context, exposed_name="t")
    statements = [s.statement_text for s in emit_equivalence(node, PostgresEmitter())]
    assert any(s.startswith("CREATE TEMPORARY VIEW") for s in statements), statements
    assert any(s.startswith("CREATE TABLE") for s in statements), statements
    matview = statements[-1]
    assert matview.startswith("CREATE MATERIALIZED VIEW t AS SELECT * FROM")
    assert "TEMPORARY" not in matview
    # The matview's FROM target is the permanent table, not the temp view.
    assert "_table_" in matview or matview.endswith("FROM t_table_1") or "FROM t_" in matview
    assert not matview.rstrip(";").endswith("view_1")


def test_postgres_array_pack_casts_extract_back_to_the_declared_type() -> None:
    """ARRAY[numeric(p,s)][1] is unconstrained NUMERIC; the CAST is what keeps the type gate green."""
    from eqgen.dialects.postgres.builders import PostgresArrayPackRoundTripBuilder
    from eqgen.ir.render import render

    builder = PostgresArrayPackRoundTripBuilder.__new__(PostgresArrayPackRoundTripBuilder)
    rewrite = builder._column_rewriter(None)  # type: ignore[arg-type]
    node = rewrite("c_dec", NumericType(10, 2))
    assert node is not None
    assert render(node) == "CAST((ARRAY[c_dec])[1] AS NUMERIC(10, 2))"
    int_node = rewrite("c_pk", IntegerType())
    assert int_node is not None
    assert render(int_node) == "CAST((ARRAY[c_pk])[1] AS INTEGER)"


def test_postgres_whole_row_json_pack_emits_jsonb_object_and_unpacks() -> None:
    """Snowflake v3 whole-row OBJECT_CONSTRUCT, spelled with jsonb_build_object / ->>."""
    from eqgen.builder.constraint_set import ConstraintSet
    from eqgen.core.catalog import Column, Table
    from eqgen.dialects.postgres.builders import PostgresWholeRowJsonPackBuilder
    from eqgen.dialects.postgres.emitter import PostgresEmitter
    from eqgen.equivalence.ast import BaseTableSource, CreateView, SelectQuery
    from eqgen.equivalence.context import EquivalenceContext, NameGenerator
    from eqgen.equivalence.emitter import emit_equivalence
    from eqgen.equivalence.factory import EquivalenceBuilderFactory

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
    original = factory.build_subtree

    def force_select_star(result_type: object, constraints: object, ctx: object) -> object:
        from eqgen.equivalence.ast import QueryNode

        if result_type is QueryNode:
            return SelectQuery(BaseTableSource(table), None)
        return original(result_type, constraints, ctx)  # type: ignore[arg-type]

    factory.build_subtree = force_select_star  # type: ignore[method-assign]
    node = PostgresWholeRowJsonPackBuilder(factory)._build(ConstraintSet([]), context)
    assert node is not None
    wrapped = CreateView.build(context.namer, node)
    sql = "\n".join(s.statement_text for s in emit_equivalence(wrapped, PostgresEmitter()))
    assert "jsonb_build_object('c_pk', c_pk, 'c_txt', c_txt)" in sql
    assert "->> 'c_pk'" in sql and "->> 'c_txt'" in sql
    assert "CAST(" in sql and "AS INTEGER" in sql and "AS TEXT" in sql


def test_postgres_whole_row_json_pack_declines_double() -> None:
    """jsonb rejects Inf/NaN; the PG catalog plants both, so DOUBLE is not JSON-native here."""
    from eqgen.builder.constraint_set import ConstraintSet
    from eqgen.core.catalog import Column, Table
    from eqgen.dialects.postgres.builders import PostgresWholeRowJsonPackBuilder
    from eqgen.equivalence.context import EquivalenceContext, NameGenerator
    from eqgen.equivalence.factory import EquivalenceBuilderFactory

    table = Table("t", [Column("c_pk", IntegerType(), 1), Column("c_dbl", DoubleType(), 2)])
    context = EquivalenceContext(
        default_equivalence_config(),
        base_table=table,
        predicate_source=None,
        name_generator=NameGenerator(),
    )
    node = PostgresWholeRowJsonPackBuilder(EquivalenceBuilderFactory())._build(ConstraintSet([]), context)
    assert node is None


def test_the_catalogs_match_the_duckdb_ones() -> None:
    """Portable columns match DuckDB; Postgres appends jsonb/uuid/range for new Mats."""
    pytest.importorskip("psycopg")
    pytest.importorskip("duckdb")
    from eqgen.dialects.duckdb.adapter import rich_catalog as duckdb_rich
    from eqgen.dialects.postgres.adapter import rich_catalog as postgres_rich

    def shape(table: Table) -> list[tuple[str, str]]:
        return [(c.get_column_name(), repr(c.get_data_type())) for c in table.get_column_list()]

    duck = shape(duckdb_rich("t"))
    pg = shape(postgres_rich("t"))
    assert pg[: len(duck)] == duck
    assert [n for n, _ in pg[len(duck) :]] == ["c_json", "c_uuid", "c_range"]


def test_the_dialect_declares_its_builder_set_in_gcl() -> None:
    """Portable builders plus the dialect's own extras — stated as data in ``postgres.gcl``."""
    pytest.importorskip("psycopg")
    from eqgen.dialects.postgres.adapter import postgres_equivalence_config
    from eqgen.dialects.postgres.builders import (
        PostgresArrayPackRoundTripBuilder,
        PostgresWholeRowJsonPackBuilder,
        PostgresBrinIndexBuilder,
        PostgresBtreeIndexBuilder,
        PostgresCoveringIndexBuilder,
        PostgresDistinctOnQueryBuilder,
        PostgresExpressionIndexBuilder,
        PostgresExtendedStatisticsBuilder,
        PostgresGinJsonbIndexBuilder,
        PostgresGistRangeIndexBuilder,
        PostgresDomainColumnBuilder,
        PostgresGeneratedColumnBuilder,
        PostgresHashIndexBuilder,
        PostgresLegacyInheritanceBuilder,
        PostgresMergeUpsertBuilder,
        PostgresParallelToggleMatBuilder,
        PostgresPartialCoveringIndexBuilder,
        PostgresPartialIndexBuilder,
        PostgresPartitionedTableMatBuilder,
        PostgresPrimaryKeyMatBuilder,
        PostgresSecurityBarrierViewBuilder,
        PostgresUnloggedTableBuilder,
    )

    portable = set(default_equivalence_config().builder_weights)
    postgres = set(postgres_equivalence_config().builder_weights)
    extras = {
        PostgresDistinctOnQueryBuilder.__name__,
        PostgresArrayPackRoundTripBuilder.__name__,
        PostgresWholeRowJsonPackBuilder.__name__,
        PostgresBtreeIndexBuilder.__name__,
        PostgresHashIndexBuilder.__name__,
        PostgresBrinIndexBuilder.__name__,
        PostgresPartialIndexBuilder.__name__,
        PostgresExpressionIndexBuilder.__name__,
        PostgresCoveringIndexBuilder.__name__,
        PostgresPartialCoveringIndexBuilder.__name__,
        PostgresGinJsonbIndexBuilder.__name__,
        PostgresGistRangeIndexBuilder.__name__,
        PostgresPartitionedTableMatBuilder.__name__,
        PostgresParallelToggleMatBuilder.__name__,
        PostgresPrimaryKeyMatBuilder.__name__,
        PostgresMergeUpsertBuilder.__name__,
        PostgresGeneratedColumnBuilder.__name__,
        PostgresLegacyInheritanceBuilder.__name__,
        PostgresDomainColumnBuilder.__name__,
        PostgresUnloggedTableBuilder.__name__,
        PostgresSecurityBarrierViewBuilder.__name__,
        PostgresExtendedStatisticsBuilder.__name__,
    }
    assert portable <= postgres, portable - postgres
    assert postgres - portable == extras, (postgres - portable) ^ extras


# ---------------------------------------------------------------------------
# Against a live server
# ---------------------------------------------------------------------------


def test_a_failed_statement_does_not_poison_the_session() -> None:
    """The single most consequential line in the adapter is ``autocommit=True``.

    Without it psycopg leaves the connection in an aborted transaction, so every query after the
    first invalid one fails with "current transaction is aborted" — and the oracle reports each of
    them as a one-sided error, i.e. a round full of findings that are not findings.
    """
    adapter = _adapter()
    connection = adapter.connect()  # type: ignore[attr-defined]
    try:
        connection.execute("CREATE TABLE t (a INTEGER)")
        connection.execute("INSERT INTO t VALUES (1)")
        with pytest.raises(Exception):  # - any driver error will do
            connection.execute("SELECT this_function_does_not_exist(a) FROM t")
        assert connection.execute("SELECT a FROM t").fetchall() == [(1,)]
    finally:
        connection.close()


def test_two_connections_cannot_see_each_other() -> None:
    """One server, but the harness needs two databases. Each connection owns a schema and points
    ``search_path`` at it, so the base table and the equivalent can both be called ``t``."""
    adapter = _adapter()
    one, two = adapter.connect(), adapter.connect()  # type: ignore[attr-defined]
    try:
        one.execute("CREATE TABLE t (a INTEGER)")
        one.execute("INSERT INTO t VALUES (1)")
        two.execute("CREATE TABLE t (a INTEGER)")  # same name, and no conflict
        two.execute("INSERT INTO t VALUES (2)")
        assert one.execute("SELECT a FROM t").fetchall() == [(1,)]
        assert two.execute("SELECT a FROM t").fetchall() == [(2,)]
    finally:
        one.close()
        two.close()


def test_a_dropped_schema_leaves_nothing_behind() -> None:
    """``close()`` drops the schema, or a long run accumulates one per round until the server
    complains."""
    adapter = _adapter()
    connection = adapter.connect()  # type: ignore[attr-defined]
    connection.execute("CREATE TABLE t (a INTEGER)")
    schema = connection._schema
    connection.close()

    checker = adapter.connect()  # type: ignore[attr-defined]
    try:
        found = checker.execute(f"SELECT 1 FROM information_schema.schemata WHERE schema_name = '{schema}'").fetchall()
        assert found == []
    finally:
        checker.close()


def test_generated_objects_hold_the_same_rows_on_postgres() -> None:
    """The claim the whole project rests on, checked against PostgreSQL rather than argued.

    Also establishes something incidental and useful: the default emitter's output runs on
    PostgreSQL unmodified, which is what makes it the reference the other engines override.
    """
    adapter = _adapter()
    table = adapter.rich_catalog("t")  # type: ignore[attr-defined]
    rows = sample_rows(table, 8, seed=3)
    hidden = Table(hidden_base_name(table), table.get_column_list())
    generator = EquivalenceGenerator(
        adapter.equivalence_config(),  # type: ignore[attr-defined]
        predicate_source=RandomPredicateSource(),
        emitter=adapter.emitter(),  # type: ignore[attr-defined]
    )
    for seed in range(6):
        statements = [s.statement_text for s in generator.generate(hidden, seed=seed, exposed_name="t").setup_statements]
        base = Database.build_base(adapter, table, rows)  # type: ignore[arg-type]
        equivalent = Database.build_equivalent(adapter, table, rows, statements=statements)  # type: ignore[arg-type]
        try:
            comparison = compare_objects(base, equivalent, table, column_names(table))
            assert comparison.equal, f"seed {seed}: {comparison.verdict}"
        finally:
            base.close()
            equivalent.close()


def test_a_round_runs_end_to_end_on_postgres() -> None:
    """The whole loop on the real engine: build both sides, run the queries, compare."""
    adapter = _adapter()
    table = adapter.rich_catalog("t")  # type: ignore[attr-defined]
    rows = sample_rows(table, 8, seed=3)
    generator = EquivalenceGenerator(
        adapter.equivalence_config(),  # type: ignore[attr-defined]
        predicate_source=RandomPredicateSource(),
        emitter=adapter.emitter(),  # type: ignore[attr-defined]
    )
    queries = list(RandomSelectSource().iter_queries(table, seed=5, limit=8))
    outcome = run_round(adapter, generator, table, rows, queries, seed=11)  # type: ignore[arg-type]
    assert outcome.setup_error is None
    assert outcome.object_comparison is not None and outcome.object_comparison.equal
    assert not outcome.findings, [f.query for f in outcome.findings]


def test_the_catalogs_avoid_varchar_because_postgres_promotes_it() -> None:
    """``VARCHAR`` must not appear in a catalog, and this is the one place that says why.

    On PostgreSQL any function applied to a ``varchar`` returns ``text`` — ``UPPER``, ``||`` and
    ``MIN`` all do. So a rewrite that touches the column changes its declared type, the object stops
    being equivalent, and the round is discarded with no engine bug involved. It was the window
    rewrite that found this. DuckDB cannot even distinguish the two, so nothing is lost by carrying
    only the stable one.

    ``NumericType`` and ``DoubleType`` are deliberately *both* kept: measured on both engines,
    neither collapses into the other.
    """
    pytest.importorskip("psycopg")
    from eqgen.core.types import DoubleType, NumericType, VarcharType
    from eqgen.dialects.postgres.adapter import rich_catalog, simple_catalog

    for table in (simple_catalog("t"), rich_catalog("t")):
        for column in table.get_column_list():
            data_type = column.get_data_type()
            assert type(data_type) is not VarcharType, f"{column.get_column_name()} is a bare VARCHAR"

    kinds = {type(c.get_data_type()) for c in rich_catalog("t").get_column_list()}
    assert NumericType in kinds and DoubleType in kinds, "both numeric families should be covered"
