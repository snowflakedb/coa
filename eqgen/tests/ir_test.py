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

"""The expression nodes: how they are written, and — more importantly — that the rewrites built from
them really do return the same rows.

Two halves. The first pins how nodes render, including the one invariant that has already
cost this project a real defect (parenthesisation of an opaque predicate). The second
*runs* them against a live engine over awkward rows, which is the only
way to actually establish them:

* ``p`` / ``NOT p`` / ``p IS NULL`` covers rows **exactly** — every row in one branch,
  none in two.
* ``determined_true(p)`` is TRUE for every row, whatever ``p`` does.
* ``determined_false(p)`` is FALSE for every row.

These hold for *any* deterministic boolean, so the test drives them with predicates from
the example generator rather than hand-picked ones — including predicates that are NULL for
some rows, which is where a naive ``p OR NOT p`` fails.
"""

from __future__ import annotations

import pytest

from eqgen.core.catalog import Column, Table
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
from eqgen.generators.example_generator import random_predicate
from eqgen.ir import expr as E
from eqgen.ir.expr import WindowFrameKind, WindowFunction
from eqgen.ir.render import DEFAULT_SPELLING, PostgresSpelling, Spelling, UnsupportedForDialect, render

pytestmark = pytest.mark.unit

_INT = IntegerType()
_TXT = VarcharType()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_column_refs_render_bare_or_qualified() -> None:
    assert render(E.col("c", _INT)) == "c"
    assert render(E.qualified_col("l", "c", _INT)) == "l.c"


def test_the_parity_predicates_the_partitioning_builders_emit() -> None:
    key = E.col("c_int", _INT)
    even = E.eq(E.mod(key, 2), E.int_lit(0))
    odd = E.or_(E.ne(E.mod(key, 2), E.int_lit(0)), E.is_null(key))
    assert render(even) == "MOD(c_int, 2) = 0"
    assert render(odd) == "(MOD(c_int, 2) <> 0) OR (c_int IS NULL)"


def test_typed_null_uses_standard_cast() -> None:
    """``CAST(NULL AS t)`` rather than PostgreSQL's ``NULL::t``: every engine takes it, and
    portability costs nothing here."""
    assert render(E.typed_null(_TXT)) == "CAST(NULL AS VARCHAR)"


def test_generated_predicates_are_always_parenthesised() -> None:
    """The text may contain a top-level ``AND``/``OR``, and the caller applies
    ``NOT`` and ``IS NULL`` to whatever it is handed — without brackets the operator binds
    to one conjunct, the split stops being exhaustive, and rows vanish from every branch."""
    predicate = E.generated_predicate("a > 1 OR b IS NULL")
    assert render(predicate) == "(a > 1 OR b IS NULL)"
    assert render(E.not_(predicate)) == "NOT (a > 1 OR b IS NULL)"
    assert render(E.is_null(predicate)) == "(a > 1 OR b IS NULL) IS NULL"


def test_compound_operands_are_bracketed_but_atoms_are_not() -> None:
    assert render(E.and_(E.eq(E.col("a", _INT), E.int_lit(1)), E.is_null(E.col("b", _INT)))) == "(a = 1) AND (b IS NULL)"
    assert render(E.eq(E.col("a", _INT), E.int_lit(1))) == "a = 1"


def test_case_renders_with_a_typed_else() -> None:
    rendered = render(E.case_when(E.is_null(E.col("a", _INT)), E.col("c", _TXT), E.typed_null(_TXT), _TXT))
    assert rendered == "CASE WHEN a IS NULL THEN c ELSE CAST(NULL AS VARCHAR) END"


def test_an_unknown_node_raises_rather_than_guessing() -> None:
    class Rogue(E.ExpressionNode):
        @property
        def data_type(self) -> IntegerType:
            return _INT

    with pytest.raises(UnsupportedForDialect):
        render(Rogue())


def test_generated_predicate_rejects_empty_text() -> None:
    with pytest.raises(AssertionError):
        E.generated_predicate("   ")


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data_type", "expected"),
    [
        (IntegerType(), "INTEGER"),
        (NumericType(), "NUMERIC"),
        (NumericType(38, 0), "NUMERIC(38, 0)"),
        (NumericType(10, 2), "NUMERIC(10, 2)"),
        (DoubleType(), "DOUBLE PRECISION"),
        (VarcharType(), "VARCHAR"),
        (VarcharType(20), "VARCHAR(20)"),
        (CharType(3), "CHAR(3)"),
        (TextType(), "TEXT"),
        (BooleanType(), "BOOLEAN"),
        (DateType(), "DATE"),
        (TimestampType(), "TIMESTAMP"),
        (JsonbType(), "JSONB"),
        (UuidType(), "UUID"),
        (Int4RangeType(), "INT4RANGE"),
    ],
)
def test_postgres_type_names(data_type: object, expected: str) -> None:
    """Subclasses must be tested before their bases in ``type_sql`` — ``IntegerType`` is a
    ``NumericType``, ``CharType``/``TextType`` are ``VarcharType`` — so each mapping is
    pinned rather than left to isinstance ordering."""
    assert PostgresSpelling().type_sql(data_type) == expected  # type: ignore[arg-type]


def test_a_dialect_overrides_only_what_it_spells_differently() -> None:
    """The extension shape: DuckDB's whole divergence from the reference is type names."""

    class ToySpelling(PostgresSpelling):
        def type_sql(self, data_type: SqlType) -> str:
            if isinstance(data_type, IntegerType):
                return "BIGINT"
            return super().type_sql(data_type)

    assert ToySpelling().type_sql(IntegerType()) == "BIGINT"
    assert ToySpelling().type_sql(TextType()) == "TEXT"
    assert ToySpelling().expr(E.col("c", _INT)) == "c"


def test_spelling_is_abstract_so_a_dialect_must_answer_type_sql() -> None:
    with pytest.raises(TypeError):
        Spelling()  # type: ignore[abstract]


def test_mod_spelling_is_overridable() -> None:
    """An engine writing ``x % n`` overrides one method, not the renderer."""

    class PercentSpelling(PostgresSpelling):
        def mod_sql(self, node: E.Mod) -> str:
            return f"{self.expr(node.operand)} % {node.modulus}"

    assert PercentSpelling().expr(E.mod(E.col("c", _INT), 2)) == "c % 2"


# ---------------------------------------------------------------------------
# The identities, executed
# ---------------------------------------------------------------------------


def _table() -> Table:
    return Table(
        "t",
        [
            Column("c_int", IntegerType(), 1),
            Column("c_dec", NumericType(10, 2), 2),
            Column("c_dbl", DoubleType(), 3),
            Column("c_txt", VarcharType(), 4),
            Column("c_flag", BooleanType(), 5),
            Column("c_date", DateType(), 6),
        ],
    )


def _connection() -> object:
    """A live table whose rows are chosen to make three-valued logic bite: a full-NULL row,
    negatives (so ``MOD`` sign matters), an empty string, and duplicates."""
    duckdb = pytest.importorskip("duckdb")
    connection = duckdb.connect()
    connection.execute(
        "CREATE TABLE t (c_int INTEGER, c_dec DECIMAL(10, 2), c_dbl DOUBLE, c_txt VARCHAR, c_flag BOOLEAN, c_date DATE)"
    )
    connection.execute(
        """
        INSERT INTO t VALUES
            (1, 1.50, 1.5, 'a', TRUE, '2024-01-15'),
            (2, -2.50, -1.5, '', FALSE, '1999-12-31'),
            (-1, 0.00, 0.0, 'abc', NULL, '2030-06-01'),
            (0, NULL, NULL, NULL, TRUE, NULL),
            (NULL, NULL, NULL, NULL, NULL, NULL),
            (1, 1.50, 1.5, 'a', TRUE, '2024-01-15')
        """
    )
    return connection


def _predicates(count: int = 60) -> list[E.ExpressionNode]:
    """Predicates from the example generator, wrapped as opaque leaves — the same path a
    third-party source takes."""
    table = _table()
    out: list[E.ExpressionNode] = []
    for seed in range(count):
        text = random_predicate(table, seed=seed)
        if text is not None:
            out.append(E.generated_predicate(text))
    assert out, "expected the example generator to produce predicates"
    return out


def _count(connection: object, where: str) -> int:
    rows = connection.execute(f"SELECT COUNT(*) FROM t WHERE {where}").fetchall()  # type: ignore[attr-defined]
    return int(rows[0][0])


def test_tlp_three_way_split_partitions_rows_exactly() -> None:
    """``filter(R, p) ⊎ filter(R, NOT p) ⊎ filter(R, p IS NULL) ≡ R`` for any deterministic
    ``p`` — all of them, none twice. This is what the row split is built on, and the reason
    it needs three branches rather than two."""
    connection = _connection()
    total = _count(connection, "TRUE")
    for predicate in _predicates():
        branches = [predicate, E.not_(predicate), E.is_null(predicate)]
        counts = [_count(connection, DEFAULT_SPELLING.expr(branch)) for branch in branches]
        assert sum(counts) == total, f"not exhaustive: {counts} vs {total} for {render(predicate)}"
        # Disjointness: no row satisfies two branches at once.
        for i in range(len(branches)):
            for j in range(i + 1, len(branches)):
                overlap = f"({DEFAULT_SPELLING.expr(branches[i])}) AND ({DEFAULT_SPELLING.expr(branches[j])})"
                assert _count(connection, overlap) == 0, f"branches {i},{j} overlap for {render(predicate)}"


def test_determined_true_is_true_for_every_row() -> None:
    """``p OR NOT p OR p IS NULL``. A naive ``p OR NOT p`` would leave NULL rows out, which
    is exactly what the third disjunct is for."""
    connection = _connection()
    total = _count(connection, "TRUE")
    for predicate in _predicates():
        rendered = DEFAULT_SPELLING.expr(E.determined_true(predicate))
        assert _count(connection, rendered) == total, f"not a tautology: {render(predicate)}"


def test_determined_false_is_false_for_every_row() -> None:
    connection = _connection()
    for predicate in _predicates():
        rendered = DEFAULT_SPELLING.expr(E.determined_false(predicate))
        assert _count(connection, rendered) == 0, f"not a contradiction: {render(predicate)}"


def test_conjoining_a_determined_true_preserves_a_filter() -> None:
    """``(p OR NOT p OR p IS NULL) AND q`` selects the same rows as ``q`` alone."""
    connection = _connection()
    filter_expr = E.eq(E.mod(E.col("c_int", _INT), 2), E.int_lit(0))
    baseline = _count(connection, DEFAULT_SPELLING.expr(filter_expr))
    for predicate in _predicates(20):
        combined = E.and_(E.determined_true(predicate), filter_expr)
        assert _count(connection, DEFAULT_SPELLING.expr(combined)) == baseline


def test_the_naive_two_way_split_really_does_lose_rows() -> None:
    """Guards the three tests above against passing for trivial reasons: if ``p OR NOT p``
    were already total, the third branch would be decoration rather than the fix."""
    connection = _connection()
    total = _count(connection, "TRUE")
    lossy = [p for p in _predicates() if _count(connection, f"({render(p)}) OR (NOT ({render(p)}))") < total]
    assert lossy, "expected at least one predicate that is NULL for some row"


# ---------------------------------------------------------------------------
# Window calls
# ---------------------------------------------------------------------------


def test_a_window_call_renders_its_clauses_in_order() -> None:
    column = E.col("c_int", IntegerType())
    plain = E.window_over(WindowFunction.MAX, column, (column,), IntegerType())
    assert render(plain) == "MAX(c_int) OVER (PARTITION BY c_int)"

    framed = E.window_over(
        WindowFunction.FIRST_VALUE,
        column,
        (column,),
        IntegerType(),
        order_by=(column,),
        frame_spec=E.frame(WindowFrameKind.ROWS),
    )
    assert render(framed) == (
        "FIRST_VALUE(c_int) OVER (PARTITION BY c_int ORDER BY c_int ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)"
    )


def test_row_number_takes_no_argument() -> None:
    """``ROW_NUMBER()`` has empty parentheses, and counts rather than returning a column."""
    column = E.col("c_int", IntegerType())
    assert render(E.row_number((column,))) == "ROW_NUMBER() OVER (ORDER BY c_int)"


def test_a_window_frame_is_not_a_value() -> None:
    """A frame is a clause. Asking it for a type is a mistake worth failing on rather than
    answering with something plausible."""
    with pytest.raises(TypeError):
        E.frame(WindowFrameKind.ROWS).data_type
