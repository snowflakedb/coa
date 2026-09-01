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

# How the eqgen equivalence oracle found the TiDB `UNION ALL` FD nil-map panic

A methodology note on `tidb_run12/error_round38_0.sql`.
The bug is in [`bug_report.md`](bug_report.md); this document is about the oracle's actual
contribution. It is deliberately less triumphant than the companion note for the ClickHouse finding,
because **the honest answer here is different**: the oracle's value was not in detecting the failure —
anything would have detected it — but in *constructing an input that reaches it*.

## The bug, compressed

`LogicalUnionAll.ExtractFD()` builds `&fd.FDSet{}`, leaving `HashCodeToUniqueID` **nil** — the only
plan-node FDSet construction in the tree that omits `make(map[string]int)`. When that FDSet reaches
`ExtractFDForInnerJoin`, `ExtractEquivalenceCols` → `RegisterUniqueID` writes it unguarded: a Go
runtime panic, recovered and returned as error 1105 `assignment to entry in nil map`. The function's
own nil-repair for that field sits 11 lines *after* the write that needs it.

## Start with the uncomfortable part: detection here was trivial

The ClickHouse finding was a **silent wrong answer** — a well-typed, plausible result that only an
oracle can call wrong. This one is a **one-sided internal error**: base `t` returns 0 rows, the
equivalent raises 1105. Which means:

- **A crash/panic fuzzer would flag this immediately.** No oracle, no reference, no differential
  comparison. SQLsmith, a random-query generator, or TiDB's own AST-level fuzzing would all report
  "query returned INTERNAL error" the moment they produced the shape.
- The differential machinery was, for detection purposes, **not load-bearing**. The oracle noticed
  "error on one side, not the other," but the error alone is self-evidently a bug: `assignment to
  entry in nil map` is never an acceptable answer to a SQL query.

So any claim that "only our oracle could find this" would be false. The interesting question is why
nobody's fuzzer *had* found it, given the bug is latent in released code and the repro is three
statements long.

## The real contribution: the rewrite supplies constructs the query generator never writes

The bug needs a specific composition — measured, each ingredient necessary:

1. the **outer** relation is a `UNION ALL` (probe side of an equi-join);
2. the `IN`'s left operand is an **inequality-quantified `ALL` subquery** (`<`, `>`, `<=`; `<>` and
   `= ALL` are clean) — so the equivalence operand is a `ScalarFunction`, which is the only case where
   `ExtractEquivalenceCols` calls `RegisterUniqueID`;
3. the `IN` subquery is **correlated**.

Ingredient (1) is the one the workload generator cannot supply. Measured across the recorded logs:

| | count |
|---|---|
| workload queries recorded (all dialects/runs) | 1180 |
| …containing `UNION` in any form | **2** (0.17%) |
| …containing `UNION ALL` | **0** |
| TiDB workload queries (`tidb_run12`) containing `UNION` | **0 of 18** |
| finding files whose **rewrite** contains `UNION ALL` | **9310 of 37009** (25%) |
| TiDB findings whose rewrite contains `UNION ALL` | **360 of 1018** (35%) |

And the two outliers do not help: both are `UNION` **distinct**
(`… GROUP BY t1.name) UNION (SELECT GREATEST(True…`), and distinct `UNION` **does not trigger the
bug** — verified:

| relation | result |
|---|---|
| `CREATE VIEW u AS SELECT * FROM t0 UNION ALL SELECT * FROM t0` | **PANIC** |
| `CREATE VIEW u AS SELECT * FROM t0 UNION SELECT * FROM t0` | clean |
| `CREATE VIEW u AS SELECT * FROM t0` | clean |

TiDB plans a distinct `UNION` as `LogicalUnionAll` with an aggregation above it, and that aggregation's
`ExtractFD` goes through `BaseLogicalPlan.ExtractFD`, which *does* `make` the map — so the nil map is
masked. The defect is specific to a bare `LogicalUnionAll`.

That is the whole story: **this query generator never emits the construct the bug requires, and the
one form it does emit is immune.** The `UNION ALL` arrived from the *equivalence rewrite* — the
predicate-split partitioning builder, which splits a predicate into `p` / `NOT p` / `p IS NULL` and
recombines the three with `UNION ALL`. The oracle's rewrite layer is therefore acting as a
**second, independent source of plan constructs**, composed against the query generator's constructs
without either knowing about the other. A fuzzer with only one construct-generating layer explores the
product space of that layer alone; this one explores rewrite-constructs × query-constructs.

This is the same mechanism as the MySQL `REGEXP`-over-`UNION ALL` wrong-result bug — the `UNION ALL`
entered via the rewrite there too — and it generalizes: any construct the rewrite layer emits becomes
reachable in combination with every query feature the generator can write.

## Composability: here it was *unnecessary depth*

Worth recording honestly, because it is the reverse of the ClickHouse finding.

The generated equivalent was a **29-view chain**, four levels of nested predicate-split triples. The
reduction collapsed it to **one two-branch `UNION ALL`, and no view at all** — an inline derived table
reproduces. Stage-by-stage bisection showed the window round-trip, the sequence-augmented copy and the
positional-join reassembly were all inert; and layering an `ENUM` cast *above* the `UNION ALL` actually
**masks** the panic.

So for this bug, chain depth bought nothing: a single builder at depth 1 would have found it. The
deep chain mainly made triage harder. Contrast the ClickHouse finding, where a single perturbation
demonstrably was *not* enough and depth was essential. The lesson is not "depth is good" but
"depth is cheap" — because row-preservation composes, the generator can afford to stack builders, and
some findings need it while others are buried by it.

There is a cost worth naming: masking. An `ENUM` cast above the `UNION ALL` hid this panic, and in the
DuckDB `ANTI JOIN` finding an `ENUM` cast likewise masked the crash at one chain level and let it
reappear at the next. Deep chains can *suppress* the very bugs shallower ones expose, which argues for
varying depth across rounds rather than always maximizing it.

## Why it had not been found already

The bug is old — `&fd.FDSet{}` and the unguarded `RegisterUniqueID` are both present in **v8.5.3** —
yet the repro does not fire there. What changed is **reachability**, and that is the most interesting
part of this finding:

- At v8.5.3 there is exactly **one** `ExtractFD()` caller outside the logicalop recursion
  (`logical_plan_builder.go`, the `only_full_group_by` check — itself gated behind
  `tidb_enable_new_only_full_group_by_check`, which defaults to **0**). The FD machinery was
  effectively dormant.
- On master there are **seven**, including `constantCols := p.ExtractFD().ConstantCols()` in
  `GetMergeJoin`, added by commit `6ed8d498fd` (#68001, "preserve suffix order for MergeJoin with
  constant leading keys", 2026-04-27, closing #67755).
- `git tag --contains 6ed8d498fd` → **nothing**. The reachability is master-only and unreleased.

So a latent nil map sat harmlessly in released TiDB for a long time, and a performance improvement
wired it into physical planning. The oracle found it roughly three months later. This is a general
shape worth watching: **when a subsystem gains new consumers, latent invariant violations in it become
reachable**, and the new consumer's authors have no reason to audit the old subsystem's
initialization. A fuzzer running continuously against master is well placed to catch exactly this
class, and catching it *before* the commit ships is most of the value here.

## What generalizes

1. **Two construct-generating layers beat one.** The rewrite layer emits plan shapes the query
   generator never writes (`UNION ALL`: 0 of 1180 workload queries, 25% of rewrites). Their product is
   the real search space.
2. **A cheap oracle is still worth having on top of a crash channel.** Detection here needed nothing
   clever; the differential comparison mattered for the *other* findings in the same run, and cost
   nothing extra on this one.
3. **Latent-plus-newly-reachable is a rich seam.** Track which internal subsystems recently gained
   callers; that is where dormant invariant violations surface.
4. **Reduce the chain before believing the chain.** 29 views → 1 `UNION ALL`. The generated form badly
   overstates what the bug needs, and reporting it unreduced would have buried a 3-statement repro.
5. **Watch for masking.** Constructs layered above the trigger (`ENUM` casts, distinct `UNION`,
   aggregations) can hide it. A clean round is not evidence of a clean engine.

## Honest limits

- The oracle demonstrated *reach*, not detection power, on this finding. Do not cite it as an example
  of the differential oracle's unique capability — cite the ClickHouse wrong-result finding for that.
- The panic surfaced on `--store=unistore`; it is planner-side and never touches storage, but it has
  not been confirmed against a TiKV-backed cluster.
- I have **not** established that released TiDB is unreachable by *some other* query. The nil map is
  there, and the old `only_full_group_by` FD path also recurses through joins; whether a query reaches
  it with `tidb_enable_new_only_full_group_by_check=1` is untested. "Unreleased regression" is the
  claim I can support; "8.5 is safe" is not.
- Whether the FD machinery's other six new consumers on master admit similar nil-map paths was not
  investigated, and would be the obvious follow-up.
