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

# CrateDB: `CROSS JOIN … WHERE l.x = r.x` drops a correlated `Filter` after rewrite to HashJoin

## Summary

A view whose body is `SELECT … FROM a l CROSS JOIN b r WHERE l.x = r.x` (or the comma-join
spelling `FROM a l, b r WHERE l.x = r.x`) is rewritten to `HashJoin[INNER | (x = x)]` under an
empty `Eval[]`. A correlated predicate on that view — `WHERE q.w` with outer `w = FALSE` — then
**vanishes** from the SubPlan: the HashJoin Collects have `| true` and there is no `Filter[w]`.
`COUNT` / `EXISTS` / `IN` therefore see every row of the view.

The same view written `INNER JOIN … ON l.x = r.x` produces the same HashJoin but **keeps**
`Filter[w]` above it and returns the correct empty/zero result. Disabling either
`optimizer_move_filter_beneath_eval` or `optimizer_move_filter_beneath_rename` puts `Filter[w]`
back on the CROSS JOIN spelling. No error. `#19855` is a different bug (partitioned Collect
merge); `optimizer_merge_filter_and_collect = false` does not mask this one.

## Environment

| | |
|---|---|
| Engine | CrateDB **6.4.2** (built `1db6455`), official Docker `crate:6.4.2` |
| Session | defaults. `optimizer_move_filter_beneath_eval` and `optimizer_move_filter_beneath_rename` are `true` by default; turning **either** off is the mask |
| Access path | PostgreSQL wire via `psycopg` |
| Shards | `CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0)` |
| Determinism | stable across repeats |
| DML | views reject `DELETE`/`UPDATE`; not exercised on a base table (the trigger is the join view) |

## Minimal repro

```sql
CREATE TABLE b (name TEXT)
    CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES ('abc');
REFRESH TABLE b;
CREATE VIEW v AS SELECT l.name FROM b l CROSS JOIN b r WHERE l.name = r.name;

SELECT (SELECT COUNT(*) FROM v WHERE q.w) FROM (SELECT FALSE AS w) q;
-- Expected 0
-- Actual   1     <<< WRONG

SELECT (SELECT COUNT(*) FROM b WHERE q.w) FROM (SELECT FALSE AS w) q;
-- 0   (same predicate on the heap table)
```

`EXISTS (SELECT 1 FROM v WHERE q.w)` is TRUE instead of FALSE. `n IN (SELECT name FROM v WHERE sq.w)`
returns `'abc'` instead of empty. Reproduces if the `CREATE VIEW` is inlined as a derived table.

The full controls and the builder-emitted `ROW_NUMBER` / `CROSS JOIN WHERE eq = eq` shape are in
`reduced.sql`.

## Expected vs actual

The **heap table / INNER JOIN ON** path is the correct side. `WHERE FALSE` over `v` is empty.

| query | heap / `INNER JOIN ON` (correct) | `CROSS JOIN WHERE` (actual) |
|---|---|---|
| `SELECT (SELECT COUNT(*) FROM v WHERE q.w) FROM (SELECT FALSE AS w) q` | `0` | **`1`** |
| same, 2-row identity `v` | `0` | **`2`** |
| `EXISTS (SELECT 1 FROM v WHERE q.w)` | `FALSE` | **`TRUE`** |
| `n IN (SELECT name FROM v WHERE sq.w)` with `n = 'abc'` | empty | **`('abc')`** |
| `INNER JOIN … ON l.name = r.name` | `0` | — |
| `CROSS JOIN` with **no** equi-`WHERE` | `0` | — |
| `SET optimizer_move_filter_beneath_eval = false` | `0` | `0` (masked) |
| `SET optimizer_move_filter_beneath_rename = false` | `0` | `0` (masked) |
| `SET optimizer_merge_filter_and_collect = false` | — | still `1` |
| `SET optimizer_rewrite_equi_join_to_hash_join = false` | — | still `1` |

`MoveFilterBeneathEval` (`MoveFilterBeneathEval.java:57-62`) transposes `Filter` through `Eval`
unconditionally (*"Eval never adds columns, so this is safe"*). After the CROSS JOIN `WHERE` is
folded into the HashJoin, that Eval is empty (`Eval[]`) and the transpose drops the correlated
`w` instead of leaving `Filter[w]` above the join.

## Equivalence construction

eqgen's oracle builds a second relation with the same rows and declared types as base `t`, then
runs the same query on both. The equivalent here is the **join round-trip**: `ROW_NUMBER() AS eq`,
key table, `CREATE VIEW t AS SELECT … FROM l CROSS JOIN r WHERE l.eq = r.eq`. Distilled, the
row-number key is not needed — a one-column self `CROSS JOIN … WHERE l.name = r.name` is enough.
`INNER JOIN ON` is *not* equivalent for this planner path even though it is equivalent SQL.

Load-bearing composition: **`CROSS JOIN` / comma-join with the equi-condition in `WHERE`** ×
**correlated filter on that view** (`WHERE outer_boolean`). The harvested query's 3-way join,
`MAX(starts_with(…)) OVER (ORDER BY LEAST(created_at, ''))`, and `RIGHT(…, -n) LIKE` scalar all
reduced away; they only manufactured a runtime-false boolean (`w`) and an `IN` that became
match-all once `WHERE w` was dropped. Names that also appear as `created_at` (`'abc'`,
`'o''brien'`) were the two extra `GROUP BY` keys in the original mismatch.

Constructs reduced away: tag/`UNION ALL` copies, 4-shard `CLUSTERED BY`, window collapse, the
outer 3-way join.

## Minimal oracle exposure path

**Object composition arity:** `2`

**GCL builder path:** `CrossJoinFilterAsInnerBuilder` → `CreateViewBuilder`

**Confidence:** verified

**Realization:** A `VIEW` exposes the row-preserving `CROSS JOIN ... WHERE` identity relation.

**Workload/data requirements (excluded from arity):**
- A correlated outer boolean that evaluates false.
- The equality must be written in `WHERE`, not as `INNER JOIN ... ON`.
- At least one matching row so a dropped filter is observable.

**Exposure vs. intrinsic trigger:** The cross-join/filter transform remains in the standalone trigger, while its generated row key and flag tables reduce to a one-column self-join; the `VIEW` is the oracle realization but can be replaced by an inline derived table. Thus the builders supplied the row-identical contrasting path, while the intrinsic defect is the `CROSS JOIN ... WHERE` plan under a correlated filter.

## Characterization

**Plan diff** (1-row distilled, `EXPLAIN` of the COUNT query):

Wrong, `CROSS JOIN WHERE` (`COUNT = 1`):

```
… SubPlan
    └ HashAggregate[count(*)]
      └ Rename[] AS v
        └ Eval[]
          └ HashJoin[INNER | (name = name)]
            ├ Collect[b | [name] | true]
            └ Collect[b | [name] | true]
```

No `Filter[w]`.

Correct, `INNER JOIN ON` (`COUNT = 0`) — same HashJoin, Filter retained:

```
… SubPlan
    └ HashAggregate[count(*)]
      └ Rename[] AS v
        └ Eval[]
          └ Filter[w]
            └ HashJoin[INNER | (name = name)]
              ├ Collect[b | [name] | true]
              └ Collect[b | [name] | true]
```

Correct, `SET optimizer_move_filter_beneath_eval = false` on the CROSS JOIN view:

```
… Filter[w]
    └ Eval[]
      └ HashJoin[INNER | (name = name)]
```

Heap `Count[b | w]` also keeps the predicate on Collect.

The load-bearing spelling is `WHERE` vs `ON`: `FROM a, b WHERE a.x = b.x` is wrong; `FROM a JOIN b
ON a.x = b.x` is right. A CROSS JOIN with no equi-`WHERE` keeps `Filter[w]` above
`NestedLoopJoin[CROSS]`.

## How it was found

eqgen data-equivalence oracle, simple catalog, builder-composition hunt
`crate_simple_shuffle_keytag/cratedb_20260819-172428/`. Seed is not replay-stable; the `.sql`
files are the source of truth.

A query-rewrite oracle (TLP / NoREC / EET) would miss this: it holds the data as a plain table,
and the trigger is the join-round-trip view. The equivalence oracle keeps the query still and
swaps in a row-identical `CROSS JOIN WHERE` `t`.

Original finding: extra rows **only in equivalent** (2× `(NULL,)`), opposite direction from the
`= ALL (empty)` window cluster. Replay gates: 8=8 rows, types equal, stable.

- Repro and controls: [`reduced.sql`](reduced.sql)
- Original finding: `eqgen/log/crate_simple_shuffle_keytag/cratedb_20260819-172428/mismatch_round41_0.sql`

## Duplicate search

- [crate/crate#19855](https://github.com/crate/crate/issues/19855) (closed) — `move_filter_beneath_rename`
  plus `merge_filter_and_collect` on a **PARTITIONED** table, rows **dropped**. This finding has no
  generated partition, `merge_filter_and_collect = false` does not mask it, and the symptom is extra
  rows. Same optimizer family, different defect.
- [crate/crate#19982](https://github.com/crate/crate/issues/19982) (closed) — volatile filter pushed
  *beneath* WindowAgg. Opposite direction; not this plan.
- [crate/crate#19922](https://github.com/crate/crate/issues/19922) (open) — `NULL = ALL (empty)` on a
  window output. Vacuous ALL 3VL, not a dropped correlated Filter.
- GitHub `crate/crate` search for `move_filter_beneath_eval`, `CROSS JOIN WHERE` + correlated Filter,
  `HashJoin` dropping `Filter[w]`: no match. `gh issue list` / `gh search` (2026-08-19). No Jira in
  this environment.

## Open items

- Regression window not bisected (6.4.2 only this session).
- Whether `Util.transpose` dropping a correlated symbol through empty `Eval[]` is the whole fix, or
  the CROSS-JOIN-WHERE → HashJoin rewrite should not leave an Eval that the filter-move rules treat
  as transparent.
- Suggested fix: do not transpose a Filter whose symbols are outer/correlated through Eval/Rename
  above a join; or keep `Filter[w]` above HashJoin for the CROSS JOIN WHERE rewrite the same way
  INNER JOIN ON already does.
