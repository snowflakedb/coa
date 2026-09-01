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

# CrateDB: range predicates on a `NUMERIC`/DECIMAL column with `INDEX OFF` silently return zero rows

## Summary

On CrateDB 6.4.1 and 6.4.2, a range comparison (`<`, `>`, `<=`, `>=`, `BETWEEN`) against a
`NUMERIC(p,s)` column declared `INDEX OFF` matches **no rows at all**, even though the rows are
plainly present (a full scan returns them) and the equivalent equality/`IS NULL` predicates work.
The result is silently wrong (empty), not an error. The defect is specific to the arbitrary-precision
`NUMERIC`/DECIMAL type: the identical `INDEX OFF` + range pattern on `BIGINT`, `DOUBLE PRECISION`,
`REAL`, `TEXT`, and `TIMESTAMP` columns all return the correct rows. The mechanism is that CrateDB
resolves range predicates through the column's index/columnstore, and `NUMERIC` has neither a range
index nor a columnstore fallback when `INDEX OFF` is set, so the range term matches nothing instead
of falling back to a source scan (`=` and `<>` do fall back and are correct).

## Environment

- **Engine**: CrateDB **6.4.1** (built `45bfa80`) and **6.4.2** (built `1db6455`), official
  `crate:6.4.1` / `crate:6.4.2` Docker images, single node, `CLUSTERED INTO 1 SHARDS WITH
  (number_of_replicas = 0)`.
- **Access path**: PostgreSQL wire protocol via `psycopg` 3.3.4. Reproduces identically on both
  builds.
- **Session**: defaults (`error_on_unknown_object_key = true`, `insert_select_fail_fast = true`;
  neither is relevant to the result).
- No `sql_mode`/collation knobs apply.

## Minimal repro

```sql
CREATE TABLE t (c_pk BIGINT, c_dec NUMERIC(10, 2) INDEX OFF)
    CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t (c_pk, c_dec) VALUES (2, -5.50), (3, 12.34), (4, 0.00);
REFRESH TABLE t;

SELECT c_pk, c_dec FROM t;                 -- (2,-5.50) (3,12.34) (4,0.00)   -- rows ARE present
SELECT c_pk FROM t WHERE c_dec < 0.00;     -- Expected [2]     Actual []     <<< WRONG
SELECT c_pk FROM t WHERE c_dec >= 12.34;   -- Expected [3]     Actual []     <<< WRONG
SELECT c_pk FROM t WHERE c_dec BETWEEN 0 AND 100;  -- Expected [3,4]  Actual []  <<< WRONG

-- controls that behave CORRECTLY:
SELECT c_pk FROM t WHERE c_dec = 12.34;    -- [3]      (equality falls back to a source scan)
SELECT c_pk FROM t WHERE c_dec <> 12.34;   -- [2,4]
SELECT c_pk FROM t WHERE c_dec IS NULL;    -- []
```

The full, runnable version with all controls is `reduced.sql` in this folder; re-run it against any
`crate:<ver>` build.

## Expected vs actual

The **base** table (index on) is the correct side; the `INDEX OFF` relation is the buggy one — it
returns *too few* rows (nothing) for a range predicate.

Same 5 rows in every table (`c_pk, c_dec`): `(1,NULL) (2,-5.50) (3,12.34) (4,0.00) (5,-0.01)`.
`plain` = column index on; `idxoff` = `NUMERIC(10,2) INDEX OFF`:

| predicate                    | plain (correct) | idxoff (actual) | verdict |
|------------------------------|-----------------|-----------------|---------|
| `c_dec < 0.00`               | `[2, 5]`        | `[]`            | WRONG   |
| `c_dec > 0.00`               | `[3, 5]`        | `[]`            | WRONG   |
| `c_dec >= 12.34`             | `[3, 5]`        | `[]`            | WRONG   |
| `c_dec BETWEEN 0 AND 100`    | `[3, 4]`        | `[]`            | WRONG   |
| `c_dec = 12.34`              | `[3]`           | `[3]`           | ok      |
| `c_dec <> 12.34`             | `[2, 4, 5]`     | `[2, 4, 5]`     | ok      |
| `c_dec IS NULL`              | `[1]`           | `[1]`           | ok      |

The correct answer is established directly: the values `-5.50`, `12.34` are returned verbatim by
`SELECT c_dec FROM t` on the very same `INDEX OFF` relation, so `-5.50 < 0.00` and `12.34 >= 12.34`
are unarguably true; returning zero rows is wrong.

## Equivalence construction

**How eqgen surfaced it.** The differential oracle builds a second relation that holds *exactly the
same rows and declared types* as the base table `t`, then runs the identical workload query against
both. Here the equivalent `t` is a chain that ends in a table whose every column is `INDEX OFF`
(builder `CrateDbIndexOffBuilder`), exposed through a view — concretely:

```sql
CREATE TABLE t__base_table_18 (c_pk BIGINT INDEX OFF, c_int BIGINT INDEX OFF, c_big BIGINT INDEX OFF,
    c_dec NUMERIC(10, 2) INDEX OFF, c_dbl DOUBLE PRECISION INDEX OFF, ...) CLUSTERED INTO 1 SHARDS ...;
INSERT INTO t__base_table_18 (...) SELECT ... FROM t__base_table_17;
CREATE VIEW t AS SELECT c_pk, c_int, ..., c_dec, ... FROM t__base_table_18;
```

The object pre-gate confirmed the two `t`s are row- and type-identical (`SELECT * FROM t` agrees, and
`c_dec` is `NUMERIC(10,2)` on both, driver type_code `1700` on both), so the equivalence is
**admissible**. Then the workload query `SELECT ... FROM t WHERE c_dec < 0.00` returned 3 rows on the
base and 0 on the `INDEX OFF` equivalent — a real divergence.

**Load-bearing ingredient.** Exactly one column attribute: `INDEX OFF` on a `NUMERIC` column, plus a
range operator. It is **not** a composition — no join, view nesting, `OBJECT` pack, partitioning, or
CTE is required (those were all reduced away, and each was independently shown to filter `c_dec`
correctly). The trigger is `NUMERIC × INDEX OFF × range operator`.

## Minimal oracle exposure path

**Object composition arity:** `3`

**GCL builder path:** `CrateDbIndexOffBuilder[TABLE]` → `CreateViewBuilder`

**Confidence:** verified

**Realization:** An `INDEX OFF` `TABLE` is exposed through a final `VIEW`.

**Workload/data requirements (excluded from arity):**
- A `NUMERIC`/DECIMAL column declared `INDEX OFF`.
- An ordered comparison or `BETWEEN` predicate.
- Values on both sides of the tested range so dropped rows are visible.

**Exposure vs. intrinsic trigger:** The `CrateDbIndexOffBuilder` table layout remains intrinsic to the standalone trigger, but the final view is only the oracle exposure layer and reduces away. The statement above that this is “not a composition” refers to trigger semantics: object-path arity counts the builder’s table realization and exposure view, whereas the engine bug itself needs only `NUMERIC × INDEX OFF × range`.

## Characterization

- **Type-specific.** Repeating the exact `INDEX OFF` + range test on `BIGINT`, `DOUBLE PRECISION`,
  `REAL`, `TEXT`, and `TIMESTAMP` columns all return the correct rows. Only `NUMERIC`/DECIMAL is
  wrong.
- **Operator-specific.** `=`, `<>`, and `IS NULL` on the same `NUMERIC INDEX OFF` column are correct;
  only the ordered comparisons `<`, `>`, `<=`, `>=`, `BETWEEN` are wrong.
- **Not a value/precision artifact.** The values compare bit-exactly (`Decimal('-5.50')`), so no float
  tolerance is involved; the full scan returns them unchanged.
- **Mechanism (hypothesis).** CrateDB evaluates a range predicate as an index/columnstore term-range
  query. `NUMERIC` (BigDecimal-backed) supports neither a range index nor a columnstore, so with
  `INDEX OFF` the range term resolves against nothing and yields the empty set, with no fallback to a
  generic source scan. Equality/`<>` do fall back to a source-value filter, which is why they are
  correct. `EXPLAIN` on the range query shows an index term-range scan rather than a generic filter.
- **DML blast radius**: not separately tested; a `SELECT` wrong-result on a widely-used column
  attribute is already high impact (any range filter / `BETWEEN` / range join key on such a column
  silently drops rows). Worth confirming whether `DELETE ... WHERE c_dec < x` under-deletes.
- **Workaround** (matches the community threads below): cast in the predicate, e.g.
  `WHERE c_dec::numeric >= 12.34` — but this a wrong-result, not merely a performance, issue, so it
  should raise or scan rather than return the empty set.

## How it was found

The data-equivalence oracle fixes the query and swaps in a row-identical relation, so a trivial
`SELECT ... WHERE c_dec < 0.00` becomes the probe: it ran that same text against the plain base and
against an `INDEX OFF` copy holding the identical rows, and they disagreed. A query-rewrite oracle
(TLP/NoREC/EET) would have missed this — it holds the *table* fixed and rewrites the *query*, so it
never compares "the same rows with the index on" against "the same rows with the index off", which is
the only thing that differs here. Seed `20260813`, run `hunt1/cratedb_20260813-202101/`,
first seen round 2 (`mismatch_round2_7.sql`). One root cause accounts for the entire round-2 burst
(27 findings) and round-3 burst (15 findings): every query in those rounds has a `c_dec` range
predicate and the round's exposed object is the `INDEX OFF` table — confirmed by re-checking that
each such query is correct against the same object with the index left on.

## Open items

- Confirm on the latest 6.x nightly and on 5.10/5.9 LTS to bound the regression/limitation window.
- File upstream (crate/crate) referencing the 5.3.2 note, unless a 6.x-specific `NUMERIC INDEX OFF`
  issue already exists.
- Determine whether `DELETE`/`UPDATE ... WHERE <numeric range>` on such a column is equally affected
  (data-loss severity vs. read-only wrong result).
- Suggested fix direction: for a `NUMERIC INDEX OFF` column with no columnstore, either fall back to a
  generic source-value filter for range operators (as `=`/`<>` already do) or reject the query rather
  than silently returning the empty set.
```
