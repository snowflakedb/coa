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

"""How ClickHouse differs from the portable default.

Types are ``Nullable(...)``. ``QUALIFY`` and ``FULL OUTER JOIN`` lower like MySQL/CrateDB.
Native physical objects (projections, skip indexes, part layouts, codecs) need dialect visits.
"""

from __future__ import annotations

from eqgen.core.statement import Statement
from eqgen.core.types import SqlType
from eqgen.dialects.clickhouse.ast import (
    ClickHouseCodecObject,
    ClickHousePartLayoutKind,
    ClickHousePartLayoutObject,
    ClickHouseProjectionObject,
    ClickHouseSetupVisitor,
    ClickHouseSkipIndexObject,
)
from eqgen.dialects.clickhouse.types_sql import clickhouse_type
from eqgen.equivalence import actions
from eqgen.equivalence.ast import JoinQuery, SelectQuery
from eqgen.equivalence.emitter import QueryRenderer, SqlEmitter, _projection_sql
from eqgen.ir.expr import ValueCodec, ValueCodecRoundTrip
from eqgen.ir.render import PostgresSpelling


class ClickHouseSpelling(PostgresSpelling):

    def value_codec_sql(self, node: ValueCodecRoundTrip) -> str:
        """Verified on ClickHouse 26.8.1.701. ``hex``/``unhex`` and ``base64Encode``/``base64Decode``
        both return String, so no cast back is needed."""
        inner = self.expr(node.arg)
        if node.codec is ValueCodec.JSON_PACK:
            return f"JSONExtractString(toJSONString(map('v', {inner})), 'v')"
        if node.codec is ValueCodec.HEX:
            return f"unhex(hex({inner}))"
        return f"base64Decode(base64Encode({inner}))"

    def type_sql(self, data_type: SqlType) -> str:
        return clickhouse_type(data_type)


class ClickHouseQueryRenderer(QueryRenderer):
    def visit_select_query(self, query: SelectQuery) -> str:
        if query.qualify is None:
            return super().visit_select_query(query)
        if query.projection is None:
            raise ValueError("ClickHouse QUALIFY lowering needs an explicit projection")
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


class ClickHouseEmitter(SqlEmitter, ClickHouseSetupVisitor[list[Statement]]):
    def __init__(self) -> None:
        super().__init__(query_renderer=ClickHouseQueryRenderer(spelling=ClickHouseSpelling()))

    def visit_update(self, node: actions.Update) -> list[Statement]:
        """ClickHouse has no lightweight ``UPDATE … SET`` on MergeTree; use mutations."""
        sets = ", ".join(f"{column} = {self.spelling.expr(value)}" for column, value in node.assignments)
        where = f" WHERE {self.spelling.expr(node.predicate)}" if node.predicate is not None else ""
        return [Statement(f"ALTER TABLE {node.target} UPDATE {sets}{where}")]

    @staticmethod
    def _typed_columns(out_cols: tuple[str, ...], col_types: tuple[str, ...]) -> str:
        return ", ".join(f"{name} {col_type}" for name, col_type in zip(out_cols, col_types, strict=True))

    @staticmethod
    def _expose(node_name: str, out_cols: tuple[str, ...], body_ref: str) -> str:
        return f"CREATE VIEW {node_name} AS SELECT {', '.join(out_cols)} FROM {body_ref}"

    def _fill(self, child: str, out_cols: tuple[str, ...], body_ref: str) -> str:
        cols = ", ".join(out_cols)
        return f"INSERT INTO {child} ({cols}) SELECT {cols} FROM {body_ref}"

    def visit_clickhouse_projection_object(self, node: ClickHouseProjectionObject) -> list[Statement]:
        return [
            Statement(
                f"CREATE TABLE {node.child_name} ({self._typed_columns(node.out_cols, node.col_types)}) "
                "ENGINE = MergeTree ORDER BY tuple()"
            ),
            Statement(self._fill(node.child_name, node.out_cols, node.body_ref)),
            Statement(
                f"ALTER TABLE {node.child_name} ADD PROJECTION {node.projection_name} "
                f"(SELECT * ORDER BY {node.order_by})"
            ),
            Statement(f"ALTER TABLE {node.child_name} MATERIALIZE PROJECTION {node.projection_name}"),
            Statement(self._expose(node.name, node.out_cols, node.child_name)),
        ]

    def visit_clickhouse_skip_index_object(self, node: ClickHouseSkipIndexObject) -> list[Statement]:
        from eqgen.dialects.clickhouse.ast import ClickHouseSkipIndexType

        index_expr = node.column
        if node.index_type in (ClickHouseSkipIndexType.TOKENBF, ClickHouseSkipIndexType.NGRAMBF):
            # tokenbf/ngrambf reject Nullable(String); assumeNotNull is accepted.
            by_name = dict(zip(node.out_cols, node.col_types, strict=True))
            if "Nullable" in by_name.get(node.column, ""):
                index_expr = f"assumeNotNull({node.column})"
        return [
            Statement(
                f"CREATE TABLE {node.child_name} ({self._typed_columns(node.out_cols, node.col_types)}) "
                "ENGINE = MergeTree ORDER BY tuple()"
            ),
            Statement(self._fill(node.child_name, node.out_cols, node.body_ref)),
            Statement(
                f"ALTER TABLE {node.child_name} ADD INDEX {node.index_name} {index_expr} "
                f"TYPE {node.index_type.value} GRANULARITY 1"
            ),
            Statement(f"ALTER TABLE {node.child_name} MATERIALIZE INDEX {node.index_name}"),
            Statement(self._expose(node.name, node.out_cols, node.child_name)),
        ]

    def visit_clickhouse_part_layout_object(self, node: ClickHousePartLayoutObject) -> list[Statement]:
        if node.kind is ClickHousePartLayoutKind.SORTED:
            # Single-column reverse key: GROUP BY that column hits #111901 when
            # optimize_aggregation_in_order=1. Compound (k, second DESC) only
            # triggers when the query groups by both — rare in random workloads.
            layout = (
                f"ENGINE = MergeTree ORDER BY {node.key_column} DESC "
                f"SETTINGS allow_nullable_key = 1"
            )
        elif node.kind is ClickHousePartLayoutKind.PARTITIONED:
            layout = f"ENGINE = MergeTree PARTITION BY (ifNull({node.key_column}, 0) % 4) ORDER BY tuple()"
        else:
            layout = "ENGINE = MergeTree ORDER BY tuple() SETTINGS index_granularity = 8"
        return [
            Statement(
                f"CREATE TABLE {node.child_name} ({self._typed_columns(node.out_cols, node.col_types)}) {layout}"
            ),
            Statement(self._fill(node.child_name, node.out_cols, node.body_ref)),
            Statement(self._expose(node.name, node.out_cols, node.child_name)),
        ]

    def visit_clickhouse_codec_object(self, node: ClickHouseCodecObject) -> list[Statement]:
        codec_set = set(node.codec_columns)
        columns = ", ".join(
            f"{name} {col_type} CODEC({node.codec.value})" if name in codec_set else f"{name} {col_type}"
            for name, col_type in zip(node.out_cols, node.col_types, strict=True)
        )
        return [
            Statement(f"CREATE TABLE {node.child_name} ({columns}) ENGINE = MergeTree ORDER BY tuple()"),
            Statement(self._fill(node.child_name, node.out_cols, node.body_ref)),
            Statement(self._expose(node.name, node.out_cols, node.child_name)),
        ]
