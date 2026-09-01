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

# How the V3 equivalence oracle found the ClickHouse framed-`RANK` bug

## The bug, compressed

`RANK()` / `DENSE_RANK()` with an explicit `ROWS` frame return the row number instead of the rank, so
ORDER BY peers get distinct values. Root cause: `WindowTransform::arePeers` declares any two distinct
rows non-peers when the frame type is `ROWS` (correct for frame *boundaries*), and the ranking
functions read that same peer counter as their *ranking* peer group.

The second-order consequence is the one that matters here:

> **A framed `RANK` over ties is decided by physical row arrival order.**

So a query whose answer is fully determined by the SQL standard becomes a query whose answer depends
on how the rows happen to be laid out on disk.

## Why this bug sits exactly on our oracle's axis

The oracle's contract is: build a relation `t'` holding **the same row multiset** as `t` via
row-preserving builders, run the *same* query against both, and require identical results. Ground
truth is manufactured — no reference engine, no expected output.

Restated, the oracle's job is to **hold logical content fixed while varying physical
representation.** That is a precise description of the axis this bug lives on. The oracle did not
stumble onto it; a wrong answer that is a function of row order is the *only* kind of bug this
particular invariant is guaranteed to expose.

Measured, with byte-identical 8-row content on both sides:

| construction | active parts | scan order of `name` (no `ORDER BY`) | mismatch? |
|---|---|---|---|
| BASE — 8 separate `INSERT`s | 3 | `['a','','dup','dup',NULL,'zzz','b','é']` | — reference |
| trivial CTAS copy | 1 | `['a','','dup','dup',NULL,'zzz','b','é']` | **no** |
| only the `FIRST_VALUE` round-trip | 1 | `['','a','b','dup','dup','zzz','é',NULL]` | **YES** |
| full 4-step chain, as generated | 1 | `['','a','b','dup','dup','zzz','é',NULL]` | **YES** |

Two things in that table are worth dwelling on.

**The trigger is row order, not storage structure.** The trivial CTAS copy collapses 3 MergeTree parts
into 1 — a real physical change — and still does not reproduce, because it preserves insertion order.
Only a builder that *permutes* rows does.

**The load-bearing builder is load-bearing by accident.** `FIRST_VALUE(name) OVER (PARTITION BY name
ORDER BY name)` is a row-preserving identity transform on the data; its relevant property is the
`ORDER BY` that forces a sort, so the materialized copy is name-sorted rather than insertion-ordered.
It contributes no semantics to the query at all. That distinguishes this finding from our usual shape
— e.g. the DuckDB `ANTI JOIN … ON TRUE` and TiDB `UNION ALL` findings, where a rewrite construct trips
an optimizer rule and the construct is *semantically* implicated. Here the pairing is **query feature
× row permutation**.

## The composability property, and why it did the work

Each builder is individually row-multiset-preserving. That makes the invariant **closed under
composition**: any chain of builders is row-preserving, with no new proof obligation per chain. So
the generator can stack four, ten, or thirty builders and the oracle's ground truth still holds
exactly — which is how it explores a large space of physical representations cheaply.

This matters because, as the table shows, **a single perturbation was not enough.** An oracle armed
only with "copy the table" would have compared two identically-ordered relations and reported clean.
The bug surfaced because the chain happened to include a sort-forcing builder among its steps. Depth
and diversity of composition, not any single clever transform, is what converted a latent
order-dependence into an observable diff.

The base data mattered too, and by design: the fixed base catalog contains `(0,'dup','dup')` and
`(1,'dup','dup')` — deliberate duplicates. Ties are a **precondition** for this bug, and the two rows
that ultimately disagreed were exactly that duplicate pair. A generator that only produced distinct
values could not have found it at any chain depth.

## Why the other oracles miss it

Ordered by how plausibly someone would propose them.

**Crash / assertion fuzzers (ClickHouse's own AST fuzzer, SQLsmith).** Blind by construction: the
query succeeds and returns a well-typed result. There is no error, no assertion, no crash — only a
wrong number.

**Repeat execution, or perturbing execution settings.** The obvious cheap oracle: run the query
twice, or under different `max_threads`, and demand a stable answer. **Measured: it fails.** The BASE
side alone is stable across 6 identical runs *and* across `max_threads` 1/2/4/8/16. The reason is
structural — physical row order here is a property of the **stored data**, fixed at write time, not
of the execution plan. Perturbing the executor cannot reach it; you have to perturb the storage, which
is what a row-preserving rewrite chain does. This is the sharpest argument in the document, because
it is the alternative most likely to be assumed sufficient.

**TLP (Ternary Logic Partitioning).** Splits rows on a predicate into `p` / `NOT p` / `p IS NULL` and
requires the union of the three results to equal the unpartitioned result. Structurally inapplicable
here: a window function is computed over its partition, so removing rows changes every surviving
row's window input, and TLP's invariant does not hold for window queries in the first place. Notably
ClickHouse has an open issue to *add* TLP and NoREC oracles to its AST fuzzer
([#99980](https://github.com/ClickHouse/ClickHouse/issues/99980)) — neither would have caught this.

**NoREC (Non-Optimizing Reference Engine Construction).** Compares an optimizable query against a
form the optimizer cannot optimize, to catch bugs where the optimized plan disagrees. Blind here for
a simple reason: **this is not an optimizer bug.** It is in the window transform's semantics, and it
is computed the same wrong way whether or not the query was optimized. The optimized and
non-optimized forms agree — with each other, and both wrong.

**Differential testing against a reference engine.** *This one could have found it* — DuckDB 1.5.5,
DuckDB 2.0-alpha and PostgreSQL all return `[1,1,3]` where ClickHouse returns `[1,2,3]`, so a
cross-engine comparison on this query would have flagged it. Stating that plainly is more useful than
claiming uniqueness. What our oracle buys instead is that it needs **no second engine**: no shared
dialect subset (ClickHouse's diverges substantially), no reconciling of NULL ordering, collation or
type-promotion differences, and no canonical row ordering to compare across systems. It also keeps
working on engine-specific surface — `MergeTree` engines, `arrayJoin`, ClickHouse-only functions —
where no reference engine exists to compare against.

**A targeted test of the exact invariant — which upstream already has.** The most instructive miss.
ClickHouse's own documentation asserts frame-independence for this function class:

```
-- row_number does not respect the frame, so rn_1 = rn_2 = rn_3 != rn_4
```

demonstrated with a real `ROWS BETWEEN 1 PRECEDING AND CURRENT ROW` frame. The property was
identified, written down, and exercised — but the example uses `order` values `1…5`, all **distinct**.
With no ties, the correct and degenerate answers coincide, and `rank`/`dense_rank` slip through a test
of the very invariant they violate. A tie is one row of test data away, and the gap survived anyway.

## What generalizes

1. **Pick an invariant that brackets the failure mode.** Logical-content-fixed / physical-form-varied
   catches order-dependence; nothing about the query text does.
2. **Perturb storage, not just execution.** Row order is written, not planned. Settings-sweep oracles
   cannot see it.
3. **Composition is the cheap axis.** Because row-preservation composes, chain depth costs nothing in
   proof obligations and buys layout diversity. One perturbation demonstrably was not enough.
4. **Ties, duplicates and NULLs belong in the fixed base data.** They are preconditions for a whole
   class of peer-group and ordering bugs, and their absence is why a correct upstream test missed this.
5. **Order-dependence cuts both ways — mind admissibility.** The same query with `ROW_NUMBER()` in
   place of `RANK()` also diverges between the two sides, but *innocently*: `ROW_NUMBER` over a
   tie-incomplete `ORDER BY` is legitimately non-deterministic, so that query is inadmissible and the
   diff is an oracle defect, not a bug. `RANK` over the same `ORDER BY` is deterministic, which is what
   makes this one real. An oracle that varies physical layout **must** pair with a rule for which
   queries are permitted to notice — see the admissibility contract, and note this is exactly why the
   TiDB dialect disables `WindowFunctionBuilder` outright.

## Honest limits

- The oracle only sees this if the generator emits a framed ranking function over a column with ties;
  it found one instance, not the general class.
- With a chain that happens to preserve order (the trivial-copy row above), it reports clean. Absence
  of a diff is weak evidence.
- The finding was produced on a master nightly, because the dialect fetches from
  `builds.clickhouse.com/master`. Establishing that releases are affected took a separate,
  deliberate cross-check — the oracle says nothing about which versions ship a bug.
- Nothing here was verified against a TiKV-style multi-node ClickHouse cluster or a distributed table
  engine, where physical layout has more degrees of freedom and the effect may be larger.
