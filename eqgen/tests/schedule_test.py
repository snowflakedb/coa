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

"""Unit tests for the per-round query-phase time scheduler."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from eqgen.core.catalog import Column, Table
from eqgen.core.types import IntegerType
from eqgen.equivalence.ast import BaseTableSource, CreateView, SelectQuery
from eqgen.equivalence.context import NameGenerator, ObjectNamer
from eqgen.fuzz.cli import scheduler_from_args
from eqgen.fuzz.schedule import (
    EquivalenceComplexity,
    RoundTimeScheduler,
    default_min_seconds,
    flat_scheduler,
    measure_complexity,
)

pytestmark = pytest.mark.unit


def _shallow_root() -> CreateView:
    namer = ObjectNamer("t", NameGenerator())
    source = BaseTableSource(Table("t__base", [Column("c", IntegerType(), 1)]))
    return CreateView.build(namer, SelectQuery(source))


def test_default_min_seconds_is_a_fifth_of_max_floored_at_three() -> None:
    assert default_min_seconds(30) == 6
    assert default_min_seconds(10) == 3
    assert default_min_seconds(5) == 3


def test_score_is_monotonic_in_depth_and_nodes() -> None:
    shallow = measure_complexity(_shallow_root(), statements=1, builders_used={"CreateViewBuilder": 1})
    deeper = measure_complexity(_shallow_root(), statements=40, builders_used={"CreateViewBuilder": 1})
    assert 0.0 <= shallow.score <= 1.0
    assert shallow.score < deeper.score


def test_builder_kinds_do_not_inflate_score() -> None:
    """Many unique builders used to force score=1.0; they must not drive the budget alone."""
    few = measure_complexity(_shallow_root(), statements=1, builders_used={"CreateViewBuilder": 1})
    many = measure_complexity(
        _shallow_root(),
        statements=1,
        builders_used={f"Builder{i}": 1 for i in range(20)},
    )
    assert few.score == many.score
    assert many.builder_kinds == 20


def test_simple_tree_gets_near_min_budget() -> None:
    scheduler = RoundTimeScheduler(max_seconds=30, min_seconds=6)
    complexity = measure_complexity(_shallow_root(), statements=1, builders_used={"CreateViewBuilder": 1})
    budget = scheduler.seconds(complexity)
    assert budget == pytest.approx(6 + 24 * complexity.score)
    # CreateView over a base table is shallow — should stay near the floor, not mid-span.
    assert budget < 10
    assert complexity.score < 0.2


def test_max_score_gets_full_budget() -> None:
    scheduler = RoundTimeScheduler(max_seconds=30, min_seconds=6)
    complexity = EquivalenceComplexity(depth=30, nodes=80, statements=40, builder_kinds=6, score=1.0)
    assert scheduler.seconds(complexity) == 30


def test_flat_scheduler_ignores_complexity() -> None:
    scheduler = flat_scheduler(30)
    complexity = measure_complexity(_shallow_root(), statements=1, builders_used={})
    assert scheduler.seconds(complexity) == 30
    assert scheduler.seconds(None) == 30


def test_scheduler_from_args_defaults_to_flat() -> None:
    flat = scheduler_from_args(round_seconds=10, min_round_seconds=None, schedule=False)
    assert flat.flat
    assert flat.seconds(None) == 10
    scaled = scheduler_from_args(round_seconds=10, min_round_seconds=None, schedule=True)
    assert not scaled.flat
    assert scaled.max_seconds == 10
    assert scaled.min_seconds == 3


def test_schedule_note_names_the_allocated_budget() -> None:
    complexity = EquivalenceComplexity(depth=7, nodes=22, statements=9, builder_kinds=4, score=0.5)
    note = complexity.schedule_note(budget=18.0, min_seconds=6, max_seconds=30)
    assert note.startswith("schedule: budget=18.0s")
    assert "score=0.50" in note
    assert "depth=7" in note


def test_cli_rejects_removed_queries_flag() -> None:
    from eqgen.fuzz.cli import main

    with pytest.raises(SystemExit) as caught:
        main(["--queries", "10"])
    assert caught.value.code == 2


def test_deadline_stops_the_query_loop(tmp_path: Path) -> None:
    """A tiny budget must cut the round short even when the source would keep yielding."""
    pytest.importorskip("duckdb")
    from eqgen.dialects.duckdb.adapter import DuckDBAdapter
    from eqgen.equivalence.generator import EquivalenceGenerator
    from eqgen.fuzz.journal import QueryJournal, sample_rows
    from eqgen.fuzz.round import run_round
    from eqgen.generators.example_generator import RandomSelectSource

    adapter = DuckDBAdapter(execution_backend="wheel")
    table = adapter.simple_catalog("t")
    rows = sample_rows(table, 4, seed=1)
    generator = EquivalenceGenerator(adapter.equivalence_config(), emitter=adapter.emitter())
    scheduler = RoundTimeScheduler(max_seconds=0.01, min_seconds=0.01, flat=True)
    path = tmp_path / "round.log"
    calls = {"n": 0}

    def clock() -> float:
        calls["n"] += 1
        return 0.0 if calls["n"] == 1 else 1.0

    with QueryJournal(path) as journal:
        outcome = run_round(
            adapter,
            generator,
            table,
            rows,
            RandomSelectSource().iter_queries(table, seed=1, limit=None),
            seed=11,
            journal=journal,
            scheduler=scheduler,
            clock=clock,
        )
    assert outcome.setup_error is None
    assert outcome.round_budget_seconds == pytest.approx(0.01)
    assert outcome.stopped_for_budget
    assert outcome.results == []
    assert "schedule: budget=" in path.read_text()
