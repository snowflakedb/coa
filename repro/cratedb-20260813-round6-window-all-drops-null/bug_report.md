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

# CrateDB: `NULL = ALL (empty)` drops the row when the compared column is a window output

## Summary

`x = ALL (empty subquery)` is vacuously TRUE in SQL, including when `x` is NULL. On CrateDB 6.4.1
and 6.4.2 a Collect (plain table or plain view) honours that and keeps the NULL row. The same
predicate sitting as a `Filter` above `WindowAgg` — which is what you get from a view or inline
derived table whose SELECT list replaces `x` with `MAX(x) OVER …` / `MIN` / `SUM` — treats
`NULL = ALL (empty)` as UNKNOWN and **silently drops the row**. The window is an identity (MAX over
a singleton partition); `SELECT *` on the view returns the row. No error. `#19855`'s two optimizer
toggles do not mask it.

## Environment

| | |
|---|---|
| Engine | CrateDB **6.4.1** (built `45bfa80`) and **6.4.2** (built `1db6455`), official Docker images |
| Session | defaults (`error_on_unknown_object_key = true`, `insert_select_fail_fast = true`; neither is load-bearing) |
| Access path | PostgreSQL wire via `psycopg` 3.3.4 |
| Shards | `CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0)` |
| Determinism | stable across repeats |
| DML | views reject `DELETE`/`UPDATE`; `DELETE FROM` the base table with the same ALL predicate does **not** remove the NULL row (matches the Collect SELECT) |

## Minimal repro

```sql
CREATE TABLE b (id BIGINT, x INT)
    CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (NULL, 1);
REFRESH TABLE b;
CREATE VIEW t AS SELECT id, x FROM (
  SELECT MAX(id) OVER (PARTITION BY x) AS id, x FROM b
) s;

SELECT * FROM t;                                              -- (NULL, 1)   row is present
SELECT id FROM b WHERE id = ALL (SELECT id FROM b WHERE FALSE); -- (NULL)     correct
SELECT id FROM t WHERE id = ALL (SELECT id FROM t WHERE FALSE); -- Expected (NULL)
                                                                -- Actual   empty     <<< WRONG
```

`MAX(id) OVER (PARTITION BY x)` on a one-row table is an identity: the view holds the same `(NULL, 1)`
as `b`. The ALL subquery is empty by construction. Reproduces identically if the view is replaced by
an inline derived table (no `CREATE VIEW`). Reproduces on 6.4.2.

The full controls and the builder-emitted 8-row shape are in `reduced.sql`; re-check by running its
blocks against `crate:6.4.1`.

## Expected vs actual

The **base table / plain view** is the correct side (SQL vacuous truth). The window view is wrong —
it returns too few rows.

| query | plain / Collect (correct) | window view (actual) |
|---|---|---|
| `SELECT * FROM t` | `(NULL, 1)` | `(NULL, 1)` (ok — row exists) |
| `id = ALL (SELECT id FROM t WHERE FALSE)` | `(NULL)` | **empty** |
| `id = ALL (SELECT 1 WHERE FALSE)` | `(NULL)` | **empty** |
| `id IS NULL` | `(NULL)` | `(NULL)` (ok) |
| same window, `id = 1` instead of NULL | `(1)` | `(1)` (ok — NULL-specific) |
| window a *different* column (`MAX(x)`, ALL still on `id`) | `(NULL)` | `(NULL)` (ok) |
| 8-row finding, `GROUP BY id, created_at, name` + ALL empty | 7 groups | **6** (the all-NULL row is gone) |

Scalar `SELECT (NULL = ALL (SELECT id FROM t WHERE FALSE))` returns NULL on **both** sides — CrateDB
does not implement vacuous ALL as TRUE in scalar context. The Collect WHERE path nonetheless keeps
the row; the Filter-above-WindowAgg path agrees with the scalar (UNKNOWN → drop). The two plans
disagree with each other; SQL and the Collect path say keep.

## Equivalence construction

eqgen's oracle builds a second relation with the same rows and declared types as base `t`, then runs
the same query on both. Here the equivalent `t` is a key-expand / `UNION ALL` copy / window-collapse
view (`DISTINCT eq_key, MAX(col) OVER (PARTITION BY eq_key)` for every column) — the identity
round-trip the harness uses to force a `WindowAgg` into the plan. Distilled, one `MAX(id) OVER
(PARTITION BY x)` in a view (or an inline derived table) is enough. UNION ALL copies, `GROUP BY`,
`CASE`/`coalesce`/`NOT IN (self)`, `PARTITIONED BY`, `INDEX OFF`, and `FULL OUTER JOIN` all reduced
away.

Load-bearing composition: **window output used as the left operand of `= ALL (empty)`** × **NULL
left operand**. Extra unused windows (`ROW_NUMBER() OVER ()` as a projected-away column) are *not*
sufficient on the 2-column distilled schema — the ALL operand stays the source `id` and the window
is pruned back to a Collect.

Constructs reduced away: `UNION ALL` explode, `ROW_NUMBER` key, `GROUP BY` (needed on the 8-row
shape only because that window replaced `created_at`, not `id`), the original `created_at NOT IN
(created_at)` emptiness gadget (`WHERE FALSE` and `SELECT 1 WHERE FALSE` both trigger), generated
partition columns.

The sibling corpus query (the fat `FULL OUTER JOIN` + `created_at = ALL (…)` SELECT) mismatches in
the same direction on the same equivalents; it was not reduced separately. It is the same ALL filter
over the same windowed `t`, plus join/window noise.

## Minimal oracle exposure path

**Object composition arity:** `3`

**GCL builder path:** `KeyExplodeExpansionBuilder` → `KeyWindowAggregateReduceBuilder[VIEW]`

**Confidence:** verified

**Realization:** The reducer exposes the collapsed, row-identical relation as a `VIEW`.

**Workload/data requirements (excluded from arity):**
- A NULL value in the column rewritten as a window output.
- That window output as the left operand of `= ALL`.
- An empty right-hand subquery.
- A singleton-equivalent partition so the window rewrite preserves rows.

**Exposure vs. intrinsic trigger:** The expansion builder and its duplicate rows reduce away from the standalone trigger; the intrinsic plan need only an identity window output beneath the `= ALL (empty)` filter. The expansion/reducer pair supplied the row-identical windowed contrast, and the reducer’s window shape remains represented by the distilled inline or view form.

## Characterization

**Plan diff** (1-row distilled, `EXPLAIN` of `SELECT id FROM t WHERE id = ALL (SELECT id FROM t WHERE FALSE)`):

Correct, plain view (`rows=unknown`, result `(NULL)`):

```
MultiPhase
  └ Rename[id] AS t
      └ Collect[b | [id] | (id = ALL((SELECT id FROM (b))))]
  └ … subquery: Collect[b | [id] | false]
```

Wrong, window view (`Filter` estimated `rows=0`, result empty):

```
MultiPhase
  └ Rename[id] AS t
      └ Rename[id] AS s
          └ Eval[max(id) OVER (PARTITION BY x) AS id]
              └ Filter[(max(id) OVER (PARTITION BY x) AS id = ALL((SELECT id FROM (s))))]
                  └ WindowAgg[id, x] | [max(id) OVER (PARTITION BY x)]
                      └ Collect[b | [id, x] | true]
  └ … subquery also goes through WindowAgg, Collect filter `false`
```

The predicate text is the same ALL. The Collect path attaches it to the shard scan and keeps NULL;
the WindowAgg path evaluates it as a Filter on the window output and drops NULL. `MAX` / `MIN` /
`SUM`, `OVER ()` or `OVER (PARTITION BY x)`, view or inline — all the same Filter-above-WindowAgg
shape.

`SET optimizer_merge_filter_and_collect = false` and
`SET optimizer_move_filter_beneath_rename = false` (the pair that fixes
[crate/crate#19855](https://github.com/crate/crate/issues/19855)) leave this wrong. So do
`optimizer_move_filter_beneath_window_agg`, `optimizer_move_filter_beneath_eval`, and
`optimizer_move_filter_beneath_multi_phase`. The window plan already has the Filter *above*
WindowAgg; this is not the partition-table Collect-merge bug.

`SELECT (NULL = ALL (empty))` is NULL on CrateDB in both plans, so a maintainer looking only at the
scalar will think UNKNOWN is intended. The Collect WHERE path is the existence proof that the engine
already has a TRUE-for-NULL-ALL-empty implementation — it just is not used once a WindowAgg sits
under the filter.

The defect is broader than `WindowAgg`. A CrateDB 6.4.2 continuation run reproduced the same
Collect-vs-Filter disagreement through all three key reducers and an expression projection:

- `KeyWindowAggregateReduceBuilder` (`MAX(col) OVER (PARTITION BY key)`);
- `KeyQualifyDedupReduceBuilder` (boolean `ROW_NUMBER() = 1` filter view);
- `KeyGroupAggregateReduceBuilder` (`ANY_VALUE(col) GROUP BY key`);
- `EetCaseColumnQueryBuilder` (tautological `CASE` projection).

For each, `SELECT *` remains row-identical, while a reduced
`WHERE name = ALL (SELECT name FROM t WHERE FALSE)` loses exactly the rows whose `name` is NULL.
Materializing the reducer output restores the Collect path. The underlying split is therefore
**Collect predicate evaluation vs a standalone Filter over an equivalent relation**, not the
window function itself.

## How it was found

eqgen data-equivalence oracle, corpus replay of previously-passing queries against CrateDB storage/window builders. Run
`crate_corpus/cratedb_20260813-233946/`. Seed is not replay-stable; the `.sql` files are
the source of truth.

A query-rewrite oracle (TLP / NoREC / EET) would miss this: it holds the data fixed as a plain
table, and its rewrites dismantle the window view that is the trigger. The equivalence oracle keeps
the query still and swaps in a row-identical windowed `t`.

All **14** mismatches in the run are this bug, two query texts × many object chains:

- `mismatch_round*_1.sql` — the `id = ALL (… created_at NOT IN (created_at) …)` query (empty ALL).
- `mismatch_round*_0.sql` — the fat join query whose WHERE is also `= ALL (subquery)` over the same
  windowed `t`.

Proven by reducing `_1` to PART 1 and checking that a `PARTITIONED BY` generated column alone does
not reproduce, that `#19855`'s rule toggles do not fix it, and that every `_1` finding is the same
SQL.

- Repro and controls: [`reduced.sql`](reduced.sql)
- Original finding: `crate_corpus/cratedb_20260813-233946/mismatch_round6_1.sql`
  (first occurrence: `mismatch_round0_1.sql`)

### 6.4.2 continuation-run accounting

Deduplication of
`eqgen/log/crate_simple_shuffle_keytag/cratedb_20260819-172428` through round 277 found **54**
additional mismatch files in this family. They use several large corpus queries, but live layer
bisection always switches at one of the reducer/expression views above; replacing the workload with
the reduced ALL-empty predicate reproduces the same NULL-row loss. These are one bug, not 54 new
reports.

## Open items

- Regression window not bisected (fails on 6.4.1 and 6.4.2; no older image exercised this session).
- Whether Collect's TRUE-for-NULL-ALL-empty or the scalar's UNKNOWN is the *intended* CrateDB
  contract — either way the two plans must agree, and SQL says TRUE.
- The fat `_0` query was not reduced on its own; treated as the same ALL-over-windowed-`t` miss.
- Suggested fix: evaluate `= ALL (empty)` as TRUE (including NULL left operand) in the Filter /
  WindowAgg path the same way Collect already does; or fold empty ALL before it reaches
  WindowAgg.
