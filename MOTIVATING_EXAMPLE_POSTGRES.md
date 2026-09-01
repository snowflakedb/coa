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

# Motivating example (PostgreSQL)

Take a table. Build a second object that holds exactly the same rows — not by copying them in
Python, but by stacking rewrites that each keep the bag: a view, a doubled relation with
`DISTINCT` taken back down to one copy, a window that is an identity. Read both objects. If
the rows match, run the **same** query on both.

A query-rewrite oracle (TLP, NoREC, two `SELECT`s claimed equal) cannot do this. It holds the
relation fixed and mutates the query, so it has to prove the two texts mean the same thing.
Here the query text never changes. The object does. A ten-layer object is not a ten-step
proof: it is one more relation, checked by reading it.

PostgreSQL 20devel shows why that distinction is not academic, and also where it is thinner
than a composed-object bug. The engine agrees that the rewritten object has the same rows as
the heap. The same `COVAR_POP` then returns `NaN` on the heap and `0.0` on the rewrite. The
rewrite is how the two sides disagreed. It is not what is wrong. `float8_regr_accum` is
secretly order-dependent: a constant axis plus `Infinity` later in the input leaves
covariance at exact 0. The heap happened to see Inf first. `UNION ALL` plus `DISTINCT` took
a HashAgg/Unique plan and moved Inf off the first non-null slot.

A query-rewrite oracle that holds the table fixed never takes that second probe order. Both
of its `SELECT`s return `NaN` and look consistent.

## The algebra

`DistinctUnionDuplicateQueryBuilder` is bag identity: double the relation, then `DISTINCT`
over the base columns plus a synthetic row key, then drop the key. Without the key, plain
`SELECT DISTINCT *` would collapse genuine duplicates. With it:

```text
R ∪ R          same bag, twice the multiplicity
  DISTINCT     one copy of each (row + key)
  project      the key comes off; the bag is R again
```

That is \(R\) as a set-op plus a Unique/HashAgg, not \(R\) as a seq-scan. The hunt's `t2`
was this shape (window-`MAX` dedup over a doubled bag). Collapsing to

```sql
CREATE VIEW t2 AS SELECT DISTINCT * FROM (
  SELECT * FROM t__base UNION ALL SELECT * FROM t__base
) s;
```

is enough. Even `SELECT DISTINCT y FROM t` is enough when `y` has no genuine duplicates. The
identity is legal. The engine's aggregate is not invariant under the order that identity
happens to use.

## The object, distilled

PostgreSQL **20devel** (`--enable-cassert --enable-debug`, `CFLAGS=-O1`), source HEAD
`36f7330b8b2238c2093d7eac521f996b33e66121`. Private socket cluster. `locale=C`,
`standard_conforming_strings=on`.

The hunt used an 8-row catalog and a three-table join. The load-bearing rewrite on `t2`
reduces to Distinct∪UNION ALL. The defect reduces further, to three floats and a constant
axis. That last reduction is the root cause. It is not how the oracle found it.

```sql
CREATE TABLE t (
  c_pk INTEGER NOT NULL, c_int INTEGER, c_big NUMERIC(38, 0),
  c_dec NUMERIC(10, 2), c_dbl DOUBLE PRECISION,
  c_txt TEXT, c_chr TEXT, c_date DATE, c_ts TIMESTAMP
);
INSERT INTO t VALUES (1, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);
INSERT INTO t VALUES (2, NULL, 0, 0.0, NULL, 'Zed', 'trailing ', '1999-12-31', '2024-01-15 12:34:56');
INSERT INTO t VALUES (3, NULL, 2, -5.5, NULL, 'a', 'a', '1999-12-31', '1999-12-31 23:59:59');
INSERT INTO t VALUES (4, -7, 0, NULL, NULL, 'abc', '', '1999-12-31', '2024-01-15 12:34:56');
INSERT INTO t VALUES (5, 42, -7, 999.99, -1.5, 'Zed', '', NULL, '1999-12-31 23:59:59');
INSERT INTO t VALUES (6, -7, -7, 999.99, 1000.125, 'o''brien', 'o''brien', '2030-06-01', '2024-01-15 12:34:56');
INSERT INTO t VALUES (7, 42, 2, -5.5, 1000.125, 'Zed', NULL, '2024-01-15', NULL);
INSERT INTO t VALUES (8, NULL, 0, 0.0, NULL, 'Zed', 'trailing ', '1999-12-31', '2024-01-15 12:34:56');

CREATE TABLE t0 AS SELECT * FROM t;
CREATE TABLE t1 AS SELECT * FROM t;
CREATE TABLE t2 AS SELECT * FROM t;
```

On the equivalent side the inserts are the same, then `t` is renamed aside and `t2` is the
identity:

```sql
ALTER TABLE t RENAME TO t__base;
CREATE TABLE t0 AS SELECT * FROM t__base;
CREATE TABLE t1 AS SELECT * FROM t__base;
CREATE VIEW t2 AS SELECT DISTINCT * FROM (
  SELECT * FROM t__base UNION ALL SELECT * FROM t__base
) s;
```

`t2` on that side is the same bag as the heap `t2`. Declared types match.

## The check

eqgen does not trust the stack because the builders say so. It reads both sides:

```sql
SELECT * FROM t2;     -- heap copy
SELECT * FROM t2;     -- DISTINCT ∪ UNION ALL view
```

Same eight rows. Types match. Repeated runs are stable. The object is admissible. A
thirty-statement chain would have been checked the same way — one `SELECT *`, not a proof of
each layer.

## The effect of stacking

The stack does **not** change the bag. That is the only fact the oracle needs, and it is
checked by the read above. Everything else about the object changes, and that is the point
of stacking — here, only one of those changes matters.

**What the rewrite does to the engine, not to the rows.** A heap `t2` is a seq-scan. The
Distinct∪UNION ALL view is a Unique or HashAggregate over a doubled bag. The workload
`GROUP BY` may itself HashAgg. None of that is allowed to change `COVAR_POP` of a fixed
multiset. IEEE covariance of a constant and a set that contains Inf is NaN, in any order.

**Composition is not the trigger.** The interesting object is not “a join under an empty
filter under a union.” It is any second physical order of the same bag.

| Stack | Rows | Engine |
|---|---|---|
| heap `t2`, Inf first after NULL-skip | \(R\) | seq-scan into `float8_regr_accum`. `NaN`. Correct. |
| identity `CREATE VIEW v AS SELECT * FROM t` | \(R\) | view merge onto seq-scan. Still `NaN`. |
| `t UNION ALL t` with no `DISTINCT` | \(2R\) | bag changed. Not the object. |
| `SELECT DISTINCT * FROM (t UNION ALL t)` | \(R\) | Unique/HashAgg. Inf not first. **`0.0`.** |
| `SELECT DISTINCT y FROM t` | \(R\) on `y` | same Unique. **`0.0`.** |
| heap `t`, `INSERT` Inf second | \(R\) | seq-scan, Inf not first. **`0.0`.** No rewrite. |

The last two rows are the same illegal *engine* behaviour. The rewrite is one way to get Inf
out of first position. `INSERT` order is another. The identity did not create a bad
relation. It created a second probe order into a transition function that is not
order-invariant.

**What the original chain was doing.** The hunt did not emit the distilled view. It stacked
Postgres / shared builders until `t2` was a deep DAG: `UNLOGGED` plus `LAST_VALUE` window,
LATERAL identity, hash indexes, `ANALYZE`, even/odd `UNION ALL` splits, `security_barrier`,
BRIN, EET `CASE`, empty left join, then a doubled bag with window-`MAX` dedup. From the
heap outward:

```text
t__base
  → window / LATERAL / PK / stats     same rows; different physical form
  → partition UNION ALL               same rows; different plan
  → EET CASE, empty join, …           same rows; more operators
  → (R ∪ R) then DISTINCT/window-MAX  same rows; Unique/HashAgg  ← this reorders
```

Each step is a row-preserving rewrite. The *effect* of the whole stack is that `t2` is no
longer scanned in insert order. `COTD(c_big)` still yields `+Inf` on the `c_big = 0` rows;
those rows are just no longer first after NULL-skip. Reduction threw away everything except
Distinct∪UNION ALL. A further reduction throws away the rewrite too, and keeps only insert
order. That last step is the root cause. Say that. Do not say the oracle was unnecessary.

**What stacking does to the test.** Thirty layers is still one `SELECT *` against the
finished object. Types are checked the same way. There is no extra proof per layer. If a
layer had dropped a row or widened a type, the object gate would have failed and the
workload would not have been blamed on Postgres. The stack that shipped was admitted as
\(R\). The engine then aggregated it as if order were part of the answer.

**What stacking does not do.** It does not make the query the interesting part, and it does
not make the identity incorrect. The probe is `COVAR_POP` of a constant and an expression
that can be Inf. The stress is a second physical order of the same bag.

## The same query, two answers

On the heap, after skipping NULL, `COTD(c_big)` hits `+Inf` first for the group that
matters. `COVAR_POP` returns `NaN`.

```sql
SELECT DISTINCT COVAR_POP(LENGTH(t1.c_chr), COTD(t2.c_big)), COTD(t1.c_int)
FROM t0, t1 LEFT JOIN t2 ON (t1.c_big != t1.c_pk)
GROUP BY COTD(t1.c_int);
-- heap t2:        (NaN, 1.1106…)     -- correct
-- DISTINCT t2:    (0.0, 1.1106…)     -- wrong
```

Ground truth is IEEE: a product involving Inf with a zero deviation on the constant axis is
not a finite 0. The unary float accumulators already force NaN for Inf (`float8_accum`).
The first-input branch of `float8_regr_accum` does too. The later-Inf + constant-other path
is the hole.

The same disagreement with no join and no catalog:

```sql
CREATE TABLE t (y numeric);
INSERT INTO t VALUES (NULL), (0), (2), (-7);

SELECT COVAR_POP(0::float8, COTD(y)) FROM t;
-- NaN     -- seq-scan hits Inf first after NULL skip

SELECT COVAR_POP(0::float8, COTD(y)) FROM (SELECT DISTINCT y FROM t) s;
-- 0.0     -- Unique reorders; Inf is no longer first
```

The root-cause pair, rewrite stripped off:

```sql
CREATE TABLE t (y double precision);
INSERT INTO t VALUES (3), ('Infinity'), (4);
SELECT COVAR_POP(0::float8, y) FROM t;
-- expected: NaN
-- actual:   0.0

-- Inf first is correct:
-- INSERT INTO t VALUES ('Infinity'), (3), (4);
-- SELECT COVAR_POP(0::float8, y) FROM t;   --> NaN
```

The first two pairs are the oracle finding. The third is the engine bug with the rewrite
removed. All three are the same `Sxy = 0` hole. Do not present the third as “eqgen did not
need a rewrite.” Present it as: the rewrite exposed an order-dependent aggregate; the
aggregate is wrong even on a plain table once Inf is not first.

Same wrong `0.0` for `COVAR_SAMP` and `REGR_SXY`. `VAR_POP(y)` still correctly returns NaN.

## Why a SELECT-query oracle misses it

TLP, NoREC, and “two `SELECT`s on one table” hold the relation fixed.

- On the heap `t2`, Inf is first after NULL-skip. `COVAR_POP` returns `NaN`. There is
  nothing to disagree about. Both rewritten queries see the same scan order.
- They never emit `CREATE VIEW t2 AS SELECT DISTINCT * FROM (t UNION ALL t)`. They never
  change the order the transition function sees.
- TLP on the Distinct view is worse than silent on the heap. Every partition of the same
  `SELECT` still goes through Unique/HashAgg; every answer can be `0.0` and look internally
  consistent.

The probe is a textbook aggregate. eqgen can use it because it varied the *object*. A query
fuzzer that has to invent a hard `SELECT` to stress the engine would not think to reorder
Inf by wrapping the table in `DISTINCT`.

## What is load-bearing (controls)

Each control flips one ingredient:

| Change | Result |
|---|---|
| Inf first (`INSERT` Inf, then 3, 4) | `NaN` (first-input branch is correct) |
| All finite, constant other arg | `0.0` (intended) |
| Inf present, neither arg constant (`COVAR_POP(y, y)`) | `NaN` (`Sxy` is updated) |
| `VAR_POP(y)` with Inf | `NaN` (unary path, not `commonX`/`commonY`) |
| Heap seq-scan, Inf first after NULL-skip | `NaN` |
| Same bag, `DISTINCT` or `ORDER BY` so Inf is later | **`0.0`** |
| `COVAR_SAMP` / `REGR_SXY`, same shape | **`0.0`** (same transition function) |
| `CORR` with a constant axis | `NULL` by design under BUG #19340; not this symptom |

So: not “any Inf,” not “any `COVAR_POP`,” not “any `DISTINCT`.” A **constant axis** plus Inf
(or NaN) on a row that is **not** the first non-null input. The rewrite is load-bearing for
the *finding* only as a way to take that order. It is not load-bearing for the *bug*.

## How the hunt built it

eqgen's data-equivalence oracle, Postgres hunt, seed `1012693761`, round 135.

The workload was `COVAR_POP(LENGTH(t1.c_chr), COTD(t2.c_big))` grouped by `COTD(t1.c_int)`.
`LENGTH(c_chr)` is de-facto constant on some groups; `COTD(0)` is `+Inf`. Base `t2` is a
plain table: Inf first after NULL-skip → `NaN`. Equivalent `t2` is the Distinct∪UNION ALL /
window-dedup tail of a long chain: Inf not first → `0.0`. `t0` / `t1` / `t2` stayed row-
and description-level type-identical.

The builders that *can* emit the exposing shape:

- `DistinctUnionDuplicateQueryBuilder` — `SELECT DISTINCT * FROM (R UNION ALL R)` with a
  row key
- tag / window-`MAX` dedup — the as-found spelling of the same Unique
- any builder that HashAggs or sorts the bag (`GROUP BY`, `ORDER BY` in a subquery)

None of those builders is a bug. Each keeps the rows. The composition is legal. The
transition function is not invariant under the order the composition uses.

## What the engine does

`COVAR_POP`, `COVAR_SAMP`, and `REGR_SXY` share `float8_regr_accum` in
`src/backend/utils/adt/float.c`. BUG #19340 introduced `commonX` / `commonY` so a constant
axis can skip work. While either axis is still constant the function **does not update
`Sxy`**, leaving it at exact 0.

```c
/* after first input, while tracking commonX/commonY: */
if (isnan(commonX))
    Sxx += ...;
if (isnan(commonY))
    Syy += ...;
if (isnan(commonX) && isnan(commonY))   /* both must be non-constant */
    Sxy += ...;
```

When Y is constant (`commonY = 0`) and X later becomes Inf, `commonX` becomes NaN and `Sxx`
is updated (then forced to NaN on Inf), but **`Sxy` is never touched** and stays 0.
`float8_covar_pop` returns `Sxy/N` → `0.0`.

The first-input branch correctly does `Sxy = NaN` when the first X or Y is Inf. Hence the
order dependence: Inf first is right; Inf later, other axis still constant, is wrong.

`EXPLAIN` of the two sides is seq-scan versus HashAggregate/Unique under `DISTINCT`. Same
logical multiset, different probe order into the same transition function.

The #19340 fix is correct for finite constants. This is an Inf edge the first-input NaN
force does not cover. Suggested fix: when `newvalX` / `newvalY` is Inf/NaN (or when `Sxx` /
`Syy` is forced to NaN), also set `Sxy = NaN` even if the other axis is still constant.

## Why this is the example

It is a named PostgreSQL aggregate, IEEE `NaN` versus `0.0`, a hole left by a recent
official fix, and a ten-line comment in `float.c`.

1. Each rewrite keeps the rows, so the stack does — \(R \cup R\) then `DISTINCT` is \(R\).
2. eqgen checks that by reading `SELECT *`, not by proving the stack.
3. The same aggregate is then wrong, with no error.
4. A query-rewrite oracle cannot vary the thing that moved: the order the transition
   function sees.
5. The rewrite is the exposure, not the root cause. The distilled insert-order repro is
   the engine bug with the oracle stripped off. Say that.

What it does not show: that the interesting object had to be a join under an empty filter
under a union. The interesting object here is any second physical order of the same bag.
