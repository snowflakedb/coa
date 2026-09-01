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

# TiDB: `MIN(GREATEST(<datetime string>, DATE)) GROUP BY` over a `(ROW_NUMBER() = 1)` filter view returns DATETIME instead of DATE

## Summary

`GREATEST('2016-05-04 10:10:10.100000', <DATE column>)` compared as DATE — the datetime literal is truncated to `'2016-05-04'` — on a heap table, an identity view, and a view that filters `WHERE rn = 1` with `rn` an integer `ROW_NUMBER()` alias. Wrapping the same table in a view whose body is

```sql
SELECT … FROM (
  SELECT …, (ROW_NUMBER() OVER (PARTITION BY id ORDER BY id) = 1) AS q FROM t
) x WHERE q
```

and then running `SELECT MIN(GREATEST('2016-05-04 10:10:10.100000', d)) FROM v GROUP BY id` returns **DATETIME** `'2016-05-04 10:10:10.100000'`. The two values are not equal. Ungrouped `MIN` / bare `GREATEST` on that same view are already DATE, so this is grouped type inference over the boolean-window filter, not a different `GREATEST` evaluation of the rows.

`information_schema` still reports `d` as `date` on both sides. The oracle's type-equivalence gate on `SELECT * FROM t` therefore passes; the result type of the aggregate does not.

## Environment

- **Version:** `8.0.11-TiDB-v9.0.0-beta.2.pre-2051-g3bea8196a5` commit `3bea8196a565ca01800b2d0807868f01139d8a30` (master, 2026-07-30), unistore, assertions off.
- **Binary:** `tidb-main/bin/tidb-server`
- **Session:** `sql_mode=STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES`; charset `utf8mb4`; collation `utf8mb4_0900_bin`. None of it is load-bearing.
- Access path: local `tidb-server` via pymysql. Deterministic.

## Minimal repro

See [`reduced.sql`](./reduced.sql) PART 2. Fresh database:

```sql
CREATE TABLE t (id BIGINT, d DATE);
INSERT INTO t VALUES (1, '1999-12-31');
CREATE VIEW v AS SELECT id, d FROM (
  SELECT id, d, (ROW_NUMBER() OVER (PARTITION BY id ORDER BY id) = 1) AS q FROM t
) x WHERE q;

SELECT MIN(GREATEST('2016-05-04 10:10:10.100000', d)) FROM v GROUP BY id;
-- heap / identity view / integer-rn view:  '2016-05-04'
-- this view:                                '2016-05-04 10:10:10.100000'
```

One row is enough. `HAVING` is not required.

## Expected vs actual

| Query | Expected (heap) | Actual (boolean `ROW_NUMBER()=1` view) |
|---|---|---|
| `MIN(GREATEST('2016-05-04 10:10:10.100000', d)) GROUP BY id` | `'2016-05-04'` | `'2016-05-04 10:10:10.100000'` |
| same, no `GROUP BY` | `'2016-05-04'` | `'2016-05-04'` |
| `GREATEST(...)` ungrouped | `'2016-05-04'` | `'2016-05-04'` |
| integer `ROW_NUMBER() AS rn WHERE rn = 1` | `'2016-05-04'` | `'2016-05-04'` |
| boolean projected, not filtered | `'2016-05-04'` | `'2016-05-04'` |
| identity `CREATE VIEW v AS SELECT id, d FROM t` | `'2016-05-04'` | `'2016-05-04'` |

`'2016-05-04' = '2016-05-04 10:10:10.100000'` is FALSE in TiDB (engine-equal gate failed).

**Which side is wrong:** the **equivalent / boolean-window view**. TiDB's `resolveType4Extremum` (`pkg/expression/builtin_compare.go`) compares a string against a DATE as DATE (`GLCmpStringAsDate`) and the heap path matches that: the datetime literal is truncated. The grouped aggregate over the boolean-window view picks the DATETIME signature instead. The eqgen equivalent is row-identical and type-identical on `c_date`; it is more *wrong* here, not more correct.

## Equivalence construction

**Round 12** (`mismatch_round12_3.sql`) and **round 108** (`mismatch_round108_0.sql`, same query, different seed/data): tag + `UNION ALL` + `DELETE`, `ROW_NUMBER` key table, duplicate-100% `UNION ALL`, then

```sql
CREATE VIEW t__base_view_2 AS
SELECT … FROM (
  SELECT …, ((ROW_NUMBER() OVER (PARTITION BY eq_key_1 ORDER BY eq_key_1)) = 1) AS eq_q
  FROM t__base_table_5
) AS eq_qsrc WHERE eq_q;
CREATE VIEW t AS SELECT c_pk, …, c_date, c_ts FROM t__base_view_2;
```

Only that last boolean `ROW_NUMBER() = 1` filter is load-bearing. Exposing `t__base`, `t__base_view_1`, `t__base_table_4`, or `t__base_table_5` as `t` keeps DATE. Exposing `t__base_view_2` already returns DATETIME. Replacing the whole chain with PART 2 still diverges.

Workload (inner derived of the original query):

```sql
SELECT MIN(GREATEST('2016-05-04 10:10:10.100000', t1.c_date))
FROM t AS t1
GROUP BY t1.c_big, t1.c_ts, t1.c_txt, t1.c_dbl, t1.c_chr
HAVING COUNT(*) > t1.c_big;
```

`HAVING` and the extra `GROUP BY` items drop out.

## Minimal oracle exposure path

- **Object composition arity:** `1`.
- **GCL builder path:** `CreateViewBuilder`.
- **Confidence:** Verified for the minimal object realization. The report does not preserve a current
  GCL query-builder class for the boolean `ROW_NUMBER() = 1` body, so only the realization-class
  mapping is exact.
- **Realization:** one stored view containing the boolean-window filter.
- **Workload/data requirements (excluded from arity):** grouped `MIN(GREATEST(…))`, the DATE/string
  type combination, and the singleton row are workload/data requirements and are not counted.
- **Exposure vs. intrinsic trigger:** this single view is intrinsic; the preceding tag, union,
  keying, and duplication chain only exposed it and is excluded from the minimal arity.

## Characterization

**Trigger (all three):**

1. A view (or derived table used as the scan) whose body filters on `(ROW_NUMBER() OVER (…) = 1)` — the comparison is a **boolean** in the inner SELECT list, referenced from `WHERE`.
2. `GREATEST(<datetime-looking string>, <DATE column>)` inside `MIN`.
3. `GROUP BY` (even a singleton group).

**Not sufficient / clean controls:** identity view; `ROW_NUMBER() AS rn WHERE rn = 1`; projecting the boolean without filtering on it; ungrouped `MIN` / `GREATEST`; `CREATE TABLE` copy of the DATE column.

`pkg/expression/builtin_compare.go` `resolveType4Extremum` still documents DATE vs DATETIME vs string mixing. The grouped path over this view shape appears to take `GLRetDatetime` / `GLCmpStringAsDatetime` while the heap takes `GLRetDate`.

## How it was found

eqgen data-equivalence oracle, hunt `tidb_rich_shuffle/tidb_20260816-012710`. 3 mismatches in the run: these two (same query) plus `mismatch_round116_0.sql` (window view × a different quantified/`REPEAT` query — not this bug). Replay: row-identical, type-identical on `t`, deterministic, engine-unequal.

## Open items

- Exact planner node that flips `GLRetDate` → `GLRetDatetime` (likely the boolean `ROW_NUMBER` filter being inlined into the aggregation).
- Whether `MAX`/`AVG` of the same `GREATEST` also flip; only `MIN` was tested.
- Round 116 of the same hunt is a separate remaining mismatch (identity `MAX() OVER (PARTITION BY col)` view empties a `REPEAT`/`CASE`/` < ALL` query; distilled `< ALL` + `LEFT JOIN` is clean). Not this bug.
