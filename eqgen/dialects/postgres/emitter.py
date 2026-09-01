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

"""How PostgreSQL differs from the portable default.

Query rendering rewrites ``QUALIFY`` into a subquery + ``WHERE``, and renders
``DISTINCT ON``. Index / stats / security-barrier objects need dialect visits;
``CREATE MATERIALIZED VIEW`` / ``CREATE UNLOGGED TABLE`` stay on ``SqlEmitter``.
"""

from __future__ import annotations

from eqgen.core.statement import Statement
from eqgen.dialects.postgres.ast import (
    PostgresDistinctOnQuery,
    PostgresDomainColumnObject,
    PostgresExtendedStatisticsObject,
    PostgresGeneratedColumnObject,
    PostgresIndexObject,
    PostgresLegacyInheritanceObject,
    PostgresMergeUpsertObject,
    PostgresParallelToggleObject,
    PostgresPartitionedTableObject,
    PostgresPrimaryKeyObject,
    PostgresQueryVisitor,
    PostgresSecurityBarrierViewObject,
    PostgresSetupVisitor,
)
from eqgen.equivalence.ast import SelectQuery
from eqgen.equivalence.emitter import QueryRenderer, SqlEmitter, _projection_sql
from eqgen.ir.render import PostgresSpelling


class PostgresQueryRenderer(QueryRenderer, PostgresQueryVisitor[str]):
    """Portable queries, with ``QUALIFY`` lowered and ``DISTINCT ON`` rendered."""

    def visit_select_query(self, query: SelectQuery) -> str:
        if query.qualify is None:
            return super().visit_select_query(query)
        if query.projection is None:
            raise ValueError("PostgreSQL QUALIFY lowering needs an explicit projection")
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

    def visit_postgres_distinct_on_query(self, query: PostgresDistinctOnQuery) -> str:
        out = ", ".join(query.out_cols)
        return (
            f"SELECT DISTINCT ON ({query.key_col}) {out} "
            f"FROM {query.source.ref_sql()} ORDER BY {query.key_col}"
        )


class PostgresEmitter(SqlEmitter, PostgresSetupVisitor[list[Statement]]):
    """The shared emitter, plus PostgreSQL-only setup steps."""

    def __init__(self) -> None:
        super().__init__(query_renderer=PostgresQueryRenderer(spelling=PostgresSpelling()))

    def visit_postgres_index_object(self, node: PostgresIndexObject) -> list[Statement]:
        cols = ", ".join(node.out_cols)
        key = f"{node.expression}({node.target})" if node.expression else node.target
        keyed = f"({key})" if node.expression else key
        include = f" INCLUDE ({', '.join(node.include)})" if node.include else ""
        where = f" WHERE {node.predicate}" if node.predicate else ""
        # ANALYZE so the planner sees the index; without it a fresh CTAS often stays a seqscan.
        return [
            Statement(
                f"CREATE INDEX {node.index_name} ON {node.body_ref} "
                f"USING {node.method.value} ({keyed}){include}{where}"
            ),
            Statement(f"ANALYZE {node.body_ref}"),
            Statement(f"CREATE VIEW {node.name} AS SELECT {cols} FROM {node.body_ref}"),
        ]

    def visit_postgres_primary_key_object(self, node: PostgresPrimaryKeyObject) -> list[Statement]:
        cols = ", ".join(node.out_cols)
        return [
            Statement(f"ALTER TABLE {node.body_ref} ADD PRIMARY KEY ({node.target})"),
            Statement(f"CREATE VIEW {node.name} AS SELECT {cols} FROM {node.body_ref}"),
        ]

    def visit_postgres_merge_upsert_object(self, node: PostgresMergeUpsertObject) -> list[Statement]:
        cols = ", ".join(node.out_cols)
        set_cols = [c for c in node.out_cols if c != node.pk_col]
        set_clause = ", ".join(f"{c} = src.{c}" for c in set_cols) or f"{node.pk_col} = src.{node.pk_col}"
        return [
            Statement(f"CREATE TABLE {node.src_ref} AS SELECT * FROM {node.body_ref}"),
            Statement(
                f"MERGE INTO {node.body_ref} AS tgt "
                f"USING {node.src_ref} AS src ON tgt.{node.pk_col} = src.{node.pk_col} "
                f"WHEN MATCHED THEN UPDATE SET {set_clause} "
                # DO NOTHING, not INSERT: the join condition is UNKNOWN (not true) for a NULL
                # pk, so the not-matched branch fires for such a row and an INSERT there would
                # *add* a duplicate rather than update -- a silent extra row in the equivalent,
                # attributable to this builder rather than the engine. Every row of src came from
                # body, so there is genuinely nothing to insert; the update branch is the only one
                # that should ever do work.
                "WHEN NOT MATCHED THEN DO NOTHING"
            ),
            Statement(f"CREATE VIEW {node.name} AS SELECT {cols} FROM {node.body_ref}"),
        ]

    def visit_postgres_generated_column_object(self, node: PostgresGeneratedColumnObject) -> list[Statement]:
        exposed_cols = ", ".join(
            f"{node.gen_col} AS {c}" if c == node.target else c for c in node.out_cols
        )
        return [
            # CAST the expression rather than relying on the implicit assignment cast into
            # gen_type_sql. That type comes from the IR signature, but the body is a CTAS and its
            # real column type is whatever PostgreSQL inferred -- and unlike SQLite this dialect
            # does not cast projections, so the two can differ (a typmod dropped by arithmetic,
            # ROW_NUMBER's bigint against an INTEGER signature). Since gen_type_sql is the *base
            # table's own* declared type and the body is row-equivalent to the base, casting to it
            # restores the declared type without changing any value it holds.
            Statement(
                f"ALTER TABLE {node.body_ref} ADD COLUMN {node.gen_col} {node.gen_type_sql} "
                f"GENERATED ALWAYS AS (CAST({node.target} AS {node.gen_type_sql})) STORED"
            ),
            Statement(f"CREATE VIEW {node.name} AS SELECT {exposed_cols} FROM {node.body_ref}"),
        ]

    def visit_postgres_legacy_inheritance_object(self, node: PostgresLegacyInheritanceObject) -> list[Statement]:
        cols = ", ".join(node.out_cols)
        return [
            Statement(f"CREATE TABLE {node.parent_ref} (LIKE {node.body_ref})"),
            Statement(f"ALTER TABLE {node.body_ref} INHERIT {node.parent_ref}"),
            Statement(f"CREATE VIEW {node.name} AS SELECT {cols} FROM {node.parent_ref}"),
        ]

    def visit_postgres_domain_column_object(self, node: PostgresDomainColumnObject) -> list[Statement]:
        # Cast the domain column back to its own base type in the exposing view. A domain's array
        # type gets a fresh, driver-unknown OID (confirmed: psycopg falls back to raw text for
        # `int4_domain[]` instead of decoding it as a list) -- an oracle comparability gap, not an
        # engine bug, if a downstream ARRAY_*/aggregate function on the exposed column ever
        # produces an array of it. The CAST keeps the domain-typing exercised inside storage (the
        # ALTER above still runs through PostgreSQL's real domain machinery) without leaking the
        # exotic type into the workload query's result set.
        exposed_cols = ", ".join(
            f"{c}::{node.base_type_sql} AS {c}" if c == node.target else c for c in node.out_cols
        )
        return [
            Statement(f"CREATE DOMAIN {node.domain_name} AS {node.base_type_sql}"),
            # USING, for the same reason the generated column casts: base_type_sql is the IR
            # signature's type and the CTAS body's real type may differ, so spell the conversion
            # to the domain's own base type instead of leaving it to an implicit assignment cast.
            Statement(
                f"ALTER TABLE {node.body_ref} ALTER COLUMN {node.target} TYPE {node.domain_name} "
                f"USING CAST({node.target} AS {node.base_type_sql})"
            ),
            Statement(f"CREATE VIEW {node.name} AS SELECT {exposed_cols} FROM {node.body_ref}"),
        ]

    def visit_postgres_security_barrier_view_object(
        self, node: PostgresSecurityBarrierViewObject
    ) -> list[Statement]:
        if node.query is None:
            raise ValueError("security_barrier view needs a body query")
        return [
            Statement(
                f"CREATE VIEW {node.name} WITH (security_barrier = true) AS "
                f"{self._render_query(node.query)}"
            )
        ]

    def visit_postgres_extended_statistics_object(
        self, node: PostgresExtendedStatisticsObject
    ) -> list[Statement]:
        cols = ", ".join(node.out_cols)
        stat_cols = ", ".join(node.stat_cols)
        return [
            Statement(
                f"CREATE STATISTICS {node.stats_name} (ndistinct, dependencies, mcv) "
                f"ON {stat_cols} FROM {node.body_ref}"
            ),
            Statement(f"ANALYZE {node.body_ref}"),
            Statement(f"CREATE VIEW {node.name} AS SELECT {cols} FROM {node.body_ref}"),
        ]

    def visit_postgres_partitioned_table_object(
        self, node: PostgresPartitionedTableObject
    ) -> list[Statement]:
        cols = ", ".join(node.out_cols)
        parent = node.parent_name
        key = node.partition_key
        split = node.split_at
        return [
            Statement(
                f"CREATE TABLE {parent} (LIKE {node.body_ref} INCLUDING DEFAULTS) "
                f"PARTITION BY RANGE ({key})"
            ),
            Statement(
                f"CREATE TABLE {parent}_lo PARTITION OF {parent} "
                f"FOR VALUES FROM (MINVALUE) TO ({split})"
            ),
            Statement(
                f"CREATE TABLE {parent}_hi PARTITION OF {parent} "
                f"FOR VALUES FROM ({split}) TO (MAXVALUE)"
            ),
            Statement(f"INSERT INTO {parent} ({cols}) SELECT {cols} FROM {node.body_ref}"),
            Statement(f"ANALYZE {parent}"),
            Statement(f"CREATE VIEW {node.name} AS SELECT {cols} FROM {parent}"),
        ]

    def visit_postgres_parallel_toggle_object(
        self, node: PostgresParallelToggleObject
    ) -> list[Statement]:
        cols = ", ".join(node.out_cols)
        return [
            Statement("SET min_parallel_table_scan_size = 0"),
            Statement("SET min_parallel_index_scan_size = 0"),
            Statement("SET parallel_setup_cost = 0"),
            Statement("SET parallel_tuple_cost = 0"),
            Statement("SET max_parallel_workers_per_gather = 2"),
            # PG16 renamed force_parallel_mode -> debug_parallel_query; eqgen targets 18.4.
            Statement("SET debug_parallel_query = on"),
            Statement(f"CREATE VIEW {node.name} AS SELECT {cols} FROM {node.body_ref}"),
        ]
