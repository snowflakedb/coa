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

# MySQL: semijoin returns wrong row count over a LATERAL-derived-table view vs the base table

## Summary

A query `SELECT COUNT(*) FROM t1 CROSS JOIN t3 WHERE t3.name IN (<3-table non-equi GROUP-free
subquery>)` returns **30 over the base table `t` but only 27 over a row-identical view `t`** whose
sole difference is that it is defined with a `LATERAL` derived table
(`SELECT ll.* FROM tb AS ls, LATERAL (SELECT ls.id, ls.name, ls.created_at) AS ll`). The output
cardinality of a join+filter is pure relational algebra — a function of the data alone, independent
of physical row order **and** of the chosen plan — so two evaluations over identical rows must agree.
The divergence is entirely in the **semijoin** that MySQL derives from the `IN (subquery)`:
`SET optimizer_switch='semijoin=off'` makes both sides return 30. The buggy plan applies
duplicate-weedout semijoin over the LATERAL-materialized derived tables and drops 3 rows.

## Environment

- **Version:** `26.7.0-debug`, `@06a5c1c9` (main/trunk build, assertions on)
- `sql_mode`: `ONLY_FULL_GROUP_BY,NO_BACKSLASH_ESCAPES,STRICT_ALL_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION`
- charset `utf8mb4` / collation `utf8mb4_0900_bin`
- `sql_mode`/charset are immaterial to the divergence (it is an optimizer plan issue); pinned here to match the finding.

## Minimal repro

```sql
CREATE TABLE t (id BIGINT, name VARCHAR(255), created_at VARCHAR(255));
INSERT INTO t VALUES (1,'a','x'),(2,'b','x'),(3,'b','x'),(4,NULL,'x'),(4,'c','x'),(5,'d','x');

-- BASE (this table): 30
SELECT COUNT(*) FROM t AS t1 CROSS JOIN t AS t3
WHERE t3.name IN (SELECT t6.name FROM t AS t4 JOIN t AS t5 ON t4.id != t5.id
                                     JOIN t AS t6 ON t5.id = t6.id);
```

Now, in a **separate fresh DB**, build a row-identical view over the same rows and run the same query:

```sql
CREATE TABLE t (id BIGINT, name VARCHAR(255), created_at VARCHAR(255));
INSERT INTO t VALUES (1,'a','x'),(2,'b','x'),(3,'b','x'),(4,NULL,'x'),(4,'c','x'),(5,'d','x');
ALTER TABLE t RENAME TO tb;
CREATE VIEW t AS SELECT ll.id, ll.name, ll.created_at
                 FROM tb AS ls,
                      LATERAL (SELECT ls.id AS id, ls.name AS name, ls.created_at AS created_at) AS ll;

-- EQUIVALENT (this view): 27  (WRONG)
SELECT COUNT(*) FROM t AS t1 CROSS JOIN t AS t3
WHERE t3.name IN (SELECT t6.name FROM t AS t4 JOIN t AS t5 ON t4.id != t5.id
                                     JOIN t AS t6 ON t5.id = t6.id);

-- CONTROL: same view, semijoin off -> 30 (correct)
SET SESSION optimizer_switch='semijoin=off';
SELECT COUNT(*) FROM t AS t1 CROSS JOIN t AS t3
WHERE t3.name IN (SELECT t6.name FROM t AS t4 JOIN t AS t5 ON t4.id != t5.id
                                     JOIN t AS t6 ON t5.id = t6.id);
```

## Expected vs actual

| query | expected | actual |
|---|---|---|
| COUNT over base table `t` | 30 | 30 |
| COUNT over LATERAL-view `t` | 30 | **27** |
| COUNT over LATERAL-view `t`, `semijoin=off` | 30 | 30 |

`tb` and the view `t` are verified row-identical (both 6 rows); the base 30 is the correct answer.

## Equivalence construction

The finding's equivalent `t` was a 14-statement builder chain (parity-split CTAS + `UNION ALL` +
`ROW_NUMBER()` window + RIGHT-JOIN flag round-trip + `name(10)` prefix index + `LENGTH(id)` VIRTUAL
generated column + a final `LATERAL`-derived view). Reduction shows the **only load-bearing element
is the last one — the `LATERAL`-derived-table view**:

```sql
CREATE VIEW t AS SELECT ll.* FROM tb AS ls, LATERAL (SELECT ls.id, ls.name, ls.created_at) AS ll;
```

Everything else in the chain (UNION ALL, parity split, ROW_NUMBER, RIGHT JOIN, prefix index, virtual
generated column) was reduced away. This is a **construct × query-feature composition**:
LATERAL-derived-table view **×** an `IN (subquery)` that the optimizer rewrites to a semijoin. A
plain view (`CREATE VIEW t AS SELECT * FROM tb`) does not diverge; the base table does not diverge.

## Minimal oracle exposure path

- **Object composition arity:** **2**
- **GCL builder path:** `LateralReprojectQueryBuilder` → `CreateViewBuilder`
- **Confidence:** Verified — the report isolates the builder-shaped lateral reprojection and final view, matching the registered GCL implementations.
- **Realization:** `CreateViewBuilder` exposes the lateral reprojection as the queried row-identical `VIEW`.
- **Workload/data requirements (excluded from arity):**
  - An outer cross join and an `IN` subquery transformed into a semijoin.
  - The documented three-table non-equi subquery and three-column lateral projection.
  - Minimal data with duplicate names and a NULL name.

**Exposure vs. intrinsic trigger:** The two-builder path is load-bearing on the object side because a plain view does not reproduce. The intrinsic trigger is the semijoin/duplicate-weedout plan over per-row LATERAL materializations, together with the listed workload and data shape.

## Characterization (each ingredient verified necessary against the build)

- **LATERAL-derived view** — a plain view/derived table (no LATERAL) returns 30. The LATERAL wrapper
  is what makes the optimizer materialize a per-row derived table (`Materialize (invalidate on row
  from ls)` + `Index lookup on ll using <auto_key0>`) that the semijoin then miscounts.
- **Semijoin** — `SET optimizer_switch='semijoin=off'` → 30 (correct). The bug is in the semijoin
  path derived from `IN (subquery)`. (`duplicateweedout=off` alone does not fix it; disabling
  semijoin entirely does.)
- **A third projected column** — dropping `created_at` (a 2-column table/view) returns 30. The width
  of the LATERAL projection changes the derived-table materialization and masks the bug.
- **The IN subquery must be the 3-table non-equi join** `t4 JOIN t5 ON t4.id != t5.id JOIN t6 ON
  t5.id = t6.id`; a 2-table subquery returns 30.
- **The outer `CROSS JOIN`** is required; a single outer table (`FROM t AS t3 WHERE …`) returns 30.
- **Data:** a NULL `name` row plus duplicate names are required; **6 rows is minimal** (no 5-row
  subset diverges). `GROUP BY`, the `CASE`, `DISTINCT`, and the window functions of the original
  query are all unnecessary.

Result is deterministic (30 vs 27 stable across repeated fresh-DB runs). EXPLAIN of the wrong plan:

```
-> Aggregate: count(0)
    -> Remove duplicate (ls, ll, ls, ll) rows using temporary table (weedout)
        -> Nested loop inner join
            -> Inner hash join (no condition)
                -> Invalidate materialized tables (row from ls) -> Table scan on ls
                -> Hash -> Nested loop inner join
                    -> Inner hash join (no condition)
                        -> Invalidate materialized tables (row from ls) -> Table scan on ls
                        -> Hash -> Nested loop inner join (× several)
                            ... -> Filter: ((ll.id <> ll.id) and (ll.id is not null))
                                     -> Table scan on ll -> Materialize (invalidate on row from ls)
            -> Filter: (ll.`name` = ll.`name`)
                -> Index lookup on ll using <auto_key0> (id = ll.id)
                    -> Materialize (invalidate on row from ls)
```

The semijoin has pulled the three subquery instances of the LATERAL view into the top-level join and
is deduplicating `(ls, ll, ls, ll)` with duplicate-weedout over the per-row-materialized `ll`
derived tables; that weedout drops rows that the base-table plan (which cannot reference an
`<auto_key0>` over a LATERAL materialization) keeps.

