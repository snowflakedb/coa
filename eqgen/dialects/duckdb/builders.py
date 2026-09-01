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

"""Builders for rewrites and objects only DuckDB can express.

Column codecs use :class:`~eqgen.equivalence.builders.base.ColumnRewriteQueryBuilder`
(algebra **(Rewrite)**). Joins use portable :class:`~eqgen.equivalence.ast.JoinQuery`.
Objects (macro, index, attach, catalog) are **(Mat)**.
"""

from __future__ import annotations

from typing import Callable, ClassVar, Optional, Type

from eqgen.builder.constraint_set import Constraint, ConstraintSet
from eqgen.core.types import CharType, DateType, SqlType, TextType, TimestampType, VarcharType
from eqgen.dialects.duckdb.ast import (
    DuckDBCatalogKind,
    DuckDBCreateAttach,
    DuckDBCreateCatalog,
    DuckDBCreateIndex,
    DuckDBCreateMacro,
    DuckDBPivotStructQuery,
    DuckDBPositionalJoinQuery,
    DuckDBRecursiveCteQuery,
    DuckDBStarReplaceQuery,
)
from eqgen.equivalence.ast import (
    CreateTable,
    CreateView,
    EqNode,
    EquivalentRelation,
    ExceptAllQuery,
    JoinQuery,
    ProjectionItem,
    QueryNode,
    SelectQuery,
)
from eqgen.equivalence.builders.base import ColumnRewriteQueryBuilder, EquivalenceBuilder
from eqgen.equivalence.builders.creates import CreateFromQueryBuilder
from eqgen.equivalence.constraints import RowFilterConstraint
from eqgen.equivalence.context import EquivalenceContext
from eqgen.ir import expr
from eqgen.ir.expr import Comparison

_LEFT, _RIGHT = "l", "r"
_TEXT_TYPES = (VarcharType, TextType, CharType)
_TEMPORAL_TYPES = (DateType, TimestampType)


def _qualified(alias: str, items: tuple[ProjectionItem, ...]) -> tuple[ProjectionItem, ...]:
    return tuple(
        ProjectionItem(item.alias, expr.qualified_col(alias, item.alias, item.data_type), item.data_type)
        for item in items
    )


class DuckDBAntiJoinEmptyRoundTripBuilder(EquivalenceBuilder[JoinQuery]):
    """``ANTI JOIN`` against a relation that cannot have any rows — algebra **(AntiJoin)**.

    Uses portable :class:`JoinQuery` with ``join_type="ANTI"`` and ``ON TRUE``.
    """

    _EMPTY = expr.eq(expr.int_lit(1), expr.int_lit(0))

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return []

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[JoinQuery]:
        del constraint_set
        base_items = self._passthrough_items(context)
        if not base_items:
            return None
        left = self.builder_factory.build_subtree(
            EquivalentRelation,  # type: ignore[type-abstract]
            ConstraintSet([]),
            context,
        )
        right = self.builder_factory.build_subtree(
            EquivalentRelation,  # type: ignore[type-abstract]
            ConstraintSet([RowFilterConstraint(self._EMPTY)]),
            context,
        )
        if left is None or right is None:
            return None
        return JoinQuery(
            left,
            right,
            expr.bool_lit(True),
            "ANTI",
            _qualified(_LEFT, base_items),
            _LEFT,
            _RIGHT,
        )


class DuckDBExceptAllEmptyTableRoundTripBuilder(EquivalenceBuilder[ExceptAllQuery]):
    """``R EXCEPT ALL <empty table>`` — bag identity that keeps the set-op in the plan.

    Portable :class:`~eqgen.equivalence.builders.partitioning.ExceptEmptyRoundTripBuilder`
    uses ``WHERE FALSE``, which DuckDB often erases before ``ASOF``. Materializing the
    empty right as a CTAS table preserves the bag set-op (see
    ``repro/duckdb-20260810-except-all-asof-type-mismatch``).
    """

    _EMPTY = expr.eq(expr.int_lit(1), expr.int_lit(0))

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return []

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[ExceptAllQuery]:
        del constraint_set
        left = self.builder_factory.build_subtree(
            EquivalentRelation,  # type: ignore[type-abstract]
            ConstraintSet([]),
            context,
        )
        right_rel = self.builder_factory.build_subtree(
            EquivalentRelation,  # type: ignore[type-abstract]
            ConstraintSet([RowFilterConstraint(self._EMPTY)]),
            context,
        )
        if left is None or right_rel is None:
            return None
        right = CreateTable.build(context.namer, SelectQuery(right_rel, None))
        return ExceptAllQuery(left, right)


class DuckDBAsofLeftEmptyRoundTripBuilder(EquivalenceBuilder[JoinQuery]):
    """``ASOF LEFT JOIN`` an empty right on a temporal column — left rows preserved.

    Both sides are CTAS-materialized first. On some DuckDB builds, ``ASOF`` over plain
    views against an empty right yields zero rows (tables are fine); materializing keeps
    the identity sound for the wheel sweep and the CLI hunt.
    """

    _EMPTY = expr.eq(expr.int_lit(1), expr.int_lit(0))

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return []

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[JoinQuery]:
        del constraint_set
        base_items = self._passthrough_items(context)
        temporal = next((item for item in base_items if isinstance(item.data_type, _TEMPORAL_TYPES)), None)
        if temporal is None:
            return None
        left_rel = self.builder_factory.build_subtree(
            EquivalentRelation,  # type: ignore[type-abstract]
            ConstraintSet([]),
            context,
        )
        right_rel = self.builder_factory.build_subtree(
            EquivalentRelation,  # type: ignore[type-abstract]
            ConstraintSet([RowFilterConstraint(self._EMPTY)]),
            context,
        )
        if left_rel is None or right_rel is None:
            return None
        left = CreateTable.build(context.namer, SelectQuery(left_rel, None))
        right = CreateTable.build(context.namer, SelectQuery(right_rel, None))
        condition = expr.ge(
            expr.qualified_col(_LEFT, temporal.alias, temporal.data_type),
            expr.qualified_col(_RIGHT, temporal.alias, temporal.data_type),
        )
        return JoinQuery(
            left,
            right,
            condition,
            "ASOF LEFT",
            _qualified(_LEFT, base_items),
            _LEFT,
            _RIGHT,
        )


class DuckDBCreateMacroBuilder(CreateFromQueryBuilder[DuckDBCreateMacro]):
    """A table macro wrapping whatever query it was given, plus a view over it."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]) -> DuckDBCreateMacro:
        return DuckDBCreateMacro.build(context.namer, query, exposed_name=exposed_name)


class DuckDBCreateIndexBuilder(CreateFromQueryBuilder[DuckDBCreateIndex]):
    """CTAS table → ``CREATE INDEX`` (ART) → exposing view. Algebra **(Mat)**."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> Optional[DuckDBCreateIndex]:
        signature = query.get_signature()
        if not signature:
            return None
        out_cols = [named.alias for named in signature]
        body = CreateTable.build(context.namer, query)
        return DuckDBCreateIndex.build(
            context.namer,
            body,
            target=out_cols[0],
            out_cols=out_cols,
            exposed_name=exposed_name,
        )


#: Seed / join / integrity key — must match catalogs + ``sample_rows``.
_PK_COLUMN = "c_pk"


class DuckDBUniqueIndexMatBuilder(CreateFromQueryBuilder[DuckDBCreateIndex]):
    """CTAS → ``CREATE UNIQUE INDEX`` on ``c_pk`` → exposing view. Algebra **(Mat)** / Mat⁺."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> Optional[DuckDBCreateIndex]:
        signature = query.get_signature()
        out_cols = [named.alias for named in signature]
        if _PK_COLUMN not in out_cols:
            return None
        body = CreateTable.build(context.namer, query)
        return DuckDBCreateIndex.build(
            context.namer,
            body,
            target=_PK_COLUMN,
            out_cols=out_cols,
            exposed_name=exposed_name,
            unique=True,
        )


class DuckDBAttachedDatabaseBuilder(CreateFromQueryBuilder[DuckDBCreateAttach]):
    """``ATTACH ':memory:'`` + cross-catalog mirror view. Algebra **(Mat)**."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> DuckDBCreateAttach:
        body = CreateView.build(context.namer, query)
        return DuckDBCreateAttach.build(context.namer, body, exposed_name=exposed_name)


class DuckDBCatalogObjectBuilder(CreateFromQueryBuilder[DuckDBCreateCatalog]):
    """Shared plumbing for catalog-object round trips."""

    KIND: ClassVar[DuckDBCatalogKind]

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> DuckDBCreateCatalog:
        body = CreateView.build(context.namer, query)
        return DuckDBCreateCatalog.build(
            context.namer,
            body,
            kind=self.KIND,
            extra_col=context.names.generate_column_name("eq_dd_aux"),
            exposed_name=exposed_name,
        )


class DuckDBSchemaQualifiedTableBuilder(DuckDBCatalogObjectBuilder):
    """Materialize into a fresh schema and read back schema-qualified. Algebra **(Mat)**."""

    KIND = DuckDBCatalogKind.SCHEMA


class DuckDBAddDropColumnTableBuilder(DuckDBCatalogObjectBuilder):
    """``ALTER TABLE … ADD COLUMN`` then ``DROP COLUMN``. Algebra **(Mat)**."""

    KIND = DuckDBCatalogKind.ADD_DROP_COLUMN


class DuckDBCheckpointTableBuilder(DuckDBCatalogObjectBuilder):
    """CTAS, then ``CHECKPOINT`` before the exposing view reads it back. Algebra **(Mat)**.

    ``CHECKPOINT`` forces the table through the persistent row-group/compression write path
    instead of staying purely in-memory — same rows and signature, different storage layer.
    """

    KIND = DuckDBCatalogKind.CHECKPOINT


class DuckDBEnumTypeRoundTripBuilder(DuckDBCatalogObjectBuilder):
    """``CREATE TYPE AS ENUM`` from distinct text, cast through and back. Algebra **(Mat)**.

    Declines when the body has no text column.
    """

    KIND = DuckDBCatalogKind.ENUM_ROUND_TRIP

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> Optional[DuckDBCreateCatalog]:
        text_col = next(
            (named.alias for named in query.get_signature() if isinstance(named.target, _TEXT_TYPES)),
            None,
        )
        if text_col is None:
            return None
        body = CreateView.build(context.namer, query)
        return DuckDBCreateCatalog.build(
            context.namer,
            body,
            kind=self.KIND,
            text_col=text_col,
            exposed_name=exposed_name,
        )


class DuckDBPositionalJoinRoundTripBuilder(EquivalenceBuilder[DuckDBPositionalJoinQuery]):
    """Split columns, ``ORDER BY`` key, recombine with ``POSITIONAL JOIN`` — **(Positional)**."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return []

    def _build(
        self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext
    ) -> Optional[DuckDBPositionalJoinQuery]:
        del constraint_set
        key = context.names.generate_column_name("eq_pj")
        keyed = self._materialize_row_key(context, key)
        if keyed is None or len(keyed.base_items) < 2:
            return None
        cols = tuple(item.alias for item in keyed.base_items)
        split = len(cols) // 2
        return DuckDBPositionalJoinQuery(keyed.relation, key, cols[:split], cols[split:], keyed.base_items)


#: Comfortably above any row count this harness ever samples, so the bound is never the reason a
#: row is kept — the filter is an identity for any table, not just today's fixture. Kept well
#: under 1,000,000: DuckDB's TopNWindowElimination rewrites this exact filter shape into an
#: internal arg_min/arg_max list aggregate that throws above that value regardless of the table's
#: actual size (repro/duckdb-20260811-qualify-topn-elimination-argminmax-n-cap) — filed upstream,
#: but a builder that always trips a known engine defect never produces a usable equivalence, so
#: this stays under the cap to keep exercising the pass's main path instead of only its edge.
_ROW_NUMBER_BOUND = 100_000


class DuckDBRowNumberBoundQualifyBuilder(EquivalenceBuilder[SelectQuery]):
    """``QUALIFY ROW_NUMBER() OVER (ORDER BY c0) <= <huge literal>`` — algebra **(Qualify)**.

    ``QualifyQueryBuilder`` already covers ``ROW_NUMBER() >= 1`` (identity, unbounded above), which
    has no upper bound to fold and so never matches DuckDB's ``row_number_rewriter`` /
    ``topn_window_elimination`` passes — those specifically pattern-match a *bounded-above*
    ``row_number() <= k`` filter to rewrite the window into a physical Top-N operator. This is that
    shape, with ``k`` fixed far above any possible row count so the filter is still an identity by
    construction rather than by the data happening to be small.
    """

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return []

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[SelectQuery]:
        del constraint_set
        items = self._passthrough_items(context)
        if not items:
            return None
        source = self._dispatch_source(context)
        if source is None:
            return None
        order_col = expr.col(items[0].alias, items[0].data_type)
        keep_all = Comparison("<=", expr.row_number((order_col,)), expr.int_lit(_ROW_NUMBER_BOUND))
        return SelectQuery(source, items, qualify=keep_all)


class DuckDBRecursiveCteIdentityBuilder(EquivalenceBuilder[DuckDBRecursiveCteQuery]):
    """Recursive CTE whose recursive arm is empty — same rows as the anchor."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return []

    def _build(
        self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext
    ) -> Optional[DuckDBRecursiveCteQuery]:
        del constraint_set
        items = self._passthrough_items(context)
        if not items:
            return None
        source = self.builder_factory.build_subtree(
            EquivalentRelation,  # type: ignore[type-abstract]
            ConstraintSet([]),
            context,
        )
        if source is None:
            return None
        name = context.names.generate_object_name(f"{context.base_table.table_name}_rcte")
        return DuckDBRecursiveCteQuery(source, name, items)


class DuckDBStarReplaceIdentityBuilder(EquivalenceBuilder[DuckDBStarReplaceQuery]):
    """``SELECT * REPLACE (c AS c)`` — identity through DuckDB's star-REPLACE path."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return []

    def _build(
        self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext
    ) -> Optional[DuckDBStarReplaceQuery]:
        del constraint_set
        items = self._passthrough_items(context)
        if not items:
            return None
        source = self.builder_factory.build_subtree(
            EquivalentRelation,  # type: ignore[type-abstract]
            ConstraintSet([]),
            context,
        )
        if source is None:
            return None
        return DuckDBStarReplaceQuery(source, items[0].alias, items)


class DuckDBPivotStructRoundTripBuilder(EquivalenceBuilder[DuckDBPivotStructQuery]):
    """Pack non-key columns into a STRUCT, ``PIVOT`` on a constant key, unpack."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return []

    def _build(
        self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext
    ) -> Optional[DuckDBPivotStructQuery]:
        del constraint_set
        items = self._passthrough_items(context)
        aliases = [item.alias for item in items]
        if _PK_COLUMN not in aliases or len(aliases) < 2:
            return None
        measures = [a for a in aliases if a != _PK_COLUMN]
        source = self.builder_factory.build_subtree(
            EquivalentRelation,  # type: ignore[type-abstract]
            ConstraintSet([]),
            context,
        )
        if source is None:
            return None
        return DuckDBPivotStructQuery(source, _PK_COLUMN, measures, items)


class DuckDBListPackRoundTripBuilder(ColumnRewriteQueryBuilder):
    """``[c][1]`` per column — list pack/extract identity **(Rewrite)**.

    """

    def _column_rewriter(self, context: EquivalenceContext) -> Callable[[str, SqlType], Optional[expr.ExpressionNode]]:
        del context

        def rewrite(name: str, data_type: SqlType) -> Optional[expr.ExpressionNode]:
            return expr.raw_expr(f"[{name}][1]", data_type)

        return rewrite


class DuckDBListTransformRoundTripBuilder(ColumnRewriteQueryBuilder):
    """``list_transform([c], lambda x: x)[1]`` — identity TRANSFORM over a 1-element list **(Rewrite)**.

    """

    def _column_rewriter(self, context: EquivalenceContext) -> Callable[[str, SqlType], Optional[expr.ExpressionNode]]:
        del context

        def rewrite(name: str, data_type: SqlType) -> Optional[expr.ExpressionNode]:
            return expr.raw_expr(f"list_transform([{name}], lambda x: x)[1]", data_type)

        return rewrite


class DuckDBListFilterRoundTripBuilder(ColumnRewriteQueryBuilder):
    """``list_filter([c], lambda x: 1 = 1)[1]`` — always-true FILTER over a 1-element list **(Rewrite)**.

    """

    def _column_rewriter(self, context: EquivalenceContext) -> Callable[[str, SqlType], Optional[expr.ExpressionNode]]:
        del context

        def rewrite(name: str, data_type: SqlType) -> Optional[expr.ExpressionNode]:
            return expr.raw_expr(f"list_filter([{name}], lambda x: 1 = 1)[1]", data_type)

        return rewrite


class DuckDBMapPackRoundTripBuilder(ColumnRewriteQueryBuilder):
    """``map(['k'], [c])['k']`` per column — map pack/extract identity **(Rewrite)**."""

    def _column_rewriter(self, context: EquivalenceContext) -> Callable[[str, SqlType], Optional[expr.ExpressionNode]]:
        del context

        def rewrite(name: str, data_type: SqlType) -> Optional[expr.ExpressionNode]:
            return expr.raw_expr(f"map(['k'], [{name}])['k']", data_type)

        return rewrite


class DuckDBStructPackRoundTripBuilder(EquivalenceBuilder[SelectQuery]):
    """Pack the row into a ``STRUCT`` and project fields back — **(Rewrite)** via whole-row pack.

    Not a per-column :class:`ColumnRewriteQueryBuilder` because the pack needs every column at once.
    """

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return []

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[SelectQuery]:
        del constraint_set
        items = self._passthrough_items(context)
        if not items:
            return None
        source = self._dispatch_source(context)
        if source is None:
            return None
        cols = [item.alias for item in items]
        packed = ", ".join(f"{c} := {c}" for c in cols)
        pack_sql = f"struct_pack({packed})"
        inner_items = (ProjectionItem("s", expr.raw_expr(pack_sql, items[0].data_type), items[0].data_type),)
        inner = CreateView.build(context.namer, SelectQuery(source, inner_items))
        outer = tuple(
            ProjectionItem(c, expr.raw_expr(f"(s).{c}", item.data_type), item.data_type)
            for c, item in zip(cols, items)
        )
        return SelectQuery(inner, outer)
