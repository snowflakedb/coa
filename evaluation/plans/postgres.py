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

"""Postgres EXPLAIN → plan fingerprint. Used only from evaluation (via callback into the worker)."""

from __future__ import annotations

from typing import Optional

from evaluation.plans.normalize import fingerprint_plan
from eqgen.fuzz.database import Database


def fingerprint_query(database: Database, sql: str) -> Optional[str]:
    """``EXPLAIN (FORMAT JSON, COSTS false)`` on *sql* against *database*; return a fingerprint.

    Never raises: EXPLAIN failures become ``None`` so plan tracking cannot create findings.
    """
    try:
        cursor = database.connection.execute(f"EXPLAIN (FORMAT JSON, COSTS false) {sql}")
        rows = cursor.fetchall()
        if not rows:
            return None
        # psycopg returns the JSON document in the first column (already parsed or as str).
        payload = rows[0][0]
        if isinstance(payload, str):
            import json

            payload = json.loads(payload)
        return fingerprint_plan(payload)
    except Exception:  # noqa: BLE001 — any EXPLAIN failure is "no fingerprint"
        return None
