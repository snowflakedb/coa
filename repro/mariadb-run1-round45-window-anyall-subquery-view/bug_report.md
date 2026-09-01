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

# MariaDB: window function + quantified (`ANY`/`ALL`) subquery predicate over a view returns missing rows

## Summary

A `SELECT` that combines an **aggregate window function** with a **quantified subquery predicate**
(`col < ANY (…)`, `col >= ALL (…)`) in the `WHERE` returns **wrong (missing, often zero) rows** when
the queried relation is a mergeable **view** rather than the base table. The two relations are
row-identical and the query is permutation-invariant on the base table, so this is neither a data
difference nor order-sensitivity — it is an optimizer defect in how the quantified-subquery predicate
is evaluated once the view is merged, in the presence of a window function.

Two fuzz findings reduce to this one bug: `mismatch_round45_0.sql` (`< ANY`) and
`mismatch_round38_0.sql` (`>= ALL`).

## Environment

- **Version:** `13.1.0-MariaDB-debug`, source revision `cded2b25e65853a75c2213cfe0832819832708bd` (main, assertions on)
- `sql_mode`/charset/collation: harness defaults (`utf8mb4` / `utf8mb4_nopad_bin`); immaterial to the result.

## Minimal repro

```sql
CREATE TABLE b (id BIGINT);
INSERT INTO b VALUES (0),(1);
CREATE VIEW t AS SELECT id FROM b;

SELECT AVG(id) OVER () FROM b WHERE id < ANY (SELECT id FROM b);  -- 1 row  (correct)
SELECT AVG(id) OVER () FROM t WHERE id < ANY (SELECT id FROM t);  -- 0 rows (WRONG)
```

## Expected vs actual

| Query | Expected | Actual |
|---|---|---|
| `… FROM b WHERE id < ANY (SELECT id FROM b)` (base table) | 1 row | 1 row |
| `… FROM t WHERE id < ANY (SELECT id FROM t)` (view) | 1 row | **0 rows** |
| `… FROM t WHERE id >= ALL (SELECT id FROM t)` (view) | 1 row | **0 rows** |

## Equivalence construction

The original equivalents differed (round45 = a `ROW_NUMBER()`-filter view; round38 = a predicate-split
`UNION ALL` CTAS chain), but the trigger is neither of those constructs: **a trivial
`CREATE VIEW t AS SELECT * FROM base` already diverges** for both. So the load-bearing construct is
simply **view materialization/merge** (any mergeable view), composed with a query that has a window
function and a quantified-subquery predicate.

- **Load-bearing construct:** a mergeable **view** over the base table (the row-preserving
  view/derived-table round-trip). The `ROW_NUMBER()` chain, the `UNION ALL` splits, the self-joins,
  `GROUP BY`, and all other SELECT expressions were reduced away.
- **Composition (the actual bug):** view **×** aggregate window (`AVG/SUM/MAX OVER ()`) **×** a
  quantified subquery predicate (`< ANY` / `>= ALL`). Removing any one removes the divergence.

## Minimal oracle exposure path

- **Object composition arity:** **2**
- **GCL builder path:** `SelectStarQueryBuilder` → `CreateViewBuilder`
- **Confidence:** Verified — the report reduces both findings to a trivial identity `SELECT *` view represented directly by these GCL builders.
- **Realization:** `CreateViewBuilder` exposes the identity projection as the final mergeable `VIEW`.
- **Workload/data requirements (excluded from arity):**
  - An aggregate window such as `AVG`, `SUM`, or `MAX ... OVER`.
  - A documented quantified, anti-semijoin, or compared-`IN` predicate.
  - At least two rows for the reduced missing-row case.

**Exposure vs. intrinsic trigger:** The identity view is the complete object-side oracle contrast; the original row-number and union chains are reduced away. The intrinsic trigger is view merge combined with the aggregate window and qualifying subquery predicate.

## Characterization (verified against the build)

- **Window is required and must be an aggregate window:** `AVG/SUM/MAX … OVER ()` reproduce;
  `COUNT(*) OVER ()`, `ROW_NUMBER() OVER ()`, and a plain aggregate with `GROUP BY` (no window) do not.
- **The quantified subquery predicate is required:** `< ANY`, `> ANY`, `>= ALL`, `<= ALL` reproduce;
  the scalar-equivalent `id < (SELECT MAX(id) FROM t)`, and `IN (subquery)` / `= ANY`, do not.
  **`NOT IN (subquery)` does reproduce** (eqgen `mariadb_20260816-061046`: heap `AVG(id) OVER () … WHERE id NOT IN (SELECT id+1 FROM t)` → 1 row, same statement over the identity view → 0 rows). That is the same missing-row symptom via the anti-semijoin path; `IN` staying correct is unchanged.
  **`(col IN (SELECT …)) <= (col2 IN (SELECT …))` does reproduce** (eqgen `mismatch_round1003_1.sql` and a 2-row distilled case: heap 2 rows, identity view 0). Plain `col IN (SELECT col)` still does not. The predicate can be a comparison of two `IN` booleans, not only `ANY`/`ALL`/`NOT IN`.
- **View vs base table:** the base table returns the rows; the view returns fewer/zero. `GROUP BY`
  and self-joins are not needed; ≥2 rows are needed.
- Deterministic across runs; permutation-invariant on the base (not order-sensitivity); base `t` and
  the equivalent `t` are row-identical (passes oracle admissibility).

## How it was found

The eqgen differential fuzzer (equivalence oracle) ran a workload query against a base table and a
row-identical view rewrite; the view returned fewer rows. Reduced from a 10-expression aggregate +
window + self-join + quantified-subquery query (round45) / a similar query with `>= ALL` (round38)
down to the 3-line case above.

Eqgen `mariadb_rich_shuffle2` / `mariadb_20260816-061046` hit the same bug at scale: **7791 / 13259**
`mismatch_*.sql` files are window + (`ANY`/`ALL` or `NOT IN`) over a view. `NOT IN` was confirmed on
MariaDB 11.4.12 (`mariadb:11.4`) with the distilled `AVG(id) OVER () … NOT IN (SELECT id+1 …)` case
(heap 1 row, identity view 0 rows). `IN` still does not trigger.

- Original findings: hunt log, hunt log
- Reduced repro: `reduced.sql` (this folder)
- Fuzzer seeds: 1400813873 (round45), 333914030 (round38)
