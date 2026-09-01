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

# PostgreSQL: `COVAR_POP`/`REGR_SXY` return 0.0 instead of NaN when one arg is constant and the other has Inf (not first)

## Summary

`float8_regr_accum` (transition function for `COVAR_POP`, `COVAR_SAMP`, `REGR_SXY`, and friends) tracks whether each argument is constant via `commonX`/`commonY` (introduced for [BUG #19340](https://www.postgresql.org/message-id/19340-6fb9f6637f562092@postgresql.org)). While either axis is still constant it **skips updating `Sxy`**, leaving it at exact 0 so a finite constant axis yields covariance 0.

That optimization is wrong once the *other* axis contains `Infinity` (or produces it, e.g. `COTD(0)`): the cross-product is not well-defined and must be NaN. Inf on the **first** non-null input is handled (`Sxy = NaN` in the first-input branch), but Inf arriving **later** while the other axis is still constant leaves `Sxy = 0`, so the final function returns **0.0** instead of **NaN**. Any plan that reorders rows (DISTINCT, `ORDER BY`, HashAgg) therefore flips the answer.

## Environment

| | |
|---|---|
| Engine | PostgreSQL **20devel** (`--enable-cassert --enable-debug`, `CFLAGS=-O1`) |
| Binary | `pg-main/bin` (built ~2026-08-05) |
| Source | `pg-main-src` HEAD `36f7330b8b2238c2093d7eac521f996b33e66121` |
| Access | private socket cluster via eqgen `PostgresAdapter` |
| Session | `locale=C`, `standard_conforming_strings=on`, `statement_timeout=60s` |
| Found by | **eqgen** data-equivalence oracle |

## Minimal repro

```sql
CREATE TABLE t (y double precision);
INSERT INTO t VALUES (3), ('Infinity'), (4);

SELECT COVAR_POP(0::float8, y) FROM t;
-- Expected: NaN
-- Actual:   0.0

-- Inf first is correct (control):
-- INSERT INTO t VALUES ('Infinity'), (3), (4);
-- SELECT COVAR_POP(0::float8, y) FROM t;  --> NaN
```

Same wrong 0.0 for `COVAR_SAMP` and `REGR_SXY`. `VAR_POP(y)` still correctly returns NaN.

## Expected vs actual

| Query | Expected | Actual | Which side |
|---|---|---|---|
| `COVAR_POP(0, y)` with Inf **not** first | NaN | **0.0** | engine wrong |
| `COVAR_POP(0, y)` with Inf first | NaN | NaN | OK |
| `COVAR_POP(0, y)` all finite | 0.0 | 0.0 | OK |
| `VAR_POP(y)` with Inf | NaN | NaN | OK (unary path) |
| Original finding: base plain `t2` | NaN for group ≈1.1106 | NaN | **base correct** |
| Original finding: equiv `DISTINCT∪UNION ALL` view as `t2` | NaN | **0.0** | equiv triggers reorder |

Ground truth: IEEE — products involving Inf with a zero deviation on the constant axis are not a finite 0; the unary float accumulators already force NaN for Inf (`float8_accum`), and the first-input branch of `float8_regr_accum` does too. The later-Inf + constant-other path is the hole.

## Equivalence construction

1. **Concrete (as builders emit it):** the equivalent exposed `t2` via a UNION-ALL duplicate + `DISTINCT` + window-`MAX` dedup (tag/window round-trip). Collapsing to

   ```sql
   CREATE VIEW t2 AS SELECT DISTINCT * FROM (
     SELECT * FROM t__base UNION ALL SELECT * FROM t__base
   ) s;
   ```

   is enough; even `SELECT DISTINCT y FROM t` alone triggers it.

2. **Load-bearing composition:** `COVAR_POP(constant_or_constant_expr, expr_that_can_be_Inf)` × **row order where Inf is not first**. The equivalence builders only matter insofar as DISTINCT/HashAgg changes order relative to the plain base table (where, after skipping NULL, `COTD(0)` was first).

3. Reduced away: LATERAL self-join, LAST_VALUE window, CASE identity, PK/hash indexes, matviews, the outer `t0` cross product, `GROUP BY COTD(t1.c_int)` / `LENGTH(c_chr)` — none required once Inf order is controlled directly.

## Minimal oracle exposure path

**Object composition arity:** `3`

**GCL builder path:** `DistinctUnionDuplicateQueryBuilder[TABLE]` → `CreateViewBuilder`

**Confidence:** verified

**Realization:** Internal deduplication `TABLE`s are exposed through a final `VIEW`.

**Workload/data requirements (excluded from arity):**
- One covariance/regression argument constant within the group.
- The other argument yields Inf or NaN after the first non-NULL input.
- A plan or object path that changes aggregate input order.
- `COVAR_POP`, `COVAR_SAMP`, or `REGR_SXY`.

**Exposure vs. intrinsic trigger:** The duplicate/deduplicate builder and view are not required by the standalone trigger once row order is set directly; they supplied the row-identical contrasting path by moving Inf away from the first aggregate input. The builder's internal `UNION ALL` and `DISTINCT` table materializations were therefore exposure mechanisms, not semantic ingredients of `float8_regr_accum`'s intrinsic order-sensitive defect.

## Characterization

**Triggers**
- One of `COVAR_POP` / `COVAR_SAMP` / `REGR_SXY` (all share `float8_regr_accum`).
- One argument constant (literal or de-facto, e.g. `LENGTH('') = 0` for every row in the group).
- The other argument yields `+Inf`/`-Inf`/`NaN` on a row that is **not** the first non-null input.
- Reordering via `DISTINCT`, `GROUP BY`, `ORDER BY` in a subquery, or HashAgg.

**Does not trigger (controls)**
- Inf as first non-null input → NaN.
- Neither argument constant → NaN.
- All-finite constant axis → 0.0 (intended).
- `VAR_POP` / `STDDEV_POP` on the Inf column → NaN.

**Mechanism (`src/backend/utils/adt/float.c`)**

```c
/* after first input, while tracking commonX/commonY: */
if (isnan(commonX))
    Sxx += ...;
if (isnan(commonY))
    Syy += ...;
if (isnan(commonX) && isnan(commonY))   /* <-- both must be non-constant */
    Sxy += ...;
```

When Y is constant (`commonY = 0`) and X later becomes Inf, `commonX` becomes NaN and `Sxx` is updated (then forced to NaN on Inf), but **`Sxy` is never touched** and stays 0. `float8_covar_pop` returns `Sxy/N` → 0.0.

First-input branch correctly does `Sxy = NaN` when the first X or Y is Inf — hence the order dependence.

**EXPLAIN:** base seq-scan vs HashAggregate/Unique under DISTINCT; same logical multiset, different probe order into `float8_regr_accum`.

**DML:** not applicable (read-only aggregate).

**Related:** [BUG #19340](https://www.postgresql.org/message-id/19340-6fb9f6637f562092@postgresql.org) / commit “Handle constant inputs to corr() and related aggregates more precisely” introduced `commonX`/`commonY`. That fix is correct for finite constants; this is an Inf edge case the first-input NaN force does not cover.

## How it was found

eqgen’s data-equivalence oracle: same workload on a plain base relation vs a row-identical rewrite. Here the rewrite’s DISTINCT∪UNION ALL view reordered rows so Inf was no longer first, flipping `COVAR_POP(LENGTH(c_chr), COTD(c_big))` from NaN to 0.0 for one group while `t0`/`t1`/`t2` stayed row- and (description-level) type-identical. A query-rewrite oracle that held the table fixed would not have moved Inf’s position and would have missed it.

- Seed: `1012693761`
- Source: `postgres_hunt_20260809-175406/postgres_20260809-175452/mismatch_round135_0.sql`
- Artifacts: `repro/postgres-20260811-covar-inf-constant-axis/{original_finding.sql,reduced.sql,bug_report.md}`

## Open items

- Suggested fix: when `newvalX`/`newvalY` is Inf/NaN (or when `Sxx`/`Syy` is forced to NaN in the overflow path), also set `Sxy = NaN` even if the other axis is still constant.
- Regression window: present on this 20devel build that already contains the BUG #19340 `commonX`/`commonY` logic; not bisected to the exact commit on this box (shallow `pg-main-src` history).
- `CORR` with a constant axis returns NULL by design under #19340; not the same symptom.
