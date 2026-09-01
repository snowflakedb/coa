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

"""Normalize Postgres EXPLAIN JSON into a structural fingerprint (QPG-style).

Costs, row estimates, relation/index names, and predicate text are stripped so two plans
that differ only in object names, costing, or filter literals collapse to one fingerprint.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

#: Keys that describe *how* the plan works (kept).
_KEEP_KEYS = frozenset(
    {
        "Node Type",
        "Join Type",
        "Scan Direction",
        "Strategy",
        "Parallel Aware",
        "Async Capable",
        "Partial Mode",
        "Operation",
        "Command",
        "Inner Unique",
        "Plans",
        "Plan",
    }
)


def normalize_plan_node(node: Any) -> Any:
    """Keep operator structure only; drop costs, names, and expression text."""
    if isinstance(node, list):
        return [normalize_plan_node(item) for item in node]
    if not isinstance(node, dict):
        return node

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key not in _KEEP_KEYS:
            continue
        if key in ("Relation Name", "Schema", "Alias", "Index Name", "CTE Name"):
            continue
        out[key] = normalize_plan_node(value)
    return out


def extract_plan_tree(explain_json: Any) -> Any:
    """Unwrap ``EXPLAIN (FORMAT JSON)`` top-level list into the ``Plan`` tree."""
    root = explain_json
    if isinstance(root, list) and root:
        root = root[0]
    if isinstance(root, dict) and "Plan" in root:
        return root["Plan"]
    return root


def fingerprint_plan(explain_json: Any) -> str:
    """Stable hex digest of the normalized plan tree."""
    tree = normalize_plan_node(extract_plan_tree(explain_json))
    payload = json.dumps(tree, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def fingerprint_explain_text(text: str) -> Optional[str]:
    """Parse EXPLAIN JSON text and fingerprint it. Returns ``None`` on parse failure."""
    try:
        return fingerprint_plan(json.loads(text))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
