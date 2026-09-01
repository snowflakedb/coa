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

"""Compare metamorphic-coverage arms of a sweep, on one shared denominator.

    python -m evaluation.coverage.compare_arms /tmp/mc3/3 /tmp/mc3/15

Each argument is an ``--out`` directory from :mod:`evaluation.coverage.metamorphic` (the one holding
``pairs.json``, ``checkpoint.json`` and ``lines/``).

**Why this is a separate step.** ``gcov.fixed_denominator``'s rule pins a denominator *within* a run,
because ``lcov --capture`` only reports translation units that have written a ``.gcda``. Across arms
that is not enough: an arm whose objects are deeper touches strictly more units, so it finishes with a
larger denominator, and dividing each arm by its own figure makes the percentages incomparable in the
direction that flatters the shallow arm. The comparison therefore has to happen after both arms exist,
against the larger denominator, with the smaller-denominator arm's absent files counted as
zero-covered -- which is what they were.

The set relations are the point, not the percentages. ``only in B`` is the number a composition claim
actually rests on: lines that diverge under the deeper configuration and never diverge under the
shallower one, no matter how many queries the shallow arm is given.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path
from typing import Optional, Sequence


def load_lines(arm: Path, phase: str) -> set[tuple[str, int]]:
    """Union of one arm's per-suite divergent line sets for *phase* (``setup`` or ``query``)."""
    union: set[tuple[str, int]] = set()
    for path in sorted((arm / "lines").glob(f"suite*_{phase}.txt.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for row in handle:
                row = row.strip()
                if not row:
                    continue
                name, _, line = row.rpartition(":")
                union.add((name, int(line)))
    return union


def suite_rows(arm: Path) -> list[dict]:
    records = json.loads((arm / "pairs.json").read_text(encoding="utf-8"))
    return [r for r in records if r.get("phase") == "setup"]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Compare MC arms on a shared denominator.")
    parser.add_argument("arms", type=Path, nargs="+", help="--out directories to compare, in order")
    parser.add_argument(
        "--denominator",
        type=int,
        default=None,
        help="lines to divide by (default: the largest of the arms' own pinned denominators)",
    )
    args = parser.parse_args(argv)

    arms = []
    for arm in args.arms:
        checkpoint = json.loads((arm / "checkpoint.json").read_text(encoding="utf-8"))
        setups = suite_rows(arm)
        arms.append(
            {
                "name": arm.name,
                "path": arm,
                "checkpoint": checkpoint,
                "setup": load_lines(arm, "setup"),
                "query": load_lines(arm, "query"),
                "builders": [sum(r["builders"].values()) for r in setups],
                "statements": [r["statements"] for r in setups],
            }
        )

    denominator = args.denominator or max(a["checkpoint"]["instrumented"] for a in arms)
    print(f"denominator: {denominator:,} lines (shared across arms)")
    own = ", ".join(f"{a['name']}={a['checkpoint']['instrumented']:,}" for a in arms)
    print(f"each arm's own pinned figure: {own}")
    print()

    header = f"{'arm':>6} {'pairs':>6} {'suites':>6} {'builders':>16} {'stmts':>13}"
    print(
        f"{header} | {'setup union':>11} {'setup MC':>9} | {'query union':>11} {'query MC':>9} "
        f"| {'BOTH union':>11} {'both MC':>9}"
    )
    for a in arms:
        c = a["checkpoint"]
        b = a["builders"] or [0]
        st = a["statements"] or [0]
        b_txt = f"{min(b)}-{max(b)}" if min(b) != max(b) else str(min(b))
        s_txt = f"{min(st)}-{max(st)}" if min(st) != max(st) else str(min(st))
        both = a["setup"] | a["query"]
        print(
            f"{a['name']:>6} {c['pairs']:>6} {c['successful_suites']:>6} {b_txt:>16} {s_txt:>13} | "
            f"{len(a['setup']):>11,} {100 * len(a['setup']) / denominator:>8.3f}% | "
            f"{len(a['query']):>11,} {100 * len(a['query']) / denominator:>8.3f}% | "
            f"{len(both):>11,} {100 * len(both) / denominator:>8.3f}%"
        )

    # The two surfaces are far from redundant, and how far varies with the arm: deep composition is
    # planned and executed at DDL time, so it lands in setup and leaves the query reading a finished
    # heap. An arm's query figure alone can therefore *fall* while its true reach doubles -- so the
    # share of the union that only setup ever sees is reported per arm, not assumed constant.
    print()
    print("surface complementarity (why the union is the headline, not either column):")
    for a in arms:
        s, q = a["setup"], a["query"]
        both = s | q
        if not both:
            continue
        print(
            f"  {a['name']:>6}  union {len(both):>7,}  = query-only {len(q - s):>7,} "
            f"+ shared {len(q & s):>7,} + setup-only {len(s - q):>7,}   "
            f"setup-only is {100 * len(s - q) / len(both):>5.1f}% of union   "
            f"Jaccard {len(q & s) / len(both):.3f}"
        )

    if len(arms) < 2:
        return 0

    print()
    print("pairwise set relations (what the deeper arm reaches that the shallower never does):")
    first = arms[0]
    for other in arms[1:]:
        for phase in ("setup", "query", "both"):
            if phase == "both":
                a, b = first["setup"] | first["query"], other["setup"] | other["query"]
            else:
                a, b = first[phase], other[phase]
            print(
                f"  {phase:>5}  {first['name']} -> {other['name']}: "
                f"shared {len(a & b):>7,}   only in {first['name']} {len(a - b):>7,}   "
                f"only in {other['name']} {len(b - a):>7,}   "
                f"gain {100 * len(b - a) / denominator:>6.3f}%"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
