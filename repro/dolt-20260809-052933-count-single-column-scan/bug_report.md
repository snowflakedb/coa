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

# Dolt: `COUNT(<column>)` counts the wrong column when the plan is `GroupBy` directly over a one-column table scan

## Summary

`SELECT COUNT(q) FROM u` returns the wrong number. When the optimizer prunes the table scan down to a
single column and `GroupBy` sits directly on it, the `COUNT` aggregate reads the wrong slot: for column
index *i>0* it returns the count of the column **preceding** it in the table's declared order, and for
index 0 — including a one-column table — it degenerates to `COUNT(*)`, ignoring the column's NULLs
entirely. The engine contradicts itself in the same session: `COUNT(*)` and `SUM(q IS NULL)` agree the
answer is 2 while `COUNT(q)` says 3. Any node between `GroupBy` and `Table` (a `Filter` from a real
`WHERE`, a `Sort` from `ORDER BY`), a `GROUP BY`, a derived table, or a second column in the scan all
avoid it. `SUM`/`MIN`/`MAX`/`AVG`/`COUNT(DISTINCT)` over the same scan are correct — only `COUNT` is
affected. **`SELECT COUNT(col) FROM tbl` is one of the most common queries in SQL, and it silently
returns the wrong count.**

## Environment

| | |
|---|---|
| Engine | `dolt version 2.2.3`, binary `dolt-main/bin/dolt` |
| `VERSION()` | `8.0.31` — Dolt's **MySQL compatibility string**, not its own version |
| Access path | `dolt sql-server` **only**; the in-process `dolt sql` CLI is correct |
| Clients | reproduced through **pymysql and the mariadb CLI** against the same server and database, so it is server-side, not a driver artefact |
| Session | all defaults. `sql_mode` and collation are not load-bearing |
| Regression window | **not determined** — only one Dolt build was available (see *Open items*) |

## Minimal repro

```sql
CREATE TABLE u (p BIGINT, q BIGINT, r BIGINT);
INSERT INTO u VALUES (1,NULL,NULL),(2,20,NULL),(3,30,300),(NULL,NULL,NULL);
-- truth: p has 3 non-NULL, q has 2, r has 1; COUNT(*) = 4

SELECT COUNT(q) FROM u;   -- expected 2; Dolt returns 3  (= COUNT(p), the preceding column)
SELECT COUNT(r) FROM u;   -- expected 1; Dolt returns 2  (= COUNT(q))
SELECT COUNT(p) FROM u;   -- expected 3; Dolt returns 4  (= COUNT(*))
```

Even a single-column table is wrong, which is the broadest form:

```sql
CREATE TABLE s (q BIGINT);
INSERT INTO s VALUES (NULL),(20),(30),(NULL);
SELECT COUNT(q) FROM s;   -- expected 2; Dolt returns 4  (counts rows, ignores NULLs)
```

Reproduce through the server — `dolt sql` masks it:

```bash
dolt sql-server --host 127.0.0.1 --port 3306 --data-dir /tmp/r &
mysql -h127.0.0.1 -P3306 -uroot -e "CREATE DATABASE d;"
mysql -h127.0.0.1 -P3306 -uroot d -e "
  CREATE TABLE u (p BIGINT, q BIGINT, r BIGINT);
  INSERT INTO u VALUES (1,NULL,NULL),(2,20,NULL),(3,30,300),(NULL,NULL,NULL);
  SELECT COUNT(q) FROM u;"
```

## Expected vs actual

`u` as above unless noted. **The base (plain table) side is the wrong one**; the correct answer was
established three independent ways: counting non-NULLs in the returned rows, the engine's own
`COUNT(*) - SUM(col IS NULL)`, and the equivalence rewrite (which routes around the bug).

| Query | Expected | Actual |
|---|---|---|
| `SELECT COUNT(q) FROM u` | 2 | **3** |
| `SELECT COUNT(r) FROM u` | 1 | **2** |
| `SELECT COUNT(p) FROM u` | 3 | **4** |
| `SELECT COUNT(q) FROM s` (one-column table) | 2 | **4** |
| `SELECT COUNT(q), COUNT(q) FROM u` | (2,2) | **(3,3)** |
| `SELECT COUNT(q), 1 FROM u` | (2,1) | **(3,1)** |
| `SELECT COUNT(q) FROM u WHERE 1=1` | 2 | **3** |
| the finding's own query (`reduced.sql`, `concrete-as-emitted`) | 7 | **6** |
| `SELECT COUNT(q), COUNT(r) FROM u` | (2,1) | (2,1) — correct |
| `SELECT COUNT(*), COUNT(q) FROM u` | (4,2) | (4,2) — correct |
| `SELECT COUNT(q) FROM u WHERE q IS NOT NULL` | 2 | 2 — correct |
| `SELECT COUNT(q) FROM u ORDER BY q` | 2 | 2 — correct |
| `SELECT SUM(q)` / `MIN(q)` / `COUNT(DISTINCT q)` | 50 / 20 / 2 | correct |

## Equivalence construction

**(1) The construct as the builder emits it.** The finding is round 18 of an eqgen same-base fork
round. The base database seeds the 9-column `t` and forks it with `CREATE TABLE t2 AS SELECT * FROM t`;
the equivalent renames `t` aside and rebuilds `t2` through row-preserving builders. The workload query
is as simple as they come:

```sql
SELECT COUNT(t2.c_chr) FROM t2 WHERE (NOT false);
```

`NOT false` folds to TRUE, so no `Filter` survives above the scan, and the scan prunes to `[c_chr]`.
`c_chr` has 7 non-NULL values of 8 rows; the base returns **6**, which is `COUNT(c_txt)` — the column
declared immediately before `c_chr`.

**(2) The load-bearing construct — and the inversion.** Not a builder at all: it is
`GroupBy` × *one-column pruned scan*. The **equivalent is the correct side**, for the same reason as
the `<=`/`>=` bug in `dolt-20260809-004415-lte-gte-null-filter`: the builders wrap `t2` in extra
relations, which puts nodes between the aggregate and the table scan and routes execution off the
faulty path. The plain fork copy takes the faulty path. So the oracle's "only in base" row is Dolt's
wrong answer and the elaborate rewrite is the reference.

The query itself was not rewritten. The outermost SQL that made equivalent `t2` differ from a bare
table was **`QualifyQueryBuilder`** under **`CreateViewBuilder`**:

```sql
CREATE VIEW t2 AS
SELECT … FROM (
  SELECT …, ((ROW_NUMBER() OVER (ORDER BY c_pk)) >= 1) AS eq_q
  FROM t__base_table_67
) AS eq_qsrc
WHERE eq_q;
```

That derived table + window/filter is enough to leave the buggy plan. Round 18's builder mix also
included `TlpPartitionUnionQueryBuilder` / `PartitionUnionQueryBuilder`,
`ScalarIdentityColumnQueryBuilder` (`COALESCE(c,c)`), `CrossJoinFilterAsInnerBuilder`, and
`UnionEmptyRoundTripBuilder`, but those sit under `t__base_table_67` and are not load-bearing — any
intervening node (real `WHERE`, `ORDER BY`, derived table) would have done the same job.

**(3) Reduced away.** The whole equivalence chain; 8 of 9 columns and every type but `BIGINT`; 4 of 8
rows; the `WHERE (NOT false)` (a bare `SELECT COUNT(q) FROM u` is enough, though `WHERE 1=1` is kept as
a control because it is the reduced form of the original predicate).

## Minimal oracle exposure path

- **Object composition arity:** `2`.
- **GCL builder path:** `QualifyQueryBuilder → CreateViewBuilder`.
- **Confidence:** high; both exact current factory class names are recorded in the report for the
  outermost equivalent SQL.
- **Realization:** `CreateViewBuilder` persists the qualify-wrapped relation as the queried object.
- **Workload/data requirements (excluded from arity):** `COUNT(column)`, a directly adjacent one-column
  pruned scan, nullable values, and the target column's original schema position are workload/plan/data
  requirements.
- **Exposure vs. intrinsic trigger:** the arity-2 object path is the oracle contrast that routes around
  the bug and supplies the correct side. It is reduced away from the intrinsic trigger, which is the
  plain base plan `GroupBy` directly over a one-column table scan.

## Characterization

Every row below is a block in `reduced.sql`, re-run against the live engine (18 blocks, all pass).

**What is required:**

| Ingredient | Control that behaves correctly |
|---|---|
| the scan projects exactly ONE column | `COUNT(q), COUNT(r)` → scan `[q r]` → correct |
| `GroupBy` sits directly on the `Table` | real `WHERE` (adds `Filter`) → correct; `ORDER BY` (adds `Sort`) → correct |
| no grouping | `GROUP BY p IS NULL` → correct |
| no intervening relation | derived table with `ORDER BY` → correct |
| the aggregate is `COUNT` | `SUM`/`MIN`/`AVG`/`COUNT(DISTINCT)` on the same scan → correct |

**What does NOT help** — these are the traps: repeating the same aggregate (`COUNT(q), COUNT(q)`),
adding a literal (`COUNT(q), 1`), and a `WHERE` the optimizer folds away (`WHERE 1=1`, or the
finding's `WHERE (NOT false)`) all stay wrong. It is the *scan's* column count and the absence of an
intervening node that matter, not the number of `SELECT` items.

**Decisive plan diff** (`EXPLAIN PLAN`, same server):

```
WRONG                                     correct (real WHERE)
Project [count(u.q)]                      Project [count(u.q)]
 └─ GroupBy select: COUNT(u.q)             └─ GroupBy select: COUNT(u.q)
     └─ Table u  columns: [q]                  └─ Filter (NOT(u.q IS NULL))
                                                   └─ Table u  columns: [q]

correct (two columns)
Project [count(u.q), count(u.r)]
 └─ GroupBy select: COUNT(u.q), COUNT(u.r)
     └─ Table u  columns: [q r]
```

The single change between wrong and right is whether `GroupBy` sits directly on a one-column `Table`
scan. That the pruned scan projects `[q]` while the wrong answer equals the count of `p` points at a
column index that is not remapped onto the pruned row — i.e. the aggregate indexes the table's
original schema position rather than the projected row's. I did not confirm that in source; see
*Open items*.

**Access path.** Same binary, same data, `dolt sql` returns the correct `(2, 1, 3)`, so a fix validated
only through the CLI would not be validated at all. Reproduced through two independent clients on the
server path (pymysql, mariadb CLI), which rules out driver decoding.

**Blast radius.** Wrong reads only, but wide: any `SELECT COUNT(col) FROM tbl` without a surviving
filter is affected, including through a scalar subquery's own result if it is shaped the same way. I
did not find a DML analogue (`COUNT` does not drive `DELETE`/`UPDATE` row selection here), so no data
loss. Not tested: replication, `information_schema` counts, or Dolt's own bookkeeping queries.

**Same family as, but distinct from, the `<=`/`>=` bug** already filed from
`dolt-20260809-004415-lte-gte-null-filter`: both appear only on the server, both need a bare plan over
a minimal scan, and both are masked by any extra node. That one is in
`LessThanOrEqual/GreaterThanOrEqual.EvalValue`; this one is in the `COUNT` aggregation's value path.
Whoever fixes either should look at the whole `ValueRow` fast-path family for the same class of defect.

## How it was found

The eqgen data-equivalence oracle. It holds the workload query fixed and swaps in a relation that is
row- and type-identical to the base table, so any difference in the result multiset is a divergence
with no reference engine and no expected output needed. Round 18 compared a plain
`CREATE TABLE t2 AS SELECT * FROM t` (wrong) against the qualify-view rebuild above (correct).

**Does this bug need our oracle?** No — not to detect or confirm, only as the path that stumbled
into it. Once the data are known, `SELECT COUNT(q) FROM u` is wrong on its face (2 non-NULLs → engine
says 3), and the engine contradicts itself in one session: `COUNT(*) - SUM(q IS NULL)` says 2 while
`COUNT(q)` says 3. A MySQL differential would catch it immediately. What our oracle *did* buy is
keeping the workload query fixed on a bare table while the other fork accidentally added plan nodes;
query-rewrite oracles (TLP/NoREC/EET) that wrap the same query tend to *hide* this bug by inserting
exactly the Filter/Sort/derived-table nodes that route around it. So data equivalence was a good
discovery tool for this plan-shape class, not a requirement for the bug's existence or triage.

* Round 18, seed 1360946956
* Reduced repro: [`reduced.sql`](reduced.sql)
* Original finding: `original_finding.sql` (from `dolt_20260809-052933/mismatch_round18_0.sql`)
* Other findings in the same run are catalogued in the triage summary; this bug accounts for the
  round-18 finding. The run was still in progress when this was written, so the remaining mismatches
  (rounds 1, 7, 11, 16×3, 19) are **not** all attributed yet — see *Open items*.

## Open items

* **Regression window not determined** — only dolt 2.2.3 was on the box. Worth bisecting, especially
  against the go-mysql-server revisions in the local module cache.
* **Source location not pinned.** Unlike the `<=`/`>=` bug I did not trace this to a `file:line`. The
  place to look is the `COUNT` aggregation's `ValueRow`/`EvalValue` path and how the aggregate's column
  index is remapped after projection pruning (`GroupBy` over a pruned `Table` scan).
* **Remaining findings in the run unattributed** — see the triage summary; the run was still producing
  findings while this was written.
