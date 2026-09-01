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

"""SQLite: pinned amalgamation (3.53.4+) plus portable and native Mat builders."""

from __future__ import annotations

import contextlib
import itertools
import os
import tempfile
from typing import Any, Optional, Sequence

# Bootstrap the pinned libsqlite *before* importing sqlite3 so _sqlite3 binds 3.53.4.
from eqgen.dialects.sqlite.ensure import PINNED_VERSION, bootstrap, library_label

_SQLITE_VERSION = bootstrap()

import sqlite3  # noqa: E402  — must follow bootstrap()

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
from eqgen.dialects.sqlite.builders import (
    SqliteAnalyzeIndexMatBuilder,
    SqliteAttachRoundTripBuilder,
    SqliteCreateIndexBuilder,
    SqliteExpressionIndexMatBuilder,
    SqliteGeneratedColumnRoundTripBuilder,
    SqliteNestedMaterializedCteBuilder,
    SqlitePartialIndexBuilder,
    SqliteRecursiveCteIdentityBuilder,
    SqliteStoredGeneratedColumnRoundTripBuilder,
    SqliteStrictTableBuilder,
    SqliteTruthyPartialIndexBuilder,
    SqliteConstantPartialIndexBuilder,
    SqliteUniqueIndexMatBuilder,
    SqliteWithoutRowidIndexedBuilder,
    SqliteWithoutRowidTableBuilder,
)
from eqgen.dialects.sqlite.emitter import SqliteEmitter
from eqgen.dialects.sqlite.types_sql import sqlite_literal, sqlite_type
from eqgen.equivalence.config import EquivalenceConfig, load_config
from eqgen.equivalence.emitter import SqlEmitter
from eqgen.fuzz.adapter import Connection, DialectAdapter

_GCL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sqlite.gcl")

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

_db_counter = itertools.count(1)


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


def sqlite_equivalence_config() -> EquivalenceConfig:
    return load_config(_GCL_PATH, key="equivalence_generator_v3")


class _SqliteConnection:
    def __init__(self, conn: sqlite3.Connection, db_path: Optional[str]) -> None:
        self._conn = conn
        self._db_path = db_path

    def execute(self, sql: str, /) -> sqlite3.Cursor:
        return self._conn.execute(sql)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._conn.close()
        if self._db_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(self._db_path)


class SqliteAdapter(DialectAdapter):
    """SQLite via stdlib ``sqlite3`` bound to the eqgen-cached amalgamation (3.53.4+)."""

    name = "sqlite"
    db_error = sqlite3.Error
    nondeterministic_funcs = frozenset({"random", "randomblob"})

    def equivalence_config(self) -> EquivalenceConfig:
        return sqlite_equivalence_config()

    def emitter(self) -> SqlEmitter:
        return SqliteEmitter()

    def extra_builders(self) -> tuple[type, ...]:
        return (
            SqliteCreateIndexBuilder,
            SqliteUniqueIndexMatBuilder,
            SqlitePartialIndexBuilder,
            SqliteAttachRoundTripBuilder,
            SqliteWithoutRowidTableBuilder,
            SqliteGeneratedColumnRoundTripBuilder,
            SqliteStoredGeneratedColumnRoundTripBuilder,
            SqliteStrictTableBuilder,
            SqliteExpressionIndexMatBuilder,
            SqliteWithoutRowidIndexedBuilder,
            SqliteRecursiveCteIdentityBuilder,
            SqliteTruthyPartialIndexBuilder,
            SqliteConstantPartialIndexBuilder,
            SqliteNestedMaterializedCteBuilder,
            SqliteAnalyzeIndexMatBuilder,
        )

    def connect(self) -> Connection:
        n = next(_db_counter)
        # File-backed DBs exercise the pager / WAL / rollback journal. Shared-memory
        # (default) never hits those paths — fine for speed, blind to storage bugs.
        if os.environ.get("EQGEN_SQLITE_FILE") == "1":
            handle = tempfile.NamedTemporaryFile(prefix=f"eqgen_sqlite_{n}_", suffix=".db", delete=False)
            handle.close()
            conn = sqlite3.connect(handle.name, isolation_level=None)
            db_path: Optional[str] = handle.name
        else:
            uri = f"file:eqgen_{os.getpid()}_{n}?mode=memory&cache=shared"
            conn = sqlite3.connect(uri, uri=True, isolation_level=None)
            db_path = None
        # Storage / temp-materialization knobs (no-ops when unsupported on :memory:).
        journal = os.environ.get("EQGEN_SQLITE_JOURNAL_MODE", "").strip().lower()
        if journal and db_path is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.execute(f"PRAGMA journal_mode={journal}")
        temp_store = os.environ.get("EQGEN_SQLITE_TEMP_STORE", "").strip().lower()
        if temp_store in {"default", "file", "memory", "0", "1", "2"}:
            with contextlib.suppress(sqlite3.Error):
                conn.execute(f"PRAGMA temp_store={temp_store}")
        mmap = os.environ.get("EQGEN_SQLITE_MMAP_SIZE", "").strip()
        if mmap.isdigit() and db_path is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.execute(f"PRAGMA mmap_size={mmap}")
        cache = os.environ.get("EQGEN_SQLITE_CACHE_SIZE", "").strip()
        if cache.lstrip("-").isdigit():
            with contextlib.suppress(sqlite3.Error):
                conn.execute(f"PRAGMA cache_size={cache}")
        return _SqliteConnection(conn, db_path)

    def base_table_ddl(self, table: Table) -> str:
        parts = []
        for c in table.get_column_list():
            piece = f"{c.get_column_name()} {sqlite_type(c.get_data_type())}"
            if not c.get_is_nullable():
                piece += " NOT NULL"
            parts.append(piece)
        return f"CREATE TABLE {table.get_sql_name()} ({', '.join(parts)})"

    def literal(self, value: object) -> str:
        return sqlite_literal(value)

    def engine_banner(self) -> str:
        return f"sqlite {sqlite3.sqlite_version} (eqgen cache {library_label()})"

    def session_context(self) -> list[tuple[str, str]]:
        ctx = [
            ("sqlite_version", sqlite3.sqlite_version),
            ("sqlite_pinned", PINNED_VERSION),
            ("sqlite_lib", library_label()),
            ("sqlite_file", os.environ.get("EQGEN_SQLITE_FILE", "0")),
        ]
        for key in (
            "EQGEN_SQLITE_JOURNAL_MODE",
            "EQGEN_SQLITE_TEMP_STORE",
            "EQGEN_SQLITE_MMAP_SIZE",
            "EQGEN_SQLITE_CACHE_SIZE",
        ):
            val = os.environ.get(key)
            if val:
                ctx.append((key.removeprefix("EQGEN_").lower(), val))
        return ctx

    def known_issue_label(self, exc: Exception) -> Optional[str]:
        message = str(exc).lower()
        if "no such function" in message:
            return "sqlite-unsupported-function"
        if "near" in message and "syntax error" in message:
            if "qualify" in message or "lateral" in message or "materialized" in message:
                return "sqlite-unsupported-syntax"
        return None

    def simple_catalog(self, name: str = "t") -> Table:
        return simple_catalog(name)

    def rich_catalog(self, name: str = "t") -> Table:
        return rich_catalog(name)

    def catalog_type_pool(self) -> Sequence[tuple[str, SqlType]]:
        return tuple((name, dtype) for name, dtype in _RICH_SPEC if name != "c_pk")
