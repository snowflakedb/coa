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

"""Union set of distinct plan fingerprints + CSV curve for coverage campaigns."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable, Optional

from eqgen.fuzz.compare import QueryComparison
from eqgen.fuzz.round import RoundOutcome

_CSV_FIELDS = ("label", "round", "queries", "elapsed_seconds", "distinct_plans")


class PlanTracker:
    """Accumulate a union of plan fingerprints from both sides of each comparison."""

    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        self._seen: set[str] = set()
        self._csv_path = out_dir / "plans.csv"
        self._header_written = self._csv_path.exists() and self._csv_path.stat().st_size > 0

    @property
    def distinct_plans(self) -> int:
        return len(self._seen)

    def observe_comparison(self, comparison: QueryComparison) -> None:
        for fingerprint in (comparison.base_plan, comparison.equivalent_plan):
            if fingerprint:
                self._seen.add(fingerprint)

    def observe(self, outcome: RoundOutcome) -> None:
        for comparison in outcome.results:
            self.observe_comparison(comparison)

    def observe_many(self, comparisons: Iterable[QueryComparison]) -> None:
        for comparison in comparisons:
            self.observe_comparison(comparison)

    def write_row(
        self,
        label: str,
        *,
        round_number: Optional[int] = None,
        queries: int,
        elapsed_seconds: float,
    ) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        with open(self._csv_path, "a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(_CSV_FIELDS))
            if not self._header_written:
                writer.writeheader()
                self._header_written = True
            writer.writerow(
                {
                    "label": label,
                    "round": "" if round_number is None else round_number,
                    "queries": queries,
                    "elapsed_seconds": round(elapsed_seconds, 3),
                    "distinct_plans": self.distinct_plans,
                }
            )
