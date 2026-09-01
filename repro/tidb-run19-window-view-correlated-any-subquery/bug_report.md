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

# TiDB: an aggregate window function inside a view breaks correlated quantified subqueries (`< ANY` / `= ALL` / `IN (SELECT …)`) — the correlated predicate stops filtering

## Summary

When a relation is a **view whose body computes an aggregate window function**
(`MAX(col) OVER (PARTITION BY k)`), a query that outer-joins that relation to itself and filters with a
**correlated quantified comparison subquery** returns wrong results. The correlated predicate inside
the subquery stops constraining it: rows that must be eliminated survive, or (in the mirror case) rows
that must survive are eliminated. No error, no warning.

The same relation expressed as `GROUP BY k` + `MAX(col)` — identical semantics, byte-identical row
multiset — is **correct**, as is the same view body with a *ranking* window function
(`ROW_NUMBER() OVER (ORDER BY id)`). So neither "it's a view" nor "it's a MAX" is the trigger: it is
specifically an **aggregate window function in the view body**. On the query side, `EXISTS` is correct
and a correlated **scalar** subquery is correct — only the **quantified** forms break. Adding the
correlated column to the outer `SELECT` list also makes it correct.

This is **five of the 29 mismatches in `tidb_run19`** (rounds 1795, 2990, 3188, 3621, 3654). Each
round's whole multi-link equivalence chain can be replaced by one minimal window view and the
divergence still reproduces, which is what establishes them as one root cause. The other 24 are a
separate, already-known bug ([`../tidb-run19-round3247-mod-view-restore-parens`](../tidb-run19-round3247-mod-view-restore-parens/bug_report.md)).

## Environment

| | |
|---|---|
| Engine | tidb `8.0.11-TiDB-v9.0.0-beta.2.pre-2051-g3bea8196a5` @ `3bea8196`, unistore, assertions off |
| `sql_mode` | `STRICT_ALL_TABLES,NO_ZERO_DATE,NO_ZERO_IN_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION,ONLY_FULL_GROUP_BY,NO_BACKSLASH_ESCAPES` — **not load-bearing** |
| Charset / collation | `utf8mb4` / `utf8mb4_0900_bin` — not load-bearing |
| Determinism | deterministic; **2 rows** suffice for the minimal repro |
| Store | `unistore`; not verified against a real TiKV cluster (none available) |
| Admissibility | all five findings pass: base `t` and equivalent `t` are row-identical (`replay.py`), and the one-view substitute relation is row-identical to a plain table |

## Minimal repro

```sql
CREATE TABLE k (id BIGINT, name VARCHAR(255), created_at VARCHAR(255), eq_key_1 BIGINT);
INSERT INTO k (id, name, created_at, eq_key_1) VALUES (1, 'a', 'a', 1);
INSERT INTO k (id, name, created_at, eq_key_1) VALUES (NULL, 'b', 'b', 2);

CREATE VIEW t AS SELECT id, name, created_at FROM (
  SELECT eq_key_1,
         MAX(id)         OVER (PARTITION BY eq_key_1) AS id,
         MAX(name)       OVER (PARTITION BY eq_key_1) AS name,
         MAX(created_at) OVER (PARTITION BY eq_key_1) AS created_at
  FROM k) AS w;

SELECT t2.id
FROM t AS t2 RIGHT OUTER JOIN t AS t3 ON t2.id >= t3.id
WHERE (t2.id IS NULL) < ANY (SELECT 1 FROM t AS t4 WHERE t3.id IS NULL)
GROUP BY t2.id;
-- Expected: 0 rows.   Actual: 1 row, (1).
```

**Why 0 is the correct answer.** `t3` is the preserved side of the `RIGHT OUTER JOIN`, so every output
row has a non-NULL-or-NULL `t3.id` drawn from `t`, and `t2` is null-padded where nothing matched.

- For `t3.id = 1`: the subquery `SELECT 1 FROM t t4 WHERE t3.id IS NULL` is **empty**, and
  `x < ANY (empty set)` is **FALSE** by definition. No row with `t3.id = 1` may survive.
- For `t3.id = NULL`: no `t2` row satisfies `t2.id >= NULL`, so `t2` is null-padded and the left
  operand is `(NULL IS NULL)` = `1`. The subquery yields `{1}`, so the test is `1 < 1` — false.

Nothing survives either way. Returning `(1)` means the correlated `t3.id IS NULL` did not filter the
subquery for the `t3.id = 1` group.

## Expected vs actual

| Relation holding the same rows | Round 1795's query | 2990 | 3188 | 3621 | 3654 (`COUNT(*)`) |
|---|---|---|---|---|---|
| plain table (**correct**) | 0 rows | 36 rows | 1 row | 30 rows | 72 |
| view: `MAX() OVER (PARTITION BY k)` + `DISTINCT` | **6 rows** | **0** | **0** | **0** | **240** |
| view: `MAX() OVER (PARTITION BY k)`, no `DISTINCT` | **6 rows** | **0** | **0** | **0** | **240** |
| view: `GROUP BY k` + `MAX()` aggregate | 0 rows | 36 | 1 | 30 | 72 |
| view: `DISTINCT`, no window function | 0 rows | 36 | 1 | 30 | 72 |
| view: `ROW_NUMBER() OVER (ORDER BY id)` | 0 rows | 36 | 1 | 30 | 72 |

Every relation in that table was checked row-identical to the plain table before use. The `plain table`
row is taken as correct: for round 1795 it is independently derivable (the argument above), and for the
others it agrees with all four non-triggering view shapes.

Note the two directions. Rounds 2990 / 3188 / 3621 lose rows (the quantified predicate becomes
unsatisfiable); rounds 1795 and 3654 gain them (it stops filtering). Both are consistent with "the
correlated quantified subquery is mis-evaluated", and the direction follows from whether the query's
predicate is satisfied by the empty or the non-empty subquery result.

## Equivalence construction

### (1) The construct as the eqgen builder emits it

All five rounds end their chain with the **same** final link — the duplicate-and-reduce builder's
window-based reduce step. Verbatim from `logs/tidb_run19/mismatch_round1795_0.sql`:

```sql
ALTER TABLE t RENAME TO t__base;
-- key each row, duplicate it 100x, then collapse back by taking MAX per key
CREATE TABLE t__base_table_1 (…, `eq_key_2` BIGINT);
INSERT INTO t__base_table_1 (…) SELECT id, name, created_at, ROW_NUMBER() OVER (ORDER BY id) FROM t__base;
CREATE TABLE t__base_table_2 (…);
INSERT INTO t__base_table_2 … SELECT … FROM t__base_table_1
  UNION ALL SELECT … FROM t__base_table_1 CROSS JOIN (WITH RECURSIVE eq_gen_series … ) AS eq_gen;
INSERT INTO t__base_table_2 SELECT … FROM (SELECT … FROM t__base_table_2) AS eq_src CROSS JOIN (…) AS eq_gen;
CREATE VIEW t__base_view_1 AS SELECT ANY_VALUE(id) AS id, … FROM t__base_table_2 GROUP BY eq_key_2;
-- … keyed again, duplicated again …
CREATE VIEW t AS SELECT id, name, created_at FROM (
  SELECT DISTINCT eq_key_1,
         MAX(id) OVER (PARTITION BY eq_key_1) AS id,
         MAX(name) OVER (PARTITION BY eq_key_1) AS name,
         MAX(created_at) OVER (PARTITION BY eq_key_1) AS created_at
  FROM t__base_table_4) AS eq_reduced;        -- <-- the load-bearing link
```

and the workload query (round 1795), which references `t` six times:

```sql
SELECT t2.id FROM t AS t1 RIGHT OUTER JOIN t AS t2 ON t1.name = t2.created_at
                          RIGHT OUTER JOIN t AS t3 ON t2.id >= t3.id
WHERE if(CAST('2014-04-04T10:10:10.10' AS CHAR(255)) IS NULL, …,
         (CASE WHEN t2.id IS NULL THEN '…' IS NOT NULL ELSE 'HOUR' LIKE '__x__%' END) AND 1)
      < ANY (SELECT DISTINCT IFNULL(1,0) AND (1 OR 0) FROM t AS t4
             LEFT OUTER JOIN t AS t5 ON t4.name >= t5.name
             INNER JOIN t AS t6 ON t4.created_at = t6.name
             WHERE t3.id IS NULL)                       -- <-- the correlation
GROUP BY t2.id;
```

**Mapping onto the distilled repro.** The `if(…)`/`CASE` left operand folds to `(t2.id IS NULL)`:
`CAST('2014-04-04T10:10:10.10' AS CHAR(255)) IS NULL` is false so the `if` returns its third argument,
and `'HOUR' LIKE '__x__%'` is false (four characters, five-character pattern), leaving `1` when
`t2.id IS NULL` and `0` otherwise. The subquery's three-way join and `DISTINCT IFNULL(1,0) AND (1 OR 0)`
collapse to `SELECT 1 FROM t AS t4`. `t1` and its join drop out. Eight rows become two, three columns
stay (the view body needs them, the query does not). What remains is the trigger.

### (2) The load-bearing construct — a construct × query-feature composition

**Aggregate-window-function view × self outer join × correlated quantified subquery.** All three are
required and none is sufficient:

- swap the window function for `GROUP BY` + `MAX()`, or for `ROW_NUMBER()`, or drop the view → correct;
- swap the `RIGHT OUTER JOIN` for an `INNER JOIN` → correct;
- swap `< ANY (…)` for `EXISTS (…)`, or use a correlated scalar subquery → correct.

`DISTINCT` in the view body is *not* part of the trigger, despite appearing in every finding.

### (3) Constructs reduced away

For round 1795: four of the five chain links (the `ROW_NUMBER` keying table, both
duplicate-100×-via-`WITH RECURSIVE` steps, and the `ANY_VALUE`/`GROUP BY` reduce view), the `DISTINCT`
in the final view, the third joined instance `t1`, the `if`/`CASE`/`IFNULL`/`CAST`/`LIKE` wrapper on
the left operand, the subquery's `LEFT JOIN` + `INNER JOIN` and its `DISTINCT`, and six of the eight
rows. For the other four rounds the entire chain was likewise replaced by the single minimal view (see
`reduced.sql` PART 4); their queries were left verbatim, so the *relation* side is reduced and the
*query* side is not.

## Minimal oracle exposure path

- **Object composition arity:** `2`.
- **GCL builder path:** `KeyWindowAggregateReduceBuilder → hardcoded VIEW`.
- **Confidence:** Verified against the report's final emitted link, the current TiDB GCL, and the
  builder implementation.
- **Realization:** `KeyWindowAggregateReduceBuilder` directly creates the window-reduce view; no
  separately selected `CreateViewBuilder` is part of the minimal path.
- **Workload/data requirements (excluded from arity):** the self outer join, correlated quantified
  subquery, aggregate-window choice, and two-row seed are workload/data requirements and are not
  counted.
- **Exposure vs. intrinsic trigger:** the aggregate-window transform and its stored-view boundary are
  intrinsic; the earlier keying, expansion, aggregate reduction, and other wrappers only exposed this
  final relation shape and remain reduced away.

## Characterization

### What triggers it / what does not

| Variant (from the PART 2 minimal repro) | Result |
|---|---|
| view with `MAX(col) OVER (PARTITION BY k)` | **1 row — WRONG** |
| …same, plus `DISTINCT` in the view body | **wrong** |
| plain table, same rows | 0 rows, correct |
| view: `GROUP BY k` + `MAX(col)` aggregate | 0 rows, correct |
| view: `DISTINCT` only, no window function | 0 rows, correct |
| view: `ROW_NUMBER() OVER (ORDER BY id)` | 0 rows, correct |
| trivial view `SELECT * FROM b` | 0 rows, correct |
| view `SELECT id, name, created_at FROM b` | 0 rows, correct |
| view over a derived table `(SELECT * FROM b) x` | 0 rows, correct |
| inline derived table, no view at all | 0 rows, correct |
| `EXISTS (…)` instead of `< ANY (…)` | 0 rows, correct |
| correlated **scalar** subquery over the same window view | correct, identical to plain table |
| `INNER JOIN` instead of `RIGHT OUTER JOIN` | 0 rows, correct |
| the correlated column added to the outer `SELECT` list | 0 rows, correct |
| uncorrelated subquery (`WHERE t4.id IS NULL`) | 6 rows on **both** shapes — agrees, so not a discriminator |

The last two rows are the interesting ones for a triager. Adding `t3.id` to the outer select list — a
change that alters no semantics of the filter — fixes the result, which is the signature of a
column-pruning or correlated-column-resolution problem rather than a bad rewrite of the subquery
itself. And the uncorrelated variant agrees across shapes, confirming the correlation is what breaks.

### Plan diff

`EXPLAIN` of the minimal query over the window view versus over the `GROUP BY` view. The subquery
branch of the `Apply` is where they differ:

```
-- WINDOW VIEW (wrong)
└─Apply_52            CARTESIAN semi join, other cond:or(lt(isnull(Column#12), Column#50), if(ne(Column#51,0), NULL, 0))
  ├─HashJoin_54       CARTESIAN right outer join, other cond:ge(Column#12, Column#28)
  │ ├─Selection_63    not(isnull(Column#12))
  │ │ └─Window_64     max(k.id)->Column#12 over(partition by k.eq_key_1)
  │ └─Window_74       max(k.id)->Column#28 over(partition by k.eq_key_1)     <-- outer t3.id
  └─Selection_84      ne(Column#52, 0)
    └─HashAgg_87      funcs:max(1)->Column#50, sum(0)->Column#51, count(1)->Column#52
      └─Window_90     max(k.id)->Column#44 over(partition by k.eq_key_1)     <-- recomputed inside
        └─Sort_98
          └─TableReader_97
            └─Selection_96   cop[tikv]  isnull(Column#28)                    <-- correlated filter, pushed
              └─TableFullScan_95  table:k                                        BELOW the window operators

-- GROUP BY VIEW (correct)
  └─Selection_51      ne(Column#31, 0)
    └─HashAgg_54      funcs:max(1)->Column#29, sum(0)->Column#30, count(1)->Column#31
      └─HashAgg_62    group by:k.eq_key_1, funcs:count(Column#36)->Column#35
        └─TableReader_63
          └─HashAgg_57       cop[tikv]  group by:k.eq_key_1, funcs:count(1)->Column#36
            └─Selection_61   cop[tikv]  isnull(Column#16)                    <-- same push, correct result
              └─TableFullScan_60  table:k
```

Both plans push the correlated `isnull(<outer column>)` down to a `cop[tikv]` `Selection` over the
subquery's own base table, so "the filter is pushed down" is not by itself the defect. The difference
is what sits between that `Selection` and the correlated column's definition: in the window case the
filter is placed **below `Sort_98` and the three `Window_90/91/93` operators**, i.e. below the
operators that establish the view's output columns, while the correlated reference `Column#28` is
defined by a *different* `Window` instance in the join's probe side. In the `GROUP BY` case the pushed
filter references `Column#16` under an aggregation that TiDB evaluates correctly.

I did not isolate the responsible optimizer rule. `Apply` + window + correlated column pushdown is the
neighbourhood; a TiDB engineer with `pkg/planner/core` in hand should be able to name the rule from the
plan above faster than I can from the outside. What I can state from measurement is the trigger set and
the controls.

### What is verified per round, and what is not

- **All five**: admissibility (base `t` ≡ equivalent `t`), divergence reproduces, divergence still
  reproduces when the entire chain is replaced by the single minimal window view, and all four
  non-triggering view shapes agree with the plain table.
- **Round 1795 only**: reduced to a 2-row / 6-statement minimal repro with the correct answer
  *independently derived* from the semantics of `< ANY (empty set)`, plus seven controls.
- **Rounds 2990 / 3188 / 3621 / 3654**: relation reduced to the one view; queries left verbatim. Their
  correct answers are established by agreement across five independent relation shapes rather than by
  hand-derivation. Each contains a correlated quantified subquery (`!= ANY`, `IN (SELECT …)`, `> ALL`,
  `< ANY` nested in `= ALL`), consistent with 1795's trigger, but I did not reduce each to its own
  minimal form — so "one root cause" is an inference from the shared trigger shape, not five separate
  proofs.

## How it was found

eqgen v3 data-equivalence oracle, `tidb_run19`, rounds 1795 / 2990 / 3188 / 3621 / 3654 (seeds
2132979842, 806554086, 1332445482, 1566574447, 560221390). The oracle holds the query fixed and swaps
in a row-identical relation. Here the swap was the duplicate-and-reduce builder: key every row, blow it
up 100×, then collapse it back with `MAX(col) OVER (PARTITION BY key)`. That round-trip is a pure
identity on the data — and it is exactly the relation shape the planner mishandles.

This is a case a query-rewrite oracle cannot construct. TLP / NoREC / EET hold the data fixed and
rewrite the query, so the window function would have to already be in the user's query; and none of
their rewrites introduce an aggregate window function *in a view* underneath an unchanged correlated
quantified subquery — the trigger is a property of the relation, not of the query. The generator got
there because "duplicate and reduce" is a natural row-preserving data rewrite that happens to be built
out of a window function.

Worth feeding back to the generator: the duplicate-and-reduce builder has two spellings, window
(`MAX() OVER (PARTITION BY key)`) and aggregate (`GROUP BY key` + `MAX()`), and it emitted both in this
run — round 1795's chain contains an `ANY_VALUE … GROUP BY` reduce *and* the window reduce. Only the
window spelling triggers this, so the run's yield here is a direct consequence of that builder choice.

- Reduced repro, controls, and all five queries over the one-view relation: [`reduced.sql`](reduced.sql)
- Original findings: `mismatch_round1795_0.sql`,
  `2990`,
  `3188`,
  `3621`,
  `3654`
