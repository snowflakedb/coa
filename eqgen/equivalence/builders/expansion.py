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

"""Grow the table with copies, then collapse them back.

A *reducer* mints a channel and asks for an expansion underneath; an *expander* requires that
channel, so it can only ever be built under a reducer; and the reducer does not support the
channel, so a second reducer cannot appear in between. Net effect is the base multiset, but the
engine had to build and filter a bigger table on the way.

Two channels:

* **tag** — expanders mark one copy per base row KEEP and the rest throwaway; the DELETE /
  FILTER reducers drop everything but KEEP.
* **key** — expanders give each base row's copies a shared ``ROW_NUMBER`` identity; the GROUP /
  DISTINCT / window-aggregate reducers collapse each key's copies (dup-safe: duplicate base
  rows get distinct keys).

CONNECT BY expanders are deliberately absent: that construct is not portable.
"""

from __future__ import annotations

from typing import ClassVar, Optional, Type

from eqgen.builder.constraint_set import Constraint, ConstraintSet
from eqgen.builder.type_variables import ResultT_co
from eqgen.core.types import IntegerType, TypeProperty
from eqgen.equivalence.actions import Insert
from eqgen.equivalence.ast import (
    CreateTable,
    CreateView,
    EqNode,
    EquivalentRelation,
    InsertExtrasExpansion,
    ProjectionItem,
    SelectQuery,
    TagPruneDeleteTable,
    UnionAllQuery,
)
from eqgen.equivalence.builders.base import EquivalenceBuilder
from eqgen.equivalence.constraints import (
    AcceptsDmlConstraint,
    ColumnRewriteConstraint,
    ExposedNameConstraint,
    KeyChannelConstraint,
    TagChannelConstraint,
)
from eqgen.equivalence.context import EquivalenceContext
from eqgen.equivalence.keys import KeyScope, KeySpec
from eqgen.ir import expr
from eqgen.ir.expr import WindowFunction

#: How many times each base row is written out is a GCL knob (``big_table_rowcount_weights``,
#: pinned to 2 by default — see the comment there), not a constant here: mirrors the source
#: this was ported from, which exposes the same choice so a dedicated scale-oriented campaign
#: can dial it up without a code change.
_KEEP = 1
_THROWAWAY = 0


# ---------------------------------------------------------------------------
# Shared expander / reducer routing
# ---------------------------------------------------------------------------


class _ExpansionBuilderBase(EquivalenceBuilder[ResultT_co]):
    """An expander requires and supports exactly its own channel (+ ``AcceptsDml``), so it only
    fires below a reducer of that channel and never at the top."""

    _channel_type: ClassVar[Type[Constraint[EqNode]]]

    def required_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return [self._channel_type]

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return [self._channel_type, AcceptsDmlConstraint]


class _TagReduceBuilderBase(EquivalenceBuilder[ResultT_co]):
    """Mint the tag channel, dispatch an expander, decline if nothing expandable comes back."""

    #: The channel this reducer mints. Declared so the pairing is readable from *both* sides — the
    #: expanders already carry it — which is what lets a config test find a reducer whose channel has
    #: no enabled producer. Such a reducer declines every draw, so its weight is silently dead.
    _channel_type: ClassVar[Type[Constraint[EqNode]]] = TagChannelConstraint

    _requires_dml: ClassVar[bool]

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return [ExposedNameConstraint]

    def _build_reduced(
        self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext
    ) -> Optional[tuple[EquivalentRelation, tuple[ProjectionItem, ...], str]]:
        base_items = self._passthrough_items(context)
        if not base_items:
            return None
        tag_col = context.names.generate_column_name("eq_tag")
        forwarded: list[Constraint[EqNode]] = [TagChannelConstraint(tag_col, _KEEP)]
        if self._requires_dml:
            forwarded.append(AcceptsDmlConstraint())
        child = self.builder_factory.build_subtree(
            EquivalentRelation,  # type: ignore[type-abstract]
            ConstraintSet(forwarded),
            context,
        )
        if child is None:
            return None
        return child, base_items, tag_col


class _KeyReduceBuilderBase(EquivalenceBuilder[ResultT_co]):
    """Mint the key channel, dispatch an expander, decline if nothing expandable comes back."""

    #: The channel this reducer mints. See :class:`_TagReduceBuilderBase`.
    _channel_type: ClassVar[Type[Constraint[EqNode]]] = KeyChannelConstraint

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return [ExposedNameConstraint]

    def _build_keyed(
        self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext
    ) -> Optional[tuple[EquivalentRelation, tuple[ProjectionItem, ...], str]]:
        base_items = self._passthrough_items(context)
        if not base_items:
            return None
        key_col = context.names.generate_column_name("eq_key")
        child = self.builder_factory.build_subtree(
            EquivalentRelation,  # type: ignore[type-abstract]
            ConstraintSet([KeyChannelConstraint(KeySpec(key_col, KeyScope.IDENTITY))]),
            context,
        )
        if child is None:
            return None
        return child, base_items, key_col


# ---------------------------------------------------------------------------
# Tag-channel expanders
# ---------------------------------------------------------------------------


class TagExplodeExpansionBuilder(_ExpansionBuilderBase[CreateTable]):
    """Tag-channel bottom expander: keep-tagged base rows ``UNION ALL`` throwaway-tagged copies.

    The keep rows are materialized once as a table; throwaway copies are a projection of that
    same table with the tag rewritten, so every copy agrees on the base values.
    """

    _channel_type: ClassVar[Type[Constraint[EqNode]]] = TagChannelConstraint

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[CreateTable]:
        channel = constraint_set.get_optional_constraint(TagChannelConstraint)
        base_items = self._passthrough_items(context)
        if channel is None or not base_items:
            return None
        keep_items = (*base_items, ProjectionItem(channel.tag_col, expr.int_lit(channel.keep_value), IntegerType()))
        keep = self.builder_factory.build_subtree(
            EquivalentRelation,  # type: ignore[type-abstract]
            ConstraintSet([ColumnRewriteConstraint(keep_items), AcceptsDmlConstraint()]),
            context,
        )
        if keep is None:
            return None
        throw_items = tuple(
            ProjectionItem(item.alias, expr.col(item.alias, item.data_type), item.data_type) for item in base_items
        ) + (ProjectionItem(channel.tag_col, expr.int_lit(_THROWAWAY), IntegerType()),)
        throw = CreateTable.build(context.namer, SelectQuery(keep, throw_items))
        rowcount = context.config.big_table_rowcount_weights.choose_one()
        branches = (keep, *([throw] * (rowcount - 1)))
        if len(branches) == 1:
            return CreateTable.build(context.namer, SelectQuery(keep, None))
        return CreateTable.build(context.namer, UnionAllQuery(*branches))


class TagInsertExtrasExpansionBuilder(_ExpansionBuilderBase[InsertExtrasExpansion]):
    """Tag-channel stacking expander: ``INSERT`` more throwaway-tagged copies into the child's table."""

    _channel_type: ClassVar[Type[Constraint[EqNode]]] = TagChannelConstraint

    def _build(
        self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext
    ) -> Optional[InsertExtrasExpansion]:
        channel = constraint_set.get_optional_constraint(TagChannelConstraint)
        base_items = self._passthrough_items(context)
        if channel is None or not base_items:
            return None
        child = self.builder_factory.build_subtree(
            EquivalentRelation,  # type: ignore[type-abstract]
            ConstraintSet([TagChannelConstraint(channel.tag_col, channel.keep_value), AcceptsDmlConstraint()]),
            context,
        )
        if child is None:
            return None
        throw_items = tuple(
            ProjectionItem(item.alias, expr.col(item.alias, item.data_type), item.data_type) for item in base_items
        ) + (ProjectionItem(channel.tag_col, expr.int_lit(_THROWAWAY), IntegerType()),)
        insert = Insert(
            target=child.materialized_name,
            query=SelectQuery(
                child,
                throw_items,
                expr.eq(expr.col(channel.tag_col, IntegerType()), expr.int_lit(channel.keep_value)),
            ),
        )
        return InsertExtrasExpansion.build(child, insert)


# ---------------------------------------------------------------------------
# Tag-channel reducers
# ---------------------------------------------------------------------------


class TagPruneDeleteReduceBuilder(_TagReduceBuilderBase[TagPruneDeleteTable]):
    """Reducer that prunes the expansion with an in-place ``DELETE`` (needs a mutable table)."""

    _requires_dml: ClassVar[bool] = True

    def _build(
        self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext
    ) -> Optional[TagPruneDeleteTable]:
        built = self._build_reduced(constraint_set, context)
        if built is None:
            return None
        child, base_items, tag_col = built
        return TagPruneDeleteTable.build(
            context.namer, child, base_items, tag_col, _KEEP, exposed_name=self._exposed_name(constraint_set)
        )


class TagPruneFilterReduceBuilder(_TagReduceBuilderBase[CreateView]):
    """Reducer that prunes the expansion with a read-time ``WHERE tag = KEEP`` view (no DML)."""

    _requires_dml: ClassVar[bool] = False

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[CreateView]:
        built = self._build_reduced(constraint_set, context)
        if built is None:
            return None
        child, base_items, tag_col = built
        return CreateView.build(
            context.namer,
            SelectQuery(
                child,
                base_items,
                expr.eq(expr.col(tag_col, IntegerType()), expr.int_lit(_KEEP)),
            ),
            exposed_name=self._exposed_name(constraint_set),
        )


# ---------------------------------------------------------------------------
# Key-channel expanders
# ---------------------------------------------------------------------------


class KeyExplodeExpansionBuilder(_ExpansionBuilderBase[CreateTable]):
    """Write every base row out ``big_table_rowcount_weights`` times, all copies sharing one key.

    An N-fold ``UNION ALL`` over the keyed table, rather than a function that generates rows: every
    engine has ``UNION ALL``, and the row-generating functions are spelled differently by each one.
    The keyed table is written once and read N times, so all the copies agree on the key — over a
    view, each read could number the rows differently and the collapse would merge the wrong ones.
    """

    _channel_type: ClassVar[Type[Constraint[EqNode]]] = KeyChannelConstraint

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[CreateTable]:
        channel = constraint_set.get_optional_constraint(KeyChannelConstraint)
        if channel is None:
            return None
        keyed = self._materialize_row_key(context, channel.key.column)
        if keyed is None:
            return None
        rowcount = context.config.big_table_rowcount_weights.choose_one()
        return CreateTable.build(context.namer, UnionAllQuery(*([keyed.relation] * rowcount)))


class KeyInsertExtrasExpansionBuilder(_ExpansionBuilderBase[InsertExtrasExpansion]):
    """Key-channel stacking expander: ``INSERT`` another full copy of the child's rows into itself."""

    _channel_type: ClassVar[Type[Constraint[EqNode]]] = KeyChannelConstraint

    def _build(
        self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext
    ) -> Optional[InsertExtrasExpansion]:
        channel = constraint_set.get_optional_constraint(KeyChannelConstraint)
        if channel is None:
            return None
        child = self.builder_factory.build_subtree(
            EquivalentRelation,  # type: ignore[type-abstract]
            ConstraintSet([KeyChannelConstraint(channel.key)]),
            context,
        )
        if child is None:
            return None
        insert = Insert(target=child.materialized_name, query=SelectQuery(child, None))
        return InsertExtrasExpansion.build(child, insert)


# ---------------------------------------------------------------------------
# Key-channel reducers
# ---------------------------------------------------------------------------


class KeyDistinctReduceBuilder(_KeyReduceBuilderBase[CreateView]):
    """Collapse the copies back with ``DISTINCT``, then project the key away.

    Two views rather than one query with a subquery in it: a source is referenced by name here, and
    an inline ``FROM (SELECT DISTINCT …)`` would mean composing SQL text outside the emitter.

    Declines unless every column can go in a ``DISTINCT``. It compares all of them, so a type that
    cannot be grouped would make the statement invalid — the GROUP / window reducers key on the
    integer alone and pass the value columns through.
    """

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[CreateView]:
        base_items = self._passthrough_items(context)
        if not base_items:
            return None
        if not all(item.data_type.get_properties() & TypeProperty.GROUPABLE for item in base_items):
            return None
        built = self._build_keyed(constraint_set, context)
        if built is None:
            return None
        expansion, base_items, key_name = built
        key_item = ProjectionItem(key_name, expr.col(key_name, IntegerType()), IntegerType())
        collapsed = CreateView.build(context.namer, SelectQuery(expansion, (key_item, *base_items), distinct=True))
        return CreateView.build(
            context.namer,
            SelectQuery(collapsed, base_items),
            exposed_name=self._exposed_name(constraint_set),
        )


class KeyGroupAggregateReduceBuilder(_KeyReduceBuilderBase[CreateView]):
    """Collapse copies with ``GROUP BY <key>`` and ``ANY_VALUE`` per column, then drop the key.

    ``ANY_VALUE`` is safe because every copy in a key's group holds the same base values. Unlike
    ``DISTINCT``, only the integer key has to be groupable — value columns pass through the
    aggregate untouched, and ``ANY_VALUE`` is defined for every type, so this builder needs no
    per-column property check at all. (It used to require ORDERABLE / AGGREGATABLE: the PostgreSQL
    spelling rewrote ``ANY_VALUE`` to ``MAX`` for servers older than 16, and ``MAX`` does not exist
    for jsonb. That rewrite is gone now the pinned server is 18.4.)
    """

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[CreateView]:
        base_items = self._passthrough_items(context)
        if not base_items:
            return None
        built = self._build_keyed(constraint_set, context)
        if built is None:
            return None
        expansion, base_items, key_name = built
        aggregated = tuple(
            ProjectionItem(item.alias, expr.any_value(expr.col(item.alias, item.data_type), item.data_type), item.data_type)
            for item in base_items
        )
        return CreateView.build(
            context.namer,
            SelectQuery(
                expansion,
                aggregated,
                group_by=(expr.col(key_name, IntegerType()),),
            ),
            exposed_name=self._exposed_name(constraint_set),
        )


class KeyWindowAggregateReduceBuilder(_KeyReduceBuilderBase[CreateView]):
    """Recover each column via ``MAX(col) OVER (PARTITION BY key)``, then ``DISTINCT`` to collapse.

    Mirrors the GROUP reducer but through a window aggregate — a different planner path over the
    same identity key. Declines unless every column is aggregatable (``MAX``) and groupable (the
    inner ``DISTINCT``).
    """

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[CreateView]:
        base_items = self._passthrough_items(context)
        if not base_items:
            return None
        for item in base_items:
            props = item.data_type.get_properties()
            if not (props & TypeProperty.AGGREGATABLE and props & TypeProperty.GROUPABLE):
                return None
        built = self._build_keyed(constraint_set, context)
        if built is None:
            return None
        expansion, base_items, key_name = built
        key_expr = expr.col(key_name, IntegerType())
        key_item = ProjectionItem(key_name, key_expr, IntegerType())
        windowed = tuple(
            ProjectionItem(
                item.alias,
                expr.window_over(
                    WindowFunction.MAX,
                    expr.col(item.alias, item.data_type),
                    (key_expr,),
                    item.data_type,
                ),
                item.data_type,
            )
            for item in base_items
        )
        collapsed = CreateView.build(
            context.namer, SelectQuery(expansion, (key_item, *windowed), distinct=True)
        )
        return CreateView.build(
            context.namer,
            SelectQuery(collapsed, base_items),
            exposed_name=self._exposed_name(constraint_set),
        )


class KeyQualifyDedupReduceBuilder(_KeyReduceBuilderBase[CreateView]):
    """Collapse copies with ``QUALIFY ROW_NUMBER() OVER (PARTITION BY key) = 1``, then drop the key.

    Sibling of the DISTINCT / GROUP / window key reducers — ranking dedup instead of aggregate.
    """

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[CreateView]:
        built = self._build_keyed(constraint_set, context)
        if built is None:
            return None
        expansion, base_items, key_name = built
        key_expr = expr.col(key_name, IntegerType())
        key_item = ProjectionItem(key_name, key_expr, IntegerType())
        rn = expr.window_over(
            WindowFunction.ROW_NUMBER,
            None,
            (key_expr,),
            IntegerType(),
            order_by=(key_expr,),
        )
        keep_one = expr.eq(rn, expr.int_lit(1))
        collapsed = CreateView.build(
            context.namer,
            SelectQuery(expansion, (key_item, *base_items), qualify=keep_one),
        )
        return CreateView.build(
            context.namer,
            SelectQuery(collapsed, base_items),
            exposed_name=self._exposed_name(constraint_set),
        )
