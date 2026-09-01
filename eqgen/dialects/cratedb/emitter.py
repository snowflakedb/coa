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

"""How CrateDB differs from the portable default.

CTAS is rewritten to typed ``CREATE TABLE … CLUSTERED INTO 1 SHARDS`` plus ``INSERT … SELECT``.
``QUALIFY`` and ``FULL OUTER JOIN`` are lowered like MySQL. Native physical objects need dialect visits.
"""

from __future__ import annotations

from eqgen.core.statement import Statement
from eqgen.core.types import SqlType
from eqgen.dialects.cratedb.ast import (
    CrateColumnIndexObject,
    CrateIndexMode,
    CrateObjectPackObject,
    CratePartitionedObject,
    CrateSetupVisitor,
    CrateShardLayoutObject,
)
from eqgen.dialects.cratedb.types_sql import cratedb_type
from eqgen.equivalence import objects
from eqgen.equivalence.ast import JoinQuery, SelectQuery
from eqgen.equivalence.emitter import QueryRenderer, SqlEmitter, _projection_sql
from eqgen.ir.render import PostgresSpelling

_SHARD_CLAUSE = " CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0)"


class CrateSpelling(PostgresSpelling):
    def type_sql(self, data_type: SqlType) -> str:
        return cratedb_type(data_type)


class CrateQueryRenderer(QueryRenderer):
    def visit_select_query(self, query: SelectQuery) -> str:
        if query.qualify is None:
            return super().visit_select_query(query)
        if query.projection is None:
            raise ValueError("CrateDB QUALIFY lowering needs an explicit projection")
        select_list = ", ".join(_projection_sql(item, self._spelling) for item in query.projection)
        distinct = "DISTINCT " if query.distinct else ""
        where = f" WHERE {self._spelling.expr(query.predicate)}" if query.predicate is not None else ""
        group = (
            " GROUP BY " + ", ".join(self._spelling.expr(key) for key in query.group_by)
            if query.group_by is not None
            else ""
        )
        q_expr = self._spelling.expr(query.qualify)
        inner = (
            f"SELECT {distinct}{select_list}, ({q_expr}) AS eq_q "
            f"FROM {query.source.ref_sql()}{where}{group}"
        )
        outer_list = ", ".join(item.alias for item in query.projection)
        body = f"SELECT {outer_list} FROM ({inner}) AS eq_qsrc WHERE eq_q"
        if query.order_by is None:
            return body
        order = ", ".join(self._spelling.expr(key) for key in query.order_by)
        return f"SELECT {outer_list} FROM ({body} ORDER BY {order}) AS eq_ord"

    def visit_join_query(self, query: JoinQuery) -> str:
        if query.join_type == "FULL OUTER":
            query = JoinQuery(
                query.left,
                query.right,
                query.condition,
                "LEFT OUTER",
                query.projection,
                query.left_alias,
                query.right_alias,
                query.predicate,
            )
        return super().visit_join_query(query)


class CrateEmitter(SqlEmitter, CrateSetupVisitor[list[Statement]]):
    def __init__(self) -> None:
        super().__init__(query_renderer=CrateQueryRenderer(spelling=CrateSpelling()))

    def visit_table_object(self, node: objects.TableObject) -> list[Statement]:
        if node.query is None:
            raise ValueError(f"{node.name}: CrateDB requires a typed CREATE + INSERT rewrite")
        signature = node.query.get_signature()
        declarations = ", ".join(f"{named.alias} {cratedb_type(named.target)}" for named in signature)
        column_list = ", ".join(named.alias for named in signature)
        return [
            Statement(f"CREATE TABLE {node.name} ({declarations}){_SHARD_CLAUSE}"),
            Statement(f"INSERT INTO {node.name} ({column_list}) {self._render_query(node.query)}"),
        ]

    @staticmethod
    def _typed_columns(out_cols: tuple[str, ...], col_types: tuple[str, ...]) -> str:
        return ", ".join(f"{name} {col_type}" for name, col_type in zip(out_cols, col_types, strict=True))

    @staticmethod
    def _expose(node_name: str, out_cols: tuple[str, ...], body_ref: str) -> str:
        return f"CREATE VIEW {node_name} AS SELECT {', '.join(out_cols)} FROM {body_ref}"

    def _fill(self, child: str, out_cols: tuple[str, ...], body_ref: str) -> str:
        cols = ", ".join(out_cols)
        return f"INSERT INTO {child} ({cols}) SELECT {cols} FROM {body_ref}"

    def visit_cratedb_column_index_object(self, node: CrateColumnIndexObject) -> list[Statement]:
        chosen = set(node.index_columns)
        if node.mode is CrateIndexMode.INDEX_OFF:
            declarations = ", ".join(
                f"{name} {col_type} INDEX OFF" if name in chosen else f"{name} {col_type}"
                for name, col_type in zip(node.out_cols, node.col_types, strict=True)
            )
        elif node.mode is CrateIndexMode.COLUMNSTORE_OFF:
            declarations = ", ".join(
                (
                    f"{name} {col_type} STORAGE WITH (columnstore = false)"
                    if name in chosen
                    else f"{name} {col_type}"
                )
                for name, col_type in zip(node.out_cols, node.col_types, strict=True)
            )
        else:
            fulltext = (
                f", INDEX {node.index_name} USING FULLTEXT ({', '.join(node.index_columns)}) "
                "WITH (analyzer='english')"
            )
            declarations = self._typed_columns(node.out_cols, node.col_types) + fulltext
        return [
            Statement(f"CREATE TABLE {node.child_name} ({declarations}){_SHARD_CLAUSE}"),
            Statement(self._fill(node.child_name, node.out_cols, node.body_ref)),
            Statement(self._expose(node.name, node.out_cols, node.child_name)),
        ]

    def visit_cratedb_partitioned_object(self, node: CratePartitionedObject) -> list[Statement]:
        declarations = (
            self._typed_columns(node.out_cols, node.col_types)
            + f", {node.bucket_column} INTEGER GENERATED ALWAYS AS ({node.bucket_source} % {node.buckets})"
        )
        return [
            Statement(
                f"CREATE TABLE {node.child_name} ({declarations}) "
                f"PARTITIONED BY ({node.bucket_column}){_SHARD_CLAUSE}"
            ),
            Statement(self._fill(node.child_name, node.out_cols, node.body_ref)),
            Statement(self._expose(node.name, node.out_cols, node.child_name)),
        ]

    def visit_cratedb_shard_layout_object(self, node: CrateShardLayoutObject) -> list[Statement]:
        declarations = self._typed_columns(node.out_cols, node.col_types)
        clustered = (
            f"CLUSTERED BY ({node.routing_column}) INTO {node.shards} SHARDS"
            if node.routing_column
            else f"CLUSTERED INTO {node.shards} SHARDS"
        )
        return [
            Statement(
                f"CREATE TABLE {node.child_name} ({declarations}) "
                f"{clustered} WITH (number_of_replicas = 0)"
            ),
            Statement(self._fill(node.child_name, node.out_cols, node.body_ref)),
            Statement(self._expose(node.name, node.out_cols, node.child_name)),
        ]

    def visit_cratedb_object_pack_object(self, node: CrateObjectPackObject) -> list[Statement]:
        object_body = self._typed_columns(node.out_cols, node.col_types)
        packed = ", ".join(f"{col} = {col}" for col in node.out_cols)
        unpacked = ", ".join(f"{node.object_column}['{col}'] AS {col}" for col in node.out_cols)
        return [
            Statement(
                f"CREATE TABLE {node.child_name} ({node.object_column} OBJECT(STRICT) AS ({object_body}))"
                f"{_SHARD_CLAUSE}"
            ),
            Statement(
                f"INSERT INTO {node.child_name} ({node.object_column}) SELECT {{{packed}}} FROM {node.body_ref}"
            ),
            Statement(f"CREATE VIEW {node.name} AS SELECT {unpacked} FROM {node.child_name}"),
        ]
