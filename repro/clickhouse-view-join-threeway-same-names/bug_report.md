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

# ClickHouse: join-order optimizer LOGICAL_ERROR (Code 49) when a join-bodied VIEW participates in a 3-way join with a cross-table WHERE

## Summary

With the **new analyzer** (default), a `VIEW` whose body is an `INNER JOIN` / `CROSS JOIN`, used as one
arm of a **three-or-more-table** comma/`CROSS` join that also has a `WHERE` equi-predicate linking that
view to another table, aborts planning with:

```text
Code: 49. DB::Exception: Left and right columns have same names:
  [__table2.c_pk, __table2.c_int, __table2.c_big, __table3.c_pk, …],
  [__table2.c_pk, __table2.c_int, __table2.c_big]. (LOGICAL_ERROR)
```

Both sides of the reconstructed join cite **`__table2`** — the join-order optimizer has reused an
internal table id after flattening the view’s join into the outer multi-way join. The same SQL
succeeds under `enable_analyzer=0`, under `query_plan_optimize_join_order_limit=0`, when the third
table is dropped, or when the join view is wrapped in an extra derived-table boundary.

This is the same *class* of defect as [#89166](https://github.com/ClickHouse/ClickHouse/issues/89166) /
the join-order overlap guards in [#100401](https://github.com/ClickHouse/ClickHouse/pull/100401) and
[#106418](https://github.com/ClickHouse/ClickHouse/pull/106418), but the trigger here needs **no
parallel replicas** and is a plain view+3-way join shape that still fires on **26.8.1.701**.

## Environment

- ClickHouse **26.8.1.701** (official static build; `clickhouse`)
- Linux aarch64, throwaway `clickhouse server` with `join_use_nulls=1`, `max_threads=1`
- Found by eqgen differential fuzzing (`--generator sqlancerpp`, CrossJoin / FlagTableJoin-style
  equivalence views vs plain MergeTree forks)

## Minimal repro

```sql
CREATE TABLE L (c_pk Int64, c_int Int64, c_big Int64, eq Int64)
ENGINE = MergeTree ORDER BY tuple();
CREATE TABLE R (eq Int64) ENGINE = MergeTree ORDER BY tuple();
INSERT INTO L VALUES (1, 100, 1, 1), (2, 200, 2, 1);
INSERT INTO R VALUES (1);

CREATE VIEW t0 AS
SELECT l.c_pk AS c_pk, l.c_int AS c_int, l.c_big AS c_big
FROM L AS l INNER JOIN R AS r ON l.eq = r.eq;

CREATE TABLE t1 (c_pk Int64, c_int Int64, c_big Int64)
ENGINE = MergeTree ORDER BY tuple();
CREATE TABLE t2 (c_pk Int64, c_int Int64, c_big Int64)
ENGINE = MergeTree ORDER BY tuple();
INSERT INTO t1 VALUES (10, 10, 10);
INSERT INTO t2 VALUES (1, 1, 100), (2, 2, 200);

SELECT * FROM t0, t1, t2 WHERE t0.c_int = t2.c_big;
```

## Expected vs actual

| Query / setting | Expected | Actual on 26.8.1.701 |
|---|---|---|
| `SELECT * FROM t0, t1, t2 WHERE t0.c_int = t2.c_big` | 2 rows | **Code 49 LOGICAL_ERROR** (`__table2` on both sides) |
| same + `SETTINGS enable_analyzer=0` | 2 rows | 2 rows ✓ |
| same + `SETTINGS query_plan_optimize_join_order_limit=0` | 2 rows | 2 rows ✓ |
| drop `t1` (`FROM t0, t2 WHERE …`) | 2 rows | 2 rows ✓ |
| wrap join body in an extra derived table, then 3-way | 2 rows | 2 rows ✓ |
| `FROM t0 CROSS JOIN t1 CROSS JOIN t2 WHERE …` | 2 rows | **Code 49** (same) |

## Minimal oracle exposure path

**Object composition arity:** `2`

**GCL builder path:** `CrossJoinFilterAsInnerBuilder` → `CreateViewBuilder`

**Confidence:** inferred

**Realization:** A join-bodied `VIEW` is exposed as one arm of the outer multi-way join.

**Workload/data requirements (excluded from arity):**
- At least three relations in the outer comma/`CROSS JOIN`.
- A cross-table `WHERE` equi-predicate linking the join view to another relation.
- The new analyzer and join-order optimizer enabled.
- A join inside the exposed view.

**Exposure vs. intrinsic trigger:** The join-bodied view remains intrinsic to the standalone planner failure; generated keys and flag-table details are not required. The historical GCL mapping is inferred from the emitted CrossJoin/FlagTableJoin-style SQL rather than recorded builder metadata, so `CrossJoinFilterAsInnerBuilder` is the closest verified current class name, not a proven historical selection.

## Characterization

- **Planning, not execution.** Failure is at analyze/plan time; no rows are produced.
- **Join-order optimizer.** `query_plan_optimize_join_order_limit=0` silences it; `enable_analyzer=0`
  also silences it (old analyzer never builds this plan).
- **Needs ≥3 relations in the outer join** after the view’s internal join is inlined — two outer
  tables is clean.
- **Needs a join inside the view.** A plain `CREATE VIEW t0 AS SELECT … FROM L` does not trigger.
- An extra subquery wrapper around the view body (`CREATE VIEW t0s AS SELECT * FROM (…join…)`)
  restores a naming boundary and the query succeeds — evidence the bug is in flattening / renaming
  when a nested join is merged into a larger join graph for reordering.
- Error text shows **identical `__tableN` prefixes on left and right**, which matches the
  `JoinExpressionActions` invariant the earlier overlap-skip patches defend against — this shape
  still slips through on 26.8.1.701 without parallel replicas.

## Relation to known issues

Likely the same root cause family as:

- https://github.com/ClickHouse/ClickHouse/issues/89166
- https://github.com/ClickHouse/ClickHouse/pull/100401
- https://github.com/ClickHouse/ClickHouse/pull/106418

Those focus on parallel-replicas re-analysis and scalar-subquery collisions. **This report adds a
deterministic, no-replicas repro** (join-bodied VIEW + 3-way join + WHERE) that still LOGICAL_ERRORs
on 26.8.1.701, so either the guard does not cover view inlining or 26.8.1.701 lacks a needed
backport.

## Oracle / how found

eqgen equivalence: base forks are plain `CREATE TABLE tN AS SELECT * FROM t`; equivalent forks include
a CrossJoin/FlagTableJoin-style view (`l CROSS JOIN r WHERE l.eq = r.eq` projecting `l.*`). Same
8-row multisets on `t0`/`t1`/`t2`, then workload

```sql
SELECT * FROM t0, t1, t2 WHERE ((t0.c_int)=(t2.c_big)) ORDER BY t1.c_pk
```

base returns 16 rows; equivalent raises Code 49. Admissibility: `t0`/`t1`/`t2` row- and
type-identical (`t` renamed aside). Original finding:
`clickhouse_spp_20260810-063504/clickhouse_20260810-063505/error_round24_1.sql`.

## Files

- `reduced.sql` — self-contained minimal repro + controls
