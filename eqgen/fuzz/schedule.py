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

"""Per-round query-phase time budgets, scaled by equivalence complexity.

A shallow tree gets near ``min_seconds``; a deep / multi-statement tree gets up to ``max_seconds``.
Scoring is a cheap tree walk — negligible next to DDL or a single differential query.
Builder diversity is recorded for the journal but does not drive the score.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Mapping, Optional

from eqgen.equivalence.ast import EqNode

#: Soft caps for normalizing complexity. Tuned so a *typical* generated tree lands mid-range;
#: the old (15 / 40 / 20 / 6) caps saturated almost every round at score 1.0 because real
#: trees routinely hit depth≈20+, and ``builder_kinds ≥ 6`` alone was enough to max out.
_DEPTH_CAP = 30
_NODE_CAP = 80
_STMT_CAP = 40


@dataclass(frozen=True)
class EquivalenceComplexity:
    """Shape metrics for one generated equivalence, plus a score in ``[0, 1]``."""

    depth: int
    nodes: int
    statements: int
    builder_kinds: int
    score: float

    def schedule_note(self, *, budget: float, min_seconds: float, max_seconds: float) -> str:
        """One journal / log line recording the allocated query-phase budget."""
        return (
            f"schedule: budget={budget:.1f}s "
            f"(min={min_seconds:g} max={max_seconds:g} score={self.score:.2f} "
            f"depth={self.depth} nodes={self.nodes} stmts={self.statements} "
            f"builders={self.builder_kinds})"
        )


def measure_complexity(
    root: EqNode,
    *,
    statements: int,
    builders_used: Mapping[str, int],
) -> EquivalenceComplexity:
    """Walk *root* once (DAG-safe) and return normalized complexity."""
    depth = 0
    nodes = 0
    seen: set[int] = set()
    stack = [(root, 1)]
    while stack:
        node, level = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        nodes += 1
        depth = max(depth, level)
        stack.extend((child, level + 1) for child in node.children())

    builder_kinds = len(builders_used)
    # Builder diversity is journaled but not scored: unique builder count grows quickly
    # with the catalog and was pinning nearly every round at ``max_seconds``.
    parts = (
        depth / _DEPTH_CAP,
        nodes / _NODE_CAP,
        statements / _STMT_CAP,
    )
    score = min(1.0, max(parts))
    return EquivalenceComplexity(
        depth=depth,
        nodes=nodes,
        statements=statements,
        builder_kinds=builder_kinds,
        score=score,
    )


@dataclass(frozen=True)
class RoundTimeScheduler:
    """Map :class:`EquivalenceComplexity` to a query-phase wall-clock budget."""

    max_seconds: float = 10.0
    min_seconds: float = 3.0
    #: When True, ignore complexity and always return :attr:`max_seconds`.
    flat: bool = False

    def __post_init__(self) -> None:
        if self.max_seconds <= 0:
            raise ValueError(f"max_seconds must be positive, got {self.max_seconds}")
        if self.min_seconds <= 0:
            raise ValueError(f"min_seconds must be positive, got {self.min_seconds}")
        if self.min_seconds > self.max_seconds:
            raise ValueError(f"min_seconds ({self.min_seconds}) > max_seconds ({self.max_seconds})")

    def seconds(self, complexity: Optional[EquivalenceComplexity] = None) -> float:
        if self.flat or complexity is None:
            return float(self.max_seconds)
        span = self.max_seconds - self.min_seconds
        return self.min_seconds + span * complexity.score


def default_min_seconds(max_seconds: float) -> float:
    """Floor used when ``--min-round-seconds`` is omitted: ``max(3, max // 5)``."""
    return float(max(3, max_seconds // 5))


def flat_scheduler(seconds: float) -> RoundTimeScheduler:
    """Hard query-phase cap with no complexity scaling (the default)."""
    return RoundTimeScheduler(max_seconds=seconds, min_seconds=seconds, flat=True)
