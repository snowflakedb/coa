<!--
Copyright 2026 Snowflake Inc.
SPDX-License-Identifier: Apache-2.0

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
-->

# MariaDB: a second window `OVER (PARTITION BY <constant> ORDER BY …)` drops the first window's ORDER BY sort → wrong aggregate

## Summary

When a SELECT has two window functions and the second one uses `OVER (PARTITION BY <constant> ORDER
BY …)`, MariaDB's window-function computation coalesces both windows into a **single filesort keyed
only by the second window's (constant) ORDER BY** and drops the first window's own `ORDER BY`. The
first window's running aggregate is then evaluated over the wrong row order, producing wrong,
run-order-dependent values (e.g. `SUM(c) OVER (ORDER BY k)` stops being peer-equal over `k`). The
first window alone is correct; MySQL keeps the two sorts separate and returns the correct,
order-independent result. `EXPLAIN FORMAT=JSON` shows the first window's sort key is simply absent.

## Environment

- **Engine**: MariaDB 12.3.3-MariaDB, commit `2883bccc`, release build, assertions off
  (`mariadb-release/bin`).
- **Session (finding)**: `sql_mode = ONLY_FULL_GROUP_BY,NO_BACKSLASH_ESCAPES,STRICT_ALL_TABLES,
  NO_ZERO_IN_DATE,NO_ZERO_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION`; `utf8mb4` /
  `utf8mb4_nopad_bin`. The bug is independent of these — the minimal repro is integer-only.
- **Contrast**: MySQL 9.7.2 (`mysql-9.7`) — correct on every case below.

## Minimal repro

See [`reduced.sql`](./reduced.sql). Single integer column, no DISTINCT, no subquery:

```sql
CREATE TABLE t (id BIGINT);
INSERT INTO t VALUES (-3),(-1),(0),(1),(2),(2),(NULL),(7);

SELECT id,
       SUM(41) OVER (ORDER BY id < 1 DESC)               AS a,   -- window A
       MAX(id) OVER (PARTITION BY '3' ORDER BY '3' DESC)  AS b    -- window B (partition by constant)
FROM t;
```

`id < 1` has ties: 3 rows true (`-3,-1,0`), 4 rows false (`1,2,2,7`), 1 NULL. Under the default RANGE
frame, `SUM(41) OVER (ORDER BY id<1 DESC)` must give every peer the same value: `123` (=41×3) to the
true group, `287` (=41×7) to the false group, `328` (=41×8) to the NULL row.

## Expected vs actual

| Query | column `a` — MySQL 9.7 (expected) | column `a` — MariaDB (actual) |
|---|---|---|
| window A **+** window B (repro) | `{123×3, 287×4, 328}` | **garbage, run-dependent** — e.g. `{41,82,123, 246×3, 287, 328}` |
| window A **alone** (control 2) | `{123×3, 287×4, 328}` | `{123×3, 287×4, 328}` ✓ |
| window B `PARTITION BY id` (real col, control 3) | `{123×3, 287×4, 328}` | `{123×3, 287×4, 328}` ✓ |
| window B no `ORDER BY` (control 4) | `{123×3, 287×4, 328}` | `{123×3, 287×4, 328}` ✓ |

The MariaDB "actual" for the repro changes between runs/processes (frame sizes 1..8 appear), because
window A is computed over an arbitrary order — that run-to-run variance is itself a symptom of the
dropped sort.

## Equivalence construction

### Concrete, as the builder emits it

The eqgen equivalent `t` is the MariaDB **`eq_my` rebuild chain** — a row- and type-preserving
round-trip that materialises the base through a CTE, a delete/reinsert split on `MOD(id,2)`, a
`ROW_NUMBER()` surrogate-key table with a unique index, a view, and a final CTAS:

```sql
CREATE TABLE t__base_table_1 AS WITH eq_my_cte_1 AS (SELECT id,name,created_at FROM t__base)
                                SELECT id,name,created_at FROM eq_my_cte_1;
DELETE FROM t__base_table_1 WHERE MOD(id,2)=1;
INSERT INTO t__base_table_1 SELECT id,name,created_at FROM t__base WHERE MOD(id,2)=1;
CREATE TABLE t__base_table_2 AS WITH eq_my_cte_2 AS (SELECT … FROM t__base_table_1) SELECT … ;
CREATE TABLE t__base_table_3 AS SELECT …, ROW_NUMBER() OVER (ORDER BY id) AS eq_my_uk_3 FROM t__base_table_2;
CREATE UNIQUE INDEX … ON t__base_table_3 (eq_my_uk_3);
CREATE VIEW  t__base_view_1 AS SELECT id,name,created_at FROM t__base_table_3;
CREATE TABLE t          AS SELECT id,name,created_at FROM t__base_view_1;
```

It holds the identical 8 rows as the base table (oracle admissibility verified) but in a **different
physical order**. That reordering is what made the divergence observable: window A is order-sensitive
*because of the bug*, so base `t` and equivalent `t` return different garbage.

### The load-bearing composition — a pure engine bug, no construct required

The bug reproduces on a **single plain table with two window functions** (repro above) — it needs
none of the equivalence chain. Necessary ingredients (each proven by a control that swaps exactly one
token):

1. **two window functions in one SELECT** — window A alone is correct (control 2);
2. **window B partitions by a constant** — `PARTITION BY '3'`; a real-column partition is correct
   (control 3);
3. **window B has its own `ORDER BY`** — removing it is correct (control 4);
4. **window A's `ORDER BY` key has ties** — peers are where the wrong frame shows up (a unique key
   would give each row its own frame and hide it).

`SELECT DISTINCT`, the subquery, the second/outer window, and the `WEEKOFYEAR`/string machinery in
the original finding all reduce away.

## Minimal oracle exposure path

- **Object composition arity:** **1**
- **GCL builder path:** `CreateTableBuilder[ROW_NUMBER-ordered CTAS]` (**inferred mapping**)
- **Confidence:** Inferred — the report records the ordered CTAS emitted by the historical `eq_my` chain, but not its exact GCL AST builder metadata.
- **Realization:** One CTAS materializes a row-identical relation in a different physical order; later view/table wrappers are not needed for exposure.
- **Workload/data requirements (excluded from arity):**
  - Two windows in one SELECT.
  - The second window partitions by a constant and has its own `ORDER BY`.
  - The first window's `ORDER BY` key has peers, so dropping its sort changes frame results.

**Exposure vs. intrinsic trigger:** The inferred one-object CTAS path only makes the failure visible by changing physical order. The intrinsic trigger is entirely in the workload's two-window sort coalescing and reproduces on one plain table; no equivalence builder or realization is intrinsically required.

## Characterization

`EXPLAIN FORMAT=JSON` names the mechanism — the first window's sort key vanishes when the second is
added:

```
window A alone       → window_functions_computation.sorts = [ "t1.`id` < 1 desc" ]
window B alone       → window_functions_computation.sorts = [ "'3', '3' desc" ]
window A + window B  → window_functions_computation.sorts = [ "'3', '3' desc" ]   ← A's sort dropped
```

MariaDB emits a single filesort for both windows, keyed by window B's constant `ORDER BY`, so
`SUM(41) OVER (ORDER BY id<1 DESC)` is computed over window B's ordering instead of its own. When
window B partitions by a *constant*, its ORDER BY collapses to a constant sort key, and the planner
wrongly treats one sort as sufficient for both windows. A real-column partition, or no ORDER BY on
window B, avoids the faulty coalescing. Not a crash; assertions-off build, but no assertion is
involved — it is a wrong-result / plan bug.

## How it was found

The eqgen **data-equivalence oracle** ran the generated two-window query against the base table and
the row-identical `eq_my` rebuild. Because the bug makes window A order-sensitive, the two physical
orders produced different results and the oracle flagged a mismatch. That is the oracle working as
intended: it manufactured a second relation that *must* return the same multiset, so an
order-dependent wrong result surfaced as a contradiction. Reducing against MySQL then showed the
value is outright wrong (not merely reordered), and `EXPLAIN` pinned the dropped sort. A pure
query-rewrite oracle (TLP/NoREC) over a fixed table might never have produced two windows with a
constant partition next to a tie-keyed window; the data-equivalence rewrite is what put the query in
a position where the order-dependence became a visible divergence.

- **Seed**: 853578499 (round0).
- Reduced repro: [`reduced.sql`](./reduced.sql).
- Original finding: hunt log.
- **Distinct from** the run1 root cause [[mariadb-window-subquery-over-view-wrong-result]] (window
  fn + materialized-semijoin subquery over a view → *missing rows*): this needs no subquery/view and
  produces *wrong aggregate values* via a dropped window sort. Different mechanism.

Eqgen `mariadb_20260816-061046` hits the same order-dependent window values when the equivalent
reorders rows: `mismatch_round122_1.sql` (`MAX(1) OVER (ORDER BY '2016-04-10')` plus other windows;
identity view already diverges) and `mismatch_round134_1.sql` (`MAX … OVER (ORDER BY '-12345678', …)`;
identity matches, reversing `INSERT`s changes values). Not re-reduced; the distilled two-window
constant-partition case above is the root.
