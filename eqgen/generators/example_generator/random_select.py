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

"""A small example query and predicate source, so the fuzzer runs with nothing else installed.

**Not the project's query generator.** It is here so there is a worked example of the two protocols
in :mod:`eqgen.plugins`, and so ``pip install duckdb`` is enough to try the tool. Replace it with
SQLancer, sqlsmith, a captured trace, or your own.

Small is enough because the *object* is what varies, not the query. Both sides hold the same rows,
so a query only has to reach them by a different route on each side, and a handful of templates does
that.

Nothing is generated and then filtered out. There is simply no rule below that produces ``LIMIT``,
a function whose answer changes per call, or a sum over a float — the three things that would make a
query give different answers on two correct engines.
"""

from __future__ import annotations

import random
from typing import Iterator, Optional, Sequence

from eqgen.core.catalog import Column, Table
from eqgen.core.types import (
    BooleanType,
    DateType,
    DoubleType,
    Int4RangeType,
    JsonbType,
    NumericType,
    SqlType,
    TimestampType,
    TypeProperty,
    UuidType,
    VarcharType,
)

#: Comparison operators. Portable for every type in the vocabulary, and total enough that
#: any of them can appear against any column.
_COMPARISONS = ("=", "<>", "<", "<=", ">", ">=")

#: String literal pool. The empty string and an embedded quote are here on purpose: both
#: are ordinary values that a careless escaping path turns into a syntax error, and a
#: trailing-space value probes blank-padding collations.
_STRINGS = ("", "a", "abc", "Zed", "o'brien", "trailing ")

#: How deep a predicate may nest. Two levels give ``(a AND b) OR NOT c`` — enough shape to
#: exercise three-valued logic without producing predicates nobody can read in a repro.
_MAX_PREDICATE_DEPTH = 2


def _quote(text: str) -> str:
    """A single-quoted SQL string literal, with embedded quotes doubled."""
    escaped = text.replace("'", "''")
    return f"'{escaped}'"


def _literal(dtype: SqlType, rng: random.Random) -> str:
    """A literal of *dtype*, spelled portably.

    Temporal and string literals are plain quoted strings rather than typed constructors
    (``DATE '…'``): in a comparison against a column of the right type every engine here
    coerces them, and it keeps the text free of per-dialect constructor syntax.
    """
    if isinstance(dtype, BooleanType):
        return rng.choice(("TRUE", "FALSE"))
    if isinstance(dtype, DoubleType):
        return rng.choice(("0.0", "1.5", "-0.25", "1000.125"))
    if isinstance(dtype, NumericType):
        scale = dtype.get_scale()
        if scale:  # a scaled decimal wants a scaled literal
            return rng.choice(("0.00", "12.34", "-5.50", "999.99"))
        # Negatives and zero matter to the row-splitting builders: MOD keeps the
        # dividend's sign in several engines, so -1 % 2 = -1, not 1.
        return str(rng.choice((-7, -1, 0, 1, 2, 3, 42, 1000)))
    if isinstance(dtype, DateType):
        return _quote(rng.choice(("2024-01-15", "1999-12-31", "2030-06-01")))
    if isinstance(dtype, TimestampType):
        return _quote(rng.choice(("2024-01-15 12:34:56", "1999-12-31 23:59:59")))
    if isinstance(dtype, JsonbType):
        return rng.choice(("'{}'::jsonb", "'[]'::jsonb", '\'{"a": 1}\'::jsonb'))
    if isinstance(dtype, UuidType):
        return rng.choice(
            (
                "'00000000-0000-0000-0000-000000000000'::uuid",
                "'550e8400-e29b-41d4-a716-446655440000'::uuid",
            )
        )
    if isinstance(dtype, Int4RangeType):
        return rng.choice(("'empty'::int4range", "'[1,10)'::int4range", "'[0,100]'::int4range"))
    return _quote(rng.choice(_STRINGS))


def _is_exact_numeric(dtype: SqlType) -> bool:
    """Exact numeric — safe to ``SUM``/``AVG`` over.

    ``DoubleType`` is excluded and that exclusion is the whole reason this helper exists:
    floating-point addition is not associative, so a sum over a ``DOUBLE`` column can
    differ in the last bit between two plans that are otherwise the same. The comparison
    compares for exact equality, so it reports that as a mismatch — a false finding
    produced entirely by the workload.
    """
    return isinstance(dtype, NumericType) and not isinstance(dtype, DoubleType)


def _same_family(left: SqlType, right: SqlType) -> bool:
    """Whether two columns can be compared to each other without a cast.

    Comparisons are kept inside a family (numeric with numeric, string with string) so the
    engine never has to guess. A cross-family comparison is not *wrong* — it usually errors
    — but an errored query is a dropped query, and dropping them at generation time costs
    nothing.
    """
    for family in (NumericType, VarcharType, BooleanType, DateType, TimestampType):
        if isinstance(left, family) and isinstance(right, family):
            return True
    return False


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------


def _atom(columns: Sequence[Column], rng: random.Random) -> str:
    """One comparison or null test over a single column."""
    column = rng.choice(list(columns))
    name, dtype = column.get_column_name(), column.get_data_type()
    if not (dtype.get_properties() & TypeProperty.ORDERABLE):
        choice = rng.random()
        if choice < 0.4:
            return f"{name} IS NULL"
        if choice < 0.7:
            return f"{name} IS NOT NULL"
        return f"{name} = {_literal(dtype, rng)}"
    choice = rng.random()
    if choice < 0.15:
        return f"{name} IS NULL"
    if choice < 0.25:
        return f"{name} IS NOT NULL"
    if choice < 0.4:
        peers = [c for c in columns if c.get_column_name() != name and _same_family(dtype, c.get_data_type())]
        if peers:
            other = rng.choice(peers).get_column_name()
            return f"{name} {rng.choice(_COMPARISONS)} {other}"
    return f"{name} {rng.choice(_COMPARISONS)} {_literal(dtype, rng)}"


def _predicate(columns: Sequence[Column], rng: random.Random, depth: int = 0) -> str:
    """A boolean predicate, recursively combined and fully parenthesised.

    Every sub-expression is wrapped. That is not cosmetic: an unparenthesised ``NOT`` or
    ``IS NULL`` applied to a predicate with a top-level ``AND``/``OR`` binds to the wrong
    operand, and since the caller negates and null-tests whatever it is given, the split stops
    being exhaustive and rows vanish from every branch.
    """
    if depth >= _MAX_PREDICATE_DEPTH or rng.random() < 0.45:
        return _atom(columns, rng)
    if rng.random() < 0.2:
        return f"NOT ({_predicate(columns, rng, depth + 1)})"
    operator = rng.choice(("AND", "OR"))
    left = _predicate(columns, rng, depth + 1)
    right = _predicate(columns, rng, depth + 1)
    return f"({left}) {operator} ({right})"


def random_predicate(table: Table, *, seed: int) -> Optional[str]:
    """A deterministic boolean predicate over *table*'s columns, as SQL text.

    Shared by both plugin implementations below, because "random predicate over these
    columns" is exactly what each of them needs — the row split and the ``CASE`` wrapper want one
    in a ``WHERE`` or a
    ``CASE`` condition, and a workload query wants one in its own ``WHERE``.

    Returns ``None`` for a table with no columns, which is the only case it cannot serve.
    """
    columns = table.get_column_list()
    if not columns:
        return None
    return _predicate(columns, random.Random(seed))


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def _projection(columns: Sequence[Column], rng: random.Random) -> list[str]:
    """A non-empty subset of column names, in declaration order."""
    keep = [c.get_column_name() for c in columns if rng.random() < 0.7]
    return keep or [columns[0].get_column_name()]


def _aggregates(columns: Sequence[Column], rng: random.Random) -> list[str]:
    """Aliased aggregate items. Aliases just make a saved repro readable — the comparison uses
    positionally — but an unaliased aggregate makes a saved ``.sql`` much harder to read."""
    items = ["COUNT(*) AS n"]
    for i, column in enumerate(columns):
        if rng.random() >= 0.4:
            continue
        name, dtype = column.get_column_name(), column.get_data_type()
        # COUNT takes anything. MIN/MAX do not take a boolean on every engine — PostgreSQL has no
        # min(boolean), DuckDB does — and a query the base rejects is a skipped query, i.e. lost
        # coverage rather than a finding. Not generating it beats generating and discarding it.
        functions = ["COUNT"] if isinstance(dtype, BooleanType) else ["COUNT", "MIN", "MAX"]
        if _is_exact_numeric(dtype):
            functions += ["SUM", "AVG"]
        items.append(f"{rng.choice(functions)}({name}) AS a{i}")
    return items


def _where(columns: Sequence[Column], rng: random.Random) -> str:
    return f" WHERE {_predicate(columns, rng)}" if rng.random() < 0.65 else ""


def _order_by(selected: Sequence[str], rng: random.Random) -> str:
    """``ORDER BY`` over already-selected columns only.

    Ordering is *encouraged*: rows are compared ignoring order, so it cannot change the
    answer, but it does change the plan, which is the point. Restricting it to selected
    columns keeps it legal alongside ``DISTINCT``.
    """
    if not selected or rng.random() >= 0.4:
        return ""
    keys = rng.sample(list(selected), k=rng.randint(1, min(2, len(selected))))
    directions = [f"{key}{rng.choice(('', ' DESC'))}" for key in keys]
    return " ORDER BY " + ", ".join(directions)


def _grouped_query(relation: str, columns: Sequence[Column], rng: random.Random) -> str:
    group_cols = _projection(columns, rng)
    items = group_cols + _aggregates(columns, rng)
    having = " HAVING COUNT(*) >= 1" if rng.random() < 0.3 else ""
    return (
        f"SELECT {', '.join(items)} FROM {relation}"
        f"{_where(columns, rng)} GROUP BY {', '.join(group_cols)}{having}"
        f"{_order_by(group_cols, rng)}"
    )


def _flat_query(relation: str, columns: Sequence[Column], rng: random.Random) -> str:
    distinct = "DISTINCT " if rng.random() < 0.25 else ""
    selected = _projection(columns, rng)
    return f"SELECT {distinct}{', '.join(selected)} FROM {relation}{_where(columns, rng)}{_order_by(selected, rng)}"


def _wrapped_query(relation: str, columns: Sequence[Column], rng: random.Random) -> str:
    """A derived-table wrap. Same rows, one more plan level for the optimizer to fold."""
    selected = _projection(columns, rng)
    inner = f"SELECT {', '.join(selected)} FROM {relation}{_where(columns, rng)}"
    return f"SELECT {', '.join(selected)} FROM ({inner}) AS sub{_order_by(selected, rng)}"


def _aggregate_only_query(relation: str, columns: Sequence[Column], rng: random.Random) -> str:
    return f"SELECT {', '.join(_aggregates(columns, rng))} FROM {relation}{_where(columns, rng)}"


def _join_key(columns: Sequence[Column]) -> str:
    """Prefer ``c_pk`` for fork joins; otherwise the first column."""
    names = [c.get_column_name() for c in columns]
    if "c_pk" in names:
        return "c_pk"
    return names[0]


def _join_query(left: str, right: str, columns: Sequence[Column], rng: random.Random) -> str:
    """Self-join across two exposed fork names that share the same signature."""
    key = _join_key(columns)
    selected = _projection(columns, rng)
    # Sometimes project only the left alias so UNIQUE/PK join removal can fire.
    if rng.random() < 0.45:
        items = [f"a.{name}" for name in selected]
    else:
        # Avoid duplicate output names when both sides contribute the same column.
        items = [f"a.{name}" for name in selected]
        extra = [c.get_column_name() for c in columns if c.get_column_name() not in selected]
        if extra and rng.random() < 0.5:
            items.append(f"b.{rng.choice(extra)} AS b_{rng.choice(extra)}")
    join_kw = "LEFT JOIN" if rng.random() < 0.35 else "JOIN"
    where = ""
    if rng.random() < 0.4:
        column = rng.choice(list(columns))
        name = column.get_column_name()
        if rng.random() < 0.5:
            where = f" WHERE a.{name} IS NOT NULL"
        else:
            where = f" WHERE a.{name} {rng.choice(_COMPARISONS)} {_literal(column.get_data_type(), rng)}"
    return (
        f"SELECT {', '.join(items)} FROM {left} AS a {join_kw} {right} AS b "
        f"ON a.{key} = b.{key}{where}"
    )


def random_query(
    table: Table,
    rng: random.Random,
    *,
    relation: Optional[str] = None,
    exposed_names: Sequence[str] = (),
) -> str:
    """One workload query over *table*'s columns, drawn from the template space.

    *relation* / *exposed_names* select which installed names appear in the ``FROM`` clause.
    When several names are exposed, join shapes are mixed in with single-relation ones.
    """
    columns = table.get_column_list()
    names = tuple(exposed_names) if exposed_names else ((relation or table.get_sql_name()),)
    if len(names) >= 2 and rng.random() < 0.4:
        left, right = rng.sample(list(names), k=2)
        return _join_query(left, right, columns, rng)
    name = relation or rng.choice(list(names))
    shape = rng.random()
    if shape < 0.45:
        return _flat_query(name, columns, rng)
    if shape < 0.65:
        return _grouped_query(name, columns, rng)
    if shape < 0.85:
        return _wrapped_query(name, columns, rng)
    return _aggregate_only_query(name, columns, rng)


class RandomSelectSource:
    """The example :class:`~eqgen.plugins.QuerySource`.

    Uses a private :class:`random.Random` rather than the module-level functions. That is
    required, not tidiness: the equivalence generator seeds the *global* RNG to make a
    round reproducible, so drawing from it here would shift the generator's stream and a
    seed would no longer rebuild the same equivalence.
    """

    name = "example"

    def iter_queries(
        self,
        table: Table,
        *,
        seed: int,
        limit: Optional[int] = None,
        exposed_names: Sequence[str] = (),
    ) -> Iterator[str]:
        if not table.get_column_list():
            return
        names = tuple(exposed_names) if exposed_names else (table.get_sql_name(),)
        rng = random.Random(seed)
        # ``limit=None`` streams until the consumer stops (time-budgeted rounds). An explicit
        # *limit* caps how many queries a caller materializes in one go.
        if limit is None:
            while True:
                yield random_query(table, rng, exposed_names=names)
        else:
            for _ in range(limit):
                yield random_query(table, rng, exposed_names=names)


class RandomPredicateSource:
    """The example :class:`~eqgen.plugins.PredicateSource` — a wrapper over
    :func:`random_predicate`, which is the same generator the queries above draw on."""

    name = "example"

    def boolean_predicate(self, table: Table, *, seed: int) -> Optional[str]:
        return random_predicate(table, seed=seed)
