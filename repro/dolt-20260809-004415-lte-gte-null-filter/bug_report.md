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

# Dolt: `WHERE col <= expr` and `col >= expr` return rows where `col IS NULL` (ValueRow fast path reports a NULL comparison as "equal", and `<=`/`>=` treat that as TRUE)

## Summary

On a nullable, **unindexed** column, `WHERE b <= 1` and `WHERE b >= 1` return rows whose `b` is NULL.
SQL three-valued logic makes those predicates NULL, not TRUE, so the rows must be filtered out. The
cause is in go-mysql-server's `ValueRow` execution fast path: `comparison.CompareValue` signals a NULL
operand by returning `cmp == 0` — indistinguishable from "the operands are equal" — and
`LessThanOrEqual.EvalValue` / `GreaterThanOrEqual.EvalValue` return `TrueValue` for everything that is
not strictly greater/less. `>` and `<` share the same NULL-blind comparison but default to
`FalseValue`, so they are accidentally correct; `=` and `<>` do not implement the fast path at all.
The row path (`Eval`) handles NULL correctly, which is why any `Project`/`Sort`/`LIMIT` above the
filter, an index on the filtered column, `DELETE`/`UPDATE`, or the `dolt sql` CLI all mask it.
**Wrong results only — `DELETE`/`UPDATE` are unaffected, so no data is lost or silently modified.**

## Environment

| | |
|---|---|
| Engine | `dolt version 2.2.3`, binary `dolt-main/bin/dolt` |
| `VERSION()` | `8.0.31` — this is Dolt's **MySQL compatibility string**, not its own version (see *Harness notes*) |
| go-mysql-server | `v0.20.1-0.20260805191915-e5eafe0da809` (module cache); dolt-src at `a995f245c`, 2026-08-05 |
| Access path | `dolt sql-server` **only**; the in-process `dolt sql` CLI is correct |
| Clients | reproduced via pymysql **and** the mariadb CLI — server-side, not a client artefact |
| Session | all defaults. `sql_mode` is **irrelevant**: verified identical with `''` and with the fuzzer's `STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES,PIPES_AS_CONCAT` |
| Collation / charset | `utf8mb4_0900_bin` / `utf8mb4` (not load-bearing) |
| Regression window | **not determined** — only one Dolt build was available on this machine (see *Open items*) |

## Minimal repro

**The route matters: this reproduces only through `dolt sql-server`.** `dolt sql -q` returns the
correct answer on the same data, so trying the CLI first will look like a non-reproduction.

```bash
mkdir /tmp/r && cd /tmp/r
dolt sql-server --host 127.0.0.1 --port 3306 --data-dir /tmp/r &
mysql -h127.0.0.1 -P3306 -uroot -e "CREATE DATABASE d;"
mysql -h127.0.0.1 -P3306 -uroot d -e "
  CREATE TABLE t (a BIGINT NOT NULL, b BIGINT);
  INSERT INTO t VALUES (1, NULL);
  SELECT * FROM t WHERE b <= 1;"
```

```sql
CREATE TABLE t (a BIGINT NOT NULL, b BIGINT);
INSERT INTO t VALUES (1, NULL);

SELECT * FROM t WHERE b <= 1;   -- expected 0 rows; Dolt returns (1, NULL)
SELECT * FROM t WHERE b >= 1;   -- expected 0 rows; Dolt returns (1, NULL)
```

`b` IS NULL, so `b <= 1` evaluates to NULL. A `WHERE` clause keeps a row only when the predicate is
TRUE, so no row qualifies.

Because the defect is in go-mysql-server rather than in Dolt's storage, the natural home for a
regression test is a gms engine test whose plan is a bare `Filter` over `Table` and which is executed
through the `ValueRow` path — a `Project`, `Sort` or `LIMIT` above the filter, or an index on the
filtered column, all silently avoid the bug and would make the test vacuous.

## Expected vs actual

| Query (table above unless noted) | Expected | Actual |
|---|---|---|
| `SELECT * FROM t WHERE b <= 1` | 0 rows | 1 row: `(1, NULL)` |
| `SELECT * FROM t WHERE b >= 1` | 0 rows | 1 row: `(1, NULL)` |
| `SELECT * FROM t WHERE NOT (b > 1)` | 0 rows | 1 row: `(1, NULL)` |
| `SELECT * FROM t WHERE b <= a` | 0 rows | 1 row: `(1, NULL)` |
| `SELECT * FROM t WHERE b > 1` / `b < 1` / `b = 1` / `b <> 1` | 0 rows | 0 rows — correct |
| rows `(1,NULL),(2,2),(3,5)`, `WHERE NOT (b > a)` | `(2,2)` | `(2,2)` **and** `(1,NULL)` |
| the finding's own query (`reduced.sql`, `concrete-as-emitted`) | 7 rows | 8 rows |
| `SELECT COUNT(*) FROM (SELECT * FROM t WHERE b <= 1) w` | 0 | 0 — correct |

The filter is not simply dropped: in the three-row case, `(3,5)` has `5 > 3` so `NOT (b > a)` is FALSE
and it is correctly excluded. Only the NULL row leaks.

## Equivalence construction

**(1) The construct as the builder emits it.** These are eqgen same-base fork rounds: the base
database seeds `t` and forks it with `CREATE TABLE t0/t1/t2 AS SELECT * FROM t`, while the equivalent
database renames `t` aside to `t__base` and rebuilds each exposed name through row-preserving
builders. For `mismatch_round3_1.sql` the equivalent's `t1` is

```sql
CREATE TABLE t__base_table_1 AS SELECT c_pk, ..., c_ts FROM t__base;
CREATE VIEW t1 AS SELECT c_pk, ..., c_ts
  FROM (SELECT c_pk, ..., c_ts, ((ROW_NUMBER() OVER (ORDER BY c_pk)) >= 1) AS eq_q
        FROM t__base_table_1) AS eq_qsrc
  WHERE eq_q;
```

and the workload query is `SELECT c_pk, c_int, ..., c_ts FROM t1 WHERE NOT (c_int > c_pk)`.

**(2) The load-bearing construct — and an inversion worth stating plainly.** For once the equivalence
rewrite is the side that is *right*. The builders wrap `t1` in a derived table plus an always-true
`WHERE eq_q`, which forces a `Project` node above the filter and switches execution onto the correct
row path. The base side is the plain `CREATE TABLE t1 AS SELECT * FROM t` fork, whose plan is a bare
`Filter` over `Table` — the shape that takes the buggy fast path. So the oracle's "only in base" rows
are Dolt's wrong answers, and the elaborate equivalent is the reference. Nothing in the builder chain
is load-bearing; what matters is that it forces a `Project`.

**(3) Reduced away.** All fork DDL and the entire equivalence chain; 8 of 9 columns and every type
except one `BIGINT`; 7 of 8 rows; the `NOT (... > ...)` spelling (plain `<=` is enough); the
column-vs-column form (a literal is enough). Final repro: two columns, one row, one predicate.

## Minimal oracle exposure path

- **Object composition arity:** `2`.
- **GCL builder path:** `QualifyQueryBuilder → CreateViewBuilder`.
- **Confidence:** high; both exact current factory class names map directly to the report's emitted
  qualify-derived query and final view.
- **Realization:** `CreateViewBuilder` exposes the qualify-wrapped equivalent under the workload name.
- **Workload/data requirements (excluded from arity):** a nullable operand, `<=`/`>=` (or a normalized
  negation), a NULL-bearing row, and the server `ValueRow` execution path are workload/data/plan
  conditions.
- **Exposure vs. intrinsic trigger:** the arity-2 path is the oracle contrast that adds a `Project` and
  routes around the bug. The entire equivalence chain reduces away from the intrinsic trigger, which
  is a bare `Filter` over `Table` taking the NULL-blind fast path.

## Characterization

**Exactly the four comparisons that implement the `ValueRow` fast path are involved, and exactly the
two whose fast path defaults to `TrueValue` are wrong.** In
`sql/expression/comparison.go` only `>`, `<`, `>=`, `<=` define `EvalValue`:

```go
// comparison.CompareValue -- comparison.go:232.  NULL is reported as cmp == 0, with no error,
// which is indistinguishable from "the operands compare equal".
if lv.IsNull() || rv.IsNull() {
    return 0, nil
}

// LessThanOrEqual.EvalValue -- comparison.go:898.  NULL (cmp == 0) falls into the TRUE branch.
if cmp == 1 { return sql.FalseValue, nil }
return sql.TrueValue, nil                      // <-- NULL becomes TRUE

// GreaterThan.EvalValue -- comparison.go:693.  Same NULL-blindness, but the default is FALSE,
// so a NULL comparison is excluded and the result happens to be right.
if cmp != 1 { return sql.FalseValue, nil }
return sql.TrueValue, nil
```

The row path immediately above the buggy method gets it right, which makes the intended contract
explicit — `Compare` raises `ErrNilOperand` and `Eval` maps it to SQL NULL:

```go
// LessThanOrEqual.Eval -- comparison.go:864
result, err := lte.Compare(ctx, row)
if err != nil {
    if ErrNilOperand.Is(err) { return nil, nil }   // NULL, correctly
    return nil, err
}
return result < 1, nil
```

And the filter node chooses between the two purely on whether the pipeline supports `ValueRowIter`
(`sql/plan/filter.go`): `Next` uses `sql.EvaluateCondition` + `sql.IsTrue(res)` — NULL-safe —
while `NextValueRow` uses `EvalValue` and tests `res.Val[0] == 1`, with no NULL check.

**Truth table observed** (one row, `b IS NULL`, so every predicate is NULL and the correct answer is
always 0 rows):

| Predicate | Normalises to | Fast-path default | Result |
|---|---|---|---|
| `b > 1`, `b < 1` | — | `FalseValue` | 0 rows — correct |
| `b <= 1`, `b >= 1` | — | `TrueValue` | **1 row — wrong** |
| `NOT (b > 1)` | `b <= 1` | `TrueValue` | **1 row — wrong** |
| `NOT (b < 1)` | `b >= 1` | `TrueValue` | **1 row — wrong** |
| `NOT (b >= 1)` | `b < 1` | `FalseValue` | 0 rows — correct |
| `NOT (b <= 1)` | `b > 1` | `FalseValue` | 0 rows — correct |
| `b = 1`, `b <> 1`, `BETWEEN`, `IN`, `b + 1 <= 1`, `(b <= 1) IS TRUE` | no `EvalValue` → row path | — | 0 rows — correct |

The `NOT (b > 1)` / `NOT (b >= 1)` pair is the sharpest control: two adjacent spellings, one wrong and
one right, differing only in which fast-path branch NULL lands in.

**What masks it** (each control in `reduced.sql` changes exactly one thing and is correct):

| Change | Why it masks the bug |
|---|---|
| `SELECT b, a` instead of `SELECT *` | forces a `Project` above the `Filter` → row path |
| `SELECT a` (column the filter does not use) | scan must still read `b`, so a `Project` is required |
| `ORDER BY a` / `LIMIT 5` | any node above the `Filter` has the same effect |
| wrapping in a derived table and **aggregating** (`SELECT COUNT(*) FROM (…) w`) | the aggregation node forces the row path |
| `KEY(b)` on the filtered column | plan becomes `IndexedTableAccess`, no `FilterIter` at all |
| `DELETE` / `UPDATE` with the same `WHERE` | take the row path |
| the `dolt sql` CLI | correct; only `dolt sql-server` shows it |

A `PRIMARY KEY` alone does **not** mask it — only an index on the *filtered* column does.

**Decisive plan diff.** Wrong and correct differ by one node:

```
-- WRONG: SELECT * FROM t WHERE b <= 1
Filter
 ├─ (t.b <= 1)
 └─ Table
     ├─ name: t
     └─ columns: [a b]

-- correct: SELECT b, a FROM t WHERE b <= 1
Project
 ├─ columns: [t.b, t.a]
 └─ Filter
     ├─ (t.b <= 1)
     └─ Table  ... columns: [a b]

-- correct: SELECT * FROM ti WHERE b <= 1   (index on b)
IndexedTableAccess(ti)
 ├─ index: [ti.b]
 ├─ filters: [{(NULL, 1]}]        <-- NULL-exclusive lower bound: the index path is correct
 └─ columns: [a b]
```

Note `SELECT * FROM t WHERE b < 1` has the *same* bare `Filter`/`Table` plan and is correct — so the
plan shape alone is not sufficient; the operator must also be one whose fast path defaults to TRUE.

**Same bug via `dolt sql-server`, correct via `dolt sql`, on one data directory — and the plans are
identical.** Same binary, same data dir, same database, same query. `EXPLAIN PLAN` returns the *same*
tree on both routes, so the difference is not in planning at all — it is which `FilterIter` method the
row source drives (`NextValueRow` vs `Next`):

```
$ dolt sql -q "USE d; SELECT * FROM t WHERE b <= 1;"          # (server stopped)
(0 rows)                                                       <-- correct
$ dolt sql -q "USE d; EXPLAIN PLAN SELECT * FROM t WHERE b <= 1;"
Filter
 ├─ (t.b <= 1)
 └─ Table  name: t  columns: [a b]

$ mariadb -h127.0.0.1 -P13399 -uroot --skip-ssl d -e "SELECT * FROM t WHERE b <= 1;"
a=1  b=NULL                                                    <-- WRONG
$ mariadb ... -e "EXPLAIN PLAN SELECT * FROM t WHERE b <= 1;"
Filter
 ├─ (t.b <= 1)
 └─ Table  name: t  columns: [a b]                             <-- byte-identical plan
```

This is why the bug belongs to **go-mysql-server**, not to either front end: both link the same gms
and produce the same plan; only the server's execution engages the buggy `ValueRow` fast path. It also
means a fix validated only through `dolt sql` would not be validated at all.

Reproduced through two independent clients (pymysql and the mariadb CLI), so it is a server-side
result, not a client decoding artefact.

## How it was found

The eqgen data-equivalence oracle: it holds the workload query fixed and swaps in a relation that is
row- and type-identical to the base table, so any difference in the result multiset is a divergence,
with no reference engine and no expected output needed.

That is what made a bug this shallow visible. The predicates involved are as ordinary as SQL gets
(`WHERE c_big <= -7`), so the engine never errors and never crashes — it just silently returns one row
too many, and you would only notice by knowing the right answer. The oracle manufactured that right
answer as a side effect: its builders wrap the relation in a derived table with an always-true `WHERE`,
which happens to force the plan onto Dolt's correct execution path. So the rewritten, far more
complicated relation became the reference for the plain one. A query-rewrite oracle (TLP/NoREC/EET)
would also have had a decent chance here, but these runs found it 70 times without being aimed at NULL
semantics at all.

**All 70 findings across both dolt gate runs are this one bug** — 56 in `dolt_20b/dolt_20260809-004415`
and 14 in `dolt_20/dolt_20260809-003653` (same engine, same session settings). Two independent
confirmations, run over every finding:

1. Their predicates partition exactly into the leaking forms, with nothing left over:
   `dolt_20b` — `<=` 28, `>=` 19, `NOT >` 6, `NOT <` 3 (56/56);
   `dolt_20` — `<=` 8, `>=` 5, `NOT <` 1 (14/14).
2. Re-running each finding's base side with the identical filter under an aggregation
   (`SELECT COUNT(*) FROM (<query>) w`, which forces the row path) yields a **smaller count than the
   number of rows the bare query returns** — 56/56 and 14/14. For the 14 `dolt_20` findings the check
   was tightened further: the masked count equals the **equivalent side's** row count exactly (14/14),
   which is what proves the equivalent is the correct side and the base merely over-returns.

Every finding has rows "only in base" and none "only in equivalent", which is the signature of the
inversion described above.

Note for anyone re-running this: the mask must add a node the optimizer cannot flatten away.
`SELECT COUNT(*) FROM (<query>) w` works; **`SELECT * FROM (<query>) w` does not** — the derived table
is flattened and the plan is the same bare `Filter` over `Table`, so it still returns the wrong rows.

* `dolt_20b` rounds: 2, 3, 4, 5, 6, 8, 9, 11, 12, 16, 18, 19 · seeds incl. 1055310003, 1355787176, 1739442354
* `dolt_20` rounds: 5, 16, 18 · seeds 432918046, 13079857, 1534987412
* Reduced repro: [`reduced.sql`](reduced.sql)
* Original findings: `dialect_gates_20260809/dolt_20b/dolt_20260809-004415/`
  (`mismatch_round3_1.sql` is the smallest at 3.8 KB) and
  `dialect_gates_20260809/dolt_20/dolt_20260809-003653/`

## Open items

* **Regression window not determined.** Only one Dolt build (2.2.3) was present on this machine. Given
  the `ValueRow`/`EvalValue` fast path looks like a recent addition, bisecting it across gms revisions
  would sharpen the report; two gms versions are in the module cache
  (`…20260730213208-115d071462d7` and `…20260805191915-e5eafe0da809`) as a starting point.
* **Suggested fix**, for the report's benefit: have `CompareValue` distinguish NULL from "equal"
  (return `ErrNilOperand`, as `Compare` does, or a third result), and make all four `EvalValue`
  implementations return `sql.NullValue` in that case. Fixing only `<=`/`>=` would leave `>` and `<`
  correct by accident, and would still be wrong for any future caller that negates the result.

## Harness notes (eqgen, not Dolt)

Two defects in the fuzzer surfaced while triaging; neither affects the finding's validity.

1. **The finding header records the wrong version.** `DoltAdapter.engine_banner()` uses
   `cluster.server_version()`, which is `SELECT VERSION()` → `8.0.31`, Dolt's MySQL compatibility
   string. Every Dolt finding is therefore stamped `dolt 8.0.31`, and the actual build (`dolt version`
   → 2.2.3) is nowhere in the file. A repro pinned to the wrong version number is not filable as-is —
   the banner should carry `dolt version` (`eqgen/dialects/dolt/cluster.py:227`).
2. **The base block omits the fork DDL** (already reported from the duckdb run, same root cause in
   `eqgen/fuzz/report.py:225-227`): for a same-base fork round the base half never creates
   `t0`/`t1`/`t2`, so it cannot run the query as written. All 56 files here are affected.
