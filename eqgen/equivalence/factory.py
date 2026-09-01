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

"""Picking which builder runs, by weight.

One list in this file — no plugin discovery, no registry to scan. A weight of ``0`` turns a
builder off, which is how an engine limits itself to what it can run.

Builders are grouped by what they *return*, automatically. Asking for a ``QueryNode`` only ever
offers builders that produce one, so the builders returning a ``CREATE`` and the builders
returning a ``SELECT`` never compete despite sitting in one list::

    build_subtree(QueryNode, ...)          -> SelectStar / UnionAll / Eet ...
    build_subtree(EquivalentRelation, ...) -> CreateView / CreateTable / DeleteReinsert ...

An engine appends to this list rather than replacing it, so registering a new builder needs no
change to any existing one.
"""

from __future__ import annotations

from typing import Optional, Sequence

from eqgen.builder.builder import NodeBuilder
from eqgen.builder.builder_settings import BuilderSettings
from eqgen.builder.weighted_factory import WeightedBuilderFactory
from eqgen.equivalence.ast import EqNode
from eqgen.equivalence.builders import (
    AddDropColumnQueryBuilder,
    Base64CodecRoundTripBuilder,
    BaseTableSourceBuilder,
    CreateMaterializedViewBuilder,
    CreateTableBuilder,
    CreateTemporaryTableBuilder,
    CreateTemporaryViewBuilder,
    CreateViewBuilder,
    CrossJoinFilterAsInnerBuilder,
    CteQueryBuilder,
    DeleteReinsertTableBuilder,
    DistinctAsGroupByQueryBuilder,
    DistinctUnionDuplicateQueryBuilder,
    EetCaseColumnQueryBuilder,
    EetDeterminedFilterQueryBuilder,
    ExceptEmptyRoundTripBuilder,
    HexCodecRoundTripBuilder,
    JsonPackCodecRoundTripBuilder,
    MixedCodecRoundTripBuilder,
    ExplicitProjectionQueryBuilder,
    FlagTableJoinQueryBuilder,
    IntersectSelfRoundTripBuilder,
    KeyDistinctReduceBuilder,
    KeyExplodeExpansionBuilder,
    KeyGroupAggregateReduceBuilder,
    KeyInsertExtrasExpansionBuilder,
    KeyQualifyDedupReduceBuilder,
    KeyWindowAggregateReduceBuilder,
    LateralReprojectQueryBuilder,
    LeftJoinEmptyQueryBuilder,
    MaterializedCteQueryBuilder,
    NotMaterializedCteQueryBuilder,
    NoopUpdateTableBuilder,
    OrderedScanQueryBuilder,
    PartitionUnionQueryBuilder,
    ProjectionQueryBuilder,
    QualifyQueryBuilder,
    RankModUnionQueryBuilder,
    SelectStarQueryBuilder,
    SemiJoinFlagRoundTripBuilder,
    SequenceOuterJoinQueryBuilder,
    TagExplodeExpansionBuilder,
    TagInsertExtrasExpansionBuilder,
    TagPruneDeleteReduceBuilder,
    TagPruneFilterReduceBuilder,
    TlpPartitionUnionQueryBuilder,
    UnionEmptyRoundTripBuilder,
    WindowRewriteQueryBuilder,
)
from eqgen.equivalence.config import EquivalenceConfig, default_equivalence_config
from eqgen.equivalence.context import EquivalenceContext

#: The portable builders, in the order they are registered. Order does not affect selection
#: (the shuffle is weighted), but it groups the families for a reader.
PORTABLE_BUILDERS: tuple[type[NodeBuilder[EquivalenceContext, EqNode, EqNode]], ...] = (
    # Create builders — which object materializes the equivalence.
    CreateViewBuilder,
    CreateTableBuilder,
    CreateTemporaryViewBuilder,
    CreateTemporaryTableBuilder,
    CreateMaterializedViewBuilder,
    DeleteReinsertTableBuilder,
    NoopUpdateTableBuilder,
    # Query builders — which defining query it wraps.
    SelectStarQueryBuilder,
    ExplicitProjectionQueryBuilder,
    CteQueryBuilder,
    LateralReprojectQueryBuilder,
    MaterializedCteQueryBuilder,
    NotMaterializedCteQueryBuilder,
    AddDropColumnQueryBuilder,
    ProjectionQueryBuilder,
    QualifyQueryBuilder,
    OrderedScanQueryBuilder,
    PartitionUnionQueryBuilder,
    TlpPartitionUnionQueryBuilder,
    RankModUnionQueryBuilder,
    UnionEmptyRoundTripBuilder,
    ExceptEmptyRoundTripBuilder,
    IntersectSelfRoundTripBuilder,
    # Value-codec round trips — one builder per codec, spelled by the engine.
    HexCodecRoundTripBuilder,
    Base64CodecRoundTripBuilder,
    JsonPackCodecRoundTripBuilder,
    MixedCodecRoundTripBuilder,
    EetCaseColumnQueryBuilder,
    EetDeterminedFilterQueryBuilder,
    WindowRewriteQueryBuilder,
    FlagTableJoinQueryBuilder,
    SequenceOuterJoinQueryBuilder,
    LeftJoinEmptyQueryBuilder,
    SemiJoinFlagRoundTripBuilder,
    CrossJoinFilterAsInnerBuilder,
    DistinctAsGroupByQueryBuilder,
    DistinctUnionDuplicateQueryBuilder,
    # Expansion / reduction: reducers mint a channel; expanders require it and only fire underneath.
    KeyDistinctReduceBuilder,
    KeyGroupAggregateReduceBuilder,
    KeyWindowAggregateReduceBuilder,
    KeyQualifyDedupReduceBuilder,
    KeyExplodeExpansionBuilder,
    KeyInsertExtrasExpansionBuilder,
    TagPruneDeleteReduceBuilder,
    TagPruneFilterReduceBuilder,
    TagExplodeExpansionBuilder,
    TagInsertExtrasExpansionBuilder,
    # The leaf.
    BaseTableSourceBuilder,
)


class EquivalenceBuilderFactory(WeightedBuilderFactory[EquivalenceContext, EqNode]):
    """Weighted dispatch over the equivalence builders."""

    def __init__(
        self,
        config: Optional[EquivalenceConfig] = None,
        *,
        extra_builders: Sequence[type[NodeBuilder[EquivalenceContext, EqNode, EqNode]]] = (),
    ) -> None:
        self._config = config if config is not None else default_equivalence_config()
        builder_classes = (*PORTABLE_BUILDERS, *extra_builders)
        builders = [builder_cls(self) for builder_cls in builder_classes]
        settings = BuilderSettings()
        settings.max_depth = self._config.max_depth
        settings.max_nodes = self._config.max_nodes
        settings.max_attempts = self._config.max_attempts
        super().__init__(builders, settings)

    @property
    def config(self) -> EquivalenceConfig:
        return self._config

    @property
    def registered_builder_names(self) -> frozenset[str]:
        """Class names of every registered builder — used by the config drift guard."""
        return frozenset(type(builder).__name__ for builder in self._builders)

    def _build_weight_cache(self, context: EquivalenceContext) -> dict[str, float]:
        return dict(self._config.builder_weights)

    def _build_root_weight_cache(self, context: EquivalenceContext) -> dict[str, float] | None:
        """Separate weights for the root object.

        The root is the one the workload queries, under the base table's own name, so it must be a
        named relation rather than a bare query. Empty means "no root distinction".
        """
        return dict(self._config.root_builder_weights) or None
