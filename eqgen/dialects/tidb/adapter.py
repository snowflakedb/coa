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

"""TiDB catalogs and adapter."""

from __future__ import annotations

import itertools
import os
from typing import Optional, Sequence

from eqgen.core.catalog import Table
from eqgen.core.types import SqlType
from eqgen.dialects.mysql.adapter import (
    MYSQL_KNOWN_ISSUE_ERRNOS,
    _MyConnection,
    _RICH_SPEC,
    mysql_apostrophe_union_syntax_label,
    mysql_error_parts,
    rich_catalog,
    simple_catalog,
)
from eqgen.dialects.mysql.types_sql import mysql_literal, mysql_type
from eqgen.dialects.tidb.builders import TiDbCachedTableBuilder
from eqgen.dialects.tidb.cluster import TiDbCluster, shared_cluster
from eqgen.dialects.tidb.emitter import TiDbEmitter
from eqgen.equivalence.config import EquivalenceConfig, load_config
from eqgen.equivalence.emitter import SqlEmitter
from eqgen.fuzz.adapter import Connection, DialectAdapter

_GCL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tidb.gcl")

_DB_COUNTER = itertools.count()


def tidb_equivalence_config() -> EquivalenceConfig:
    return load_config(_GCL_PATH, key="equivalence_generator_v3")


class TiDbAdapter(DialectAdapter):
    """TiDB via local ``tidb-server`` or Docker."""

    name = "tidb"
    watchdog_seconds: float = 0.0

    def __init__(self, cluster: Optional[TiDbCluster] = None) -> None:
        import pymysql

        self.db_error = pymysql.Error
        self._cluster_or_none = cluster

    @property
    def _cluster(self) -> TiDbCluster:
        if self._cluster_or_none is None:
            self._cluster_or_none = shared_cluster()
        return self._cluster_or_none

    def equivalence_config(self) -> EquivalenceConfig:
        return tidb_equivalence_config()

    def emitter(self) -> SqlEmitter:
        return TiDbEmitter()

    def extra_builders(self) -> tuple[type, ...]:
        return (TiDbCachedTableBuilder,)

    def connect(self) -> Connection:
        import pymysql

        self._cluster.ensure_running()
        database = f"eqgen_{os.getpid()}_{next(_DB_COUNTER)}"
        kwargs = self._cluster.connect_kwargs()
        conn = pymysql.connect(**kwargs)
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE `{database}` DEFAULT CHARACTER SET utf8mb4 COLLATE {self._cluster.collation}"
            )
            cur.execute(f"USE `{database}`")
            cur.execute(
                "SET SESSION sql_mode='STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES'"
            )
        return _MyConnection(
            conn,
            database,
            connect_kwargs=kwargs,
            abort_on_crash=os.getpid() != self._cluster.owner_pid,
            watchdog_seconds=self.watchdog_seconds,
        )

    def base_table_ddl(self, table: Table) -> str:
        parts = []
        for c in table.get_column_list():
            piece = f"{c.get_column_name()} {mysql_type(c.get_data_type())}"
            if not c.get_is_nullable():
                piece += " NOT NULL"
            parts.append(piece)
        return f"CREATE TABLE {table.get_sql_name()} ({', '.join(parts)})"

    def fork_copy_sql(self, seed_name: str, exposed_name: str) -> list[str]:
        """TiDB has no CTAS — typed CREATE + INSERT … SELECT (same repair as the emitter)."""
        # Column list unknown here; SELECT * into a freshly typed table matching the seed.
        # ``CREATE TABLE new LIKE seed`` is MySQL-family and works on TiDB.
        return [
            f"CREATE TABLE {exposed_name} LIKE {seed_name}",
            f"INSERT INTO {exposed_name} SELECT * FROM {seed_name}",
        ]

    def literal(self, value: object) -> str:
        return mysql_literal(value)

    def engine_banner(self) -> str:
        source = self._cluster._bindir or f"docker {self._cluster.image}"
        return (
            f"{self._cluster.server_version()} "
            f"({source}, {self._cluster.host}:{self._cluster.port})"
        )

    def session_context(self) -> list[tuple[str, str]]:
        return [
            ("sql_mode", "STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES"),
            ("collation", self._cluster.collation),
            ("character_set", "utf8mb4"),
        ]

    def known_issue_label(self, exc: Exception) -> Optional[str]:
        errno, message = mysql_error_parts(exc)
        if isinstance(errno, int) and errno in MYSQL_KNOWN_ISSUE_ERRNOS:
            return MYSQL_KNOWN_ISSUE_ERRNOS[errno]
        lower = message.lower()
        if "max_execution_time" in lower or "query execution was interrupted" in lower:
            return "tidb-statement-timeout"
        labeled = mysql_apostrophe_union_syntax_label(errno, message)
        if labeled is not None:
            return labeled
        # Dup of dbfuzz repro tidb-run19-anyvalue-view-expr-in-subquery: ANY_VALUE view
        # + scalar expr in IN/NOT IN/<> ALL subquery → planner 1105 Can't find column.
        if errno == 1105 and "can't find column" in lower:
            return "tidb-anyvalue-view-cant-find-column"
        if errno == 1105 and "not implemented" in lower:
            return "tidb-unsupported-feature"
        # Dup of tidb-run18-predicate-pushdown-nil-plan (and LOJ×3-way cross variants).
        if errno == 1105 and "nil pointer dereference" in lower:
            return "tidb-predicate-pushdown-nil-plan"
        # Dup of tidb-run12-round38-unionall-fd-nil-map.
        if errno == 1105 and "assignment to entry in nil map" in lower:
            return "tidb-unionall-fd-nil-map"
        return None

    def simple_catalog(self, name: str = "t") -> Table:
        return simple_catalog(name)

    def rich_catalog(self, name: str = "t") -> Table:
        return rich_catalog(name)

    def catalog_type_pool(self) -> Sequence[tuple[str, SqlType]]:
        return tuple((name, dtype) for name, dtype in _RICH_SPEC if name != "c_pk")

    nondeterministic_funcs = frozenset(
        {
            "RAND",
            "RANDOM",
            "UUID",
            "NOW",
            "CURRENT_TIMESTAMP",
            "CURRENT_TIME",
            "CURRENT_DATE",
            "CURDATE",
            "CURTIME",
            "SYSDATE",
            "UNIX_TIMESTAMP",
            "ANY_VALUE",  # group pick is plan/order dependent → false mismatches
        }
    )
