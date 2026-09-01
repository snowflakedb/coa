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

"""One open database, holding all the error handling.

Every way a query can fail is dealt with here, so comparing two sides is a few lines::

    base_outcome = base.query(sql)             -- rows, or an error, never a raise
    other_outcome = equivalent.query(sql)

An earlier version had two near-identical ``try``/``except`` ladders, one per side, each catching a
driver error then a decode error then formatting a note three different ways. Two copies of the
rules that decide whether something counts as a bug is one copy too many.
"""

from __future__ import annotations

import contextlib
import math
import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Optional, Sequence

from eqgen.core.catalog import Table
from eqgen.fuzz.adapter import Connection, DialectAdapter

#: One result row, canonicalised. Float NaN/±inf are mapped to stable sentinels so a Counter can
#: key them (IEEE NaN is not equal to itself). Near-equal finite floats are reconciled in
#: :func:`compare_multisets` (plan-order DOUBLE noise is not an engine bug).
Row = tuple[object, ...]

#: Relative / absolute tolerances for float agreement across rewrite plans. Public because they are
#: oracle policy: the report prints them next to the count of rows they absorbed.
#:
#: ULP / reduction-order noise from ``SUM``/``VARIANCE``/``STDDEV``/``COVAR`` is typically
#: ~1e-15–1e-16 relative on small intermediates, but bitwise payloads and wide joins push
#: MariaDB/MySQL ``STDDEV_POP`` / ``VAR_*`` disagreement into the ~1e-5–1e-4 band (e.g.
#: ``STDDEV(c_big | const)`` ~9e-5 relative). Near-zero residuals and 4-decimal driver
#: rounding (``0.2813`` vs ``0.2812``) need a looser absolute floor.
FLOAT_REL_TOL = 1e-4
FLOAT_ABS_TOL = 5e-4


#: Stable stand-ins for non-finite floats. Both sides returning NaN is agreement for eqgen —
#: the engines matched on an undefined numeric result (e.g. ``VAR_POP`` of a degenerate set).
_NAN = ("__nan__",)
_POS_INF = ("__inf__", True)
_NEG_INF = ("__inf__", False)


def _freeze(value: object) -> object:
    """Make a driver cell hashable for :class:`~collections.Counter` keys.

    PostgreSQL array / composite results arrive as ``list`` (e.g. ``REGEXP_MATCH`` → ``text[]``).
    Nested structures are frozen recursively; non-finite floats become sentinels.
    """
    if isinstance(value, float):
        if math.isnan(value):
            return _NAN
        if math.isinf(value):
            return _POS_INF if value > 0 else _NEG_INF
        return value
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, dict):
        return tuple(sorted(((_freeze(k), _freeze(v)) for k, v in value.items()), key=repr))
    if isinstance(value, set):
        return tuple(sorted((_freeze(item) for item in value), key=repr))
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, bytearray):
        return bytes(value)
    return value


def canonical_row(row: Iterable[object]) -> Row:
    """Normalise a driver row for comparison.

    Drivers differ in what they hand back for the same SQL value — ``Decimal`` versus ``float``,
    ``date`` versus string, arrays as ``list``. Comparison happens between two connections to the
    *same* engine, so this only has to be internally consistent, not canonical across engines.
    """
    return tuple(_freeze(value) for value in row)


#: Dolt (and some other MySQL-wire drivers) return DOUBLE aggregates as decimal *strings*
#: (``'28.324648528899953'``). Accept only plain numeric spellings so real VARCHAR diffs stay exact.
_NUMERIC_STRING = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


def _as_float(value: object) -> Optional[float]:
    """Coerce a numeric driver cell to ``float``, or ``None`` if it is not numeric."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        return value
    if isinstance(value, int):
        return float(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            # Decimal NaN / ±Inf — treat like the float sentinels below.
            if value.is_nan():
                return float("nan")
            return float("inf") if value > 0 else float("-inf")
        return float(value)
    if isinstance(value, str) and _NUMERIC_STRING.fullmatch(value.strip()):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _values_close(left: object, right: object) -> bool:
    """Exact for non-numerics; ``math.isclose`` for numeric cells (float / Decimal / int / numeric str).

    ``bool`` is excluded (it subclasses ``int``). NaN agrees with NaN (via :data:`_NAN` or raw).
    Drivers return DOUBLE aggregates as ``float``, ``Decimal``, or decimal *strings* depending
    on the engine — all shapes must take the tolerant path, or MariaDB/MySQL/Dolt flood false
    STDDEV mismatches.
    """
    if left == right:
        return True
    if left is _NAN or right is _NAN or left == _NAN or right == _NAN:
        # After canonical_row both sides should already be _NAN; keep this for raw floats.
        def _is_nan(value: object) -> bool:
            if value is _NAN or value == _NAN:
                return True
            if isinstance(value, float) and math.isnan(value):
                return True
            return isinstance(value, Decimal) and value.is_nan()

        return _is_nan(left) and _is_nan(right)
    if isinstance(left, bool) or isinstance(right, bool):
        return False

    a = _as_float(left)
    b = _as_float(right)
    if a is None or b is None:
        return False
    if math.isnan(a) and math.isnan(b):
        return True
    if math.isnan(a) or math.isnan(b) or math.isinf(a) or math.isinf(b):
        return a == b
    return math.isclose(a, b, rel_tol=FLOAT_REL_TOL, abs_tol=FLOAT_ABS_TOL)


def _rows_close(left: Row, right: Row) -> bool:
    return len(left) == len(right) and all(_values_close(a, b) for a, b in zip(left, right))


@dataclass(frozen=True)
class MultisetDiff:
    """Which rows one side has that the other does not, and how many of each."""

    equal: bool
    only_in_base: list[tuple[Row, int]]
    only_in_other: list[tuple[Row, int]]
    #: Rows (counting multiplicity) that matched **only** through the float tolerance. Counted
    #: because tolerance decides "is this a bug?" — the same job as ``known_issue_label``, which is
    #: labelled and tallied for exactly that reason. Without this, a widening tolerance and a fixed
    #: engine look identical.
    reconciled: int = 0


def compare_multisets(base: Counter[Row], other: Counter[Row]) -> MultisetDiff:
    """Compare two sets of rows, ignoring order but **counting duplicates**::

        base:  (1, 'a'), (1, 'a'), (2, 'b')
        other: (1, 'a'), (2, 'b')                 -- not equal: one row short

    Counting matters. Ignore it and an object that quietly removed duplicate rows would pass.

    Float / Decimal / numeric-string cells use a loose tolerance: ``SUM``/``VARIANCE``/``STDDEV``
    over a rewritten plan can differ by plan-order noise with no engine bug behind it. Exact
    equality still applies to everything else, and every row that needed the tolerance is counted
    into :attr:`MultisetDiff.reconciled` so the leniency can be audited rather than trusted.
    """
    missing = Counter(base)
    extra = Counter(other)

    # Exact consume first (cheap path when both sides agree bit-for-bit).
    for row, count in list(missing.items()):
        shared = min(count, extra[row])
        if shared:
            missing[row] -= shared
            extra[row] -= shared

    # Approximate consume: pair leftover rows whose float cells only differ by ULP noise.
    reconciled = 0
    for base_row, base_count in list(missing.items()):
        if base_count <= 0:
            continue
        for other_row, other_count in list(extra.items()):
            if other_count <= 0 or other_row == base_row:
                continue
            if not _rows_close(base_row, other_row):
                continue
            shared = min(missing[base_row], extra[other_row])
            if not shared:
                continue
            missing[base_row] -= shared
            extra[other_row] -= shared
            reconciled += shared
            if missing[base_row] <= 0:
                break

    missing = Counter({row: n for row, n in missing.items() if n > 0})
    extra = Counter({row: n for row, n in extra.items() if n > 0})
    return MultisetDiff(
        equal=not missing and not extra,
        only_in_base=sorted(missing.items(), key=lambda kv: repr(kv[0])),
        only_in_other=sorted(extra.items(), key=lambda kv: repr(kv[0])),
        reconciled=reconciled,
    )


@dataclass(frozen=True)
class QueryOutcome:
    """What one side did with one query: rows, or a failure.

    ``known_issue`` is set when the failure is one the engine was *right* to raise — a
    generator-provoked invalid input rather than a defect. Recording it separately is what keeps such
    failures out of the findings while still counting them.
    """

    rows: Optional[Counter[Row]] = None
    error: Optional[str] = None
    known_issue: Optional[str] = None

    @property
    def failed(self) -> bool:
        return self.rows is None


class Database:
    """A live database plus the base table and/or equivalent inside it."""

    def __init__(self, adapter: DialectAdapter, connection: Connection) -> None:
        self._adapter = adapter
        self._connection = connection
        self._teardown_statements: tuple[str, ...] = ()

    @property
    def connection(self) -> Connection:
        return self._connection

    # --- construction ----------------------------------------------------------------

    @classmethod
    def build_base(
        cls,
        adapter: DialectAdapter,
        table: Table,
        rows: Sequence[Row],
        *,
        connection: Optional[Connection] = None,
        exposed_names: Sequence[str] = (),
    ) -> "Database":
        """A database holding just the base table, populated.

        *connection* is optional so metamorphic coverage can pass a file-backed DuckDB CLI handle —
        objects must outlive the process that built them, and ``:memory:`` cannot.

        *exposed_names* (same-base forks): after seeding *table*, copy it to each name other than
        the seed so the workload can join ``t0``/``t1``/… on the base side.
        """
        database = cls(adapter, connection if connection is not None else adapter.connect())
        database.run(base_setup_statements(adapter, table, rows))
        names = tuple(exposed_names) if exposed_names else (table.get_sql_name(),)
        database.run(fork_copy_statements(table.get_sql_name(), names, adapter=adapter))
        return database

    @classmethod
    def build_equivalent(
        cls,
        adapter: DialectAdapter,
        table: Table,
        rows: Sequence[Row],
        *,
        statements: Sequence[str],
        connection: Optional[Connection] = None,
        exposed_names: Sequence[str] = (),
    ) -> "Database":
        """A database holding the base table renamed aside, plus the equivalence under exposed names.

        The rename is what lets the workload run byte-identical text against both databases. It also
        means the generator must be told the *hidden* name, or the equivalence would read from
        itself. *exposed_names* is accepted for API symmetry with :meth:`build_base` (fork DDL
        already creates those names).
        """
        del exposed_names  # fork DDL from generate_forks installs the exposed names
        database = cls(adapter, connection if connection is not None else adapter.connect())
        database.run(base_setup_statements(adapter, table, rows))
        database.run([adapter.rename_aside_sql(table.get_sql_name(), hidden_base_name(table))])
        database.run(statements)
        database._teardown_statements = tuple(statements)
        return database

    # --- execution -------------------------------------------------------------------

    def run(self, statements: Sequence[str]) -> None:
        """Execute *statements* in order. Raises on the first failure."""
        for statement in statements:
            self._connection.execute(statement)

    def query(self, sql: str) -> QueryOutcome:
        """Run *sql* and return its outcome. **Never raises.**

        Every way a query can fail is classified here and nowhere else: a driver error, a
        known-issue signature, or a result the client cannot decode.
        """
        try:
            cursor = self._connection.execute(sql)
            return QueryOutcome(rows=Counter(canonical_row(row) for row in cursor.fetchall()))
        except self._adapter.db_error as exc:
            label = self._adapter.known_issue_label(exc)
            return QueryOutcome(error=label or str(exc), known_issue=label)
        except TypeError as exc:
            # A cell the Counter cannot key (should be rare after :func:`canonical_row` freezes
            # lists). Keep it a per-side error so the worker process stays alive.
            return QueryOutcome(error=f"unhashable query result cell: {exc}")
        except UnicodeDecodeError as exc:
            # The engine returned bytes that are not valid UTF-8 — a string function truncating a
            # multibyte character, say. That is a genuine engine defect but *not* a crash: the server
            # is fine and simply produced a value the client cannot represent. Recording it as an
            # ordinary per-side error keeps it a comparable outcome instead of letting the exception
            # escape and be misreported as the engine dying.
            self._reset_after_decode_failure()
            return QueryOutcome(error=f"engine returned a value that is not valid UTF-8: {exc}")

    def accepts(self, sql: str) -> bool:
        """Whether the engine will accept *sql* — asked via ``EXPLAIN``, so nothing is executed.

        This is the check applied to a plugin-supplied predicate before it is embedded in DDL. It
        catches, in one question, everything a Python-side inspection cannot: an aggregate or window
        function in a ``WHERE``, an unresolvable or wrongly-qualified column, a type error, and a
        predicate written for the wrong dialect. Asking the engine beats asking ourselves — the
        engine is the authority, and a rejected predicate that reaches DDL costs the whole round.
        """
        try:
            self._connection.execute(f"EXPLAIN {sql}")
            return True
        except Exception:  # - any rejection means "do not use this"
            return False

    def multiset(self, name: str, columns: Sequence[str]) -> Counter[Row]:
        """Read every row, ignoring order but counting duplicates.

        Columns are named explicitly and in base order, so the comparison cannot be thrown off by an
        equivalent whose physical column order differs.
        """
        cursor = self._connection.execute(f"SELECT {', '.join(columns)} FROM {name}")
        return Counter(canonical_row(row) for row in cursor.fetchall())

    def column_types(self, name: str, columns: Sequence[str]) -> tuple[str, ...]:
        """The declared type of each column, read from the cursor description.

        Worth checking separately from the rows, because a rewrite can be row-exact and still change
        a column's *type* — one in the original was, turning a 64-bit integer into a decimal. The
        rows compared equal; every later workload query using an integer-only function then failed on
        one side and was reported as a finding with no engine bug behind it.
        """
        cursor = self._connection.execute(f"SELECT {', '.join(columns)} FROM {name} WHERE 1 = 0")
        description = getattr(cursor, "description", None) or ()
        return tuple(str(entry[1]) for entry in description)

    def _reset_after_decode_failure(self) -> None:
        """Recover a connection whose wire protocol desynced mid-result.

        A decode failure part-way through a result set can leave unread bytes on the socket, after
        which every later query in the round fails spuriously and is misreported as its own error. A
        dialect that can cheaply re-establish its session implements ``reset``; one that cannot omits
        it and is left alone.
        """
        reset = getattr(self._connection, "reset", None)
        if callable(reset):
            with contextlib.suppress(Exception):  # best effort; the round is already degraded
                reset()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._adapter.teardown_statements(self._teardown_statements)
        # A dead connection cannot fail to close in a way that matters.
        with contextlib.suppress(Exception):
            self._connection.close()


# ---------------------------------------------------------------------------
# Setup statements
# ---------------------------------------------------------------------------

#: Suffix the base table is renamed to in the equivalent's database.
BASE_SUFFIX = "__base"


def hidden_base_name(table: Table) -> str:
    """The name the base table hides under so the equivalent can take its own."""
    return f"{table.get_sql_name()}{BASE_SUFFIX}"


def column_names(table: Table) -> list[str]:
    """Base column names in declaration order — the canonical projection order."""
    return [column.get_column_name() for column in table.get_column_list()]


def exposed_fork_names(forks: int, *, seed_name: str = "t") -> tuple[str, ...]:
    """Visible relation names for a same-base fork round.

    ``forks == 1`` keeps the seed name (usually ``t``) for corpus compatibility.
    ``forks > 1`` yields ``t0``, ``t1``, … so join workloads have stable distinct names.
    """
    if forks < 1:
        raise ValueError(f"forks must be >= 1, got {forks}")
    if forks == 1:
        return (seed_name,)
    return tuple(f"{seed_name}{i}" for i in range(forks))


def fork_copy_statements(
    seed_name: str, exposed_names: Sequence[str], *, adapter: Optional[DialectAdapter] = None
) -> list[str]:
    """Copy the seed table to every other exposed name (same-base forks).

    Uses :meth:`DialectAdapter.fork_copy_sql` when *adapter* is given so dialects without CTAS
    (TiDB) can emit typed ``CREATE`` + ``INSERT``. Without an adapter, keeps the historical CTAS
    form for report/repro helpers.
    """
    statements: list[str] = []
    for name in exposed_names:
        if name == seed_name:
            continue
        if adapter is None:
            statements.append(f"CREATE TABLE {name} AS SELECT * FROM {seed_name}")
        else:
            statements.extend(adapter.fork_copy_sql(seed_name, name))
    return statements


def base_setup_statements(adapter: DialectAdapter, table: Table, rows: Sequence[Row]) -> list[str]:
    """``CREATE TABLE`` plus one ``INSERT`` per row."""
    statements = [adapter.base_table_ddl(table)]
    for row in rows:
        values = ", ".join(adapter.literal(value) for value in row)
        statements.append(f"INSERT INTO {table.get_sql_name()} VALUES ({values})")
    return statements
