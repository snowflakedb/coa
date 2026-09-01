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

"""PostgreSQL-only builders.

Names start with ``Postgres`` — ``tests/boundaries_test.py`` enforces the prefix.

Query:
    PostgresDistinctOnQueryBuilder              DISTINCT ON (c_pk) **(Rewrite)**
    PostgresArrayPackRoundTripBuilder           CAST((ARRAY[c])[1] AS <type>) **(Rewrite)**
    PostgresWholeRowJsonPackBuilder             jsonb_build_object pack / ->> unpack **(Rewrite)**

Index Mat:
    PostgresBtreeIndexBuilder / Hash / Brin
    PostgresPartialIndexBuilder / ExpressionIndex / CoveringIndex
    PostgresPartialCoveringIndexBuilder   partial + INCLUDE
    PostgresGinJsonbIndexBuilder          GIN on c_json
    PostgresGistRangeIndexBuilder         GiST on c_range

Other Mat:
    PostgresPrimaryKeyMatBuilder
    PostgresMergeUpsertBuilder            no-op self-MERGE upsert (PG15+)
    PostgresGeneratedColumnBuilder        GENERATED ALWAYS AS (col) STORED twin
    PostgresLegacyInheritanceBuilder      ALTER TABLE ... INHERIT (legacy, pre-declarative)
    PostgresDomainColumnBuilder           CREATE DOMAIN + ALTER COLUMN ... TYPE
    PostgresUnloggedTableBuilder
    PostgresSecurityBarrierViewBuilder
    PostgresExtendedStatisticsBuilder
    PostgresPartitionedTableMatBuilder    RANGE partition on c_pk
    PostgresParallelToggleMatBuilder      parallel GUCs
"""

from __future__ import annotations

from typing import Callable, ClassVar, Optional, Type

from eqgen.builder.constraint_set import Constraint, ConstraintSet
from eqgen.core.types import BooleanType, Int4RangeType, JsonbType, NumericType, SqlType, VarcharType
from eqgen.dialects.postgres.ast import (
    PostgresCreateExtendedStatistics,
    PostgresCreateIndex,
    PostgresCreateParallelToggle,
    PostgresCreatePartitionedTable,
    PostgresCreatePrimaryKey,
    PostgresCreateSecurityBarrierView,
    PostgresDistinctOnQuery,
    PostgresDomainColumn,
    PostgresGeneratedColumn,
    PostgresIndexMethod,
    PostgresLegacyInheritance,
    PostgresMergeUpsert,
)
from eqgen.equivalence.ast import CreateTable, CreateUnloggedTable, CreateView, EqNode, ProjectionItem, QueryNode, SelectQuery
from eqgen.equivalence.builders.base import ColumnRewriteQueryBuilder, EquivalenceBuilder
from eqgen.equivalence.builders.creates import CreateFromQueryBuilder
from eqgen.equivalence.context import EquivalenceContext
from eqgen.ir import expr
from eqgen.ir.render import DEFAULT_SPELLING

_PK_COLUMN = "c_pk"
_JSON_COLUMN = "c_json"
_RANGE_COLUMN = "c_range"


def _out_cols(query: QueryNode) -> list[str]:
    return [named.alias for named in query.get_signature()]


def _first_text_col(query: QueryNode) -> Optional[str]:
    for named in query.get_signature():
        if isinstance(named.target, VarcharType):
            return named.alias
    return None


def _first_col_of_type(query: QueryNode, type_cls: type) -> Optional[str]:
    for named in query.get_signature():
        if isinstance(named.target, type_cls):
            return named.alias
    return None


def _prefer_pk_or_first(out_cols: list[str]) -> str:
    """Index on ``c_pk`` when present — unique, selective, and what joins/filters already hit."""
    if _PK_COLUMN in out_cols:
        return _PK_COLUMN
    return out_cols[0]


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


class PostgresDistinctOnQueryBuilder(EquivalenceBuilder[PostgresDistinctOnQuery]):
    """``SELECT DISTINCT ON (c_pk) … ORDER BY c_pk`` — row-exact when ``c_pk`` is unique."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return []

    def _build(
        self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext
    ) -> Optional[PostgresDistinctOnQuery]:
        del constraint_set
        base_items = self._passthrough_items(context)
        if not base_items or _PK_COLUMN not in {item.alias for item in base_items}:
            return None
        source = self._dispatch_source(context)
        if source is None:
            return None
        source_cols = {named.alias for named in source.get_signature()}
        if _PK_COLUMN not in source_cols:
            return None
        return PostgresDistinctOnQuery(source, _PK_COLUMN, base_items)


class PostgresArrayPackRoundTripBuilder(ColumnRewriteQueryBuilder):
    """``CAST((ARRAY[c])[1] AS <type>)`` per column — array pack/extract identity **(Rewrite)**.

    The CAST is load-bearing: PostgreSQL arrays of ``NUMERIC(p,s)`` (and some other decorated
    types) come back unconstrained, which would fail the type-equivalence gate. DuckDB's
    ``[c][1]`` keeps the declared type, so it does not need this.
    """

    def _column_rewriter(self, context: EquivalenceContext) -> Callable[[str, SqlType], Optional[expr.ExpressionNode]]:
        del context

        def rewrite(name: str, data_type: SqlType) -> Optional[expr.ExpressionNode]:
            type_sql = DEFAULT_SPELLING.type_sql(data_type)
            return expr.raw_expr(f"CAST((ARRAY[{name}])[1] AS {type_sql})", data_type)

        return rewrite


class PostgresWholeRowJsonPackBuilder(EquivalenceBuilder[SelectQuery]):
    """Pack the whole row into one JSONB object, then unpack each column **(Rewrite)**.

    Declines unless every column is a JSON-native scalar (exact numeric / text / boolean).
    DOUBLE is excluded because PostgreSQL jsonb rejects Inf/NaN, which the PG catalog plants.
    """

    _JSON_SAFE = (NumericType, VarcharType, BooleanType)

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return []

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[SelectQuery]:
        del constraint_set
        columns = context.base_table.get_column_list()
        if not columns or not all(isinstance(column.get_data_type(), self._JSON_SAFE) for column in columns):
            return None
        source = self._dispatch_source(context)
        if source is None:
            return None
        pairs = ", ".join(f"'{column.get_column_name()}', {column.get_column_name()}" for column in columns)
        obj_name = context.names.generate_column_name("eq_json_obj")
        packed = CreateView.build(
            context.namer,
            SelectQuery(
                source,
                (ProjectionItem(obj_name, expr.raw_expr(f"jsonb_build_object({pairs})", JsonbType()), JsonbType()),),
            ),
        )
        items = tuple(
            ProjectionItem(
                column.get_column_name(),
                expr.raw_expr(
                    f"CAST({obj_name} ->> '{column.get_column_name()}' AS {DEFAULT_SPELLING.type_sql(column.get_data_type())})",
                    column.get_data_type(),
                ),
                column.get_data_type(),
            )
            for column in columns
        )
        return SelectQuery(packed, items)


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------


class PostgresIndexBuilderBase(CreateFromQueryBuilder[PostgresCreateIndex]):
    """Shared plumbing: CTAS the query, attach this builder's index shape."""

    METHOD: ClassVar[PostgresIndexMethod] = PostgresIndexMethod.BTREE

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _index_args(
        self, query: QueryNode, context: EquivalenceContext
    ) -> Optional[tuple[str, Optional[str], Optional[str], tuple[str, ...]]]:
        """Return ``(target, expression, predicate, include)`` or ``None`` to decline."""
        del context
        out_cols = _out_cols(query)
        if not out_cols:
            return None
        return _prefer_pk_or_first(out_cols), None, None, ()

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> Optional[PostgresCreateIndex]:
        args = self._index_args(query, context)
        if args is None:
            return None
        target, expression, predicate, include = args
        out_cols = _out_cols(query)
        body = CreateTable.build(context.namer, query)
        return PostgresCreateIndex.build(
            context.namer,
            body,
            method=self.METHOD,
            target=target,
            out_cols=out_cols,
            exposed_name=exposed_name,
            expression=expression,
            predicate=predicate,
            include=include,
        )


class PostgresBtreeIndexBuilder(PostgresIndexBuilderBase):
    """Plain B-tree index — default AM; index / index-only scans."""

    METHOD = PostgresIndexMethod.BTREE


class PostgresHashIndexBuilder(PostgresIndexBuilderBase):
    """Hash index: equality-only, different code path from B-tree."""

    METHOD = PostgresIndexMethod.HASH


class PostgresBrinIndexBuilder(PostgresIndexBuilderBase):
    """BRIN — lossy block-range summaries → bitmap heap scan with recheck."""

    METHOD = PostgresIndexMethod.BRIN


class PostgresPartialIndexBuilder(PostgresIndexBuilderBase):
    """Partial index ``WHERE <text_col> IS NOT NULL`` — predicate implication."""

    METHOD = PostgresIndexMethod.BTREE

    def _index_args(
        self, query: QueryNode, context: EquivalenceContext
    ) -> Optional[tuple[str, Optional[str], Optional[str], tuple[str, ...]]]:
        del context
        out_cols = _out_cols(query)
        text = _first_text_col(query)
        if not out_cols or text is None:
            return None
        return _prefer_pk_or_first(out_cols), None, f"{text} IS NOT NULL", ()


class PostgresExpressionIndexBuilder(PostgresIndexBuilderBase):
    """Expression index ``((lower(text_col)))`` — matching expressions only."""

    METHOD = PostgresIndexMethod.BTREE

    def _index_args(
        self, query: QueryNode, context: EquivalenceContext
    ) -> Optional[tuple[str, Optional[str], Optional[str], tuple[str, ...]]]:
        del context
        text = _first_text_col(query)
        if text is None:
            return None
        return text, "lower", None, ()


class PostgresCoveringIndexBuilder(PostgresIndexBuilderBase):
    """Covering btree ``INCLUDE (…)`` — enables index-only scans of extra columns.

    Keys on ``c_pk`` when present so index-only scans line up with the join/filter columns the
    workload already uses; every other projected column rides as non-key INCLUDE payload.
    """

    METHOD = PostgresIndexMethod.BTREE

    def _index_args(
        self, query: QueryNode, context: EquivalenceContext
    ) -> Optional[tuple[str, Optional[str], Optional[str], tuple[str, ...]]]:
        del context
        out_cols = _out_cols(query)
        if len(out_cols) < 2:
            return None
        target = _prefer_pk_or_first(out_cols)
        include = tuple(c for c in out_cols if c != target)
        if not include:
            return None
        return target, None, None, include


class PostgresPartialCoveringIndexBuilder(PostgresIndexBuilderBase):
    """Partial covering index: ``INCLUDE (…) WHERE <text> IS NOT NULL``.

    Combines predicate implication (partial) with index-only scan payload (INCLUDE) — a planner
    path neither plain partial nor plain covering alone exercises.
    """

    METHOD = PostgresIndexMethod.BTREE

    def _index_args(
        self, query: QueryNode, context: EquivalenceContext
    ) -> Optional[tuple[str, Optional[str], Optional[str], tuple[str, ...]]]:
        del context
        out_cols = _out_cols(query)
        text = _first_text_col(query)
        if len(out_cols) < 2 or text is None:
            return None
        target = _prefer_pk_or_first(out_cols)
        include = tuple(c for c in out_cols if c != target)
        if not include:
            return None
        return target, None, f"{text} IS NOT NULL", include


class PostgresGinJsonbIndexBuilder(PostgresIndexBuilderBase):
    """``CREATE INDEX … USING gin (c_json)`` — jsonb containment / existence scans."""

    METHOD = PostgresIndexMethod.GIN

    def _index_args(
        self, query: QueryNode, context: EquivalenceContext
    ) -> Optional[tuple[str, Optional[str], Optional[str], tuple[str, ...]]]:
        del context
        target = _first_col_of_type(query, JsonbType)
        if target is None:
            # Prefer the fixed-schema name when present even if type was lost in projection.
            out = _out_cols(query)
            if _JSON_COLUMN not in out:
                return None
            target = _JSON_COLUMN
        return target, None, None, ()


class PostgresGistRangeIndexBuilder(PostgresIndexBuilderBase):
    """``CREATE INDEX … USING gist (c_range)`` — range overlap / containment scans."""

    METHOD = PostgresIndexMethod.GIST

    def _index_args(
        self, query: QueryNode, context: EquivalenceContext
    ) -> Optional[tuple[str, Optional[str], Optional[str], tuple[str, ...]]]:
        del context
        target = _first_col_of_type(query, Int4RangeType)
        if target is None:
            out = _out_cols(query)
            if _RANGE_COLUMN not in out:
                return None
            target = _RANGE_COLUMN
        return target, None, None, ()


# ---------------------------------------------------------------------------
# Other Mat
# ---------------------------------------------------------------------------


class PostgresPrimaryKeyMatBuilder(CreateFromQueryBuilder[PostgresCreatePrimaryKey]):
    """CTAS → ``ALTER TABLE … ADD PRIMARY KEY (c_pk)`` → exposing view."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> Optional[PostgresCreatePrimaryKey]:
        out_cols = _out_cols(query)
        if _PK_COLUMN not in out_cols:
            return None
        body = CreateTable.build(context.namer, query)
        return PostgresCreatePrimaryKey.build(
            context.namer,
            body,
            target=_PK_COLUMN,
            out_cols=out_cols,
            exposed_name=exposed_name,
        )


class PostgresMergeUpsertBuilder(CreateFromQueryBuilder[PostgresMergeUpsert]):
    """CTAS → self-copy → no-op ``MERGE`` upsert from the copy → exposing view.

    Every row's ``c_pk`` exists in both the target and its copy, so ``WHEN MATCHED`` fires for
    every row and ``WHEN NOT MATCHED`` never does — same rows, same values, written through
    PostgreSQL 15's newest DML statement (far less battle-tested against fuzzing than plain
    ``UPDATE``/``INSERT``).
    """

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._DML_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> Optional[PostgresMergeUpsert]:
        out_cols = _out_cols(query)
        if _PK_COLUMN not in out_cols:
            return None
        body = CreateTable.build(context.namer, query)
        return PostgresMergeUpsert.build(
            context.namer,
            body,
            pk_col=_PK_COLUMN,
            out_cols=out_cols,
            exposed_name=exposed_name,
        )


class PostgresGeneratedColumnBuilder(CreateFromQueryBuilder[PostgresGeneratedColumn]):
    """CTAS → ``ALTER TABLE … ADD COLUMN … GENERATED ALWAYS AS (<col>) STORED`` → exposing view.

    The generated twin computes from the real column via the identity expression, and the view
    exposes it under the real column's own name in place of the raw value — same rows, same
    values, read back through PostgreSQL's generated-column storage/evaluation path instead of a
    plain heap column.
    """

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> Optional[PostgresGeneratedColumn]:
        signature = query.get_signature()
        candidates = [named for named in signature if named.alias != _PK_COLUMN]
        if not candidates:
            return None
        target = candidates[0]
        body = CreateTable.build(context.namer, query)
        return PostgresGeneratedColumn.build(
            context.namer,
            body,
            target=target.alias,
            gen_type_sql=DEFAULT_SPELLING.type_sql(target.target),
            out_cols=_out_cols(query),
            exposed_name=exposed_name,
        )


class PostgresLegacyInheritanceBuilder(CreateFromQueryBuilder[PostgresLegacyInheritance]):
    """CTAS → empty parent via ``LIKE`` → ``ALTER TABLE … INHERIT`` → view over the parent.

    Querying the parent under legacy (pre-declarative) inheritance returns the parent's own rows
    (none) unioned with every child's — the child holds all of ``body``'s rows, so the view reads
    them back exactly, through the old constraint-exclusion / inherited-column planning path
    rather than declarative partitioning's dedicated pruning logic.
    """

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> Optional[PostgresLegacyInheritance]:
        out_cols = _out_cols(query)
        if not out_cols:
            return None
        body = CreateTable.build(context.namer, query)
        return PostgresLegacyInheritance.build(
            context.namer,
            body,
            out_cols=out_cols,
            exposed_name=exposed_name,
        )


class PostgresDomainColumnBuilder(CreateFromQueryBuilder[PostgresDomainColumn]):
    """CTAS → ``CREATE DOMAIN`` over one column's own base type → ``ALTER COLUMN … TYPE`` → view.

    The domain has no ``CHECK`` (or a trivially-true one), so it accepts exactly the values the
    base type already does — same rows, same values, read back through PostgreSQL's domain
    type-checking / comparison path instead of the bare base type.
    """

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> Optional[PostgresDomainColumn]:
        signature = query.get_signature()
        candidates = [named for named in signature if named.alias != _PK_COLUMN]
        if not candidates:
            return None
        target = candidates[0]
        body = CreateTable.build(context.namer, query)
        return PostgresDomainColumn.build(
            context.namer,
            body,
            target=target.alias,
            base_type_sql=DEFAULT_SPELLING.type_sql(target.target),
            out_cols=_out_cols(query),
            exposed_name=exposed_name,
        )


class PostgresUnloggedTableBuilder(CreateFromQueryBuilder[CreateUnloggedTable]):
    """``CREATE UNLOGGED TABLE … AS <query>`` — same rows, no WAL."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._DML_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> CreateUnloggedTable:
        return CreateUnloggedTable.build(context.namer, query, exposed_name=exposed_name)


class PostgresSecurityBarrierViewBuilder(CreateFromQueryBuilder[PostgresCreateSecurityBarrierView]):
    """``CREATE VIEW … WITH (security_barrier = true)`` — blocks pushdown, same rows."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> PostgresCreateSecurityBarrierView:
        return PostgresCreateSecurityBarrierView.build(context.namer, query, exposed_name=exposed_name)


class PostgresExtendedStatisticsBuilder(CreateFromQueryBuilder[PostgresCreateExtendedStatistics]):
    """``CREATE STATISTICS`` + ``ANALYZE`` on ≥2 columns — estimate-only Mat."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> Optional[PostgresCreateExtendedStatistics]:
        out_cols = _out_cols(query)
        if len(out_cols) < 2:
            return None
        # Prefer c_pk + another column when present; otherwise the first two.
        if _PK_COLUMN in out_cols:
            other = next(c for c in out_cols if c != _PK_COLUMN)
            stat_cols = (_PK_COLUMN, other)
        else:
            stat_cols = (out_cols[0], out_cols[1])
        body = CreateTable.build(context.namer, query)
        return PostgresCreateExtendedStatistics.build(
            context.namer,
            body,
            out_cols=out_cols,
            stat_cols=stat_cols,
            exposed_name=exposed_name,
        )


class PostgresPartitionedTableMatBuilder(CreateFromQueryBuilder[PostgresCreatePartitionedTable]):
    """``PARTITION BY RANGE (c_pk)`` parent + two partitions + INSERT from CTAS body."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._DML_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> Optional[PostgresCreatePartitionedTable]:
        out_cols = _out_cols(query)
        if _PK_COLUMN not in out_cols:
            return None
        body = CreateTable.build(context.namer, query)
        return PostgresCreatePartitionedTable.build(
            context.namer,
            body,
            out_cols=out_cols,
            partition_key=_PK_COLUMN,
            split_at=5,
            exposed_name=exposed_name,
        )


class PostgresParallelToggleMatBuilder(CreateFromQueryBuilder[PostgresCreateParallelToggle]):
    """Session parallel GUCs on the equivalent connection, then exposing view."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> PostgresCreateParallelToggle:
        out_cols = _out_cols(query)
        body = CreateTable.build(context.namer, query)
        return PostgresCreateParallelToggle.build(
            context.namer, body, out_cols=out_cols, exposed_name=exposed_name
        )
