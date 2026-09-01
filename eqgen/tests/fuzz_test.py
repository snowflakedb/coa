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

"""The harness and the DuckDB dialect, end to end.

The centrepiece is :func:`test_a_full_fuzz_run_finds_nothing_on_a_correct_engine`. A clean run is a
meaningful result here, not an empty one: it says the generator built provably-equivalent objects, the
queries were usable, and the comparison did not invent a difference. Reporting findings against a
correct engine is worse than reporting none, because every real finding then has to be argued
against its own false-positive rate.

The rules for what counts as reportable are pinned too, especially the one-sided one: if the base
fails, nothing is reported no matter what the other side did.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator

import pytest

from eqgen.core.catalog import Column, Table
from eqgen.core.types import BooleanType, DoubleType, IntegerType, NumericType, TextType, VarcharType
from eqgen.dialects.duckdb.adapter import DuckDBAdapter, duckdb_equivalence_config
from eqgen.dialects.duckdb.ast import DuckDBCreateMacro
from eqgen.dialects.duckdb.emitter import DuckDBEmitter, duckdb_type
from eqgen.equivalence.ast import CreateTable, CreateView, JoinQuery, ProjectionItem, SelectQuery
from eqgen.equivalence.config import default_equivalence_config
from eqgen.equivalence.context import NameGenerator, ObjectNamer
from eqgen.equivalence.emitter import SqlEmitter, emit_equivalence
from eqgen.equivalence.generator import EquivalenceGenerator
from eqgen.generators.example_generator import RandomPredicateSource, RandomSelectSource
from eqgen.fuzz.cli import run_fuzz
from eqgen.fuzz.compare import ObjectComparison, QueryComparison, compare_objects, compare_one
from eqgen.fuzz.database import (
    Database,
    MultisetDiff,
    column_names,
    compare_multisets,
    hidden_base_name,
)
from eqgen.fuzz.journal import QueryJournal, parse_journal, queries_from_journal, sample_rows
from eqgen.fuzz.report import FuzzReport, record_round, repro_script
from eqgen.fuzz.round import RoundOutcome, run_round
from eqgen.ir import expr
from eqgen.plugins import CorpusSource

pytestmark = pytest.mark.unit


def _adapter() -> DuckDBAdapter:
    pytest.importorskip("duckdb")
    # Offline unit tests use the wheel so they do not need a downloaded CLI binary.
    return DuckDBAdapter(execution_backend="wheel")


def _namer() -> ObjectNamer:
    return ObjectNamer("t", NameGenerator())


# ---------------------------------------------------------------------------
# The dialect: type names and native renders
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data_type", "expected"),
    [
        (IntegerType(), "BIGINT"),  # wide, to keep MOD away from the type's edges
        (NumericType(10, 2), "DECIMAL(10, 2)"),
        (NumericType(38, 0), "DECIMAL(38, 0)"),
        (DoubleType(), "DOUBLE"),
        (VarcharType(), "VARCHAR"),
        (TextType(), "VARCHAR"),
        (BooleanType(), "BOOLEAN"),
    ],
)
def test_duckdb_type_names(data_type: Any, expected: str) -> None:
    assert duckdb_type(data_type) == expected


def test_the_dialect_spelling_reaches_emitted_casts() -> None:
    """One source of truth for a type name: a column's declared type and a ``CAST`` naming it must
    agree, and they only do if both come from the same place."""
    namer = _namer()
    items = [ProjectionItem("c", expr.typed_null(IntegerType()), IntegerType())]
    source = CreateTable.build(namer, SelectQuery(_base_source()))
    rendered = emit_equivalence(CreateView.build(namer, SelectQuery(source, items)), DuckDBEmitter())[-1].statement_text
    assert "CAST(NULL AS BIGINT)" in rendered


def test_duckdb_list_transform_and_filter_match_snowflake_array_codecs() -> None:
    """Yeti v3 ArrayTransform / ArrayFilter, spelled with DuckDB list_transform / list_filter."""
    from eqgen.dialects.duckdb.builders import (
        DuckDBListFilterRoundTripBuilder,
        DuckDBListTransformRoundTripBuilder,
    )

    transform = DuckDBListTransformRoundTripBuilder.__new__(DuckDBListTransformRoundTripBuilder)
    filtered = DuckDBListFilterRoundTripBuilder.__new__(DuckDBListFilterRoundTripBuilder)
    t_node = transform._column_rewriter(None)("c", IntegerType())  # type: ignore[arg-type]
    f_node = filtered._column_rewriter(None)("c", IntegerType())  # type: ignore[arg-type]
    assert t_node is not None and t_node.sql == "list_transform([c], lambda x: x)[1]"
    assert f_node is not None and f_node.sql == "list_filter([c], lambda x: 1 = 1)[1]"


def _base_source() -> Any:
    from eqgen.equivalence.ast import BaseTableSource

    return BaseTableSource(Table("t__base", [Column("c", IntegerType(), 1)]))


def test_the_native_anti_join_renders() -> None:
    namer = _namer()
    source = CreateTable.build(namer, SelectQuery(_base_source()))
    empty = CreateTable.build(namer, SelectQuery(source, predicate=expr.eq(expr.int_lit(1), expr.int_lit(0))))
    query = JoinQuery(
        source,
        empty,
        expr.bool_lit(True),
        "ANTI",
        [ProjectionItem("c", expr.qualified_col("l", "c", IntegerType()), IntegerType())],
        "l",
        "r",
    )
    rendered = emit_equivalence(CreateView.build(namer, query), DuckDBEmitter())[-1].statement_text
    assert "ANTI JOIN" in rendered and "ON TRUE" in rendered


def test_the_native_macro_renders_two_statements() -> None:
    """One node, two statements — and the exposing view is not optional: a macro is reachable only as
    ``m()``, never as a bare name, so without it the equivalent could not be queried under the base
    table's name."""
    namer = _namer()
    statements = [
        s.statement_text for s in emit_equivalence(DuckDBCreateMacro.build(namer, SelectQuery(_base_source())), DuckDBEmitter())
    ]
    assert any(s.startswith("CREATE MACRO") for s in statements)
    assert any(s.startswith("CREATE VIEW") and "_macro()" in s for s in statements)


def test_the_macro_body_carries_the_rewrite_rather_than_a_reference_to_it() -> None:
    """The macro exists to make the engine inline something *interesting*.

    Materializing the body as a named view first is the obvious implementation and it produces
    correct SQL — but then every macro body is ``SELECT * FROM <view>`` and the rewrite sits outside
    the macro, so the inlining path is exercised over nothing. The body is the dispatched query
    itself for that reason.
    """
    namer = _namer()
    items = [ProjectionItem("c", expr.typed_null(IntegerType()), IntegerType())]
    macro = DuckDBCreateMacro.build(namer, SelectQuery(_base_source(), items))
    macro_sql = next(
        s.statement_text for s in emit_equivalence(macro, DuckDBEmitter()) if s.statement_text.startswith("CREATE MACRO")
    )
    assert "CAST(NULL AS BIGINT)" in macro_sql, macro_sql


def test_the_codec_touches_only_text_columns_and_guards_nulls() -> None:
    """The NULL guard is explicit even though the functions propagate NULL: it makes it impossible for
    the round trip to turn a NULL into an empty string."""
    namer = _namer()
    source = CreateTable.build(namer, SelectQuery(_base_source()))
    c_txt = expr.col("c_txt", VarcharType())
    items = [
        ProjectionItem("c_int", expr.col("c_int", IntegerType()), IntegerType()),
        ProjectionItem(
            "c_txt",
            expr.case_when(
                expr.is_null(c_txt),
                expr.typed_null(VarcharType()),
                expr.raw_expr("decode(from_base64(to_base64(encode(c_txt))))", VarcharType()),
                VarcharType(),
            ),
            VarcharType(),
        ),
    ]
    query = SelectQuery(source, items)
    rendered = emit_equivalence(CreateView.build(namer, query), DuckDBEmitter())[-1].statement_text
    assert "to_base64" in rendered and "c_txt IS NULL THEN CAST(NULL AS VARCHAR)" in rendered
    assert "to_base64(encode(c_int" not in rendered  # the integer column passes through


def test_a_dialect_node_reaching_the_portable_emitter_fails_loud() -> None:
    """Rather than rendering as something plausible. A wrongly-rendered statement is a false finding;
    a raised error is a counted loss."""
    from eqgen.dialects.duckdb.ast import DuckDBCreateIndex

    namer = _namer()
    source = CreateTable.build(namer, SelectQuery(_base_source()))
    with pytest.raises(TypeError):
        emit_equivalence(
            DuckDBCreateIndex.build(namer, source, target="c", out_cols=["c"]),
            SqlEmitter(),
        )

def test_the_dialect_declares_its_builder_set_in_gcl() -> None:
    """The reason the config language is kept: an engine states what it can run as *data*, by
    inheriting the portable configuration and overriding one list."""
    portable = set(default_equivalence_config().builder_weights)
    duckdb = set(duckdb_equivalence_config().builder_weights)
    assert portable < duckdb, "DuckDB should inherit the portable builders and add its own"
    assert {
        "DuckDBAntiJoinEmptyRoundTripBuilder",
        "DuckDBCreateMacroBuilder",
        "DuckDBCreateIndexBuilder",
        "DuckDBUniqueIndexMatBuilder",
        "DuckDBAttachedDatabaseBuilder",
        "DuckDBEnumTypeRoundTripBuilder",
    } <= duckdb


def test_every_configured_duckdb_builder_is_registered() -> None:
    """The drift guard, for the dialect. A weight naming a builder nobody implements reads as though
    a transform is enabled when nothing does it."""
    adapter = _adapter()
    generator = EquivalenceGenerator(adapter.equivalence_config(), extra_builders=adapter.extra_builders())
    assert set(adapter.equivalence_config().builder_weights) == generator.factory.registered_builder_names


def _generated_base_table() -> Table:
    """The DuckDB rich catalog under the hidden base name the round uses."""
    return Table(hidden_base_name(_adapter().rich_catalog("t")), _adapter().rich_catalog("t").get_column_list())


def _walk(node: Any) -> Iterator[Any]:
    """Every node in an equivalence, by identity — the AST is a DAG, not a tree."""
    seen: set[int] = set()

    def visit(current: Any) -> Iterator[Any]:
        if id(current) in seen:
            return
        seen.add(id(current))
        yield current
        for child in current.children():
            yield from visit(child)

    yield from visit(node)


def _anti_join_equivalences(seeds: range) -> list[Any]:
    adapter = _adapter()
    weights = dict(adapter.equivalence_config().builder_weights)
    weights["DuckDBAntiJoinEmptyRoundTripBuilder"] = 40.0
    config = replace(adapter.equivalence_config(), max_depth=6, builder_weights=weights)
    found = []
    for seed in seeds:
        generator = EquivalenceGenerator(
            config,
            predicate_source=RandomPredicateSource(),
            emitter=adapter.emitter(),
            extra_builders=adapter.extra_builders(),
        )
        equivalence = generator.generate(_generated_base_table(), seed=seed, exposed_name="t")
        if any("ANTI JOIN" in s.statement_text for s in equivalence.setup_statements):
            found.append(equivalence)
    return found


def test_the_anti_joins_empty_side_is_dispatched_not_hard_coded() -> None:
    """ "Provably empty" is a *constraint*, so the empty side goes through the factory like any other
    child — and therefore varies in object kind across seeds.

    Pinning this matters because the tempting shortcut (construct a ``CREATE TABLE ... WHERE 1 = 0``
    inline) is invisible in the emitted SQL of any single round: it looks correct, it just silently
    caps that half of the rewrite at one shape forever.
    """
    equivalences = _anti_join_equivalences(range(40))
    assert equivalences, "the anti-join builder never fired; the weighting is wrong"

    kinds = set()
    for equivalence in equivalences:
        for node in _walk(equivalence.root):
            if isinstance(node, JoinQuery) and node.join_type == "ANTI":
                kinds.add(type(node.right).__name__)
    assert len(kinds) > 1, f"the empty side is stuck on one object kind: {kinds}"


def test_the_anti_joins_empty_side_stays_empty_however_it_was_built() -> None:
    """The soundness premise, **executed**.

    Dispatching the empty side widens what can build it — a view, a table, a macro over a view, a
    nested partition — so the guarantee can no longer be read off the SQL shape. It comes from the
    constraint instead: ``1 = 0`` survives conjunction with any parent filter, so everything built
    beneath it is empty too. That is an argument on paper, and the way to check one is to run it and
    count the rows.
    """
    pytest.importorskip("duckdb")
    adapter = _adapter()
    table = adapter.rich_catalog("t")
    rows = sample_rows(table, 8, seed=5)
    checked = 0
    for equivalence in _anti_join_equivalences(range(20)):
        empty_names = {
            node.right.materialized_name
            for node in _walk(equivalence.root)
            if isinstance(node, JoinQuery) and node.join_type == "ANTI"
        }
        assert empty_names
        statements = [s.statement_text for s in equivalence.setup_statements]
        database = Database.build_equivalent(adapter, table, rows, statements=statements)
        try:
            for name in empty_names:
                outcome = database.query(f"SELECT COUNT(*) FROM {name}")
                assert outcome.error is None, outcome.error
                assert outcome.rows is not None
                assert list(outcome.rows) == [(0,)], f"{name} was not empty: {outcome.rows}"
                checked += 1
        finally:
            database.close()
    assert checked, "no anti-join empty side was checked"


def test_the_anti_join_is_row_equivalent_on_a_live_engine() -> None:
    """The dispatched empty side, executed. A unit test can show the SQL is shaped right; only the
    engine shows the rows still match."""
    pytest.importorskip("duckdb")
    adapter = _adapter()
    table = _generated_base_table()
    rows = sample_rows(table, 8, seed=11)
    weights = dict(adapter.equivalence_config().builder_weights)
    weights["DuckDBAntiJoinEmptyRoundTripBuilder"] = 40.0
    config = replace(adapter.equivalence_config(), max_depth=6, builder_weights=weights)
    checked = 0
    for seed in range(12):
        generator = EquivalenceGenerator(
            config,
            predicate_source=RandomPredicateSource(),
            emitter=adapter.emitter(),
            extra_builders=adapter.extra_builders(),
        )
        queries = list(RandomSelectSource().iter_queries(table, seed=seed, limit=4))
        outcome = run_round(adapter, generator, table, rows, queries, seed=seed)
        assert outcome.setup_error is None, outcome.setup_error
        if not any("ANTI JOIN" in s for s in outcome.equivalent_statements):
            continue
        checked += 1
        comparison = outcome.object_comparison
        assert comparison is not None and comparison.rows.equal, comparison
        assert comparison.base_types == comparison.equivalent_types, comparison
        assert all(result.equal for result in outcome.results), [r for r in outcome.results if not r.equal]
    assert checked, "no round used the anti-join"


# ---------------------------------------------------------------------------
# Database and comparison
# ---------------------------------------------------------------------------


def _table() -> Table:
    return Table("t", [Column("c_int", IntegerType(), 1), Column("c_txt", VarcharType(), 2)])


def _rows() -> list[tuple[object, ...]]:
    return [(1, "a"), (2, ""), (None, None), (1, "a")]


def test_database_query_returns_rows_rather_than_raising() -> None:
    adapter = _adapter()
    database = Database.build_base(adapter, _table(), _rows())
    outcome = database.query("SELECT c_int FROM t")
    assert outcome.error is None and outcome.rows is not None
    assert sum(outcome.rows.values()) == 4


def test_database_query_classifies_a_failure_instead_of_raising() -> None:
    adapter = _adapter()
    database = Database.build_base(adapter, _table(), _rows())
    outcome = database.query("SELECT nope FROM t")
    assert outcome.rows is None and outcome.error


def test_database_query_labels_a_known_issue() -> None:
    """A conversion error depends on which rows a plan happens to evaluate, so it is not a dependable
    signal."""
    adapter = _adapter()
    database = Database.build_base(adapter, _table(), _rows())
    outcome = database.query("SELECT CAST('nonsense' AS INTEGER) FROM t")
    assert outcome.known_issue == "duckdb-conversion-error"


def test_duckdb_labels_join_order_reconstruct_as_known_issue() -> None:
    """Filed INTERNAL Error — demote so hunts do not re-report the same crash."""
    adapter = _adapter()
    msg = "INTERNAL Error: Operator occurrence 2 was reconstructed more than once"
    assert adapter.known_issue_label(Exception(msg)) == "duckdb-join-order-reconstruct"
    assert adapter.known_issue_label(Exception("Could not orient operator occurrence 1")) == (
        "duckdb-join-order-reconstruct"
    )
    assert adapter.known_issue_label(Exception("INTERNAL Error: other")) is None


def test_accepts_uses_explain_so_nothing_executes() -> None:
    """The predicate gate. It asks the engine, which is the authority on what it will accept."""
    adapter = _adapter()
    database = Database.build_base(adapter, _table(), _rows())
    assert database.accepts("SELECT 1 FROM t WHERE c_int > 1")
    assert not database.accepts("SELECT 1 FROM t WHERE COUNT(*) > 1")  # aggregate in WHERE
    assert not database.accepts("SELECT 1 FROM t WHERE nope > 1")  # unresolvable column
    assert not database.accepts("SELECT 1 FROM t WHERE t.c_int > 1 AND other.x = 1")  # unknown relation


def test_multisets_compare_duplicates_not_just_sets() -> None:
    """A rewrite that deduplicated rows would pass a set comparison and be wrong."""
    diff = compare_multisets(Counter({("a",): 2}), Counter({("a",): 1}))
    assert not diff.equal and diff.only_in_base == [(("a",), 1)]


def test_canonical_row_freezes_array_cells() -> None:
    """Postgres ``REGEXP_MATCH`` / arrays arrive as ``list`` — must still be Counter keys."""
    from eqgen.fuzz.database import canonical_row

    frozen = canonical_row([(["a", "b"], 1), None])
    assert frozen == ((("a", "b"), 1), None)
    assert Counter([frozen, frozen])[frozen] == 2


def test_multisets_treat_nan_as_equal() -> None:
    """Both sides returning NaN (e.g. VAR_POP of a degenerate input) is agreement, not a mismatch."""
    from eqgen.fuzz.database import canonical_row

    left = Counter([canonical_row((float("nan"),))])
    right = Counter([canonical_row((float("nan"),))])
    assert compare_multisets(left, right).equal
    assert not compare_multisets(
        Counter([canonical_row((float("nan"),))]),
        Counter([canonical_row((1.0,))]),
    ).equal


def test_multisets_tolerate_float_ulp_noise() -> None:
    """Plan-order DOUBLE aggregates differ in the last bits — that is not an engine bug."""
    from decimal import Decimal

    from eqgen.fuzz.database import canonical_row

    # Same shape as the postgres sqlancerpp VARIANCE / COVAR false positives.
    assert compare_multisets(
        Counter({(2.146007029070656,): 1}),
        Counter({(2.1460070290706548,): 1}),
    ).equal
    assert compare_multisets(
        Counter({(-2.3718400980628343e-17,): 1}),
        Counter({(1.7612174345189984e-16,): 1}),
    ).equal
    # Near-zero STDDEV / COVAR residuals (~3e-8) across plans — absolute floor, not a bug.
    assert compare_multisets(
        Counter({(0.0,): 1}),
        Counter({(3.169707414775591e-08,): 1}),
    ).equal
    # Near-zero COVAR_SAMP sign flip (~5e-9 apart) — still noise, not a bug.
    assert compare_multisets(
        Counter({(-1.7149674008264565e-09,): 1}),
        Counter({(3.6967075084481397e-09,): 1}),
    ).equal
    # MariaDB STDDEV_POP over bitwise OR (~2.7e-6 relative) — plan-order, not a bug.
    assert compare_multisets(
        Counter({(179670095.6465,): 1}),
        Counter({(179669606.4001,): 1}),
    ).equal
    # MariaDB STDDEV_POP over bitwise OR + join (~1.5e-7 relative) — plan-order, not a bug.
    assert compare_multisets(
        Counter({(411247692.1744,): 1}),
        Counter({(411247751.8523,): 1}),
    ).equal
    # MariaDB STDDEV over bitwise OR (~9e-5 relative) — still plan-order noise.
    assert compare_multisets(
        Counter({(32737872.0929,): 1}),
        Counter({(32734835.5828,): 1}),
    ).equal
    assert compare_multisets(
        Counter({(80898210.6266,): 1}),
        Counter({(80897249.87,): 1}),
    ).equal
    # MariaDB VAR_SAMP (~1.2e-5 relative on 1e16-scale values).
    assert compare_multisets(
        Counter({(7.375513696495891e16,): 1}),
        Counter({(7.375601231659795e16,): 1}),
    ).equal
    # Small VAR_POP with 4-decimal driver rounding (0.2813 vs 0.2812).
    assert compare_multisets(
        Counter({(0.2813,): 1}),
        Counter({(0.2812,): 1}),
    ).equal
    # Drivers may hand DOUBLE back as Decimal on one or both sides.
    assert compare_multisets(
        Counter({(Decimal("411247692.1744"),): 1}),
        Counter({(411247751.8523,): 1}),
    ).equal
    assert compare_multisets(
        Counter([canonical_row((Decimal("411247692.1744"),))]),
        Counter([canonical_row((Decimal("411247751.8523"),))]),
    ).equal
    # Dolt sql-server returns DOUBLE aggregates as decimal *strings*.
    assert compare_multisets(
        Counter({("28.324648528899953",): 1}),
        Counter({("28.32464852889995",): 1}),
    ).equal
    assert compare_multisets(
        Counter({("358536261167658300",): 1}),
        Counter({("358536261167658400",): 1}),
    ).equal
    assert compare_multisets(
        Counter({("28.324648528899953",): 1}),
        Counter({(28.32464852889995,): 1}),
    ).equal
    # Still catch a real disagreement.
    assert not compare_multisets(Counter({(1.0,): 1}), Counter({(2.0,): 1})).equal
    # Non-floats stay exact.
    assert not compare_multisets(Counter({(1,): 1}), Counter({(2,): 1})).equal
    assert not compare_multisets(Counter({("a",): 1}), Counter({("b",): 1})).equal
    assert not compare_multisets(Counter({("100",): 1}), Counter({("200",): 1})).equal


def test_tolerance_reconciled_rows_are_counted_not_just_absorbed() -> None:
    """Agreement bought with the float tolerance has to say so, or a widening tolerance and a fixed
    engine look identical."""
    exact = compare_multisets(Counter({(1.0, "a"): 3}), Counter({(1.0, "a"): 3}))
    assert exact.equal and exact.reconciled == 0

    tolerant = compare_multisets(Counter({(1.0,): 2}), Counter({(1.00001,): 2}))
    assert tolerant.equal and tolerant.reconciled == 2

    real = compare_multisets(Counter({(1.0,): 1}), Counter({(2.0,): 1}))
    assert not real.equal and real.reconciled == 0

    assert QueryComparison("q", equal=True).verdict == "PASS"
    assert "3 row(s) reconciled" in QueryComparison("q", equal=True, reconciled=3).verdict


def test_the_report_totals_the_leniency_it_applied() -> None:
    report = FuzzReport()
    record_round(
        report,
        RoundOutcome(
            seed=1,
            results=[
                QueryComparison("q1", equal=True),
                QueryComparison("q2", equal=True, reconciled=2),
                QueryComparison("q3", equal=True, reconciled=5),
            ],
            object_comparison=ObjectComparison(
                rows=MultisetDiff(equal=True, only_in_base=[], only_in_other=[]),
                base_types=("BIGINT",),
                equivalent_types=("BIGINT",),
            ),
        ),
        0,
    )
    assert report.passed == 3
    assert (report.tolerated_queries, report.tolerated_rows) == (2, 7)
    assert "float tolerance reconciled 7 row(s) across 2" in report.summary()
    assert "rel=0.0001 abs=0.0005" in report.summary()

    assert "float tolerance" not in FuzzReport().summary()


def test_nothing_is_reported_when_the_base_failed() -> None:
    """The asymmetry that keeps the run honest: a query the base rejects was already invalid, so the
    equivalent's behaviour on it says nothing."""
    both_failed = QueryComparison("q", equal=False, base_error="boom", equivalent_error="boom")
    assert both_failed.is_uncomparable and not both_failed.is_reportable
    only_equivalent = QueryComparison("q", equal=False, equivalent_error="boom")
    assert only_equivalent.is_error and only_equivalent.is_reportable


def test_a_known_issue_is_skipped_rather_than_reported() -> None:
    comparison = QueryComparison("q", equal=False, equivalent_error="x", equivalent_known_issue="label")
    assert comparison.is_known_issue and not comparison.is_reportable


def test_compare_one_agrees_when_both_sides_agree() -> None:
    adapter = _adapter()
    table, rows = _table(), _rows()
    base = Database.build_base(adapter, table, rows)
    equivalent = Database.build_base(adapter, table, rows)
    assert compare_one(base, equivalent, "SELECT c_int FROM t").is_pass


def test_the_object_comparison_catches_a_type_change_with_identical_rows() -> None:
    """Rows can match while a column's declared type does not — which then breaks every workload query
    using a type-restricted function, reported as a finding with no engine bug behind it."""
    adapter = _adapter()
    table, rows = _table(), _rows()
    base = Database.build_base(adapter, table, rows)
    equivalent = Database.build_base(adapter, table, rows)
    # INTEGER rather than the declared BIGINT: identical Python values, different declared type. That
    # is the shape of the real defect -- a rewrite that was row-exact while narrowing a column.
    equivalent.run(["CREATE OR REPLACE TABLE t AS SELECT CAST(c_int AS INTEGER) AS c_int, c_txt FROM t"])
    comparison = compare_objects(base, equivalent, table, column_names(table))
    assert comparison.rows.equal, "the rows must match, or this is testing the wrong thing"
    assert not comparison.types_agree
    assert not comparison.equal


# ---------------------------------------------------------------------------
# Journal and seed rows
# ---------------------------------------------------------------------------


def test_the_journal_records_a_query_before_its_verdict(tmp_path: Path) -> None:
    """Ordering is the design: a crash must leave the offending query already on disk."""
    path = tmp_path / "round.log"
    with QueryJournal(path, header="test") as journal:
        journal.begin("SELECT 1")
        # Readable mid-round, before the verdict is known — which is the crash case.
        assert "SELECT 1;" in path.read_text()
        journal.end("PASS")
    entries = parse_journal(path.read_text())
    assert [(e.sql, e.annotation) for e in entries] == [("SELECT 1", "PASS")]


def test_a_journal_round_trips_into_a_replayable_corpus(tmp_path: Path) -> None:
    """This is what makes a run reproducible query-for-query."""
    path = tmp_path / "round.log"
    with QueryJournal(path) as journal:
        for sql in ("SELECT 1", "SELECT 2"):
            journal.begin(sql)
            journal.end("PASS")
    corpus = CorpusSource.from_queries(queries_from_journal(path))
    assert list(corpus.iter_queries(_table(), seed=0)) == ["SELECT 1", "SELECT 2"]


def test_a_truncated_journal_still_parses_up_to_the_last_entry() -> None:
    """Truncation is exactly what a crash produces, and the last entry is the interesting one."""
    entries = parse_journal("-- header\nSELECT 1;\n-- => PASS\nSELECT 2;\n")
    assert [e.sql for e in entries] == ["SELECT 1", "SELECT 2"]
    assert entries[-1].annotation is None


def test_recorded_ddl_is_readable_but_never_replayed(tmp_path: Path) -> None:
    """The two things a recorded block must be at once.

    Present, because the equivalence is half of any repro. And *not* a query on the way back, because
    a journal replays as a corpus: replayed DDL would write to the database under test and diverge the
    two sides for a reason that is not a finding.
    """
    path = tmp_path / "round.log"
    with QueryJournal(path) as journal:
        journal.record("equivalence DDL", ["CREATE VIEW v AS SELECT * FROM t;"])
        journal.begin("SELECT 1")
        journal.end("PASS")

    text = path.read_text()
    assert "CREATE VIEW v AS SELECT * FROM t;" in text
    assert queries_from_journal(path) == ["SELECT 1"]
    assert CorpusSource.from_text(text).queries == ("SELECT 1",)


def test_seed_rows_include_the_awkward_cases_every_time() -> None:
    """An all-NULL row and a duplicate pair catch more broken rewrites than any amount of random
    data, so neither is left to chance. With ``c_pk``, keys stay unique while the payload still dupes."""
    table = Table(
        "t",
        [Column("c_int", IntegerType(), 1), Column("c_txt", VarcharType(), 2), Column("c_flag", BooleanType(), 3)],
    )
    rows = sample_rows(table, 8, seed=1)
    assert rows[0] == (None, None, None)
    assert rows[-1] == rows[1], "expected a duplicate pair"
    assert len(rows) >= 3

    keyed = Table(
        "t",
        [
            Column("c_pk", IntegerType(), 1, nullable=False),
            Column("c_int", IntegerType(), 2),
            Column("c_txt", VarcharType(), 3),
        ],
    )
    keyed_rows = sample_rows(keyed, 8, seed=1)
    pks = [row[0] for row in keyed_rows]
    assert pks == list(range(1, len(keyed_rows) + 1))
    assert keyed_rows[-1][1:] == keyed_rows[1][1:], "non-key payload should still duplicate"


# ---------------------------------------------------------------------------
# A round, and a whole run
# ---------------------------------------------------------------------------


def _rich_setup() -> tuple[DuckDBAdapter, Table, list[tuple[object, ...]], EquivalenceGenerator]:
    adapter = _adapter()
    table = adapter.rich_catalog("t")
    rows = sample_rows(table, 8, seed=7)
    generator = EquivalenceGenerator(
        adapter.equivalence_config(),
        predicate_source=RandomPredicateSource(),
        emitter=adapter.emitter(),
        extra_builders=adapter.extra_builders(),
    )
    return adapter, table, rows, generator


def test_a_round_builds_both_sides_and_compares() -> None:
    adapter, table, rows, generator = _rich_setup()
    queries = list(RandomSelectSource().iter_queries(table, seed=3, limit=8))
    outcome = run_round(adapter, generator, table, rows, queries, seed=11)
    assert outcome.setup_error is None
    assert outcome.object_comparison is not None and outcome.object_comparison.equal
    assert len(outcome.results) == len(queries)
    assert not outcome.findings


def test_the_round_hides_the_base_so_the_equivalent_can_take_its_name() -> None:
    """The three-name dance: the workload queries one name on both sides, which is what makes the
    query text identical and therefore the comparison meaningful."""
    adapter, table, rows, generator = _rich_setup()
    outcome = run_round(adapter, generator, table, rows, [], seed=5)
    assert any(hidden_base_name(table) in statement for statement in outcome.equivalent_statements)


def test_a_round_is_reproducible_from_its_seed() -> None:
    adapter, table, rows, generator = _rich_setup()
    first = run_round(adapter, generator, table, rows, [], seed=99).equivalent_statements
    second = run_round(adapter, generator, table, rows, [], seed=99).equivalent_statements
    assert first == second


def test_a_full_fuzz_run_finds_nothing_on_a_correct_engine(tmp_path: Path) -> None:
    """The end-to-end check, and a clean run is the meaningful outcome.

    It says three things at once: the generator built objects that really were equivalent, the
    queries were usable, and the comparison did not invent a difference. A run that reports
    findings against a correct engine would make every real finding arguable.
    """
    adapter = _adapter()
    table = adapter.rich_catalog("t")
    rows = sample_rows(table, 8, seed=3)
    report = run_fuzz(
        adapter,
        table,
        rows,
        query_source=CorpusSource.from_queries(["SELECT 1"] * 12),
        predicate_source=RandomPredicateSource(),
        rounds=6,
        seed=2024,
        workdir=tmp_path,
        verbose=False,
        scheduler=None,
    )
    assert report.rounds == 6
    assert report.queries_run == 72
    assert report.passed == 72, f"unexpected non-passes: {report.summary()}"
    assert not report.findings, [f.query for f in report.findings]
    assert report.discarded_not_equivalent == 0, "the generator produced a non-equivalent object"


def test_a_run_writes_a_journal_per_round(tmp_path: Path) -> None:
    adapter = _adapter()
    table = adapter.simple_catalog("t")
    run_fuzz(
        adapter,
        table,
        sample_rows(table, 4, seed=1),
        query_source=CorpusSource.from_queries(["SELECT 1"] * 3),
        rounds=2,
        seed=7,
        workdir=tmp_path,
        verbose=False,
        scheduler=None,
    )
    journals = sorted(tmp_path.rglob("round*.log"))
    assert len(journals) == 2
    assert "-- => PASS" in journals[0].read_text()


def test_a_round_journals_the_equivalence_it_generated(tmp_path: Path) -> None:
    """Every round, not only the ones that report something.

    A workload query on its own reproduces nothing — the object it ran against is the other half, and
    which round built which object is unrecoverable afterwards. So the DDL, the composition, and the
    equivalence check's own verdict go in as the round starts.
    """
    adapter, table, rows, generator = _rich_setup()
    path = tmp_path / "round.log"
    with QueryJournal(path) as journal:
        outcome = run_round(adapter, generator, table, rows, ["SELECT * FROM t"], seed=11, journal=journal)
    assert outcome.setup_error is None

    text = path.read_text()
    for statement in outcome.equivalent_statements:
        assert statement in text, "the DDL that builds the equivalent must be in the round's log"
    assert "==== equivalence:" in text, "the composition summary must be in the round's log"
    assert "equivalence check: OK" in text, "the object-level verdict must be in the round's log"


def test_a_corpus_run_replays_exactly_the_given_queries(tmp_path: Path) -> None:
    """The parity-replay path: fixed queries, so only the implementation varies."""
    adapter = _adapter()
    table = adapter.simple_catalog("t")
    corpus = CorpusSource.from_queries(["SELECT id FROM t", "SELECT COUNT(*) FROM t"])
    report = run_fuzz(
        adapter,
        table,
        sample_rows(table, 4, seed=1),
        query_source=corpus,
        rounds=2,
        seed=1,
        workdir=tmp_path,
        verbose=False,
    )
    assert report.queries_run == 4 and report.passed == 4


def test_a_report_separates_a_generator_defect_from_an_engine_finding() -> None:
    """A non-equivalent object must never be counted as an engine finding — it would be blaming the
    engine for our own bug."""
    diverged = ObjectComparison(
        rows=MultisetDiff(equal=False, only_in_base=[(("x",), 1)], only_in_other=[]),
        base_types=("BIGINT",),
        equivalent_types=("BIGINT",),
    )
    report = FuzzReport()
    findings = record_round(report, RoundOutcome(seed=1, object_comparison=diverged), 0)
    assert findings == []
    assert report.discarded_not_equivalent == 1 and not report.findings
    assert "DISCARDED" in report.summary()


def test_a_repro_script_rebuilds_both_databases() -> None:
    """A finding is only as useful as the file a human can run."""
    adapter, table, rows, generator = _rich_setup()
    outcome = run_round(adapter, generator, table, rows, [], seed=13)
    script = repro_script(adapter, table, rows, outcome, "SELECT * FROM t", kind="MISMATCH", session=[("mode", "strict")])
    assert "-- engine: duckdb" in script and "-- mode: strict" in script
    assert script.count("CREATE TABLE t (") == 2  # one per database
    assert hidden_base_name(table) in script
    assert script.rstrip().endswith("SELECT * FROM t;")


def test_a_repro_script_emits_base_side_fork_copies() -> None:
    """Same-base forks: the base half must recreate t0/t1/… or the workload cannot run."""
    adapter, table, rows, generator = _rich_setup()
    outcome = run_round(adapter, generator, table, rows, [], seed=13, forks=3)
    assert outcome.exposed_names == ("t0", "t1", "t2")
    script = repro_script(
        adapter, table, rows, outcome, "SELECT * FROM t0 NATURAL JOIN t1", kind="MISMATCH"
    )
    base_half, _, _ = script.partition("database 2: the equivalent")
    for name in ("t0", "t1", "t2"):
        assert f"CREATE TABLE {name} AS SELECT * FROM t;" in base_half, name


def test_a_repro_script_records_per_side_errors() -> None:
    """Error findings must carry the engine message in the header, not only in the round log."""
    adapter, table, rows, generator = _rich_setup()
    outcome = run_round(adapter, generator, table, rows, [], seed=13)
    script = repro_script(
        adapter,
        table,
        rows,
        outcome,
        "SELECT * FROM t",
        kind="ERROR",
        equivalent_error='(1064, "syntax near \'brien\'")',
    )
    assert "-- EQUIVALENT error: (1064, \"syntax near 'brien'\")" in script
    assert "-- BASE error:" not in script


def test_a_repro_script_logs_mismatch_results(tmp_path: Path) -> None:
    """Mismatch repros must show the differing rows — not only the query that produced them."""
    from eqgen.fuzz.report import Finding, write_finding

    adapter, table, rows, generator = _rich_setup()
    outcome = run_round(adapter, generator, table, rows, [], seed=13)
    only_base = [(("only-base",), 2)]
    only_equiv = [(("only-equiv",), 1), (("also-equiv",), 3)]
    script = repro_script(
        adapter,
        table,
        rows,
        outcome,
        "SELECT c_txt FROM t",
        kind="MISMATCH",
        only_in_base=only_base,
        only_in_equivalent=only_equiv,
    )
    assert "-- mismatch: 1 distinct only in base, 2 distinct only in equivalent" in script
    assert "-- ============ mismatch results ============" in script
    assert "-- only in base (1 distinct row(s), 2 row(s) counting multiplicity):" in script
    assert "--   ×2 ('only-base',)" in script
    assert "-- only in equivalent (2 distinct row(s), 4 row(s) counting multiplicity):" in script
    assert "--   ×1 ('only-equiv',)" in script
    assert "--   ×3 ('also-equiv',)" in script

    finding = Finding(
        kind="mismatch",
        round_number=7,
        seed=outcome.seed,
        query="SELECT c_txt FROM t",
        only_in_base=only_base,
        only_in_equivalent=only_equiv,
    )
    path = write_finding(tmp_path, adapter, table, rows, outcome, finding, index=0)
    text = path.read_text()
    assert "mismatch results" in text
    index = (tmp_path / "findings.txt").read_text()
    assert "only in base" in index
    assert "×2 ('only-base',)" in index
