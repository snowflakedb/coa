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

"""PostgreSQL: the catalogs, and the adapter that connects the harness to a server.

Dialect-only SQL lives in ``emitter.py`` (today: ``CREATE INDEX``). Portable nodes — including
``CREATE MATERIALIZED VIEW`` — are already rendered by ``SqlEmitter`` / ``PostgresSpelling``.

Two things in here are load-bearing, in the sense that the results are wrong without them:

**``autocommit=True``.** psycopg wraps statements in a transaction, so after one error every later
statement fails with "current transaction is aborted"::

    SELECT bad syntax        -> error, as expected
    SELECT c_int FROM t      -> "current transaction is aborted" -- and now every query is a finding

``Database.query`` deliberately catches a per-side error and carries on, so without autocommit the
first invalid generated query turns the rest of the round into false findings.

**One private schema per connection.** The harness opens two connections and needs them mutually
invisible — for DuckDB they are separate in-memory databases, but here they are one server. Each
connection creates a schema, points ``search_path`` at it, and drops it on close.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import time
from typing import Any, Optional, Sequence

import psycopg

from eqgen.core.catalog import Column, Table
from eqgen.core.types import (
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
)
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
from eqgen.dialects.postgres.cluster import PgCluster, shared_cluster
from eqgen.dialects.postgres.emitter import PostgresEmitter
from eqgen.equivalence.config import EquivalenceConfig, load_config
from eqgen.equivalence.emitter import SqlEmitter
from eqgen.fuzz.adapter import Connection, DialectAdapter

#: This dialect's own weights, inheriting the portable ones. See ``postgres.gcl``.
_GCL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "postgres.gcl")

#: ``c_pk`` is a unique seed key (see ``sample_rows``): PRIMARY KEY / UNIQUE Mat and fork joins
#: target it. Other columns may still intentionally duplicate across rows.
_SIMPLE_SPEC: list[tuple[str, SqlType]] = [
    ("c_pk", IntegerType()),
    ("id", IntegerType()),
    ("name", TextType()),
    ("created_at", TextType()),
]

#: Portable scalar columns match DuckDB's rich catalog; ``c_json`` / ``c_uuid`` / ``c_range``
#: are Postgres-only (GIN/GiST Mats + sqlancerpp typing). DuckDB omits them.
#: Note what is **not** here: ``VarcharType`` and ``BooleanType``. On PostgreSQL any function
#: applied to a ``varchar`` returns ``text``, so a rewrite that touches the column changes its
#: declared type with no engine bug involved; DuckDB cannot tell the two apart either, so the
#: stable member of the pair is ``TextType``. ``BooleanType`` is omitted because PostgreSQL has
#: no ``MAX(boolean)`` / ``MIN(boolean)``, and the window / key-window rewrites would emit those.
#: ``NumericType`` and ``DoubleType`` are a different case and both stay.
_RICH_SPEC: list[tuple[str, SqlType]] = [
    ("c_pk", IntegerType()),
    ("c_int", IntegerType()),
    ("c_big", NumericType(38, 0)),  # integer-valued and wide: still a valid key to split rows on
    ("c_dec", NumericType(10, 2)),  # scaled, so it must be *rejected* as one
    ("c_dbl", DoubleType()),  # here so the never-aggregate-a-float rule gets exercised
    ("c_txt", TextType()),
    ("c_chr", TextType()),
    ("c_date", DateType()),
    ("c_ts", TimestampType()),
    ("c_json", JsonbType()),
    ("c_uuid", UuidType()),
    ("c_range", Int4RangeType()),
]

#: Counts schema names within a process, so two connections never pick the same one.
_schema_counter = itertools.count(1)


def _table(name: str, spec: list[tuple[str, SqlType]]) -> Table:
    columns = []
    for i, (column, data_type) in enumerate(spec, start=1):
        nullable = column != "c_pk"
        columns.append(Column(column, data_type, i, nullable=nullable))
    return Table(name, columns)


def simple_catalog(name: str = "t") -> Table:
    """A minimal base table."""
    return _table(name, _SIMPLE_SPEC)


def rich_catalog(name: str = "t") -> Table:
    """A base table covering the types PostgreSQL and DuckDB both have."""
    return _table(name, _RICH_SPEC)


def postgres_equivalence_config() -> EquivalenceConfig:
    """The weights for this engine, from its own GCL file."""
    return load_config(_GCL_PATH, key="equivalence_generator_v3")


class _PgConnection:
    """A connection that owns one schema, and drops it when closed.

    Creating the schema and selecting it with ``search_path`` is what gives the harness two
    databases it cannot see across, out of one server.
    """

    def __init__(self, connection: "psycopg.Connection[Any]", schema: str) -> None:
        self._connection = connection
        self._schema = schema

    def execute(self, sql: str, /) -> Any:
        return self._connection.execute(sql)

    def close(self) -> None:
        """Drop the schema, then close. Best effort: if the server has already gone, there is
        nothing to clean up and nothing worth raising about."""
        with contextlib.suppress(psycopg.Error):
            if not self._connection.closed:
                self._connection.execute(f'DROP SCHEMA IF EXISTS "{self._schema}" CASCADE')
        with contextlib.suppress(psycopg.Error):
            self._connection.close()


class PostgresAdapter(DialectAdapter):
    """PostgreSQL, over a private server this process starts and throws away."""

    name = "postgres"
    supports_float_inf = True
    #: ``ctid`` is the live hazard: a literal (block, offset) address, so it differs between the base
    #: table and anything that re-materialises the rows. The rest are transaction or relation
    #: identity and cannot survive re-materialisation either. A query source must not name them.
    nondeterministic_funcs = frozenset({"random", "now", "clock_timestamp", "timeofday", "gen_random_uuid"})

    def __init__(self) -> None:
        # Started here, in the parent: load_adapter runs once before the first round forks, so the
        # server comes up once per process rather than once per round, and the parent owns it.
        self._cluster: PgCluster = shared_cluster()
        self.db_error = psycopg.Error

    # --- generation ------------------------------------------------------------------

    def equivalence_config(self) -> EquivalenceConfig:
        return postgres_equivalence_config()

    def emitter(self) -> SqlEmitter:
        return PostgresEmitter()

    def extra_builders(self) -> tuple[type, ...]:
        return (
            PostgresDistinctOnQueryBuilder,
            PostgresArrayPackRoundTripBuilder,
            PostgresWholeRowJsonPackBuilder,
            PostgresBtreeIndexBuilder,
            PostgresHashIndexBuilder,
            PostgresBrinIndexBuilder,
            PostgresPartialIndexBuilder,
            PostgresExpressionIndexBuilder,
            PostgresCoveringIndexBuilder,
            PostgresPartialCoveringIndexBuilder,
            PostgresGinJsonbIndexBuilder,
            PostgresGistRangeIndexBuilder,
            PostgresPartitionedTableMatBuilder,
            PostgresParallelToggleMatBuilder,
            PostgresPrimaryKeyMatBuilder,
            PostgresMergeUpsertBuilder,
            PostgresGeneratedColumnBuilder,
            PostgresLegacyInheritanceBuilder,
            PostgresDomainColumnBuilder,
            PostgresUnloggedTableBuilder,
            PostgresSecurityBarrierViewBuilder,
            PostgresExtendedStatisticsBuilder,
        )

    # --- the physical database -------------------------------------------------------

    #: How long to keep retrying a connection refused for "too many clients already", and how long to
    #: wait between attempts. See :meth:`connect`.
    _CONNECT_RETRY_SECONDS = 30.0
    _CONNECT_RETRY_PAUSE = 0.05

    def connect(self) -> Connection:
        """A connection with its own schema. See the module docstring on ``autocommit``.

        **Why this retries.** A backend exits asynchronously: the client closing its socket does not
        mean the server slot is free yet. A workload that opens and closes a connection every round can
        therefore run ahead of the server's ability to reap backends and hit ``max_connections``, at
        which point ``psycopg.connect`` raises "sorry, too many clients already" and the run dies.

        That is a real risk on any build and a near-certainty on a **coverage** build, because an exiting
        backend has to write out roughly 940 ``.gcda`` counter files before it goes. Measured: a
        connection-per-round workload at ~47 rounds/second exhausted a 50-slot cluster in seven minutes.

        Waiting is the correct response — the slots *are* coming back, just not instantly — so this
        retries for :data:`_CONNECT_RETRY_SECONDS` and only then gives up. The wait is bounded and the
        message is matched narrowly, so any other connection failure still surfaces immediately.
        """
        deadline = time.monotonic() + self._CONNECT_RETRY_SECONDS
        while True:
            try:
                connection: "psycopg.Connection[Any]" = psycopg.connect(self._cluster.dsn, autocommit=True)
                break
            except psycopg.OperationalError as exc:
                if "too many clients" not in str(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(self._CONNECT_RETRY_PAUSE)
        schema = f"eqgen_{os.getpid()}_{next(_schema_counter)}"
        connection.execute(f'CREATE SCHEMA "{schema}"')
        connection.execute(f'SET search_path TO "{schema}"')
        return _PgConnection(connection, schema)

    def base_table_ddl(self, table: Table) -> str:
        parts = []
        for c in table.get_column_list():
            piece = f"{c.get_column_name()} {postgres_type(c.get_data_type())}"
            if not c.get_is_nullable():
                piece += " NOT NULL"
            parts.append(piece)
        return f"CREATE TABLE {table.get_sql_name()} ({', '.join(parts)})"

    def literal(self, value: object) -> str:
        """A literal for the seed ``INSERT``.

        Booleans are checked before numbers because ``bool`` is a subclass of ``int`` in Python, so
        testing numbers first would write ``True`` as ``1``. Only single quotes need doubling —
        ``standard_conforming_strings`` is pinned on, so a backslash is just a backslash.
        """
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, float):
            import math

            if math.isnan(value):
                return "'NaN'::float8"
            if math.isinf(value):
                return ("'Infinity'::float8" if value > 0 else "'-Infinity'::float8")
            return repr(value)
        if isinstance(value, int):
            return repr(value)
        escaped = str(value).replace("'", "''")
        return f"'{escaped}'"

    # --- diagnosis -------------------------------------------------------------------

    def engine_banner(self) -> str:
        return f"{self._cluster.server_version()} (private cluster, socket {self._cluster.sockdir})"

    def session_context(self) -> list[tuple[str, str]]:
        """Settings that decide how values compare, stamped into every repro."""
        return [
            ("locale", "C (initdb --locale=C)"),
            ("standard_conforming_strings", "on"),
            ("statement_timeout", "60s"),
        ]

    def known_issue_label(self, exc: Exception) -> Optional[str]:
        """Demote the errors this generator provokes and the server is right to raise.

        Kept narrow on purpose. A pattern that matches too much is indistinguishable from a fix.

        ``statement_timeout`` fires on a pathological generated query. It is time-dependent, so it
        can hit one side and not the other, and that is not a difference in results.

        ``invalid input syntax for type boolean`` is the same story for ``CAST(text AS BOOLEAN)`` /
        ``::boolean``: plan-dependent evaluation of bad input, not a semantic bug.

        Domain errors (``SQRT`` of a negative, ``LN`` of non-positive, ``SPLIT_PART``/``SUBSTR``
        position 0, negative substring length, a 1 GiB ``REPEAT``) likewise: base plans often
        short-circuit and skip the call; equivalence rewrites evaluate it → one-sided ERROR, not a
        PG defect. Non-equijoin ``FULL JOIN`` is a documented restriction the rewrite can hit by
        changing join type.
        """
        message = str(exc)
        lower = message.lower()
        if "canceling statement due to statement timeout" in message:
            return "postgres-statement-timeout"
        if "invalid input syntax for type boolean" in message:
            return "postgres-invalid-boolean-cast"
        # CAST(text AS INT/NUMERIC/…) — same plan-dependent invalid-input story as boolean.
        if "invalid input syntax for type integer" in lower:
            return "postgres-invalid-numeric-cast"
        if "invalid input syntax for type numeric" in lower:
            return "postgres-invalid-numeric-cast"
        if "invalid input syntax for type double precision" in lower:
            return "postgres-invalid-numeric-cast"
        # Plan-dependent math domain errors (generator feeds out-of-domain args).
        if "cannot take square root of a negative number" in lower:
            return "postgres-math-domain-error"
        if "cannot take cube root" in lower:
            return "postgres-math-domain-error"
        if "cannot take logarithm of" in lower:  # zero / negative
            return "postgres-math-domain-error"
        if "input is out of range" in lower:  # acos/asin/… of |x|>1, etc.
            return "postgres-math-domain-error"
        if "division by zero" in lower:
            return "postgres-division-by-zero"
        if "character number must be positive" in lower:  # CHR(-n)
            return "postgres-chr-domain-error"
        # LCM/GCD / int mul overflow: base plans often skip the call via a false filter;
        # equivalence rewrites evaluate it → one-sided "integer out of range".
        if "integer out of range" in lower:
            return "postgres-integer-out-of-range"
        # Plan-dependent / rewrite-widened casts into jsonb/uuid/range.
        if "invalid input syntax for type json" in lower:
            return "postgres-invalid-json-cast"
        if "invalid input syntax for type uuid" in lower:
            return "postgres-invalid-uuid-cast"
        if "malformed range literal" in lower:
            return "postgres-malformed-range-literal"
        if "cannot cast" in lower and ("json" in lower or "uuid" in lower or "range" in lower):
            return "postgres-invalid-surface-cast"
        # SPLIT_PART / SUBSTR with position 0: base plans often skip the call; a rewrite evaluates it.
        if "field position must not be zero" in lower:
            return "postgres-field-position-zero"
        # SUBSTR with a negative length: same plan-dependent domain error.
        if "negative substring length not allowed" in lower:
            return "postgres-negative-substring-length"
        # Non-equijoin FULL JOIN is a documented restriction, not a crash. Stats/rewrite can
        # pick a join type the other side never attempted.
        if "full join is only supported with merge-joinable or hash-joinable join conditions" in lower:
            return "postgres-full-join-not-equijoin"
        # REPEAT / string concat overflowing 1 GiB: plan-dependent evaluation of a huge argument.
        if "string buffer exceeds maximum allowed length" in lower:
            return "postgres-string-buffer-exceeded"
        return None

    # --- catalogs --------------------------------------------------------------------

    def simple_catalog(self, name: str = "t") -> Table:
        return simple_catalog(name)

    def rich_catalog(self, name: str = "t") -> Table:
        return rich_catalog(name)

    def catalog_type_pool(self) -> Sequence[tuple[str, SqlType]]:
        return tuple((name, dtype) for name, dtype in _RICH_SPEC if name != "c_pk")


def postgres_type(data_type: SqlType) -> str:
    """The column type for ``CREATE TABLE``.

    Delegates to the portable spelling rather than repeating the mapping: ``PostgresSpelling`` is
    already what every ``CAST`` in a generated statement goes through, and a column declared one way
    and cast another is a bug this project has already had to pin with a test.
    """
    from eqgen.ir.render import DEFAULT_SPELLING

    return DEFAULT_SPELLING.type_sql(data_type)
