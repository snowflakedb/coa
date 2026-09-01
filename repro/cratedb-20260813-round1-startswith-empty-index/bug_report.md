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

# CrateDB: `starts_with(col, '')` on a default-indexed TEXT column matches only `''`

## Summary

Every string starts with the empty prefix: `SELECT starts_with('a', '')` is TRUE, and
`WHERE starts_with(name, '')` must return every non-NULL `name`. On CrateDB 6.4.1 and 6.4.2 that
WHERE, on a **default-indexed** TEXT column, goes through Collect's lucene `toQuery` path and
returns **only the empty string**. The same predicate on `INDEX OFF`, or as a Filter above
WindowAgg, returns all non-NULL rows. No error.

This is not `#15743` / `#16567` (LIKE empty pattern; those are closed). `starts_with` was added
later (`#16869` / `#17877`) with its own `toQuery`; the empty-prefix indexed case is still wrong.
It is also not the window-view `= ALL (empty)` NULL-drop (Filter-above-WindowAgg 3VL) — here the
**indexed Collect is the wrong side** and the window / INDEX OFF path is correct.

## Environment

| | |
|---|---|
| Engine | CrateDB **6.4.1** (built `45bfa80`) and **6.4.2** (built `1db6455`), official Docker images |
| Session | defaults |
| Access path | PostgreSQL wire via `psycopg` |
| Shards | `CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0)` |
| Determinism | stable across repeats |

## Minimal repro

```sql
CREATE TABLE t (name TEXT)
    CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t VALUES ('a');
INSERT INTO t VALUES ('');
INSERT INTO t VALUES ('abc');
INSERT INTO t VALUES (NULL);
REFRESH TABLE t;

SELECT starts_with('a', '');                         -- TRUE
SELECT name FROM t WHERE starts_with(name, '');
-- Expected: 'a', '', 'abc'     (NULL → UNKNOWN → dropped)
-- Actual:   ''                 <<< WRONG
```

`INDEX OFF` on `name`, or wrapping `name` in `MAX(name) OVER (PARTITION BY pk)`, both return the
three non-NULL rows. Reproduces on 6.4.2.

Full controls are in `reduced.sql`; re-check by running it against `crate:6.4.1`.

## Expected vs actual

The **INDEX OFF / window** path is the correct side. The default-indexed Collect is wrong — it
returns too few rows.

| query | default index (actual) | INDEX OFF / window (correct) |
|---|---|---|
| `SELECT starts_with('a', '')` | TRUE | TRUE |
| `WHERE starts_with(name, '')` | **`''` only** | `'a'`, `''`, `'abc'` |
| `WHERE name LIKE '%'` | `'a'`, `''`, `'abc'` | same |
| `WHERE name LIKE ''` | `''` (correct SQL — LIKE `''` is not starts_with) | `''` |
| `WHERE name = ''` | `''` | `''` |

## Equivalence construction

eqgen's oracle builds a second relation with the same rows as base `t` and runs the same query on
both. The equivalent here included `INDEX OFF` and an identity window collapse. Distilled, **neither
is needed to see the bug** — a one-column default-indexed table is enough. The equivalent is useful
only as the correct-side control (it does not use lucene `toQuery` on `name`).

The harvested query was

```sql
SELECT t2.name, t3.name
FROM t t1 CROSS JOIN t t2 CROSS JOIN t t3
WHERE (t3.name != ANY (
        SELECT DISTINCT t4.name FROM t t4 WHERE starts_with(t4.name, '')
      ))
      IN (SELECT FALSE FROM t t5 GROUP BY t5.created_at, t5.name)
```

On Collect the ANY-set is `{''}`, so `'' != ANY ({''})` is FALSE and `FALSE IN (FALSE)` keeps the
empty-string rows (64 = 8×8×1). On INDEX OFF / window the ANY-set is every non-NULL name, so
`'' != ANY (…)` is TRUE and `TRUE IN (FALSE)` drops everything. Same root cause as PART 1.

## Minimal oracle exposure path

**Object composition arity:** `2`

**GCL builder path:** `CrateDbIndexOffBuilder` → `TABLE` realization

**Confidence:** verified

**Realization:** An `INDEX OFF` `TABLE` supplies the row-identical correct-side relation.

**Workload/data requirements (excluded from arity):**
- A default-indexed `TEXT` column on the buggy side.
- `starts_with(column, '')` in a filtering context.
- At least one non-NULL, non-empty string to distinguish the results.

**Exposure vs. intrinsic trigger:** No equivalence builder remains in the standalone trigger: the wrong result occurs on the ordinary default-indexed base table. `CrateDbIndexOffBuilder` and its table realization merely provided the row-identical contrasting path that bypassed the Lucene `toQuery` behavior and exposed the defect.

## Suggested fix

`starts_with(col, '')` must not be rewritten to a lucene prefix query that only matches the empty
token. Either skip `toQuery` when the prefix is empty (evaluate the scalar; equivalent to
`col IS NOT NULL`) or emit a match-all-on-that-field query. The INDEX OFF path already does the
right thing.

## Origin

eqgen corpus replay of previously-passing queries, CrateDB 6.4.1, simple catalog.
Finding: `crate_corpus_full/cratedb_20260814-001112/mismatch_round1_9.sql`.
