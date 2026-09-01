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

# TiDB: `predicate_push_down` returns a nil plan, and `logicalOptimize`'s deferred timer turns it into an unattributable nil-pointer panic

## Summary

Two defects, one behind the other. The visible one is a mask over the real one.

1. **`PPDSolver.Optimize` can return a nil plan.** `rule_predicate_push_down.go:43-47` is
   `_, p, err := lp.PredicatePushDown(nil); return p, planChanged, err`. For the plan shape below —
   a `LEFT OUTER JOIN` relation with an `IS NULL` predicate on its null-padded side, grouped by that
   same column, under an `x = ALL (subquery) IN (subquery)` filter — it returns `p == nil`.

2. **`logicalOptimize` dereferences that nil in a `defer`, destroying the diagnosis.**
   `optimizer.go:1075-1078`:

   ```go
   func logicalOptimize(ctx context.Context, flag uint64, logic base.LogicalPlan) (base.LogicalPlan, error) {
       defer func(begin time.Time) {
           logic.SCtx().GetSessionVars().DurationOptimizer.LogicalOpt = time.Since(begin)   // line 1077
       }(time.Now())
   ```

   The closure captures the **variable** `logic`, not its value at entry. The rule loop then does
   `logic, planChanged, err = rule.Optimize(ctx, logic)`, so the moment any rule yields nil, `logic`
   is nil for the deferred call. `logic.SCtx()` then dereferences nil: SIGSEGV, recovered by
   `planner.optimizeNoCache` (`optimize.go:226`), and reported to the client as

   ```
   ERROR 1105 (HY000): runtime error: invalid memory address or nil pointer dereference
   ```

   The rule's own error, if it had one, never reaches the user. Nor does the identity of the rule.
   Any logical rule returning `(nil, _, err)` lands here — and 25 `rule_*.go` files do exactly that
   on their error paths (`rule_decorrelate.go:313, 349, 386, 393`, `rule_correlate.go:63`, …), so
   this defect is not specific to predicate pushdown. It converts every one of those error paths into
   the same undiagnosable panic.

Fixing (2) alone would already be worthwhile: it costs nothing and turns a class of panics into
actionable errors. Fixing (1) is the real bug.

## Environment

```
Release Version: v9.0.0-beta.2.pre-2051-g3bea8196a5
Git Commit Hash: 3bea8196a565ca01800b2d0807868f01139d8a30
Git Branch:      master
UTC Build Time:  2026-07-30 16:56:32
GoVersion:       go1.26.4
Race Enabled:    false
Store:           unistore
Kernel Type:     Classic
```

## Reproduction

See `reduced.sql`. Two DDL statements and one `EXPLAIN`, on an **empty table**:

```sql
CREATE TABLE b (id BIGINT, k BIGINT);
-- then the EXPLAIN in reduced.sql
```

`sql_mode` and collation are irrelevant; the fuzzer's session pins
`STRICT_ALL_TABLES,…,ONLY_FULL_GROUP_BY` and `utf8mb4_0900_bin`, but the panic reproduces without them.

## Attribution: which rule, established by elimination

TiDB's own `mysql.opt_rule_blacklist` identifies the culprit:

| blacklisted rule | result |
|---|---|
| `predicate_push_down` | **plans cleanly** |
| `decorrelate` | panics |
| `correlate` | panics |
| `aggregation_push_down` | panics |
| *(none)* | panics |

So the nil plan comes from predicate pushdown. Given the shape, the suspect transformation is
pushing `IS NULL` through the null-padded side of a `LEFT OUTER JOIN` — a rewrite that is not
generally sound, and where a rejection path plausibly returns no plan.

## Minimal oracle exposure path

- **Object composition arity:** `2`.
- **GCL builder path:** `SequenceOuterJoinQueryBuilder → CreateViewBuilder`.
- **Confidence:** Inferred from the report's surrogate-key column split/rejoin SQL and the current
  Sequence builder; historical GCL AST metadata is not preserved.
- **Realization:** a root view exposes the split-and-rejoin outer-join query. The key table and half
  views are implementation details hardcoded inside the Sequence transform, not separately selected
  arity factors.
- **Workload/data requirements (excluded from arity):** the `IS NULL`, `GROUP BY`, three-way cross
  join, quantified `ALL`, and nested `IN` shape are workload requirements; the empty table is a data
  condition. None is counted.
- **Exposure vs. intrinsic trigger:** for this report's reduced producer, the split/rejoin outer-join
  relation is intrinsic, while the root view is exposure-only because inline derived tables reproduce.
  This path does not claim that all later nil-plan findings share the same underlying producer.

## Minimality

Each ingredient was removed individually; the panic stops when any one goes.

| ingredient | removing it |
|---|---|
| `LEFT OUTER JOIN` source | plans fine (`SELECT id, id AS name FROM b` as the source) |
| `t4.name IS NULL` on the null-padded side | plans fine |
| `GROUP BY t5.id, t4.name` (grouping by that column) | plans fine with `GROUP BY t5.id` |
| 3-way `CROSS JOIN` in the `IN` subquery | plans fine with 2 tables |
| `x = ALL (subquery)` as the `IN` left operand | plans fine with `x = 1`, and fine with the `ALL` dropped |
| rows in the table | not needed — empty table panics |
| executing the query | not needed — bare `EXPLAIN` panics |
| the views | not needed — inline derived tables panic identically |

## Suggested fix

For (2), either capture the context before the loop or guard the deferred read:

```go
func logicalOptimize(ctx context.Context, flag uint64, logic base.LogicalPlan) (base.LogicalPlan, error) {
    sctx := logic.SCtx()                       // captured once, cannot become nil
    defer func(begin time.Time) {
        sctx.GetSessionVars().DurationOptimizer.LogicalOpt = time.Since(begin)
    }(time.Now())
```

A nil check inside the closure works too, but capturing is better: it also records the duration on
the error path, which the current code silently skips whenever a rule fails.

Worth considering alongside it: the loop does not check `logic` for nil after `rule.Optimize`, so a
rule returning `(nil, changed, nil)` — nil plan, **no** error — hands nil to the *next* rule rather
than stopping. A `nil` plan with a `nil` error is a rule-contract violation, and asserting it in the
loop would attribute the fault to the offending rule by name.

## How it was found

The differential fuzzer builds
row-equivalent rewrites of a base table and diffs query results across them. This surfaced as 7
findings in one run (`logs/tidb_run18`, rounds 99, 261, 296×3, 309, 373) where the query succeeded
against the base table and panicked against an equivalent whose source was a split-rejoin view — a
table split into two column-disjoint views and re-joined on a synthetic `ROW_NUMBER` key, which is
where the `LEFT OUTER JOIN` came from.

Because defect (2) erases the rule identity, **all 7 share this proximate cause but may not share a
single underlying rule error.** Only this one has been attributed to `predicate_push_down` by
elimination; the other six are recorded as the same panic signature and no more than that. That
ambiguity is itself an argument for fixing (2) first.

## Also seen in `tidb_run19` — 62 further findings, and they sharpen the case for fixing defect (2) first

`logs/tidb_run19` produced 300 `error_*` findings in three message classes; **62** are this one
(`1105 runtime error: invalid memory address or nil pointer dereference`), across 44 distinct rounds.
Same build as this report's (`3bea8196`, unistore, assertions off).

All 62 reproduce with **one of two** minimal relations, each replacing the finding's whole chain while
keeping its own workload query (both verified row-identical to a plain table, which is clean for every
one of these queries):

| minimal relation | findings |
|---|---|
| surrogate-key **column split + `LEFT OUTER JOIN` rejoin** (this report's construct) | **59 / 62** |
| **`CASE WHEN <tautology> THEN col ELSE NULL END` passthrough wrapper** view | **3 / 62** (rounds 897, 1956, 3260) |

For the 59, the `LEFT OUTER JOIN` is load-bearing — swapping it for an `INNER JOIN` is clean — and an
inline derived table works as well as a view. Of the 3 `CASE`-wrap findings, two reproduce with the
wrapper over a plain table and one (round 3260) needs it composed over a uid/flag `RIGHT OUTER JOIN`
filter view.

### The rule fingerprints are heterogeneous — which is exactly what defect (2) predicts

`mysql.opt_rule_blacklist`, on the minimal relations:

| finding | *(none)* | `predicate_push_down` | `decorrelate` | `column_prune` | `aggregation_push_down` |
|---|---|---|---|---|---|
| round 147 (split-rejoin) | `nilptr` | **clean** | `nilptr` | **clean** | `nilptr` |
| round 1018 (split-rejoin) | `nilptr` | `nilptr` | `nilptr` | `nilptr` | `nilptr` |
| round 897 (CASE-wrap) | `nilptr` | `nilptr` | **clean** | `nilptr` | **clean** |
| round 1956 (CASE-wrap) | `nilptr` | `nilptr` | **clean** | `nilptr` | **clean** |

So within this one message class the nil plan comes from **at least three different rules**
(`predicate_push_down`, `decorrelate`, `aggregation_push_down`, plus `column_prune` as an alternative
cure), and round 1018 is cured by none of the four — so there is at least one further producer outside
that set.

This is precisely the prediction of **defect (2)**: `logicalOptimize`'s deferred closure captures the
`logic` *variable*, so the moment *any* rule yields nil the deferred `logic.SCtx()` dereferences nil
and the resulting SIGSEGV is reported as an unattributable 1105. The rule identity is erased before it
reaches the client.

**Revised conclusion for this report.** Defect (2) is confirmed as the shared proximate cause across
62 more findings and 44 more rounds — it is the high-value fix, and fixing it alone would convert all
of them from one indistinguishable panic into attributable per-rule errors. Defect (1)
(`predicate_push_down` returning nil) is **not** the sole underlying producer: it accounts for only
part of cluster B. The `AND`-ed claim to avoid making is "these 62 are all the predicate-pushdown bug";
the supportable claim is "these 62 are all the nil-plan mask, over ≥3 distinct producers".

**Caveat:** no backtrace was captured for the run19 findings — the panic is recovered and nothing is
written at the server's default `error` log level — so producer attribution rests entirely on the
blacklist elimination above.
