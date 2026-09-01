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

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from eqgen.builder.builder import NodeBuilder


@dataclass
class BuilderContextDebugInfo:
    generator_name: str
    constraint_names: list[str]
    successful: bool
    message: Optional[str]


class BuilderContext:
    """Base context threaded through the builder tree.

    **depth** counts builder invocations and is incremented/decremented
    automatically by the factory via ``_enter_builder``.

    SQL-semantic state (clause flags, aggregate tracking, correlated-ref
    eligibility) lives on the query-level ``Scope``, not here.

    Mutable state (counters, debug info) lives on the single context
    instance and is naturally shared across the entire build.
    """

    _depth: int
    _debug_info: list[BuilderContextDebugInfo]
    _collect_debug: bool
    _nodes_attempted: list[int]
    _node_count: list[int]
    _excluded_types: frozenset[type]
    _deadline: datetime | None

    def __init__(
        self,
        *,
        collect_debug: bool = True,
        deadline: datetime | None = None,
    ) -> None:
        self._depth = 0
        self._debug_info: list[BuilderContextDebugInfo] = []
        self._collect_debug: bool = collect_debug
        self._nodes_attempted: list[int] = [0]
        self._node_count: list[int] = [0]
        self._excluded_types: frozenset[type] = frozenset()
        self._deadline = deadline

    # ------------------------------------------------------------------
    # Builder depth (managed by the factory only)
    # ------------------------------------------------------------------

    @contextmanager
    def _enter_builder(self, builder: NodeBuilder) -> Iterator[Callable[[], None]]:  # type: ignore[type-arg]
        """Increment depth; decrement on exit. Factory-only.

        Yields a ``commit`` callback.  Call it when the build succeeds to
        increment both node counters.  If the block exits without calling
        ``commit``, ``node_count`` is rolled back to its pre-entry value
        (undoing any child-node increments from the failed subtree).
        ``nodes_attempted`` is never rolled back.
        """
        self._depth += 1
        saved = self._node_count[0]
        committed = False

        def commit() -> None:
            nonlocal committed
            committed = True
            self._node_count[0] += 1
            self._nodes_attempted[0] += 1

        try:
            yield commit
        finally:
            self._depth -= 1
            if not committed:
                self._node_count[0] = saved

    @property
    def depth(self) -> int:
        return self._depth

    # ------------------------------------------------------------------
    # Subtree exclusion
    # ------------------------------------------------------------------

    @contextmanager
    def exclude(self, *types: type) -> Iterator[None]:
        """Ban AST node types from the current subtree.

        Builders whose ``result_type()`` is a subclass of any excluded
        type are skipped by ``_filter_builders``.  Exclusions propagate
        through recursive ``build_subtree`` calls and are restored when
        the ``with`` block exits.
        """
        saved = self._excluded_types
        self._excluded_types = saved | frozenset(types)
        try:
            yield
        finally:
            self._excluded_types = saved

    @property
    def excluded_types(self) -> frozenset[type]:
        return self._excluded_types

    # ------------------------------------------------------------------
    # Debug info
    # ------------------------------------------------------------------

    @property
    def debug_info(self) -> list[BuilderContextDebugInfo]:
        return self._debug_info

    def formatted_debug_info(self) -> str:
        return "\n".join([str(debug_info) for debug_info in self.debug_info])

    def add_debug_info(self, generator_name: str, constraints: list[str], successful: bool, message: Optional[str] = None) -> None:
        if not self._collect_debug:
            return
        self._debug_info.append(BuilderContextDebugInfo(generator_name, constraints, successful, message))

    # ------------------------------------------------------------------
    # Node counters
    # ------------------------------------------------------------------

    @property
    def nodes_attempted(self) -> int:
        return self._nodes_attempted[0]

    @property
    def node_count(self) -> int:
        """Number of nodes in the surviving tree (rolled back on failure)."""
        return self._node_count[0]

    # ------------------------------------------------------------------
    # Deadline
    # ------------------------------------------------------------------

    @property
    def deadline_exceeded(self) -> bool:
        """True when the current time has passed the configured deadline."""
        return self._deadline is not None and datetime.now() > self._deadline

    def check_deadline(self) -> None:
        """Raise ``GenerationDeadlineExceeded`` if the deadline has passed."""
        if self.deadline_exceeded:
            # Deferred to break builder.py → type_variables.py → context.py cycle.
            from eqgen.builder.builder import GenerationDeadlineExceeded

            raise GenerationDeadlineExceeded(self)
