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

# MariaDB: `DISTINCT` + `char_length(SPACE(AVG(…)))` + `HAVING` returns empty for one insertion order of the same rows

## Summary

A grouped `SELECT DISTINCT c_int, char_length(SPACE(AVG(CAST(c_int AS SIGNED)))) … HAVING AVG(c_big) <= c_int` is **not permutation-invariant** on MariaDB 11.4.12. The same 8 rows yield **2 rows** (groups `c_int=0` and `c_int=2`, the SQL-correct answer) or **0 rows** depending only on `INSERT` order. MySQL 9.7.2 returns 2 rows for both orders. Dropping `DISTINCT`, replacing `char_length(SPACE(AVG(…)))` with `AVG(c_int)`, or adding `PRIMARY KEY (c_pk)` restores the 2-row answer on MariaDB.

This is not a PAD SPACE / `ANY_VALUE` comparability artefact: `HAVING AVG(c_big) <= c_int` is determined by the group values, which do not change with insertion order (confirmed: the same `GROUP BY` without `DISTINCT`+`char_length` is stable). The engine is dropping every group in one physical order.

## Environment

- **Version:** MariaDB `11.4.12-MariaDB-ubu2404` (Docker `mariadb:11.4`).
- **`sql_mode`:** `STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES` — not load-bearing.
- **charset / collation:** `utf8mb4` / `utf8mb4_nopad_bin`.
- **Contrast:** MySQL 9.7.2 — 2 rows for both INSERT orders.

## Minimal repro

See [`reduced.sql`](./reduced.sql). Two sessions, identical `CREATE TABLE` and values, opposite `INSERT` order.

Even pks first (WRONG):

```sql
-- INSERTs for pks 2,4,6,8 then 1,3,5,7  (see reduced.sql PART 1)
SELECT DISTINCT t1.c_int,
       char_length(SPACE(AVG(CAST(t1.c_int AS SIGNED))))
FROM t t1
WHERE t1.c_int IS NOT NULL
GROUP BY t1.c_int, t1.c_ts, t1.c_dec, t1.c_big, t1.c_dbl
HAVING AVG(t1.c_big) <= t1.c_int;
-- Expected 2 rows. Actual 0 rows.
```

Odd pks first (CORRECT): 2 rows `(0,0), (2,2)`.

## Expected vs actual

Hand-evaluated groups with `c_int IS NOT NULL`:

| c_int | AVG(c_big) | HAVING `AVG(c_big) <= c_int` |
|---|---|---|
| -7 | 2.0 | 2 <= -7? no |
| 0 | 0.0 | 0 <= 0? **yes** |
| 2 | -1.0 | -1 <= 2? **yes** (two groups, different `c_dec`) |
| 42 | NULL | unknown, excluded |

`SPACE(AVG(c_int))` is `SPACE(0)` → `''` (char_length 0) and `SPACE(2)` → two spaces (char_length 2). Distinct pairs: `(0,0), (2,2)`.

| Query | Expected | Actual |
|---|---|---|
| PART 1 even-then-odd | 2 rows | **0 rows** |
| PART 2 odd-then-even | 2 rows | 2 rows |
| PART 1 + `PRIMARY KEY (c_pk)` | 2 rows | 2 rows |
| PART 1 drop `DISTINCT` | 3 rows (two `c_int=2`) | 3 rows |
| PART 1 `DISTINCT` + `AVG(c_int)` (no `char_length`/`SPACE`) | 2 rows | 2 rows |
| MySQL 9.7.2, either order | 2 rows | 2 rows |

The **even-then-odd MariaDB result is wrong**. Odd-then-even, PK, no-`DISTINCT`, and MySQL agree.

## Equivalence construction

`mismatch_round831_44.sql` (seed 149000898) ended as `CREATE VIEW t AS SELECT … FROM t__base_table_32` where `table_32` is `SELECT * FROM t__base_view_2 UNION ALL SELECT * FROM t__base_view_23`. Those views hold 4 rows each (even pks vs odd pks). `SELECT *` from the equivalent is row-identical to the heap (oracle admissibility **passes**). The UNION ALL **materialises even pks first**, which is the WRONG insertion order.

An identity `CREATE VIEW t AS SELECT * FROM b` does **not** diverge (it preserves heap insert order). `CREATE TABLE t AS SELECT * FROM b UNION ALL SELECT * FROM b WHERE 0` does not. Reversing the heap `INSERT`s is enough — no view is required.

- **Load-bearing construct:** none of the builders. This is **query × physical row order** (the UNION ALL tag-split only *revealed* it by changing insert order).
- **Composition:** `DISTINCT` **×** `char_length(SPACE(AVG(…)))` (or `char_length(AVG(…))`) **×** `HAVING AVG(c_big) <= c_int` **×** a multi-column `GROUP BY` **×** InnoDB table without a PRIMARY KEY **×** one of the two 4+4 insertion orders.
- **Reduced away:** the view, the UNION ALL, JSON, CAST, every other builder.

`mismatch_round256_50.sql` is the same class: identity matches; reversing the 8 `INSERT`s on a plain table turns 1 row into 0. Its SELECT is `DISTINCT CAST(MIN(c_int+c_big) AS TIME) … GROUP BY CONCAT(…)`.

## Minimal oracle exposure path

- **Object composition arity:** **2**
- **GCL builder path:** `PartitionUnionQueryBuilder` → `CreateTableBuilder` (**inferred mapping**)
- **Confidence:** Inferred — the report preserves the emitted partitioned `UNION ALL` and final table SQL, but not the historical GCL AST builder selections.
- **Realization:** `CreateTableBuilder` materializes the partition-union output, making its even-then-odd row order physical.
- **Workload/data requirements (excluded from arity):**
  - `DISTINCT` plus `char_length` of an aggregate, the wide `GROUP BY`, and the documented aggregate `HAVING`.
  - An InnoDB table without a primary key.
  - The exposing physical row order; reversing it or adding a primary key masks the result.

**Exposure vs. intrinsic trigger:** The inferred `PartitionUnionQueryBuilder` → `CreateTableBuilder` path is only the oracle exposure mechanism: it reorders the same rows and materializes that order. The intrinsic bug is **query × physical row order**, and it reproduces with reordered plain `INSERT`s after every equivalence builder has been removed; no builder is intrinsically load-bearing.

## Characterization

Verified against `mariadb:11.4.12`.

**Required**

1. `DISTINCT` in the SELECT list together with `char_length(…)` of an aggregate (`SPACE(AVG(c_int))` or `AVG(c_int)` itself). `REPEAT(' ', AVG(…))` also fires.
2. The wide `GROUP BY c_int, c_ts, c_dec, c_big, c_dbl` plus `HAVING AVG(c_big) <= c_int` from the finding. `GROUP BY c_int` alone + `HAVING` is stable.
3. Even-pk rows inserted before odd-pk rows (or the equivalent UNION ALL order). The other order is correct.
4. No `PRIMARY KEY`. Adding `PRIMARY KEY (c_pk)` masks it.

**Not required**

- A view or derived table.
- `CAST AS SIGNED` (`SPACE(AVG(c_int))` without CAST still fires).
- ≥2 engines: MySQL does not reproduce.

`EXPLAIN` of the empty result is still `ALL` + `Using where; Using temporary; Using filesort` (same shape as the correct order). The defect is in how the temporary table for `DISTINCT` is filled after `HAVING`, not a scan-type change.

## How it was found

Eqgen data-equivalence oracle, `mariadb_rich_shuffle2` / `mariadb_20260816-061046`. Identity view matched the heap (same INSERT order). The original equivalent reordered rows via UNION ALL of two 4-row views; replay_adapter still reports row-identical `SELECT *` and a 2-vs-0 query diff. Bisecting `table_32 = view2 UNION ALL view23` showed view2 alone is correct, view23 alone has no HAVING survivors (correct for that subset), and concatenating them in view2-first order zeros the result. Replaying the 8 values as plain `INSERT`s in that order is sufficient.

Gates: row-identical equivalent, type-identical, each *single* order is deterministic — the two orders disagree with each other, which SQL does not allow for this query.

## Open items

- Optimizer rule / `file:line` not named. Starting point: end_select / DISTINCT tmp table after filesort, interacting with `Item_func_space` / `Item_func_char_length` of an `Item_sum`. `PRIMARY KEY` changing the access path is the mask for this HAVING/SPACE shape.
- Several leftover seeds also fire this 831 query (emptiness 0 vs 2) as well as the MDEV-9445-shaped stitch.
