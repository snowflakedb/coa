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

"""Try one builder at a time, to see whether *that* rewrite works on this engine.

An ad-hoc utility, run by hand via ``--sweep`` when a builder is added or a dialect's weights change.
Nothing else calls it: a full run asks "is the engine correct", this asks "is this builder correct",
and only the first belongs in the loop.

It runs one builder plus the few needed to produce anything at all, so a failure has one candidate
rather than six::

    DuckDBAntiJoinEmptyRoundTripBuilder   ok                 built, ran, same rows
    EetCaseColumnQueryBuilder             NOT EQUIVALENT     the builder is wrong; a full run
                                                             would have blamed the engine
    DeleteReinsertTableBuilder            failed             would not build or run at all
    SomeUnconfiguredBuilder               not_exercised      never contributed a node

``not_exercised`` is a normal result, not a failure — a builder can decline every draw. Only
``NOT EQUIVALENT`` blocks enabling one.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Optional, Sequence

from eqgen.core.catalog import Table
from eqgen.equivalence.generator import EquivalenceGenerator
from eqgen.fuzz.adapter import DialectAdapter
from eqgen.fuzz.compare import compare_objects
from eqgen.fuzz.database import Database, Row, column_names, hidden_base_name
from eqgen.plugins import PredicateSource

#: The minimum builders needed for anything to be generated at all: a materialization, a defining
#: query, and the source leaf. Weighted low so the builder under test dominates the draw.
SCAFFOLD = ("CreateViewBuilder", "SelectStarQueryBuilder", "BaseTableSourceBuilder")

#: Node budget for a swept tree, replacing the dialect's own (``max_nodes = 0``, unlimited). At 20:1
#: the builder under test wins ~87% of draws, so a *branching* one compounds: with predicates
#: available ``TlpPartitionUnionQueryBuilder`` reached ~23,000 statements per seed. ``max_nodes``
#: rather than ``max_depth`` because it triggers the config's leaf-only wind-down, so the tree stays
#: valid and the builder still appears.
_SWEEP_MAX_NODES = 40


@dataclass(frozen=True)
class SweepResult:
    """What one builder did across the swept seeds."""

    builder: str
    ok: int = 0
    not_equivalent: int = 0
    failed: int = 0
    not_exercised: int = 0
    first_failure: Optional[str] = None
    first_divergence: Optional[str] = None

    @property
    def verdict(self) -> str:
        if self.not_equivalent:
            return "NOT EQUIVALENT"
        if self.ok:
            return "ok"
        if self.failed:
            return "failed"
        return "not_exercised"


def sweep_builder(
    adapter: DialectAdapter,
    table: Table,
    rows: Sequence[Row],
    builder: str,
    *,
    seeds: int = 20,
    predicate_source: Optional[PredicateSource] = None,
) -> SweepResult:
    """Generate with *builder* plus scaffolding only, and check row equivalence each time.

    *predicate_source* is passed straight to the generator. Without one, every builder that embeds a
    generated predicate declines on every seed and reports ``not_exercised``.
    """
    config = adapter.equivalence_config()
    weights = dict.fromkeys(config.builder_weights, 0.0)
    for name in SCAFFOLD:
        if name in weights:
            weights[name] = 1.0
    if builder not in weights:
        return SweepResult(builder=builder, not_exercised=seeds)
    weights[builder] = 20.0
    # Root weights are cleared so the root draws from the same restricted set; leaving them would let
    # a root builder in that the sweep did not ask for.
    swept = dataclasses.replace(
        config,
        builder_weights=weights,
        root_builder_weights={},
        max_nodes=_SWEEP_MAX_NODES,
    )

    generator = EquivalenceGenerator(
        swept,
        predicate_source=predicate_source,
        emitter=adapter.emitter(),
        extra_builders=adapter.extra_builders(),
    )
    hidden = Table(hidden_base_name(table), table.get_column_list())
    columns = column_names(table)

    result = SweepResult(builder=builder)
    for seed in range(seeds):
        try:
            equivalence = generator.generate(hidden, seed=seed, exposed_name=table.get_sql_name())
            statements = [statement.statement_text for statement in equivalence.setup_statements]
        except Exception as exc:
            result = dataclasses.replace(
                result, failed=result.failed + 1, first_failure=result.first_failure or f"{type(exc).__name__}: {exc}"
            )
            continue
        # The factory's own record, not an inference from the output. A builder that was offered the
        # slot and declined it is absent here — the case that must not read as "ok".
        if not generator.builders_used.get(builder):
            result = dataclasses.replace(result, not_exercised=result.not_exercised + 1)
            continue

        base = equivalent = None
        try:
            base = Database.build_base(adapter, table, rows)
            equivalent = Database.build_equivalent(adapter, table, rows, statements=statements)
            comparison = compare_objects(base, equivalent, table, columns)
            if comparison.equal:
                result = dataclasses.replace(result, ok=result.ok + 1)
            else:
                detail = f"rows: missing={comparison.rows.only_in_base} extra={comparison.rows.only_in_other}"
                if not comparison.types_agree:
                    detail = f"types: {comparison.base_types} vs {comparison.equivalent_types}"
                result = dataclasses.replace(
                    result,
                    not_equivalent=result.not_equivalent + 1,
                    first_divergence=result.first_divergence or detail,
                )
        except Exception as exc:
            result = dataclasses.replace(
                result, failed=result.failed + 1, first_failure=result.first_failure or f"{type(exc).__name__}: {exc}"
            )
        finally:
            for database in (base, equivalent):
                if database is not None:
                    database.close()
    return result


def sweep_all(
    adapter: DialectAdapter,
    table: Table,
    rows: Sequence[Row],
    *,
    seeds: int = 20,
    predicate_source: Optional[PredicateSource] = None,
) -> list[SweepResult]:
    """Sweep every builder the dialect's configuration enables."""
    config = adapter.equivalence_config()
    enabled = sorted(name for name, weight in config.builder_weights.items() if weight > 0)
    return [
        sweep_builder(adapter, table, rows, name, seeds=seeds, predicate_source=predicate_source)
        for name in enabled
    ]
