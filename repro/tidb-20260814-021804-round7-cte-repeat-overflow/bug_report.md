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

# TiDB: grouped `REPEAT` over a `WITH` CTE returns a string; the same query on the table returns NULL

## Summary

`REPEAT(name, 5592406)` on VARCHAR `'abc'` is 16 777 218 bytes, one past
`mysql.MaxBlobWidth` (16 777 216). Vectorized `REPEAT`
(`builtin_string_vec.go`) NULLs that call via `len(str) > Flen/num`. Scalar
`REPEAT` (`builtin_string.go` `evalString`) only checks `max_allowed_packet`
(64 MiB here) and **produces the string**.

On a plain table, `GROUP BY name` plus `HAVING MAX(id) <= (SELECT COUNT(*) FROM t)`
takes the vectorized path: `CAST(… AS CHAR(255))` is NULL. Replace the table with
the identity CTE

```sql
CREATE VIEW t AS WITH t__base_cte_1 AS (SELECT * FROM t__base) SELECT * FROM t__base_cte_1;
```

(or `WITH t AS (SELECT * FROM t__base) SELECT …`) and the same `SELECT` returns a
255-character `'abcabc…'` string. The two relations are row-identical; the two
`REPEAT` implementations must agree.

## Environment

| | |
|---|---|
| Engine | tidb `8.0.11-TiDB-v9.0.0-beta.2.pre-2051-g3bea8196a5` @ `3bea8196a5` |
| `@@max_allowed_packet` | `67108864` (64 MiB) — **not** the cap that fires |
| `sql_mode` | `STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES` — not load-bearing |
| collation | `utf8mb4_0900_bin` |

Observed through pymysql against the local `tidb-server`.

## Minimal repro

See [`reduced.sql`](./reduced.sql). Distilled from `mismatch_round7_5.sql`.
`CAST(… AS CHAR(255))` is observation only (never print 16 MiB+):

| Side | `name` | `IS NULL` | `CHAR_LENGTH` |
|---|---|---|---|
| base table | `abc` | 1 | NULL |
| CTE view / query-level `WITH` | `abc` | 0 | 255 |

```sql
CREATE TABLE t__base (id BIGINT, name VARCHAR(255));
INSERT INTO t__base VALUES (2, 'abc'), (42, '');
CREATE VIEW t AS WITH t__base_cte_1 AS (SELECT * FROM t__base) SELECT * FROM t__base_cte_1;

SELECT name,
       CAST(IF(name = name, REPEAT(name, 5592406), REGEXP_INSTR(name, '.')) AS CHAR(255)) IS NULL,
       CHAR_LENGTH(CAST(IF(name = name, REPEAT(name, 5592406), REGEXP_INSTR(name, '.')) AS CHAR(255)))
FROM t
GROUP BY name
HAVING MAX(id) <= (SELECT COUNT(*) FROM t);
```

## Expected vs actual

| Query | Expected | Actual |
|---|---|---|
| table, distilled `IF`/`REPEAT`/`REGEXP_INSTR` + `HAVING` | NULL (`IS NULL` = 1) | NULL |
| CTE view, same query | NULL (same rows) | **255-char string** |
| query-level `WITH t AS (SELECT * FROM t__base)`, same query | NULL | **255-char string** |
| identity `VIEW` (no `WITH`) | NULL | NULL |
| `REPEAT(name, 5592405)` (3·n = 16 777 215 ≤ MaxBlobWidth) | 255 on both | 255 on both |
| `IF(TRUE, REPEAT(…), REGEXP_INSTR(…))` | NULL on both | NULL on both |
| `ELSE NULL` | NULL on both | NULL on both |
| no `HAVING` | `'abc'` NULL and `''` length 0 on both | both agree |

**Which side is wrong:** the **CTE** (scalar / fold path) relative to the table's
vectorized `REPEAT`. `REPEAT('abc', 5592406)` as a literal also takes the scalar
path and succeeds under a 64 MiB `max_allowed_packet`, so the CTE answer matches
the *literal* implementation and the table answer matches *vectorized column*
`REPEAT`. Either both must NULL or both must produce the string.

## Equivalence construction

The fuzzer equivalent is exactly `NotMaterializedCteQueryBuilder`:

```sql
CREATE VIEW t AS WITH t__base_cte_1 AS (SELECT * FROM t__base) SELECT * FROM t__base_cte_1;
```

No CACHE, no UNION ALL, no window. Load-bearing construct: **WITH CTE × this
SELECT shape** (`IF(name = name, REPEAT(name, n), REGEXP_INSTR(name, '.'))` with
`GROUP BY name` and `HAVING MAX(id) <= (SELECT COUNT(*) FROM t)`, n ≥ 5 592 406
for `'abc'`).

The VIEW wrapper is **not** required: a query-level `WITH t AS (SELECT * FROM
t__base)` diverges the same way. An identity `CREATE VIEW t AS SELECT * FROM
t__base` and a derived table `FROM (SELECT * FROM t__base) t` do **not**.

## Minimal oracle exposure path

- **Object composition arity:** `2`.
- **GCL builder path:** `NotMaterializedCteQueryBuilder → CreateViewBuilder`.
- **Confidence:** Historical-report exact, with a current-GCL caveat: TiDB now gives
  `NotMaterializedCteQueryBuilder` weight 0 and enables `CteQueryBuilder`; the recorded SQL has no
  `NOT MATERIALIZED` token. The manifest retains the historical class name, but a current run would
  map this SQL shape to `CteQueryBuilder`.
- **Realization:** a CTE defining query exposed through a root `VIEW`; the view is not a
  materialization.
- **Workload/data requirements (excluded from arity):** the `IF`/`REPEAT`/`REGEXP_INSTR`,
  `GROUP BY`/`HAVING`, the length threshold, and the two seed rows are query/data requirements and
  are not counted.
- **Exposure vs. intrinsic trigger:** arity 2 records the as-found oracle object. Intrinsically the
  CTE is sufficient because a query-level `WITH` reproduces; the root view is exposure-only.

Reduced away from the as-found query: `GREATEST` / `'HOUR'`, `CAST(GREATEST(name,
name) AS CHAR(255))` in the `IF` condition (plain `name = name` is enough),
`SPACE(… * ('-12345678'+'3'))` (bare `REGEXP_INSTR(name, '.')` is enough), the
`1 IN (1,1,1,1,0)` filter on the `HAVING` subquery, `c_pk` / `created_at`, and
six of the eight seed rows. Two rows `(2, 'abc'), (42, '')` suffice because
`HAVING` drops the `''` group (`MAX(id)=42 > COUNT(*)=2`) and keeps `'abc'`.

## Characterization

**Trigger (all required):**

1. A `WITH` CTE (view or query-level), not an identity view or derived table.
2. `GROUP BY name` plus `HAVING` that leaves **only** the `'abc'` group.
3. `IF(name = name, REPEAT(name, n), REGEXP_INSTR(name, '.'))` — the condition
   must mention `name` (`TRUE` / `1` lets `ifFoldHandler` drop the `IF` and both
   sides vectorize). The ELSE must be `REGEXP_INSTR` on the grouping column
   (constant `NULL` / `SPACE(-1)` / `UPPER(name)` all agree, both NULL).
4. `n ≥ 5592406` for `'abc'` (`3 * n > 16777216`).

**Source split:**

```go
// pkg/expression/builtin_string_vec.go  (vectorized — table path)
if int64(byteLength) > int64(b.tp.GetFlen())/num {
    result.AppendNull()
    continue
}

// pkg/expression/builtin_string.go evalString  (scalar — no Flen check)
if uint64(byteLength)*uint64(num) > b.maxAllowedPacket {
    return "", true, handleAllowedPacketOverflowed(...)
}
return strings.Repeat(str, int(num)), false, nil
```

`getFunction` sets `Flen` to `mysql.MaxBlobWidth`. Integer division
`16777216 / 5592406 == 2`, and `len("abc") == 3 > 2`, so the vectorized path
NULLs; `5592405` yields `16777216 / 5592405 == 3` and both sides succeed.

**EXPLAIN** (both still show `repeat(name, 5592406)` in the projection — the
split is execution, not a folded constant in the plan text):

- table: `TableFullScan` → cop `HashAgg` → root `Selection` (HAVING) →
  `Projection` of `if(eq(name, name), repeat(name, 5592406), regexp_instr(…))`
- CTE: `CTEFullScan` → root `HashAgg` → `HashJoin` (cartesian, HAVING as join
  cond) → same `if`/`repeat` projection over `t__base.name`

**Does NOT (alone) trigger:** identity view; derived table; `IF(TRUE, …)`;
`ELSE NULL`; `n = 5592405`; no `HAVING`; a single `'abc'` row.

`tidb-20260812-regexp-cachetable-null-shortcircuit` is CACHE × `REGEXP_REPLACE`.
The ELSE `REGEXP_INSTR` here is only a plan-shape ingredient; the surviving
value is `'abc'*n` (THEN / REPEAT), not a regexp result.

DML not tested (`GROUP BY`/`HAVING` probe, not a simple `WHERE`).

## How it was found

eqgen corpus-shuffle
`tidb_corpus_shuffle/tidb_20260814-021804/mismatch_round7_5.sql`
(seed 1534789295). One mismatch of this shape in the run. Admissibility and type
gates passed; result stable across repeats. The as-found SELECT was a wide
projection; only `expr_6` (`CAST(IF(… GREATEST(… REPEAT …) …) AS CHAR(255))`)
disagreed (`NULL` vs 255-char `'abc'*n`).

A query-rewrite oracle (TLP/NoREC) that holds the table fixed would not swap in
the CTE and would miss the split.

## Open items

- Suggested fix: apply the same length guard in `evalString` as in
  `vecEvalString`, or drop the extra `Flen/num` check from the vectorized path
  so both honour only `max_allowed_packet`.
- Second mismatch in the same run (`mismatch_round42_0.sql`, `ALTER TABLE …
  CACHE` + window-dedup view + `IN`/`NOT IN` joins) is a **separate** candidate,
  not reduced here.
- GitHub issue not opened (not requested).
