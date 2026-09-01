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

"""How DuckDB differs from the PostgreSQL default.

Type renames plus renderings for DuckDB-only query/object nodes.
"""

from __future__ import annotations

from eqgen.core.statement import Statement
from eqgen.core.types import (
    BooleanType,
    CharType,
    DateType,
    DoubleType,
    IntegerType,
    NumericType,
    SqlType,
    TextType,
    TimestampType,
    VarcharType,
)
from eqgen.dialects.duckdb.ast import (
    DuckDBAttachObject,
    DuckDBCatalogKind,
    DuckDBCatalogObject,
    DuckDBIndexObject,
    DuckDBMacroObject,
    DuckDBPivotStructQuery,
    DuckDBPositionalJoinQuery,
    DuckDBQueryVisitor,
    DuckDBRecursiveCteQuery,
    DuckDBSetupVisitor,
    DuckDBStarReplaceQuery,
)
from eqgen.equivalence.emitter import QueryRenderer, SqlEmitter
from eqgen.ir.expr import ValueCodec, ValueCodecRoundTrip
from eqgen.ir.render import PostgresSpelling, UnsupportedForDialect


def duckdb_type(data_type: SqlType) -> str:
    if isinstance(data_type, DoubleType):
        return "DOUBLE"
    if isinstance(data_type, IntegerType):
        return "BIGINT"
    if isinstance(data_type, NumericType):
        precision, scale = data_type.get_precision(), data_type.get_scale()
        if precision is None:
            return "BIGINT" if scale in (None, 0) else "DECIMAL"
        return f"DECIMAL({precision}, {scale or 0})"
    if isinstance(data_type, BooleanType):
        return "BOOLEAN"
    if isinstance(data_type, DateType):
        return "DATE"
    if isinstance(data_type, TimestampType):
        return "TIMESTAMP"
    if isinstance(data_type, (TextType, CharType, VarcharType)):
        return "VARCHAR"
    raise UnsupportedForDialect(f"no DuckDB type name for {data_type.get_type_kind()}")


class DuckDBSpelling(PostgresSpelling):

    def value_codec_sql(self, node: ValueCodecRoundTrip) -> str:
        """Verified on DuckDB v2.0.0-alpha37690. ``encode``/``decode`` bridge VARCHAR and BLOB."""
        inner = self.expr(node.arg)
        if node.codec is ValueCodec.JSON_PACK:
            return f"json_extract_string(json_object('v', {inner}), '$.v')"
        wrap = "hex" if node.codec is ValueCodec.HEX else "to_base64"
        unwrap = "unhex" if node.codec is ValueCodec.HEX else "from_base64"
        return f"decode({unwrap}({wrap}(encode({inner}))))"

    def type_sql(self, data_type: SqlType) -> str:
        return duckdb_type(data_type)


class DuckDBQueryRenderer(QueryRenderer, DuckDBQueryVisitor[str]):
    def __init__(self) -> None:
        super().__init__(spelling=DuckDBSpelling())

    def visit_duckdb_positional_join_query(self, query: DuckDBPositionalJoinQuery) -> str:
        left = ", ".join(query.left_cols)
        right = ", ".join(query.right_cols)
        out = ", ".join(query.out_cols)
        src = query.source.ref_sql()
        key = query.key_col
        return (
            f"SELECT {out} FROM "
            f"(SELECT {key}, {left} FROM {src} ORDER BY {key}) "
            f"POSITIONAL JOIN "
            f"(SELECT {right} FROM {src} ORDER BY {key})"
        )

    def visit_duckdb_recursive_cte_query(self, query: DuckDBRecursiveCteQuery) -> str:
        cols = ", ".join(query.out_cols)
        src = query.source.ref_sql()
        name = query.cte_name
        return (
            f"WITH RECURSIVE {name} AS ("
            f"SELECT {cols} FROM {src} "
            f"UNION ALL "
            f"SELECT {cols} FROM {name} WHERE FALSE"
            f") SELECT {cols} FROM {name}"
        )

    def visit_duckdb_star_replace_query(self, query: DuckDBStarReplaceQuery) -> str:
        col = query.replace_col
        return f"SELECT * REPLACE ({col} AS {col}) FROM {query.source.ref_sql()}"

    def visit_duckdb_pivot_struct_query(self, query: DuckDBPivotStructQuery) -> str:
        key = query.key_col
        packed = ", ".join(f"{c} := {c}" for c in query.measure_cols)
        src = query.source.ref_sql()
        select_parts = []
        for named in query.get_signature():
            col = named.alias
            if col == key:
                select_parts.append(col)
            else:
                select_parts.append(f'("x").{col} AS {col}')
        return (
            f"SELECT {', '.join(select_parts)} FROM ("
            f"FROM (SELECT {key}, 'x' AS k, struct_pack({packed}) AS v FROM {src}) "
            f"PIVOT (first(v) FOR k IN ('x'))"
            f")"
        )


class DuckDBEmitter(SqlEmitter, DuckDBSetupVisitor[list[Statement]]):
    def __init__(self) -> None:
        super().__init__(query_renderer=DuckDBQueryRenderer())

    def visit_duckdb_macro_object(self, node: DuckDBMacroObject) -> list[Statement]:
        if node.query is None:
            raise UnsupportedForDialect("a macro needs a body query")
        return [
            Statement(f"CREATE MACRO {node.macro_name}() AS TABLE {self._render_query(node.query)}"),
            Statement(f"CREATE VIEW {node.name} AS SELECT * FROM {node.macro_name}()"),
        ]

    def visit_duckdb_index_object(self, node: DuckDBIndexObject) -> list[Statement]:
        cols = ", ".join(node.out_cols)
        unique = "UNIQUE " if node.unique else ""
        return [
            Statement(f"CREATE {unique}INDEX {node.index_name} ON {node.body_ref} ({node.target})"),
            Statement(f"CREATE VIEW {node.name} AS SELECT {cols} FROM {node.body_ref}"),
        ]

    def visit_duckdb_attach_object(self, node: DuckDBAttachObject) -> list[Statement]:
        return [
            Statement(f"ATTACH ':memory:' AS {node.alias}"),
            Statement(f"CREATE TABLE {node.alias}.{node.mirror} AS SELECT * FROM {node.body_ref}"),
            Statement(f"CREATE VIEW {node.name} AS SELECT * FROM {node.alias}.{node.mirror}"),
        ]

    def visit_duckdb_catalog_object(self, node: DuckDBCatalogObject) -> list[Statement]:
        out = ", ".join(node.out_cols)
        body, aux, table, extra = node.body_ref, node.aux, node.aux_table, node.extra_col
        kind = node.kind

        if kind is DuckDBCatalogKind.SCHEMA:
            statements = [
                f"CREATE SCHEMA {aux}",
                f"CREATE OR REPLACE TABLE {aux}.{table} AS SELECT {out} FROM {body}",
                f"CREATE VIEW {node.name} AS SELECT {out} FROM {aux}.{table}",
            ]
        elif kind is DuckDBCatalogKind.ADD_DROP_COLUMN:
            statements = [
                f"CREATE OR REPLACE TABLE {table} AS SELECT {out} FROM {body}",
                f"ALTER TABLE {table} ADD COLUMN {extra} INTEGER DEFAULT 42",
                f"ALTER TABLE {table} DROP COLUMN {extra}",
                f"CREATE VIEW {node.name} AS SELECT {out} FROM {table}",
            ]
        elif kind is DuckDBCatalogKind.CHECKPOINT:
            statements = [
                f"CREATE OR REPLACE TABLE {table} AS SELECT {out} FROM {body}",
                "CHECKPOINT",
                f"CREATE VIEW {node.name} AS SELECT {out} FROM {table}",
            ]
        elif kind is DuckDBCatalogKind.ENUM_ROUND_TRIP:
            cast_back = ", ".join(
                f"CAST(CAST({col} AS {aux}) AS VARCHAR) AS {col}" if col == node.text_col else col
                for col in node.out_cols
            )
            statements = [
                f"CREATE TYPE {aux} AS ENUM (SELECT DISTINCT {node.text_col} FROM {body} WHERE {node.text_col} IS NOT NULL)",
                f"CREATE VIEW {node.name} AS SELECT {cast_back} FROM {body}",
            ]
        else:
            raise NotImplementedError(f"no DuckDB rendering for catalog kind {kind}")
        return [Statement(sql) for sql in statements]
