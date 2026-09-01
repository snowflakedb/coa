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

# DuckDB: `partial_aggregate_pushdown` double-eager rewrite computes the wrong `MEDIAN` / `QUANTILE_CONT` / `LIST` over an inner join

## Summary

`PartialAggregatePushdown::TryDoubleEagerPushdown` rewrites `MEDIAN(fact.x) … JOIN … GROUP BY dim.g` into per-join-key `median … EXPORT_STATE` on one side, `count_star` on the other, a join of those partials, and `combine_aggr(state, opposite_count)`. That is correct for `SUM` / `COUNT` / `AVG` (the tests cover those). It is **not** correct for `MEDIAN`, `QUANTILE_CONT`, `MAD`, or `LIST`: the dimension-side multiplicity never makes it into the quantile/list state, so the result is the median of the *unweighted* per-key bags. On the distilled table, group `g=1` is six `1`s and four `42`s (median **1.0**); the rewrite answers **21.5** (`median({1,1,42,42})`).

`SET disabled_optimizers='partial_aggregate_pushdown'` restores 1.0. Disabling `join_order` also avoids the plan — that is the buggy rewrite not firing, not unspecified SQL. Python DuckDB 1.5.0 does not have this optimizer and already returns 1.0.

## Environment

- **DuckDB v2.0.0-alpha37826 (Cyanoptera)** `a9f869b6a7` — eqgen CLI
  `duckdb`.
- Access path: CLI `:memory:`. No `sql_mode`/collation.
- Confirmed on this CLI only. The `duckdb` 1.5.0 wheel rejects
  `disabled_optimizers='partial_aggregate_pushdown'` (`Optimizer type not recognized`) and returns the correct 1.0.

## Minimal repro

See [`reduced.sql`](./reduced.sql):

```sql
CREATE TABLE t(i INT, g INT, d INT, txt INT, chr INT);
INSERT INTO t VALUES
  (1, 1, 1, 1, 0),
  (1, 1, 1, 1, 0),
  (42, 1, 2, 3, 9),
  (42, 1, 1, 2, 2),
  (NULL, -7, 1, 0, 0),
  (42, NULL, 3, NULL, 3);

SELECT t1.g, MEDIAN(t3.i) AS m
FROM t t1
INNER JOIN t t2 ON t1.d = t2.d
INNER JOIN t t3 ON t2.txt = t3.chr
GROUP BY t1.g
ORDER BY 1 NULLS FIRST;
-- -7  1.0
--  1  21.5     WRONG (should be 1.0)
```

All six rows are load-bearing for this cardinality (dropping any one either stops PAP or changes the true bag so both plans agree).

## Expected vs actual

| Query | Expected | Actual |
|---|---|---|
| distilled `MEDIAN(t3.i) GROUP BY t1.g` | `-7 → 1.0`, `1 → 1.0` | `1 → 21.5` |
| same, `disabled_optimizers='partial_aggregate_pushdown'` | `1.0` / `1.0` | `1.0` / `1.0` |
| `CREATE TABLE j AS <the join>; SELECT g, MEDIAN(i) FROM j GROUP BY g` | `1.0` / `1.0` | `1.0` / `1.0` |
| `SUM` / `COUNT(*)` / `AVG` / `MIN` / `MAX` on the same join | match PAP-off | match |
| `LIST(t3.i ORDER BY t3.i)` for `g=1` | six `1`s, four `42`s, three `NULL`s (13) | `[1,1,42,42,NULL]` (5) |
| `QUANTILE_CONT(t3.i, 0.5)` | `1.0` | `21.5` |
| `MAD(t3.i)` for `g=1` | `0.0` | `20.5` |
| round 148 `SELECT DISTINCT MEDIAN(t3.c_int) … FOJ+IJ GROUP BY t1.c_big` | `{1.0}` | `{1.0, 21.5}` |

**Which side is wrong:** the **base table**. The equivalent's view/CTAS/index chain produces a plan that does **not** take the double-eager path (no `combine_aggr`), so it matches materializing the join. Ground truth is PAP off / `CREATE TABLE AS` the join, which also matches `COUNT`/`SUM` in the same `SELECT` as the wrong `MEDIAN` (`n=13`, `sum=174` both ways).

The join bag for `g=1` is determined: `list` after PAP-off / after CTAS is `[1,1,1,1,1,1,42,42,42,42,NULL,NULL,NULL]`. `MEDIAN` ignoring NULLs of ten values with six `1`s is 1.0. `21.5` is `MEDIAN(1,42)` — the median of the unweighted per-join-key bags `{1,1,NULL} ∪ {42} ∪ {42}`.

## Equivalence construction

**Round 148** (`mismatch_round148_0.sql`): 8-row heap `t`, long equivalent (array/struct pack, PIVOT macro, `UNION ALL` split, ENUM round-trip of `c_txt`, `ATTACH` mirror, `EXCEPT ALL` of an empty table, `QUALIFY` key-dedup). `SELECT * FROM t` is row-identical. Workload:

```sql
SELECT DISTINCT MEDIAN(t3.c_int)
FROM t t1
FULL OUTER JOIN t t2 ON t1.c_dec = t2.c_dec
INNER JOIN t t3 ON t2.c_txt = t3.c_chr
GROUP BY t1.c_big;
```

The equivalent is the *correct* side because its stats/plan never satisfy `DEEstimateCollapse`, so `TryDoubleEagerPushdown` does not run. The builder chain is not load-bearing: dropping it and running the same query on the heap already diverges from PAP-off. `FULL OUTER JOIN` is not required either — `INNER JOIN` on `c_dec` then `c_txt = c_chr` is enough. `DISTINCT` only hid the `-7` group (also 1.0).

`replay_eqgen.py` flags this as plan-dependent because `SET disabled_optimizers='join_order'` changes the **base** answer. That knob merely prevents the PAP plan (EXPLAIN loses `combine_aggr` / `EXPORT_STATE`). The SQL is determined: one join bag, one median. Treat it as an optimizer wrong-result, not an oracle comparability gap.

## Minimal oracle exposure path

- **Object composition arity:** unresolved.
- **GCL builder path:** unresolved — the equivalent chain changed statistics/plan selection, but its minimal builder subset was not bisected.
- **Confidence:** Unresolved; only the as-found mixed chain is known, so no class path or arity is inferred beyond the manifest.
- **Realization:** unresolved; the finding used a mixture of views, CTAS, catalog/file round-trips, and a terminal key-dedup view.
- **Workload/data requirements (excluded from arity):** a holistic aggregate such as `MEDIAN` over one join side, grouping from the other, and join cardinalities for which `DEEstimateCollapse` selects double-eager partial aggregate pushdown.

**Exposure vs. intrinsic trigger:** The builder chain is not intrinsic to the DuckDB bug: the heap query alone is wrong when partial aggregate pushdown fires. It was nevertheless part of the original oracle exposure because its unbisected statistics/plan effect prevented that rewrite on the equivalent side; “not load-bearing” here means not required for the engine defect, not that a minimal oracle contrast path was established.

## Characterization

**Trigger:** `MEDIAN` / `QUANTILE_CONT` / `MAD` / `LIST` of a column from one side of an inner join, `GROUP BY` a column from the other side, at a cardinality where `TryDoubleEagerPushdown` fires (`DEEstimateCollapse`: both sides collapse ≥2×). EXPLAIN contains:

```
Perfect Hash Group By   Aggregates: combine_aggr(#1, #2)
  Hash Join INNER
    Perfect Hash Group By  Groups: <join key>
                           Aggregates: median(#) EXPORT_STATE
    Perfect Hash Group By  Groups: <join key>, <group col>
                           Aggregates: count_star()
```

**Does NOT trigger / stays correct:**

- `SET disabled_optimizers='partial_aggregate_pushdown'`
- `SET disabled_optimizers='join_order'` (different plan, not a second bug)
- Materializing the join (`CREATE TABLE AS`) then aggregating
- `SUM`, `COUNT`, `AVG`, `MIN`, `MAX` on the same join (RepeatedCombine is right for those)
- `QUANTILE_DISC(0.5)` / `MODE` / `approx_quantile` on *this* bag (they happen to still return 1; not a proof they are safe)
- DuckDB 1.5.0 (no `partial_aggregate_pushdown` optimizer)

**Mechanism** (`duckdb/src/optimizer/partial_aggregate_pushdown.cpp`):

`DECanRepeatAggregateState` (~608–623) is a denylist of `decimal_average` and hugeint `sum`/`avg`. Everything else with `HasStateCombineCallback` is treated as repeatable:

```cpp
if ((name != "sum" && name != "avg") || aggr.GetChildren().empty()) {
    return true;  // MEDIAN, LIST, MAD, …
}
```

`DECreateUpperAggregates` (~771–775) then emits `combine_aggr(partial_ref, cnt_ref(1 - de.side))`. `combine_aggr`'s multiplicity path (`AggregateExecutor::GenericRepeatedCombine`) reconstructs a join-expanded **sum**. Quantile/list state combine does not replicate the sample bag `count` times, so the upper `GROUP BY g` merges one copy of each join-key's `{values}` — here `{1,1,NULL}`, `{42}`, `{42}` → median 21.5.

`IsSupportedAggregate` (~91–113) already rejects `DISTINCT` / `FILTER` / `ORDER BY` / `decimal_average`, but not holistic aggregates. `test/optimizer/partial_aggregate_pushdown.test` checks `sum`/`count`/`min`/`max`/`avg` only.

Introduced when PAP was generalized past distributive aggregates: `6a7345a0a3` *"eager aggregation for all aggregate functions with export, not just distributive ones"*.

Fix shape: whitelist aggregates that implement a correct `RepeatedCombine` (`sum`, `count`, `min`, `max`, `avg`), or add a real multiplicity-aware combine for quantile/list. Do not trust `HasStateCombineCallback` alone.

DML not applicable (SELECT aggregate). CLI nightly only.

## How it was found

eqgen rich-shuffle3 hunt `eqgen/duck_rich_shuffle3/duckdb_20260815-234735/` (seed 1098581485). 8 mismatches on this CLI; 7 are two already-filed bugs:

| Rounds | Bug | Mask |
|---|---|---|
| 45, 250 | `WindowSelfJoinOptimizer` duplicate `PARTITION BY` keys (`bit_xor … PARTITION BY c_int, c_int` next to another window) | `window_self_join` |
| 137, 247, 263, 273, 288 | `SUBSTR` ASCII-stats vs Unicode (`SUBSTR(s, 3, negative)` or `SUBSTR(s, -12345678, n)`; round 288 wraps it in `MAX(SUBSTR(…, c_int, 1)) OVER`) | `statistics_propagation` |

Round 148 is this one. Original finding: `mismatch_round148_0.sql`. Repro: [`reduced.sql`](./reduced.sql).

A query-rewrite oracle that only permutes the SELECT would miss it: both plans are “`MEDIAN` after an inner join”, and `COUNT`/`SUM` in the same statement stay right.

## Open items

- Regression window: PAP file exists since mid-2026 on this tree; the “all aggregates with export” expansion is `6a7345a0a3`. Not bisected to a release tag.
- `VAR_POP` on the distilled query differs by 1 ULP (`403.44` vs `403.44000000000005`) under PAP; likely the same combine-order class, not investigated.
- `STRING_AGG` was correct here; `LIST` was not. Not every ordered aggregate is safe to assume.
- Do not open a GitHub issue unless asked; this folder is filing-ready.
