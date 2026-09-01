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

# MySQL: merged CASE/CAST view + aliased `HAVING MIN(col) >= IFNULL(col, col)` drops groups that the same expression in SELECT reports as true

## Summary

A mergeable `VIEW` whose column is a real expression (`CASE WHEN TRUE THEN created_at ELSE CAST(NULL AS CHAR(255)) END`, `CAST(created_at AS CHAR(255))`, or a `JSON_EXTRACT` unpack) silently **drops `GROUP BY` groups** when queried as

```sql
SELECT created_at FROM t t3
GROUP BY t3.id, t3.created_at
HAVING MIN(t3.created_at) >= IFNULL(t3.created_at, t3.created_at);
```

Every non-NULL group is a singleton `created_at`, so `MIN(created_at) >= IFNULL(created_at, created_at)` is tautologically TRUE. The CASE view keeps only the `'trailing '` groups and drops `"o'brien"`, `''`, and `'Zed'`. Projecting that same comparison in the SELECT list (no `HAVING`) reports `ok = 1` for the dropped groups — **HAVING and SELECT disagree on the identical expression**.

`ALGORITHM=TEMPTABLE`, a CTAS of the same `CASE`, an identity view, the base table, the same predicate in `WHERE`, and the same `HAVING` **without a table alias** are all correct. `EXPLAIN FORMAT=TREE` shows the alias as the rewrite switch: the buggy plan expands the `CASE` into every `IFNULL`/`LEAST` operand; the unprefixed plan leaves bare `created_at` inside `IFNULL`.

This is the same view-merge neighbourhood as
[`mysql-20260812-view-merge-having-and-count-drops-group`](../mysql-20260812-view-merge-having-and-count-drops-group/)
(`HAVING (col AND COUNT(col))`) and
[`mysql-20260814-021542-round0-having-max-view-expr-1054`](../mysql-20260814-021542-round0-having-max-view-expr-1054/)
(`HAVING col` → 1054). Different `HAVING` shape, different symptom (dropped groups, not 1054), and a **load-bearing table alias**.

## Environment

- **Engine**: MySQL 9.7.2 (docker `mysql:9.7.2`).
- **Session**: `sql_mode = STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES`;
  `utf8mb4` / `utf8mb4_0900_bin`. Not load-bearing.
- **Types**: `SHOW COLUMNS` / `COLLATION(created_at)` / `CHARSET(created_at)` of the CASE view match the base table (`varchar(255)` `utf8mb4_0900_bin`). Not a type-equivalence artefact.
- **Determinism**: 2 vs 5 rows, stable across fresh connections.

## Minimal repro

See [`reduced.sql`](./reduced.sql). Distilled core (PART 2; PART 1 is a separate database):

```sql
CREATE TABLE b (id BIGINT, created_at VARCHAR(255));
INSERT INTO b VALUES
  (NULL, NULL), (-1, 'trailing '), (2, 'o''brien'), (-7, ''),
  (-7, NULL), (1, 'trailing '), (-7, 'Zed'), (-1, 'trailing ');

CREATE VIEW t AS
SELECT id, CASE WHEN TRUE THEN created_at ELSE CAST(NULL AS CHAR(255)) END AS created_at
FROM b;

SELECT created_at FROM t t3
GROUP BY t3.id, t3.created_at
HAVING MIN(t3.created_at) >= IFNULL(t3.created_at, t3.created_at);
```

## Expected vs actual

| Query (CASE view unless noted) | Expected | Actual |
|---|---|---|
| distilled repro above | 5 rows: two `'trailing '`, `"o'brien"`, `''`, `'Zed'` | **2 rows: both `'trailing '`** |
| same `HAVING` on the base table / identity view / `ALGORITHM=TEMPTABLE` / CTAS of the CASE | 5 | 5 |
| `FROM t` (no alias), unprefixed `GROUP BY` / `HAVING`, CASE view | 5 | 5 |
| `WHERE t3.created_at >= IFNULL(t3.created_at, t3.created_at)` (no `HAVING`) | 6 non-NULL rows | 6 |
| `HAVING MIN(t3.created_at) >= t3.created_at` (no `IFNULL`) | 5 | 5 |
| `SELECT …, MIN(t3.created_at) >= IFNULL(t3.created_at, t3.created_at) AS ok` (no `HAVING`) | `ok=1` on every non-NULL group | `ok=1` on every non-NULL group, **including the three HAVING drops** |
| `CAST(created_at AS CHAR(255))` view, same aliased `HAVING` | 5 | **3** (sibling trigger; drops a different subset) |
| as-found `HAVING MIN >= LEAST(IFNULL(…), 'x', col)` | 5 | **2** (same groups) |

**Which side is wrong:** the **merged CASE/CAST/JSON view**. Ground truth is the SQL semantics of `MIN(col) >= IFNULL(col, col)` over singleton groups, confirmed by the table, by TEMPTABLE, by `WHERE`, and by the SELECT-list evaluation of the same predicate.

## Equivalence construction

### Concrete, as the builder emits it

`mismatch_round0_1.sql` seeds 8 rows, then a tautology-`CASE` view, an indexed join-reattachment, a tag/`UNION ALL` round-trip, and finally `MySqlJsonPackRoundTripBuilder`:

```sql
CREATE VIEW t AS SELECT
  CASE WHEN JSON_EXTRACT(eq_json.j, '$.c_pk') = CAST('null' AS JSON) THEN NULL
       ELSE CAST(JSON_EXTRACT(eq_json.j, '$.c_pk') AS SIGNED) END AS c_pk,
  …,
  CASE WHEN JSON_EXTRACT(eq_json.j, '$.created_at') = CAST('null' AS JSON) THEN NULL
       ELSE CAST(JSON_UNQUOTE(JSON_EXTRACT(eq_json.j, '$.created_at')) AS CHAR(255))
            COLLATE utf8mb4_0900_bin END AS created_at
FROM (SELECT JSON_OBJECT(…) AS j FROM t__base_table_8) AS eq_json;
```

Workload (abridged): 3-way `CROSS JOIN` of `t`, `GROUP BY t3.id, t3.created_at`,
`HAVING MIN(t3.created_at) >= LEAST(IFNULL(t3.created_at, t3.created_at), '𒀀', t3.created_at)`.
Base keeps `"o'brien"`, `''`, `'Zed'`; equivalent drops them.

`reduced.sql` PART 1 is that JSON view plus the aliased `HAVING` (cross joins and the window in the SELECT list removed). PART 2 replaces JSON with `CASE WHEN TRUE THEN created_at ELSE CAST(NULL AS CHAR(255)) END` and `LEAST(…, 'x', …)` with `IFNULL` alone.

### Load-bearing composition

**Expression-valued mergeable view column × table alias in `GROUP BY`/`HAVING` × `HAVING MIN(col) >= IFNULL(col, col)`** (or `COALESCE` / `IF(col IS NULL, col, col)` / `LEAST(col, literal, col)`). All three are required.

### Reduced away

JSON pack, tautology `CASE` conditions, 3-way `CROSS JOIN`, the window `MIN(CAST(created_at AS CHAR(255))) OVER (…)`, scalar subqueries, `LEAST` and the `'𒀀'`/`'x'` literal, `name` / `c_pk`, and the earlier chain links. `CAST(created_at AS CHAR(255))` is a sibling, not a reduction of CASE.

## Minimal oracle exposure path

- **Object composition arity:** **2**
- **GCL builder path:** `MySqlJsonPackRoundTripBuilder` → `VIEW` realization
- **Confidence:** Verified — the report names the builder and its emitted JSON-unpack view, and the GCL implementation hardcodes that realization.
- **Realization:** The builder internally CTASes its input, then exposes the unpacked columns through the final mergeable `VIEW`.
- **Workload/data requirements (excluded from arity):**
  - An expression-valued string column behind the mergeable view.
  - A table alias used in `GROUP BY`/`HAVING`.
  - `HAVING MIN(col) >= IFNULL(col, col)` or a documented equivalent shape.

**Exposure vs. intrinsic trigger:** The JSON round trip is the oracle path that supplied an expression-valued merged view; JSON itself is not intrinsic. The engine trigger is expression substitution across the aliased grouped `HAVING`, which also reproduces with a simple `CASE` or `CAST` view.

## Characterization

### `EXPLAIN FORMAT=TREE`

```
-- CASE view, FROM t (no alias) — CORRECT. IFNULL keeps the grouped column name.
Filter: (min((case when true then b.created_at else <cache>(cast(NULL as char(255) charset utf8mb4)) end))
         >= least(ifnull(created_at,created_at),'x',created_at))
  -> Aggregate using temporary table
      -> Table scan on b

-- CASE view, FROM t t3 — WRONG. CASE is substituted into every IFNULL/LEAST operand.
Filter: (min((case when true then b.created_at else <cache>(cast(NULL as char(255) charset utf8mb4)) end))
         >= least(ifnull((case when true then … end),(case when true then … end)),'x',(case when true then … end)))
  -> Aggregate using temporary table
      -> Table scan on b

-- ALGORITHM=TEMPTABLE — CORRECT. View is materialized; HAVING sees t3.created_at.
Filter: (min(t3.created_at) >= least(ifnull(t3.created_at,t3.created_at),'x',t3.created_at))
  -> Aggregate using temporary table
      -> Table scan on t3
          -> Materialize
              -> Table scan on b
```

View merge inlines the `CASE`, then the aliased `HAVING` re-expands it on both sides of `>=` and evaluates that rewritten filter incorrectly. Unprefixed `HAVING` only expands `MIN`, leaving `IFNULL(created_at, created_at)` as a grouped-column ref — which happens to be the correct plan.

### What triggers / what does not

| Variant | Result |
|---|---|
| CASE view + `FROM t t3` + `HAVING MIN >= IFNULL` | **2 rows ✗** |
| `FROM t t` (alias equal to the view name) | **2 ✗** |
| `FROM t t1` but unprefixed `GROUP BY id, created_at` / `HAVING` | 5 ✓ |
| mixed prefixes (`GROUP BY t3.id` but `MIN(created_at) >= IFNULL(t3.created_at, created_at)`) | 5 ✓ |
| `COALESCE` / `IF(col IS NULL, col, col)` / `LEAST(col, 'x', col)` / `LEAST(IFNULL, 'x')` in place of `IFNULL` | **2 ✗** |
| `HAVING MIN >= t3.created_at` | 5 ✓ |
| `CAST(created_at AS CHAR(255))` view | **3 ✗** (sibling) |
| `(created_at) AS created_at` view | 5 ✓ |
| `ALGORITHM=MERGE` (explicit) | **2 ✗** |
| `ALGORITHM=TEMPTABLE` / CTAS / identity view / base table | 5 ✓ |
| same predicate in `WHERE` | correct on CASE view ✓ |
| `HAVING (col AND COUNT(col))` over this CASE view | 0 on **both** sides — not the AND-COUNT bug |

`FROM t t1 GROUP BY id` succeeding while `FROM t t3 GROUP BY t3.id` fails is the kind of self-alias token the equivalence builders emit unconditionally (`t AS t1`, `t AS t3`).

### Blast radius

Read-only `SELECT`. The matching `WHERE` is correct, so `UPDATE`/`DELETE` with that predicate should not fire this path (not separately tested). Any mergeable view that exposes a computed string column and is queried with an aliased `HAVING MIN(col) >= IFNULL(col, col)` (or `LEAST` of that) can silently drop groups.

## How it was found

eqgen data-equivalence oracle, `mysql_20260814-021542` round 0 seed 777025934, `mismatch_round0_1.sql`. The oracle held the query fixed and swapped in a row-identical JSON-unpack view. A query-rewrite oracle over the base table never builds that view, and NoREC pulling the `HAVING` into the SELECT list would have measured the *correct* SELECT-list evaluation (control (h)) and missed the bug.

The same `HAVING MIN >= LEAST(IFNULL…)` also drops groups through the tautology-`CASE` view on round 36's seed (loses `'a'`). Round 36's *original* `HAVING MIN(CAST(id AS SIGNED)) >= CHAR_LENGTH(created_at)` did **not** diverge on a CASE view, so that finding is not claimed as this bug.

## Open items

- Not bisected; verified on 9.7.2 only.
- `CAST(created_at AS CHAR(255))` drops a different subset (3 vs 2) — same neighbourhood, not fully reduced as its own repro.
- Optimizer rule / `file:line` not named. The TREE plan above is the starting point: aliased `HAVING` substitutes the view expression into `IFNULL` and then evaluates that filter in the wrong scope.
