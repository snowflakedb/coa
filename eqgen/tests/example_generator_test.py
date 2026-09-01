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

"""Step-1b gate: the example generator upholds its contracts, by construction.

The point of generating usable SQL rather than filtering it afterwards is that the rule
becomes a property of the code. This file is where that claim is checked — over a few
thousand queries, since a template space is only as safe as its least likely branch.

Every assertion here corresponds to a documented contract clause in
:mod:`eqgen.plugins`. When one fails, the generator has started producing workloads that
would yield mismatches with no engine bug behind them, which is the most expensive kind of
wrong this project can be.
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
from eqgen.generators.example_generator import RandomPredicateSource, RandomSelectSource, random_predicate
from eqgen.plugins import CorpusSource, PredicateSource, QuerySource

pytestmark = pytest.mark.unit

#: Wide enough that every template branch and every literal pool is reached.
_SEEDS = range(120)

#: Functions whose value is not a function of the input rows. A workload query containing
#: one cannot be compared across two executions at all.
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

#: Storage-dependent pseudo-columns: a base table and its view disagree on these for
#: identical rows, so a query referencing one is not comparable.
_PSEUDO_COLUMNS = ("rowid", "ctid", "oid", "_row_id")


def _table() -> Table:
    """A catalog spanning the vocabulary, including the two columns that carry traps: a
    ``DOUBLE`` (unsafe to aggregate) and a scaled decimal (unsafe as a parity key)."""
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


def _all_queries() -> list[str]:
    source = RandomSelectSource()
    return [q for seed in _SEEDS for q in source.iter_queries(_table(), seed=seed, limit=30)]


def _all_predicates() -> list[str]:
    return [p for seed in _SEEDS if (p := random_predicate(_table(), seed=seed)) is not None]


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_the_example_sources_satisfy_the_protocols() -> None:
    assert isinstance(RandomSelectSource(), QuerySource)
    assert isinstance(RandomPredicateSource(), PredicateSource)
    assert isinstance(CorpusSource.from_queries(["SELECT 1"]), QuerySource)


# ---------------------------------------------------------------------------
# QuerySource contract
# ---------------------------------------------------------------------------


def test_queries_are_read_only_selects() -> None:
    for query in _all_queries():
        assert query.startswith("SELECT "), query
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|MERGE|TRUNCATE)\b", query, re.I), query


def test_queries_never_limit_or_offset() -> None:
    """Without a total order these select an arbitrary subset, and the two sides are free
    to pick differently — a mismatch with no bug behind it."""
    for query in _all_queries():
        assert not re.search(r"\b(LIMIT|OFFSET|FETCH\s+FIRST|TOP)\b", query, re.I), query


def test_queries_reference_only_the_base_table() -> None:
    """The equivalent is exposed under the base's name in its own database, so a reference
    to anything else resolves on one side only. ``sub`` is the derived-table alias."""
    for query in _all_queries():
        referenced = set(re.findall(r"\bFROM\s+([A-Za-z_][A-Za-z0-9_]*)", query, re.I))
        assert referenced <= {"t"}, f"{referenced} in {query}"


def test_queries_contain_no_nondeterministic_functions() -> None:
    for query in _all_queries():
        lowered = query.lower()
        for name in _NONDETERMINISTIC:
            assert f"{name}(" not in lowered, f"{name} in {query}"


def test_queries_never_reference_physical_pseudo_columns() -> None:
    for query in _all_queries():
        lowered = query.lower()
        for name in _PSEUDO_COLUMNS:
            assert not re.search(rf"\b{name}\b", lowered), f"{name} in {query}"


def test_queries_never_sum_or_average_a_double_column() -> None:
    """Floating-point addition is not associative, so the last bit can differ between two
    plans that are otherwise the same, and comparing exactly calls that a
    mismatch. ``COUNT``/``MIN``/``MAX`` over a double are order-independent and fine."""
    for query in _all_queries():
        assert not re.search(r"\b(SUM|AVG)\s*\(\s*c_dbl\s*\)", query, re.I), query


def test_double_columns_are_still_exercised() -> None:
    """Guard against the previous test passing for the wrong reason — a generator that
    simply never touched the double column would satisfy it vacuously."""
    queries = _all_queries()
    assert any("c_dbl" in q for q in queries)
    assert any(re.search(r"\b(MIN|MAX|COUNT)\s*\(\s*c_dbl\s*\)", q, re.I) for q in queries)


def test_grouped_queries_select_only_grouped_or_aggregated_columns() -> None:
    """A bare column alongside an aggregate without being grouped is an error in
    PostgreSQL and, worse, silently order-dependent in some engines."""
    for query in _all_queries():
        match = re.match(r"SELECT (.+?) FROM .*?GROUP BY (.+?)(?: HAVING| ORDER BY|$)", query)
        if match is None:
            continue
        items = [item.strip() for item in match.group(1).split(", ")]
        grouped = {col.strip() for col in match.group(2).split(",")}
        for item in items:
            is_aggregate = re.match(r"(COUNT|MIN|MAX|SUM|AVG)\s*\(", item, re.I)
            assert is_aggregate or item in grouped, f"{item!r} neither grouped nor aggregated in {query}"


def test_order_by_only_references_selected_columns() -> None:
    """Otherwise it is illegal alongside ``DISTINCT``."""
    for query in _all_queries():
        match = re.match(r"SELECT (?:DISTINCT )?(.+?) FROM .*?ORDER BY (.+)$", query)
        if match is None:
            continue
        selected = {item.strip().split(" AS ")[0] for item in match.group(1).split(", ")}
        for key in match.group(2).split(", "):
            assert key.replace(" DESC", "").strip() in selected, f"{key!r} not selected in {query}"


def test_query_generation_is_deterministic_per_seed() -> None:
    source = RandomSelectSource()
    first = list(source.iter_queries(_table(), seed=7, limit=10))
    second = list(source.iter_queries(_table(), seed=7, limit=10))
    assert first == second
    assert list(source.iter_queries(_table(), seed=8, limit=10)) != first


def test_query_generation_does_not_disturb_the_global_rng() -> None:
    """The equivalence generator seeds the *global* RNG so a round is reproducible from a
    seed. A source that drew from it would shift that stream."""
    import random

    random.seed(1234)
    expected = [random.random() for _ in range(5)]

    random.seed(1234)
    list(RandomSelectSource().iter_queries(_table(), seed=99, limit=20))
    assert [random.random() for _ in range(5)] == expected


def test_limit_caps_the_query_count() -> None:
    assert len(list(RandomSelectSource().iter_queries(_table(), seed=1, limit=3))) == 3


def test_a_table_with_no_columns_yields_nothing() -> None:
    assert list(RandomSelectSource().iter_queries(Table("t", []), seed=1)) == []


# ---------------------------------------------------------------------------
# PredicateSource contract
# ---------------------------------------------------------------------------


def test_predicates_are_row_local() -> None:
    """No aggregates (need ``GROUP BY``), no window functions (``WHERE``-illegal), no
    subqueries (would not survive being embedded in three separate branch bodies)."""
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
    """They are substituted into queries whose source may be an alias or a derived table."""
    known = {c.get_column_name() for c in _table().get_column_list()}
    for predicate in _all_predicates():
        for reference in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\.", predicate):
            pytest.fail(f"qualified reference {reference!r} in {predicate}")
        for name in re.findall(r"\bc_[a-z]+\b", predicate):
            assert name in known, f"unknown column {name!r} in {predicate}"


def test_predicate_subexpressions_are_parenthesised() -> None:
    """The caller negates and null-tests whatever it is handed. An unparenthesised ``NOT`` over a
    top-level ``AND`` binds to the wrong operand, and rows then fall out of every branch —
    a defect this project has already been bitten by once."""
    for predicate in _all_predicates():
        for operator in (" AND ", " OR "):
            for part in predicate.split(operator):
                stripped = part.strip()
                if operator.strip() in stripped:
                    assert stripped.startswith("(") or stripped.startswith("NOT ("), predicate


def test_predicates_are_balanced() -> None:
    for predicate in _all_predicates():
        assert predicate.count("(") == predicate.count(")"), predicate


def test_predicates_escape_embedded_quotes() -> None:
    """``o'brien`` is in the literal pool precisely so this path is exercised."""
    quoted = [p for p in _all_predicates() if "o''brien" in p]
    assert quoted, "expected the embedded-quote literal to be reached"
    for predicate in quoted:
        assert "'o''brien'" in predicate


def test_predicate_generation_is_deterministic_per_seed() -> None:
    assert random_predicate(_table(), seed=5) == random_predicate(_table(), seed=5)


def test_predicate_declines_for_a_table_with_no_columns() -> None:
    assert random_predicate(Table("t", []), seed=1) is None


def test_predicates_reach_all_three_truth_values_structurally() -> None:
    """Null tests and comparisons against NULL-able columns are what make the third
    branch reachable; a generator emitting only total predicates would leave it dead."""
    predicates = _all_predicates()
    assert any("IS NULL" in p for p in predicates)
    assert any("IS NOT NULL" in p for p in predicates)


# ---------------------------------------------------------------------------
# CorpusSource
# ---------------------------------------------------------------------------


def test_corpus_source_parses_semicolon_separated_text() -> None:
    corpus = CorpusSource.from_text("SELECT 1;\n-- a comment\nSELECT 2;\n\n")
    assert list(corpus.iter_queries(_table(), seed=0)) == ["SELECT 1", "SELECT 2"]


def test_corpus_replay_ignores_seed_and_table() -> None:
    """Replay must not vary with either, or a saved finding would not reproduce."""
    corpus = CorpusSource.from_queries(["SELECT 1", "SELECT 2"])
    assert list(corpus.iter_queries(_table(), seed=1)) == list(corpus.iter_queries(Table("other", []), seed=2))


def test_corpus_honours_limit() -> None:
    corpus = CorpusSource.from_queries(["SELECT 1", "SELECT 2", "SELECT 3"])
    assert list(corpus.iter_queries(_table(), seed=0, limit=2)) == ["SELECT 1", "SELECT 2"]


def test_shuffled_corpus_is_seed_deterministic_and_covers_all() -> None:
    queries = tuple(f"SELECT {i}" for i in range(20))
    corpus = CorpusSource(queries, shuffle=True)
    a = list(corpus.iter_queries(_table(), seed=7))
    b = list(corpus.iter_queries(_table(), seed=7))
    c = list(corpus.iter_queries(_table(), seed=8))
    assert a == b
    assert a != c
    assert sorted(a) == sorted(queries)
    assert list(corpus.iter_queries(_table(), seed=7, limit=3)) == a[:3]


# ---------------------------------------------------------------------------
# The strongest check: a real engine accepts every generated statement
# ---------------------------------------------------------------------------
#
# The assertions above are structural, and a regex can only rule out what it thinks to
# look for. Executing the output settles validity outright: if DuckDB parses, plans and
# runs 3,000+ generated queries and every generated predicate over a table holding NULLs,
# empty strings, negatives and trailing spaces, then the grammar produces real SQL rather
# than something that merely looks like it.
#
# In-process and in-memory, so this is hermetic and fast (~1s) despite touching an engine.


def _duckdb_connection() -> object:
    duckdb = pytest.importorskip("duckdb")
    connection = duckdb.connect()
    connection.execute(
        """
        CREATE TABLE t (
            c_int INTEGER, c_big DECIMAL(38, 0), c_dec DECIMAL(10, 2), c_dbl DOUBLE,
            c_txt VARCHAR, c_chr VARCHAR, c_flag BOOLEAN, c_date DATE, c_ts TIMESTAMP
        )
        """
    )
    # Adversarial rows: a NULL in every column (so three-valued logic is live), an empty
    # string and a trailing-space string (blank-padding), and negatives (MOD sign).
    connection.execute(
        """
        INSERT INTO t VALUES
            (1, 2, 3.50, 1.5, 'a', 'b', TRUE, '2024-01-15', '2024-01-15 12:34:56'),
            (NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL),
            (-1, -2, -3.50, -1.5, '', 'trailing ', FALSE, '1999-12-31', '1999-12-31 23:59:59')
        """
    )
    return connection


def test_every_generated_query_runs_on_duckdb() -> None:
    connection = _duckdb_connection()
    failures: list[tuple[str, str]] = []
    for query in _all_queries():
        try:
            connection.execute(query).fetchall()  # type: ignore[attr-defined]
        except Exception as exc:  # - any engine rejection is a generator defect
            failures.append((query, str(exc).splitlines()[0]))
    assert not failures, f"{len(failures)} query/queries rejected, e.g. {failures[:3]}"


def test_every_generated_predicate_runs_on_duckdb() -> None:
    connection = _duckdb_connection()
    failures: list[tuple[str, str]] = []
    for predicate in _all_predicates():
        try:
            connection.execute(f"SELECT 1 FROM t WHERE {predicate}").fetchall()  # type: ignore[attr-defined]
        except Exception as exc:  # - any engine rejection is a generator defect
            failures.append((predicate, str(exc).splitlines()[0]))
    assert not failures, f"{len(failures)} predicate(s) rejected, e.g. {failures[:3]}"


def test_the_template_space_is_actually_covered() -> None:
    """Guards every structural test above against passing vacuously: a generator that
    never emitted a ``GROUP BY`` would satisfy the grouping rule trivially."""
    queries = _all_queries()
    assert sum("GROUP BY" in q for q in queries) > 100
    assert sum("ORDER BY" in q for q in queries) > 100
    assert sum("DISTINCT" in q for q in queries) > 50
    assert sum("AS sub" in q for q in queries) > 50
    assert sum("HAVING" in q for q in queries) > 20
