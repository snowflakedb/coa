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

# DuckDB: `TopNWindowElimination::CreateAggregateOperator` throws `INTERNAL Error: Attempted to access index N within vector of size N` for a flag-joined view read by a whole-relation top-N `QUALIFY` — same code path as a closed issue, incompletely fixed

## Summary

A view built as "a keyed relation `INNER JOIN`ed with a single-column flag table `WHERE flag =
1`" (row-preserving by construction: every row's key appears in the flag table exactly once), read
by an outer query with **no `PARTITION BY`** — just a whole-relation
`QUALIFY ROW_NUMBER() OVER (ORDER BY ...) <= n` — throws
`INTERNAL Error: Attempted to access index N within vector of size N` from
`TopNWindowElimination::CreateAggregateOperator`, provided the view's payload has at least one
`INTEGER`, one `DOUBLE`, and one `VARCHAR` column together. The exact same stack (
`CreateAggregateOperator` → `OptimizeInternal` → `LogicalFilter::ResolveTypes` →
`LogicalOperator::MapTypes` → out-of-bounds vector index) is reported, and was closed as fixed, in
[duckdb/duckdb#21820](https://github.com/duckdb/duckdb/issues/21820) — but that issue's own repro
(a `PARTITION BY` + `WHERE ... IN (...) AND ... = FALSE` filter, no join at all) is confirmed clean
on the build tested here. The underlying defect in `CreateAggregateOperator`/`MapTypes` was
evidently not fully fixed — this finding reaches the identical assertion through a different SQL
shape that issue's fix did not cover.

## Environment

- **DuckDB v2.0.0-alpha37464 (Cyanoptera)** `ea53ecdca1` — the `main`/CLI build fuzzed by eqgen,
  downloaded from `artifacts.duckdb.org/latest` at time of triage.
- Access path: CLI (`:memory:`). No `sql_mode`/charset/collation applicable.

## Minimal repro

```sql
CREATE TABLE t__base (c_pk INTEGER NOT NULL, c_int INTEGER, c_dbl DOUBLE, c_txt VARCHAR);
INSERT INTO t__base VALUES (1, NULL, NULL, NULL);
INSERT INTO t__base VALUES (2, 1, 0.0, NULL);
INSERT INTO t__base VALUES (3, 2, 1.5, 'a');

CREATE TABLE t_uid AS SELECT c_pk, c_int, c_dbl, c_txt, ROW_NUMBER() OVER (ORDER BY c_pk) AS eq_uid_1 FROM t__base;
CREATE TABLE t_flag AS SELECT eq_uid_1, 1 AS eq_flag_1 FROM t_uid;
CREATE VIEW t AS
  SELECT l.c_pk AS c_pk, l.c_int AS c_int, l.c_dbl AS c_dbl, l.c_txt AS c_txt
  FROM t_uid l INNER JOIN t_flag r ON l.eq_uid_1 = r.eq_uid_1
  WHERE r.eq_flag_1 = 1;

SELECT c_dbl, c_int FROM t WHERE (c_txt IS NULL) QUALIFY ROW_NUMBER() OVER (ORDER BY c_pk) <= 10;
```

Full version with controls in `reduced.sql`.

## Expected vs actual

| Query | Expected | Actual |
|---|---|---|
| Minimal repro above | 2 rows: `(NULL, NULL)`, `(0.0, 1)` | `INTERNAL Error: Attempted to access index 5 within vector of size 5` |
| Same rows, plain table, no join/view (Control A, harness's "base" side) | 2 rows | 2 rows (correct) |
| `SEMI JOIN` instead of `INNER JOIN ... WHERE flag = 1` (Control B) | 2 rows | 2 rows (correct) |
| Drop the `DOUBLE` column, keep two `INTEGER`s instead (Control C) | 2 rows | 2 rows (correct) |
| Drop the `VARCHAR` column (Control D) | 1 row | 1 row (correct) |
| Original query, `SET disabled_optimizers='top_n_window_elimination'` (Control E) | 2 rows | 2 rows (correct) |
| duckdb/duckdb#21820's own original repro, verbatim (Control F) | succeeds | succeeds (correct — that issue is fixed) |

**The engine is wrong**, established the same way as the sibling findings:

1. **By construction:** the view is a textbook row-preserving rewrite — every row of `t__base`
   gets a unique key via `ROW_NUMBER()`, the flag table holds each key exactly once, and an
   `INNER JOIN` against a table where every probe-side key matches exactly one build-side row is a
   lossless 1:1 join. `t` holds exactly the rows of `t__base`, confirmed directly (row-identical,
   3 rows each) independent of anything the CLI text-mode replay might get confused by.
2. **By elimination:** disabling exactly `top_n_window_elimination` makes the identical query
   succeed (Control E), and the stack trace names that pass's `CreateAggregateOperator` directly.
3. **By regression-window:** the *only* other query in this report to reach this exact assertion,
   #21820, is independently confirmed fixed on this same build (Control F) — so this is not simply
   "the same known bug, still open."

## Equivalence construction

Surfaced from the same live campaign as the sibling `QUALIFY`-composition finding
(`repro/duckdb-20260811-qualify-partition-dedup-view-where-cast-binding`), in the shape eqgen's
**`FlagTableJoinQueryBuilder`** actually emits: a keyed materialization
(`_materialize_row_key`: `ROW_NUMBER() OVER (ORDER BY c0) AS eq_uid_N`), a projection of just that
key with a constant flag column (`eq_flag_N`), and an `INNER JOIN` between them filtered to
`flag = 1` — algebra rule **(Mat)**/flag-join, provably row-preserving (every keyed row is
retained exactly once, since the flag side holds each key exactly once). Read by a workload query
carrying the *new* `DuckDBRowNumberBoundQualifyBuilder` / sqlancerpp-generator outer
`QUALIFY ROW_NUMBER() OVER (ORDER BY <pk>) <= n` (no `PARTITION BY` — a whole-relation top-N) plus
an ordinary `WHERE ... IS NULL`. The original finding's chain was **39 statements** deep
(partition-union, two `SEMI JOIN`s, two `ATTACH` mirrors, a text-codec round-trip, `struct_pack`/
unpack, `[col][1]` array indexing, `EXCEPT ALL`/`INTERSECT ALL` via `MACRO`s, a full-frame window
aggregate over every column, `CHECKPOINT`, `ADD`/`DROP COLUMN`) — **none of that depth is
load-bearing**. Automated prefix-collapse delta-debugging failed to shrink the chain (it is a
graph of several parallel branches feeding `UNION ALL`/`EXCEPT ALL` combination points, not a
line, so "redefine `t` as an earlier intermediate" does not apply); hypothesis-driven reduction
based on the two join builders visible near the end of the chain (`FlagTableJoinQueryBuilder`,
`SemiJoinFlagRoundTripBuilder`) found the load-bearing construct on the first attempt — the
`INNER JOIN`/flag-filter shape specifically, not the semantically equivalent `SEMI JOIN` one
(Control B).

**The load-bearing composition** is three-way: the flag-join view (not a `SEMI JOIN` — Control B),
the outer whole-relation `QUALIFY` (no `PARTITION BY` needed, unlike #21820), and a specific
payload-type mix (`INTEGER` + `DOUBLE` + `VARCHAR` together — Controls C/D). None of the deeper
chain's other constructs (codecs, struct packing, indexes, attach, checkpoint, macros) contribute;
they were all reduced away.

## Minimal oracle exposure path

- **Object composition arity:** 4.
- **GCL builder path:** `CreateTableBuilder` [row key] → `FlagTableJoinQueryBuilder` [flag `TABLE`] → `CreateViewBuilder`.
- **Confidence:** Exact against the report SQL and current GCL.
- **Realization:** one CTAS materializes the keyed rows, `FlagTableJoinQueryBuilder` creates its own flag CTAS, and the filtered identity join is exposed as a `VIEW`.
- **Workload/data requirements (excluded from arity):** an outer whole-relation `QUALIFY ROW_NUMBER() ... <= n`, a `WHERE` filter, and a payload containing `INTEGER`, `DOUBLE`, and `VARCHAR`.

**Exposure vs. intrinsic trigger:** The object path is the minimal relation shape that exposes the bug: its inlined filtered flag join leaves `TopNWindowElimination` with inconsistent bindings/type-vector positions. The outer `QUALIFY`, filter, and payload mix are intrinsic workload/data conditions and therefore do not increase object arity.

## Characterization

**Trigger:** `SELECT ... FROM v WHERE ... QUALIFY ROW_NUMBER() OVER (ORDER BY ...) <= n` (no
`PARTITION BY`), where `v` is a view of the shape
`SELECT ... FROM keyed l INNER JOIN flag r ON l.key = r.key WHERE r.flag_col = 1`, and the
projected payload includes at least one `INTEGER`-family, one `DOUBLE`, and one `VARCHAR` column.

**Does NOT trigger it (controls, `reduced.sql`):**
- Same rows on a plain table, no view/join at all (Control A).
- `SEMI JOIN` in place of `INNER JOIN ... WHERE flag = 1` (Control B) — the exact
  `SemiJoinFlagRoundTripBuilder` shape is clean; only the `FlagTableJoinQueryBuilder` shape fails.
- The DOUBLE payload column replaced with a second INTEGER (Control C).
- The VARCHAR payload column dropped entirely (Control D).
- `SET disabled_optimizers='top_n_window_elimination'` (Control E).
- duckdb/duckdb#21820's own repro, verbatim, on this build (Control F) — confirmed fixed, and
  structurally different from this finding (needs `PARTITION BY` + an `IN`-list/boolean `WHERE`,
  no join at all).

**Mechanism, so far as pinned down by the stack trace (matching #21820's, frame for frame):**

```
TopNWindowElimination::CreateAggregateOperator(...)
  -> TopNWindowElimination::OptimizeInternal(...)     [recurses through the plan]
  -> LogicalOperator::ResolveOperatorTypes()
  -> LogicalFilter::ResolveTypes()
  -> LogicalOperator::MapTypes(vector<LogicalType> const&, vector<idx_t> const&)
  -> vector<LogicalType>::operator[](idx_t)            <- throws: index >= size
```

`CreateAggregateOperator` builds the replacement aggregate-based plan for the eliminated window,
and constructs a `LogicalFilter` above it whose column-index list (built from the *original*
window operator's bindings) outruns the *rebuilt* operator's type vector once the source is itself
a filtered join (the flag join's own `WHERE r.eq_flag_1 = 1`) rather than a bare scan or a `SEMI`
join — `MapTypes` then indexes past the shorter vector. The exact line inside the ~1270-line
`topn_window_elimination.cpp` that builds the mismatched index list was not isolated further
within triage-time budget (see Open Items) — but the requirement for a *specific* payload-type mix
(int + double + varchar) is consistent with a column-count/type-vector construction that only
misaligns for particular struct layouts, matching #24609's independent report that this pass'
late-materialization path is payload-shape-sensitive.

**Raw stack trace** (release build, no debug symbols — bare addresses; the frame-name mapping
above comes from #21820's own symbolized trace on a debug build, cross-referenced by matching
message text and by the `disabled_optimizers` control, not from resolving these addresses
directly):

```
INTERNAL Error: Attempted to access index 5 within vector of size 5
Stack Trace:
duckdb() [0x7693c0]
duckdb() [0x769428]
duckdb() [0x76dc5c]
duckdb() [0x64d3bc]
duckdb() [0x427248]
duckdb() [0xf5cf40]
duckdb() [0xf059d0]
duckdb() [0xf4f634]
duckdb() [0xdc85b8]
duckdb() [0xdf0dc4]
duckdb() [0xdf0acc]
duckdb() [0xdf1534]
duckdb() [0xdf16e4]
duckdb() [0xd880a4]
duckdb() [0xd88d40]
duckdb() [0xd8968c]
duckdb() [0x924cc0]
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
/lib64/libc.so.6(+0x27540) [0xffffa6e40540]
/lib64/libc.so.6(__libc_start_main+0x98) [0xffffa6e40618]
duckdb() [0x4528b0]
```

The tail six `duckdb()` frames plus the `libc` pair are byte-identical in address to the sibling
`QUALIFY`-composition finding's trace (both crash while processing a batch script through the
same CLI driver/`main` path) — expected, and the divergence starts immediately above that shared
tail, confirming these are two genuinely different call paths through the engine rather than the
same crash observed twice.

**DML impact:** not exercised (the trigger is a read-only `SELECT`; the concrete-form variant used
`INSERT ... SELECT`, which also reproduces — see part 1 of `reduced.sql`).

## How it was found

Surfaced by the same live eqgen campaign as the sibling `QUALIFY`-composition finding, discovered
while triaging the run's 188 findings by grouping distinct error signatures rather than assuming
they were all one bug (per this skill's own guidance) — 187 shared the sibling finding's
column-binding assertion; this one stood out as a different message entirely
(`Attempted to access index N within vector of size N`, not
`Failed to bind column reference`), surfacing in exactly one of the 188 finding files sampled.
Both this finding and the sibling one exist only because two things added in this session combined
in a live campaign: `FlagTableJoinQueryBuilder`'s existing flag-join construct, and a new
sqlancerpp-fork generator feature emitting outer `QUALIFY` workload queries at real volume. Neither
construct is individually novel or wrong (each is sound per `ORACLE_ALGEBRA.md` and passes its own
builder sweep); the campaign found the composition.

A query-pair rewrite oracle (TLP/NoREC/EET) could not have found this via the same route: the
trigger needs a *specific object shape* (a flag-joined view, not a semantically identical
`SEMI JOIN` one) as the thing being read, which is exactly the class of defect an object-equivalence
oracle reaches and a same-query rewrite oracle structurally cannot, since the rewrite would apply to
the query, not manufacture a differently-constructed but row-identical relation to read it from.

- `reduced.sql` in this directory is the full, live-engine-verified repro plus every control listed
  above (including #21820's own repro, run verbatim), against the tip-of-main CLI as part of this
  triage.
- Original finding: `error_round916_0.sql` under the campaign's run directory (not itself checked
  into this repo).

## Open items

- The exact line inside `topn_window_elimination.cpp`'s `CreateAggregateOperator` that constructs
  the mismatched column-index list was not pinned to a `file:line` within triage-time budget.
- The precise reason the payload needs `INTEGER` + `DOUBLE` + `VARCHAR` together (rather than any
  three payload columns, or any two) was characterized empirically (Controls C/D) but not traced
  to a specific struct-layout or type-width calculation in the source.
- Not bisected to a specific commit; no regression window established beyond "the specific shape
  #21820 reported is fixed, this shape is not."
- Recommend filing as a comment/reopen on #21820 rather than a wholly fresh issue, given the
  identical stack trace — the maintainers who reasoned about the original fix are best placed to
  judge whether it needs broadening or a second, shape-specific fix.
