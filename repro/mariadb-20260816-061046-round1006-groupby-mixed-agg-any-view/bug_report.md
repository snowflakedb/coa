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

# MariaDB: `GROUP BY` mixing an aggregate with the grouping column over a mergeable view returns zero rows when `WHERE` has `ANY`/`ALL` or a join-`IN`

## Summary

A `SELECT` that **mixes an aggregate with the grouping column in one expression** (`MIN(id)+id`, `CONCAT(MIN(txt), txt)`, `COUNT(*)+id`, …), `GROUP BY` that column, and a `WHERE` quantified subquery (`id >= ANY (SELECT id …)`) returns **zero rows** when the relation is a **mergeable view or derived table**. The same statement on the base table returns one row per group. `ALGORITHM=TEMPTABLE` and `derived_merge=off` (derived tables) restore the heap answer. MySQL 9.7.2 does not reproduce it.

This is not [MDEV-40557](https://jira.mariadb.org/browse/MDEV-40557) (window × `ANY`/`ALL` over a view, no `GROUP BY`). A window is not required here; a mixed aggregate/`GROUP BY` expression is.

## Environment

- **Version:** MariaDB `11.4.12-MariaDB-ubu2404` (Docker `mariadb:11.4`). Confirmed on that image via eqgen's adapter.
- **`sql_mode`:** `STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES` — not load-bearing.
- **charset / collation:** `utf8mb4` / `utf8mb4_nopad_bin`.
- **Access path:** server via pymysql (eqgen adapter) and the same SQL on the view vs heap. `EXPLAIN EXTENDED` / `SHOW WARNINGS` rewrite captured below.

## Minimal repro

See [`reduced.sql`](./reduced.sql) PART 2:

```sql
CREATE TABLE b (id BIGINT);
INSERT INTO b VALUES (1), (2);
CREATE VIEW t AS SELECT * FROM b;

SELECT MIN(id) + id
FROM t t1
WHERE t1.id >= ANY (SELECT t2.id FROM t t2)
GROUP BY t1.id;
-- heap: (2), (4). view: empty.
```

An inline derived table of `b` diverges the same way (no `VIEW` keyword). `id IN (SELECT id FROM t t2 JOIN t t3 ON t2.id = t3.id)` is an alternate `WHERE` that also fires.

## Expected vs actual

`id >= ANY (SELECT id)` is true for every non-NULL `id` in `{1,2}`. `MIN(id)+id` per group is `1+1=2` and `2+2=4`.

| Query | Expected | Actual (mergeable view / derived) |
|---|---|---|
| PART 2 `MIN(id)+id` + `>= ANY` + `GROUP BY` | 2 rows `(2),(4)` | **0 rows** |
| PART 2 derived table (no VIEW) | 2 rows | **0 rows** |
| PART 2 `CONCAT(MIN(txt), txt)` + `>= ANY` | 2 rows `('aa'),('bb')` | **0 rows** |
| PART 1 `CONCAT_WS(MIN(txt), …, txt)` on 1006_46 seed | 3 rows | **0 rows** |
| Heap / `ALGORITHM=TEMPTABLE` / `derived_merge=off` | 2 rows | 2 rows |
| MySQL 9.7.2, same SQL | 2 rows | 2 rows |

The **view / merged derived table is the wrong side**. The **base table is correct** (hand-evaluated groups; TEMPTABLE agrees; MySQL agrees).

## Equivalence construction

`mismatch_round1006_46.sql` (seed 186452534) ended equivalent `t` as a long row-preserving chain (`UNION ALL` / tag-split / `INTERSECT ALL` / empty CTE) whose last step is `CREATE VIEW t AS SELECT … FROM t__base_table_26`. The workload was

```sql
SELECT REGEXP_SUBSTR(CONCAT_WS(',', MIN(c_txt), 'YEAR', c_txt, 'HOUR'), '[0-9]+')
FROM t WHERE c_txt >= ANY (SELECT c_txt FROM t WHERE …) GROUP BY c_txt
```

`REGEXP_SUBSTR` and the `CASE` in the subquery are not required. **`CREATE VIEW t AS SELECT * FROM b` already diverges.**

- **Load-bearing construct:** merge of a view or derived table (MariaDB `ALGORITHM=MERGE` / `derived_merge=on`).
- **Composition:** that merge **×** `GROUP BY col` **×** a SELECT expression that mixes `MIN`/`MAX`/`SUM`/`AVG`/`COUNT(*)` with `col` **×** `WHERE col {>=,<} ANY/ALL (SELECT col)` or `col IN (SELECT col FROM t JOIN t …)`.
- **Reduced away:** JSON, CAST, UNION ALL, extra SELECT items, `REGEXP_SUBSTR`, `CONCAT_WS` separators, the original `CASE` in the subquery, HAVING, windows, extra columns/rows (1 row still diverges).

`mismatch_round115_1.sql` looked like a different `IN (join)` bug until the same mixed `MIN(c_int)+c_big` + `>= ANY` + `GROUP BY` fired on that seed. Join-`IN` is an alternate subquery shape, not a second root.

## Minimal oracle exposure path

- **Object composition arity:** **2**
- **GCL builder path:** `SelectStarQueryBuilder` → `CreateViewBuilder`
- **Confidence:** Verified — the reduced identity view is the direct output of these registered GCL builders.
- **Realization:** `CreateViewBuilder` exposes `SELECT *` as the final mergeable `VIEW`.
- **Workload/data requirements (excluded from arity):**
  - `GROUP BY` on the relevant column.
  - One SELECT expression mixing an aggregate with that grouping column.
  - A documented `ANY`/`ALL` or join-`IN` predicate in `WHERE`.

**Exposure vs. intrinsic trigger:** The identity-view path is sufficient to expose the defect; the long original row-preserving chain is not. The intrinsic trigger is view/derived merge interacting with the mixed aggregate/grouping expression and quantified or join-`IN` predicate.

## Characterization

Verified against `mariadb:11.4.12`.

**Required**

1. Mergeable view or derived table. Identity `SELECT *` is enough. `ALGORITHM=TEMPTABLE` masks. For derived tables, `SET optimizer_switch='derived_merge=off'` masks (2 rows). `derived_merge=off` on a *VIEW* does not (MariaDB applies that switch to derived tables, not to view merge).
2. `GROUP BY` the column used in the mixed expression.
3. One SELECT expression combining an aggregate with that grouping column. `MIN(id)+id`, `MIN(id)*id`, `MAX(id)+id`, `SUM(id)+id`, `AVG(id)+id`, `COUNT(*)+id`, `CONCAT(MIN(txt), txt)` all fire. `MIN(id)` alone, `CONCAT(MIN(txt), 'x')`, and `SELECT MIN(id), id` (two items) do **not**.
4. `WHERE col >= ANY (SELECT col)`, `col < ANY`, or `col >= ALL` (non-empty). Alternate: `col IN (SELECT col FROM t JOIN t …)` (self-join or cross-join inside the `IN` subquery). `= ANY` and `EXISTS` do **not**. Plain `IN (SELECT col FROM t)` does **not** on an identity view; it **does** on PART 3's padded `WHERE tag=1` view (203_11).

**Not required / not the bug**

- A window function (that is [MDEV-40557](https://jira.mariadb.org/browse/MDEV-40557) / `mariadb-run1-round45-window-anyall-subquery-view`).
- HAVING. `mismatch_round112_25.sql` still diverges after dropping HAVING; `SUM(c_int)+c_dec >= ANY … GROUP BY c_dec` is enough.
- `in_to_exists`, `semijoin`, `materialization`, `condition_pushdown_for_derived` — turning each off does not mask.
- ≥2 rows. A 1-row table still diverges (unlike MDEV-40557).

**`EXPLAIN EXTENDED` / `SHOW WARNINGS` rewrite** of the distilled view query (11.4.12):

```
select min(`b`.`id`) + `b`.`id`
from `b`
where <nop>(<in_optimizer>(`b`.`id`,
      (select min(`b`.`id`) from `b`) <= <cache>(`b`.`id`)))
group by `b`.`id`
```

So `id >= ANY (SELECT id)` is rewritten to `MIN(id) <= id`, which is true for every non-NULL `id`. The rewrite looks semantically correct; the empty result appears only when that rewritten predicate is planned together with the mixed `MIN(id)+id` after view merge. `EXPLAIN` of heap vs view is the same shape (`PRIMARY` table scan + `SUBQUERY` scan, `Using where; Using temporary; Using filesort`); the view's table is `b` (already merged).

`SELECT id FROM t WHERE id >= ANY (SELECT id FROM t)` on the view is **correct** (2 rows). DML that used only that predicate would not hit extra/missing rows. The mixed SELECT list is what empties the groups.

## How it was found

Eqgen data-equivalence oracle, `mariadb_rich_shuffle2` / `mariadb_20260816-061046`. After stripping the already-filed window+`ANY`/`ALL`/`NOT IN` cluster ([MDEV-40557](https://jira.mariadb.org/browse/MDEV-40557), 7791 files) and the two-window constant-partition cluster, leftover unique queries whose **original SQL already diverges on an identity view** collapse onto this mixed-`GROUP BY` shape:

| Finding | Original fragment | Same root |
|---|---|---|
| `mismatch_round1006_46.sql` | `CONCAT_WS(MIN(txt), …, txt)` + `>= ANY` + `GROUP BY txt` | yes |
| `mismatch_round1006_14.sql` | `LEFT(txt, MIN(int))` + `!= ANY` + `GROUP BY txt` | yes |
| `mismatch_round115_1.sql` / round3_7 | `MIN(int)+c_big` + `IN (join)` + `GROUP BY c_big` | yes (`ANY` also fires on that seed) |
| `mismatch_round112_25.sql` | `GREATEST(dec, SUM(…))` + join/`ANY` + `GROUP BY dec` | yes (HAVING not required) |
| `mismatch_round203_11.sql` | `NULLIF(COUNT(*), c_dec)` + simple `IN` + `GROUP BY`. Identity view does **not** fire; PART 3 padded `WHERE tag=1` view does | yes |

Ten unique `ANY`/`ALL`-no-window leftovers, seven unique `IN` leftovers, and three unique `HAVING` leftovers identity-diverge and also fire the distilled `MIN(c_int)+c_int >= ANY GROUP BY c_int` on their own seed.

Gates: identity view is row-identical to the heap; types match; result is deterministic empty vs two groups (not PAD SPACE / order).

## Open items

- Optimizer rule / `file:line` not named. Starting point: view/derived merge + `in_optimizer` rewrite of `ANY` to `MIN(subquery) <= col`, then item equalization of the mixed `Item_sum` + grouping field so the filter becomes `<nop>`/FALSE.
- Join-`IN` vs `ANY` may share that `in_optimizer` path; not proven they are one C++ site.
- Eqgen leftovers whose identity view matches the heap because it preserves INSERT order, but the original equivalent reorders rows, are DISTINCT-after-GROUP-BY insert-order bugs ([`round831-distinct-space-avg-having-insert-order`](../mariadb-20260816-061046-round831-distinct-space-avg-having-insert-order/), [MDEV-9445](https://jira.mariadb.org/browse/MDEV-9445)), not this view-merge × `ANY` bug. `mismatch_round1006_17.sql` ORIG is that class; that seed’s identity view still hits **this** `MIN+id` × `ANY` bug.
