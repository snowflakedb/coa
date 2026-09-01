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

# DuckDB: `INNER JOIN` with a single-sided `ON` predicate in a comma cross-product makes join-order plan reconstruction assign one operator occurrence twice (`INTERNAL Error: Operator occurrence N was reconstructed more than once`)

## Summary

The join-order optimizer's plan reconstruction (`QueryGraphManager::GenerateJoins`) violates its own
consistency invariant on a plain four-relation query. When a comma-separated `FROM` list ends in
`tX INNER JOIN tY ON (<predicate referencing only tX>)` — a join whose `ON` clause constrains one
side only, so it is really a cross product plus a filter — the DP plan reconstruction matches the
same operator descriptor to two different nodes of the plan tree and throws
`INTERNAL Error: Operator occurrence N was reconstructed more than once`
(`src/optimizer/join_order/query_graph_manager.cpp:624`). Both the join order optimizer and filter
pushdown must be enabled for it to fire, and whether it fires depends on the estimated
cardinalities, so it is a plan-choice-dependent assertion, not a syntax-level rejection. `EXPLAIN`
alone reproduces it, so nothing executes. It is a **regression**: clean on v1.5.5, failing from
v1.6.0-dev onward.

## Environment

| | |
|---|---|
| Engine | DuckDB CLI, in-memory, all defaults |
| Version (finding) | `v2.0.0-alpha37185 (Cyanoptera) e500d77864` |
| Internal checks | on (the failure *is* an `InternalException`) |
| Session settings | none required — no `SET`/`PRAGMA` needed to reproduce |
| Reproduces on | `v2.0.0-alpha37185 e500d77864`, `v2.0.0-alpha37080 e85c4d27d7`, `v2.0.0-alpha36998 ff4fd138db`, `v1.6.0-dev12322 76dd1e7d6f` |
| Does NOT reproduce on | `v1.5.5 (Variegata) d8cdaa3` |

## Minimal repro

```sql
CREATE TABLE a AS SELECT i AS x FROM range(8)  s(i);
CREATE TABLE b AS SELECT i AS x FROM range(8)  s(i);
CREATE TABLE c AS SELECT i AS x FROM range(16) s(i);
CREATE TABLE d AS SELECT i AS x FROM range(8)  s(i);

SELECT 1 FROM a, b, c INNER JOIN d ON (c.x > 0);
```

```
INTERNAL Error: Operator occurrence 2 was reconstructed more than once
```

`EXPLAIN SELECT 1 FROM a, b, c INNER JOIN d ON (c.x > 0);` fails identically.

## Expected vs actual

| Query | Expected | Actual |
|---|---|---|
| `SELECT 1 FROM a, b, c INNER JOIN d ON (c.x > 0)` | 7680 rows (8·8·15·8) | `INTERNAL Error: Operator occurrence 2 was reconstructed more than once` |
| `EXPLAIN SELECT 1 FROM a, b, c INNER JOIN d ON (c.x > 0)` | a plan | same internal error |
| the finding's own query (see `reduced.sql`, block `concrete-as-emitted`) | 0 rows (its `ON c_chr > c_chr` is never true) | same internal error |
| `SELECT 1 FROM a, b, c, d WHERE c.x > 0` (semantically identical) | 7680 rows | 7680 rows — correct |

## Equivalence construction

**(1) The construct as the builder emits it.** The finding is round 28 of an eqgen same-base fork
round: three relations `t0`, `t1`, `t2` are each built from one hidden base table `t__base` by a
chain of row-preserving builders, then the workload joins them. `t1`'s chain, verbatim in
`reduced.sql` block `concrete-as-emitted`, composes a projection round-trip, a cross-schema CTAS
round-trip, a table-macro round-trip with `decode(unhex(hex(encode(...))))`, an `ENUM` round-trip
through a `WHERE 1 = 0` view, an `ANTI JOIN` against the empty side, a tag/`UNION ALL` round-trip
(`1 AS eq_tag_1` ∪ `0 AS eq_tag_1`, then `DELETE ... WHERE eq_tag_1 <> 1`), a
`MAX(...) OVER (PARTITION BY col ORDER BY col RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)`
window round-trip per column, a second `ENUM` round-trip, and a final table macro wrapped in a
tautological `WHERE`. The workload query is

```sql
SELECT t1.c_big, t2.c_chr, t0.c_ts, t2.c_ts, t1.c_int, t0.c_date, t2.c_pk, t0.c_big, t0.c_pk
FROM t2, t0, t1 INNER JOIN (SELECT PI() AS col0 FROM t2) AS sub0 ON ((t1.c_chr)>(t1.c_chr))
ORDER BY ((t2.c_txt)||(t2.c_chr));
```

The distilled repro maps onto it one-for-one: `a`←`t2`, `b`←`t0`, `c`←`t1`,
`d`←`(SELECT PI() AS col0 FROM t2) AS sub0`, and `c.x > 0`←`(t1.c_chr)>(t1.c_chr)`. The
`ORDER BY`, the nine projected columns and the derived-table form of `sub0` all reduce away.

**(2) The load-bearing construct.** Not a construct at all — a **query shape × cardinality**
composition:

* `FROM r1, r2, r3 INNER JOIN r4 ON (<predicate over r3 only>)` — four relations, the last joined by
  an `ON` clause that constrains only the relation immediately to its left; and
* an estimated cardinality for `r3` that makes the DP enumerator pick the offending plan.

That second half is the only reason the builder chain appeared to matter. Bisecting `t1`'s 22-statement
chain, the failure switches on at exactly the tag/`UNION ALL` statement
(`CREATE TABLE ..._table_9 AS SELECT * FROM ..._table_7 UNION ALL SELECT * FROM ..._table_8`) — which
doubles the row count from 8 to 16. Every earlier truncation of the chain is a plain table of 8 rows
and plans fine. Substituting four plain `range()` tables at 8/8/16/8 rows reproduces it with no view,
macro, `ENUM`, window function or `UNION ALL` anywhere, which is what identifies cardinality rather
than any builder as the second ingredient.

**(3) Reduced away.** `t0` and `t2`'s entire chains (trivialising either keeps the failure); all 22
statements of `t1`'s chain; the `ORDER BY`; eight of the nine projected columns; the derived-table
wrapper around `sub0`; all nine base columns and their types (one `BIGINT` column suffices); and all
eight seeded rows (`range()` data works).

## Minimal oracle exposure path

- **Object composition arity:** 4.
- **GCL builder path:** `TagExplodeExpansionBuilder` [`TABLE` realization] → `TagPruneDeleteReduceBuilder` [`TABLE` realization].
- **Confidence:** Exact against the emitted tag/`UNION ALL`/`DELETE` SQL and current GCL.
- **Realization:** the expander materializes keep and throwaway tagged copies; the reducer requires a mutable table, deletes throwaway tags, and exposes the surviving base columns.
- **Workload/data requirements (excluded from arity):** `FROM r1, r2, r3 INNER JOIN r4 ON <r3-only predicate>` with four relations and a cardinality/estimate regime equivalent to 8/8/16/8 for the distilled repro.

**Exposure vs. intrinsic trigger:** The tag path exposed the assertion by creating the transient doubled cardinality and resulting plan/statistics state before pruning back to the row-equivalent object. After isolation, both builders can be replaced by plain tables at the decisive cardinalities: they are the minimal historical oracle path, while the intrinsic trigger is query shape × cardinality, not `UNION ALL` or `DELETE` semantics. Thus “reduced away” refers to the standalone engine repro, not to how the original contrast was reached.

## Characterization

**What is required** — each control in `reduced.sql` swaps exactly one ingredient and succeeds;
All 14 blocks were re-run against the live engine, confirming every claim here:

| Ingredient | Control that behaves correctly |
|---|---|
| `INNER` join type | `LEFT JOIN` with the same `ON` → fine |
| `ON` clause constrains one side only | `ON (c.x = d.x)` → fine; `CROSS JOIN d` → fine |
| the predicate targets the relation *immediately* left of the join | `ON (d.x > 0)` → fine; `ON (a.x > 0)` → fine |
| the predicate sits in `ON`, not `WHERE` | `FROM a, b, c, d WHERE c.x > 0` → fine (**same result, same rows**) |
| four relations | dropping `b` → fine |
| no extra conjunct | adding `WHERE b.x > 0` → fine |
| cardinality | `c` at 8 rows instead of 16 → fine |
| join order optimizer enabled | `SET disabled_optimizers='join_order'` → fine |
| filter pushdown enabled | `SET disabled_optimizers='filter_pushdown'` → fine |

The cardinality dependence is non-monotonic, which is the signature of DP plan choice rather than of
a threshold in any one rule. With `a=b=d=8`: `c` fails at ≥10 rows, is clean at 8–9. Varying `d` with
`a=b=8, c=16`: clean at 1, **fails at 2–8**, clean again at ≥16. Varying `a` or `b`: clean at 1–4 (`a`)
and 1–2 (`b`), failing from 8 up.

**Mechanism.** Both `join_order` and `filter_pushdown` must be on, so the two stages interact: filter
pushdown rewrites the single-sided `ON` predicate (in the working control plan it lands as
`Filters: x > 0` inside `c`'s sequential scan, under a chain of cross products), and the join-order
DP then reconstructs a tree in which the operator descriptor left behind is assignable to two
different node pairs. The guard that catches it is the `reconstructed_operators.insert(...)` check:

```cpp
// src/optimizer/join_order/query_graph_manager.cpp:620-626
if (!direct && !inverted) {
    throw InternalException("Could not orient operator occurrence %llu in reconstructed join tree",
                            descriptor->index);
}
if (!reconstructed_operators.insert(descriptor->index).second) {
    throw InternalException("Operator occurrence %llu was reconstructed more than once", descriptor->index);
}
```

Because the `ON` predicate constrains one side only, the descriptor's `left_total_set` /
`right_total_set` do not separate the two children, so the `IsSubset` tests that pick `direct` /
`inverted` can succeed for more than one `(left, right)` split — and the same occurrence is consumed
twice. That is consistent with the controls: an equi-join (which separates the sets) is fine, a
`CROSS_PRODUCT` descriptor (which takes the `Intersects` branch instead) is fine, and moving the
predicate to `WHERE` (so no operator descriptor carries it) is fine.

**No stack trace section beyond the below**: this is a thrown `InternalException`, not a crash — the
process stays alive and the CLI returns the error to the client. The CLI's own backtrace, as recorded
in the finding, resolves only to addresses (release build, no symbols); the source location above is
the actionable form and comes from the matching checkout.

**Plan diff.** `EXPLAIN` on the repro fails, so there is no buggy plan to show; the control (`c` at 8
rows) plans as three stacked cross products with the pushed-down filter in `c`'s scan:

```
Projection → Cross Product → Cross Product → Cross Product
                                              ├── Seq Scan d  (~8 rows)
                                              └── Seq Scan c  Filters: x > 0  (~1 row)
```

The `~1 row` estimate for `c` after pushdown next to `d`'s `~8` is the estimate the DP works from;
that the failure switches on when `c` grows to 16 raw rows points at this estimate feeding the
enumeration.

**Regression window.** Clean on v1.5.5, failing on v1.6.0-dev12322 and all v2.0.0-alphas tested. The
assertion text itself was added in `7c7886495a` ("unified semantics, remove NonInnerJoinEdge",
2026-07-23), part of the join-order rework merged as
[#23658 "Correlated non-`INNER` joins"](https://github.com/duckdb/duckdb/pull/23658) on 2026-07-28 —
after the v1.5.5 branch point, so the failure cannot predate that work.

## How it was found

The eqgen data-equivalence oracle. It builds, from one base table, several relations that are
provably row- and type-identical to it (chains of row-preserving builders: views, CTAS, `UNION ALL`
tag round-trips, window round-trips, `ENUM` round-trips, table macros, attached-database mirrors),
then runs byte-identical workload SQL against a database holding the plain base copies and a database
holding the rewritten equivalents. The two must agree, so any difference — here, an error on one side
only — is a divergence with no reference engine and no expected output needed.

What the oracle contributed is not the query, which is trivially simple, but the *cardinality* and
*statistics* the query was planned against: the builder chain's tag/`UNION ALL` round-trip is what put
16 rows behind `t1` while the base side had 8, and that is the whole difference between planning and
failing. A query-rewrite oracle (TLP / NoREC / EET) holds the data fixed and perturbs the query, so it
would not have produced the two different cardinality/statistics environments for one fixed query
text; here the query is the constant and the relation is the variable.

The same bug was hit three times in one 34-round session (rounds 7, 18, 28 — occurrences 3, 3 and 2),
each time via a different builder chain but the same
`comma-list … INNER JOIN (subquery) ON (<single-sided predicate>)` workload shape. Confirmed one bug:
`SET disabled_optimizers='join_order'` and `SET disabled_optimizers='filter_pushdown'` each silence
all three findings.

* Seeds: 1423554624 (round 7), 1565482207 (round 18), 1805397586 (round 28)
* Reduced repro: [`reduced.sql`](reduced.sql)
* Original findings: `duckdb_20260808-225941/error_round7_0.sql`,
  `error_round18_0.sql`, `error_round28_0.sql`
