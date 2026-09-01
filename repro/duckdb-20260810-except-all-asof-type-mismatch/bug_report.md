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

# DuckDB: `ASOF` join whose left child is `EXCEPT ALL` / `INTERSECT ALL` raises an INTERNAL type-mismatch (`ExpressionExecutor` / `Vector::Reference`)

## Summary

An `ASOF LEFT JOIN` (and `ASOF JOIN`) whose **left** input is an `EXCEPT ALL` or
`INTERSECT ALL` set-operation aborts at execution with

```
INTERNAL Error: ExpressionExecutor::Execute called with a result vector of type INTEGER
that does not match expression type BIGINT
```

On wider schemas the same plan also surfaces as

```
INTERNAL Error: Vector::Reference used on vector of different type
(source DECIMAL(38,0) referenced BIGINT)
```

`EXCEPT` without `ALL`, `UNION ALL`, materializing the set-op into a table first, and
replacing `ASOF` with a plain `LEFT`/`SEMI`/`ANTI` join are all clean. The failure is
therefore specific to **bag set-ops feeding the left side of `ASOF`**.

## Environment

| | |
|---|---|
| Engine | DuckDB CLI, in-memory, defaults |
| Version (finding) | `v2.0.0-alpha37247 (Cyanoptera) e500d77864` |
| Also fails on | `v2.0.0-alpha37080`, `v1.6.0-dev12322`, `v1.5.5 (Variegata) d8cdaa3` |
| Does **not** fail on | `v1.0.0 1f98600` |
| Session settings | none |

## Minimal repro

```sql
CREATE TABLE a(d INTEGER);
INSERT INTO a VALUES (1), (2);
CREATE TABLE b(d INTEGER);
CREATE TABLE r AS SELECT * FROM a;

SELECT * FROM (SELECT * FROM a EXCEPT ALL SELECT * FROM b) t0
ASOF LEFT JOIN r t1 ON t0.d >= t1.d;
```

```
INTERNAL Error: ExpressionExecutor::Execute called with a result vector of type INTEGER
that does not match expression type BIGINT
```

Same assertion with `INTERSECT ALL`:

```sql
SELECT * FROM (SELECT * FROM a INTERSECT ALL SELECT * FROM a) t0
ASOF LEFT JOIN r t1 ON t0.d >= t1.d;
```

## Expected vs actual

| Query | Expected | Actual |
|---|---|---|
| `… (a EXCEPT ALL b) ASOF LEFT JOIN r ON d >= d` | 2 rows | INTERNAL Error (INTEGER≠BIGINT) |
| `… (a INTERSECT ALL a) ASOF LEFT JOIN r …` | 2 rows | same INTERNAL |
| `… (a EXCEPT b) ASOF LEFT JOIN r …` (no `ALL`) | 2 rows | 2 rows — clean |
| `CREATE TABLE t0 AS SELECT * FROM a EXCEPT ALL …; t0 ASOF …` | 2 rows | 2 rows — clean |
| same left child, `LEFT` / `SEMI` / `ANTI` instead of `ASOF` | matching rows | clean |

## Equivalence construction

Found by eqgen (corpus hunt `duckdb_hunt4_…/corpus`, `error_round37_1.sql`, seed
`1922960036`) on DuckDB `e500d77864`.

Three fork equivalents `t0`/`t1`/`t2` were built from one hidden base. `t0` ended as

```sql
CREATE VIEW t0 AS
  SELECT * FROM t__base_view_11
  EXCEPT ALL
  SELECT * FROM t__base_view_42;
```

The workload was

```sql
SELECT * FROM t0 ASOF LEFT JOIN t1 ON t0.c_date >= t1.c_date;
```

- **Base** side: ASOF succeeds.
- **Equivalent** side: INTERNAL (`DECIMAL(38,0)` referenced `BIGINT`).
- Oracle admissibility: `t0`/`t1`/`t2` row-identical across sides.

Delta reduction showed only the `EXCEPT ALL` left child of `ASOF` is load-bearing; the
long view chain collapses to the four-statement repro above.

## Minimal oracle exposure path

- **Object composition arity:** 3.
- **GCL builder path:** `DuckDBExceptAllEmptyTableRoundTripBuilder` [empty `TABLE` realization] → `CreateViewBuilder`.
- **Confidence:** Exact against the report SQL and current GCL.
- **Realization:** the bag set-op transform forces its empty right input through CTAS, and the resulting `EXCEPT ALL` query is exposed as a `VIEW`.
- **Workload/data requirements (excluded from arity):** `ASOF` (inner or left) must consume a non-empty `EXCEPT ALL`/`INTERSECT ALL` result; a catalog-empty set-op input is needed so optimization does not erase it.

**Exposure vs. intrinsic trigger:** The object path supplies the bag set-op as an unmaterialized child of `ASOF`; the intrinsic engine trigger is that plan shape, not the preceding relation chain. Materializing the set-op result before `ASOF` removes the trigger.

## Required ingredients

1. **Left *or right* child of `ASOF` is `EXCEPT ALL` or `INTERSECT ALL`** (bag set-op).
   Plain `EXCEPT` / `UNION ALL` / a materialized table of the same rows do not reproduce.
2. **`ASOF` (LEFT or INNER)**. Plain `LEFT`/`SEMI`/`ANTI`, and `ASOF SEMI`/`ASOF ANTI`,
   over the same set-op are clean.
3. **Non-empty set-op result**. Empty `EXCEPT ALL` returns 0 rows cleanly.
4. **Empty table as the `EXCEPT ALL` right child, not `WHERE FALSE`.** The optimizer
   erases `(a EXCEPT ALL SELECT * FROM a WHERE FALSE)` before `ASOF`, so that form is
   clean; a catalog-empty `b` keeps the bag set-op and reproduces.
5. Column types are irrelevant for triggering: `INTEGER`, `BIGINT`, `DATE`, `ENUM`, and
   the rich eqgen schema all fail (error text varies).

## Non-duplicates

| Issue | Why different |
|---|---|
| Filed eqgen `semijoin-vector-type` (`Vector::Reference` BIGINT/VARCHAR on SEMI/ANTI + COALESCE) | Fixed on this binary; different join family and trigger |
| Filed eqgen `antijoin-filter-pushdown` (index-within-vector on ANTI `ON TRUE` + filter pushdown) | Fixed on this binary; no ASOF / set-op |
| GitHub `#15444` / `#15584` / `#17335` / `#18833` (`Vector::Reference` in other join/UPSERT shapes) | No `EXCEPT ALL`/`INTERSECT ALL` left of `ASOF` |

No open DuckDB issue was found that pairs bag set-ops with `ASOF`.

## Source finding

`duckdb_hunt4_20260810-173314/corpus/duckdb_20260810-173341/error_round37_1.sql`
