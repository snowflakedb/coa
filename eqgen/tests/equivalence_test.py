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

"""The equivalence core: structure, determinism, and — the point — row equivalence.

The last group is what this project is for. It generates equivalences, executes their DDL against
a real engine, and checks that the object really does hold the same rows as the base table.
Everything else here is scaffolding around that claim.

Note what the row-equivalence test also establishes incidentally: the base emitter's
PostgreSQL-spelled output runs **unmodified** on DuckDB. The portable emitter is portable in fact,
not just in intention.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import pytest

from eqgen.builder.constraint_set import ConstraintSet
from eqgen.core.catalog import Column, Table
from eqgen.core.types import BooleanType, DateType, IntegerType, NumericType, TextType, TimestampType, VarcharType
from eqgen.equivalence.ast import (
    BaseTableSource,
    CreateMaterializedView,
    CreateTable,
    CreateTemporaryTable,
    CreateTemporaryView,
    CreateView,
    DeleteReinsertTable,
    EqNode,
    EquivalentRelation,
    JoinQuery,
    NoopUpdateTable,
    ProjectionItem,
    SelectQuery,
    UnionAllQuery,
    describe_shape,
    render_tree,
)
from eqgen.equivalence.builders.base import EquivalenceBuilder
from eqgen.equivalence.capabilities import ObjectKind
from eqgen.equivalence.config import default_equivalence_config
from eqgen.equivalence.constraints import (
    ColumnRewriteConstraint,
    ExposedNameConstraint,
    KeyChannelConstraint,
    RowFilterConstraint,
    TagChannelConstraint,
)
from eqgen.equivalence.context import EquivalenceContext, NameGenerator, ObjectNamer
from eqgen.equivalence.emitter import SqlEmitter, emit_equivalence
from eqgen.equivalence.factory import EquivalenceBuilderFactory
from eqgen.equivalence.generator import EquivalenceGenerator
from eqgen.generators.example_generator import RandomPredicateSource
from eqgen.ir import expr
from eqgen.ir.expr import ColumnRef
from eqgen.ir.render import render

pytestmark = pytest.mark.unit


def _namer() -> ObjectNamer:
    return ObjectNamer("t", NameGenerator())


def _base_source() -> BaseTableSource:
    return BaseTableSource(Table("t", [Column("c", IntegerType(), 1)]))


# ---------------------------------------------------------------------------
# Node-level rendering, including nodes no shipped builder produces
# ---------------------------------------------------------------------------


def test_create_view_and_table_render_the_expected_ddl() -> None:
    namer = _namer()
    view = CreateView.build(namer, SelectQuery(_base_source()))
    table = CreateTable.build(namer, SelectQuery(_base_source()))
    assert emit_equivalence(view)[0].statement_text == "CREATE VIEW t_view_1 AS SELECT * FROM t"
    assert emit_equivalence(table)[0].statement_text == "CREATE TABLE t_table_1 AS SELECT * FROM t"


@pytest.mark.parametrize(
    ("node_cls", "keyword", "kind"),
    [
        (CreateTemporaryView, "CREATE TEMPORARY VIEW", ObjectKind.TEMPORARY_VIEW),
        (CreateMaterializedView, "CREATE MATERIALIZED VIEW", ObjectKind.MATERIALIZED_VIEW),
        (CreateTemporaryTable, "CREATE TEMPORARY TABLE", ObjectKind.TEMPORARY_TABLE),
    ],
)
def test_builderless_create_kinds_still_render(node_cls: Any, keyword: str, kind: ObjectKind) -> None:
    """Node-level rendering for the whole ``CREATE … AS`` family.

    The temporary kinds and materialized views all have builders now; they stay here because this
    is where the keyword and the ``ObjectKind`` are pinned.
    """
    node = node_cls.build(_namer(), SelectQuery(_base_source()))
    assert node.kind is kind
    assert emit_equivalence(node)[0].statement_text.startswith(keyword)


def test_join_query_renders_with_qualified_projection() -> None:
    """Also builderless, for the same reason. ``qualified_col`` exists in the IR only for this."""
    namer = _namer()
    left = CreateTable.build(namer, SelectQuery(_base_source()))
    right = CreateTable.build(namer, SelectQuery(_base_source()))
    join = JoinQuery(
        left,
        right,
        condition=expr.eq(expr.qualified_col("l", "c", IntegerType()), expr.qualified_col("r", "c", IntegerType())),
        join_type="INNER",
        projection=[ProjectionItem("c", expr.qualified_col("l", "c", IntegerType()), IntegerType())],
        left_alias="l",
        right_alias="r",
    )
    rendered = emit_equivalence(CreateView.build(namer, join))[-1].statement_text
    assert "INNER JOIN" in rendered and "ON l.c = r.c" in rendered
    assert "l.c AS c" in rendered  # qualified, so it needs the alias


def test_a_join_without_a_condition_is_rejected() -> None:
    namer = _namer()
    left = CreateTable.build(namer, SelectQuery(_base_source()))
    right = CreateTable.build(namer, SelectQuery(_base_source()))
    join = JoinQuery(left, right, None, "INNER", [], "l", "r")
    with pytest.raises(ValueError):
        emit_equivalence(CreateView.build(namer, join))


def test_passthrough_projections_omit_the_redundant_as() -> None:
    """Decided by comparing rendered text with the alias, so it works for opaque nodes too — and
    keeps a saved repro readable, which matters because humans read those while triaging."""
    namer = _namer()
    items = [ProjectionItem("c", expr.col("c", IntegerType()), IntegerType())]
    plain = emit_equivalence(CreateView.build(namer, SelectQuery(_base_source(), items)))[0].statement_text
    assert plain.endswith("SELECT c FROM t")

    aliased = [ProjectionItem("d", expr.col("c", IntegerType()), IntegerType())]
    renamed = emit_equivalence(CreateView.build(namer, SelectQuery(_base_source(), aliased)))[0].statement_text
    assert renamed.endswith("SELECT c AS d FROM t")


def test_delete_reinsert_renders_the_same_predicate_on_both_steps() -> None:
    """The correctness argument for this rewrite is that the rows deleted are the rows put back.
    Both steps therefore share one expression node, and the emitter renders it twice."""
    table = Table("t__base", [Column("c", IntegerType(), 1)])
    predicate = expr.eq(expr.mod(expr.col("c", IntegerType()), 2), expr.int_lit(1))
    node = DeleteReinsertTable.build(_namer(), SelectQuery(BaseTableSource(table)), table, predicate)
    statements = [s.statement_text for s in emit_equivalence(node)]
    assert statements[0].startswith("CREATE TABLE")
    assert statements[1] == "DELETE FROM t_table_1 WHERE MOD(c, 2) = 1"
    assert statements[2] == "INSERT INTO t_table_1 SELECT c FROM t__base WHERE MOD(c, 2) = 1"


def test_delete_reinsert_without_a_key_deletes_and_reinserts_everything() -> None:
    table = Table("t__base", [Column("c", TextType(), 1)])
    node = DeleteReinsertTable.build(_namer(), SelectQuery(BaseTableSource(table)), table, None)
    statements = [s.statement_text for s in emit_equivalence(node)]
    assert statements[1] == "DELETE FROM t_table_1"
    assert "WHERE" not in statements[2]


def test_delete_reinsert_builder_embeds_a_plugin_predicate_on_both_steps() -> None:
    """A PredicateSource text must appear on both DELETE and INSERT — same object, same spelling."""

    class StubPredicateSource:
        @property
        def name(self) -> str:
            return "stub"

        def boolean_predicate(self, table: Table, *, seed: int) -> str:
            return "c > 0"

    factory = EquivalenceBuilderFactory()
    builder = next(b for b in factory._builders if type(b).__name__ == "DeleteReinsertTableBuilder")
    table = Table("t__base", [Column("c", IntegerType(), 1)])
    context = EquivalenceContext(
        default_equivalence_config(),
        base_table=table,
        predicate_source=StubPredicateSource(),
        name_generator=NameGenerator(),
    )
    node = builder._build(ConstraintSet([]), context)
    assert isinstance(node, DeleteReinsertTable)
    statements = [s.statement_text for s in emit_equivalence(node)]
    assert any(s.startswith("DELETE FROM") and "(c > 0)" in s for s in statements), statements
    assert any(s.startswith("INSERT INTO") and "(c > 0)" in s for s in statements), statements


def test_noop_update_renders_identity_assignments() -> None:
    table = Table("t__base", [Column("c", IntegerType(), 1), Column("t", TextType(), 2)])
    query = SelectQuery(BaseTableSource(table))
    assignments = (
        ("c", expr.col("c", IntegerType())),
        ("t", expr.col("t", TextType())),
    )
    predicate = expr.eq(expr.mod(expr.col("c", IntegerType()), 2), expr.int_lit(1))
    node = NoopUpdateTable.build(_namer(), query, assignments, predicate)
    statements = [s.statement_text for s in emit_equivalence(node)]
    assert statements[0].startswith("CREATE TABLE")
    assert statements[1] == "UPDATE t_table_1 SET c = c, t = t WHERE MOD(c, 2) = 1"


def test_noop_update_builder_embeds_a_plugin_predicate() -> None:
    class StubPredicateSource:
        @property
        def name(self) -> str:
            return "stub"

        def boolean_predicate(self, table: Table, *, seed: int) -> str:
            return "c <> 0"

    factory = EquivalenceBuilderFactory()
    builder = next(b for b in factory._builders if type(b).__name__ == "NoopUpdateTableBuilder")
    table = Table("t__base", [Column("c", IntegerType(), 1)])
    context = EquivalenceContext(
        default_equivalence_config(),
        base_table=table,
        predicate_source=StubPredicateSource(),
        name_generator=NameGenerator(),
    )
    node = builder._build(ConstraintSet([]), context)
    assert isinstance(node, NoopUpdateTable)
    statements = [s.statement_text for s in emit_equivalence(node)]
    assert any(s.startswith("UPDATE ") and "(c <> 0)" in s and " = " in s for s in statements), statements


def test_a_query_node_cannot_be_referenced_by_name() -> None:
    with pytest.raises(TypeError):
        SelectQuery(_base_source()).ref_sql()


def test_union_all_requires_two_branches() -> None:
    with pytest.raises(AssertionError):
        UnionAllQuery(CreateView.build(_namer(), SelectQuery(_base_source())))


def test_a_tree_describes_itself_two_ways() -> None:
    """Two descriptions because they answer different questions.

    The summary is one line and groups rounds — "depth 3, two views" is what you sort on. The tree
    names each object, which is what ties a node to the statement that created it; nesting rebuilt by
    hand from forty ``CREATE``\\ s is the tedious half of reading a round.
    """
    inner = CreateView.build(_namer(), SelectQuery(_base_source()))
    root = CreateTable.build(_namer(), SelectQuery(inner))

    summary = describe_shape(root)
    assert summary.startswith("CreateTable, depth 5, 5 nodes")
    assert "SelectQuery 2" in summary and "BaseTableSource 1" in summary

    tree = render_tree(root)
    assert tree[0] == f"CreateTable {root.materialized_name}"
    assert tree[2] == f"    CreateView {inner.materialized_name}"
    assert [line for line in tree if line.strip().startswith("BaseTableSource")]


# ---------------------------------------------------------------------------
# The DAG walk
# ---------------------------------------------------------------------------


def test_a_shared_child_is_created_exactly_once() -> None:
    """The AST is a DAG: one relation can feed several parents. Emitting a shared child twice
    would make the second ``CREATE`` fail outright."""
    namer = _namer()
    shared = CreateTable.build(namer, SelectQuery(_base_source()))
    left = CreateView.build(namer, SelectQuery(shared))
    right = CreateView.build(namer, SelectQuery(shared))
    statements = [s.statement_text for s in emit_equivalence(CreateView.build(namer, UnionAllQuery(left, right)))]
    assert sum(1 for s in statements if s.startswith("CREATE TABLE t_table_1 ")) == 1


def test_children_are_created_before_the_parents_that_read_them() -> None:
    namer = _namer()
    inner = CreateTable.build(namer, SelectQuery(_base_source()))
    outer = CreateView.build(namer, SelectQuery(inner))
    statements = [s.statement_text for s in emit_equivalence(outer)]
    assert statements.index("CREATE TABLE t_table_1 AS SELECT * FROM t") < statements.index(
        "CREATE VIEW t_view_1 AS SELECT * FROM t_table_1"
    )


def test_the_emitter_is_a_pure_render_pass() -> None:
    """Emitting twice must produce identical text: no names minted, no state mutated. Otherwise a
    repro written after a comparison would not match the SQL that was actually run."""
    node = CreateView.build(_namer(), SelectQuery(_base_source()))
    assert [s.statement_text for s in emit_equivalence(node)] == [s.statement_text for s in emit_equivalence(node)]


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------


def test_two_row_filters_in_one_set_conjoin() -> None:
    left = RowFilterConstraint(expr.eq(expr.col("a", IntegerType()), expr.int_lit(1)))
    right = RowFilterConstraint(expr.is_null(expr.col("b", IntegerType())))
    merged = ConstraintSet([left, right]).get_constraint(RowFilterConstraint)
    from eqgen.ir.render import render

    assert render(merged.predicate) == "(a = 1) AND (b IS NULL)"


def test_overlapping_column_rewrites_raise_rather_than_silently_picking_one() -> None:
    item = ProjectionItem("c", expr.col("c", IntegerType()), IntegerType())
    with pytest.raises(ValueError):
        ConstraintSet([ColumnRewriteConstraint((item,)), ColumnRewriteConstraint((item,))])


def test_exposed_name_is_checked_not_merely_requested() -> None:
    node = CreateView.build(_namer(), SelectQuery(_base_source()), exposed_name="t")
    assert ExposedNameConstraint("t").meets_constraint(node)
    assert not ExposedNameConstraint("other").meets_constraint(node)


# ---------------------------------------------------------------------------
# Factory and config
# ---------------------------------------------------------------------------


def test_every_registered_builder_is_listed_in_the_config() -> None:
    """The drift guard. A builder that is registered but absent from configuration silently
    defaults to weight 1, so it would start firing without anyone deciding it should."""
    factory = EquivalenceBuilderFactory()
    configured = set(default_equivalence_config().builder_weights)
    assert factory.registered_builder_names <= configured, factory.registered_builder_names - configured


def test_the_config_lists_no_builder_that_does_not_exist() -> None:
    """The other direction: a weight for a builder that was removed is dead configuration, and
    reads as though a transform is enabled when nothing implements it."""
    factory = EquivalenceBuilderFactory()
    configured = set(default_equivalence_config().builder_weights)
    assert configured <= factory.registered_builder_names, configured - factory.registered_builder_names


def test_root_weights_restrict_the_root_to_named_objects() -> None:
    """The outermost object is the one the workload queries by name, so every builder eligible to be
    it must produce a named relation rather than a bare query.

    Asserted as that property rather than as a fixed list of names: the list changes every time an
    object kind is added, and a test that has to be edited to stay passing stops being a check.
    """
    factory = EquivalenceBuilderFactory()
    produces = {type(builder).__name__: builder.result_type() for builder in factory._builders}
    for name in default_equivalence_config().root_builder_weights:
        assert issubclass(produces[name], EquivalentRelation), f"{name} produces {produces[name].__name__}"


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _rich_table(name: str = "t__base") -> Table:
    return Table(
        name,
        [
            Column("c_int", IntegerType(), 1),
            Column("c_dec", NumericType(10, 2), 2),
            Column("c_txt", VarcharType(), 3),
            Column("c_flag", BooleanType(), 4),
            Column("c_date", DateType(), 5),
            Column("c_ts", TimestampType(), 6),
        ],
    )


def test_generation_is_deterministic_per_seed() -> None:
    """A finding is worthless if it cannot be rebuilt. One seed must rebuild one equivalence,
    byte for byte."""
    generator = EquivalenceGenerator(predicate_source=RandomPredicateSource())
    first = [s.statement_text for s in generator.generate(_rich_table(), seed=11).setup_statements]
    second = [s.statement_text for s in generator.generate(_rich_table(), seed=11).setup_statements]
    assert first == second
    assert [s.statement_text for s in generator.generate(_rich_table(), seed=12).setup_statements] != first


def test_everything_dispatch_hands_back_exposes_the_base_signature() -> None:
    """Four builders project ``context.base_table``'s columns onto a child they asked the factory
    for, so that is only correct while **asking** always returns the base's columns.

    Checked by watching every ``build_subtree`` result rather than by walking the finished tree: a
    builder may legitimately construct a child of a different shape for its own use — as
    ``AddDropColumnQueryBuilder`` does, adding a column an outer query projects away again — and
    walking the tree cannot tell that apart from the mistake.

    Nothing enforced this before, and the breakage would not point at itself: the projection would
    name a column its source lacks, and the round would land in the unbuildable or not-equivalent
    counter with the cause several levels down.
    """
    table = _rich_table()
    want = [(named.alias, named.target) for named in table.get_signature()]
    generator = EquivalenceGenerator(predicate_source=RandomPredicateSource())
    factory = generator.factory
    original = factory.build_subtree
    checked = 0

    def watched(result_type: Any, constraints: Any, context: Any) -> Any:
        nonlocal checked
        node = original(result_type, constraints, context)
        # The exceptions are the two ways of *asking* for a different shape, and both are explicit:
        # a column rewrite names the projection it wants, and a channel asks for a relation carrying
        # a key column. Whoever asks projects the extra column away again before the object is
        # exposed. Everything else has to come back with the base table's columns.
        asked_for_another_shape = any(
            constraints.get_optional_constraint(kind) is not None
            for kind in (ColumnRewriteConstraint, KeyChannelConstraint, TagChannelConstraint)
        )
        if node is not None and not asked_for_another_shape:
            got = [(named.alias, named.target) for named in node.get_signature()]
            assert got == want, f"{type(node).__name__} was dispatched with columns {[g[0] for g in got]}"
            checked += 1
        return node

    factory.build_subtree = watched  # type: ignore[assignment,method-assign]
    try:
        for seed in range(60):
            generator.generate(table, seed=seed, exposed_name="t")
    finally:
        factory.build_subtree = original  # type: ignore[method-assign]
    assert checked > 500, f"expected a broad sample, saw only {checked} dispatches"


def test_the_root_is_named_for_the_drop_in_replacement() -> None:
    generator = EquivalenceGenerator()
    assert generator.generate(_rich_table(), seed=1, exposed_name="t").exposed_name == "t"
    # Default: the base table's own name, for the same-database case.
    assert generator.generate(_rich_table("orders"), seed=1).exposed_name == "orders"


def test_generation_works_with_no_predicate_source_at_all() -> None:
    """The three-way split declines and the always-true ``CASE`` builder writes its own predicate, so
    generation still
    runs — narrower, but with no dependency on any external tool."""
    equivalence = EquivalenceGenerator().generate(_rich_table(), seed=3, exposed_name="t")
    assert equivalence.setup_statements


def test_a_predicate_source_reaches_the_emitted_sql() -> None:
    """Proof the plugin boundary is actually wired: text from the source appears in the DDL."""
    generator = EquivalenceGenerator(predicate_source=RandomPredicateSource())
    for seed in range(40):
        statements = [s.statement_text for s in generator.generate(_rich_table(), seed=seed, exposed_name="t").setup_statements]
        if any("c_txt" in s and "WHERE" in s for s in statements):
            return
    pytest.fail("no generated predicate reached the emitted SQL in 40 seeds")


def test_the_ported_builders_each_fire_and_stay_equivalent() -> None:
    """The four builders ported from the original generator, checked at the node level.

    ``--sweep`` cannot answer this for ``EetDeterminedFilterQueryBuilder``: the sweep supplies no
    predicate source, so it declines every seed while the scaffolding\'s output is credited to it.
    Row equivalence for all four is executed in ``fuzz_test.py``; here we pin that each one is
    reachable from ``generate()`` at all, which is what a missing ``.gcl`` weight would break.
    """
    generator = EquivalenceGenerator(predicate_source=RandomPredicateSource())
    fired: set[str] = set()
    for seed in range(60):
        root = generator.generate(_rich_table(), seed=seed, exposed_name="t").root
        seen: set[int] = set()
        stack: list[EqNode] = [root]
        while stack:
            node = stack.pop()
            if id(node) in seen:
                continue
            seen.add(id(node))
            if isinstance(node, CreateTemporaryView):
                fired.add("temp view")
            if isinstance(node, CreateTemporaryTable):
                fired.add("temp table")
            if isinstance(node, SelectQuery):
                if node.projection is None and node.predicate is not None and "OR (NOT (" in render(node.predicate):
                    fired.add("determined filter")
                if node.projection and all(isinstance(i.expr, ColumnRef) and i.expr.name == i.alias for i in node.projection):
                    fired.add("explicit projection")
            stack.extend(node.children())
    assert fired == {"temp view", "temp table", "determined filter", "explicit projection"}, fired


def test_the_determined_filter_keeps_the_filter_it_was_given() -> None:
    """Listing ``RowFilterConstraint`` is a promise to honour it. Replacing the filter instead of
    conjoining onto it would widen the rows of every branch dispatched into this builder — and the
    round would be thrown out as our bug rather than the engine\'s."""
    factory = EquivalenceBuilderFactory()
    builder = next(b for b in factory._builders if type(b).__name__ == "EetDeterminedFilterQueryBuilder")
    context = EquivalenceContext(
        default_equivalence_config(),
        base_table=_rich_table(),
        predicate_source=RandomPredicateSource(),
        name_generator=NameGenerator(),
    )
    asked = expr.eq(expr.mod(expr.col("c_int", IntegerType()), 2), expr.int_lit(0))
    query = builder._build(ConstraintSet([RowFilterConstraint(asked)]), context)
    assert isinstance(query, SelectQuery) and query.predicate is not None
    rendered = render(query.predicate)
    assert "MOD(c_int, 2) = 0" in rendered, rendered  # the filter it was handed survived
    assert "OR (NOT (" in rendered, rendered  # and the always-true part was added


def test_generation_composes_to_depth() -> None:
    """Chaining is the whole reason relations are also sources. If every equivalence were a single
    view over the base, the composition claim would be untested."""
    generator = EquivalenceGenerator(predicate_source=RandomPredicateSource())
    deepest = max(len(generator.generate(_rich_table(), seed=seed, exposed_name="t").setup_statements) for seed in range(30))
    assert deepest > 3, "expected at least one multi-level equivalence"


def test_a_custom_emitter_is_used_when_supplied() -> None:
    class ShoutingEmitter(SqlEmitter):
        def visit_view_object(self, node: Any) -> Any:
            return [type(s)(s.statement_text.upper()) for s in super().visit_view_object(node)]

    generator = EquivalenceGenerator(emitter=ShoutingEmitter())
    for seed in range(20):
        statements = [s.statement_text for s in generator.generate(_rich_table(), seed=seed, exposed_name="t").setup_statements]
        shouted = [s for s in statements if s.startswith("CREATE") and s.upper() == s]
        if shouted:
            return
    pytest.fail("the custom emitter never rendered a view in 20 seeds")


# ---------------------------------------------------------------------------
# Row equivalence, executed — the claim the whole project rests on
# ---------------------------------------------------------------------------

_COLUMNS = (
    ("c_int", IntegerType(), "INTEGER"),
    ("c_dec", NumericType(10, 2), "DECIMAL(10, 2)"),
    ("c_txt", VarcharType(), "VARCHAR"),
    ("c_flag", BooleanType(), "BOOLEAN"),
)

#: Adversarial rows: a full-NULL row (three-valued logic), a negative and a zero (the ``MOD`` sign
#: trap), an empty string, and a duplicate pair (so any reduction that collapsed duplicates would
#: be caught).
_ROWS = """
    (1, 1.50, 'a', TRUE),
    (2, -2.50, '', FALSE),
    (-1, 0.00, 'abc', NULL),
    (0, NULL, NULL, TRUE),
    (NULL, NULL, NULL, NULL),
    (1, 1.50, 'a', TRUE)
"""


def _seeded_connection() -> Any:
    duckdb = pytest.importorskip("duckdb")
    connection = duckdb.connect()
    declarations = ", ".join(f"{name} {sql}" for name, _, sql in _COLUMNS)
    connection.execute(f"CREATE TABLE t__base ({declarations})")
    connection.execute(f"INSERT INTO t__base VALUES {_ROWS}")
    return connection


def _generated_base_table() -> Table:
    """The base under the name it has *in the equivalent database*.

    The harness renames the real base aside so the equivalent can occupy its name; the generator
    must therefore be told the renamed name, or every source reference would resolve to the
    equivalent itself.
    """
    return Table("t__base", [Column(name, data_type, i + 1) for i, (name, data_type, _) in enumerate(_COLUMNS)])


@pytest.mark.parametrize("seed", range(40))
def test_a_generated_equivalence_holds_the_same_rows_as_the_base(seed: int) -> None:
    """Build the equivalence on a real engine and compare row multisets, order-blind.

    This is the project's central claim, and it doubles as proof that the base emitter's
    PostgreSQL-spelled SQL runs unmodified on a second engine.
    """
    connection = _seeded_connection()
    generator = EquivalenceGenerator(predicate_source=RandomPredicateSource())
    equivalence = generator.generate(_generated_base_table(), seed=seed, exposed_name="t")

    for statement in equivalence.setup_statements:
        connection.execute(statement.statement_text)

    columns = ", ".join(name for name, _, _ in _COLUMNS)
    base = Counter(tuple(row) for row in connection.execute(f"SELECT {columns} FROM t__base").fetchall())
    equivalent = Counter(tuple(row) for row in connection.execute(f"SELECT {columns} FROM t").fetchall())
    assert equivalent == base, f"missing={base - equivalent} extra={equivalent - base}"


def test_equivalences_without_a_predicate_source_are_also_row_equivalent() -> None:
    """The no-plugin path is a supported configuration, so it gets the same check."""
    columns = ", ".join(name for name, _, _ in _COLUMNS)
    for seed in range(15):
        connection = _seeded_connection()
        equivalence = EquivalenceGenerator().generate(_generated_base_table(), seed=seed, exposed_name="t")
        for statement in equivalence.setup_statements:
            connection.execute(statement.statement_text)
        base = Counter(tuple(r) for r in connection.execute(f"SELECT {columns} FROM t__base").fetchall())
        equivalent = Counter(tuple(r) for r in connection.execute(f"SELECT {columns} FROM t").fetchall())
        assert equivalent == base, f"seed {seed}: missing={base - equivalent} extra={equivalent - base}"


def test_the_key_is_written_once_so_every_reader_agrees_on_it() -> None:
    """``_materialize_row_key`` writes a table, not a view, and that is the whole point.

    Over a view each reader could evaluate ``ROW_NUMBER()`` separately and number the rows
    differently — then a join on that key matches the wrong rows, or a collapse merges the wrong
    copies. Both are wrong *rows*, blamed on the engine.
    """
    factory = EquivalenceBuilderFactory()
    builder = next(b for b in factory._builders if type(b).__name__ == "FlagTableJoinQueryBuilder")
    assert isinstance(builder, EquivalenceBuilder)
    context = EquivalenceContext(
        default_equivalence_config(),
        base_table=_rich_table(),
        predicate_source=RandomPredicateSource(),
        name_generator=NameGenerator(),
    )
    keyed = builder._materialize_row_key(context, "eq_uid_1")
    assert keyed is not None
    statements = [s.statement_text for s in emit_equivalence(keyed.relation)]
    assert statements[-1].startswith(("CREATE TABLE", "CREATE TEMPORARY TABLE", "CREATE UNLOGGED TABLE")), statements[-1]
    assert "ROW_NUMBER() OVER (ORDER BY" in statements[-1]
    # base_items carries the split, so a caller never has to work out which columns were the base's
    assert [i.alias for i in keyed.base_items] == [c.get_column_name() for c in _rich_table().get_column_list()]
    assert "eq_uid_1" not in [i.alias for i in keyed.base_items]


def test_the_expander_can_only_be_built_under_its_reducer() -> None:
    """The ``required_constraint_types`` half of the filter.

    Asked for without the channel, the expander is not even offered — so an expansion can never be
    left in a tree with nothing above it to collapse the copies again. Both channels share the rule.
    """
    factory = EquivalenceBuilderFactory()
    context = EquivalenceContext(
        default_equivalence_config(),
        base_table=_rich_table(),
        predicate_source=RandomPredicateSource(),
        name_generator=NameGenerator(),
    )
    for expander_name, reducer_name, channel in (
        ("KeyExplodeExpansionBuilder", "KeyDistinctReduceBuilder", KeyChannelConstraint),
        ("TagExplodeExpansionBuilder", "TagPruneFilterReduceBuilder", TagChannelConstraint),
    ):
        expander = next(b for b in factory._builders if type(b).__name__ == expander_name)
        reducer = next(b for b in factory._builders if type(b).__name__ == reducer_name)

        assert channel in expander.required_constraint_types()
        assert channel in expander.supported_constraint_types()
        # The reducer must NOT support it, or a second reducer could wedge inside an expansion.
        assert channel not in reducer.supported_constraint_types()
        assert expander._build(ConstraintSet([]), context) is None


def test_the_collapse_keeps_duplicate_base_rows_apart() -> None:
    """Why the key is in the ``DISTINCT`` at all.

    Two identical base rows must survive as two. A bare ``DISTINCT`` over the columns would merge
    them; keyed on a per-row number, each keeps its own group.
    """
    factory = EquivalenceBuilderFactory()
    reducer = next(b for b in factory._builders if type(b).__name__ == "KeyDistinctReduceBuilder")
    context = EquivalenceContext(
        default_equivalence_config(),
        base_table=_rich_table(),
        predicate_source=RandomPredicateSource(),
        name_generator=NameGenerator(),
    )
    node = reducer._build(ConstraintSet([]), context)
    assert isinstance(node, EquivalentRelation)
    statements = [s.statement_text for s in emit_equivalence(node)]
    distinct = next(s for s in statements if "SELECT DISTINCT" in s)
    assert "eq_key" in distinct, distinct  # the key is inside the DISTINCT, not projected away yet
    assert "UNION ALL" in " ".join(statements)  # and there really was an expansion underneath


def test_tag_filter_reducer_emits_keep_predicate() -> None:
    """Tag-channel collapse is a ``WHERE tag = KEEP`` over the expansion, not a key ``DISTINCT``."""
    factory = EquivalenceBuilderFactory()
    reducer = next(b for b in factory._builders if type(b).__name__ == "TagPruneFilterReduceBuilder")
    context = EquivalenceContext(
        default_equivalence_config(),
        base_table=_rich_table(),
        predicate_source=RandomPredicateSource(),
        name_generator=NameGenerator(),
    )
    node = reducer._build(ConstraintSet([]), context)
    assert isinstance(node, EquivalentRelation)
    text = " ".join(s.statement_text for s in emit_equivalence(node))
    assert "UNION ALL" in text
    assert "eq_tag" in text
    assert "WHERE" in text


def test_key_group_reducer_emits_any_value_and_group_by() -> None:
    """GROUP collapse recovers values through ``ANY_VALUE``, not ``DISTINCT``."""
    factory = EquivalenceBuilderFactory()
    reducer = next(b for b in factory._builders if type(b).__name__ == "KeyGroupAggregateReduceBuilder")
    context = EquivalenceContext(
        default_equivalence_config(),
        base_table=_rich_table(),
        predicate_source=RandomPredicateSource(),
        name_generator=NameGenerator(),
    )
    node = reducer._build(ConstraintSet([]), context)
    assert isinstance(node, EquivalentRelation)
    text = " ".join(s.statement_text for s in emit_equivalence(node))
    assert "ANY_VALUE(" in text
    assert "GROUP BY" in text
    assert "UNION ALL" in text


def test_tag_delete_reducer_deletes_then_views() -> None:
    """DELETE prune mutates the expansion table, then exposes the base columns via a view."""
    factory = EquivalenceBuilderFactory()
    reducer = next(b for b in factory._builders if type(b).__name__ == "TagPruneDeleteReduceBuilder")
    context = EquivalenceContext(
        default_equivalence_config(),
        base_table=_rich_table(),
        predicate_source=RandomPredicateSource(),
        name_generator=NameGenerator(),
    )
    node = reducer._build(ConstraintSet([]), context)
    assert isinstance(node, EquivalentRelation)
    statements = [s.statement_text for s in emit_equivalence(node)]
    assert any(s.startswith("DELETE FROM") for s in statements), statements
    assert any(s.startswith("CREATE VIEW") for s in statements), statements


def test_the_keyed_relation_is_asked_for_rather_than_built() -> None:
    """The keyed relation used to be hard-coded to ``CreateTable``, which froze that part of every
    join and expansion to one object kind.

    Now it is *asked* for — "exactly these columns" (``ColumnRewriteConstraint``) and "something you
    can write to" (``AcceptsDmlConstraint``) — so it is drawn from the pool like anything else, and
    grows automatically as writable object kinds are added.

    Both halves of that request are load-bearing. Without the column rewrite, nothing can express a
    relation with a key column added. Without the DML constraint a **view** could come back, and then
    two readers could evaluate ``ROW_NUMBER()`` separately and number the rows differently — a join
    on that key would match the wrong rows.
    """
    table = _rich_table()
    generator = EquivalenceGenerator(predicate_source=RandomPredicateSource())
    factory = generator.factory
    original = factory.build_subtree
    shapes: set[str] = set()

    def watched(result_type: Any, constraints: Any, context: Any) -> Any:
        node = original(result_type, constraints, context)
        asked = constraints.get_optional_constraint(ColumnRewriteConstraint)
        if node is not None and asked is not None and isinstance(node, EquivalentRelation):
            shapes.add(type(node).__name__)
            # the shape that came back is the shape that was asked for
            assert [n.alias for n in node.get_signature()] == [i.alias for i in asked.projection]
        return node

    factory.build_subtree = watched  # type: ignore[assignment,method-assign]
    try:
        for seed in range(40):
            generator.generate(table, seed=seed, exposed_name="t")
    finally:
        factory.build_subtree = original  # type: ignore[method-assign]

    assert len(shapes) > 1, f"still frozen to one kind: {shapes}"
    assert not any("View" in shape for shape in shapes), f"a view cannot hold a stable key: {shapes}"
