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

from __future__ import annotations

import abc
import random
from collections import Counter
from typing import TYPE_CHECKING, Generic, Optional, Sequence, Type, get_args, get_origin

from eqgen.builder.builder_settings import BuilderSettings
from eqgen.builder.constraint_set import Constraint, ConstraintSet
from eqgen.builder.interfaces import BuilderFactoryT
from eqgen.builder.type_variables import (
    ContextTypeT,
    NodeTypeT,
    ResultT_co,
)

if TYPE_CHECKING:
    from eqgen.builder.context import BuilderContext


class GenerationError(Exception):
    """
    A generation exception is raised when something goes wrong during node generation.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class GenerationDeadlineExceeded(GenerationError):
    """Raised when the generation deadline is exceeded.

    Carries the ``context`` snapshot so callers can inspect builder depth,
    node counts, and (when ``collect_debug=True``) the full builder trace.
    """

    context: BuilderContext

    def __init__(self, context: BuilderContext) -> None:
        super().__init__(
            f"Generation deadline exceeded (depth={context.depth}, nodes={context.node_count}, attempted={context.nodes_attempted})"
        )
        self.context = context


class ConstraintViolationError(GenerationError):
    """Raised when a builder produces a result that fails a constraint's
    ``meets_constraint`` check.

    Distinct from other ``GenerationError`` causes (deadline, depth limits,
    no eligible builders) so callers can measure constraint-driven failures
    separately. Callers that want any generation failure should catch the
    base ``GenerationError``.
    """


def _resolve_result_type(cls: type, sentinel: type) -> type | None:
    """Extract the concrete ``ResultT_co`` binding from a builder class.

    This is used to handle abstract base classes that get their result type
    from the concrete subclass. For example, the `EquivalenceBuilder` or
    `_SubqueryBuilderBase` classes.

    It's a bit of a hack, but it makes the builders themselves cleaner.

    *sentinel* is the base class to match against (e.g. ``NodeBuilder``).

    Handles two cases:
    * **Direct**: ``class Foo(NodeBuilder[C, N, Literal])`` -- the third
      ``get_args`` element is the result type.
    * **Via intermediate ABC**: ``class Foo(Base[ExistsExpr])`` where
      ``Base`` passes ``ResultT_co`` through -- matched by checking
      ``__parameters__`` on the origin class.
    """
    for base in getattr(cls, "__orig_bases__", ()):
        origin = get_origin(base)
        if origin is None:
            continue
        args = get_args(base)
        if not args:
            continue
        if isinstance(origin, type) and issubclass(origin, sentinel) and len(args) >= 3 and isinstance(args[2], type):
            return args[2]
        params = getattr(origin, "__parameters__", ())
        for param, arg in zip(params, args):
            if param is ResultT_co and isinstance(arg, type):
                return arg
    return None


class NodeBuilder(
    abc.ABC,
    Generic[ContextTypeT, NodeTypeT, ResultT_co],
):
    """
    A node builder creates a Node and all of its children (or inputs).
    The node builder is responsible for:
    - Representing the constraints it supports
    - Building all inputs to the given node
    - Validating that the built node satisfies the constraints given to the builder
    """

    _result_type: type
    __builder_factory: BuilderFactoryT[ContextTypeT, NodeTypeT]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        resolved = _resolve_result_type(cls, NodeBuilder)
        if resolved is not None:
            cls._result_type = resolved

    def result_type(self) -> Type[ResultT_co]:
        """Return the AST node type this builder produces.

        Auto-set by ``__init_subclass__`` from the third generic parameter.
        Override only for dynamic result types.
        """
        return self._result_type

    def __init__(self, builder_factory: BuilderFactoryT[ContextTypeT, NodeTypeT]) -> None:
        self.__builder_factory = builder_factory

    def required_constraint_types(self) -> list[Type[Constraint[NodeTypeT]]]:
        """
        This should return a list of constraint types that this builder requires to be present in the constraint set.

        By default, this is empty.
        """
        return []

    @abc.abstractmethod
    def supported_constraint_types(self) -> list[Type[Constraint[NodeTypeT]]]:
        """
        This should return a list of constraint types that this builder supports.

        We will not use the builder if there are constraints that are unsupported by the builder.
        """
        pass

    @property
    def is_leaf(self) -> bool:
        """Whether this builder is a leaf (terminal) node that does not recurse.

        Leaf builders are the only ones eligible at max depth.
        Subclasses that produce terminal nodes (e.g. column refs, literals)
        should override this to return ``True``.
        """
        return False

    @property
    def builder_factory(self) -> BuilderFactoryT[ContextTypeT, NodeTypeT]:
        return self.__builder_factory

    @abc.abstractmethod
    def _build(self, constraint_set: ConstraintSet[NodeTypeT], context: ContextTypeT) -> Optional[ResultT_co]:
        """Build the node and its children.

        Subclasses override this — it is the single method every builder must
        implement.  The factory's ``_run_builder`` helper calls this, then
        handles debug logging and constraint validation.
        """
        pass


class BuilderFactory(BuilderFactoryT[ContextTypeT, NodeTypeT]):
    _builders: Sequence[NodeBuilder[ContextTypeT, NodeTypeT, NodeTypeT]]
    _settings: BuilderSettings

    def __init__(
        self,
        builders: Sequence[NodeBuilder[ContextTypeT, NodeTypeT, NodeTypeT]],
        settings: BuilderSettings,
    ) -> None:
        self._builders = builders
        #: How many nodes each builder class actually produced, keyed by class name. A builder that was
        #: asked but declined does not appear. Reset with :meth:`reset_chosen`.
        #:
        #: This exists because a node census cannot answer "which builders ran": `SelectQuery` alone is
        #: produced by seven different builders, so ten of eqgen's twenty-one are indistinguishable from
        #: the tree shape. Recording it at the point of dispatch is the only place the answer is known.
        self.chosen: Counter[str] = Counter()
        self._settings = settings

    @property
    def settings(self) -> BuilderSettings:
        return self._settings

    def _filter_builders(
        self,
        type: Type[NodeTypeT],
        constraint_set: ConstraintSet[NodeTypeT],
        context: ContextTypeT,
    ) -> Optional[list[NodeBuilder[ContextTypeT, NodeTypeT, NodeTypeT]]]:
        """Return eligible builders for the given constraints and depth, or ``None``.

        Depth accounting: the factory dispatches at depth D, but each
        builder runs at depth D+1 (``_enter_builder`` pushes the stack).
        Therefore:
        - D >= max_depth  -> nothing can succeed (builder sees D+1 > max_depth)
        - D == max_depth-1 -> only leaf builders can succeed (non-leaf children
          would dispatch at D+1 = max_depth where nothing succeeds)
        """
        depth = context.depth
        max_depth = self.settings.max_depth

        if depth >= max_depth:
            return None

        max_nodes = self.settings.max_nodes
        node_budget_exhausted = max_nodes > 0 and context.node_count >= max_nodes
        max_attempts = self.settings.max_attempts
        attempt_budget_exhausted = max_attempts > 0 and context.nodes_attempted >= max_attempts
        leaf_only = depth >= max_depth - 1 or node_budget_exhausted or attempt_budget_exhausted
        type_constraint_set = frozenset(constraint_set.get_constraint_types())
        excluded = context.excluded_types

        # Hoist tuple() out of the loop and cache result_type() per builder:
        # tuple(excluded) was recreated on every iteration, and result_type()
        # was called twice per builder (both involve ABC __subclasscheck__).
        excluded_tuple = tuple(excluded) if excluded else ()

        supported: list[NodeBuilder[ContextTypeT, NodeTypeT, NodeTypeT]] = []
        for b in self._builders:
            rt = b.result_type()
            if excluded_tuple and issubclass(rt, excluded_tuple):
                continue
            if not type_constraint_set <= frozenset(b.supported_constraint_types()):
                continue
            if not issubclass(rt, type):
                continue
            if not frozenset(b.required_constraint_types()) <= type_constraint_set:
                continue
            if leaf_only and not b.is_leaf:
                continue
            supported.append(b)

        if not supported:
            context.add_debug_info(
                self.__class__.__name__,
                [str(c) for c in constraint_set.all_constraints()],
                False,
                "No builders supported the constraints" if not leaf_only else "At max depth with no leaf builders available",
            )
            return None

        return supported

    def _run_builder(
        self,
        builder: NodeBuilder[ContextTypeT, NodeTypeT, NodeTypeT],
        constraint_set: ConstraintSet[NodeTypeT],
        context: ContextTypeT,
    ) -> Optional[NodeTypeT]:
        """Execute a single builder with debug logging and constraint validation."""
        result = builder._build(constraint_set, context)
        if result is None:
            context.add_debug_info(builder.__class__.__name__, [str(c) for c in constraint_set.all_constraints()], False)
            return None
        assert isinstance(result, builder.result_type()), (
            f"{type(builder).__name__} declared {builder.result_type().__name__} but produced {type(result).__name__}"
        )
        context.add_debug_info(builder.__class__.__name__, [str(c) for c in constraint_set.all_constraints()], True)
        for constraint in constraint_set.all_constraints():
            if not constraint.meets_constraint(result):
                raise ConstraintViolationError(f"Result {result!r} not valid for constraints: {constraint} was not met")
        return result

    def reset_chosen(self) -> None:
        """Forget which builders were chosen. Called at the start of each generation."""
        self.chosen.clear()

    def _dispatch(
        self, type: Type[NodeTypeT], constraint_set: ConstraintSet[NodeTypeT], context: ContextTypeT
    ) -> Optional[NodeTypeT]:
        eligible = self._filter_builders(type, constraint_set, context)
        if eligible is None:
            return None
        random.shuffle(eligible)
        for builder in eligible:
            with context._enter_builder(builder) as commit:
                result = self._run_builder(builder, constraint_set, context)
                if result is not None:
                    commit()
                    # `builder.__class__`, not `type(builder)`: this method's first parameter is named
                    # `type`, which shadows the builtin.
                    self.chosen[builder.__class__.__name__] += 1
                    return result
        context.add_debug_info(
            self.__class__.__name__,
            [str(c) for c in constraint_set.all_constraints()],
            False,
            "No builders were able to build the constraints",
        )
        return None
