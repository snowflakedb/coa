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

"""Compare two metamorphic-coverage arms on the seeds that succeeded in *both*.

    python -m evaluation.coverage.matched_arms /tmp/mc5/15 /tmp/mc_forks2/run

Why this exists. :mod:`evaluation.coverage.compare_arms` compares arms on their full suite sets, which
is right when both arms discarded suites at a similar rate. It is wrong when they did not. A suite is
discarded when its build raises, and the failures are not random -- in the rich Postgres catalog they
are dominated by ``WindowRewriteQueryBuilder`` emitting ``MIN(c_uuid)``, which PostgreSQL has no
aggregate for. Every discarded attempt is re-rolled under a fresh seed, so an arm that discards more
is more strongly selected *against* the builder combination that fails, and an arm-level difference
then mixes the variable under test with that differential selection.

Both arms walk seeds ``--seed + attempt`` in the same order, so the seed identifies the *intended*
suite independently of how many earlier attempts survived. Restricting to the intersection of
succeeded seeds gives a subset where both arms drew the same trees, which is what makes the remaining
difference attributable to the variable under test.

Reports the matched-subset union for each arm and the pairwise set relations, on one shared
denominator (see ``compare_arms`` for why the denominator has to be shared).
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path
from typing import Optional, Sequence


def seed_to_suite(arm: Path) -> dict[int, int]:
    """``seed -> suite index`` for the suites this arm completed."""
    records = json.loads((arm / "pairs.json").read_text(encoding="utf-8"))
    return {r["seed"]: r["suite"] for r in records if r.get("phase") == "setup"}


def suite_lines(arm: Path, suite: int, phase: str) -> Optional[set[tuple[str, int]]]:
    """One suite's divergent ``(file, line)`` set, or None when the file is absent."""
    path = arm / "lines" / f"suite{suite:03d}_{phase}.txt.gz"
    if not path.is_file():
        return None
    out: set[tuple[str, int]] = set()
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for row in handle:
            row = row.strip()
            if row:
                name, _, line = row.rpartition(":")
                out.add((name, int(line)))
    return out


def union_over(arm: Path, seeds: Sequence[int], mapping: dict[int, int], phase: str) -> set[tuple[str, int]]:
    union: set[tuple[str, int]] = set()
    for seed in seeds:
        lines = suite_lines(arm, mapping[seed], phase)
        if lines:
            union |= lines
    return union


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Compare MC arms on seeds that succeeded in both.")
    parser.add_argument("arms", type=Path, nargs=2, help="two --out directories")
    parser.add_argument("--denominator", type=int, default=None)
    args = parser.parse_args(argv)

    left, right = args.arms
    lmap, rmap = seed_to_suite(left), seed_to_suite(right)
    shared = sorted(set(lmap) & set(rmap))
    if not shared:
        print("no seeds succeeded in both arms", file=sys.stderr)
        return 1

    denominator = args.denominator or max(
        json.loads((a / "checkpoint.json").read_text(encoding="utf-8"))["instrumented"] for a in args.arms
    )
    print(f"denominator : {denominator:,} lines (shared)")
    print(f"{left.name:>12} completed {len(lmap):>3} suites; {right.name:>12} completed {len(rmap):>3}")
    print(f"matched seeds: {len(shared)}  (succeeded in both)")
    print(f"  only in {left.name}: {sorted(set(lmap) - set(rmap))}")
    print(f"  only in {right.name}: {sorted(set(rmap) - set(lmap))}")
    print()

    header = f"{'arm':>12} {'suites':>7} | {'setup':>9} {'query':>9} {'UNION':>9} {'union MC':>9}"
    print(header)
    print("-" * len(header))
    got = {}
    for arm, mapping in ((left, lmap), (right, rmap)):
        s = union_over(arm, shared, mapping, "setup")
        q = union_over(arm, shared, mapping, "query")
        got[arm.name] = (s, q)
        both = s | q
        print(
            f"{arm.name:>12} {len(shared):>7} | {len(s):>9,} {len(q):>9,} {len(both):>9,} "
            f"{100 * len(both) / denominator:>8.3f}%"
        )

    print()
    print("pairwise, matched subset:")
    (ls, lq), (rs, rq) = got[left.name], got[right.name]
    for phase, a, b in (("setup", ls, rs), ("query", lq, rq), ("both", ls | lq, rs | rq)):
        print(
            f"  {phase:>5}  shared {len(a & b):>7,}   only {left.name} {len(a - b):>7,}   "
            f"only {right.name} {len(b - a):>7,}   net {100 * (len(b) - len(a)) / denominator:>+7.3f}%"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
