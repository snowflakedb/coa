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

"""The per-round log file, and the awkward rows to fill a table with.

**The log** writes each query to disk *before* running it::

    -- ==== equivalence DDL ====
    --   CREATE VIEW t AS SELECT * FROM t__base
    -- => equivalence check: OK (rows and declared types agree)
    SELECT c_int FROM t WHERE c_big > 0;
    -- => PASS
    SELECT c_txt FROM t ORDER BY c_txt;          <- process died here, and the query is on disk

A crash in the engine can take the process down with nothing written afterwards, so the query has
to be recorded first. The statements that build the object go in too, commented out, because a
query alone does not reproduce anything — see :meth:`QueryJournal.record`.

:func:`queries_from_journal` reads a finished log back, so a run can be replayed query for query.

**The rows** are chosen to be awkward. A table of ordinary values makes almost any rewrite look
correct; the interesting values are NULL, zero, negative numbers, the empty string, a string ending
in a space, a duplicated row, and — on dialects that spell them — IEEE ±Inf in ``DOUBLE``.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, TextIO

from eqgen.core.catalog import Table
from eqgen.core.types import (
    BooleanType,
    DateType,
    DoubleType,
    Int4RangeType,
    JsonbType,
    NumericType,
    SqlType,
    TimestampType,
    UuidType,
)
from eqgen.fuzz.database import Row

#: Prefix marking a journal annotation line, so queries and verdicts are distinguishable on replay.
_ANNOTATION = "-- => "


class QueryJournal:
    """A file that gets one line per query and one per verdict, flushed as it goes."""

    def __init__(self, path: str | Path, header: str = "") -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle: TextIO = self._path.open("w")
        if header:
            self._write(f"-- {header}")

    @property
    def path(self) -> Path:
        return self._path

    def _write(self, line: str) -> None:
        self._handle.write(line + "\n")
        # Flushed per line, not buffered: an engine crash must not take the record with it.
        self._handle.flush()

    def begin(self, sql: str) -> None:
        """Record a query about to run."""
        self._write(sql.rstrip().rstrip(";") + ";")

    def end(self, annotation: str) -> None:
        """Record the verdict of the query just run."""
        self._write(f"{_ANNOTATION}{annotation}")

    def note(self, message: str) -> None:
        """Record something that is not a query — a generation failure, a skipped round."""
        self._write(f"{_ANNOTATION}{message}")

    def record(self, title: str, lines: Sequence[str]) -> None:
        """Write a titled block that is not a query::

            -- ==== equivalence DDL ====
            --   CREATE VIEW t AS SELECT * FROM t__base;

        Commented out, every line. This file can be replayed as a list of queries, and running that
        ``CREATE`` as one would write to the database being tested and make the two sides differ for
        a reason that is not a bug.
        """
        self._write(f"-- ==== {title} ====")
        for line in lines:
            for physical in str(line).splitlines() or [""]:
                self._write(f"--   {physical}")

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()

    def __enter__(self) -> "QueryJournal":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@dataclass(frozen=True)
class JournalEntry:
    """One query from a journal, with its verdict if it got one."""

    sql: str
    annotation: Optional[str] = None


def parse_journal(text: str) -> list[JournalEntry]:
    """Read a log file back into entries.

    A file cut off mid-write is what a crash leaves behind, so parsing stops cleanly at the last
    complete entry instead of failing. That last entry is the interesting one.
    """
    entries: list[JournalEntry] = []
    pending: list[str] = []
    for line in text.splitlines():
        if line.startswith(_ANNOTATION):
            if entries and pending == []:
                entries[-1] = JournalEntry(entries[-1].sql, line[len(_ANNOTATION) :])
            continue
        if line.startswith("--") or not line.strip():
            continue
        pending.append(line)
        if line.rstrip().endswith(";"):
            entries.append(JournalEntry("\n".join(pending).rstrip().rstrip(";")))
            pending = []
    if pending:
        entries.append(JournalEntry("\n".join(pending).rstrip().rstrip(";")))
    return entries


def queries_from_journal(path: str | Path) -> list[str]:
    """Every query a journal recorded, for replay through a corpus source."""
    return [entry.sql for entry in parse_journal(Path(path).read_text())]


# ---------------------------------------------------------------------------
# Seed rows
# ---------------------------------------------------------------------------


def _pool_for(data_type: SqlType, *, allow_inf: bool = False) -> list[object]:
    """Awkward values to choose from, per column type.

    ``None`` is in every list, and so are zero and negatives: ``MOD`` takes the dividend's sign in
    several engines, so ``MOD(-1, 2)`` is ``-1``, and a rewrite that splits on parity gets that
    wrong before it gets anything else wrong.

    *allow_inf* adds IEEE ±Inf to the ``DOUBLE`` pool (Postgres/DuckDB). MySQL-family engines
    reject Inf literals, so callers leave this off for those dialects.
    """
    if isinstance(data_type, BooleanType):
        return [True, False, None]
    if isinstance(data_type, DoubleType):
        pool: list[object] = [0.0, 1.5, -1.5, 1000.125, None]
        if allow_inf:
            pool.extend([float("inf"), float("-inf")])
        return pool
    if isinstance(data_type, NumericType):
        scale = data_type.get_scale()
        if scale:
            return [0.00, 12.34, -5.50, 999.99, None]
        return [0, 1, 2, -1, -7, 42, None]
    if isinstance(data_type, DateType):
        return ["2024-01-15", "1999-12-31", "2030-06-01", None]
    if isinstance(data_type, TimestampType):
        return ["2024-01-15 12:34:56", "1999-12-31 23:59:59", None]
    if isinstance(data_type, JsonbType):
        return ["{}", "[]", '{"a": 1}', '{"k": "v", "n": null}', '{"a": [1, 2]}', None]
    if isinstance(data_type, UuidType):
        return [
            "00000000-0000-0000-0000-000000000000",
            "550e8400-e29b-41d4-a716-446655440000",
            "ffffffff-ffff-ffff-ffff-ffffffffffff",
            None,
        ]
    if isinstance(data_type, Int4RangeType):
        return ["empty", "[1,10)", "[0,100]", "(,)", "[-5,5]", None]
    # Strings: empty and trailing-space are the two that expose padding and comparison rules.
    return ["", "a", "abc", "Zed", "o'brien", "trailing ", None]


#: Seed / join / PRIMARY KEY column. Unique per row even when other columns intentionally duplicate.
PK_COLUMN = "c_pk"

#: Default number of non-key columns drawn by :func:`sample_catalog`.
_DEFAULT_MIN_EXTRA_COLS = 1
_DEFAULT_MAX_EXTRA_COLS = 8


def sample_catalog(
    adapter: object,
    name: str = "t",
    *,
    seed: Optional[int] = None,
    min_extra: int = _DEFAULT_MIN_EXTRA_COLS,
    max_extra: int = _DEFAULT_MAX_EXTRA_COLS,
) -> Table:
    """Sample a per-round table signature from *adapter*'s type pool.

    Always includes ``c_pk`` (``IntegerType``, ``NOT NULL``) so UNIQUE/PK Mat builders and
    :func:`sample_rows` uniquify stay sound. Remaining columns are drawn with replacement from
    :meth:`~eqgen.fuzz.adapter.DialectAdapter.catalog_type_pool`; the first use of a preferred
    name keeps that name, later draws of the same preferred stem get ``_2``, ``_3``, ….
    """
    from eqgen.core.catalog import Column
    from eqgen.core.types import IntegerType

    pool = list(adapter.catalog_type_pool())  # type: ignore[attr-defined]
    if not pool:
        raise ValueError(f"{type(adapter).__name__} has an empty catalog_type_pool()")
    if min_extra < 0 or max_extra < min_extra:
        raise ValueError(f"invalid extra column range [{min_extra}, {max_extra}]")

    rng = random.Random(seed)
    n_extra = rng.randint(min_extra, max_extra)
    used: dict[str, int] = {}
    columns: list[Column] = [Column(PK_COLUMN, IntegerType(), 1, nullable=False)]
    for _ in range(n_extra):
        preferred, dtype = rng.choice(pool)
        count = used.get(preferred, 0) + 1
        used[preferred] = count
        col_name = preferred if count == 1 else f"{preferred}_{count}"
        columns.append(Column(col_name, dtype, len(columns) + 1, nullable=True))
    return Table(name, columns)


def sample_rows(
    table: Table,
    count: int = 8,
    *,
    seed: Optional[int] = None,
    allow_inf: bool = False,
) -> list[Row]:
    """Awkward rows for *table*::

        (None, None, None)      <- row 0, always all-NULL on non-key columns
        (1, 'abc', True)
        (0, '', None)
        (1, 'abc', True)        <- last row always repeats row 1 on non-key columns

    Those two are fixed rather than left to chance: an all-NULL row and a duplicated row catch more
    broken rewrites than any amount of random data, and leaving them to luck means some rounds do not
    test them at all.

    When the catalog has ``c_pk``, that column is overwritten with ``1..n`` after the duplicate is
    planted, so PRIMARY KEY / UNIQUE Mat and fork joins stay sound without dropping the dupe payload
    (**IC contract**: builders may only UNIQUE/PK columns that sampling has made unique and
    non-null — today that is only ``c_pk``).

    Pass ``allow_inf=True`` for dialects that spell IEEE ±Inf (see
    :attr:`~eqgen.fuzz.adapter.DialectAdapter.supports_float_inf`): the first ``DOUBLE`` column then
    gets ``+Inf`` / ``-Inf`` forced onto rows 1 and 2 so Inf is not left to chance. The duplicate is
    planted after that, so it carries ``+Inf`` too; at ``count <= 3`` the duplicate consumes the
    -Inf slot, which is why the default row count is 8.
    """
    rng = random.Random(seed)
    columns = table.get_column_list()
    if not columns:
        return []
    pools = [_pool_for(column.get_data_type(), allow_inf=allow_inf) for column in columns]

    rows: list[Row] = [tuple(None for _ in columns)]
    while len(rows) < max(count, 3):
        rows.append(tuple(rng.choice(pool) for pool in pools))

    # Inf is forced *before* the duplicate is planted. The other order mutates ``rows[1]`` after the
    # copy is taken, silently removing duplicate-row coverage from every ``allow_inf`` dialect
    # (DuckDB, Postgres) whose catalog has a DOUBLE column.
    if allow_inf:
        dbl_indexes = [
            i for i, column in enumerate(columns) if isinstance(column.get_data_type(), DoubleType)
        ]
        if dbl_indexes:
            di = dbl_indexes[0]
            pos = list(rows[1])
            pos[di] = float("inf")
            rows[1] = tuple(pos)
            neg = list(rows[2])
            neg[di] = float("-inf")
            rows[2] = tuple(neg)

    # A duplicate pair, so any deduplicating rewrite is caught. At ``count <= 3`` this slot *is*
    # the -Inf row, and the duplicate wins: it is the stronger invariant, and callers wanting both
    # ask for four rows or more.
    rows[-1] = rows[1]

    pk_indexes = [i for i, column in enumerate(columns) if column.get_column_name() == PK_COLUMN]
    if pk_indexes:
        pk_i = pk_indexes[0]
        unique_rows: list[Row] = []
        for n, row in enumerate(rows, start=1):
            values = list(row)
            values[pk_i] = n
            unique_rows.append(tuple(values))
        return unique_rows
    return rows


def sample_rows_sql(adapter_literal: object, rows: Sequence[Row]) -> list[str]:
    """Render rows as SQL value tuples using an adapter's ``literal``. Small helper for repros."""
    assert callable(adapter_literal)
    return ["(" + ", ".join(adapter_literal(value) for value in row) + ")" for row in rows]
