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

# MariaDB: two window functions over a merged identity `VIEW` make `GROUP BY id` raise 1052 (ambiguous column)

## Summary

A trivial `CREATE VIEW t AS SELECT * FROM b` is not transparent to MariaDB 11.4 when the query has **two window functions** and `GROUP BY` on **unprefixed** columns. The server raises

```
ERROR 1052 (23000): Column 'id' in GROUP BY is ambiguous
```

even though `t` has a single `id` and the `FROM` list names `t` once. The same statement succeeds on the base table, on `ALGORITHM=TEMPTABLE`, with one window, without `GROUP BY`, and with `GROUP BY t1.id, t1.name`. MySQL 9.7.2 accepts the identity-view form.

The hunt found this as `NATURAL LEFT OUTER JOIN` + two windows + `GROUP BY id, name` over a window/DISTINCT equivalent. The join is **not** load-bearing: `FROM t GROUP BY id, name` with two windows is enough. View merge of two windows appears to clone the underlying table so a one-relation `GROUP BY id` looks like two `id` columns.

## Environment

- **Engine**: MariaDB 11.4.12-MariaDB-ubu2404 (docker `mariadb:11.4`).
- **Session**: `sql_mode = STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES`;
  finding collation `utf8mb4_bin`. Not load-bearing.
- **Contrast**: MySQL 9.7.2, identical identity view + two windows + `GROUP BY id, name` — **3 rows, no error**.
- One-sided error finding (base succeeds, equivalent 1052). Determinism gate does not apply.

## Minimal repro

See [`reduced.sql`](./reduced.sql). Distilled core (PART 2; PART 1 is a separate database):

```sql
CREATE TABLE b (id BIGINT, name VARCHAR(255));
INSERT INTO b VALUES (NULL, NULL), (2, 'abc'), (2, 'o''brien');
CREATE VIEW t AS SELECT * FROM b;

SELECT MIN(name) OVER (PARTITION BY id), MIN(id) OVER (ORDER BY name)
FROM t
GROUP BY id, name;
-- ERROR 1052 (23000): Column 'id' in GROUP BY is ambiguous
```

## Expected vs actual

| Query | Expected | Actual (MariaDB 11.4.12) |
|---|---|---|
| distilled repro (identity `VIEW`) | 3 rows | **1052 `id` in GROUP BY is ambiguous** |
| `CREATE TABLE t AS SELECT * FROM b` | 3 rows | 3 rows |
| `CREATE ALGORITHM=TEMPTABLE VIEW t AS SELECT * FROM b` | 3 rows | 3 rows |
| `CREATE ALGORITHM=MERGE VIEW t AS SELECT * FROM b` | 3 rows | **1052** |
| `GROUP BY t1.id, t1.name` (qualify) | 3 rows | 3 rows |
| one window + `GROUP BY id, name` | 3 rows | 3 rows |
| two windows, no `GROUP BY` | 3 rows | 3 rows |
| `SELECT MIN(name), MIN(id) … GROUP BY id, name` (no windows) | 3 rows | 3 rows |
| two windows, same `PARTITION BY id` (no second `ORDER BY`) over `NATURAL JOIN` | 3 rows | 3 rows |
| MySQL 9.7.2 identity view, distilled query | 3 rows | 3 rows |

**Which side is wrong:** the **equivalent** (any merged view, including `SELECT * FROM b`). An identity view must accept every query the base table accepts. Ground truth is the table result and MySQL's result.

`EXPLAIN` of the identity-view query is not available: planning fails with 1052. The table plan is `SIMPLE t1 ALL, Using temporary; Using filesort`.

## Equivalence construction

### Concrete, as the builder emits it

The 14 errors share the same workload shape. Two representative equivalents:

- `error_round72_5.sql` — duplicate-and-reduce ending in
  `CREATE VIEW t AS SELECT c_pk, id, name, created_at FROM (SELECT DISTINCT eq_key_1, MAX(…) OVER (PARTITION BY eq_key_1) …)`.
- `error_round2_9.sql` — shorter chain ending in
  `CREATE VIEW t AS SELECT c_pk, id, name, created_at FROM t__base_view_1` where that view is `SELECT DISTINCT eq_key_1, c_pk, id, name, created_at`.

Both are row-identical to the base table. The workload (abridged) is

```sql
SELECT DISTINCT CAST(MIN(if(id IS NOT NULL, name, name)) OVER (PARTITION BY SHA2(name, '256'), id …) AS CHAR(255)),
       IFNULL(name, name),
       if(MIN(greatest(…)) OVER (ORDER BY …), CEIL(id), id),
       name, (SELECT COUNT(*) FROM t t4 CROSS JOIN t t5 CROSS JOIN t t6), …
FROM t AS t1 NATURAL LEFT OUTER JOIN t AS t2
GROUP BY id, name;
```

Two windows (`MIN(…) OVER (PARTITION BY SHA2…, id)` and `MIN(…) OVER (ORDER BY …)`) plus unprefixed `GROUP BY id, name`. Bisecting the SELECT list: **either window alone is fine; both together 1052** on an identity `MERGE` view. `NATURAL JOIN` is not required.

### Load-bearing composition

**Merged view × two window functions whose frames are not fused × `GROUP BY` unprefixed columns.** Removing any one fixes it (controls (a)–(g)).

### Reduced away

`NATURAL JOIN`, `SHA2`, scalar subqueries, `DISTINCT`, the duplicate-and-reduce / `MAX() OVER` chain (an identity view is enough), `created_at` / `c_pk`, and six of the original SELECT items.

## Minimal oracle exposure path

- **Object composition arity:** **2**
- **GCL builder path:** `SelectStarQueryBuilder` → `CreateViewBuilder`
- **Confidence:** Verified — the reduced identity `SELECT *` view matches these builders and the GCL implementations.
- **Realization:** `CreateViewBuilder` exposes the identity projection as the final mergeable `VIEW`.
- **Workload/data requirements (excluded from arity):**
  - Two window functions whose computations cannot be fused.
  - `GROUP BY` on unqualified columns of the merged view.

**Exposure vs. intrinsic trigger:** The two-builder path creates the oracle contrast by placing row-identical data behind a mergeable identity view. The intrinsic engine trigger is view merge combined with two non-fusible windows and unqualified grouping; the original joins and deeper builder chain are not object-composition requirements.

## Characterization

View merge is the mechanism: `TEMPTABLE` (no merge) is clean; explicit `ALGORITHM=MERGE` 1052s; qualifying `t1.id` is clean (the alias picks one of the cloned copies). Two windows with identical `PARTITION BY id` and no extra `ORDER BY` do **not** 1052 — those windows can be computed in one pass, so the clone/ambiguity does not appear. Two windows that cannot be fused (`PARTITION BY id` vs `ORDER BY name`) 1052, as does `MAX(name) OVER (ORDER BY name)` as the second window (`Column 'name' in ORDER BY is ambiguous`).

Not the already-filed [`mariadb-run2-round0-two-window-partition-const-sort-dropped`](../mariadb-run2-round0-two-window-partition-const-sort-dropped/) (wrong result from dropping the first window's sort on a **table**; no 1052, no view).

## How it was found

eqgen data-equivalence oracle, `mariadb_20260814-021538`. **All 14** `1052 Column 'id' in GROUP BY is ambiguous` errors in this run are this bug:

`error_round2_9`, `error_round17_5`, `error_round18_2`, `error_round33_5`, `error_round38_8`, `error_round43_6`, `error_round44_11`, `error_round46_1`, `error_round53_22`, `error_round54_8`, `error_round56_11`, `error_round58_10`, `error_round67_18`, `error_round72_5`.

A query-rewrite oracle over the base table never introduces a view, and the table plan is legal, so it cannot surface this.

## Open items

- Not bisected across 11.4.x / 11.8 / 12.x.
- Responsible merge/window rule not named; planning fails before `EXPLAIN`.
- Whether three windows, or two ranking windows (`ROW_NUMBER`), 1052 the same way was not exhaustively mapped — two aggregate windows with different frames are sufficient.
