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

# CrateDB: `optimizer_rewrite_filter_on_outer_join_to_inner_join` pushes a non-null-rejecting filter into the left input of a FULL OUTER JOIN without downgrading the join — silent wrong rows

## Summary

`optimizer_rewrite_filter_on_outer_join_to_inner_join` exists to downgrade an outer join to an
inner/one-sided join when the filter above it is null-rejecting — which then licenses pushing that
filter into an input. On CrateDB 6.4.1 it pushes the filter into the **left input of a FULL OUTER
JOIN** while leaving the join type as `FULL`. The join therefore still null-extends exactly the rows
the pushed filter removed, and the (correctly retained) `Filter` above the join then *passes* them.

The filter here is `coalesce(l.id, 15) NOT IN (SELECT …)`, which is **not** null-rejecting: it is TRUE
when `l.id IS NULL`, because `coalesce(NULL, 15) = 15` and 15 is not in the subquery result. So the
precondition for the rewrite does not hold, yet the pushdown happens.

One table, one column, one row reproduce it. No error is raised. `SET
optimizer_rewrite_filter_on_outer_join_to_inner_join = false` fixes both the minimal repro and the
original 8-row finding.

## Environment

| | |
|---|---|
| Engine | CrateDB **6.4.1** (official release tarball) |
| Session | all defaults; the rule is `true` by default. No other setting is load-bearing |
| Determinism | deterministic |
| Origin | `logs/cratedb_run3/mismatch_round6_0.sql`; admissibility verified (base `t` ≡ equivalent `t`, 8 identical rows) |

## Minimal repro

```sql
CREATE TABLE b (id BIGINT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (2);
REFRESH TABLE b;

SELECT l.id FROM b l FULL OUTER JOIN b r ON l.id = r.id
WHERE coalesce(l.id, 15) NOT IN (SELECT t2.id FROM b t2 WHERE t2.id = 2);
```

The only row has `id = 2`; `coalesce(2, 15) = 2`, which **is** in the subquery result `{2}`, so
`NOT IN` is false and the correct answer is **zero rows**. The join key is unique on both sides, so a
FULL OUTER JOIN cannot legitimately null-extend anything.

## Expected vs actual

| | Expected | Actual |
|---|---|---|
| **minimal repro** | 0 rows | **1 row: `(NULL)`** |
| minimal repro + `SET optimizer_rewrite_filter_on_outer_join_to_inner_join = false` | 0 rows | 0 rows |
| the FULL OUTER JOIN alone, unfiltered | `(2)` | `(2)` |
| original 8-row finding on base `t` | 6 rows | 6 rows |
| original 8-row finding on the row-identical equivalent `t` | 6 rows | **8 rows** |
| …same, with the rule disabled | 6 rows | 6 rows |

## The decisive plan diff

Same data, same query. The `Filter[…]` above the join and `NestedLoopJoin[FULL | (id = id)]` are
present in **both** plans — the rule pushed the filter down but did **not** downgrade the join. The
only difference is the left `Collect`'s filter:

**Default (wrong — 1 row):**

```
Filter[(NOT (coalesce(id, 15::bigint) = ANY((SELECT id FROM (t2)))))]
  └ NestedLoopJoin[FULL | (id = id)]
    ├ Collect[b | [id] | (NOT (coalesce(id, 15::bigint) = ANY((SELECT id FROM (t2)))))]   <-- pushed in
    └ Collect[b | [id] | true]
```

**`optimizer_rewrite_filter_on_outer_join_to_inner_join = false` (correct — 0 rows):**

```
Filter[(NOT (coalesce(id, 15::bigint) = ANY((SELECT id FROM (t2)))))]
  └ NestedLoopJoin[FULL | (id = id)]
    ├ Collect[b | [id] | true]
    └ Collect[b | [id] | true]
```

Tracing the wrong plan by hand: the left `Collect` drops `id = 2`; the right side still yields
`id = 2`; `FULL` null-extends it to `(l.id = NULL, r.id = 2)`; the retained `Filter` computes
`coalesce(NULL, 15) = 15`, which is not in `{2}`, so `NOT IN` is true and the row is emitted as
`(NULL)`.

## Equivalence construction

The equivalent is four statements — the **column-split + FULL-OUTER-JOIN-rejoin builder**, verbatim
from the finding:

```sql
ALTER TABLE t RENAME TO t__base;
CREATE TABLE t__base_table_1 (id BIGINT, name TEXT, created_at TEXT, eq_seq_key_1 BIGINT) …;
INSERT INTO t__base_table_1 (id, name, created_at, eq_seq_key_1)
  SELECT id, name, created_at, ROW_NUMBER() OVER (ORDER BY id) AS eq_seq_key_1 FROM t__base;
CREATE VIEW t__base_view_1 AS SELECT id, eq_seq_key_1 FROM t__base_table_1;
CREATE VIEW t__base_view_2 AS SELECT name, created_at, eq_seq_key_1 FROM t__base_table_1;
CREATE VIEW t AS SELECT l.id AS id, r.name AS name, r.created_at AS created_at
                 FROM t__base_view_1 l FULL OUTER JOIN t__base_view_2 r
                   ON l.eq_seq_key_1 = r.eq_seq_key_1;
```

Row-preserving because `eq_seq_key_1` is a `ROW_NUMBER`, unique on both sides — the FULL JOIN is a
pure round-trip. The workload query's load-bearing fragment is its `WHERE`:

```sql
WHERE coalesce(CEIL(t1.id), 15) NOT IN (
        SELECT DISTINCT CASE WHEN t2.name IS NOT NULL THEN t2.id + t2.id
                             ELSE CAST(13.89822 AS DOUBLE PRECISION) END
        FROM t AS t2 WHERE CAST(t2.id AS BOOLEAN))
```

**Mapping onto the minimal repro:** the split/rejoin view → `b l FULL OUTER JOIN b r ON l.id = r.id`;
`eq_seq_key_1` → `id` (the join key and the projected column collapse into one column);
`coalesce(CEIL(t1.id), 15)` → `coalesce(l.id, 15)` (`CEIL` is irrelevant); the `DISTINCT`/`CASE`
subquery → `SELECT t2.id FROM b t2 WHERE t2.id = 2`.

Reduced away: the two split views, the `l.X AS X` self-aliases, the wrapping `CREATE VIEW`, `CEIL`,
the `DISTINCT` and `CASE` in the subquery, the outer `GROUP BY`, the other nine select items
(two window functions and three scalar subqueries), two of the three columns, and seven of the eight
rows.

## Minimal oracle exposure path

**Object composition arity:** `2`

**GCL builder path:** `SequenceOuterJoinQueryBuilder` → `CreateViewBuilder`

**Confidence:** verified

**Realization:** A `VIEW` exposes the row-preserving column-split/full-outer-join reassembly.

**Workload/data requirements (excluded from arity):**
- A `FULL OUTER JOIN` relation with matching unique keys.
- A subquery-backed anti predicate (`NOT IN` or `<> ALL`).
- A non-null-rejecting wrapper such as `coalesce(left_col, literal)`.
- At least one row rejected before the join so null-extension exposes the pushdown error.

**Exposure vs. intrinsic trigger:** The full-outer-join relation supplied by `SequenceOuterJoinQueryBuilder` remains in the standalone trigger, but its row-number key, split views, and outer `CreateViewBuilder` wrapper reduce away. The builders created the row-identical contrasting relation; the intrinsic defect is the unsound anti-predicate pushdown into the left input while the join remains `FULL`.

## Characterization

Every row below was measured on the one-row minimum.

| variant | result |
|---|---|
| **minimal repro** | **`(NULL)`** ✗ |
| the FULL OUTER JOIN alone, unfiltered | `(2)` ✓ |
| a benign non-subquery filter over the same join (`coalesce(l.id,15) >= -100`) | `(2)` ✓ |
| `INNER JOIN` | 0 ✓ |
| `LEFT OUTER JOIN` | 0 ✓ |
| `RIGHT OUTER JOIN` | 0 ✓ |
| no `coalesce` (`l.id NOT IN …`) | 0 ✓ — damage hidden, not absent |
| `coalesce(l.id, l.id)` (nullable default) | 0 ✓ |
| `IN` instead of `NOT IN` | `(2)` ✓ |
| `NOT EXISTS` instead of `NOT IN` | 0 ✓ |
| **`<> ALL` instead of `NOT IN`** | **`(NULL)`** ✗ — the quantified/anti-join form, not the `NOT IN` spelling |
| a **constant** `IN`-list `(2)` | 0 ✓ |
| a constant `IN`-list that also contains coalesce's default, `(2, 15)` | 0 ✓ |
| **the subquery UNFILTERED** | **`(NULL)`** ✗ — a *subquery* is required; its filter is not |
| two **different** tables rather than a self-join | `(NULL)` ✗ |
| `SET optimizer_rewrite_filter_on_outer_join_to_inner_join = false` | 0 ✓ |
| `SET optimizer_merge_filter_and_collect = false` | `(NULL)` ✗ |
| `SET optimizer_move_filter_beneath_join = false` | `(NULL)` ✗ |

So the necessary ingredients are: a **FULL OUTER JOIN** as the queried relation, an **anti-join
predicate over a subquery** (`NOT IN` / `<> ALL`; a constant list or `NOT EXISTS` will not do), and a
**non-null-rejecting wrapper** on the left-hand side (`coalesce(col, <non-null literal>)`).

`coalesce` **exposes** the bug rather than causing it: without it the null-extended row evaluates
`NULL NOT IN (…)` → `NULL` and is silently re-filtered, so the corruption is invisible. Any wrapper
that makes the predicate true for a null-extended row will expose it.

**Corrections to an earlier revision of this report** (both were wrong, both are now measured):

- An **unfiltered subquery is *not* a passing control** — it fails identically. The earlier claim that
  "the subquery must have a filter that excludes coalesce's default" is wrong; what matters is
  subquery-vs-constant-list.
- The mechanism is **not** a generic pushdown. It is specifically
  `optimizer_rewrite_filter_on_outer_join_to_inner_join`; `optimizer_merge_filter_and_collect` and
  `optimizer_move_filter_beneath_join` both leave the bug in place. (`merge_filter_and_collect` is the
  rule that fixes the *sibling* finding `cratedb-run4-round341`, which is a different defect.)

## How it was found

eqgen v3 data-equivalence oracle, `cratedb_run3` round 6, seed 649806657. Base `t` (a plain table)
and equivalent `t` (the column-split + FULL-JOIN rejoin) hold the same 8 rows; the same workload query
returned 6 rows against the base and 8 against the equivalent.

The trigger is a property of the *relation* — it has to be a `FULL OUTER JOIN` — which the base table
cannot express, so no amount of query rewriting over the original table reaches it. The rejoin is also
exactly the kind of relation a human would not hand-write but an equivalence generator emits by
construction. And the wrong row is an extra `NULL`-filled row that looks like ordinary outer-join
padding, so on a single relation it reads as plausible output; only a second, row-identical relation
makes it obviously wrong.

Note the same round's sibling, `cratedb_run3/mismatch_round14_0.sql`, has the **byte-identical**
equivalence chain and is a *different* bug (the bare-boolean `WHERE` defect) — identical construction
is not identical cause.

- Repro, controls and plan diff: [`reduced.sql`](reduced.sql) — 23 queries, each verified on 6.4.1
- Original finding: hunt log
