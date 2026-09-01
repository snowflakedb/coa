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

# Dolt: `COUNT(*) OVER ()` returns 1 instead of the partition size when nothing in the query references a column

## Summary

A window aggregate whose value depends on the row count (`COUNT`, `SUM`) but whose argument
references **no column** (`COUNT(*)`, `COUNT(0)`, `COUNT('s')`, `SUM(1)`) returns the *current-row*
count rather than the partition count — whenever **no column of the table is referenced anywhere in
the query**:

```sql
SELECT COUNT(*) OVER () FROM b;   -- 3-row table b: dolt 1,1,1 ; MySQL 9.7 3,3,3
```

Row counts are **not** affected (`SELECT 1 FROM b` correctly yields 3 rows), and the ordinary
`COUNT(*)` is correct, so this is the window *frame* collapsing to a single row rather than a scan
losing rows. Referencing a column anywhere — a projection, a `WHERE`, a *column-dependent*
`PARTITION BY` or `ORDER BY`, or the aggregate's own argument — restores the correct answer, which
points at column pruning: when nothing needs a column from the table, the partition the window
iterator is handed appears to hold one row.

**The window spec does not have to be empty.** A `PARTITION BY`/`ORDER BY` over *constants* does not
rescue it (`OVER (PARTITION BY 'k' ORDER BY 'j' ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED
FOLLOWING)` is still `1,1,1`); only a column-dependent one does. The governing condition is the
absence of any column reference in the whole query, not the shape of the `OVER` clause.

Silent wrong result, no error. It surfaced as **missing rows**, not a wrong number, because the
window value fed a join key.

## Environment

- **Engine**: Dolt 8.0.31 (`VERSION()`), source `v2.2.3-49-ga995f245c`, commit
  `a995f245c032bc412aed308194d81ee12bc74f19`, assertions off.
- **go-mysql-server**: `v0.20.1-0.20260805191915-e5eafe0da809`.
- **sql_mode**: not load-bearing — reproduces under any; the run used
  `STRICT_ALL_TABLES,NO_ZERO_DATE,NO_ZERO_IN_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION,ONLY_FULL_GROUP_BY,NO_BACKSLASH_ESCAPES,PIPES_AS_CONCAT`.
- **charset / collation**: `utf8mb4`; database `utf8mb4_0900_bin`, connection `utf8mb4_0900_ai_ci`.
- Independent of primary key, of row count (still `1` at 100 rows), and of `LIMIT`.
- **Contrast engine**: MySQL 9.7.2 release build.

## Minimal repro

```sql
CREATE TABLE b (x BIGINT);
INSERT INTO b VALUES (1),(2),(3);
SELECT COUNT(*) OVER () FROM b;
```

## Expected vs actual

| query | expected (MySQL 9.7) | actual (Dolt) |
|---|---|---|
| `SELECT COUNT(*) OVER () FROM b` | `3, 3, 3` | **`1, 1, 1`** |
| `SELECT COUNT(0) OVER () FROM b` | `3, 3, 3` | **`1, 1, 1`** |
| `SELECT SUM(1) OVER () FROM b` | `3, 3, 3` | **`1, 1, 1`** |
| `SELECT COUNT(*) OVER (ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) FROM b` | `3, 3, 3` | **`1, 1, 1`** |
| `SELECT COUNT(*) OVER (PARTITION BY 'k') FROM b` | `3, 3, 3` | **`1, 1, 1`** |
| `SELECT COUNT(*) OVER (ORDER BY 'k') FROM b` | `3, 3, 3` | **`1, 1, 1`** |
| `SELECT COUNT(*) OVER (PARTITION BY 'k' ORDER BY 'j' ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) FROM b` | `3, 3, 3` | **`1, 1, 1`** |
| `SELECT COUNT(*) OVER () FROM (SELECT x FROM b) d` | `3, 3, 3` | **`1, 1, 1`** |
| `WITH c AS (SELECT x FROM b) SELECT COUNT(*) OVER () FROM c` | `3, 3, 3` | **`1, 1, 1`** |
| C1 `SELECT x, COUNT(*) OVER () FROM b` | `3, 3, 3` | `3, 3, 3` |
| C2 `SELECT COUNT(x) OVER () FROM b` | `3, 3, 3` | `3, 3, 3` |
| C3 `SELECT COUNT(x + 0) OVER () FROM b` | `3, 3, 3` | `3, 3, 3` |
| C4 `SELECT COUNT(*) OVER () FROM b WHERE x >= 0` | `3, 3, 3` | `3, 3, 3` |
| C5 `SELECT COUNT(*) OVER (PARTITION BY x IS NOT NULL) FROM b` | `3, 3, 3` | `3, 3, 3` |
| C6 `SELECT COUNT(*) OVER (ORDER BY x) FROM b` | `1, 2, 3` | `1, 2, 3` |
| C7 `SELECT COUNT(*) OVER () FROM bv` (a view) | `3, 3, 3` | `3, 3, 3` |
| C8 `SELECT 1 FROM b` | 3 rows | 3 rows |
| C9 `SELECT COUNT(*) FROM b` | `3` | `3` |

## Equivalence construction

### Concrete, as the builder emits it

`mismatch_round43_0`'s equivalent `t` is a **predicate-split (TLP) partition chain** — `p` / `NOT p` /
`p IS NULL` branches recombined with `UNION ALL` across 20 intermediate tables and views — sitting
under a **QUALIFY window-filter view**:

```sql
ALTER TABLE t RENAME TO t__base;
CREATE TABLE t__base_table_1 AS SELECT id, name, created_at FROM
  (SELECT id, name, created_at, ROW_NUMBER() OVER (ORDER BY id) >= 1 AS _qf FROM t__base) AS _qw WHERE _qf;
CREATE TABLE t__base_table_2 AS SELECT ... FROM t__base_table_1 WHERE <p>;
CREATE VIEW  t__base_view_1  AS SELECT ... FROM t__base   WHERE <NOT p>;
...
CREATE VIEW  t__base_view_2  AS SELECT * FROM t__base_view_1 UNION ALL SELECT * FROM t__base_table_3
                                 UNION ALL SELECT * FROM t__base_table_4;
...
```

Oracle admissibility passed: base `t` and equivalent `t` are row-identical (both 8 rows).

### The load-bearing construct

**None of it.** This is the useful part: the equivalence chain is not the trigger, it is the
*contrast*. The bug is on the **base** side — the plain table gives `1` — and every one of the 20-odd
builder-emitted relations gives the correct `8`, because each of them puts a view, a filter or a
`UNION ALL` above the scan and that keeps a column alive (control C7 isolates this to a single view).

So the oracle found a base-side wrong result by having a *correct* equivalent to compare against —
the inverse of the usual direction, and the reason a single-relation fuzzer would have had to already
know the right answer to notice.

Reduced away: the whole 20-relation chain, the outer 3-way join, the `LEAST`/`SUBSTR`/`DAYNAME`/`hex`/
`SHA2`/`NULLIF` expression tree, `DISTINCT`, 7 of 8 rows and 2 of 3 columns.

## Minimal oracle exposure path

- **Object composition arity:** `3`.
- **GCL builder path:** `QualifyQueryBuilder → TlpPartitionUnionQueryBuilder → CreateViewBuilder`.
- **Confidence:** high; all three are exact current factory class names and map to the report's
  qualify layer, three-way predicate partition, and final view.
- **Realization:** `CreateViewBuilder` exposes the rebuilt relation under the queried name.
- **Workload/data requirements (excluded from arity):** the column-free window aggregate, constant-only
  window specification, and having more than one input row are workload/data conditions.
- **Exposure vs. intrinsic trigger:** arity 3 describes the object contrast that supplied a correct
  equivalent. The entire chain can reduce away as an intrinsic requirement: the bug is in the plain
  base-table plan, while even a simple intervening view is enough to route the equivalent correctly.

## Characterization

- **Trigger**: a row-count-dependent window aggregate (`COUNT`, `SUM`) whose argument references no
  column, in a query where **nothing else references a column of that table either**. The `OVER`
  clause's shape is not the governing condition.
- **Necessary and sufficient, by control**: adding *any* column reference fixes it — as a projection
  (C1), as the aggregate's argument (C2, C3), in a `WHERE` even one that is always true (C4), in a
  `PARTITION BY` (C5), or in an `ORDER BY` (C6). Reading the same table through a view fixes it (C7).
- **A constant `PARTITION BY`/`ORDER BY` does NOT fix it**: `OVER (PARTITION BY 'k')`,
  `OVER (ORDER BY 'k')` and `OVER (PARTITION BY 'k' ORDER BY 'j' ROWS BETWEEN UNBOUNDED PRECEDING AND
  UNBOUNDED FOLLOWING)` all still return `1`. Only a *column-dependent* partition/order key rescues
  it, which is why the condition is stated as "no column reference anywhere" rather than "empty
  window spec". `mismatch_round61_0` is the finding that established this — its `sq2` has a full
  `PARTITION BY CONCAT('YEAR','MONTH') ORDER BY <constants> ROWS BETWEEN …` spec and is still wrong.
- **Not every constant-argument aggregate shows it**: `AVG('-12345678') OVER (ORDER BY 'k')` returns
  the constant on both engines, because its value does not depend on how many rows are in the frame.
  The symptom needs an aggregate that counts.
- **Not frame defaulting**: an explicit `ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING`
  still returns `1`, so this is distinct from dolt#11381 (closed, default frame was ROWS not RANGE).
- **Not COUNT-specific**: `SUM(1) OVER ()` is equally wrong, so it is the aggregate's *input rows*,
  not `COUNT`'s star handling.
- **Not row loss**: `SELECT 1 FROM b` returns 3 rows (C8) and plain `COUNT(*)` returns 3 (C9). Only
  the window partition is wrong.
- **Derived tables and CTEs do not mask it**, while a view does — consistent with the derived
  table/CTE being flattened into the scan while a view is not.
- **Likely mechanism**: column pruning reduces the table scan to zero columns when no column is
  needed, and the window iterator then treats its input as a single row. Not confirmed against the
  source.
- Not a crash; a silent wrong result. Assertions-off build; irrelevant here.

## How it was found

The eqgen v3 data-equivalence oracle, and this one is a good illustration of *why* the oracle
manufactures ground truth rather than needing an expected output. The generator produced

```sql
SELECT DISTINCT CEIL(CAST(NULL AS SIGNED)) AS expr_0_number,
                CAST(COUNT(*) OVER (ORDER BY '©' DESC) AS SIGNED) AS expr_1_number FROM t AS t1
```

— a subquery projecting only a NULL constant and a window value, i.e. exactly the no-column-reference
shape — and then joined it on `sq2.expr_1_number = t3.id`. On the base table the window value was `1`,
on the row-identical equivalent it was `8`, so the join matched **different rows** and two rows went
missing from the base side's result. The visible symptom was a 2-row multiset difference; the cause
was a wrong scalar three levels down.

A query-rewrite oracle (TLP / NoREC / EET) would struggle here: it holds the relation fixed and
rewrites the query, and the trigger is destroyed by almost any rewrite — adding a predicate,
projecting a column, or partitioning the window all make Dolt answer correctly. The relation-swapping
axis is what kept the query intact while changing the answer.

- Seed: `1824219467`, engine `@a995f245` (current `main`, so not a stale-build artifact).
- `reduced.sql` in this folder.
- Original findings: hunt log
  (seed 1824219467) and hunt log
  (seed 42337059). The second is the same bug reached through a `COUNT(*) OVER (PARTITION BY
  CONCAT('YEAR','MONTH') ORDER BY …)` in a constants-only derived table; base returned `1`, the
  equivalent `8`, and adding one column projection to that derived table makes both sides agree.
