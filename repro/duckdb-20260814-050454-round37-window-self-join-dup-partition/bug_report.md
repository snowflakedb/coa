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

# DuckDB: `WindowSelfJoinOptimizer` merges two `OVER` clauses whose `PARTITION BY` lists are not equal when one list has duplicate keys

## Summary

`BoundWindowExpression::PartitionsAreEquivalent` is supposed to decide whether two window
functions share a partition. It compares *vector lengths* and then checks that every key of
`this` appears in the *set* of `other`. Duplicate keys (`PARTITION BY created_at, created_at`)
keep the vector length at 2 while collapsing the set to `{created_at}`, so that list is
treated as equivalent to a different length-2 list such as `(id, created_at)`.
`WindowSelfJoinOptimizer` then rewrites **both** aggregates into one `GROUP BY` on the
*first* window's keys. The second window's result is whatever that coarser (or finer) grouping
produces — here `bit_or(id) OVER (PARTITION BY created_at)` becomes `bit_or` per `(id, created_at)`,
i.e. just `id`.

`SET disabled_optimizers='window_self_join'` restores two `Window` operators and the correct
answer. Dropping the duplicate key does the same.

## Environment

- **DuckDB v2.0.0-alpha37826 (Cyanoptera)** `a9f869b6a7` — eqgen CLI
  `duckdb`.
- Access path: CLI `:memory:`. No `sql_mode`/collation.

## Minimal repro

See [`reduced.sql`](./reduced.sql):

```sql
CREATE TABLE t(id BIGINT, name VARCHAR, created_at VARCHAR);
INSERT INTO t VALUES (NULL, 'a', ''), (2, 'Zed', '');

SELECT name, id,
       bit_and(id) OVER (PARTITION BY id, created_at) AS ba,
       bit_or(id)  OVER (PARTITION BY created_at, created_at) AS bo
FROM t;
-- row 'a':  ba=NULL, bo=NULL   WRONG (bo should be 2)
-- row 'Zed': ba=2,    bo=2
```

`bit_or(NULL, 2) = 2`. The duplicate `created_at` is load-bearing.

## Expected vs actual

| Query | Expected | Actual |
|---|---|---|
| `bit_or(id) OVER (PARTITION BY created_at, created_at)` next to `bit_and … (id, created_at)` | `2` on both rows | `NULL` on the NULL-id row |
| same, `PARTITION BY created_at` once | `2` / `2` | `2` / `2` |
| same, `disabled_optimizers='window_self_join'` | `2` / `2` | `2` / `2` |
| round 37: `GREATEST(MAX(name) OVER (PARTITION BY id, created_at, id), name)` next to `MAX(TRUE) OVER (PARTITION BY (name IS NOT NULL), id, created_at)` | `'o''brien'` on the NULL name | `NULL` |
| round 419: `bit_xor(12) OVER (PARTITION BY name, name)` next to `MAX(id) OVER (PARTITION BY name, created_at)` on two NULL names | `0` | `12` |
| rich-shuffle2 83: `REPEAT(..., CAST(MEDIAN(c_int) OVER (PARTITION BY c_chr, c_dec, c_dec) AS BIGINT))` next to `bit_and … (c_int, c_chr, c_dec)` | `REPEAT('-7', 42)` (length 84) | `''` (REPEAT count −7) |

**Which side is wrong:** the **base table**. The equivalent in all three findings included a
`QUALIFY (ROW_NUMBER() …) = 1` window in the builder chain; that leftover `LogicalWindow` makes
`WindowSelfJoinOptimizer::CanOptimize(child)` return false, so the rewrite does not fire and the
answer is correct. Identity views and ENUM round-trips do **not** mask it. Ground truth is
`window_self_join` off / no duplicate key, which also matches evaluating the second window alone.

## Equivalence construction

1. **Round 37** (`mismatch_round37_0.sql`): long chain ending in `UNION ALL` + `QUALIFY` key-dedup
   + `MAX() OVER (PARTITION BY eq_key)` view. Workload is two framed window aggregates, the
   disagreeing column `GREATEST(MAX(name) OVER (PARTITION BY id, created_at, id …), name, …)`.
   Duplicate `id` in that partition list vs `(name IS NOT NULL, id, created_at)` on the other
   window.
2. **Round 419** (`mismatch_round419_0.sql`): `ROW_NUMBER` + `UNION ALL` + `QUALIFY` dedup.
   `bit_xor(12) OVER (PARTITION BY name, name)` next to `MAX(id) OVER (PARTITION BY name, created_at)`.
3. **Round 832_1** (`mismatch_round832_1.sql`): long chain (SEMI JOIN, PIVOT, ENUM, …) ending in
   ENUM. The ENUM alone does **not** mask the bug; a QUALIFY-dedup view does. Query is
   `bit_and(id) OVER (PARTITION BY id, CAST(created_at AS VARCHAR))` plus
   `bit_or(id) OVER (PARTITION BY created_at, created_at)` after `GROUP BY`. `GROUP BY` and
   `CAST` are not required once the duplicate key is present.

One bug: `SET disabled_optimizers='window_self_join'` silences all three original findings.

## Minimal oracle exposure path

- **Object composition arity:** 3.
- **GCL builder path:** `CreateTableBuilder` [row key] → `KeyQualifyDedupReduceBuilder` [`VIEW` realization].
- **Confidence:** Exact against the report SQL and current GCL.
- **Realization:** CTAS materializes the row key; the reducer exposes a partitioned-`QUALIFY` view whose residual `LogicalWindow` blocks `WindowSelfJoinOptimizer`.
- **Workload/data requirements (excluded from arity):** at least two full-partition window aggregates with equal-length but non-equivalent partition lists, one containing a duplicate key, plus rows for which the two true partitions produce different aggregates.

**Exposure vs. intrinsic trigger:** The object path masks the faulty optimization and therefore provides the correct side of the oracle contrast; it is not the engine defect. The intrinsic trigger is the workload's duplicate-key partition-equivalence check on a plan where `WindowSelfJoinOptimizer` is allowed to run.

## Characterization

**Trigger:** two (or more) full-partition window aggregates in one `SELECT`, where
`PartitionsAreEquivalent(w_i, w_0)` is true only because `w_i`'s key *list* contains
duplicates and is a set-subset of `w_0`'s keys, with equal *vector* size. The first
window in the select list supplies the `GROUP BY` keys for every aggregate in that
`LogicalWindow`. Select-list order is load-bearing: swap the two `OVER` clauses and
the shared grouping keys follow the new first window (round 832: `bit_and` then
incorrectly returns `bit_or`'s values).

**Does NOT trigger:**
- The same pair with the duplicate key dropped (`PARTITION BY created_at` once) — sizes
  differ, two `Window` ops.
- A single window function.
- `SET disabled_optimizers='window_self_join'`.
- `statistics_propagation` / `join_order` / `filter_pushdown` off (those do not mask it).
- Identity `CREATE VIEW v AS SELECT * FROM t`.

**Mechanism** (`duckdb/src/planner/expression/bound_window_expression.cpp:100-115`):

```cpp
bool BoundWindowExpression::PartitionsAreEquivalent(const BoundWindowExpression &other) const {
    if (partitions.size() != other.partitions.size()) {
        return false;
    }
    // TODO: Should partitions be an expression_set_t?
    expression_set_t others;
    for (const auto &partition : other.partitions) {
        others.insert(*partition);
    }
    for (const auto &partition : partitions) {
        if (!others.count(*partition)) {
            return false;
        }
    }
    return true;
}
```

That is a one-directional subset check plus a length check on the *multiset*.
`{created_at, created_at}` (length 2, set size 1) is a subset of `{id, created_at}`
(length 2). They are not equal.

`WindowSelfJoinOptimizer::CanOptimize` (`window_self_join.cpp:165`) trusts that predicate
and then (`OptimizeInternal` ~259) builds one `LogicalAggregate` from **all**
`window.expressions` using `w_expr0.Partitions()` as the groups. EXPLAIN of the distilled
repro:

```
Hash Join  INNER  id IS NOT DISTINCT FROM #0, created_at IS NOT DISTINCT FROM #1
Hash Group By  Groups: #0, #1  Aggregates: bit_and(#2), bit_or(#3)
```

versus the control (one `created_at`): two stacked `Window` operators, no join.

Fix shape: compare the two *sets* (or uniquify before comparing length), not
`len(vector) + subset(set)`. The `TODO` on that function already flags the mismatch.

Related but distinct: #21592 (self-join on `ROWS` frames that are not full-partition),
#22791 / #22844 (nested self-join stale bindings, crash). This is a wrong result on
two full-partition aggregates with *different* keys that the equality helper lies about.

DML not tested (window SELECT only). CLI only.

## How it was found

eqgen corpus-shuffle hunt `duck_corpus_shuffle/duckdb_20260814-050454/`.
44 findings; 39 are `SUBSTR`/`SUBSTRING` dups of
`eqgen/repro/duckdb-20260814-015700-round29-substr-ascii-stats`. The three non-SUBSTR
mismatches (rounds 37, 419, 832_1) are this one bug: one `disabled_optimizers` setting
silences all three, and all three reduce to duplicate `PARTITION BY` keys plus a second
window. Original findings:
`mismatch_round37_0.sql`, `mismatch_round419_0.sql`, `mismatch_round832_1.sql`.

Same bug in rich-shuffle2 `duck_rich_shuffle2/duckdb_20260815-183409/` rounds
83, 383, 406: `REPEAT` count from `MEDIAN(...) OVER (PARTITION BY c_chr, c_dec, c_dec)`
next to `bit_and(...) OVER (PARTITION BY c_int, c_chr, c_dec)`.
`SET disabled_optimizers='window_self_join'` on the base side matches the equivalent;
`statistics_propagation` does not silence them. Equivalents all carry a leftover
`QUALIFY` window (`CanOptimize` false). Round 406's diff is the other way
(base long string, equiv empty) because the *correct* median there CASTs to 0;
the wrongly-grouped singleton median is `c_int=42`. Select-list order is still
load-bearing: putting `MEDIAN` first makes `GROUP BY` use the duplicate-key list,
which SQL collapses to `(c_chr, c_dec)` and the REPEAT count becomes correct.

## Open items

- Regression window not bisected (single CLI on this box). The optimizer exists since ~v1.5.0
  (#20459); the subset-vs-multiset hole is in `PartitionsAreEquivalent` itself and may be older.
- GitHub issue not opened (not requested).
