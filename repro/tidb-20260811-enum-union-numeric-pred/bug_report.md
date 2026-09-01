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

# TiDB: `ENUM`/`SET` numeric predicates wrong through `UNION ALL` (ordinal lost to varchar cast)

## Summary

`UNION ALL` of homogeneous `ENUM` (or `SET`) columns widens the result type to `VARCHAR` on both TiDB and MySQL. When a numeric predicate such as `e = 1` is pushed into a union branch, **MySQL 9.7.2 keeps native ENUM/SET ordinal comparison** (`Filter: (tb.e = 1)`), so `WHERE e = 1` still selects the first label.

**TiDB** rewrites the pushed predicate as

```text
eq(cast(cast(tb.e, varchar(...)), double BINARY), 1)
```

Every label string casts to `0.0`, so:

| Predicate on `UNION ALL` of ENUM | MySQL 9.7.2 | TiDB @3bea8196 |
|---|---|---|
| `WHERE e = 1` | 1 row (`'a'`) | **0 rows** |
| `WHERE e = 0` | 0 rows | **all labels** |

The same swap happens for `SET` (`s = 1` / `s = 0`). A plain (non-`UNION`) derived table keeps `ENUM` and matches MySQL. String predicates (`e = 'a'`) are unaffected.

## Environment

| | |
|---|---|
| Engine | `tidb 8.0.11-TiDB-v9.0.0-beta.2.pre-2051-g3bea8196a5` (unistore) |
| Compared to | MySQL 9.7.2 (docker) — correct ordinal `WHERE` via PPD |
| Access path | pymysql via eqgen `TiDbAdapter` |
| Session | `STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES` (not load-bearing) |
| Found by | manual base-vs-`UNION ALL` probing (eqgen hunt 2026-08-11) |

## Minimal repro

```sql
CREATE TABLE tb (e ENUM('a','b','c'));
INSERT INTO tb VALUES ('a'),('b'),('c');

SELECT e FROM tb WHERE e = 1;
-- ('a')

SELECT e FROM (
  SELECT e FROM tb
  UNION ALL
  SELECT e FROM tb WHERE FALSE
) AS x
WHERE e = 1;
-- MySQL: ('a') ; TiDB: empty

SELECT e FROM (
  SELECT e FROM tb
  UNION ALL
  SELECT e FROM tb WHERE FALSE
) AS x
WHERE e = 0;
-- MySQL: empty ; TiDB: ('a'),('b'),('c')
```

`SET` mirror:

```sql
CREATE TABLE sb (s SET('x','y','z'));
INSERT INTO sb VALUES ('x'),('y'),('x,y');
SELECT s FROM (SELECT s FROM sb UNION ALL SELECT s FROM sb WHERE FALSE) x WHERE s = 1;
-- MySQL: ('x') ; TiDB: empty
```

## Why this is wrong

- MySQL-compatible ENUM/SET numeric comparison uses the **member index / bitmap**, not the label string.
- After `UNION ALL`, both engines advertise `varchar` (`DESCRIBE`), and projection-side `e+0` is `0` on **both** (string→double). That part is shared.
- The divergence is in **predicate pushdown into the union branch**: MySQL compares the still-ENUM child column; TiDB inserts `ENUM→VARCHAR→DOUBLE` casts and evaluates the predicate as if every label were the number `0`.
- TiDB `EXPLAIN` for `SELECT e FROM v WHERE e = 1` shows the bad cast chain on `Selection` under `Union`; MySQL `EXPLAIN` shows `Filter: (tb.e = 1)` on the table scan under `Union all materialize`.

## Minimal oracle exposure path

- **Object composition arity:** `1`.
- **GCL builder path:** `UnionEmptyRoundTripBuilder[inline]`.
- **Confidence:** Inferred from the reduced `R UNION ALL (R WHERE FALSE)` SQL shape. This was a
  manual probe, not a finding with preserved GCL AST metadata, so the historical builder selection is
  not known.
- **Realization:** inline derived `UNION ALL`; no named view or table realization is required.
- **Workload/data requirements (excluded from arity):** the `ENUM`/`SET` declarations, label rows, and
  numeric predicate (`e = 1`/`0`) are type, data, and workload requirements and are not counted.
- **Exposure vs. intrinsic trigger:** the inline union is itself the intrinsic plan-shape trigger.
  This is a MySQL-compatibility probe, not a type-equivalent eqgen oracle hit.

## Oracle / admissibility notes

- Base table `SELECT *` and `UNION ALL` view `SELECT *` agree on **string** labels; `information_schema` types differ (`enum`/`set` vs `varchar`) because of union type aggregation.
- Still a **MySQL-compatibility wrong-result** on `WHERE` (and a planner defect: incorrect cast when pushing through `Union`), not an eqgen type-equivalent metamorphic hit.
- Not a duplicate of the known open TiDB eqgen repros (STDDEV/unsigned union, PPD nil plan, FD nil map, ANY_VALUE column, MOD paren restore, window+correlated ANY, #67648).

## Verify

```bash
cd /path/to/eqgen
```
