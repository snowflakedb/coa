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

# MySQL: HAVING applied after SELECT DISTINCT deduplication → order-dependent wrong (empty) result

## Summary

For `SELECT DISTINCT <collapsing aggregate> … GROUP BY g HAVING <aggregate condition>`, MySQL 9.7.2
applies the `HAVING` filter **after** the `SELECT DISTINCT` deduplication step, not before (SQL
evaluation order is `GROUP BY → HAVING → SELECT → DISTINCT`). When `DISTINCT` collapses several
groups into a single output row, the post-dedup `HAVING` re-reads its aggregate from one arbitrary
surviving row, so the query's result **depends on physical row order** — which a `GROUP BY` query
must never do. With the group the `HAVING` excludes (via a `NULL` aggregate) physically first, MySQL
drops **every** row. MariaDB evaluates the identical query correctly and order-independently.

## Environment

- **Engine**: MySQL 9.7.2, commit `008e09c2`, release build, assertions off
  (`mysql-release/bin`).
- **Session**: `sql_mode = ONLY_FULL_GROUP_BY,NO_BACKSLASH_ESCAPES,STRICT_ALL_TABLES,NO_ZERO_IN_DATE,
  NO_ZERO_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION`; `utf8mb4` / `utf8mb4_0900_bin`.
  The bug is independent of `sql_mode`/collation.
- **Reference**: MariaDB 12.3.3 (`mariadb-release`) — correct in both row orders.

## Minimal repro

Two rows, one plain table (see [`reduced.sql`](./reduced.sql)):

```sql
CREATE TABLE t (id BIGINT, name VARCHAR(255));
INSERT INTO t VALUES (NULL, 'b'), (1, 'a');           -- NULL-id row FIRST
SELECT DISTINCT CAST(MAX(1) AS SIGNED) AS e0
FROM t GROUP BY name HAVING MAX(id) <= 100;
```

Group `'a'` passes the HAVING (`MAX(id)=1 <= 100`) and yields `e0 = 1`; group `'b'` is excluded
(`MAX(id)=NULL → NULL <= 100 → NULL`). The correct result is the single row `(1)`.

## Expected vs actual

| Setup (same 2 rows, same query) | Expected | Actual |
|---|---|---|
| NULL-id row physically **first** | `(1)` | **0 rows** |
| NULL-id row physically **last** (control) | `(1)` | `(1)` ✓ |
| MariaDB 12.3.3, **either** order | `(1)` | `(1)` ✓ (reference: order-independent) |
| drop the `CAST` — `SELECT DISTINCT MAX(1) …` | `(1)` | `(1)` ✓ |
| drop `DISTINCT` | `(1)` | `(1)` ✓ |
| non-collapsing agg — `CAST(MAX(id) AS SIGNED)` | `(1)` | `(1)` ✓ |

## Equivalence construction

### Concrete, as the builder emits it

The finding's equivalent `t` is the MySQL `eq_my` rebuild; its load-bearing step reorders rows:

```sql
CREATE TABLE t__base_table_3 AS SELECT id, name, created_at, ROW_NUMBER() OVER (ORDER BY id) AS eq_my_uk_3 FROM t__base;
CREATE VIEW  t AS SELECT id, name, created_at FROM t__base_table_3;
```

`ROW_NUMBER() OVER (ORDER BY id)` materialises the rows in `id` order and MySQL sorts the `NULL` `id`
row **first**. The equivalent `t` is row-identical to the base table (admissibility passes), but its
*physical order* differs — which is the whole trigger. The finding's workload query
(`mismatch_round161_0.sql`) is a longer instance of the reduced shape below.

### The load-bearing composition — a pure engine bug, no rewrite needed

Four ingredients, each proven necessary by a control that removes exactly one (all four controls
return the correct `(1)`):

1. **`SELECT DISTINCT`** that actually **collapses** ≥2 groups into one row — here `CAST(MAX(1) AS
   SIGNED)` is `1` for every surviving group. A non-collapsing aggregate (`CAST(MAX(id) …)`, distinct
   per group) does not trigger it.
2. **the `CAST(… AS SIGNED)` wrapper** on the aggregate — bare `MAX(1)` does not trigger it (the CAST
   is what makes MySQL choose the stacked aggregate-temp → dedup-temp plan).
3. **`GROUP BY`** with a **`HAVING`** whose aggregate condition is `NULL` for one group (the `NULL`-id
   group: `MAX(id) → NULL`).
4. that excluded group being **physically first**.

The bug needs no view/CTAS at all — it reproduces on a 2-row plain table purely from insert order.
The eqgen equivalence rewrite only supplied the reordering (via `ROW_NUMBER`).

## Minimal oracle exposure path

- **Object composition arity:** **2**
- **GCL builder path:** `CreateTableBuilder[ROW_NUMBER-ordered CTAS]` → `CreateViewBuilder` (**inferred mapping**)
- **Confidence:** Inferred — the report preserves the emitted ordered CTAS and exposing view, but not the historical GCL AST builder selections.
- **Realization:** The CTAS materializes NULL-first order and the final view exposes those row-identical contents to the workload.
- **Workload/data requirements (excluded from arity):**
  - `SELECT DISTINCT` that collapses multiple grouped outputs, with the documented aggregate `CAST`.
  - `GROUP BY` plus an aggregate `HAVING` that excludes one group.
  - The excluded/NULL-aggregate group physically first.

**Exposure vs. intrinsic trigger:** The inferred two-object path is the oracle's reordering-and-exposure mechanism. The intrinsic bug is `HAVING` evaluated after DISTINCT deduplication and is query × physical row order; it reproduces on a plain table with NULL-first inserts, so neither object is intrinsically required.

## Characterization

`EXPLAIN FORMAT=TREE` names the mechanism — the `HAVING` filter sits **above** the DISTINCT dedup:

```
-> Filter: (max(t.id) <= 100)                 <- HAVING, applied LAST
   -> Temporary table with deduplication       <- SELECT DISTINCT, runs FIRST
      -> Table scan on <temporary>
         -> Aggregate using temporary table     <- GROUP BY
            -> Table scan on t
```

The aggregation temp table is deduplicated (collapsing the surviving groups to a single `(1)` row),
and only then is `HAVING MAX(id) <= 100` applied — but after dedup there is no per-group `MAX(id)`
left, so MySQL evaluates it against an order-dependent surviving row; NULL-first yields `NULL` and
every row is dropped. Controls: **no `DISTINCT`** → order-independent and correct; **no `CAST`** →
correct; **non-collapsing aggregate** → correct; **NULL-last** → correct; **MariaDB** → correct in
both orders. Not a crash.

## How it was found

The eqgen **data-equivalence oracle** ran the (longer) workload against the base table and the
row-identical `eq_my` rebuild. The rebuild's `ROW_NUMBER() OVER (ORDER BY id)` reorders the rows
(NULL `id` first), and because the query is — due to this bug — order-dependent, the two relations
returned different multisets (base `(1,NULL,'')`, equivalent empty). That is the oracle turning a
subtle order-dependence into a hard contradiction: base and equivalent hold identical rows and *must*
agree, so an order-sensitive wrong result surfaces even though any single run looks self-consistent.
Comparing to MariaDB then showed the empty result is outright wrong, and `EXPLAIN` pinned the
HAVING-after-DISTINCT plan. (This is a textbook "one permutation is not enough" case — three sampled
row orders all gave the correct answer; only the NULL-first order the rebuild happens to produce
exposed it.)

- **Seed**: 1725557167 (round161).
- Reduced repro: [`reduced.sql`](./reduced.sql).
- Original finding: hunt log.
- **Same class (not separately reduced):** `mismatch_round78_0`, `mismatch_round108_0`,
  `mismatch_round229_0` — all `SELECT DISTINCT` + aggregate/`GROUP BY` queries whose result is
  order-dependent on MySQL (verified: each flips across base / reversed / NULL-first orders), all
  exposed by the same `ROW_NUMBER` reorder.
