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

# DuckDB: `TopNWindowElimination` rewrites a large-but-valid `ROW_NUMBER() <= k` bound into an internal `arg_min`/`arg_max` (or `MIN`/`MAX`) list aggregate without checking `k` against that aggregate's own size cap, so a tautological filter throws instead of returning every row

## Summary

`QUALIFY`/`WHERE (ROW_NUMBER() OVER (ORDER BY ...)) <= k` for any `k >= 1,000,000` throws
`Invalid Input Error: Invalid input for arg_min/arg_max: n value must be < 1000000` (or, for a
single-column projection, the sibling message `Invalid input for MIN/MAX: n value must be <
1000000`) — regardless of how many rows the table actually has, including zero. The
`TopNWindowElimination` optimizer pass rewrites this filter shape into an internal
`arg_min`/`arg_max`/`MIN`/`MAX` **list aggregate** call, passing the literal `k` straight through
as that aggregate's `n` parameter. Those aggregates cap `n` at `MAX_N = 1,000,000` — a sane guard
against an unbounded heap allocation when a *user* calls `arg_min(x, y, 10000000000)` directly —
but the rewrite never checks the literal against that cap, or falls back to the plain window path
when it would be exceeded. A query that never mentions `arg_min`/`arg_max`/`MIN`/`MAX` as
aggregates fails with an error naming a function it doesn't call, for a filter that is true of
every row on any table smaller than a million rows.

## Environment

- **DuckDB v2.0.0-alpha37464 (Cyanoptera)** `ea53ecdca1` — the `main`/CLI build fuzzed by eqgen,
  downloaded from `artifacts.duckdb.org/latest` at time of triage.
- Also reproduces **unchanged on released DuckDB v1.5.0** (via the `duckdb` Python wheel) — this is
  not a recent regression; the defect has been present since at least v1.5.0 and survives to the
  current nightly.
- Access path: reproduces identically via the CLI (`:memory:`) and via the Python wheel (embedded).
  No `sql_mode`/charset/collation applicable (DuckDB).

## Minimal repro

```sql
CREATE TABLE t (c_pk INTEGER, c_int INTEGER);
INSERT INTO t VALUES (1,10),(2,20),(3,30);
SELECT c_pk, c_int FROM t QUALIFY (ROW_NUMBER() OVER (ORDER BY c_pk)) <= 1000000;
```

Full version with controls in `reduced.sql`.

## Expected vs actual

| Query | Expected | Actual |
|---|---|---|
| `... QUALIFY (ROW_NUMBER() OVER (ORDER BY c_pk)) <= 1000000` (2+ columns) | 3 rows (all of them) | `Invalid Input Error: Invalid input for arg_min/arg_max: n value must be < 1000000` |
| same, single-column projection | 1 row | `Invalid Input Error: Invalid input for MIN/MAX: n value must be < 1000000` |
| same, bound `<= 999999` | 3 rows | 3 rows (correct) |
| `... ORDER BY c_pk LIMIT 4611686018427387904` (same huge constant, no `QUALIFY`) | 3 rows | 3 rows (correct) |
| same query, `SET disabled_optimizers='top_n_window_elimination'` | 3 rows | 3 rows (correct) |
| same shape over a **0-row** table | 0 rows | 0 rows (correct — see Characterization for why this one is fine) |

**The engine is wrong**, established two independent ways without needing a second engine:

1. By construction: `ROW_NUMBER() OVER (ORDER BY c_pk)` for a table of size *N* never exceeds *N*,
   and no real table reaches `10^6` rows in this fuzzer's fixtures — or, more strongly, for *any*
   value of `k` at or above the table's row count, `row_number() <= k` is a tautology, independent
   of what `k` actually is. So the correct answer for *every* row count below the bound is "return
   every row," which is exactly what the query does for `k = 999999` and what `LIMIT` with the
   identical constant does.
2. By elimination: the only variable that changes the outcome is whether `TopNWindowElimination`
   runs (Control C). Disabling exactly that one optimizer, with everything else about the query
   byte-identical, makes it succeed. That localises the defect to the rewrite, not to the
   `arg_min`/`arg_max`/`MIN`/`MAX` guards themselves, which are legitimate for their designed
   purpose (bounding a user-requested heap size on a direct call).

## Equivalence construction

This was found while validating a new eqgen DuckDB builder, `DuckDBRowNumberBoundQualifyBuilder`
(algebra rule **(Qualify)**, see `ORACLE_ALGEBRA.md` §3), *before* it was ever weighted into a fuzz
run — i.e. by hand, not from an automated finding file. The builder emits:

```sql
SELECT <passthrough columns> FROM <source>
QUALIFY (ROW_NUMBER() OVER (ORDER BY <col0>)) <= 4611686018427387904   -- 2**62
```

`2**62` was chosen specifically to be "comfortably above any row count this harness ever samples"
so the filter is an identity **by construction**, not because the fixture happens to be small — the
same argument as the existing, already-shipped `QualifyQueryBuilder` (`ROW_NUMBER() >= 1`, unbounded
above). The difference is direction: `>= 1` has no upper bound to fold, so it can never match
`TopNWindowElimination`'s pattern (which specifically targets a **bounded-above**
`row_number() <= k` filter, the shape that can become a physical Top-N/list aggregate). This new
builder exists precisely to exercise that pattern, which is why it tripped over the defect on its
very first sweep against the tip-of-main CLI, with zero rows-mismatch involved — the *equivalent*
object's own `CREATE VIEW`/`SELECT` failed to even execute while the base table read succeeds
trivially, i.e. a clean one-sided failure (equivalent to an `error_*.sql`-shaped finding), which
passes the admissibility gate independent of anything eqgen's row/type comparison does.

No construct is reduced away here: the distilled repro (`reduced.sql` §2/§3) drops eqgen's view
wrapper, base-table naming, and extra columns entirely and still reproduces identically — this is a
plain-SQL DuckDB defect with no dependency on the equivalence machinery at all.

## Minimal oracle exposure path

- **Object composition arity:** 0 — no base/equivalent object contrast.
- **GCL builder path:** none; this was a hand/pre-flight sweep of `DuckDBRowNumberBoundQualifyBuilder`, not a minimal equivalence-object chain.
- **Confidence:** Exact.
- **Realization:** none; the minimal exposure is a bare `SELECT` over an ordinary table.
- **Workload/data requirements (excluded from arity):** unpartitioned `ROW_NUMBER() OVER (ORDER BY ...) <= k`, `k >= 1,000,000`, and at least one input row.

**Exposure vs. intrinsic trigger:** The pre-flight sweep supplied the query shape but no base/equivalent object contrast is part of the minimal exposure. The intrinsic trigger is entirely the `TopNWindowElimination` rewrite passing the oversized bound to an internal list aggregate.

## Characterization

**Trigger:** `(ROW_NUMBER() OVER (ORDER BY <col>)) <= k` (or `WHERE`, not just `QUALIFY`; both lower
to the same filter) with `k >= 1,000,000`, no `PARTITION BY`.

**Does NOT trigger it (controls, `reduced.sql`):**
- `k <= 999999` — succeeds, returns every row. Bisected boundary is exactly `1,000,000`.
- A plain `ORDER BY ... LIMIT <same huge constant>` — no `QUALIFY`, so `TopNWindowElimination`'s
  filter-shape match never engages; no cap anywhere in the `LIMIT` path.
- `SET disabled_optimizers='top_n_window_elimination'` — same query, same huge bound, succeeds.
  This is the decisive control: it isolates the defect to this one pass.
- Zero rows: plans and runs the identical rewrite (confirmed via `EXPLAIN`, see below) but does not
  throw. **Mechanism:** the guard lives inside the aggregate's per-state `Update` callback
  (`extension/core_functions/aggregate/distributive/arg_min_max.cpp:720-730`, and the sibling
  `src/function/aggregate/distributive/minmax.cpp:483-486` for the single-column/no-payload case),
  and only runs `if (!state.is_initialized)` — i.e. on the *first row that reaches the aggregate*.
  With zero input rows the state is never initialized, so the check never runs. This also means the
  defect is masked by an empty table, which is exactly the kind of table an under-populated test
  fixture is likely to use — a plausible reason it survived from v1.5.0 to `main` unnoticed.

**Mechanism, named in `file:line` terms:**

```cpp
// extension/core_functions/aggregate/distributive/arg_min_max.cpp:720-730
static constexpr int64_t MAX_N = 1000000;
const auto nval = UnifiedVectorFormat::GetData<int64_t>(n_format)[nidx];
if (nval <= 0) {
    throw InvalidInputException("Invalid input for arg_min/arg_max: n value must be > 0");
}
if (nval >= MAX_N) {
    throw InvalidInputException("Invalid input for arg_min/arg_max: n value must be < %d", MAX_N);
}
```

```cpp
// src/function/aggregate/distributive/minmax.cpp:483-486  (sibling guard, single-column case)
throw InvalidInputException("Invalid input for MIN/MAX: n value must be > 0");
...
throw InvalidInputException("Invalid input for MIN/MAX: n value must be < %d", MAX_N);
```

Both guards are correct and necessary **for their designed purpose**: bounding the heap DuckDB
allocates for an `arg_min(x, y, n)` / `MIN(x, n)` *list* aggregate (top-`n`-values-as-a-list), which
is a real, user-facing function where a literal `n` in the billions would try to allocate a
proportionally sized heap. The defect is that `TopNWindowElimination` (confirmed as the responsible
pass via Control C; source at `src/optimizer/topn_window_elimination.cpp` and
`src/optimizer/topn_optimizer.cpp`) constructs one of these aggregates from a `row_number() <= k`
filter **without first checking `k < MAX_N`**, and without a fallback to the plain windowed-filter
plan when it isn't. The exact call site inside `TopNWindowElimination`/`OrderedAggregateOptimizer`
that builds the `arg_min`/`arg_max`/`MIN`/`MAX` node from the bound was not pinned within triage-time
budget — the two guard sites and the responsible pass are pinned precisely; see Open Items.

**`EXPLAIN` evidence** — the rewrite happens at logical-plan time, but the guard only fires at
*execution* (inside `Update`, on the first row), so a bare `EXPLAIN` does **not** reproduce this
(unlike some of the join-family findings already in this repo, where `EXPLAIN` alone throws):

```
EXPLAIN SELECT c_pk FROM t8 QUALIFY (ROW_NUMBER() OVER (ORDER BY c_pk)) <= 1000000;

╭─ Projection ── Projections: #1 ──╮
╭─ Unnest ──────────────────────────╮
╭─ Ungrouped Aggregate ── Aggregates: min(#0, #1) ──╮   <-- the rewrite, visible pre-execution
╭─ Projection ── Projections: c_pk, 1000000 ────────╮
╭─ Seq Scan ── Table: memory.main.t8 ───────────────╯
```

**DML impact:** not applicable — the trigger is a `SELECT`/view-defining filter, not writable
through `DELETE`/`UPDATE` in any way that differs from a plain `SELECT`.

**Severity bound:** this is an availability defect (valid query refuses to run), not a wrong-result
defect — the engine never returns incorrect rows here, it refuses to return any.

## How it was found

Found by the data-equivalence oracle's construction step, not its comparison step: eqgen's
`sweep_builder` harness (`eqgen/fuzz/sweep.py`) builds a candidate equivalence object and checks it
row-for-row against the base table before ever trusting it in a differential run. Validating a new,
not-yet-weighted builder (`DuckDBRowNumberBoundQualifyBuilder`) against the tip-of-main CLI failed
immediately — 25/25 seeds raised the same exception rather than diverging in rows — which is exactly
the harness's designed behavior for "the object itself is broken" (§5 of `ARCHITECTURE.md`: a
generated object is *decidable by execution*, and here execution says no). Manually distilling the
generated DDL (three chained `CREATE VIEW`s, each repeating the same `QUALIFY`) down to one bare
`SELECT` reproduced the identical error with no eqgen machinery involved at all, which is what
established this as a plain DuckDB defect rather than anything about the harness's own construction.

A query-pair rewrite oracle (TLP/NoREC/EET) would be unlikely to surface this class at all: those
approaches hold the *query* fixed and vary its algebraic form, but the trigger here is a **filter
shape** (`row_number() <= k` for a specific range of `k`) that has to appear in the query text
itself — there is no pair of differently-shaped-but-equivalent queries that isolates "a huge but
otherwise ordinary literal breaks an internal aggregate's heap-size guard." It surfaced here only
because eqgen validates a *new object construction* against a live engine before ever using it, and
that construction happened to need exactly this filter shape to express "keep every row" as an
upper-bounded `ROW_NUMBER` predicate.

- Single symptom, single root cause — every seed in the sweep (25/25) failed with the identical
  message; confirmed not data-dependent by Control D (fails on a populated table, does not fail on
  an empty one, independent of *which* rows are present).
- `reduced.sql` in this directory is the full, live-engine-verified repro plus every control listed
  above, run end-to-end against the tip-of-main CLI as part of this triage.

## Open items

- The precise call site inside `TopNWindowElimination` (or `OrderedAggregateOptimizer`, per the
  `arg_min`/`arg_max` construction site found at `src/optimizer/rule/ordered_aggregate_optimizer.cpp`)
  that builds the `arg_min`/`arg_max`/`MIN`/`MAX` node from the `row_number() <= k` filter was not
  pinned to an exact `file:line` within triage-time budget — the guard sites and the responsible pass
  (via `disabled_optimizers`) are pinned precisely, but the fix location (add a `k < MAX_N` check, or
  fall back to the plain window+filter plan above the cap) needs that exact call site.
- Not established whether `PARTITION BY <key> ... QUALIFY row_number() <= k` (per-partition, rather
  than whole-relation) hits the same cap — worth a follow-up query if filing upstream, since eqgen's
  `KeyQualifyDedupReduceBuilder` uses a per-partition `= 1` form that would not trigger this (the
  literal there is always `1`, far under `MAX_N`), so this repo's fuzzing does not currently exercise
  that variant.
- Not bisected to a specific commit — confirmed present on both v1.5.0 (released) and the current
  nightly, so this is a long-lived defect rather than a fresh regression; no commit range narrowed.
