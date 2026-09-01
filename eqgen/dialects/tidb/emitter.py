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

"""TiDB emitter: MySQL-family query lowerings plus CACHE and typed CTAS repair."""

from __future__ import annotations

from eqgen.core.statement import Statement
from eqgen.dialects.mysql.emitter import MySqlEmitter
from eqgen.dialects.mysql.types_sql import mysql_type
from eqgen.dialects.tidb.ast import TiDbCachedTableObject, TiDbSetupVisitor
from eqgen.equivalence import objects


def _typed_columns(out_cols: tuple[str, ...], col_types: tuple[str, ...]) -> str:
    return ", ".join(f"{name} {col_type}" for name, col_type in zip(out_cols, col_types, strict=True))


class TiDbEmitter(MySqlEmitter, TiDbSetupVisitor[list[Statement]]):
    """MySQL-family emitter with CTAS split and TiDB CACHE support."""

    def visit_tidb_cached_table_object(self, node: TiDbCachedTableObject) -> list[Statement]:
        columns = _typed_columns(node.out_cols, node.col_types)
        insert_cols = ", ".join(node.out_cols)
        view_cols = ", ".join(node.out_cols)
        return [
            Statement(f"CREATE TABLE {node.child_name} ({columns})"),
            Statement(
                f"INSERT INTO {node.child_name} ({insert_cols}) SELECT {insert_cols} FROM {node.body_ref}"
            ),
            Statement(f"ALTER TABLE {node.child_name} CACHE"),
            Statement(f"CREATE VIEW {node.name} AS SELECT {view_cols} FROM {node.child_name}"),
        ]

    def visit_table_object(self, node: objects.TableObject) -> list[Statement]:
        """TiDB has no CTAS — typed CREATE + INSERT ... SELECT instead."""
        if node.query is None:
            return super().visit_table_object(node)
        signature = node.query.get_signature()
        if not signature:
            return super().visit_table_object(node)
        out_cols = [named.alias for named in signature]
        col_types = [mysql_type(named.target) for named in signature]
        columns = _typed_columns(tuple(out_cols), tuple(col_types))
        insert_cols = ", ".join(out_cols)
        body = self._render_query(node.query)
        return [
            Statement(f"CREATE TABLE {node.name} ({columns})"),
            Statement(f"INSERT INTO {node.name} ({insert_cols}) {body}"),
        ]
