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

# TiDB: `ANY_VALUE()` in a view + a scalar expression over that column in the left operand of `IN (subquery)` → planner error 1105 `Can't find column Column#N in schema`

## Summary

A view whose body applies `ANY_VALUE()` to a column cannot be used with a query that wraps that
column in **any** scalar expression and puts it in the left operand of `IN (subquery)`. The planner
fails with `1105 Can't find column Column#N in schema Column: [...]`. It fails at bare `EXPLAIN`, on an
**empty table**, with **three statements** and one column — so it is a pure name-resolution/schema
bookkeeping failure, not an execution or data problem.

The bug is narrow in a way that pins the mechanism: `MAX()`/`MIN()` over the same shape is clean, the
**same view body as an inline derived table is clean**, a bare column reference is clean, and
`= ANY (subquery)` — by definition the same predicate as `IN (subquery)` — is clean while `IN`,
`NOT IN` and `<> ALL` all fail.

This is **226 of the 300 error findings in `tidb_run19`**, across 107 rounds. All 226 have `ANY_VALUE`
in the equivalence chain and a scalar-function wrapper in the query, and 13 of 13 sampled findings
reproduce when their entire multi-link chain is replaced by one `ANY_VALUE` view.

## Environment

| | |
|---|---|
| Engine | tidb `8.0.11-TiDB-v9.0.0-beta.2.pre-2051-g3bea8196a5` @ `3bea8196`, unistore, assertions off |
| `sql_mode` | `STRICT_ALL_TABLES,NO_ZERO_DATE,NO_ZERO_IN_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION,ONLY_FULL_GROUP_BY,NO_BACKSLASH_ESCAPES` — **not load-bearing** |
| Charset / collation | `utf8mb4` / `utf8mb4_0900_bin` — not load-bearing |
| Determinism | fully deterministic and **data-independent**: reproduces with zero rows, and at `EXPLAIN` |
| Store | `unistore`; the failure is in the planner, so TiKV is not expected to matter (not verified) |
| Admissibility | **not establishable, and that is the finding** — `SELECT * FROM t` on the equivalent succeeds (the relation is row-identical), but the workload query cannot be planned at all. One-sided error: base plans and runs, equivalent raises. |

## Minimal repro

```sql
CREATE TABLE k (c VARCHAR(9), g BIGINT);
CREATE VIEW t AS SELECT ANY_VALUE(c) AS c FROM k GROUP BY g;

EXPLAIN SELECT 1 FROM t AS t1 WHERE UPPER(t1.c) IN (SELECT 'x');
-- ERROR 1105 (HY000): Can't find column Column#5 in schema Column: [Column#15] PKOrUK: [] NullableUK: []
```

No rows are inserted. The `GROUP BY` is not needed either (`CREATE VIEW t AS SELECT ANY_VALUE(c) AS c
FROM k` fails the same way). The `Column#N` and the bracketed schema list both vary with plan context
— only the error class is stable, so don't pattern-match the brackets.

## Expected vs actual

Every row below is on an empty or single-row table; the expected result is always "plans, returns 0
rows" (or 1 row where noted).

| | Expected | Actual |
|---|---|---|
| `UPPER(v.c) IN (SELECT 'x')`, `v` = `ANY_VALUE` view | 0 rows | **`1105 Can't find column`** |
| same, `v` = `MAX(c)` view | 0 rows | 0 rows |
| same, `v` = `MIN(c)` view | 0 rows | 0 rows |
| same, `v` = `GROUP BY c` view, no aggregate | 0 rows | 0 rows |
| same, `v` = plain view | 0 rows | 0 rows |
| same, **same body as an inline derived table** | 0 rows | 0 rows |
| `ANY_VALUE` view, expression over a *passthrough* column | 0 rows | 0 rows |
| `ANY_VALUE` view, **bare** column: `t1.c IN (SELECT 'x')` | 0 rows | 0 rows |
| `UPPER` / `CONCAT` / `NULLIF` / `IFNULL` / `COALESCE` / `CASE` / `IF` over the agg column | 0 rows | **all `1105`** |
| `… NOT IN (SELECT 'x')` | 0 rows | **`1105`** |
| `… <> ALL (SELECT 'x')` | 0 rows | **`1105`** |
| `… = ANY (SELECT 'x')` | 0 rows | 0 rows |
| `… IN ('x','y')` (literal list) | 0 rows | 0 rows |
| `… = 'x'` (no subquery) | 0 rows | 0 rows |
| `SELECT UPPER(t1.c) FROM t AS t1` (expression alone) | 1 row | 1 row |
| `SELECT COUNT(*) FROM t` on the finding's real chain | 8 | 8 |
| the finding's workload query on base `t` | 7 rows | 7 rows |
| the finding's workload query on the equivalent `t` | 7 rows | **`1105`** |

## Equivalence construction

### (1) The construct as the eqgen builder emits it

The equivalent `t` is the duplicate-and-reduce builder in its **aggregate spelling**: key each row with
`ROW_NUMBER()`, duplicate it 100× through a `UNION ALL` against a recursive generator, then collapse it
back with `ANY_VALUE(col) … GROUP BY key`. Verbatim from `logs/tidb_run19/error_round1001_0.sql`:

```sql
ALTER TABLE t RENAME TO t__base;
CREATE TABLE t__base_table_1 (…, `eq_key_1` BIGINT);
INSERT INTO t__base_table_1 (…) SELECT id, name, created_at, ROW_NUMBER() OVER (ORDER BY id) FROM t__base;
CREATE TABLE t__base_table_2 (…, `eq_key_1` BIGINT);
INSERT INTO t__base_table_2 … SELECT … FROM t__base_table_1
  UNION ALL SELECT … FROM t__base_table_1 CROSS JOIN
    (WITH RECURSIVE eq_gen_series (eq_gen_n) AS (SELECT 1 UNION ALL SELECT eq_gen_n + 1
       FROM eq_gen_series WHERE eq_gen_n < 100) SELECT eq_gen_n FROM eq_gen_series) AS eq_gen;
CREATE VIEW t__base_view_1 AS SELECT ANY_VALUE(id) AS id, ANY_VALUE(name) AS name,
       ANY_VALUE(created_at) AS created_at FROM t__base_table_2 GROUP BY eq_key_1;   -- load-bearing
CREATE VIEW t__base_view_2 AS SELECT * FROM t__base_view_1;
CREATE VIEW t__base_view_3 AS SELECT id, name, created_at, CAST(NULL AS SIGNED) AS eq_tmp_col_2 FROM t__base_view_2;
CREATE VIEW t AS SELECT id, name, created_at FROM t__base_view_3;
```

and the workload query's `WHERE`:

```sql
WHERE CAST(NULLIF(t1.created_at, t1.created_at) AS CHAR(255))
      NOT IN (SELECT t3.name FROM t AS t3 WHERE 0 GROUP BY t3.id, t3.name)
```

**Mapping onto the distilled repro.** `t__base_view_1` → the one `ANY_VALUE … GROUP BY` view;
`CAST(NULLIF(created_at, created_at) AS CHAR(255))` → `UPPER(c)` (any wrapper does); the `NOT IN
(SELECT … WHERE 0 GROUP BY …)` → `IN (SELECT 'x')` — the `WHERE 0`, the `GROUP BY`, the `DISTINCT` and
the whole SELECT list are all irrelevant. The eight rows go to zero and the three columns to one.

The `CAST(NULL AS SIGNED) AS eq_tmp_col_2` add-then-drop wrapper looks like a prime suspect — the
missing symbol is a `Column#N`, after all — but it is not: the same wrapper over a plain table is
clean, and only 37 of the 226 findings even have it.

### (2) The load-bearing construct — a construct × query-feature composition

**`ANY_VALUE` in a view × a scalar expression over that column × `IN (subquery)`.** All three are
required, and the negative controls are unusually sharp:

- `MAX`/`MIN` in place of `ANY_VALUE` → clean, so it is not "an aggregate in a view".
- the **same body inline as a derived table** → clean, so it is specifically the *view*.
- a **bare column** instead of the expression → clean, so it is not any particular function
  (`UPPER`, `CONCAT`, `CAST`, `NULLIF`, `IFNULL`, `COALESCE`, `CASE`, `IF` all trigger it equally).
- `= ANY (subquery)` → clean, though it is the same predicate as `IN (subquery)`; a literal `IN` list
  and a plain comparison are clean too.

### (3) Constructs reduced away

The `ROW_NUMBER()` keying table, the `UNION ALL` × `WITH RECURSIVE` 100× duplication, the `SELECT *`
wrapper view, the add-then-drop `eq_tmp_col_2` wrapper and its projecting view — five of the six chain
links. On the query side: the entire SELECT list, `DISTINCT`, the `CAST`, the `NULLIF`, `WHERE 0`, the
subquery's `GROUP BY`, and the subquery's relation (a constant `SELECT 'x'` suffices). All eight rows
and two of the three columns.

## Minimal oracle exposure path

- **Object composition arity:** `2`.
- **GCL builder path:** `KeyGroupAggregateReduceBuilder → hardcoded VIEW`.
- **Confidence:** Verified against the emitted construction, the report's builder attribution, the
  current TiDB GCL, and the builder implementation.
- **Realization:** `KeyGroupAggregateReduceBuilder` directly returns a `CreateView`; there is no
  separately selected `CreateViewBuilder` in this minimal path.
- **Workload/data requirements (excluded from arity):** the scalar wrapper, `IN (subquery)` spelling,
  and zero-row/one-column reduction are workload/data conditions and are not counted.
- **Exposure vs. intrinsic trigger:** both `ANY_VALUE` production and stored-view realization are
  intrinsic, because the identical body inline as a derived table is clean. Earlier keying,
  duplication, and wrapper objects are exposure history and remain reduced away.

## Characterization

### What triggers it / what does not

See the tables above; the three sharpest facts are that `= ANY` is clean where `IN` is not, that a
derived table is clean where the identical view body is not, and that `ANY_VALUE` is the only aggregate
affected. Together they say the defect is in how the planner rewrites `IN (subquery)` — presumably to a
semi-join or an `Apply` — against the schema of a *view* whose output column is produced by
`ANY_VALUE`, when that column is consumed through an expression rather than referenced directly. The
expression is what forces a new `Column#N` to be allocated, and that new symbol is the one reported
missing.

### No plan diff is available

There is nothing to diff: `EXPLAIN` raises the same 1105, so no plan is ever produced for the failing
side. The clean controls do produce plans, but comparing "a plan" with "no plan" says nothing the error
message doesn't already say. The `Column#N` in the message is the closest thing to a mechanism pointer,
and it is unstable across contexts (`Column#5`, `Column#9`, `Column#4` depending on how many symbols
were allocated first), so it should not be treated as an identifier.

I did not attribute this to a specific optimizer rule beyond the observation below, and no server-side
stack is available: the panic-free 1105 path is a returned error, not a recovered panic, and at the
server's `error` log level nothing is written.

### Relationship to the other two error clusters in this run

`mysql.opt_rule_blacklist` puts all three clusters in the same neighbourhood, which is worth stating so
nobody over-reads it:

| blacklisted rule | cluster A (this bug) | cluster B (`nil pointer`, 62) | cluster C (`nil map`, 12) |
|---|---|---|---|
| *(none)* | `findcol` | `nilptr` | `nilmap` |
| `predicate_push_down` | **clean** | clean *or* `findcol` | clean *or* `findcol` |
| `decorrelate` | `findcol` | `nilptr` | `nilmap` |
| `aggregation_push_down` | `findcol` | `nilptr` | `nilmap` |
| `column_prune` | `findcol` | clean *or* `nilptr` | `nilmap` |

Disabling `predicate_push_down` clears cluster A entirely, and on several B and C findings it converts
the panic into *this* error — so the three are layered failures reachable from overlapping query
shapes, not three unrelated bugs. But they are structurally distinct as *findings*: `ANY_VALUE` is
present in 226/226 of A and only 8/62 of B and 5/12 of C.

## How it was found

eqgen v3 data-equivalence oracle, `tidb_run19`, 226 `error_*` findings across 107 rounds (sample:
round 1001, seed 178284896). The oracle holds the query fixed and swaps in a row-identical relation.
The relation it swapped in was the duplicate-and-reduce builder's aggregate spelling — key, blow up
100×, collapse with `ANY_VALUE` — which is a pure identity on the data and happens to be a shape the
planner cannot handle.

The dedup path is worth recording because it is what made 300 findings tractable. Normalising the
`EQUIVALENT error:` header line (mask database names, quoted literals and integers) collapsed 300
findings to **three** message classes — 226 / 62 / 12, all error code 1105. Grepping the equivalence
chains for builder markers then gave cluster A a 226/226 signature (`ANY_VALUE` + `UNION ALL` +
`ROW_NUMBER`), and grepping the queries gave 226/226 for a scalar-function wrapper. Only after that did
anything touch the engine. Because 1105 is TiDB's catch-all, clustering on the *message* rather than
the code is what carried the weight — a bare-1105 rule would have merged three different bugs.

Note the symmetry with this run's mismatch findings: those came from the **window** spelling of the
same duplicate-and-reduce builder (`MAX(col) OVER (PARTITION BY key)`, see
[`../tidb-run19-window-view-correlated-any-subquery`](../tidb-run19-window-view-correlated-any-subquery/bug_report.md)),
and these come from its **aggregate** spelling. One builder, two spellings, two unrelated TiDB bugs.

A query-rewrite oracle would not construct this: the `ANY_VALUE` view is a property of the relation,
and TLP/NoREC/EET hold the data fixed. It is also a one-sided *error*, so it needs no result comparison
at all — a single-query fuzzer emitting `ANY_VALUE` views would find it too, which makes the
226-finding volume here a statement about the builder's frequency rather than about oracle power.

- Reduced repro and all controls: [`reduced.sql`](reduced.sql)
- Original finding: hunt log (+ 225 siblings)
