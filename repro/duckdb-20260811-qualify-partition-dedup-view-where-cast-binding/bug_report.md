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

# DuckDB: `INTERNAL Error: Failed to bind column reference` when an outer `QUALIFY` reads a view whose own body already filters via `QUALIFY ROW_NUMBER() OVER (PARTITION BY ...) = 1`, and the outer `WHERE` needs any implicit `CAST`

## Summary

Querying a view `v` — where `v`'s own definition already filters with
`QUALIFY (ROW_NUMBER() OVER (PARTITION BY key ORDER BY key)) = 1` (a per-key dedup) — with an outer
query of the shape `SELECT ... FROM v WHERE <cast-requiring expr> QUALIFY ROW_NUMBER() OVER (ORDER
BY ...) <= n` throws `INTERNAL Error: Failed to bind column reference "" [N.M] (bindings: {...})`.
The trigger for the outer `WHERE` is broad: it fires for a bare non-boolean column reference
(needing an implicit numeric→boolean cast), an explicit cast, a scalar function call, `OR`, and
some (not all) cross-type comparisons — see Characterization for the exact accounting. `SET
disabled_optimizers='top_n_window_elimination'` makes every case clean, localising the defect to
the same optimizer pass as `repro/duckdb-20260811-qualify-topn-elimination-argminmax-n-cap`, via a
completely different mechanism and symptom (an internal binder assertion here, versus a guarded
`InvalidInputException` there).

## Environment

- **DuckDB v2.0.0-alpha37464 (Cyanoptera)** `ea53ecdca1` — the `main`/CLI build fuzzed by eqgen,
  downloaded from `artifacts.duckdb.org/latest` at time of triage.
- Access path: CLI (`:memory:`). No `sql_mode`/charset/collation applicable.

## Minimal repro

```sql
CREATE TABLE t (c_pk BIGINT NOT NULL, c_int BIGINT);
INSERT INTO t VALUES (1, 5), (2, -7), (3, 0);
CREATE TABLE t_keyed AS SELECT c_pk, c_int, ROW_NUMBER() OVER (ORDER BY c_pk) AS eq_key_1 FROM t;
CREATE VIEW v AS
  SELECT c_pk, c_int FROM t_keyed QUALIFY (ROW_NUMBER() OVER (PARTITION BY eq_key_1 ORDER BY eq_key_1)) = 1;

SELECT * FROM v WHERE c_int::BOOLEAN QUALIFY ROW_NUMBER() OVER (ORDER BY c_pk) <= 1;
```

The cast is written explicitly here so the trigger is visible in the SQL text; `WHERE c_int` (no
`::BOOLEAN`) reproduces identically, since `WHERE` on a non-`BOOLEAN` expression casts to
`BOOLEAN` implicitly regardless of whether the source text spells it out (`reduced.sql` Control A0).

Full version with controls in `reduced.sql`.

## Expected vs actual

| Query | Expected | Actual |
|---|---|---|
| Minimal repro above | 1 row (`c_pk=1, c_int=5`) | `INTERNAL Error: Failed to bind column reference ""#[0.1]"" [0.1] (bindings: {#[18.0]})` |
| Same table, no view / no inner `QUALIFY` (Control A shape, harness's "base" side) | 1 row | 1 row (correct) |
| Drop outer `QUALIFY`, keep everything else (Control B) | 2 rows | 2 rows (correct) |
| Drop the inner view's `QUALIFY` entirely (Control C) | 1 row | 1 row (correct) |
| Inner `QUALIFY` unpartitioned — `ROW_NUMBER() OVER (ORDER BY c_pk) >= 1` (Control D) | 1 row | 1 row (correct) |
| `WHERE c_pk > 0` instead of `WHERE c_int` (Control E, no cast needed) | 1 row | 1 row (correct) |
| Original query, `SET disabled_optimizers='top_n_window_elimination'` (Control F) | 1 row | 1 row (correct) |

**The engine is wrong**, established the same two ways as the sibling finding:

1. **By construction:** every one of the six controls above is a one-token change from the failing
   query, and every one of them is clean. Only the combination — view whose body has a
   `PARTITION BY` `QUALIFY`, read by an outer query with both its own `QUALIFY` and a
   cast-requiring `WHERE` — fails.
2. **By elimination:** disabling exactly `top_n_window_elimination`, and nothing else, makes the
   *identical* query succeed (Control F). No other optimizer flag tested (`filter_pushdown`,
   `expression_rewriter`, `statistics_propagation`, `column_lifetime`, `common_subplan_optimizer`,
   `unnest_rewriter`, `join_order`, `extension`) silences it.

## Equivalence construction

Found the same way as the sibling finding — but this one surfaced from a **live campaign** (not a
pre-flight sweep), in the very shape eqgen's own harness produces: a materialized key column
(`ROW_NUMBER() OVER (ORDER BY c0) AS eq_key_N`, from `_materialize_row_key`, shared by several
builders) feeding a **`KeyQualifyDedupReduceBuilder`** view — `QUALIFY (ROW_NUMBER() OVER
(PARTITION BY eq_key_N ORDER BY eq_key_N)) = 1`, algebra rule **(Qualify)**, a collapse-back-to-one-
row-per-key reducer — read by a workload query carrying the *new* `DuckDBRowNumberBoundQualifyBuilder`
/ sqlancerpp-generator-style outer `QUALIFY ROW_NUMBER() OVER (ORDER BY <pk>) <= n` plus an ordinary
`WHERE` clause. 131 findings landed in the triggering campaign run; sampling ~60 of them and
grouping by the outer `WHERE` expression's shape:

| WHERE shape | Count (of ~60 sampled) | Reproduces on the 2-column distilled repro? |
|---|---|---|
| `CASE <col> WHEN <col> THEN ...` (cross-type branches) | 28 | Yes |
| `IFNULL`/`COALESCE` (cross-type args) | 2 | Yes |
| Direct comparison between differently-typed columns (`<>`, `<`, `IS NOT DISTINCT FROM`, …) | ~7 | Yes, for `<`/`<>`/`OR`; **not** for `=`/`>=`/`BETWEEN` (see below) |
| Scalar math function on a single column (`TAN`, `LOG10`, `COS`, `ASIN`, `BIT_COUNT`, …) | remainder of the ~94 "other" bucket | Yes (`TAN(c_int)` alone reproduces on a single-type, 2-column table) |

The unifying thread, confirmed on the 2-column distilled repro (Control G, `reduced.sql`): **any
expression in the outer `WHERE` that forces DuckDB to insert an implicit `CAST` node** —
whether promoting an operand for a comparison, converting a math function's result, or coercing a
non-`BOOLEAN` column to `BOOLEAN` for `WHERE` itself — triggers it. `WHERE c_pk > 0` and
`WHERE c_pk = c_int` (same-type, no cast) are clean; `WHERE c_int` bare, `WHERE c_int::BOOLEAN`,
`WHERE TAN(c_int)`, `WHERE (c_int OR c_int)` all reproduce, on a table with **no cross-type columns
at all**. Exactly which comparison operators fall on which side of the line (`=`/`>=`/`BETWEEN`
clean; `<`/`<>` failing, for the same operand types) was not fully pinned down — see Open Items.

**Constructs reduced away:** the original findings' equivalence chains were 20-30 statements deep
(partition-union, `UNION ALL`, `ENUM` round-trips, recursive CTE, materialized CTE, `COALESCE`
self-reference, `ATTACH`, index, checkpoint). None of that depth is load-bearing — the two-statement
distilled repro (a materialized key column + one `QUALIFY`-filtered view) reproduces identically.

## Minimal oracle exposure path

- **Object composition arity:** 3.
- **GCL builder path:** `CreateTableBuilder` [row key] → `KeyQualifyDedupReduceBuilder` [`VIEW` realization].
- **Confidence:** Exact against the report SQL and current GCL.
- **Realization:** CTAS materializes the row key; the reducer emits the partitioned-`QUALIFY` view (with its projection-only exposing view) as one transform-plus-view realization.
- **Workload/data requirements (excluded from arity):** the reader needs an outer bounded `ROW_NUMBER` `QUALIFY` and a `WHERE` expression that introduces a cast.

**Exposure vs. intrinsic trigger:** The object path contributes the inlined, partitioned-`QUALIFY` relation that leaves a nested window/filter binding to rewrite. The intrinsic failure additionally needs the outer top-N-window rewrite and cast-bearing filter; those are workload features, not object-composition factors.

## Characterization

**Trigger:** `SELECT ... FROM v WHERE <cast-requiring expr> QUALIFY ROW_NUMBER() OVER (ORDER BY
...) <= n`, where `v` is a `VIEW` (not a materialized `TABLE` — Control C/the "materialize instead"
variant is clean, matching this project's own note that a view gets inlined into the query reading
it while a table does not) whose body ends in `QUALIFY (ROW_NUMBER() OVER (PARTITION BY key ORDER
BY key)) = 1`.

**Does NOT trigger it (controls, `reduced.sql`):**
- Same rows on a plain table with no view/inner `QUALIFY` at all (Control A).
- The inner view without its `PARTITION BY` `QUALIFY` (Control C) — a plain view.
- The inner view's `QUALIFY` present but **unpartitioned** (Control D, `ROW_NUMBER() OVER (ORDER
  BY c_pk) >= 1`) — `PARTITION BY` specifically is load-bearing, not "any window filter in the
  view".
- The outer query without its own `QUALIFY` (Control B) — just `WHERE c_int` alone.
- A `WHERE` clause that needs no cast at all (Control E, `WHERE c_pk > 0`; also `WHERE c_pk =
  c_int` cross-type).
- `SET disabled_optimizers='top_n_window_elimination'` on the exact failing query (Control F).

**Mechanism, so far as pinned down:** the assertion is thrown from
`src/execution/column_binding_resolver.cpp:246`:

```cpp
// could not bind the column reference, this should never happen and indicates a bug in the code
throw InternalException("Failed to bind column reference \"%s\" [%d.%d] (bindings: %s)", ...);
```

— the bare "could not find this binding at all" branch (distinct from the two sibling throws in
the same function for "inequal num bindings/types" and "inequal types", which name the mismatched
types explicitly; this one has neither, meaning the resolver's current binding set doesn't contain
the referenced index *at all*). `SET disabled_optimizers='top_n_window_elimination'` implicates
`src/optimizer/topn_window_elimination.cpp`: when it eliminates the outer `QUALIFY`'s window
operator in favor of a virtual-rowid-based Top-N plan, it must rewrite column bindings for
everything reading from underneath it — and when that underlying relation is *itself* a
`QUALIFY`-filtered view (already lowered to its own window/filter operator) that the outer `WHERE`
also needs to route a `CAST` through, the rewrite leaves a binding that points nowhere the resolver
can find. The precise rewrite call site inside that ~1270-line file was not isolated further within
triage-time budget — see Open Items.

**Raw stack trace** (release build, no debug symbols — bare addresses only; the message-text
`grep` above is what actually located the throw site, not this trace):

```
INTERNAL Error: Failed to bind column reference ""#[0.1]"" [0.1] (bindings: {#[24.0], #[24.1]})
Stack Trace:
duckdb() [0x7693c0]
duckdb() [0x769428]
duckdb() [0x76dc5c]
duckdb() [0x856c90]
duckdb() [0x8570c0]
duckdb() [0xf4a9d8]
duckdb() [0xf4c57c]
duckdb() [0xf4d1f8]
duckdb() [0xf4c43c]
duckdb() [0xf4d1f8]
duckdb() [0xf50920]
duckdb() [0xf519d8]
duckdb() [0x83b1e8]
duckdb() [0xf7753c]
duckdb() [0x83b1dc]
duckdb() [0xf7753c]
duckdb() [0x83b1dc]
duckdb() [0xf7753c]
duckdb() [0x83b1dc]
duckdb() [0x85fa00]
duckdb() [0x85fdb0]
duckdb() [0x9249d4]
duckdb() [0x925170]
duckdb() [0x9662ac]
duckdb() [0x966708]
duckdb() [0x96edf8]
duckdb() [0x9744b4]
duckdb() [0x97460c]
duckdb() [0x9746b8]
duckdb() [0x9752ac]
duckdb() [0x475a00]
duckdb() [0x475e70]
duckdb() [0x4761f4]
duckdb() [0x481160]
duckdb() [0x48207c]
duckdb() [0x44d7ec]
/lib64/libc.so.6(+0x27540) [0xffffb850d540]
/lib64/libc.so.6(__libc_start_main+0x98) [0xffffb850d618]
duckdb() [0x4528b0]
```

The recurring `0xf7753c`/`0x83b1dc` pair three times in a row is consistent with a recursive
tree-walk (e.g. `ColumnBindingResolver::VisitOperator` descending through nested logical
operators) before the leaf `VisitReplace` throw; the last six `duckdb()` frames plus the `libc`
pair are the CLI's own driver/`main`, not engine code.

**DML impact:** not applicable — the trigger is a read-only `SELECT`/view-definition shape.

**Relationship to open issue #24609** ("`TopNWindowElimination` late-materialization drops
`TRY_CAST` NULL-semantics and collapses nested casts"): both bugs sit in the same file and both
involve casts interacting with this optimizer's rewrite, but they are distinct. #24609's casts live
in the **payload/projection** (the `SELECT` list below the window) and its symptoms are a
`Conversion Error` or a *silently wrong value* — never an `INTERNAL Error`, and no inner `QUALIFY`
is needed. This finding's cast lives in the **outer `WHERE`** (a filter, not a projection), requires
the FROM-source to be a view with its *own* `PARTITION BY`-`QUALIFY`, and throws an internal binder
assertion rather than returning any answer at all. Confirmed not a duplicate.

## How it was found

Surfaced organically by a live eqgen campaign (`--generator sqlancerpp --predicates sqlancerpp`)
combining two things added in this same session: `KeyQualifyDedupReduceBuilder`'s existing
per-key-dedup view construct, and a new sqlancerpp-fork generator feature
(`QUALIFY ROW_NUMBER() OVER (ORDER BY <pk>) <= n`, ordered by the schema's unique key so the query
stays deterministic per `ORACLE_ALGEBRA.md` Definition 6.1) that started emitting outer `QUALIFY`
workload queries at real volume. Both constructs are individually correct (each is sound per the
algebra and passes its own sweep); the campaign found the composition. 131 findings in the run
shared this symptom (`INTERNAL Error: Failed to bind column reference`); sampling ~60 confirmed they
all carry the `KeyQualifyDedupReduceBuilder`-shaped inner view plus an outer `QUALIFY`, differing
only in which cast-requiring `WHERE` expression the query generator happened to draw — one root
cause, confirmed by the distilled two-statement repro reproducing all of the sampled `WHERE` shapes
except the same-type-comparison and `BETWEEN` ones (which never appeared as *sole* triggers in the
sample; every sampled finding's `WHERE` contained at least one cast-requiring sub-expression).

A query-pair rewrite oracle (TLP/NoREC/EET) could not have found this: the trigger needs a
*specific object shape* (a view whose body already contains a windowed filter) as the thing being
read, not an alternate but equivalent phrasing of one query — exactly the class of defect this
project's object-equivalence oracle is built to reach that a same-query rewrite oracle structurally
cannot.

- `reduced.sql` in this directory is the full, live-engine-verified repro plus every control listed
  above, run end-to-end against the tip-of-main CLI as part of this triage.
- Original findings: `error_round53_0.sql`, `error_round104_0.sql`, `error_round623_0.sql` and 128
  others under the campaign's run directory (not itself checked into this repo).

## Open items

- The exact call site inside `topn_window_elimination.cpp` that rewrites the outer `WHERE`'s column
  bindings when the FROM-source view already carries its own window/`QUALIFY` was not pinned to a
  `file:line` within triage-time budget.
- The precise boundary among comparison operators — `=`/`>=`/`BETWEEN` are clean on
  differently-typed operands, `<`/`<>` are not, for the same pair of types — was characterized
  empirically (Control G) but not traced to a specific expression-simplification rule
  (`comparison_simplification.cpp` / `constant_order_normalization.cpp` are plausible candidates
  given they canonicalize/flip comparison operators, which could explain an asymmetric operand-index
  effect, but this was not confirmed by reading their source).
- Not bisected to a specific commit or checked against a released version (v1.5.x) — unlike the
  sibling finding, no regression window was established here.
