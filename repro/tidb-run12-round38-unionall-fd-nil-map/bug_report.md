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

# TiDB: `UNION ALL` relation + `IN (correlated subquery)` over an `ALL` subquery panics the planner (nil-map write in functional-dependency extraction)

## Summary

`LogicalUnionAll.ExtractFD()` constructs its `FDSet` as `&fd.FDSet{}`, leaving the
`HashCodeToUniqueID` map **nil** — it is the only plan-node `FDSet` construction in the tree that
omits `make(map[string]int)`. When that `FDSet` flows into `LogicalJoin.ExtractFDForInnerJoin` (via
the semi-join FD path, during physical planning of a merge join), `ExtractEquivalenceCols` calls
`FDSet.RegisterUniqueID`, which writes the map **without a nil check** — a Go runtime panic,
`assignment to entry in nil map`, recovered by `planner.optimizeNoCache` and returned to the client
as error 1105. The function's own nil-repair for that exact field sits 11 lines *later*
(`logical_join.go:890`), so the guard exists but runs after the write that needs it. Reproduces on an
**empty table** with a **3-statement** script, fails at bare `EXPLAIN`, and is
**`sql_mode`-independent**.

## Environment

`SELECT tidb_version();`

```
Release Version: v9.0.0-beta.2.pre-2051-g3bea8196a5
Edition: Community
Git Commit Hash: 3bea8196a565ca01800b2d0807868f01139d8a30
Git Branch: master
UTC Build Time: 2026-07-30 16:56:32
GoVersion: go1.26.4
Race Enabled: false
Check Table Before Drop: false
Store: unistore
Kernel Type: Classic
```

`SELECT VERSION();` → `8.0.11-TiDB-v9.0.0-beta.2.pre-2051-g3bea8196a5`

- Built from source off `master`; assertions off, race detector off.
- `Store: unistore` (single-process embedded test store). **Caveat:** unistore is not TiKV. This is a
  planner-side panic that never reaches the storage layer, so the store is very unlikely to matter,
  but it has not been confirmed against a TiKV-backed cluster.
- Linux aarch64 (6.1.166 / Amazon Linux 2023), client `pymysql` over a Unix socket.
- `character_set_connection` utf8mb4, `collation_connection` **utf8mb4_0900_bin** (pinned
  client-side; TiDB's default `utf8mb4_bin` is PAD SPACE).
- `sql_mode`: **irrelevant** — reproduces under the fuzzer's
  `STRICT_ALL_TABLES,NO_ZERO_DATE,NO_ZERO_IN_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION,ONLY_FULL_GROUP_BY,NO_BACKSLASH_ESCAPES`,
  under `ONLY_FULL_GROUP_BY` alone, and under `sql_mode=''`.
- Buggy code is **still present on upstream `pingcap/tidb` master** (verified against
  `logical_union_all.go` and `fd_graph.go` via the GitHub contents API).

## Minimal repro

```sql
CREATE TABLE t0 (id BIGINT);
CREATE VIEW u AS SELECT * FROM t0 UNION ALL SELECT * FROM t0;

SELECT 1 FROM u AS t1 WHERE (t1.id < ALL (SELECT 1)) IN (SELECT t1.id);
```

No rows inserted; one column; the view can be an inline derived table instead.

## Expected vs actual

| Query | Expected | Actual |
|---|---|---|
| minimal `SELECT` above | 0 rows (`t0` is empty) | `ERROR 1105 (HY000): assignment to entry in nil map` |
| `EXPLAIN` of the same | a plan | same 1105 (fails in physical planning) |
| same query against `t0` directly, or a non-`UNION ALL` view | 0 rows | 0 rows ✓ |
| as-found finding query (`logs/tidb_run12/error_round38_0.sql`) | 0 rows (BASE returns 0 rows) | same 1105 |

## Equivalence construction

eqgen built the equivalent `t` as a 29-view chain over `t__base`: a `ROW_NUMBER`-windowed
pass-through view, then repeated **predicate-split partitioning** (each split emitting a
`pred` / `NOT pred` / `pred IS NULL` triple recombined with `UNION ALL`), nested four levels deep and
finally `UNION ALL`-ed into `t`.

**Load-bearing construct:** the `UNION ALL` — nothing else in the chain matters. The whole 29-view
tower collapses to a single two-branch `UNION ALL`, and it does not even need to be a view (an inline
derived table reproduces). The elaborate split predicates, the window round-trip, and the nesting all
reduced away.

It is a **construct × query-feature composition**: the `UNION ALL` relation alone is fine and the
query alone against the base table is fine. The query must supply an `IN (correlated subquery)` whose
LHS is an inequality-quantified `ALL` subquery.

**Position matters:** only the *outer* relation need be the `UNION ALL`. In the original 5-alias
query, making just `t1` the `UNION ALL` reproduces; making only `t6`, `t7`, `t8` or `t9` the
`UNION ALL` is clean.

## Minimal oracle exposure path

- **Object composition arity:** `2`.
- **GCL builder path:** `UnionEmptyRoundTripBuilder → CreateViewBuilder`.
- **Confidence:** Inferred from the minimized two-branch union. The as-found 29-view construction
  used repeated predicate-split unions, and no historical GCL AST was preserved; the current class is
  the exact match for the reduced `R UNION ALL (R WHERE FALSE)` shape.
- **Realization:** a root view over the two-branch `UNION ALL`.
- **Workload/data requirements (excluded from arity):** the correlated `IN`, quantified `ALL`
  expression, outer-relation position, and empty-table condition belong to the workload/data side and
  are not counted.
- **Exposure vs. intrinsic trigger:** `UNION ALL` is intrinsic; the root view only exposes it because
  an inline derived union reproduces. Thus “the tower reduced away” is consistent with retaining this
  two-factor oracle exposure path.

## Characterization

Every case below was run against the live server (matrix in `reduced.sql`).

**Triggers**
- outer relation is a `UNION ALL` (view or inline derived table; either branch may be `WHERE FALSE`).
- the `IN`'s LHS is `<`, `>` or `<=` quantified by `ALL`. The `ALL` subquery may be uncorrelated and
  table-free (`SELECT 1`).
- the `IN` subquery is **correlated** to the outer relation — `IN (SELECT t1.id)`, with no table at
  all, is enough.
- the predicate sits in `WHERE`.
- empty table; any `sql_mode`; fails at `EXPLAIN`.

**Does NOT trigger (controls)**
- `UNION` (distinct) instead of `UNION ALL` — clean. Distinct union gets an aggregation on top, whose
  `ExtractFD` goes through `BaseLogicalPlan.ExtractFD` and *does* `make` the map.
- plain view `SELECT * FROM t0`, or the base table directly — clean.
- `UNION ALL` of two constant `SELECT`s with no table scan — clean.
- `<> ALL` or `= ALL` — clean; `< ANY` — clean; a plain `(t1.id < 1)` LHS — clean.
  `ExtractEquivalenceCols` only reaches `RegisterUniqueID` when an equivalence operand is a
  `ScalarFunction` rather than a bare `Column`, which is why the quantified-comparison LHS is
  required.
- `= ANY (SELECT t1.id)` — clean, **despite being semantically identical to `IN`**; the panic is
  specific to the `IN` rewrite path.
- uncorrelated `IN (SELECT 1)`, or a value list `IN (1, 0)` — clean.
- `EXISTS (...)` instead of `IN` — clean.
- the same expression in the `SELECT` list instead of `WHERE` — clean.

### Root cause

`pkg/planner/core/operator/logicalop/logical_union_all.go:208`

```go
func (p *LogicalUnionAll) ExtractFD() *fd.FDSet {
	...
	res := &fd.FDSet{}          // <-- HashCodeToUniqueID left nil
```

Every other plan-node `FDSet` construction supplies the map — `logical_join.go:714`,
`logical_join.go:935`, `base_logical_plan.go:339`, `logical_datasource.go:402`,
`logical_apply.go:307` all use `&fd.FDSet{HashCodeToUniqueID: make(map[string]int)}`.
`LogicalUnionAll` is the lone exception.

`pkg/planner/funcdep/fd_graph.go:1216` writes it unguarded:

```go
func (s *FDSet) RegisterUniqueID(hashCode string, uniqueID int) {
	if len(hashCode) == 0 { ...; return }
	if _, ok := s.HashCodeToUniqueID[hashCode]; ok { ...; return }   // reads are nil-safe
	s.HashCodeToUniqueID[hashCode] = uniqueID                        // <-- panics if nil
}
```

`pkg/planner/core/operator/logicalop/logical_join.go` — `ExtractFDForInnerJoin` takes the left
child's `FDSet` as its own and writes to it before repairing the field:

```go
fds := leftFD
fds.MakeCartesianProduct(rightFD)                                  // nil + nil stays nil
...
equivUniqueIDs := util.ExtractEquivalenceCols(allConds, p.SCtx(), fds)   // :879  PANICS HERE
...
if fds.HashCodeToUniqueID == nil {                                 // :890  the guard, too late
	fds.HashCodeToUniqueID = rightFD.HashCodeToUniqueID
}
```

Either fix is one line: give `LogicalUnionAll.ExtractFD` the same `make(map[string]int)` as its five
siblings, and/or lazily initialise inside `RegisterUniqueID` (its sibling merge helpers at
`fd_graph.go:858` and `:929` already nil-check, so the invariant is clearly meant to hold).

### Full stack trace

Resolved Go stack, captured from the server error log. The stack is **not** visible by default: the
error text alone reaches the client, and the log line that carries the trace
(`conn.go:1327 "command dispatched failed"`) is logged at **WARN** while the eqgen cluster pins
`log.level = "error"`. Re-run with `level = "warn"` to obtain it. Untrimmed, from the build named
under **Environment**:

```
[WARN] command dispatched failed  [conn=2097158]
  [sql="SELECT 1 FROM u AS t1 WHERE (t1.id < ALL (SELECT 1)) IN (SELECT t1.id)"]
  [err="assignment to entry in nil map
github.com/pingcap/errors.Trace
	$GOPATH/pkg/mod/github.com/pingcap/errors@v0.11.5-0.20260508054701-306e305bcf41/juju_adaptor.go:15
github.com/pingcap/tidb/pkg/util.GetRecoverError
	pkg/util/util.go:296
github.com/pingcap/tidb/pkg/planner.optimizeNoCache.func1
	pkg/planner/optimize.go:226
runtime.gopanic
	runtime/panic.go:860
runtime.mapassign_faststr
	internal/runtime/maps/runtime_faststr.go:265
github.com/pingcap/tidb/pkg/planner/funcdep.(*FDSet).RegisterUniqueID
	pkg/planner/funcdep/fd_graph.go:1227
github.com/pingcap/tidb/pkg/planner/util.ExtractEquivalenceCols
	pkg/planner/util/funcdep_misc.go:117
github.com/pingcap/tidb/pkg/planner/core/operator/logicalop.(*LogicalJoin).ExtractFDForInnerJoin
	pkg/planner/core/operator/logicalop/logical_join.go:879
github.com/pingcap/tidb/pkg/planner/core/operator/logicalop.(*LogicalJoin).ExtractFD
	pkg/planner/core/operator/logicalop/logical_join.go:708
github.com/pingcap/tidb/pkg/planner/core/operator/logicalop.(*LogicalJoin).ExtractFDForSemiJoin
	pkg/planner/core/operator/logicalop/logical_join.go:845
github.com/pingcap/tidb/pkg/planner/core/operator/logicalop.(*LogicalJoin).ExtractFD
	pkg/planner/core/operator/logicalop/logical_join.go:712
github.com/pingcap/tidb/pkg/planner/core/operator/physicalop.GetMergeJoin
	pkg/planner/core/operator/physicalop/physical_merge_join.go:54
github.com/pingcap/tidb/pkg/planner/core.exhaustPhysicalPlans4LogicalJoin
	pkg/planner/core/exhaust_physical_plans.go:2192
github.com/pingcap/tidb/pkg/planner/core.exhaustPhysicalPlans
	pkg/planner/core/exhaust_physical_plans.go:71
github.com/pingcap/tidb/pkg/planner/core.findBestTask
	pkg/planner/core/find_best_task.go:663
github.com/pingcap/tidb/pkg/planner/core/operator/physicalop.FindBestTask
	pkg/planner/core/operator/physicalop/base_physical_plan.go:496
github.com/pingcap/tidb/pkg/planner/core.iteratePhysicalPlan4BaseLogical
	pkg/planner/core/find_best_task.go:343
github.com/pingcap/tidb/pkg/planner/core.enumeratePhysicalPlans4TaskHelper
	pkg/planner/core/find_best_task.go:177
github.com/pingcap/tidb/pkg/planner/core.enumeratePhysicalPlans4Task
	pkg/planner/core/find_best_task.go:126
github.com/pingcap/tidb/pkg/planner/core.findBestTask
	pkg/planner/core/find_best_task.go:717
github.com/pingcap/tidb/pkg/planner/core/operator/physicalop.FindBestTask
	pkg/planner/core/operator/physicalop/base_physical_plan.go:496
github.com/pingcap/tidb/pkg/planner/core.physicalOptimize
	pkg/planner/core/optimizer.go:1135
github.com/pingcap/tidb/pkg/planner/core.VolcanoOptimize
	pkg/planner/core/optimizer.go:387
github.com/pingcap/tidb/pkg/planner/core.doOptimize
	pkg/planner/core/optimizer.go:340
github.com/pingcap/tidb/pkg/planner/core.DoOptimize
	pkg/planner/core/optimizer.go:437
github.com/pingcap/tidb/pkg/planner.buildAndOptimizeLogicalPlanRound
	pkg/planner/optimize.go:568
github.com/pingcap/tidb/pkg/planner.optimize
	pkg/planner/optimize.go:773
github.com/pingcap/tidb/pkg/planner.optimizeNoCache
	pkg/planner/optimize.go:367
github.com/pingcap/tidb/pkg/planner.optimizeCache
	pkg/planner/optimize.go:220
github.com/pingcap/tidb/pkg/planner.Optimize
	pkg/planner/optimize.go:207
github.com/pingcap/tidb/pkg/executor.(*Compiler).Compile
	pkg/executor/compiler.go:107
github.com/pingcap/tidb/pkg/session.(*session).executeStmtImpl
	pkg/session/session.go:2563"]
```

(Absolute `tidb-src/` prefixes on the TiDB frames shortened to
repo-relative paths; the Go runtime frames' nix store prefix likewise. Nothing else altered.)

Note the double `ExtractFD` entry: the semi-join FD path (`:712` → `ExtractFDForSemiJoin` → `:845`)
recurses and lands in the *inner*-join FD path (`:708` → `ExtractFDForInnerJoin` → `:879`), which is
where the unguarded write happens.

## How it was found

eqgen's differential/metamorphic equivalence oracle (v3 Data Equivalence Generator), tidb dialect,
`tidb_run12` round 38. Base `t` and the rewritten equivalent `t` are row-identical (verified: 8 rows
each, empty symmetric difference in both directions), so the one-sided error on the equivalent side
is a genuine engine divergence — **admissibility passes**. The base side returns 0 rows; the
equivalent side raises 1105.

- seed `1772301789` (informational only — the generator is not seed-reproducible across processes)
- reduced repro: [`reduced.sql`](reduced.sql)
- original finding: hunt log

## Also seen in `tidb_run19` — 12 further findings, same defect

`logs/tidb_run19` produced 300 `error_*` findings; normalising the `EQUIVALENT error:` header line
collapsed them into three message classes, and **12** are this one (`1105 assignment to entry in nil
map`), across 11 distinct rounds. Same build as this report's (`3bea8196`, unistore, assertions off).

Evidence that they are this defect and not a look-alike:

- **12/12 have a `UNION ALL` in the equivalence chain.** That is required by the root cause — the nil
  map is constructed in `LogicalUnionAll.ExtractFD()` — so its universal presence is a strong
  structural match rather than a coincidence of the message.
- **12/12 reproduce when the entire multi-link chain is replaced by one minimal `UNION ALL` view**
  (`CREATE VIEW t AS SELECT * FROM b UNION ALL SELECT * FROM b WHERE 0`, row-preserving, verified
  row-identical to a plain table), keeping each finding's own workload query.
- **The `UNION ALL` is load-bearing**: swapping it for `UNION` (distinct) is clean, and a plain table
  and a split-rejoin `LEFT OUTER JOIN` relation are both clean for the same queries.
- An inline derived table works as well as a view, so this is not view-specific.

### One discrepancy, and why it does not change the conclusion

The `mysql.opt_rule_blacklist` fingerprints differ between this report's reduced repro and the run19
findings:

| blacklisted rule | this report's repro | run19 findings (6 sampled, on the minimal `UNION ALL` view) |
|---|---|---|
| *(none)* | `nilmap` | `nilmap` |
| `predicate_push_down` | clean | clean |
| `decorrelate` | **clean** | **`nilmap`** |
| `column_prune` | `nilmap` | `nilmap` |
| `aggregation_push_down` | `nilmap` | `nilmap` |

That is a **query-level** difference, not a different code site: this report's repro uses a single
minimal correlated shape (`(t1.id < ALL (SELECT 1)) IN (SELECT t1.id)`) for which removing
decorrelation also avoids the path, while the run19 workload queries reach the same `ExtractFD` call
through shapes that survive `decorrelate` removal. The relation-side requirement (`UNION ALL`, with
`UNION` distinct clean) is identical in both, which is the part tied to the faulting function.

**Caveat:** I did not confirm the faulting site directly for the run19 findings. The 1105 is a
recovered panic and at the server's default `error` log level no stack is written, so the attribution
rests on the structural argument above plus the shared message — not on a backtrace.

Practical consequence: no new issue for these 12; they raise this defect's observed frequency and give
11 more query shapes that reach it.
