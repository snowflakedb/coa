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

"""WeightedBuilderFactory -- extends BuilderFactory with weight-based selection.

Overrides ``_dispatch`` to use weighted-random ordering while preserving the
constraint-based and depth-based builder filtering from the base
``BuilderFactory``.

Subclasses must implement ``_build_weight_cache`` to supply a
``{builder_name: weight}`` mapping from their own configuration source.
"""

from __future__ import annotations

import abc
from typing import Generic, Optional, Sequence, Type

from eqgen.builder.builder import BuilderFactory, NodeBuilder
from eqgen.builder.builder_settings import BuilderSettings
from eqgen.builder.constraint_set import ConstraintSet
from eqgen.builder.type_variables import ContextTypeT, NodeTypeT
from eqgen.config.weighted import weighted_shuffle


class WeightedBuilderFactory(BuilderFactory[ContextTypeT, NodeTypeT], Generic[ContextTypeT, NodeTypeT]):
    """A ``BuilderFactory`` that selects builders via weighted-random draw.

    Each builder's class name (``type(builder).__name__``) is matched against
    the weight mapping returned by ``_build_weight_cache`` to determine its
    selection weight.  Builders whose class name has no entry default to
    weight 1 (matching the GCL ``Weighted`` default), so only builders
    explicitly configured with weight 0 are excluded.

    Constraint filtering and depth-based leaf filtering are handled by the
    base ``BuilderFactory._filter_builders`` method.  This subclass only
    replaces the ordering step with a weighted shuffle.

    Weight lookups are cached per-factory instance: ``_build_weight_cache``
    is called once on the first ``_weighted_shuffle`` call and the resulting
    ``{builder_name: weight}`` dict is reused for every subsequent call.

    Subclasses may also override ``_build_root_weight_cache`` to supply a
    *separate* weight map applied only at the root (``context.depth == 0``) --
    e.g. to constrain the top-level object independently of the interior.  The
    default returns ``None`` (no root distinction).  When the root map is
    ``None`` or empty, the root falls back to the ordinary weight cache, so
    interior and root behave identically (the pre-existing behavior).
    """

    _weight_cache: dict[str, float] | None
    _root_weight_cache: dict[str, float] | None
    _root_weight_cache_built: bool

    def __init__(
        self,
        builders: Sequence[NodeBuilder[ContextTypeT, NodeTypeT, NodeTypeT]],
        settings: BuilderSettings,
    ) -> None:
        super().__init__(builders, settings)
        self._weight_cache = None
        self._root_weight_cache = None
        self._root_weight_cache_built = False

    def _dispatch(
        self,
        type: Type[NodeTypeT],
        constraint_set: ConstraintSet[NodeTypeT],
        context: ContextTypeT,
    ) -> Optional[NodeTypeT]:
        eligible = self._filter_builders(type, constraint_set, context)
        if eligible is None:
            return None

        ordered = self._weighted_shuffle(list(eligible), context)

        for builder in ordered:
            with context._enter_builder(builder) as commit:
                result = self._run_builder(builder, constraint_set, context)
                if result is not None:
                    commit()
                    # `builder.__class__`, not `type(builder)`: this method's first parameter is named
                    # `type`, shadowing the builtin. This subclass overrides `_dispatch`, so recording it
                    # in the base class alone records nothing -- which is how the first attempt at this
                    # produced an empty counter.
                    self.chosen[builder.__class__.__name__] += 1
                    return result

        context.add_debug_info(
            self.__class__.__name__,
            [str(c) for c in constraint_set.all_constraints()],
            False,
            "No builders were able to build the constraints",
        )
        return None

    @abc.abstractmethod
    def _build_weight_cache(self, context: ContextTypeT) -> dict[str, float]:
        """Return a ``{builder_class_name: weight}`` mapping.

        Called once and cached.  Subclasses should extract weights from
        whatever configuration source their context provides.
        """
        ...

    def _build_root_weight_cache(self, context: ContextTypeT) -> dict[str, float] | None:
        """Return an optional ``{builder_class_name: weight}`` map for the root.

        Applied only at ``context.depth == 0`` (the top-level object).  Return
        ``None`` (the default) for no root distinction; an empty dict is treated
        the same as ``None`` (falls back to the ordinary weight cache).  Called
        once and cached.
        """
        return None

    def _active_weight_cache(self, context: ContextTypeT) -> dict[str, float]:
        """The weight cache in effect for *context*: the root map at depth 0
        (when non-empty), else the ordinary cache."""
        if context.depth == 0:
            if not self._root_weight_cache_built:
                self._root_weight_cache = self._build_root_weight_cache(context)
                self._root_weight_cache_built = True
            if self._root_weight_cache:
                return self._root_weight_cache
        if self._weight_cache is None:
            self._weight_cache = self._build_weight_cache(context)
        return self._weight_cache

    def _get_builder_weight(
        self,
        builder: NodeBuilder[ContextTypeT, NodeTypeT, NodeTypeT],
        context: ContextTypeT,
    ) -> float:
        """Look up the cached weight for *builder*."""
        return self._active_weight_cache(context).get(type(builder).__name__, 1.0)

    def _weighted_shuffle(
        self,
        builders: list[NodeBuilder[ContextTypeT, NodeTypeT, NodeTypeT]],
        context: ContextTypeT,
    ) -> list[NodeBuilder[ContextTypeT, NodeTypeT, NodeTypeT]]:
        """Order builders by weighted-random draw."""
        cache = self._active_weight_cache(context)
        return weighted_shuffle(builders, lambda b: cache.get(type(b).__name__, 1.0))
