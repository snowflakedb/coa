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

# MySQL: `SELECT DISTINCT ... GROUP BY <same list>` drops a view-derived boolean-OR column from its dedup key, collapsing distinct groups

## Summary

For `SELECT DISTINCT <cols> FROM ... GROUP BY <same cols> ORDER BY ...`, when one of the columns is a
boolean `||` (logical OR — no `PIPES_AS_CONCAT`) expression over a column read through a **merged
VIEW**, and the `FROM` involves a join, MySQL's post-`GROUP BY` `DISTINCT` step silently omits that
expression from its own deduplication key. It then treats rows that share only the *other* projected
column(s) as duplicates of each other, collapsing multiple legitimately-distinct `GROUP BY` groups
into one and discarding every row whose boolean expression evaluated to `0` — keeping only the rows
where it evaluated to `NULL`. `DISTINCT` alone and `GROUP BY` alone (on the identical column list)
are each independently correct; only their combination, over a view, is wrong.

## Environment

- **Engine**: MySQL 9.7.2 (docker image `mysql:9.7.2`).
- **Session**: `sql_mode = STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES`; `utf8mb4` /
  `utf8mb4_0900_bin`. `PIPES_AS_CONCAT` is *not* set (the default), so `||` is logical OR — not
  load-bearing beyond that; the bug is about `DISTINCT`+`GROUP BY` interaction, not the `||` spelling
  itself (any boolean-valued expression over the view's columns should trigger it).

## Minimal repro

See [`reduced.sql`](./reduced.sql):

```sql
CREATE TABLE t (c_pk BIGINT NOT NULL, c_txt VARCHAR(255), c_chr VARCHAR(255));
INSERT INTO t VALUES (1, NULL, NULL), (2, 'a', 'a'), (3, 'o''brien', ''), (4, NULL, 'Zed');
CREATE VIEW t0 AS SELECT * FROM t;

SELECT DISTINCT a.c_pk, (t0.c_txt||t0.c_chr) FROM t a, t0
GROUP BY a.c_pk, (t0.c_txt||t0.c_chr) ORDER BY a.c_pk;
```

## Expected vs. actual

| Query (same 4×4 cross join) | Result |
|---|---|
| `SELECT DISTINCT a.c_pk, (t0.c_txt\|\|t0.c_chr) FROM t a, t0 ORDER BY a.c_pk` (DISTINCT, no GROUP BY) | 8 rows: `(1,∅),(1,0),(2,∅),(2,0),(3,∅),(3,0),(4,∅),(4,0)` ✓ |
| `SELECT a.c_pk, (t0.c_txt\|\|t0.c_chr) FROM t a, t0 GROUP BY a.c_pk, (t0.c_txt\|\|t0.c_chr) ORDER BY a.c_pk` (GROUP BY, no DISTINCT) | same 8 rows ✓ |
| **`SELECT DISTINCT a.c_pk, (t0.c_txt\|\|t0.c_chr) FROM t a, t0 GROUP BY a.c_pk, (t0.c_txt\|\|t0.c_chr) ORDER BY a.c_pk`** (both) | **4 rows, all NULL** — every `0`-valued row is gone ✗ |
| Same query, `t0` replaced by an equivalent materialized `TABLE` (control) | correct 8 rows ✓ |

`t0`'s own four `(c_txt||c_chr)` values are `{NULL, 0, 0, NULL}` — both `0` and `NULL` genuinely
occur, confirmed by a plain `SELECT c_pk, (c_txt||c_chr) FROM t0` (4 rows, 2 zeros and 2 NULLs). The
`GROUP BY`-alone query already proves MySQL *can* correctly enumerate both value classes for every
`a.c_pk`; only adding `DISTINCT` on top of the identical `GROUP BY` list breaks it.

## Minimal oracle exposure path

- **Object composition arity:** **2**
- **GCL builder path:** `SelectStarQueryBuilder` → `CreateViewBuilder`
- **Confidence:** Verified — the report identifies the final `SelectStarQueryBuilder`/`CreateViewBuilder` identity-view exposure, and the reduced repro keeps exactly that boundary.
- **Realization:** `CreateViewBuilder` exposes `SELECT *` as the mergeable view used by the join.
- **Workload/data requirements (excluded from arity):**
  - `SELECT DISTINCT` and `GROUP BY` over the same projected list.
  - A join involving a view-derived boolean expression.
  - Data producing at least two distinct boolean classes for the same other dedup key.

**Exposure vs. intrinsic trigger:** The identity-view path creates the object contrast after the earlier builders are removed. The intrinsic trigger is the merged view-derived expression interacting with the redundant DISTINCT/GROUP BY plan so the outer dedup key drops that expression.

## Mechanism (`EXPLAIN FORMAT=TREE`)

```
-- buggy: t0 is a VIEW
-> Sort: a.c_pk, ((0 <> t.c_txt) or (0 <> t.c_chr))
    -> Sort with duplicate removal: a.c_pk                      <-- DISTINCT's dedup key is `a.c_pk` ALONE
        -> Table scan on <temporary>
            -> Temporary table with deduplication                <-- GROUP BY, correct
                -> Inner hash join (no condition)
                    -> Table scan on t
                    -> Hash -> Table scan on a

-- control: t0 is a TABLE
-> Sort: a.c_pk, (t0.c_txt||t0.c_chr)
    -> Table scan on <temporary>
        -> Temporary table with deduplication                    <-- GROUP BY
            -> Inner hash join (no condition)
                -> Table scan on t0
                -> Hash -> Table scan on a
```

The control plan has **no separate dedup-sort node** at all — `DISTINCT` is folded away because the
`GROUP BY` already guarantees uniqueness (the standard, correct optimization). The buggy (view) plan
instead inserts an explicit `Sort with duplicate removal: a.c_pk` **on `a.c_pk` alone**, omitting the
second projected column `((0 <> t.c_txt) or (0 <> t.c_chr))` from its key entirely (note this is
MySQL's internal rewrite of `x||y` into `(x<>0) OR (y<>0)`, and note it also renamed the correlation
from `t0` to the merged base table `t` — a symptom of the view being inlined/flattened into the outer
query before this planning step runs). With only `a.c_pk` as the key, every row sharing a `c_pk` — one
from the `NULL` group, one from the `0` group, already correctly produced by the inner `GROUP BY`
step — looks like a duplicate to this second sort, and one of the two is discarded; the survivor is
consistently the `NULL`-valued one across all runs (verified stable across 4 repeats).

This is a distinct defect from repro `mysql-run6-round161-having-after-distinct-order-dependent`
(also a `DISTINCT`+`GROUP BY` interaction bug in this project's history): that one is about `HAVING`
reading a stale post-dedup aggregate value and is order-dependent on physical row placement; this one
has no `HAVING` and no aggregate function at all, is stable/order-independent (same wrong answer
every run), and is specifically triggered by the `DISTINCT` step's dedup key silently dropping a
column when that column is sourced through a merged view.

## Systematic, not a one-off

The same shape recurred independently in a second, unrelated fuzzing round with a `RIGHT JOIN`,
`<=>`, two `||` expressions, and a `HAVING TRUE` (`mismatch_round272_0.sql`, seed 730263766) —
different join type, different projected expressions, same signature: `SELECT DISTINCT` +
`GROUP BY <same list>` over a view-derived boolean expression drops every non-NULL-valued group,
`only in equivalent: (none)`. Both reduce to the identical mechanism above.

## Triage gates

- **Admissibility**: verified directly — `t0` (VIEW) and `t` (its own base table) hold byte-identical
  rows; not a row-preservation defect in eqgen's builder chain.
- **Determinism**: stable across repeated runs, same wrong answer every time (not a tie/physical-order
  artifact — no `LIMIT`, no non-unique `ORDER BY` key, no aggregate involved at all).
- **Not a comparability gap**: `DISTINCT` and `GROUP BY` on an identical, fully-specified column list
  have exactly one correct SQL-standard answer (the `GROUP BY`-alone query's 8 rows); there is no
  legitimate multiple-right-answer ambiguity here for the skill's "vary only the plan" exemption to
  apply to.
- **Blast radius**: read-only `SELECT`; not tested against `DELETE`/`UPDATE` (no rows at risk).

## How it was found

eqgen's data-equivalence oracle built a long builder chain culminating in `t0` exposed as a `VIEW`
(reached via `CreateViewBuilder`/`SelectStarQueryBuilder` after an `EXCEPT ALL` round-trip and a
`ROW_NUMBER`-tagged window pass) and `t1` similarly. The random workload query
(`SELECT DISTINCT t1.c_pk, (- t1.c_int), ((t0.c_txt)||(t0.c_chr)) FROM t1, t0 WHERE (NOT false) GROUP
BY t1.c_pk, (- t1.c_int), ((t0.c_txt)||(t0.c_chr)) ORDER BY t1.c_pk`) diverged from the base table's
answer (16 distinct rows on base vs. 8 on the equivalent, `only in equivalent: none`). Delta-debugging
against the live engine — cutting statements while an admissibility check confirmed `t0`/`t1` still
held 8 rows, then substituting a trivial `t1` and inlining `t0`'s source directly — collapsed the
~60-statement equivalent chain down to "one table, one view over it, one cross join, one boolean-OR
column, `DISTINCT`+`GROUP BY`" without losing the divergence, which is the repro above.
