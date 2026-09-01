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

# DuckDB: `WindowSelfJoinOptimizer` drops a throwing `ORDER BY` when rewriting `QUALIFY COUNT(*) OVER (… ROWS UNBOUNDED)`

## Summary

`WindowSelfJoinOptimizer` rewrites

```sql
QUALIFY COUNT(*) OVER (
  PARTITION BY p
  ORDER BY <expr>
  ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
) >= k
```

into a grouped `COUNT` joined back to the scan. `TranslateAggregate` then treats `COUNT` as not order-dependent and **drops `<expr>`** (`window_self_join.cpp`: “ORDER BY is a NOP, so drop it”). When `<expr>` is `HASH(…) + HASH(…)`, that addition overflows `UINT64`. The rewrite never evaluates it, so a heap table returns rows; a relation whose plan still contains a `LogicalWindow` (even a leftover `COUNT(*) OVER ()` inside a view) makes `CanOptimize(child)` return false, the `Window` operator remains, and the **same query throws**.

`SET disabled_optimizers='window_self_join'` makes the heap table throw too. DuckDB 1.5.0 does not apply this rewrite and already throws on the heap table.

This is a different symptom from the duplicate-`PARTITION BY` wrong-result bug in the same optimizer (`eqgen/repro/duckdb-20260814-050454-round37-window-self-join-dup-partition/`). That one mis-identifies partition keys; this one drops a throwing `ORDER BY` of a full-frame `COUNT`.

## Environment

- **DuckDB v2.0.0-alpha37826 (Cyanoptera)** `a9f869b6a7` — eqgen CLI
  `duckdb`.
- Access path: CLI `:memory:`. No `sql_mode`/collation.
- Python `duckdb` 1.5.0 wheel: heap query already throws
  `Out of Range Error: Overflow in addition of UINT64`.
- Local `duckdb` checkout: `TranslateAggregate` at `src/optimizer/window_self_join.cpp:89–91`
  drops `ORDER BY` for non-order-dependent aggregates; `CanOptimize(LogicalOperator)`
  (`window_self_join.cpp:176–188`) returns false unless the child is GET / projection /
  aggregate / filter (so a leftover `LOGICAL_WINDOW` blocks the rewrite).

## Minimal repro

See [`reduced.sql`](./reduced.sql). The overflowing `SELECT` is last; it does not poison `:memory:`, but later statements in the same session are skipped.

```sql
CREATE TABLE b ( … 7 rows, including c_dec = 999.99 … );
CREATE TABLE t AS SELECT * FROM b;

SELECT DISTINCT t1.c_big NOT IN (t1.c_big), t1.c_dec, t1.c_txt
FROM t AS t1
QUALIFY COUNT(*) OVER (
  PARTITION BY CASE WHEN t1.c_chr IS NULL THEN IFNULL(t1.c_int, t1.c_big) ELSE t1.c_dec END
  ORDER BY HASH(sha256(t1.c_txt), t1.c_chr) + HASH(t1.c_dec, t1.c_dec) ASC,
           CASE WHEN True THEN t1.c_chr END DESC NULLS FIRST,
           t1.c_int ASC NULLS LAST
  ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
) >= t1.c_int;
-- 4 rows  (rewrite dropped HASH+HASH)

CREATE VIEW t AS
SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts
FROM (SELECT *, COUNT(*) OVER () AS dummy FROM b);

-- same SELECT
-- Out of Range Error: Overflow in addition of UINT64
--   (18212156630472451589 + 18212156630472451589)
```

`EXPLAIN` of the heap query is a **Hash Join** of a seq scan to a **Hash Group By `count()`** on the `PARTITION BY` key — no `hash()` in the plan. `EXPLAIN` of the view query keeps a **Window** `count() OVER (… ORDER BY (hash(…) + hash(…)) … ROWS UNBOUNDED …)`.

## Expected vs actual

| Query | Expected | Actual (nightly CLI) |
|---|---|---|
| heap table, QUALIFY `COUNT(*) OVER (PARTITION BY … ORDER BY HASH+HASH ROWS UNBOUNDED)` | throw, **or** both sides rows | **4 rows** |
| leftover-`COUNT(*) OVER ()` view, same QUALIFY | throw, **or** both sides rows | **`Out of Range Error` UINT64 add** |
| heap + `SET disabled_optimizers='window_self_join'` | throw | throw |
| `CREATE VIEW t AS SELECT * FROM b` (no leftover window) | 4 rows | 4 rows (rewrite still fires) |
| CTAS `COUNT(*) OVER ()` then `DROP COLUMN dummy` | 4 rows | 4 rows (no `LogicalWindow` left) |
| `SELECT HASH(999.99::DECIMAL(10,2)) + HASH(999.99::DECIMAL(10,2))` | throw | throw |
| DuckDB 1.5.0 wheel, heap QUALIFY | throw | throw |

**Which side is wrong:** the **base (heap)** on nightly. Ground truth is “evaluate `ORDER BY`”: 1.5.0, `window_self_join` off, and the leftover-window view all throw the same UINT64 overflow, and `HASH(d)+HASH(d)` overflows on these values by itself. The nightly heap plan is the rewrite that never computes `HASH`. If maintainers consider dropping unused `ORDER BY` of a full-frame `COUNT` valid, the view side is then a missed optimization rather than a wrong result — still a plan-dependent **error**, which the equivalence oracle is entitled to flag.

The `ROWS UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING` frame *is* the whole partition, so this is not [duckdb#21592](https://github.com/duckdb/duckdb/issues/21592) (wrong results for `ROWS … CURRENT ROW`). That issue’s “don’t rewrite ROWS frames” fix would also mask this, but the load-bearing defect here is dropping a throwing `ORDER BY`, not the frame type.

## Equivalence construction

**Round 596** (`error_round596_0.sql`) and six more findings with the **identical QUALIFY**
(`error_round{697,990,1002,1143,1298,1335}_0.sql`):

1. Seeded 8-row rich table (row 8 duplicates row 2).
2. Equivalent: extra NULL column, macro, tautological `c_ts IN` CTAS, `ROW_NUMBER` key,
   `UNION ALL` double, then
   `SELECT DISTINCT k, MAX(col) OVER (PARTITION BY k) AS col …` restore view.
3. That restore view is a leftover `LogicalWindow`. `WindowSelfJoinOptimizer::CanOptimize`
   on the child returns false, so QUALIFY stays a `Window` and `HASH+HASH` runs.

The tautological filter, macro, and `UNION ALL` are **not** required. Distilled trigger is
any scanned relation whose plan still contains `LOGICAL_WINDOW`:

```sql
CREATE VIEW t AS
SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts
FROM (SELECT *, COUNT(*) OVER () AS dummy FROM b);
```

`MAX(c_dec) OVER (PARTITION BY c_pk)` as the view’s `c_dec` also throws. Materialising the
leftover window into a heap table and dropping the dummy column does **not**.

## Minimal oracle exposure path

- **Object composition arity:** 2.
- **GCL builder path:** inferred residual-window transform → `CreateViewBuilder`; the emitted `DISTINCT` + `MAX(...) OVER (PARTITION BY key)` SQL matches the current `KeyWindowAggregateReduceBuilder` shape, but the historical class selection is unresolved.
- **Confidence:** Inferred from emitted SQL, not exact AST metadata.
- **Realization:** an inlinable `VIEW` retains a `LogicalWindow`; materializing the same result as a table removes that residual operator.
- **Workload/data requirements (excluded from arity):** full-frame partitioned `COUNT(*)` with a throwing `ORDER BY` expression, a predicate shape that lets `WindowSelfJoinOptimizer` fire on the heap, and values whose `HASH + HASH` overflows.

**Exposure vs. intrinsic trigger:** The residual-window view supplies the contrasting plan by blocking the rewrite, while the heap side permits the rewrite to drop the throwing `ORDER BY`. The intrinsic issue is that optimizer behavior; the specific current GCL class remains inferred rather than claimed as exact.

## Characterization

- **`COUNT(*)` + full-partition frame + `ORDER BY` that overflows** is the query shape.
  `HASH(c_dec)+HASH(c_dec)` is enough; `sha256` / extra `ORDER BY` keys are not required
  for the overflow itself, but they are what the original QUALIFY used.
- **`PARTITION BY CASE WHEN c_chr IS NULL THEN … ELSE c_dec END` plus `>= t1.c_int`**
  is what lets the rewrite fire on a heap table. A bare
  `QUALIFY COUNT(*) OVER (ORDER BY HASH(c_dec)+HASH(c_dec) ROWS UNBOUNDED) >= 0`
  keeps a `Window` even on the heap and throws on **both** sides.
- **Mask:** `SET disabled_optimizers='window_self_join'`. Disabling `window_rewriter`,
  `top_n_window_elimination`, `filter_pushdown`, `unused_columns`, etc. does **not**.
- **Not volatile:** `HASH` is not marked volatile, so `VolatileExpressionCounter` does
  not save the `ORDER BY`.
- Source: `TranslateAggregate` (`window_self_join.cpp:89–91`) drops `ORDER BY` when
  `GetOrderDependent() != ORDER_DEPENDENT`. `COUNT` is not order-dependent. The
  comment at `CanOptimize` line 133 (“ROWS framing is excluded”) is **not** implemented
  for `UNBOUNDED_PRECEDING` / `UNBOUNDED_FOLLOWING` — those boundaries are accepted.

## How it was found

eqgen rich-shuffle3 (`duckdb_20260815-234735`), seed 613871602. The equivalence oracle
ran the same QUALIFY against a heap `t` and a row-identical restore view; only the view
threw. `t` is 8-row identical (admissibility pass). Determinism skipped (one-sided error).

Seven findings, one query. Original:
`duck_rich_shuffle3/duckdb_20260815-234735/error_round596_0.sql`
(and 697, 990, 1002, 1143, 1298, 1335).

## Open items

- Whether the intended contract is “full-frame `COUNT` may drop `ORDER BY`” (then the
  leftover-window plan is a missed optimization that surfaces a valid overflow) or
  “`ORDER BY` expressions must be evaluated” (then the heap rewrite is the bug).
- Not bisected to a DuckDB commit. Clean on 1.5.0; fails on `a9f869b6a7`.
