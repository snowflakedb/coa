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

# Dolt / go-mysql-server: `CEIL`/`FLOOR` of a windowed `DECIMAL` mutate the shared `*apd.Decimal` and overwrite later peers

## Summary

`CEIL(d)` and `FLOOR(d)` on a `DECIMAL` call `sql.DecimalCtx.Ceil(num, num)` / `Floor(num, num)`, which **mutate `num` in place**. Window aggregates (`MAX(d) OVER (PARTITION BY d)`, also `MAX() OVER ()` / `ORDER BY`) hand every peer in the partition the **same `*apd.Decimal` pointer**. Projecting the window column next to `CEIL`/`FLOOR` therefore turns `d` into the rounded value for every row after the first peer.

`ROUND` does not; `BIGINT`/`DOUBLE` windows do not; a one-row partition does not; `SELECT *` without `CEIL` does not. `index_builder.go` in the same module already copies into a fresh `*apd.Decimal` before `Ceil` — the scalar function does not.

The right tracker is **dolthub/go-mysql-server** (Dolt just vendors it).

## Environment

- **Dolt:** `VERSION()` = `8.0.31`; `dolt version` `2.2.3`; source `v2.2.3-49-ga995f245c`, commit `a995f245c032bc412aed308194d81ee12bc74f19`, assertions off.
- **go-mysql-server:** `v0.20.1-0.20260805191915-e5eafe0da809`
- **Session:** `sql_mode=STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES,PIPES_AS_CONCAT`; `utf8mb4` / `utf8mb4_0900_bin`. Not load-bearing.
- Access path: local `dolt sql-server` via pymysql. Deterministic. A view is not required (inline derived table reproduces).

## Minimal repro

See [`reduced.sql`](./reduced.sql) PART 2:

```sql
CREATE TABLE t (id BIGINT, d DECIMAL(10,2));
INSERT INTO t VALUES (1, 12.34), (2, 12.34);

SELECT id, d, CEIL(d)
FROM (SELECT id, MAX(d) OVER (PARTITION BY d) AS d FROM t) s
ORDER BY id;
```

## Expected vs actual

| Query | Expected | Actual |
|---|---|---|
| windowed `SELECT id, d, CEIL(d)` (two peers, `d=12.34`) | `(1, 12.34, 13), (2, 12.34, 13)` | `(1, 12.34, 13), (2, 13.00, 13)` |
| same with `FLOOR` | `(1, 12.34, 12), (2, 12.34, 12)` | `(1, 12.34, 12), (2, 12.00, 12)` |
| `ROUND(d)` | both rows `12.34` / `12.00` | both rows `12.34` / `12.00` ✓ |
| `SELECT *` from the window (no `CEIL`) | both `12.34` | both `12.34` ✓ |
| one-row partition + `CEIL` | `(1, 12.34, 13)` | `(1, 12.34, 13)` ✓ |
| `BIGINT` / `DOUBLE` window + `CEIL` | peers unchanged | peers unchanged ✓ |
| heap table, no window, `CEIL(d)` | both `12.34` | both `12.34` ✓ |

**Which side is wrong:** the **equivalent** in the fuzzer finding (identity `MAX(col) OVER (PARTITION BY col)` view) only because the workload projected `CEIL`. The heap table is correct. Ground truth is the heap `SELECT` and the one-row / `ROUND` / non-DECIMAL controls.

## Equivalence construction

**Round 32** (`mismatch_round32_10.sql`): eqgen identity-window builder

```sql
CREATE VIEW t AS
SELECT MAX(c_pk) OVER (PARTITION BY c_pk) AS c_pk,
       …,
       MAX(c_dec) OVER (PARTITION BY c_dec) AS c_dec,
       c_dbl,   -- not wrapped
       …
FROM <UNION ALL split of t__base>;
```

`SELECT * FROM t` is row-identical to the heap (the shared `DECIMAL` is not mutated until `CEIL` runs). The workload's
`MAX(IFNULL(GREATEST(…, ASCII(c_chr)), CEIL(c_dec)))` then overwrites `c_dec` for later peers, so some groups answer `13` (the ceiled decimal) instead of `97` (`ASCII('a')`).

The `UNION ALL` split is not required. A two-row table and `MAX(d) OVER (PARTITION BY d)` is enough. Skipping the `c_dbl` passthrough column is not required.

Round 32's other mismatches that only drop the NULL row under correlated `NOT IN` (`mismatch_round32_1.sql` etc.) need `MAX(c_chr) OVER (PARTITION BY c_chr)` and are **not** this mutation; they sit next to the known field-index / window-view family (`dolt-run2-corr-subquery-split-view-field-index`). Do not file those as this bug.

## Minimal oracle exposure path

- **Object composition arity:** `2`.
- **GCL builder path:** `WindowRewriteQueryBuilder → CreateViewBuilder`.
- **Confidence:** high; both are exact current factory class names, and the report's
  `MAX(c_dec) OVER (PARTITION BY c_dec)` identity rewrite matches `WindowRewriteQueryBuilder`.
- **Realization:** `CreateViewBuilder` persists the window-rewritten equivalent as a view.
- **Workload/data requirements (excluded from arity):** a `DECIMAL` column, at least two peers sharing
  the window result, and workload evaluation of `CEIL` or `FLOOR` are schema/data/query requirements.
- **Exposure vs. intrinsic trigger:** the object path creates the shared window-result pointer that
  exposes the mutation. The intrinsic trigger is the composition of that shared `*apd.Decimal` value
  with in-place `CEIL`/`FLOOR`; unrelated `UNION ALL` layers are not part of the arity.

## Characterization

Faulting Eval (gms `sql/expression/function/ceil_round_floor.go`):

```go
case *apd.Decimal:
    _, err = sql.DecimalCtx.Ceil(num, num) // mutates num
    child = num
```

`Floor` is the same (`DecimalCtx.Floor(num, num)`). The copy-into-new-value form already exists in `sql/index_builder.go`:

```go
newVal := new(apd.Decimal)
_, _ = DecimalCtx.Ceil(newVal, v)
return newVal
```

Suggested fix: Eval should `Ceil`/`Floor` into a freshly allocated `*apd.Decimal`, then `Convert` that. Do not reuse the child's pointer.

WindowIter (`sql/expression/function/aggregation/window_iter.go`) materializes one buffer per partition and projects the same aggregate value to every peer — correct, if the value is immutable. The combination is construct × `CEIL`/`FLOOR` on `DECIMAL`, not a bad default frame (`RANGE` vs `ROWS` is `dolt-run2-window-order-by-range-frame-wrong` and needs `OVER (ORDER BY)`).

## How it was found

eqgen data-equivalence oracle, hunt `dolt_rich_shuffle/dolt_20260816-023229`. Replay of `mismatch_round32_10.sql`: row-identical, type-identical, deterministic, engine-unequal. Reducing the equivalent to `MAX(c_dec) OVER (PARTITION BY c_dec)` and the query to `SELECT c_pk, c_dec, CEIL(c_dec)` isolates the overwrite; `SELECT c_dec` alone is clean.

## Open items

- Confirm `TRUNCATE` / other `*apd.Decimal` in-place callers in `ceil_round_floor.go`.
- File against **go-mysql-server**, not only dolt.
