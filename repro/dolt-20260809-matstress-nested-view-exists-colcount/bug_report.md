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

# Dolt: nested multi-column VIEW + self `EXISTS` → ERROR 1105 column-count mismatch

## Summary

A view whose body is a **multi-column** projection over a derived table:

```sql
CREATE VIEW t1 AS
  SELECT c_pk, c1 FROM (SELECT c_pk, c1 FROM t__base) AS eq_ns_1;
```

accepts `SELECT *`, `IN`, and self-`JOIN`, but a self-referential `EXISTS`

```sql
SELECT * FROM t1 a WHERE EXISTS (SELECT 1 FROM t1 b WHERE b.c_pk = a.c_pk);
```

raises

```text
ERROR 1105: In definition of view, derived table or common table expression,
SELECT list and column names list have different column counts
```

even though the view definition has **no** explicit column-name list. A one-column nested view,
a flat (non-nested) multi-column view, a nested `CREATE TABLE AS`, and the same shape as an
inline derived table all behave correctly. Threshold is **≥ 2** output columns on the nested view.

Sibling of the correlated-subquery-over-view family (`dolt-run2-corr-subquery-split-view-field-index`)
but a **different** symptom and trigger: here the view is a plain identity nest (what eqgen's
`NestedSubqueryIdentityBuilder` emits), not a column-split/rejoin, and the error text is the
column-count mismatch rather than `unable to find field with index`.

## Environment

| | |
|---|---|
| Affected | Dolt `VERSION()` = `8.0.31`, `DOLT_VERSION()` = `2.2.3`, commit `a995f245c` (`v2.2.3-49-ga995f245c`, assertions off) |
| Session | `STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES,PIPES_AS_CONCAT`; utf8mb4 / utf8mb4_0900_bin — not load-bearing |
| Source | eqgen mat_stress hunt `dolt_matstress_20260809-235804`, `error_round4_0.sql` (262 hits of the same symptom in one round) |

## Minimal repro

See [`reduced.sql`](./reduced.sql). Core:

```sql
CREATE TABLE t__base (c_pk BIGINT NOT NULL, c1 BIGINT);
INSERT INTO t__base VALUES (1, 10), (2, 20);
CREATE VIEW t1 AS SELECT c_pk, c1 FROM (SELECT c_pk, c1 FROM t__base) AS eq_ns_1;

SELECT * FROM t1;
-- OK: 2 rows

SELECT * FROM t1 a WHERE EXISTS (SELECT 1 FROM t1 b WHERE b.c_pk = a.c_pk);
-- Expected: 2 rows
-- Actual:   ERROR 1105 column-count mismatch
```

## Expected vs actual

| Query over nested multi-col view `t1` | Expected | Actual |
|---|---|---|
| `SELECT * FROM t1` | 2 rows | 2 rows |
| `… WHERE EXISTS (SELECT 1 FROM t1 b WHERE b.c_pk = a.c_pk)` | 2 rows | **ERROR 1105** |
| `… WHERE a.c_pk IN (SELECT b.c_pk FROM t1 b)` | 2 rows | 2 rows |
| `… JOIN t1 b ON a.c_pk = b.c_pk` | 2 rows | 2 rows |
| same `EXISTS` over flat `CREATE VIEW t1 AS SELECT … FROM t__base` | 2 rows | 2 rows |
| same `EXISTS` over 1-column nested view | 1 col × N rows | OK |
| same `EXISTS` over nested `CREATE TABLE AS` (not VIEW) | 2 rows | 2 rows |
| inline derived (no VIEW) + `EXISTS` | 2 rows | 2 rows |

## Minimal oracle exposure path

- **Object composition arity:** `2`.
- **GCL builder path:** `NestedSubqueryIdentityBuilder → CreateViewBuilder`
  *(historical/inferred)*.
- **Confidence:** inferred from the historical emitted SQL and report terminology.
  `NestedSubqueryIdentityBuilder` is absent from the current factory; `CreateViewBuilder` remains an
  exact current registered class name.
- **Realization:** the identity-derived query is persisted as a multi-column view.
- **Workload/data requirements (excluded from arity):** self-correlated `EXISTS`, at least two projected
  columns, and the chosen correlation key are workload/schema conditions.
- **Exposure vs. intrinsic trigger:** the historical object pair creates the nested persisted view
  that exposes the bug. The intrinsic trigger additionally requires self-`EXISTS`; a plain select,
  `IN`, self-join, one-column nest, or non-view realization does not fail.

## Characterization

| Ingredient | Control that behaves correctly |
|---|---|
| persisted `VIEW` | nested `CREATE TABLE AS` → OK |
| body is a derived-table nest | flat `SELECT … FROM t__base` view → OK |
| ≥ 2 output columns | 1-column nested view → OK |
| self-referential `EXISTS` | `IN` / self-`JOIN` / plain `SELECT *` → OK |

Oracle gate on the original finding: base vs equivalent `t0`/`t1` are row- and type-identical;
only the workload query one-sides. Verdict: **real engine bug**.

## Open items

- Confirm against current Dolt tip / MySQL (should be fine on MySQL — column-count error is a
  planner assert over a well-formed view).
- File at https://github.com/dolthub/dolt/issues (or go-mysql-server).
