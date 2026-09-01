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

"""ClickHouse: catalogs, throwaway HTTP server, and the dialect adapter."""

from __future__ import annotations

import os
from typing import Optional, Sequence

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
from eqgen.dialects.clickhouse import cluster, connection
from eqgen.dialects.clickhouse.builders import (
    ClickHouseArrayElementRoundTripBuilder,
    ClickHouseBloomIndexBuilder,
    ClickHouseCoalesceSelfRoundTripBuilder,
    ClickHouseDeltaCodecBuilder,
    ClickHouseFineGranuleTableBuilder,
    ClickHouseMapElementRoundTripBuilder,
    ClickHouseMinMaxIndexBuilder,
    ClickHouseNgramBfIndexBuilder,
    ClickHousePartitionedTableBuilder,
    ClickHouseProjectionBuilder,
    ClickHouseSetIndexBuilder,
    ClickHouseSortedTableBuilder,
    ClickHouseTokenBfIndexBuilder,
    ClickHouseTupleElementRoundTripBuilder,
    ClickHouseZstdCodecBuilder,
)
from eqgen.dialects.clickhouse.emitter import ClickHouseEmitter
from eqgen.dialects.clickhouse.types_sql import clickhouse_literal, clickhouse_type
from eqgen.equivalence.config import EquivalenceConfig, load_config
from eqgen.equivalence.emitter import SqlEmitter
from eqgen.fuzz.adapter import Connection, DialectAdapter

_GCL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clickhouse.gcl")

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

_NONDETERMINISTIC_FUNCS = frozenset(
    {
        "RAND",
        "RAND32",
        "RAND64",
        "RANDCANONICAL",
        "RANDUNIFORM",
        "GENERATEUUIDV4",
        "GENERATEUUIDV7",
        "NOW",
        "NOW64",
        "TODAY",
        "YESTERDAY",
        "TIMESLOT",
        "RANDOM",
        "UUID",
        "UUID_STRING",
        "CURRENT_TIMESTAMP",
        "CURRENT_DATE",
        "CURRENT_TIME",
        "LOCALTIMESTAMP",
    }
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


def clickhouse_equivalence_config() -> EquivalenceConfig:
    return load_config(_GCL_PATH, key="equivalence_generator_v3")


class ClickHouseAdapter(DialectAdapter):
    name = "clickhouse"
    db_error = connection.ClickHouseError
    nondeterministic_funcs = _NONDETERMINISTIC_FUNCS

    def __init__(self) -> None:
        # Start the server in the parent before any round forks.
        self._cluster = cluster.shared_cluster()

    def equivalence_config(self) -> EquivalenceConfig:
        return clickhouse_equivalence_config()

    def emitter(self) -> SqlEmitter:
        return ClickHouseEmitter()

    def extra_builders(self) -> tuple[type, ...]:
        return (
            ClickHouseProjectionBuilder,
            ClickHouseMinMaxIndexBuilder,
            ClickHouseSetIndexBuilder,
            ClickHouseBloomIndexBuilder,
            ClickHouseTokenBfIndexBuilder,
            ClickHouseNgramBfIndexBuilder,
            ClickHouseSortedTableBuilder,
            ClickHousePartitionedTableBuilder,
            ClickHouseFineGranuleTableBuilder,
            ClickHouseZstdCodecBuilder,
            ClickHouseDeltaCodecBuilder,
            ClickHouseTupleElementRoundTripBuilder,
            ClickHouseArrayElementRoundTripBuilder,
            ClickHouseMapElementRoundTripBuilder,
            ClickHouseCoalesceSelfRoundTripBuilder,
        )

    def connect(self) -> Connection:
        return connection.connect()

    def base_table_ddl(self, table: Table) -> str:
        columns = ", ".join(
            f"{column.get_column_name()} {clickhouse_type(column.get_data_type())}"
            for column in table.get_column_list()
        )
        return (
            f"CREATE TABLE {table.get_sql_name()} ({columns}) "
            f"ENGINE = MergeTree ORDER BY tuple()"
        )

    def literal(self, value: object) -> str:
        return clickhouse_literal(value)

    def rename_aside_sql(self, name: str, hidden: str) -> str:
        return f"RENAME TABLE {name} TO {hidden}"

    def engine_banner(self) -> str:
        marker = cluster.read_marker()
        version = self._cluster.version
        if marker and marker.get("source_version"):
            return f"clickhouse {marker['source_version']} (master, {self._cluster.binary})"
        return f"clickhouse {version} ({self._cluster.binary})"

    def session_context(self) -> list[tuple[str, str]]:
        try:
            conn = connection.connect()
            try:
                cursor = conn.execute(
                    "SELECT name, value FROM system.settings "
                    "WHERE name IN ("
                    "'join_use_nulls','max_threads',"
                    "'default_table_engine','mutations_sync','max_execution_time'"
                    ") ORDER BY name"
                )
                return [(str(row[0]), str(row[1])) for row in cursor.fetchall()]
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 — best-effort for repro headers
            return []

    def known_issue_label(self, exc: Exception) -> Optional[str]:
        msg = str(exc)
        # Filed: repro/clickhouse-view-join-threeway-same-names (Code 49 LOGICAL_ERROR).
        if "Code: 49" in msg and "same names" in msg:
            return "clickhouse-code49-same-names"
        # Filed: repro/clickhouse-lambda-matcher-join-use-nulls (#111993).
        if "Code: 49" in msg and "Cannot capture column" in msg:
            return "clickhouse-lambda-matcher-join-use-nulls"
        # Filed: repro/clickhouse-prewhere-using-lambda-matcher (#112898).
        if "Code: 49" in msg and "Unexpected return type from tuple" in msg:
            return "clickhouse-prewhere-using-lambda-matcher"
        # Filed: repro/clickhouse-prewhere-on-lambda-right-col.
        if "Code: 10" in msg and "NOT_FOUND_COLUMN_IN_BLOCK" in msg:
            return "clickhouse-prewhere-on-lambda-right-col"
        # One-sided CAST(NULL AS non-Nullable): generator noise, not an engine bug.
        if "Code: 349" in msg and "NULL" in msg:
            return "clickhouse-cast-null-non-nullable"
        return None

    def simple_catalog(self, name: str = "t") -> Table:
        return simple_catalog(name)

    def rich_catalog(self, name: str = "t") -> Table:
        return rich_catalog(name)

    def catalog_type_pool(self) -> Sequence[tuple[str, SqlType]]:
        return tuple((name, dtype) for name, dtype in _RICH_SPEC if name != "c_pk")
