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

"""Every builder, re-exported, so ``from eqgen.equivalence.builders import X`` works whichever
module ``X`` sits in and a builder can move without the factory noticing.

What each module holds:

* :mod:`.base` — the base class and shared helpers.
* :mod:`.sources` — the base table, where a chain of rewrites stops.
* :mod:`.creates` — ``CREATE VIEW`` and ``CREATE TABLE``.
* :mod:`.selects` — ``SELECT * FROM x WHERE ...``, and the only place a filter becomes a ``WHERE``.
* :mod:`.partitioning` — split the rows, union them back; set-algebra round trips.
* :mod:`.eet` — wrap each column in an always-true ``CASE``.
* :mod:`.scalars` — per-column scalar identity expressions.
* :mod:`.dml_tables` — create a table, then delete/reinsert or no-op UPDATE.
* :mod:`.joins` — split or copy the rows, then join them back on a key.
* :mod:`.expansion` — write every row out several times, then collapse the copies again.
* :mod:`.window` — replace each column with a window function that returns it unchanged.
"""

from eqgen.equivalence.builders.base import ColumnRewriteQueryBuilder, EquivalenceBuilder
from eqgen.equivalence.builders.creates import (
    CreateFromQueryBuilder,
    CreateMaterializedViewBuilder,
    CreateTableBuilder,
    CreateTemporaryTableBuilder,
    CreateTemporaryViewBuilder,
    CreateViewBuilder,
)
from eqgen.equivalence.builders.dml_tables import DeleteReinsertTableBuilder, NoopUpdateTableBuilder
from eqgen.equivalence.builders.eet import EetCaseColumnQueryBuilder, EetDeterminedFilterQueryBuilder
from eqgen.equivalence.builders.expansion import (
    KeyDistinctReduceBuilder,
    KeyExplodeExpansionBuilder,
    KeyGroupAggregateReduceBuilder,
    KeyInsertExtrasExpansionBuilder,
    KeyQualifyDedupReduceBuilder,
    KeyWindowAggregateReduceBuilder,
    TagExplodeExpansionBuilder,
    TagInsertExtrasExpansionBuilder,
    TagPruneDeleteReduceBuilder,
    TagPruneFilterReduceBuilder,
)
from eqgen.equivalence.builders.joins import (
    CrossJoinFilterAsInnerBuilder,
    FlagTableJoinQueryBuilder,
    LeftJoinEmptyQueryBuilder,
    SemiJoinFlagRoundTripBuilder,
    SequenceOuterJoinQueryBuilder,
)
from eqgen.equivalence.builders.partitioning import (
    DistinctUnionDuplicateQueryBuilder,
    ExceptEmptyRoundTripBuilder,
    IntersectSelfRoundTripBuilder,
    PartitionUnionQueryBuilder,
    RankModUnionQueryBuilder,
    TlpPartitionUnionQueryBuilder,
    UnionEmptyRoundTripBuilder,
)
from eqgen.equivalence.builders.roundtrips import (
    Base64CodecRoundTripBuilder,
    CodecRoundTripQueryBuilder,
    HexCodecRoundTripBuilder,
    JsonPackCodecRoundTripBuilder,
    MixedCodecRoundTripBuilder,
)
from eqgen.equivalence.builders.selects import (
    AddDropColumnQueryBuilder,
    CteQueryBuilder,
    DistinctAsGroupByQueryBuilder,
    ExplicitProjectionQueryBuilder,
    LateralReprojectQueryBuilder,
    MaterializedCteQueryBuilder,
    NotMaterializedCteQueryBuilder,
    OrderedScanQueryBuilder,
    ProjectionQueryBuilder,
    QualifyQueryBuilder,
    SelectStarQueryBuilder,
)
from eqgen.equivalence.builders.sources import BaseTableSourceBuilder
from eqgen.equivalence.builders.window import WindowRewriteQueryBuilder

__all__ = [
    "Base64CodecRoundTripBuilder",
    "CodecRoundTripQueryBuilder",
    "HexCodecRoundTripBuilder",
    "JsonPackCodecRoundTripBuilder",
    "MixedCodecRoundTripBuilder",
    "AddDropColumnQueryBuilder",
    "BaseTableSourceBuilder",
    "ColumnRewriteQueryBuilder",
    "CreateFromQueryBuilder",
    "CreateMaterializedViewBuilder",
    "CreateTableBuilder",
    "CreateTemporaryTableBuilder",
    "CreateTemporaryViewBuilder",
    "CreateViewBuilder",
    "CrossJoinFilterAsInnerBuilder",
    "CteQueryBuilder",
    "DeleteReinsertTableBuilder",
    "DistinctAsGroupByQueryBuilder",
    "DistinctUnionDuplicateQueryBuilder",
    "EetCaseColumnQueryBuilder",
    "EetDeterminedFilterQueryBuilder",
    "EquivalenceBuilder",
    "ExceptEmptyRoundTripBuilder",
    "ExplicitProjectionQueryBuilder",
    "FlagTableJoinQueryBuilder",
    "IntersectSelfRoundTripBuilder",
    "KeyDistinctReduceBuilder",
    "KeyExplodeExpansionBuilder",
    "KeyGroupAggregateReduceBuilder",
    "KeyInsertExtrasExpansionBuilder",
    "KeyQualifyDedupReduceBuilder",
    "KeyWindowAggregateReduceBuilder",
    "LateralReprojectQueryBuilder",
    "LeftJoinEmptyQueryBuilder",
    "MaterializedCteQueryBuilder",
    "NotMaterializedCteQueryBuilder",
    "NoopUpdateTableBuilder",
    "OrderedScanQueryBuilder",
    "PartitionUnionQueryBuilder",
    "ProjectionQueryBuilder",
    "QualifyQueryBuilder",
    "RankModUnionQueryBuilder",
    "SelectStarQueryBuilder",
    "SemiJoinFlagRoundTripBuilder",
    "SequenceOuterJoinQueryBuilder",
    "TagExplodeExpansionBuilder",
    "TagInsertExtrasExpansionBuilder",
    "TagPruneDeleteReduceBuilder",
    "TagPruneFilterReduceBuilder",
    "TlpPartitionUnionQueryBuilder",
    "UnionEmptyRoundTripBuilder",
    "WindowRewriteQueryBuilder",
]
