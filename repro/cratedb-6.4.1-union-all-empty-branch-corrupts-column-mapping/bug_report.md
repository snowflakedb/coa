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

# CrateDB: a `UNION ALL` branch that is empty but has a join somewhere in its lineage corrupts downstream processing of the union's live rows — with no error and no dependence on which columns are involved

## Summary

`SELECT * FROM (<empty relation with a join in its lineage>) UNION ALL SELECT * FROM <live table>`
returns correct rows for a table-order, full-width projection, but corrupts the result the moment
the query does anything else with it: reorders a full-width projection, sorts by a column excluded
from the projection, or joins the union against a third relation on an equi-condition. Three
symptoms, same root construction, only the query (Symptoms A/B) or the surrounding composition
(Symptom C) differs:

- **A — Reordered full-width projection, no `ORDER BY`.** `SELECT c_pk, id, created_at, name FROM v`
  returns `created_at` and `name`'s values transposed — silently, with no error.
- **B — `ORDER BY` on a column absent from the projection.** `SELECT name, created_at FROM v ORDER BY
  c_pk` doesn't just misplace values — the second row comes back as `(NULL, NULL)`, i.e. a live
  row's data goes missing entirely.
- **C — The union is later equi-joined against an unrelated table.** Confirmed by bisection on a
  real 8-row finding (not yet reduced to a from-scratch minimal case — see **Symptom C**): joining
  a `UNION ALL` relation with this empty-branch shape against a second table, `ON` a condition that
  sits in the outer `WHERE` rather than the join's `ON` clause, drops 5 of 8 rows and duplicates one
  of the survivors.

This report supersedes two findings originally filed as separate bugs
(`cratedb-6.4.1-union-all-reordered-full-width-projection` and
`cratedb-6.4.1-union-all-empty-join-branch-order-by-column-leak`, both folded into this directory),
plus a third pair of mismatches (`mismatch_round76_{2,15}`) that bisection ties to the same class.
The two originals are **one root cause**: reduced independently, each pinned the trigger to an empty
`UNION ALL` branch built from an equi-join, and the minimal repro for one symptom was checked and found to
produce the other symptom under the other symptom's query shape — the same tiny view, same two rows,
same database, both bugs. See **Unification**.

## Environment

- **CrateDB 6.4.1**, `built 45bfa80/NA`, OpenJDK 25.0.3+9-LTS, Linux aarch64 — Docker `crate:6.4.1`.
- Single node, `CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0)`.
- Access path: PostgreSQL wire protocol via `psycopg`. Not retried through HTTP `_sql` (see Open
  items).
- No session settings involved. The distilled repro is a plain `CREATE TABLE` + 2 `INSERT`s + 1
  `CREATE VIEW` + 1 query.

## Minimal repro (from scratch, verified live — no builder chain required)

```sql
CREATE TABLE a (c_pk BIGINT, id BIGINT, name TEXT, created_at TEXT)
  CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO a VALUES (1, 10, 'x', 'p');
INSERT INTO a VALUES (2, 20, 'y', 'q');
REFRESH TABLE a;

CREATE VIEW v AS
  SELECT * FROM (SELECT l.c_pk, l.id, l.name, l.created_at FROM a l JOIN a r ON l.c_pk = r.c_pk) j
   WHERE 1 = 0
  UNION ALL
  SELECT * FROM a;
```

**Symptom A — reordered full-width projection, no `ORDER BY`:**

```sql
SELECT c_pk, id, created_at, name FROM v;
-- expected: (1, 10, 'p', 'x'), (2, 20, 'q', 'y')
-- actual:   (1, 10, 'x', 'p'), (2, 20, 'y', 'q')     -- positions 3/4 transposed
```

**Symptom B — `ORDER BY` on a column outside the projection, same view:**

```sql
SELECT name, created_at FROM v ORDER BY c_pk;
-- expected: ('x', 'p'), ('y', 'q')
-- actual:   ('x', 'p'), (NULL, NULL)                -- second row's data is gone
```

Both were re-run 4 times against fresh databases; both are stable (deterministic), and Symptom A
was confirmed on all four columns as the join predicate (`c_pk`, `id`, `name`, `created_at`) and
independent of the join column's position in the branch's `SELECT` list — see **Characterization**.

## Expected vs actual

| Query (against `v` above) | Expected | Actual |
|---|---|---|
| `SELECT c_pk, id, name, created_at FROM v` (table order) | `(1,10,'x','p'),(2,20,'y','q')` | correct |
| `SELECT c_pk, id, created_at, name FROM v` (Symptom A) | `(1,10,'p','x'),(2,20,'q','y')` | `(1,10,'x','p'),(2,20,'y','q')` |
| `SELECT name, created_at FROM v ORDER BY c_pk` (Symptom B) | `('x','p'),('y','q')` | `('x','p'),(NULL,NULL)` |

**CrateDB is the wrong side on both.** `v` is `UNION ALL` of an always-empty derived table and `a`
itself, so `v`'s rows are exactly `a`'s rows. Ground truth comes directly from `a`, which is
row-identical to `v` under a plain `SELECT * FROM v` — the two queries above disagree with that
ground truth in different ways (transposed values vs. a vanished row), not with each other.

## Unification

Each finding was reduced independently to "an empty branch containing an equi-join, unioned with a
live table" — see **History of the two findings** below for each one's own reduction chain. Once both
had converged on the same shape, the natural next question was whether they were actually the same
defect wearing two query-shaped masks. They are:

- The `reduced.sql` view from the (formerly) "reordered full-width projection" finding, run under an
  `ORDER BY` on the excluded `c_pk` column, reproduces the *other* finding's symptom exactly — a
  vanished row, not a transposition.
- The 4-column, 2-row, from-scratch minimal repro above reproduces **both** symptoms from a single
  `CREATE VIEW`, with no window functions, no `OBJECT(STRICT)` pack/unpack, no `ROW_NUMBER` join key,
  and no `PARTITIONED BY` round-trip — none of which the original 20-30-statement builder chains
  behind each finding turn out to need.
- The join predicate is irrelevant: tested `ON l.c_pk = r.c_pk`, `ON l.id = r.id`, `ON l.name =
  r.name`, `ON l.created_at = r.created_at` — all four produce byte-identical wrong output for
  Symptom A. Only the **presence of an equi-join** (an `ON`-qualified join, as opposed to a plain
  `CROSS JOIN`) inside the emptied branch matters, matching the sibling finding's control (b).
- Column position within the branch's own `SELECT` list is also irrelevant: reordering the branch to
  project `id, c_pk, name, created_at` (matching the outer table's column order to that) still
  transposes the last two columns under the equivalent outer reorder.

So there is one engine defect: **an equi-join inside a `UNION ALL` branch that filters to zero rows
still leaves behind an artifact of that join's output-slot layout, which the query planner then
misapplies to the branch that does carry rows** — whether "misapplies" surfaces as a swapped pair of
values (Symptom A) or a lost row (Symptom B) apparently depends on what the outer query asks the
planner to do with the union's result (reorder columns vs. sort by an excluded one), not on anything
about the join itself.

## Minimal oracle exposure path

**Object composition arity:** `3`

**GCL builder path:** `FlagTableJoinQueryBuilder` → `UnionEmptyRoundTripBuilder` → `CreateViewBuilder`

**Confidence:** verified

**Realization:** A `VIEW` exposes a `UNION ALL` whose empty arm retains equi-join lineage.

**Workload/data requirements (excluded from arity):**
- One union arm filtered to empty but containing an equi-join in its lineage.
- A live arm with at least two rows.
- An outer projection reorder, an unselected `ORDER BY` key, or a later equi-join.
- Enough columns for the requested projection to exercise slot remapping.

**Exposure vs. intrinsic trigger:** The join-bearing empty arm, empty-union round trip, and exposed union relation all remain in the standalone trigger, although the generated flag/key machinery and deeper historical chains reduce away. The builders therefore supplied both the row-identical contrast and the intrinsic lineage whose stale output-slot layout is applied to live rows.

## Characterization

- **All three gates pass, both symptoms**: query reproduces; base ≡ equivalent (row-identical);
  declared types identical; both sides stable across repeated runs (4 for the from-scratch repro,
  4 originally for each finding); the harness's engine-equality gate confirmed the differing rows are
  genuinely unequal under CrateDB's own comparison for both original findings.
- **No error, no warning**, in either symptom. The query succeeds and returns wrong data.
- **Join column tested exhaustively** on the 4-column table: `c_pk`, `id`, `name`, `created_at` as
  the join predicate all produce the identical wrong Symptom-A output. A `CROSS JOIN` (no `ON`)
  in the same position is correct — confirmed both originally (sibling finding's control (b)) and
  again on the from-scratch repro.
- **No dependence on which physical columns get reordered** — swapping the branch's own projection
  order and mirroring it in the outer `UNION ALL`'s live branch still transposes whichever two
  columns land in the reordered outer projection's last two slots.
- **Accounts for all 8 of the original mismatches across the two runs, not the 5+3 split each
  finding separately proved**: 5 of round 319's 8 mismatches were silenced by table-order rewrite
  (Symptom A, this construction), and the other 3 plus `mismatch_round0_{9,13,7}` were silenced by
  dropping `ORDER BY` (Symptom B, this construction) — see each original finding's own accounting
  below.
- **A plausible mechanism, not confirmed**: a full-width projection may get recognised as equivalent
  to `SELECT *` (explaining why width is necessary for Symptom A and why a subset projection is
  always safe), and an `ORDER BY` on a column absent from the projection may force a materialization
  step that reuses a row-buffer offset computed from the emptied join branch's *own* output shape
  rather than the union's declared output shape (explaining Symptom B's `NULL` — the copy reads past
  where that branch's own row would have ended, landing outside the actual data and reading as
  unset/NULL). Both are consistent with "an artifact of the join's planned layout survives the branch
  producing zero rows," but neither has been confirmed against source (unavailable on this machine).
- **DML not tested.** Engine source not consulted, so no `file:line`.

## History of the two findings (kept for provenance)

### Finding 1 — "reordered full-width projection" (no `ORDER BY`)

Found in hunt C (simple catalog, forks 1, seed 31003), round 319, via eqgen's builder chain
(`UNION ALL` of three branches; the load-bearing one an EET per-column `CASE` projection emptied by
an always-false filter, itself built over a `ROW_NUMBER`-keyed self-join recombining two
vertically-split halves of a `LAST_VALUE`-windowed, `OBJECT(STRICT)`-packed source). Accounted for
5 of 8 mismatches in that round (`mismatch_round319_{0,17,22,3,4}`) by rewriting each to table-order
column order. A second occurrence, `mismatch_round0_14` from a separate run/catalog
(`log/cratedb_20260813-053520`), corroborated it on a 9-column table with a shuffled `SELECT
DISTINCT` projection.

Its own bisection was inconclusive on why the shape mattered: substituting a flattened copy of the
load-bearing branch fixed the divergence, but rebuilding just that branch's *outer* shape (an
EET `CASE` view over an always-false filter) synthetically did not reproduce — something further
down the chain was needed, and that something turned out (per Unification, above) to be the equi-join
several steps further down, which the original bisection had not yet reached.

### Finding 2 — "empty equi-join branch leaks `ORDER BY` column into projection"

Found via eqgen's builder chain at seed 884601542, round 0: a 3-way `UNION ALL` whose branch 0 is a
`RIGHT OUTER JOIN`-based view, emptied by an always-false EET filter. Accounted for 3 of 4 mismatches
in that run (`mismatch_round0_{9,13,7}`) by dropping `ORDER BY`. Reduced, independently of Finding 1,
to the exact "empty branch containing an equi-join, unioned with a live table, `ORDER BY` on a
column outside the projection" shape that the Unification section above starts from. Its own
`mismatch_round0_14` was flagged as a related-but-unreduced fourth mismatch at the time — it is
Finding 1's symptom, on this run's catalog, and is accounted for by Finding 1 above.

## How it was found

eqgen's data-equivalence oracle: it holds the query fixed and swaps in a relation proven to hold the
same rows, so a query as trivial as `SELECT c_ts FROM t ORDER BY c_pk` or `SELECT <4 cols> FROM t`
becomes the probe. The trigger is a property of the *relation* — an emptied-by-filter branch whose
body happens to be a join — which is exactly what a query-rewrite oracle (TLP/NoREC/EET) cannot vary,
since those hold the relation fixed and mutate the query.

- `reduced.sql` in this directory — the unified from-scratch repro, both symptoms, plus controls.
- Original findings: `log/crate/cratedb_20260813-053044/mismatch_round319_{0,17,22,3,4}.sql`,
  `log/cratedb_20260813-053520/mismatch_round0_{9,13,7,14}.sql`.

## Open items

- **Regression window undetermined.** Only `crate:6.4.1` is available locally; unknown whether
  earlier 6.x or 5.x are affected, and unknown whether this is a regression introduced near #14807's
  fix (PR #14816) or unrelated to it.
- **DML impact untested** — run the same shape under `UPDATE`/`DELETE` to bound severity.
- **Second access path untested** — reproduce through the HTTP `_sql` endpoint to rule out the
  PostgreSQL-wire client layer. A wrong column *value* of the right type (Symptom A) or a `NULL` where
  live data exists (Symptom B) are both unlikely to be decoding artefacts, but neither has been
  excluded this way.
- **Mechanism unconfirmed against source** — CrateDB source was not available to inspect; the
  row-buffer-offset hypothesis in Characterization is a plausible story, not a confirmed one.
- **No suggested fix.**
