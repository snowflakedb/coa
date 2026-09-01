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

"""SQLite-native Mat / Rewrite builders — index / ATTACH / WITHOUT ROWID / generated / STRICT / codecs."""

from __future__ import annotations

import random
from typing import Callable, ClassVar, Optional, Type

from eqgen.builder.constraint_set import Constraint, ConstraintSet
from eqgen.core.types import DoubleType, IntegerType, NumericType, SqlType, TextType, VarcharType
from eqgen.dialects.sqlite.ast import (
    SqliteCreateAnalyzeIndex,
    SqliteCreateAttach,
    SqliteCreateExprIndex,
    SqliteCreateGenerated,
    SqliteCreateIndex,
    SqliteCreateStrict,
    SqliteCreateWithoutRowid,
    SqliteCreateWithoutRowidIndex,
    SqliteNestedMaterializedCteQuery,
    SqliteRecursiveCteQuery,
)
from eqgen.dialects.sqlite.types_sql import sqlite_type
from eqgen.equivalence.ast import CreateTable, CreateView, EqNode, EquivalentRelation, QueryNode
from eqgen.equivalence.builders.base import ColumnRewriteQueryBuilder, EquivalenceBuilder
from eqgen.equivalence.builders.creates import CreateFromQueryBuilder
from eqgen.equivalence.context import EquivalenceContext
from eqgen.ir import expr

_PK_COLUMN = "c_pk"
_TEXT_TYPES = (TextType, VarcharType)
_NUMERIC_TYPES = (IntegerType, NumericType, DoubleType)

#: Variant spellings drawn per build. ``random`` is seeded from ``--seed`` (see
#: ``equivalence/generator.py``), which is what makes a finding replayable — drawing these from
#: ``hash()`` instead would not, because Python randomizes string hashing per process.
#:
#: Always-true / always-false partial-index predicates, in both spellings SQLite accepts.
_PARTIAL_INDEX_PREDICATES = ("0", "1", "FALSE", "TRUE")
#: ``+ 0`` keeps integer affinity; ABS is a second planner path.
_EXPR_INDEX_SPELLINGS = ("({col} + 0)", "(ABS({col}))")
#: Full / truthy-partial / IS NOT NULL, so ANALYZE sees varied index shapes.
_ANALYZE_INDEX_PREDICATES = ("", "{col}", "{col} IS NOT NULL")


def _col_defs(signature: list, *, strict: bool = False) -> list[str]:
    """``name TYPE [NOT NULL]`` for CREATE TABLE from a signature of Named[SqlType].

    STRICT tables only allow INT/INTEGER/REAL/TEXT/BLOB/ANY — map NUMERIC → INTEGER.
    """
    out = []
    for named in signature:
        type_sql = sqlite_type(named.target)
        if strict and type_sql == "NUMERIC":
            # STRICT forbids NUMERIC; REAL accepts both ints and fractional decimals losslessly
            # enough for our seed rows (unlike INTEGER, which rejects 12.34).
            type_sql = "REAL"
        piece = f"{named.alias} {type_sql}"
        if named.alias == _PK_COLUMN:
            piece += " NOT NULL"
        out.append(piece)
    return out


def _out_cols(signature: list) -> list[str]:
    return [named.alias for named in signature]


class SqliteCreateIndexBuilder(CreateFromQueryBuilder[SqliteCreateIndex]):
    """CTAS → ``CREATE INDEX`` → exposing view. Algebra **(Mat)**."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> Optional[SqliteCreateIndex]:
        signature = query.get_signature()
        if not signature:
            return None
        out_cols = _out_cols(signature)
        target = next((c for c in out_cols if c != _PK_COLUMN), out_cols[0])
        body = CreateTable.build(context.namer, query)
        return SqliteCreateIndex.build(
            context.namer,
            body,
            target=target,
            out_cols=out_cols,
            exposed_name=exposed_name,
        )


class SqliteUniqueIndexMatBuilder(CreateFromQueryBuilder[SqliteCreateIndex]):
    """CTAS → ``CREATE UNIQUE INDEX`` on ``c_pk`` → view. Algebra **(Mat)**."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> Optional[SqliteCreateIndex]:
        out_cols = _out_cols(query.get_signature())
        if _PK_COLUMN not in out_cols:
            return None
        body = CreateTable.build(context.namer, query)
        return SqliteCreateIndex.build(
            context.namer,
            body,
            target=_PK_COLUMN,
            out_cols=out_cols,
            exposed_name=exposed_name,
            unique=True,
        )


class SqlitePartialIndexBuilder(CreateFromQueryBuilder[SqliteCreateIndex]):
    """CTAS → partial ``CREATE INDEX … WHERE col IS NOT NULL`` → view. Algebra **(Mat)**."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> Optional[SqliteCreateIndex]:
        signature = query.get_signature()
        out_cols = _out_cols(signature)
        # Prefer a nullable non-pk column for the WHERE predicate.
        where_col = next((c for c in out_cols if c != _PK_COLUMN), None)
        if where_col is None:
            return None
        body = CreateTable.build(context.namer, query)
        return SqliteCreateIndex.build(
            context.namer,
            body,
            target=_PK_COLUMN if _PK_COLUMN in out_cols else where_col,
            out_cols=out_cols,
            exposed_name=exposed_name,
            where_sql=f"{where_col} IS NOT NULL",
        )


class SqliteTruthyPartialIndexBuilder(CreateFromQueryBuilder[SqliteCreateIndex]):
    """CTAS → partial ``CREATE INDEX … WHERE col`` (truthy, not IS NOT NULL). Algebra **(Mat)**.

    Matches the planner surface behind historical RIGHT/FULL+partial-index wrong results
    (forum c4676c4956 / 7dee41d32506c4ae): the partial WHERE is a bare column, so falsey
    rows are absent from the index.
    """

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> Optional[SqliteCreateIndex]:
        signature = query.get_signature()
        out_cols = _out_cols(signature)
        # Prefer nullable numeric/text non-pk — bare ``WHERE c_int`` / ``WHERE c_txt``.
        where_col = next(
            (
                named.alias
                for named in signature
                if named.alias != _PK_COLUMN and isinstance(named.target, _TEXT_TYPES + _NUMERIC_TYPES)
            ),
            None,
        )
        if where_col is None:
            return None
        body = CreateTable.build(context.namer, query)
        return SqliteCreateIndex.build(
            context.namer,
            body,
            target=where_col,
            out_cols=out_cols,
            exposed_name=exposed_name,
            where_sql=where_col,
        )


class SqliteConstantPartialIndexBuilder(CreateFromQueryBuilder[SqliteCreateIndex]):
    """CTAS → partial index with constant ``WHERE 0`` / ``WHERE 1`` / ``WHERE FALSE``. Algebra **(Mat)**.

    Constant partial-index predicates have bitten the planner around RIGHT/FULL joins
    (forum 740700bc57eb53b4 — ``WHERE FALSE`` index).
    """

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> Optional[SqliteCreateIndex]:
        signature = query.get_signature()
        out_cols = _out_cols(signature)
        if not out_cols:
            return None
        target = next((c for c in out_cols if c != _PK_COLUMN), out_cols[0])
        pred = random.choice(_PARTIAL_INDEX_PREDICATES)
        body = CreateTable.build(context.namer, query)
        return SqliteCreateIndex.build(
            context.namer,
            body,
            target=target,
            out_cols=out_cols,
            exposed_name=exposed_name,
            where_sql=pred,
        )


class SqliteAttachRoundTripBuilder(CreateFromQueryBuilder[SqliteCreateAttach]):
    """``ATTACH ':memory:'`` + cross-schema mirror view. Algebra **(Mat)**."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> SqliteCreateAttach:
        body = CreateView.build(context.namer, query)
        return SqliteCreateAttach.build(context.namer, body, exposed_name=exposed_name)


class SqliteWithoutRowidTableBuilder(CreateFromQueryBuilder[SqliteCreateWithoutRowid]):
    """``WITHOUT ROWID`` table with ``PRIMARY KEY(c_pk)``. Algebra **(Mat)**."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> Optional[SqliteCreateWithoutRowid]:
        signature = query.get_signature()
        out_cols = _out_cols(signature)
        if _PK_COLUMN not in out_cols:
            return None
        body = CreateView.build(context.namer, query)
        return SqliteCreateWithoutRowid.build(
            context.namer,
            body,
            col_defs=_col_defs(signature),
            out_cols=out_cols,
            pk_col=_PK_COLUMN,
            exposed_name=exposed_name,
        )


class SqliteGeneratedColumnRoundTripBuilder(CreateFromQueryBuilder[SqliteCreateGenerated]):
    """VIRTUAL ``GENERATED ALWAYS AS (c_pk)`` then project it away. Algebra **(Mat)**."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> Optional[SqliteCreateGenerated]:
        signature = query.get_signature()
        out_cols = _out_cols(signature)
        if _PK_COLUMN not in out_cols:
            return None
        body = CreateView.build(context.namer, query)
        return SqliteCreateGenerated.build(
            context.namer,
            body,
            col_defs=_col_defs(signature),
            out_cols=out_cols,
            gen_of=_PK_COLUMN,
            stored=False,
            exposed_name=exposed_name,
        )


class SqliteStoredGeneratedColumnRoundTripBuilder(CreateFromQueryBuilder[SqliteCreateGenerated]):
    """STORED ``GENERATED ALWAYS AS (c_pk)`` then project it away. Algebra **(Mat)**."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> Optional[SqliteCreateGenerated]:
        signature = query.get_signature()
        out_cols = _out_cols(signature)
        if _PK_COLUMN not in out_cols:
            return None
        body = CreateView.build(context.namer, query)
        return SqliteCreateGenerated.build(
            context.namer,
            body,
            col_defs=_col_defs(signature),
            out_cols=out_cols,
            gen_of=_PK_COLUMN,
            stored=True,
            exposed_name=exposed_name,
        )


class SqliteStrictTableBuilder(CreateFromQueryBuilder[SqliteCreateStrict]):
    """``CREATE TABLE … STRICT`` + insert from body. Algebra **(Mat)**. Needs SQLite ≥ 3.37."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> Optional[SqliteCreateStrict]:
        signature = query.get_signature()
        # STRICT forbids NUMERIC; remapping to REAL would change affinity and break LIKE/COMPARE
        # equivalence against the base table's NUMERIC columns — decline instead.
        if any(sqlite_type(named.target) == "NUMERIC" for named in signature):
            return None
        body = CreateView.build(context.namer, query)
        return SqliteCreateStrict.build(
            context.namer,
            body,
            col_defs=_col_defs(signature, strict=True),
            out_cols=_out_cols(signature),
            exposed_name=exposed_name,
        )


class SqliteExpressionIndexMatBuilder(CreateFromQueryBuilder[SqliteCreateExprIndex]):
    """CTAS → ``CREATE INDEX ON t((col + 0))`` / ``((ABS(col)))`` → view. Algebra **(Mat)**."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> Optional[SqliteCreateExprIndex]:
        signature = query.get_signature()
        out_cols = _out_cols(signature)
        numeric = next(
            (named.alias for named in signature if isinstance(named.target, _NUMERIC_TYPES) and named.alias != _PK_COLUMN),
            _PK_COLUMN if _PK_COLUMN in out_cols else None,
        )
        if numeric is None:
            return None
        expr_sql = random.choice(_EXPR_INDEX_SPELLINGS).format(col=numeric)
        body = CreateTable.build(context.namer, query)
        return SqliteCreateExprIndex.build(
            context.namer,
            body,
            out_cols=out_cols,
            expr_sql=expr_sql,
            exposed_name=exposed_name,
        )


class SqliteWithoutRowidIndexedBuilder(CreateFromQueryBuilder[SqliteCreateWithoutRowidIndex]):
    """WITHOUT ROWID table + secondary index on a non-pk column. Algebra **(Mat)**."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> Optional[SqliteCreateWithoutRowidIndex]:
        signature = query.get_signature()
        out_cols = _out_cols(signature)
        if _PK_COLUMN not in out_cols:
            return None
        index_col = next((c for c in out_cols if c != _PK_COLUMN), None)
        if index_col is None:
            return None
        body = CreateView.build(context.namer, query)
        return SqliteCreateWithoutRowidIndex.build(
            context.namer,
            body,
            col_defs=_col_defs(signature),
            out_cols=out_cols,
            pk_col=_PK_COLUMN,
            index_col=index_col,
            exposed_name=exposed_name,
        )


class SqliteRecursiveCteIdentityBuilder(EquivalenceBuilder[SqliteRecursiveCteQuery]):
    """Recursive CTE whose recursive arm is empty — same rows as the anchor."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return []

    def _build(
        self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext
    ) -> Optional[SqliteRecursiveCteQuery]:
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
        return SqliteRecursiveCteQuery(source, name, items)


class _SqliteNullSafeCodecBase(ColumnRewriteQueryBuilder):
    """NULL-safe rewrite: ``CASE WHEN c IS NULL THEN NULL ELSE <wrap> END``."""

    _WRAP: ClassVar[str]
    _FAMILIES: ClassVar[tuple[type, ...]]

    def _column_rewriter(self, context: EquivalenceContext) -> Callable[[str, SqlType], Optional[expr.ExpressionNode]]:
        del context
        wrap = self._WRAP
        families = self._FAMILIES

        def rewrite(name: str, data_type: SqlType) -> Optional[expr.ExpressionNode]:
            if not isinstance(data_type, families):
                return None
            inner = wrap.format(col=name, typ=sqlite_type(data_type))
            return expr.case_when(
                expr.is_null(expr.col(name, data_type)),
                expr.typed_null(data_type),
                expr.raw_expr(inner, data_type),
                data_type,
            )

        return rewrite


class SqliteNestedMaterializedCteBuilder(EquivalenceBuilder[SqliteNestedMaterializedCteQuery]):
    """``WITH a AS MATERIALIZED (…), b AS MATERIALIZED (SELECT * FROM a) SELECT * FROM b``."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return []

    def _build(
        self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext
    ) -> Optional[SqliteNestedMaterializedCteQuery]:
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
        base = context.base_table.table_name
        inner = context.names.generate_object_name(f"{base}_m1")
        outer = context.names.generate_object_name(f"{base}_m2")
        return SqliteNestedMaterializedCteQuery(source, inner, outer, items)


class SqliteAnalyzeIndexMatBuilder(CreateFromQueryBuilder[SqliteCreateAnalyzeIndex]):
    """CTAS → index (often partial) → ``ANALYZE`` → view. Algebra **(Mat)**.

    Fresh stats steer the planner into index/scan choices that bare CREATE INDEX may not.
    """

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> Optional[SqliteCreateAnalyzeIndex]:
        signature = query.get_signature()
        out_cols = _out_cols(signature)
        target = next((c for c in out_cols if c != _PK_COLUMN), out_cols[0] if out_cols else None)
        if target is None:
            return None
        where_sql = random.choice(_ANALYZE_INDEX_PREDICATES).format(col=target)
        body = CreateTable.build(context.namer, query)
        return SqliteCreateAnalyzeIndex.build(
            context.namer,
            body,
            target=target,
            out_cols=out_cols,
            where_sql=where_sql,
            exposed_name=exposed_name,
        )
