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

"""CrateDB: catalogs, refresh fence, and the adapter that ties the dialect to Docker.

One private schema per connection (PostgreSQL wire). Every ``execute`` runs through the write fence
in :mod:`refresh` so reads see prior writes.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import signal
from typing import Any, Optional, Sequence

import psycopg

from eqgen.core.catalog import Column, Table
from eqgen.core.types import (
    DateType,
    DoubleType,
    IntegerType,
    NumericType,
    SqlType,
    TextType,
    TimestampType,
)
from eqgen.dialects.cratedb.builders import (
    CrateDbClusteredByBuilder,
    CrateDbColumnstoreOffBuilder,
    CrateDbIndexOffBuilder,
    CrateDbNamedFulltextIndexBuilder,
    CrateDbObjectRoundTripBuilder,
    CrateDbPartitionedBuilder,
    CrateDbShardCountBuilder,
)
from eqgen.dialects.cratedb.cluster import CrateCluster, shared_cluster
from eqgen.dialects.cratedb.emitter import CrateEmitter
from eqgen.dialects.cratedb.refresh import FenceState
from eqgen.dialects.cratedb.types_sql import cratedb_literal, cratedb_type
from eqgen.equivalence.config import EquivalenceConfig, load_config
from eqgen.equivalence.emitter import SqlEmitter
from eqgen.fuzz.adapter import Connection, DialectAdapter

_GCL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cratedb.gcl")

_SIMPLE_SPEC: list[tuple[str, SqlType]] = [
    ("c_pk", IntegerType()),
    ("id", IntegerType()),
    ("name", TextType()),
    ("created_at", TextType()),
]

_RICH_SPEC: list[tuple[str, SqlType]] = [
    ("c_pk", IntegerType()),
    ("c_int", IntegerType()),
    ("c_big", NumericType(38, 0)),
    ("c_dec", NumericType(10, 2)),
    ("c_dbl", DoubleType()),
    ("c_txt", TextType()),
    ("c_chr", TextType()),
    ("c_date", DateType()),
    ("c_ts", TimestampType()),
]

_schema_counter = itertools.count(1)
_LOST_CONNECTION_SQLSTATES = frozenset({"57P01", "08000", "08003", "08006", "08001", "08004"})

_SESSION_SETTINGS: tuple[tuple[str, str], ...] = (
    ("insert_select_fail_fast", "true"),
    ("error_on_unknown_object_key", "true"),
)


def _table(name: str, spec: list[tuple[str, SqlType]]) -> Table:
    columns = []
    for i, (column, data_type) in enumerate(spec, start=1):
        nullable = column != "c_pk"
        columns.append(Column(column, data_type, i, nullable=nullable))
    return Table(name, columns)


def simple_catalog(name: str = "t") -> Table:
    return _table(name, _SIMPLE_SPEC)


def rich_catalog(name: str = "t") -> Table:
    return _table(name, _RICH_SPEC)


def cratedb_equivalence_config() -> EquivalenceConfig:
    return load_config(_GCL_PATH, key="equivalence_generator_v3")


class _CrateConnection:
    def __init__(
        self,
        conn: "psycopg.Connection[Any]",
        schema: str,
        *,
        abort_on_crash: bool,
    ) -> None:
        self._conn = conn
        self._schema = schema
        self._abort_on_crash = abort_on_crash
        self.fence = FenceState()

    def _run_fence(self, sql: str) -> None:
        # Paranoid: refresh every known table before any statement that may read. CrateDB's
        # eventual consistency otherwise leaves views/CTAS children empty for the row oracle.
        from eqgen.dialects.cratedb.refresh import classify

        effect = classify(sql)
        if effect.may_read:
            refreshes = self.fence.paranoid_before() or self.fence.before(sql)
        else:
            refreshes = self.fence.before(sql)
        for statement in refreshes:
            try:
                self._conn.execute(statement)
            except psycopg.Error as exc:
                if getattr(exc, "sqlstate", None) == "42P01":
                    continue
                raise
        if refreshes:
            self.fence.flushed()

    def execute(self, sql: str, /) -> Any:
        self._run_fence(sql)
        try:
            result = self._conn.execute(sql)
        except psycopg.Error as exc:
            if self._abort_on_crash and (
                self._conn.closed or getattr(exc, "sqlstate", None) in _LOST_CONNECTION_SQLSTATES
            ):
                signal.raise_signal(signal.SIGABRT)
            raise
        self.fence.after(sql)
        return result

    def close(self) -> None:
        with contextlib.suppress(psycopg.Error):
            if not self._conn.closed:
                self._conn.execute(f'DROP SCHEMA IF EXISTS "{self._schema}" CASCADE')
        with contextlib.suppress(psycopg.Error):
            self._conn.close()


class CrateDbAdapter(DialectAdapter):
    """CrateDB via Docker (``crate:6.4.1`` by default), PostgreSQL wire + refresh fence."""

    name = "cratedb"
    nondeterministic_funcs = frozenset(
        {
            "random",
            "gen_random_text_uuid",
            "now",
            "current_timestamp",
            "current_date",
            "current_time",
            "pg_backend_pid",
            "pg_sleep",
            "uuid_string",
        }
    )

    def __init__(self, cluster: Optional[CrateCluster] = None) -> None:
        self.db_error = psycopg.Error
        self._cluster = cluster or shared_cluster()

    def equivalence_config(self) -> EquivalenceConfig:
        return cratedb_equivalence_config()

    def emitter(self) -> SqlEmitter:
        return CrateEmitter()

    def extra_builders(self) -> tuple[type, ...]:
        return (
            CrateDbIndexOffBuilder,
            CrateDbColumnstoreOffBuilder,
            CrateDbNamedFulltextIndexBuilder,
            CrateDbPartitionedBuilder,
            CrateDbObjectRoundTripBuilder,
            CrateDbShardCountBuilder,
            CrateDbClusteredByBuilder,
        )

    def connect(self) -> Connection:
        self._cluster.ensure_running()
        conn: "psycopg.Connection[Any]" = psycopg.connect(self._cluster.dsn, autocommit=True)
        conn.prepare_threshold = None
        schema = f"eqgen_{os.getpid()}_{next(_schema_counter)}"
        conn.execute(f'CREATE SCHEMA "{schema}"')
        conn.execute(f'SET search_path TO "{schema}"')
        for key, value in _SESSION_SETTINGS:
            with contextlib.suppress(psycopg.Error):
                conn.execute(f"SET {key} = {value}")
        return _CrateConnection(
            conn,
            schema,
            abort_on_crash=os.getpid() != self._cluster.owner_pid,
        )

    def base_table_ddl(self, table: Table) -> str:
        parts = []
        for c in table.get_column_list():
            piece = f"{c.get_column_name()} {cratedb_type(c.get_data_type())}"
            if not c.get_is_nullable():
                piece += " NOT NULL"
            parts.append(piece)
        cols = ", ".join(parts)
        return (
            f"CREATE TABLE {table.get_sql_name()} ({cols}) "
            "CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0)"
        )

    def literal(self, value: object) -> str:
        return cratedb_literal(value)

    def engine_banner(self) -> str:
        return f"{self._cluster.server_version()} (docker {self._cluster.image}, {self._cluster.host}:{self._cluster.port})"

    def session_context(self) -> list[tuple[str, str]]:
        return [
            ("insert_select_fail_fast", "true"),
            ("error_on_unknown_object_key", "true"),
        ]

    def known_issue_label(self, exc: Exception) -> Optional[str]:
        message = str(exc)
        if "canceling statement due to statement timeout" in message:
            return "cratedb-statement-timeout"
        return None

    def simple_catalog(self, name: str = "t") -> Table:
        return simple_catalog(name)

    def rich_catalog(self, name: str = "t") -> Table:
        return rich_catalog(name)

    def catalog_type_pool(self) -> Sequence[tuple[str, SqlType]]:
        return tuple((name, dtype) for name, dtype in _RICH_SPEC if name != "c_pk")
