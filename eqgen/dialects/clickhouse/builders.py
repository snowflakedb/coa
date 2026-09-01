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

"""ClickHouse-only builders — physical MergeTree layout variations, row-neutral (Mat),
plus identity expression rewrites that force analyzer/constant-fold paths.
"""

from __future__ import annotations

import random
from typing import Callable, ClassVar, Optional, Type

from eqgen.builder.constraint_set import Constraint
from eqgen.core.types import SqlType, VarcharType
from eqgen.dialects.clickhouse.ast import (
    ClickHouseCodec,
    ClickHouseCreateCodec,
    ClickHouseCreatePartLayout,
    ClickHouseCreateProjection,
    ClickHouseCreateSkipIndex,
    ClickHousePartLayoutKind,
    ClickHouseSkipIndexType,
    bloom_filter_columns,
    numeric_columns,
    string_columns,
)
from eqgen.equivalence.ast import CreateTable, EqNode, EquivalentRelation, QueryNode
from eqgen.equivalence.builders.base import ColumnRewriteQueryBuilder
from eqgen.equivalence.builders.creates import CreateFromQueryBuilder
from eqgen.equivalence.context import EquivalenceContext
from eqgen.ir import expr


def _out_cols(query: QueryNode) -> tuple[str, ...]:
    return tuple(named.alias for named in query.get_signature())


class _ClickHousePhysicalBase(CreateFromQueryBuilder[EquivalentRelation]):
    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> Optional[EquivalentRelation]:
        out_cols = _out_cols(query)
        if not out_cols:
            return None
        body = CreateTable.build(context.namer, query)
        return self._alter(body, out_cols, context, exposed_name)

    def _alter(
        self,
        body: CreateTable,
        out_cols: tuple[str, ...],
        context: EquivalenceContext,
        exposed_name: Optional[str],
    ) -> Optional[EquivalentRelation]:
        raise NotImplementedError


class ClickHouseProjectionBuilder(_ClickHousePhysicalBase):
    """``ALTER TABLE … ADD PROJECTION`` + ``MATERIALIZE PROJECTION``."""

    def _alter(self, body, out_cols, context, exposed_name):
        order_by = random.choice(out_cols)
        return ClickHouseCreateProjection.build(
            context.namer, body, out_cols=out_cols, order_by=order_by, exposed_name=exposed_name
        )


class _ClickHouseSkipIndexBase(_ClickHousePhysicalBase):
    INDEX_TYPE: ClickHouseSkipIndexType

    def _alter(self, body, out_cols, context, exposed_name):
        column = random.choice(out_cols)
        return ClickHouseCreateSkipIndex.build(
            context.namer,
            body,
            out_cols=out_cols,
            index_type=self.INDEX_TYPE,
            column=column,
            exposed_name=exposed_name,
        )


class ClickHouseMinMaxIndexBuilder(_ClickHouseSkipIndexBase):
    INDEX_TYPE = ClickHouseSkipIndexType.MINMAX


class ClickHouseSetIndexBuilder(_ClickHouseSkipIndexBase):
    INDEX_TYPE = ClickHouseSkipIndexType.SET


class ClickHouseBloomIndexBuilder(_ClickHouseSkipIndexBase):
    INDEX_TYPE = ClickHouseSkipIndexType.BLOOM_FILTER

    def _alter(self, body, out_cols, context, exposed_name):
        # Decimal / Date32 raise Code 44 "Unexpected type … of bloom filter index".
        safe = bloom_filter_columns(body, out_cols)
        if not safe:
            return None
        column = random.choice(safe)
        return ClickHouseCreateSkipIndex.build(
            context.namer,
            body,
            out_cols=out_cols,
            index_type=self.INDEX_TYPE,
            column=column,
            exposed_name=exposed_name,
        )


class _ClickHousePartLayoutBase(_ClickHousePhysicalBase):
    KIND: ClickHousePartLayoutKind

    def _alter(self, body, out_cols, context, exposed_name):
        keys = numeric_columns(body) or out_cols
        return ClickHouseCreatePartLayout.build(
            context.namer,
            body,
            out_cols=out_cols,
            kind=self.KIND,
            key_column=keys[0],
            exposed_name=exposed_name,
        )


class ClickHouseSortedTableBuilder(_ClickHousePartLayoutBase):
    KIND = ClickHousePartLayoutKind.SORTED

    def _alter(self, body, out_cols, context, exposed_name):
        # Prefer a non-pk numeric leading key so GROUP BY sees duplicate prefixes —
        # needed to surface optimize_aggregation_in_order wrong results (#111901).
        keys = list(numeric_columns(body) or out_cols)
        non_pk = [c for c in keys if c != "c_pk"]
        key = random.choice(non_pk) if non_pk else keys[0]
        return ClickHouseCreatePartLayout.build(
            context.namer,
            body,
            out_cols=out_cols,
            kind=self.KIND,
            key_column=key,
            exposed_name=exposed_name,
        )


class ClickHousePartitionedTableBuilder(_ClickHousePartLayoutBase):
    KIND = ClickHousePartLayoutKind.PARTITIONED


class ClickHouseFineGranuleTableBuilder(_ClickHousePartLayoutBase):
    KIND = ClickHousePartLayoutKind.FINE_GRANULES


class _ClickHouseCodecBase(_ClickHousePhysicalBase):
    CODEC: ClickHouseCodec

    def _columns_for(self, body: CreateTable, out_cols: tuple[str, ...]) -> tuple[str, ...]:
        return out_cols

    def _alter(self, body, out_cols, context, exposed_name):
        columns = self._columns_for(body, out_cols)
        if not columns:
            return None
        return ClickHouseCreateCodec.build(
            context.namer,
            body,
            out_cols=out_cols,
            codec=self.CODEC,
            codec_columns=columns,
            exposed_name=exposed_name,
        )


class ClickHouseZstdCodecBuilder(_ClickHouseCodecBase):
    CODEC = ClickHouseCodec.ZSTD


class ClickHouseDeltaCodecBuilder(_ClickHouseCodecBase):
    CODEC = ClickHouseCodec.DELTA_ZSTD

    def _columns_for(self, body: CreateTable, out_cols: tuple[str, ...]) -> tuple[str, ...]:
        numeric = set(numeric_columns(body))
        return tuple(col for col in out_cols if col in numeric)


# --- Identity expression rewrites (force analyzer / fold paths) -------------------------------


class ClickHouseTupleElementRoundTripBuilder(ColumnRewriteQueryBuilder):
    """``tupleElement(tuple(c), 1)`` — pack/extract identity for every column."""

    def _column_rewriter(self, context: EquivalenceContext) -> Callable[[str, SqlType], Optional[expr.ExpressionNode]]:
        del context

        def rewrite(name: str, data_type: SqlType) -> Optional[expr.ExpressionNode]:
            return expr.raw_expr(f"tupleElement(tuple({name}), 1)", data_type)

        return rewrite


class ClickHouseArrayElementRoundTripBuilder(ColumnRewriteQueryBuilder):
    """``arrayElement([c], 1)`` — array pack/extract identity for every column."""

    def _column_rewriter(self, context: EquivalenceContext) -> Callable[[str, SqlType], Optional[expr.ExpressionNode]]:
        del context

        def rewrite(name: str, data_type: SqlType) -> Optional[expr.ExpressionNode]:
            return expr.raw_expr(f"arrayElement([{name}], 1)", data_type)

        return rewrite


class ClickHouseMapElementRoundTripBuilder(ColumnRewriteQueryBuilder):
    """``map('k', c)['k']`` — Map pack/extract identity for every column."""

    def _column_rewriter(self, context: EquivalenceContext) -> Callable[[str, SqlType], Optional[expr.ExpressionNode]]:
        del context

        def rewrite(name: str, data_type: SqlType) -> Optional[expr.ExpressionNode]:
            return expr.raw_expr(f"map('k', {name})['k']", data_type)

        return rewrite


class ClickHouseCoalesceSelfRoundTripBuilder(ColumnRewriteQueryBuilder):
    """``coalesce(c, c)`` — forces null-handling / constant-fold paths."""

    def _column_rewriter(self, context: EquivalenceContext) -> Callable[[str, SqlType], Optional[expr.ExpressionNode]]:
        del context

        def rewrite(name: str, data_type: SqlType) -> Optional[expr.ExpressionNode]:
            return expr.raw_expr(f"coalesce({name}, {name})", data_type)

        return rewrite


class ClickHouseTokenBfIndexBuilder(_ClickHouseSkipIndexBase):
    INDEX_TYPE = ClickHouseSkipIndexType.TOKENBF

    def _alter(self, body, out_cols, context, exposed_name):
        safe = string_columns(body, out_cols)
        if not safe:
            return None
        column = random.choice(safe)
        return ClickHouseCreateSkipIndex.build(
            context.namer,
            body,
            out_cols=out_cols,
            index_type=self.INDEX_TYPE,
            column=column,
            exposed_name=exposed_name,
        )


class ClickHouseNgramBfIndexBuilder(_ClickHouseSkipIndexBase):
    INDEX_TYPE = ClickHouseSkipIndexType.NGRAMBF

    def _alter(self, body, out_cols, context, exposed_name):
        safe = string_columns(body, out_cols)
        if not safe:
            return None
        column = random.choice(safe)
        return ClickHouseCreateSkipIndex.build(
            context.namer,
            body,
            out_cols=out_cols,
            index_type=self.INDEX_TYPE,
            column=column,
            exposed_name=exposed_name,
        )


CLICKHOUSE_NATIVE_BUILDERS: frozenset[str] = frozenset(
    {
        "ClickHouseProjectionBuilder",
        "ClickHouseMinMaxIndexBuilder",
        "ClickHouseSetIndexBuilder",
        "ClickHouseBloomIndexBuilder",
        "ClickHouseTokenBfIndexBuilder",
        "ClickHouseNgramBfIndexBuilder",
        "ClickHouseSortedTableBuilder",
        "ClickHousePartitionedTableBuilder",
        "ClickHouseFineGranuleTableBuilder",
        "ClickHouseZstdCodecBuilder",
        "ClickHouseDeltaCodecBuilder",
        "ClickHouseTupleElementRoundTripBuilder",
        "ClickHouseArrayElementRoundTripBuilder",
        "ClickHouseMapElementRoundTripBuilder",
        "ClickHouseCoalesceSelfRoundTripBuilder",
    }
)
