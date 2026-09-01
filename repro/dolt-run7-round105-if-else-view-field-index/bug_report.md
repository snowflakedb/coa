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

# Dolt: `if(<cond>, <literal>, <column>)` in a `WHERE` over a view-backed derived table raises an internal field-index error when the condition is NULL

## Summary

When a derived table reads from a **view** and a `WHERE` clause applies
`if(<condition over one derived column>, <literal>, <another derived column>)`, Dolt raises its own
internal error — `1105 unable to find field with index N in row of M columns. This is a bug.` — as
soon as a row makes the condition evaluate to `NULL`, i.e. as soon as the **else branch is actually
reached**. The same query over the underlying base *table* returns rows, and MySQL 9.7 returns rows
in every variant. The engine appears to resolve the else-branch column reference against the wrong
row width once the view has been inlined, so the field index it computes exceeds the tuple it is
handed.

This is a **surviving sibling of gms#3488** ("`WHERE` filter on view columns backed by string
literals fails with index out of bounds", merged 2026-03-27, present in this build) and of
dolt#11378 (fixed by gms#3664, also present): same symptom class, different reachable path.

## Environment

- **Engine**: Dolt 8.0.31 (`VERSION()`), source `v2.2.3-49-ga995f245c`, commit
  `a995f245c032bc412aed308194d81ee12bc74f19`, assertions off.
- **go-mysql-server**: `v0.20.1-0.20260805191915-e5eafe0da809` (so gms#3488 and gms#3664 are both in).
- **sql_mode**: `STRICT_ALL_TABLES,NO_ZERO_DATE,NO_ZERO_IN_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION,ONLY_FULL_GROUP_BY,NO_BACKSLASH_ESCAPES,PIPES_AS_CONCAT`
  — not load-bearing; the repro does not depend on any mode.
- **charset / collation**: `utf8mb4`; database `utf8mb4_0900_bin`, connection `utf8mb4_0900_ai_ci`.
- **Contrast engine**: MySQL 9.7.2 release build.

## Minimal repro

```sql
CREATE TABLE base (name VARCHAR(255));
INSERT INTO base VALUES (NULL);
CREATE VIEW v AS SELECT name FROM base;

SELECT sq.b
FROM (SELECT t1.name AS v, 1 AS b FROM v AS t1) AS sq
WHERE if(sq.v <> 'x', 1, sq.b);
```

## Expected vs actual

| query | expected (MySQL 9.7) | actual (Dolt) |
|---|---|---|
| minimal repro above | `1` | `1105 unable to find field with index N in row of 1 columns. This is a bug.` |
| C1 — `base` table instead of the view | `1` | `1` |
| C2 — else branch is a literal (`if(sq.v <> 'x', 1, 1)`) | `1` | `1` |
| C3 — condition is a literal (`if(1, 1, sq.b)`) | `1` | `1` |
| C4 — no `if()` (`WHERE sq.b`) | `1` | `1` |
| C5 — no NULL row, so the else branch is never reached | `1` | `1` |

`N` is not stable: 2, 4, 13, 20 and 41 were observed across this and the related findings, which is
consistent with an index computed against the wrong scope rather than a fixed off-by-one.

## Equivalence construction

### Concrete, as the builder emits it

`error_round105_1`'s equivalent `t` stacks two row-preserving builders — a surrogate-key **flag-table
join round-trip** (which drops `eq_uid_1`) underneath a **QUALIFY window-filter view** (which drops
`_qf`):

```sql
ALTER TABLE t RENAME TO t__base;
CREATE TABLE t__base_table_1 AS SELECT id, name, created_at, ROW_NUMBER() OVER (ORDER BY id) AS eq_uid_1 FROM t__base;
CREATE TABLE t__base_table_2 AS SELECT eq_uid_1, 1 AS eq_flag_2 FROM t__base_table_1;
CREATE TABLE t__base_table_3 AS SELECT l.id AS id, l.name AS name, l.created_at AS created_at
                               FROM t__base_table_1 l INNER JOIN t__base_table_2 r ON l.eq_uid_1 = r.eq_uid_1
                               WHERE r.eq_flag_2 = 1;
CREATE VIEW t AS SELECT id, name, created_at
                 FROM (SELECT id, name, created_at, ROW_NUMBER() OVER (ORDER BY id) >= 1 AS _qf
                       FROM t__base_table_3) AS _qw WHERE _qf;
```

`reduced.sql` PART 1 is this chain verbatim with the original workload query. Oracle admissibility
passed: base `t` and equivalent `t` are row-identical.

### The load-bearing construct

**Just "a view".** This is the finding's most useful signal, because it is far weaker than the
constructs the two already-fixed siblings needed:

- a plain pass-through `CREATE VIEW t AS SELECT id, name, created_at FROM t__base` reproduces it;
- neither `ROW_NUMBER()` layer is needed, nor the flag-table join, nor the surrogate key;
- the base *table* does **not** reproduce it (control C1).

So the trigger is a **composition of four ingredients**, each proven necessary by its own control:

1. a **view** in the derived table's `FROM` (C1),
2. a **derived table** projecting at least two columns, one of which the `if()` returns,
3. `if(<cond over column A>, <literal>, <column B>)` in the `WHERE` — a literal else branch (C2) or
   a literal condition (C3) or no `if()` at all (C4) each behave,
4. a row for which the condition is **NULL**, so the else branch is evaluated (C5).

Reduced away: both window functions, both intermediate tables, the join, `GROUP BY`, `HAVING`, the
`MAX`/`NULLIF`/`CASE`/`least`/`LEFT`/`to_base64`/`DAYNAME`/`LTRIM`/`LEAST` expression tree, 7 of 8
rows, and 2 of 3 columns.

## Minimal oracle exposure path

- **Object composition arity:** `3`.
- **GCL builder path:** `FlagTableJoinQueryBuilder → QualifyQueryBuilder → CreateViewBuilder`.
- **Confidence:** high; these are exact current factory class names and the report preserves the
  corresponding emitted flag-join, qualify-filter, and final-view SQL.
- **Realization:** `CreateViewBuilder` persists the exposed equivalent as a view.
- **Workload/data requirements (excluded from arity):** the derived table, `IF` expression, cross-column
  else branch, and a row making the condition `NULL` belong to the workload/data trigger.
- **Exposure vs. intrinsic trigger:** arity 3 records the finding's oracle exposure chain. It does not
  claim all three objects are intrinsically necessary: the flag join and qualify layer reduce away,
  while a plain view is the minimal relation shape that exposes the field-index defect.

## Characterization

- **Trigger**: the `if()` **else branch reaching a derived-table column** while the derived table is
  backed by a view. It is the *evaluation* of that branch that matters, not its presence — with no
  NULL in the data the same query is fine (C5), and with a literal in the else slot it is fine (C2).
- **Does NOT trigger**: base table instead of view; literal else branch; literal condition; no
  `if()`; no NULL row. `GROUP BY`/`HAVING` are irrelevant either way.
- **Not a crash**: a returned SQL error (`1105`), no panic, no stack trace. The engine's own message
  points at the issue tracker, which is what makes this class self-identifying. Assertions-off build;
  irrelevant here.
- **Not fixed by the two recent fixes.** Verified on a build that contains both gms#3488
  (2026-03-27) and gms#3664 (2026-08-03): the earlier shapes those fixed — split-rejoin view, and a
  view dropping a window-function column, both with a correlated subquery — now return rows, while
  this shape still errors. The remaining path needs no correlated subquery at all.
- `EXPLAIN` was not usable as a discriminator here: it fails with the same error on the buggy query,
  so the plan is never produced. That in itself localizes the defect to analysis/index-assignment
  rather than to row iteration.

## How it was found

The eqgen v3 data-equivalence oracle. It holds the workload query fixed and swaps the relation
underneath it for a **row-identical** rewrite built from row-preserving builders, so a trivial query
becomes the probe. Here the base table answers and the view-backed rewrite raises an internal error
— a one-sided error, which the harness records as an `error_*` finding.

A query-rewrite oracle (TLP / NoREC / EET) would have a hard time with this one: it holds the *data
and relation* fixed and mutates the query, and the trigger is not in the query — the identical query
is correct against the base table. What has to change is the relation's *shape*, which is exactly the
axis this oracle varies.

- Seed: `600545525` (`error_round105_1`), engine `@95218a00` at discovery; re-confirmed on `@a995f245`.
- `reduced.sql` in this folder.
- Original findings: hunt log,
  hunt log,
  hunt log — the last
  two are the same class over much larger builder chains (a 273-statement predicate-split partition
  chain in the `dolt_run9` case).
