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

# MySQL: `REGEXP` backslash character-class in a WHERE over a `UNION ALL` derived table drops all matching rows

## Summary

Under `NO_BACKSLASH_ESCAPES`, a `REGEXP` predicate whose pattern uses a backslash character-class
escape (`\w \d \s \W \S \D`) returns **wrong (missing) rows** when the filtered relation is a
`UNION ALL` derived table (or a view built on one). The **same predicate over the base table, or
over a plain (non-`UNION`) derived table, matches correctly**. Same session, same `sql_mode`, same
data — only the presence of `UNION ALL` under the scan changes the result.

The base scan honours `NO_BACKSLASH_ESCAPES` (the pattern reaches the regex engine as `\w`, matching
a word character); the `UNION ALL` derived branch behaves as though the mode were off (`\w` collapses
to the literal `w`, matching nothing). So the backslash-escape handling of the regex pattern is
**inconsistent across the plan**.

## Environment

- **Version:** `VERSION()` = `26.7.0-debug` (MySQL `main`, commit `@06a5c1c9`, assertions on)
- **`sql_mode`:** must include `NO_BACKSLASH_ESCAPES` (see note below)
- **`character_set_connection`:** `utf8mb4`
- **`collation_connection`:** `utf8mb4_0900_bin`

## Minimal repro

```sql
SET SESSION sql_mode = 'NO_BACKSLASH_ESCAPES';

CREATE TABLE t (name VARCHAR(255));
INSERT INTO t VALUES ('a');

-- Expected 1 row, actual 1 row (correct):
SELECT name FROM t WHERE name REGEXP '\w';

-- Expected 1 row, actual 0 rows (WRONG):
SELECT name FROM (SELECT * FROM t UNION ALL SELECT * FROM t WHERE 0) x WHERE name REGEXP '\w';
```

## Expected vs actual

| Query | Expected | Actual |
|---|---|---|
| `... FROM t WHERE name REGEXP '\w'` | 1 row | 1 row |
| `... FROM (SELECT * FROM t) x WHERE name REGEXP '\w'` (plain derived) | 1 row | 1 row |
| `... FROM (SELECT * FROM t UNION ALL SELECT * FROM t WHERE 0) x WHERE name REGEXP '\w'` | 1 row | **0 rows** |

## Equivalence construction

The equivalent `t` was a 13-object chain: a `ROW_NUMBER()` window-filter view, several `BETWEEN`-split
views recombined with `UNION ALL`, a `FIRST_VALUE()` window view, and a final
`t = … UNION ALL …`.

- **Load-bearing construct:** the `UNION ALL` (union round-trip) that produces the final `t`.
- **It is a composition:** `UNION ALL` derived table / view **×** a `REGEXP` backslash-class escape
  (`\w \d \s \W \S \D`) in the `WHERE`. A plain (non-`UNION`) derived table does not diverge, and
  non-escape patterns (literals, bracket sets, anchors, alternation, POSIX classes) pass the
  `UNION ALL` correctly.
- **Reduced away (not needed):** the `ROW_NUMBER()`/`FIRST_VALUE()` window views, the `BETWEEN`-split
  branches, and the multi-view chaining — a single `SELECT * FROM t UNION ALL SELECT * FROM t WHERE 0`
  derived table over a 1-row table reproduces.

## Minimal oracle exposure path

- **Object composition arity:** **2**
- **GCL builder path:** `UnionEmptyRoundTripBuilder` → `CreateViewBuilder`
- **Confidence:** Verified — the reduced `R UNION ALL empty(R)` form and final view correspond directly to these GCL builders.
- **Realization:** `CreateViewBuilder` exposes the union-all round trip as the queried `VIEW`.
- **Workload/data requirements (excluded from arity):**
  - `NO_BACKSLASH_ESCAPES`, so a backslash character-class escape reaches the regex engine.
  - A matching `REGEXP` backslash class such as `\w`, `\d`, or `\s` in `WHERE`.
  - At least one row that should match.

**Exposure vs. intrinsic trigger:** The two-builder path creates the union-derived relation through which the oracle sees the missing rows. The intrinsic trigger is `REGEXP` backslash-class handling over a `UNION ALL` derived plan; the original window and split-view chain is not load-bearing.

## Characterization (verified against the build)

- **The whole backslash-escape family reproduces:** `\w`, `\d`, `\s`, `\W`, `\S`, `\D` — every case
  where the escape matches in the base returns 0 through the `UNION ALL` derived table.
- **`UNION ALL` is the trigger.** A plain derived table `(SELECT * FROM t) x` matches correctly.
- **Only backslash escapes break.** Literals (`'a'`), bracket sets (`'[a]'`), anchors (`'^a'`),
  alternation (`'a|z'`), `'.'`, and POSIX classes (`'[[:alpha:]]'`) all pass the `UNION ALL`
  correctly. So this is not "REGEXP over UNION is broken" — it is specific to the escape handling.
- **`NO_BACKSLASH_ESCAPES` is required only** so the pattern reaches the engine as `\w` rather than
  `w`; without the mode, `'\w'` → `'w'` on *both* sides, and the divergence is simply not observable
  (both return 0). The defect is the inconsistent escape handling across the plan, not the mode.
- Data is unaffected: `SELECT * FROM t` (base) and `SELECT * FROM <equivalent>` are row-identical;
  only the `REGEXP`-filtered result differs.

## How it was found

Surfaced by the eqgen differential fuzzer (equivalence oracle): a query returned different row
multisets against a base table vs a row-identical rewrite whose `t` was materialised through a view
chain containing `UNION ALL`. Reduced to the above by execution-guided delta-debugging (view chain →
query → rows → predicate → `sql_mode`).

- Full original finding: hunt log (13-view chain, 6-expression `GROUP BY` query, 8 rows)
- Reduced repro: `reduced.sql` (this folder)
- Fuzzer seed: `249296182`
