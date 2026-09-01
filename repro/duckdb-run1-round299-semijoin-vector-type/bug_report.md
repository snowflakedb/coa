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

# DuckDB v2.0: execution INTERNAL Error (Vector::Reference type mismatch) over a SEMI/ANTI join

## Summary

A query over a relation containing a `SEMI`/`ANTI JOIN`, self-joined on a `VARCHAR` comparison with a
`COALESCE` across both sides and at least one NULL, aborts execution with
`INTERNAL Error: Vector::Reference used on vector of different type (source BIGINT referenced VARCHAR)`.
**Regression:** released DuckDB 1.5.0 runs the same script to completion and returns the correct
(empty) result; v2.0 aborts on a vector-type invariant.

## Environment

- **DuckDB v2.0.0-alpha36050 (Cyanoptera)** `af1b4a9bd2` — the `main`/CLI build fuzzed by eqgen.
- Reproduces on the current `artifacts.duckdb.org/latest` CLI (verified).

## Minimal repro

See `reduced.sql`:

```sql
CREATE TABLE b (id BIGINT, name VARCHAR, created_at VARCHAR);
INSERT INTO b VALUES (-3, 'a', 'a'), (NULL, 'b', 'b');
CREATE TABLE empt AS SELECT * FROM b WHERE 1 = 0;
CREATE VIEW t AS SELECT id, name, created_at FROM b ANTI JOIN empt ON TRUE;

SELECT t5.created_at
FROM (SELECT 'YEAR' AS c0
      FROM t AS t1 INNER JOIN t AS t2 ON t1.created_at <= t2.created_at
      WHERE COALESCE(t1.id, t2.id) IS NOT NULL) AS sq4
INNER JOIN t AS t5 ON sq4.c0 = t5.created_at;
```

## Required ingredients (each verified individually in `reduced.sql`)

- the scanned relation contains a **SEMI or ANTI JOIN** (plain table/view run clean; SEMI and ANTI
  both reproduce; inline three-derived-table form reproduces too — not view-specific);
- a **self-join on a VARCHAR comparison** (`ON t1.created_at <= t2.created_at`; the BIGINT `id`
  version does NOT reproduce — matches the BIGINT/VARCHAR pair in the message; CROSS JOIN doesn't);
- a filter with **`COALESCE`/`IFNULL` across BOTH self-joined sides**;
- **at least one NULL** in the coalesced column (the trigger, not the row count);
- the derived table joined to the outer relation on its **constant column** (`sq4.c0 = t5.created_at`).

Not required: any window/QUALIFY/DISTINCT/CASE/BETWEEN, FULL/RIGHT OUTER, the CAST, extra columns.

## Regression / classification

v2.0-only wrong-abort; 1.5.0 returns the correct empty result. Execution-layer (vector type
invariant), sibling to the round109 binder regression.

## Minimal oracle exposure path

- **Object composition arity:** 2.
- **GCL builder path:** `DuckDBAntiJoinEmptyRoundTripBuilder` → `CreateViewBuilder`.
- **Confidence:** Exact against the reduced SQL and current GCL.
- **Realization:** the row-preserving `ANTI JOIN ... ON TRUE` query is exposed as a `VIEW`; the empty side may be supplied by a filtered relation and is not a separate forced realization in this path.
- **Workload/data requirements (excluded from arity):** a `VARCHAR` self-join, `COALESCE`/`IFNULL` across both sides with at least one `NULL`, and an outer join on the derived constant column.

**Exposure vs. intrinsic trigger:** The anti-join view contributes the SEMI/ANTI operator retained beneath the workload and is therefore part of the intrinsic failing plan. The self-join, cross-side null handling, constant-column join, and data values complete the execution trigger but are excluded from object arity.

## How it was found

eqgen differential fuzzer (row-multiset oracle); delta-reduced from a 6-relation, 2-window,
QUALIFY query + 8-statement equivalence chain. Original: hunt log.
