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

"""MySQL catalogs and the adapter that ties the dialect to a Dockerized server.

**Strict mode and binary collation are load-bearing** — without them the row oracle lies.
**One private database per connection** isolates the two sides of a round (MySQL has no cheap
schema clone). **A crash takes the whole mysqld down**, so :meth:`MyCluster.ensure_running`
relaunches before connect. **MySQL's statement timeout is SELECT-only**, so a ``KILL QUERY``
watchdog covers DDL/setup hangs.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import re
import signal
import threading
from typing import Any, Optional, Sequence

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
from eqgen.dialects.mysql.builders import (
    MySqlInnodbTableBuilder,
    MySqlInvisibleIndexBuilder,
    MySqlJsonPackRoundTripBuilder,
    MySqlPlainIndexBuilder,
    MySqlPrefixIndexBuilder,
    MySqlUniqueIndexBuilder,
)
from eqgen.dialects.mysql.cluster import Flavor, MyCluster, shared_cluster
from eqgen.dialects.mysql.emitter import MySqlEmitter
from eqgen.dialects.mysql.types_sql import mysql_literal, mysql_type
from eqgen.equivalence.config import EquivalenceConfig, load_config
from eqgen.equivalence.emitter import SqlEmitter
from eqgen.fuzz.adapter import Connection, DialectAdapter

_GCL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mysql.gcl")
_MARIADB_GCL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mariadb.gcl")

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

_DB_COUNTER = itertools.count()

_LOST_CONNECTION_ERRNOS = frozenset({2006, 2013, 2055, 1053, 1077})

MYSQL_KNOWN_ISSUE_ERRNOS: dict[int, str] = {
    1235: "unsupported-feature (version-limited)",
    1031: "unsupported-feature (version-limited)",
    1178: "unsupported-feature (version-limited)",
    1214: "unsupported-feature (version-limited)",
    1289: "unsupported-feature (version-limited)",
    3686: "regexp-index-out-of-bounds (invalid argument)",
    # STRICT mode: COT/LN/LOG of out-of-domain values (e.g. COT('') / COT(text)). Base plans
    # often short-circuit and skip the call; equivalent rewrites evaluate it → one-sided 1690.
    1690: "numeric-out-of-range (invalid argument)",
}

#: MySQL under ``NO_BACKSLASH_ESCAPES`` often 1064s a query that filters a ``UNION``
#: (view or derived table) with a string literal containing a doubled quote (``''``).
#: The parser closes the string at the first ``'`` of the pair and the error cursor
#: lands mid-literal — e.g. ``near 'brien')'`` for ``'o''brien'``, ``near 'rG')'`` for
#: ``'…,''rG'``, ``near 'H' > …`` / ``near 'KF&' when …`` for CASE/compare shapes,
#: ``near ''…'' and/like …``, ``near '')'``, ``near '' <> …``, or ``near '\'…`` when NBE
#: leaves a literal backslash-quote at the cursor. Base tables accept the same text.
#: Demote the whole class so it does not flood ERROR findings. Require a post-cursor
#: token that is *not* a bare ``at line`` so real parse errors like ``near 'SELECT'
#: at line 1`` stay reportable.
_APOSTROPHE_POST = r"(?:when|then|else|end|and|or|not|like|rlike|regexp|between|in|[<>]=?|!=|=|<>)"
_APOSTROPHE_UNION_1064 = re.compile(
    r"near\s+(?:"
    + r"''\)"  # near '')'
    + r"|'\\'"  # near '\'… — NBE mid-apostrophe
    + r"|''[^']*''(?:\)|\s*"
    + _APOSTROPHE_POST
    + r")"
    + r"|''\s*"
    + _APOSTROPHE_POST  # near '' <> / near '' like
    + r"|'[^']+'(?:\)|\s*"
    + _APOSTROPHE_POST
    + r")"
    + r")",
    re.IGNORECASE,
)


def mysql_error_parts(exc: Exception) -> tuple[object, str]:
    """Return ``(errno, server_message)`` from a pymysql-style exception.

    Prefer ``exc.args[1]`` over ``str(exc)``: multi-arg ``Exception.__str__`` uses
    ``repr`` of the args, which doubles backslashes and escapes apostrophes — both
    of which break demotion matchers for the o'brien / TiDB Column# classes.
    """
    args = getattr(exc, "args", ()) or ()
    errno: object = args[0] if args else None
    if len(args) >= 2 and isinstance(args[1], str):
        return errno, args[1]
    if len(args) == 1 and isinstance(args[0], str):
        return None, args[0]
    return errno, str(exc)


def mysql_apostrophe_union_syntax_label(errno: object, message: str) -> Optional[str]:
    """Label for the NBE + UNION + ``''``-in-literal 1064, or ``None``."""
    if errno != 1064:
        return None
    if _APOSTROPHE_UNION_1064.search(message):
        return "mysql-obrien-apostrophe-syntax"
    return None


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


def mysql_equivalence_config() -> EquivalenceConfig:
    return load_config(_GCL_PATH, key="equivalence_generator_v3")


def mariadb_equivalence_config() -> EquivalenceConfig:
    return load_config(_MARIADB_GCL_PATH, key="equivalence_generator_v3")


class _MyConnection:
    """One private database, with optional ``KILL QUERY`` watchdog and crash signalling."""

    def __init__(
        self,
        conn: Any,
        database: str,
        *,
        connect_kwargs: dict,
        abort_on_crash: bool,
        watchdog_seconds: float,
    ) -> None:
        self._conn = conn
        self._database = database
        self._connect_kwargs = connect_kwargs
        self._abort_on_crash = abort_on_crash
        self._watchdog_seconds = watchdog_seconds

    def execute(self, sql: str) -> Any:
        watchdog = self._arm_watchdog()
        try:
            cursor = self._conn.cursor()
            cursor.execute(sql)
            return cursor
        except Exception as exc:
            self._maybe_abort(exc)
            raise
        finally:
            if watchdog is not None:
                watchdog.cancel()

    def _arm_watchdog(self) -> Optional[threading.Timer]:
        if self._watchdog_seconds <= 0:
            return None
        thread_id = self._conn.thread_id()
        timer = threading.Timer(self._watchdog_seconds, self._kill_query, args=(thread_id,))
        timer.daemon = True
        timer.start()
        return timer

    def _kill_query(self, thread_id: int) -> None:
        import pymysql

        try:
            killer = pymysql.connect(**self._connect_kwargs)
            try:
                with killer.cursor() as cur:
                    cur.execute(f"KILL QUERY {int(thread_id)}")
            finally:
                killer.close()
        except Exception:
            pass

    def _maybe_abort(self, exc: BaseException) -> None:
        errno = getattr(exc, "args", [None])[0] if getattr(exc, "args", None) else None
        if errno in _LOST_CONNECTION_ERRNOS and self._abort_on_crash:
            signal.raise_signal(signal.SIGABRT)

    def close(self) -> None:
        with contextlib.suppress(Exception), self._conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{self._database}`")
        with contextlib.suppress(Exception):
            self._conn.close()

    def reset(self) -> None:
        import pymysql

        with contextlib.suppress(Exception):
            self._conn.close()
        self._conn = pymysql.connect(**self._connect_kwargs)
        with self._conn.cursor() as cur:
            cur.execute(f"USE `{self._database}`")


class MySqlAdapter(DialectAdapter):
    """MySQL via Docker (``mysql:9.7.x`` by default)."""

    name = "mysql"
    watchdog_seconds: float = 60.0

    def __init__(self, cluster: Optional[MyCluster] = None) -> None:
        import pymysql

        self.db_error = pymysql.Error
        self._cluster = cluster or shared_cluster()

    def equivalence_config(self) -> EquivalenceConfig:
        return mysql_equivalence_config()

    def emitter(self) -> SqlEmitter:
        # Same collation the database was created with, so a CAST inside a builder cannot
        # silently change comparison semantics relative to the base table.
        return MySqlEmitter(collation=self._cluster.collation)

    def extra_builders(self) -> tuple[type, ...]:
        return (
            MySqlPlainIndexBuilder,
            MySqlUniqueIndexBuilder,
            MySqlInvisibleIndexBuilder,
            MySqlPrefixIndexBuilder,
            MySqlInnodbTableBuilder,
            MySqlJsonPackRoundTripBuilder,
        )

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

    def literal(self, value: object) -> str:
        return mysql_literal(value)

    def engine_banner(self) -> str:
        return (
            f"{self._cluster.server_version()} "
            f"(docker {self._cluster.image}, {self._cluster.host}:{self._cluster.port})"
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
            return "mysql-statement-timeout"
        # Seed / generator string pools include values with apostrophes (``o'brien``,
        # sqlancerpp strings with ``''``). Correct SQL spells them with doubled quotes,
        # which a base table accepts, but MySQL 1064s the same text against a UNION
        # view/derived table under ``NO_BACKSLASH_ESCAPES``. Match the characteristic
        # cursor ``near '<rest>')'`` only — never all of 1064.
        return mysql_apostrophe_union_syntax_label(errno, message)

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
        }
    )


class MariaDbAdapter(MySqlAdapter):
    """MariaDB via Docker (``mariadb:11.4`` by default)."""

    name = "mariadb"
    watchdog_seconds: float = 0.0

    def __init__(self, cluster: Optional[MyCluster] = None) -> None:
        super().__init__(cluster or shared_cluster(Flavor.MARIADB))

    def equivalence_config(self) -> EquivalenceConfig:
        return mariadb_equivalence_config()

    def engine_banner(self) -> str:
        return (
            f"{self._cluster.server_version()} "
            f"(docker {self._cluster.image}, {self._cluster.host}:{self._cluster.port})"
        )
