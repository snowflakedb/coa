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

# CrateDB: `WHERE <bare boolean column>` silently dropped when the column is projected with an alias through a join + filtered subquery

## Summary

A top-level `WHERE c_flag` predicate — where `c_flag` is a bare `BOOLEAN` column reference — is
**silently discarded** when the relation it filters is a subquery/view that (a) projects that
column with an **explicit alias** (`... AS c_flag`), (b) contains a **join**, and (c) has **its own
`WHERE` clause**. The optimizer pushes the outer boolean predicate down into the base-table scan
and, crossing the aliased projection, rewrites it to the literal `true` (visible in `EXPLAIN` as
`Collect[... | true]`), so the filter has no effect and **every row of the subquery is returned**.
The broken invariant is the most basic one there is: `SELECT c_flag FROM v WHERE c_flag` must never
return a row where `c_flag` is `false` or `NULL`. The defect is deterministic given those
ingredients, and disappears if any one is removed (drop the alias, the join, the inner `WHERE`, or
wrap the boolean as `= TRUE`).

## Environment

- **Engine**: CrateDB 6.4.1, release tarball, commit `45bfa80` (`SELECT version()` →
  `CrateDB 6.4.1 (built 45bfa80/NA, …)`), aarch64.
- **Assertions**: on (`-ea -esa` via `CRATE_JAVA_OPTS`) — no assertion fires; this is a
  wrong-result bug, not a crash.
- Single node, `discovery.type=single-node`; default session settings
  (`enable_hashjoin=true`, `optimizer_equi_join_to_lookup_join=true`).
- No `sql_mode` / collation dimensions apply (CrateDB).

## Minimal repro

See [`reduced.sql`](./reduced.sql) — it runs, verbatim, in one database (verified against the live
6.4.1 server). Distilled core:

```sql
CREATE TABLE lft (c_flag BOOLEAN, u BIGINT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO lft VALUES (true, 1), (false, 2);          REFRESH TABLE lft;
CREATE TABLE rgt (u BIGINT, f BIGINT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO rgt VALUES (1, 1), (2, 1);                 REFRESH TABLE rgt;

SELECT c_flag
FROM (SELECT l.c_flag AS c_flag              -- <-- the alias is load-bearing
      FROM lft l JOIN rgt r ON l.u = r.u
      WHERE r.f = 1) x
WHERE c_flag;          -- returns BOTH (true) and (false)
```

## Expected vs actual

| Query | Expected | Actual |
|---|---|---|
| distilled repro above (`AS c_flag`, join, inner WHERE, bare `c_flag`) | `(true)` | **`(true)`, `(false)`** |
| concrete as-found view (below), `SELECT c_flag FROM t WHERE c_flag` | 4×`(true)` | **4×`(true)`, 3×`(false)`, 1×`(null)`** |
| control (a): drop the alias — `SELECT l.c_flag …` | `(true)` | `(true)` ✓ |
| control (b): drop the inner `WHERE` | `(true)` | `(true)` ✓ |
| control (c): drop the join (single-table subquery) | `(true)` | `(true)` ✓ |
| control (d): `WHERE c_flag = TRUE` instead of bare | `(true)` | `(true)` ✓ |

With a single `(false)` row present, the buggy query returns that `(false)` row where it should
return nothing — proving the predicate is dropped entirely, not merely mis-evaluated.

## Equivalence construction

### Concrete, as the builder emits it

The eqgen equivalent `t` for these findings is an **`eq_uid` join-reattachment view**: tag each
base row with a unique key via `ROW_NUMBER() OVER (ORDER BY c_int)`, split the key + a constant
flag into a companion table, then rejoin and keep `eq_flag = 1`:

```sql
-- (base table t, 8 rows, renamed aside to t__base)
CREATE VIEW t AS
  SELECT l.c_int AS c_int, …, l.c_flag AS c_flag, …          -- every column self-aliased l.X AS X
  FROM   t__base_table_3 l                                    -- base rows + eq_uid key
         FULL OUTER JOIN t__base_table_4 r ON l.eq_uid_1 = r.eq_uid_1   -- companion (eq_uid, eq_flag=1)
  WHERE  r.eq_flag_2 = 1;
```

This view is **row- and type-identical to the base table `t`** (oracle admissibility verified — see
below), so it is a legal equivalent. `reduced.sql` PART 1 is a faithful, compact rebuild of exactly
this shape and reproduces the finding (`{(true)×4, (false)×3, (null)×1}`). The workload query is
then the trivial `SELECT c_flag FROM t WHERE t1.c_flag`.

### The load-bearing composition

The trigger is a **four-way composition**, and removing any single element fixes the result
(controls (a)–(d) above):

1. **an explicit column alias in the subquery projection** — `l.c_flag AS c_flag`. This is the
   ingredient the builder supplies unconditionally: it emits `l.X AS X` for *every* column, so the
   condition is always met. A *different* alias name (`l.c_flag AS g`, `… WHERE g`) triggers it too;
   the bug is the presence of the alias, not that it matches the source name. Projecting the column
   *without* an alias (`l.c_flag`) evaluates correctly.
2. **a join** inside the subquery — `FULL OUTER JOIN` (rounds 6/21/34/42), `INNER JOIN` (round 38),
   `LEFT OUTER JOIN`, and comma/`CROSS` + join predicate all trigger. A single-table subquery does
   not.
3. **the subquery's own (non-constant) `WHERE`** — `r.eq_flag = 1`, `r.f = 1`, `l.u > 0` all
   trigger; a constant `WHERE 1=1` does not; dropping it fixes the result.
4. **a bare boolean column** as the whole outer predicate — wrapping it as `= TRUE`, `IS TRUE`, or
   `NOT …` all evaluate correctly.

Constructs reduced away as irrelevant: the `ROW_NUMBER`/`eq_uid` tagging, the `UNION ALL` split
(round 34), the multi-view chain, the `FULL OUTER` vs `INNER` join choice, the number of base
columns, and the view wrapper (an inline derived table reproduces it identically).

## Minimal oracle exposure path

**Object composition arity:** `2`

**GCL builder path:** `FlagTableJoinQueryBuilder` → `CreateViewBuilder`

**Confidence:** verified

**Realization:** A `VIEW` exposes the row-preserving flag-table join with its aliased projection and inner filter.

**Workload/data requirements (excluded from arity):**
- An explicitly aliased projected boolean column.
- A join plus a non-constant inner `WHERE`.
- The projected column used as a bare outer boolean predicate.
- A false or NULL row so loss of the predicate is observable.

**Exposure vs. intrinsic trigger:** The flag join's join/filter/alias shape remains in the standalone trigger, while its generated row key, companion-table details, join type, and view wrapper reduce away or can be inlined. The builder path provided the row-identical relation that made the dropped outer predicate visible; the intrinsic trigger is the aliased join subquery with inner filtering, not the bookkeeping objects.

## Characterization

`EXPLAIN` shows the mechanism directly — the pushed-down scan filter on the left relation
**degenerates to `true`** in the buggy form, and survives in every control:

```
-- BUG  (alias + join + inner WHERE + bare c_flag): predicate lost on the lft scan
Rename[c_flag] AS x
└ HashJoin[INNER | (u = u)]
  ├ Rename[c_flag, u] AS l
  │  └ Collect[…lft | [c_flag, u] | true]              <-- predicate replaced by `true`
  └ Rename[u] AS r
    └ Collect[…rgt | [u] | (f = 1::bigint)]

-- control (a), no alias:            Collect[…lft | [c_flag, u] | c_flag]          (kept)
-- control (d), WHERE c_flag = TRUE:  Collect[…lft | [c_flag, u] | (c_flag = true)] (kept)
```

So when the subquery both carries its own `WHERE` and projects the column under an alias, CrateDB’s
predicate-pushdown/merge step mis-resolves the outer bare-boolean reference and rewrites it to
`true`. (In some neighbouring plans the same step instead *duplicates* it to `c_flag AND c_flag` —
harmless, correct — which is further evidence the fault is in predicate combination, not
evaluation.) Not a crash; no assertion fires under `-ea`.

## Why the differential *data-equivalence* oracle found this — and a query-equivalence oracle would not

This was found by the **eqgen v3 Data Equivalence Generator's differential/metamorphic oracle**,
and the *class* of oracle is the reason it surfaced at all.

**What this oracle does (relation/data equivalence).** It fixes the *query* and swaps the *relation*
underneath it: from a base table `t`, it builds a second relation `t'` that is provably identical as
a bag of rows and column types (here the `eq_uid` join-reattachment view), then runs the **same**
workload query against both. The two relations are logically identical by construction, so any
difference in the result multiset is, definitionally, an engine divergence — no reference engine and
no expected output are needed. Crucially, the workload query can be trivial: `SELECT c_flag FROM t
WHERE c_flag`. All of the structural complexity that trips the optimizer — the alias, the join, the
inner filter — lives in the *definition of `t'`*, injected by the equivalence builder, **not** in the
query. The oracle therefore decouples *what triggers the bug* (the relation shape) from *what makes
it observable* (an unchanged, obviously-correct query). That decoupling is exactly what caught this:
the query "`SELECT c_flag … WHERE c_flag`" is so plainly correct that its result on `t'` disagreeing
with its result on `t` is an unambiguous contradiction.

**Why a query-equivalence oracle misses it.** The other major oracle family holds the *database*
fixed and rewrites the *query* into a provably equivalent query `Q'` (TLP’s `p / NOT p / p IS NULL`
partitioning, NoREC’s move-the-predicate-into-the-projection, EET/CODDTest expression rewrites),
then compares `Q(D)` with `Q'(D)`. That family is structurally ill-suited here:

- **It never manufactures the triggering relation.** Starting from `SELECT c_flag FROM t WHERE
  c_flag` over the base *table*, none of these rewrites replace `FROM t` with a *join-with-inner-WHERE-
  and-aliased-projection* subquery — that is a data-side transformation, outside the query-rewrite
  vocabulary. Over the plain table the predicate is applied correctly, so both `Q` and `Q'` agree and
  nothing is flagged.
- **Its canonical rewrites dismantle the exact trigger.** The bug needs the boolean to stay a *bare
  column reference in `WHERE`*, under an *alias*, above a *join+filter*. NoREC’s core move is to pull
  the predicate out of `WHERE` and into the projection (`SELECT c_flag FROM …`) — which removes the
  bare-`WHERE` form. TLP re-expresses the filter as `p`, `NOT p`, `p IS NULL` — `NOT c_flag` and the
  null branch all evaluate correctly here (only the bare `c_flag` branch fails), and TLP compares a
  *union of three rewrites* against the unfiltered relation, so its signal depends on the generator
  having independently produced the aliased join+filter subquery in the first place. In short, the
  query-rewrite oracles either transform away the fragile shape or would only stumble onto it by
  chance; they cannot, as this oracle does, hold a trivial query fixed and slide an arbitrarily
  complex but row-identical relation beneath it.

A plain single-query, single-database fuzzer is even further out: the offending query looks like an
utterly ordinary `SELECT c_flag FROM v WHERE c_flag`, and with nothing to compare against there is no
way to notice the filter silently did nothing.

- **Seed**: 110614123 (round 34); the same bug independently recurred at seeds 327608162 (round 6),
  431657360 (round 21), 399762710 (round 38), 1267237918 (round 42).
- **Scope**: **all 20 mismatch findings in this run reproduce and pass oracle admissibility**
  (base `t` ≡ equivalent `t`). 18 of 20 are the exact `… FROM t WHERE t1.c_flag` shape and are
  direct instances of this one bug. The two round-6 findings (`mismatch_round6_0`,
  `mismatch_round6_1`) query the same aliased join-reattachment view with more complex predicates
  (`CAST(… AS BOOLEAN) AND (… < ANY (subquery))`, `… <= ANY (subquery)`) and were not independently
  reduced; they are consistent with the same predicate-handling defect but may involve an additional
  `ANY`-subquery interaction.
- Reduced repro: [`reduced.sql`](./reduced.sql).
- Original findings: hunt log —
  `mismatch_round34_5.sql` is the simplest (`SELECT t1.c_flag FROM t WHERE t1.c_flag`).
