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

"""CrateDB-only builders — physical layout variations, row-neutral (Mat)."""

from __future__ import annotations

from typing import Optional, Type

from eqgen.builder.constraint_set import Constraint
from eqgen.dialects.cratedb.ast import (
    CrateCreateColumnIndex,
    CrateCreateObjectPack,
    CrateCreatePartitioned,
    CrateCreateShardLayout,
    CrateIndexMode,
    bucketable_columns,
    fulltext_columns,
    indexable_columns,
    indexable_non_numeric_columns,
)
from eqgen.equivalence.ast import CreateTable, EqNode, EquivalentRelation, QueryNode
from eqgen.equivalence.builders.creates import CreateFromQueryBuilder
from eqgen.equivalence.context import EquivalenceContext


def _out_cols(query: QueryNode) -> tuple[str, ...]:
    return tuple(named.alias for named in query.get_signature())


class _CrateDbNativeBase(CreateFromQueryBuilder[EquivalentRelation]):
    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> Optional[EquivalentRelation]:
        out_cols = _out_cols(query)
        if not out_cols:
            return None
        body = CreateTable.build(context.namer, query)
        return self._layout(body, out_cols, context, exposed_name)

    def _layout(
        self,
        body: CreateTable,
        out_cols: tuple[str, ...],
        context: EquivalenceContext,
        exposed_name: Optional[str],
    ) -> Optional[EquivalentRelation]:
        raise NotImplementedError


class CrateDbIndexOffBuilder(_CrateDbNativeBase):
    """Per-column ``INDEX OFF`` on every indexable *non-NUMERIC* column.

    NUMERIC/DECIMAL is excluded: ``INDEX OFF`` + a range predicate on that type is a known
    silent-empty-result bug (``repro/cratedb-20260813-round2-numeric-index-off-range``).
    """

    def _layout(self, body, out_cols, context, exposed_name):
        index_columns = indexable_non_numeric_columns(body)
        if not index_columns:
            return None
        return CrateCreateColumnIndex.build(
            context.namer,
            body,
            out_cols=out_cols,
            mode=CrateIndexMode.INDEX_OFF,
            index_columns=index_columns,
            exposed_name=exposed_name,
        )


class CrateDbColumnstoreOffBuilder(_CrateDbNativeBase):
    """Per-column ``STORAGE WITH (columnstore = false)``. Independent of ``INDEX OFF``:
    CrateDB's range path can use either the Lucene index or the columnstore, so turning
    only the columnstore off exercises the index-only fallback — a different physical
    path from ``INDEX OFF`` (no index, columnstore still on)."""

    def _layout(self, body, out_cols, context, exposed_name):
        index_columns = indexable_columns(body)
        if not index_columns:
            return None
        return CrateCreateColumnIndex.build(
            context.namer,
            body,
            out_cols=out_cols,
            mode=CrateIndexMode.COLUMNSTORE_OFF,
            index_columns=index_columns,
            exposed_name=exposed_name,
        )


class CrateDbNamedFulltextIndexBuilder(_CrateDbNativeBase):
    """Table-level ``INDEX … USING FULLTEXT`` over text columns."""

    def _layout(self, body, out_cols, context, exposed_name):
        index_columns = fulltext_columns(body)
        if not index_columns:
            return None
        return CrateCreateColumnIndex.build(
            context.namer,
            body,
            out_cols=out_cols,
            mode=CrateIndexMode.NAMED_FULLTEXT,
            index_columns=index_columns,
            exposed_name=exposed_name,
        )


class CrateDbPartitionedBuilder(_CrateDbNativeBase):
    """``PARTITIONED BY`` a generated ``<int_col> % 4`` bucket column."""

    def _layout(self, body, out_cols, context, exposed_name):
        bucketable = bucketable_columns(body)
        if not bucketable:
            return None
        return CrateCreatePartitioned.build(
            context.namer,
            body,
            out_cols=out_cols,
            bucket_source=bucketable[0],
            exposed_name=exposed_name,
        )


class CrateDbObjectRoundTripBuilder(_CrateDbNativeBase):
    """Pack columns into ``OBJECT(STRICT)``, expose unpacked per column."""

    def _layout(self, body, out_cols, context, exposed_name):
        return CrateCreateObjectPack.build(
            context.namer,
            body,
            out_cols=out_cols,
            exposed_name=exposed_name,
        )


class CrateDbShardCountBuilder(_CrateDbNativeBase):
    """Re-cluster the same rows into several shards. Everything else here is single-shard, so this
    is the only builder that exercises CrateDB's distributed cross-shard merge/sort/aggregate."""

    def _layout(self, body, out_cols, context, exposed_name):
        return CrateCreateShardLayout.build(
            context.namer,
            body,
            out_cols=out_cols,
            shards=4,
            exposed_name=exposed_name,
        )


class CrateDbClusteredByBuilder(_CrateDbNativeBase):
    """Multi-shard, routed by an explicit ``CLUSTERED BY`` column — a different shard distribution
    than hash-on-``_id``, exercising routing-dependent execution paths."""

    def _layout(self, body, out_cols, context, exposed_name):
        routable = bucketable_columns(body) or out_cols
        if not routable:
            return None
        return CrateCreateShardLayout.build(
            context.namer,
            body,
            out_cols=out_cols,
            shards=4,
            routing_column=routable[0],
            exposed_name=exposed_name,
        )
