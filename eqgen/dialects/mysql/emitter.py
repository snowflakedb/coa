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

"""How MySQL differs from the portable default.

Query rendering rewrites ``QUALIFY`` into a subquery + ``WHERE``, and lowers
``FULL OUTER JOIN`` to ``LEFT OUTER JOIN`` (safe under the synthetic ``ROW_NUMBER`` keys
the portable join builders use). Index / ENGINE objects need dialect visits.
"""

from __future__ import annotations

from eqgen.core.statement import Statement
from eqgen.core.types import SqlType
from eqgen.dialects.mysql.ast import (
    MySqlIndexKind,
    MySqlIndexObject,
    MySqlJsonPackRoundTripObject,
    MySqlSetupVisitor,
    MySqlTableOptionObject,
)
from eqgen.dialects.mysql.cluster import MYSQL_COLLATION
from eqgen.dialects.mysql.types_sql import mysql_cast_type
from eqgen.equivalence.ast import JoinQuery, SelectQuery
from eqgen.equivalence.emitter import QueryRenderer, SqlEmitter, _projection_sql
from eqgen.ir.expr import ValueCodec, ValueCodecRoundTrip
from eqgen.ir.render import PostgresSpelling


class MySqlSpelling(PostgresSpelling):
    """MySQL type names for ``CAST``."""

    def value_codec_sql(self, node: ValueCodecRoundTrip) -> str:
        """Verified on the MySQL this harness starts. ``UNHEX``/``FROM_BASE64`` return binary, so the
        CAST to CHAR is load-bearing: without it the driver hands back ``bytes`` and the declared
        type changes even though the rows agree."""
        inner = self.expr(node.arg)
        if node.codec is ValueCodec.JSON_PACK:
            return f"JSON_UNQUOTE(JSON_EXTRACT(JSON_OBJECT('v', {inner}), '$.v'))"
        wrap = "HEX" if node.codec is ValueCodec.HEX else "TO_BASE64"
        unwrap = "UNHEX" if node.codec is ValueCodec.HEX else "FROM_BASE64"
        return f"CAST({unwrap}({wrap}({inner})) AS CHAR)"

    def type_sql(self, data_type: SqlType) -> str:
        return mysql_cast_type(data_type)

    def typed_null_sql(self, node) -> str:  # type: ignore[no-untyped-def]
        return f"CAST(NULL AS {mysql_cast_type(node.null_type)})"


class MySqlQueryRenderer(QueryRenderer):
    """Portable queries, with ``QUALIFY`` and ``FULL OUTER`` lowered for MySQL."""

    def visit_select_query(self, query: SelectQuery) -> str:
        if query.qualify is None:
            return super().visit_select_query(query)
        if query.projection is None:
            raise ValueError("MySQL QUALIFY lowering needs an explicit projection")
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


class MySqlEmitter(SqlEmitter, MySqlSetupVisitor[list[Statement]]):
    """The shared emitter, plus MySQL-only setup steps.

    *collation* is the binary collation the session's database was created with. It has to be
    passed in rather than hardcoded: MariaDB's is ``utf8mb4_nopad_bin`` where MySQL's is
    ``utf8mb4_0900_bin`` (both NO PAD). A fixed name would fail or silently change trailing-space
    comparison semantics on one of them — and ``MariaDbAdapter`` inherits this emitter as-is.
    The adapters pass ``cluster.collation``, the same value they give ``CREATE DATABASE``.
    """

    def __init__(self, collation: str = MYSQL_COLLATION) -> None:
        super().__init__(query_renderer=MySqlQueryRenderer(spelling=MySqlSpelling()))
        self._collation = collation

    def visit_mysql_index_object(self, node: MySqlIndexObject) -> list[Statement]:
        cols = ", ".join(node.out_cols)
        keyed = (
            f"{node.target}({node.prefix_length})"
            if node.kind is MySqlIndexKind.PREFIX
            else node.target
        )
        unique = "UNIQUE " if node.kind is MySqlIndexKind.UNIQUE else ""
        stmts = [
            Statement(f"CREATE {unique}INDEX {node.index_name} ON {node.body_ref} ({keyed})"),
        ]
        if node.kind is MySqlIndexKind.INVISIBLE:
            stmts.append(
                Statement(f"ALTER TABLE {node.body_ref} ALTER INDEX {node.index_name} INVISIBLE")
            )
        stmts.append(Statement(f"CREATE VIEW {node.name} AS SELECT {cols} FROM {node.body_ref}"))
        return stmts

    def visit_mysql_table_option_object(self, node: MySqlTableOptionObject) -> list[Statement]:
        cols = ", ".join(node.out_cols)
        typed = ", ".join(f"{name} {typ}" for name, typ in zip(node.out_cols, node.col_types, strict=True))
        return [
            Statement(f"CREATE TABLE {node.child_name} ({typed}) ENGINE={node.engine}"),
            Statement(f"INSERT INTO {node.child_name} ({cols}) SELECT {cols} FROM {node.body_ref}"),
            Statement(f"CREATE VIEW {node.name} AS SELECT {cols} FROM {node.child_name}"),
        ]

    def visit_mysql_json_pack_round_trip_object(self, node: MySqlJsonPackRoundTripObject) -> list[Statement]:
        pack_args = ", ".join(f"'{name}', {name}" for name, _, _ in node.columns)
        extract_exprs = []
        for name, cast_type, needs_unquote in node.columns:
            extracted = f"JSON_EXTRACT(eq_json.j, '$.{name}')"
            inner = f"JSON_UNQUOTE({extracted})" if needs_unquote else extracted
            # CAST(x AS CHAR(n)) with no COLLATE resets to the connection's default collation for
            # utf8mb4 (utf8mb4_0900_ai_ci -- case/accent-INSENSITIVE), not the base table's binary
            # collation. Silently turns a case-sensitive column into a case-insensitive one,
            # which flips string comparisons (confirmed: 'Zed' >= "o'brien" is false under
            # utf8mb4_0900_bin, true after this cast with no COLLATE) -- a builder-introduced
            # comparability defect, not a MySQL bug, if left unguarded.
            cast_expr = f"CAST({inner} AS {cast_type})"
            if cast_type.startswith("CHAR"):
                cast_expr += f" COLLATE {self._collation}"
            extract_exprs.append(
                f"CASE WHEN {extracted} = CAST('null' AS JSON) THEN NULL ELSE {cast_expr} END AS {name}"
            )
        extract_sql = ", ".join(extract_exprs)
        return [
            Statement(
                f"CREATE VIEW {node.name} AS SELECT {extract_sql} FROM "
                f"(SELECT JSON_OBJECT({pack_args}) AS j FROM {node.body_ref}) AS eq_json"
            ),
        ]
