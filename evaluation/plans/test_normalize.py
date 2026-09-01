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

"""Unit tests for plan normalization and the union tracker (live inside evaluation/)."""

from __future__ import annotations

from pathlib import Path

import pytest

from evaluation.plans.normalize import fingerprint_plan, normalize_plan_node
from evaluation.plans.tracker import PlanTracker
from eqgen.fuzz.compare import QueryComparison
from eqgen.fuzz.round import RoundOutcome

pytestmark = pytest.mark.unit


def _seq_scan(relation: str) -> dict:
    return {
        "Node Type": "Seq Scan",
        "Relation Name": relation,
        "Alias": relation,
        "Startup Cost": 0.0,
        "Total Cost": 35.5,
        "Plan Rows": 10,
        "Plan Width": 8,
        "Filter": f"({relation}.c_int > 1)",
    }


def _hash_join(left: str, right: str) -> dict:
    return {
        "Node Type": "Hash Join",
        "Join Type": "Inner",
        "Startup Cost": 1.0,
        "Total Cost": 99.0,
        "Plans": [
            _seq_scan(left),
            {
                "Node Type": "Hash",
                "Plans": [_seq_scan(right)],
            },
        ],
    }


def test_same_shape_different_names_same_fingerprint() -> None:
    a = fingerprint_plan([{"Plan": _seq_scan("t")}])
    b = fingerprint_plan([{"Plan": _seq_scan("t0")}])
    assert a == b


def test_different_join_type_different_fingerprint() -> None:
    inner = fingerprint_plan([{"Plan": _hash_join("t0", "t1")}])
    other = _hash_join("t0", "t1")
    other["Join Type"] = "Left"
    left = fingerprint_plan([{"Plan": other}])
    assert inner != left


def test_costs_and_filters_do_not_affect_fingerprint() -> None:
    cheap = _seq_scan("t")
    expensive = dict(cheap)
    expensive["Total Cost"] = 99999.0
    expensive["Filter"] = "(t.c_int > 999)"
    assert fingerprint_plan([{"Plan": cheap}]) == fingerprint_plan([{"Plan": expensive}])


def test_normalize_drops_relation_names() -> None:
    normalized = normalize_plan_node(_seq_scan("t__base"))
    assert "Relation Name" not in normalized
    assert normalized["Node Type"] == "Seq Scan"


def test_plan_tracker_union_and_csv(tmp_path: Path) -> None:
    tracker = PlanTracker(tmp_path)
    outcome = RoundOutcome(
        seed=1,
        results=[
            QueryComparison("q1", equal=True, base_plan="a", equivalent_plan="b"),
            QueryComparison("q2", equal=True, base_plan="a", equivalent_plan="a"),
        ],
    )
    tracker.observe(outcome)
    assert tracker.distinct_plans == 2
    tracker.write_row("final", queries=2, elapsed_seconds=1.5)
    text = (tmp_path / "plans.csv").read_text(encoding="utf-8")
    assert "distinct_plans" in text
    assert "2" in text
