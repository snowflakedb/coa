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

# DuckDB: SIGSEGV in `JoinHashTable::ScanStructure::NextRightSemiOrAntiJoin` — `RIGHT_SEMI` hash join over a window-aggregate view dereferences a non-pointer from the build-side row-pointer array

## Summary

`<bool expr> IN (<subquery>)` where the subquery is a self-join of a view whose projection is a
window aggregate (`MAX(f) OVER (PARTITION BY s)`) segfaults DuckDB `v2.0.0-alpha36551`. The window
view makes the planner overestimate the subquery side by ~2 orders of magnitude (354,375 estimated
vs 6,780 actual), so the `build_side_probe_side` optimizer flips the `SEMI` join into `RIGHT_SEMI`
— build side = the subquery, probe side = the 3-row outer table. Execution then faults inside
`NextRightSemiOrAntiJoin` at `Load<bool>(ptr + ht.tuple_size)`: the row-pointer array it reads is
not holding row pointers but a uniformly repeated non-pointer word (`0xbf58476d1ce4e5b9`), which it
dereferences.

The build side has exactly **one** distinct join key value, which puts it on the small-build-side
*dictionary emission* path added by [PR #22340](https://github.com/duckdb/duckdb/pull/22340)
(merged 2026-04-30) — the same PR that rewrote the faulting function into
`MarkChainsAsFoundLoop<ht.use_dict_emission>`. Widening the build key to many distinct values makes
the crash go away. DuckDB 1.5.0 runs the same repro correctly, so this looks like a v2.0 regression.

Not a race: it reproduces with `SET threads = 1`, deterministically (8/8 runs).

## Status — FILED AND FIXED UPSTREAM (verified 2026-08-06)

| | |
|---|---|
| Upstream issue | [duckdb/duckdb#24485](https://github.com/duckdb/duckdb/issues/24485) — filed 2026-08-04 from this finding, closed 2026-08-06 |
| Fix | [PR #24539](https://github.com/duckdb/duckdb/pull/24539) "Terminate the dead end chain when the build side uses dictionary emission", merged 2026-08-06 14:57 UTC, merge commit `76dd1e7d` |
| Verified fixed | built `main` at `76dd1e7d` from source (`v1.6.0-dev12322`): minimal repro returns `2`; the full `duckdb_run34/crash_round2175` chain runs clean and its result multiset is identical to base's |

**Upstream's root cause is a refinement of the guess above.** `BuildDictionaryArrays` repurposes a
row's chain field: instead of a pointer to the next row it holds a `uint32` index into
`aux_next_ptrs`. Separately, `MarkChainsAsFoundLoop` parks a probe pointer on the zeroed `dead_end`
row once a chain is fully marked, relying on that zero reading back as a **null pointer** to end the
scan. Under dictionary emission the same zero reads back as **index 0**, which is a live row
(`aux_next_ptrs` has exactly `build_count` entries, all live) — so the chain never terminates and the
scan walks off into a real row's payload. That is the `0xbf58476d1ce4e5b9` non-pointer word observed
below. Upstream reproduced it under ASan as `SEGV join_hashtable.cpp:1942`.

**Second occurrence, same bug:** `duckdb_run34/crash_round2175` (seed 1973449618, engine
`v2.0.0-alpha36916` / `033322be14`). Different equivalence chain (18 links, incl. `ATTACH` mirrors,
`SEMI JOIN`, `union_value`/`union_extract`, split/rejoin) and a different workload query, but the
same four ingredients: `<bool expr> = ANY (subquery)`, the relation referenced 3× in the subquery
with `CROSS JOIN` + an equality `FULL OUTER JOIN`, a window aggregate, and a tiny distinct-key build
side. Identical stack, identical mask (`disabled_optimizers='build_side_probe_side'`), crashes at
`threads=1`, result multiset identical to base under the mask. Triaged 2026-08-06 as a duplicate of
#24485 — not re-filed.

**Beware build staleness when re-checking this.** It still segfaults on the newest *published*
nightly, `v2.0.0-alpha36998` (`ff4fd138db`), because that artifact is **18 commits behind** the fix
merge (`gh api repos/duckdb/duckdb/compare/76dd1e7d...ff4fd138db` → `status: behind`). There are no
per-commit artifacts (`artifacts.duckdb.org/<sha>/…` → 404), so verifying a same-day fix requires a
source build. Refresh `duckdb` before treating a recurrence as a new bug.

## Environment

| | |
|---|---|
| Engine | DuckDB `v2.0.0-alpha36551` (Cyanoptera), commit `3958a013ed` (2026-08-03) |
| Binary | prebuilt nightly CLI (`duckdb`), **assertions OFF** |
| Platform | Linux aarch64 (6.12.92, AL2023) |
| Session | all defaults; also crashes with `SET threads = 1` |
| Clean on | DuckDB **1.5.0** (Python wheel) — returns the correct answer |
| Signal | `SIGSEGV` (core dumped). The eqgen harness recorded `SIGABRT` for the same input; the CLI run dies with `SIGSEGV` and no stderr output. |

## Minimal repro

```sql
CREATE TABLE big (f BOOLEAN, s VARCHAR);
INSERT INTO big SELECT (i % 3) = 0, ['a', '', 'dup', 'zzz'][(i % 4) + 1]
  FROM generate_series(1, 30) t(i);

CREATE VIEW v AS SELECT MAX(f) OVER (PARTITION BY s) AS f, s FROM big;

CREATE TABLE p (pb BOOLEAN);
INSERT INTO p VALUES (TRUE), (TRUE), (NULL);

SELECT COUNT(*) FROM p
WHERE p.pb IN (SELECT x.f FROM v x JOIN v y ON x.s = y.s CROSS JOIN v z);
```

Cardinalities: `v` = 30 rows / 1 distinct `f` (`true`); the subquery = 6,780 rows / 1 distinct key;
`p` = 3 rows (2 matching + 1 `NULL`).

## Expected vs actual

| Query | Expected | Actual |
|---|---|---|
| Minimal repro (above) | `2` | **SIGSEGV** (process dies, exit 139) |
| Minimal repro + `SET disabled_optimizers='build_side_probe_side'` | `2` | `2` |
| Minimal repro on DuckDB 1.5.0 | `2` | `2` |
| Original workload query on base `t` | `(0, 7, 7, 7, 7, 7, 7.77)` | `(0, 7, 7, 7, 7, 7, 7.77)` |
| Original workload query on the row-identical equivalent `t` | `(0, 7, 7, 7, 7, 7, 7.77)` | **SIGSEGV** |

The equivalent relation returns exactly the base result once the `RIGHT_SEMI` flip is disabled, so
this is a pure crash, not a wrong-result bug.

## Equivalence construction

### (1) The construct as the eqgen builder emits it

The finding's equivalent `t` is an 11-link chain of row-preserving builders over an 8-row base
table (predicate-tautology view → CTAS + column rename round-trip → `POSITIONAL JOIN` split/rejoin
→ add/drop-column round-trip → `FULL OUTER JOIN` split/rejoin on a `ROW_NUMBER` key → cross-schema
CTAS → *row-multiply then collapse*). The last link is the load-bearing one — verbatim from
`logs/duckdb_run21/crash_round7.sql`:

```sql
CREATE TABLE t__base_table_3 AS
  SELECT c_int, …, c_ts, ROW_NUMBER() OVER (ORDER BY c_int) AS eq_key_1 FROM t__base_view_9;

-- multiply every row 101x, then 101x again  (8 -> 81,608 rows)
CREATE TABLE t__base_table_4 AS
  SELECT c_int, …, eq_key_1 FROM t__base_table_3
  UNION ALL
  SELECT c_int, …, eq_key_1 FROM t__base_table_3 CROSS JOIN generate_series(1, 100);
INSERT INTO t__base_table_4
  SELECT c_int, …, eq_key_1 FROM (SELECT c_int, …, eq_key_1 FROM t__base_table_4)
  CROSS JOIN generate_series(1, 100);

-- collapse back to one row per key with a window aggregate + DISTINCT  (-> the original 8 rows)
CREATE VIEW t AS
  SELECT c_int, …, c_ts FROM (
    SELECT DISTINCT eq_key_1,
           MAX(c_int)  OVER (PARTITION BY eq_key_1) AS c_int,
           MAX(c_big)  OVER (PARTITION BY eq_key_1) AS c_big,
           …
           MAX(c_ts)   OVER (PARTITION BY eq_key_1) AS c_ts
    FROM t__base_table_4);
```

and the workload query — the `IN`-subquery references `t` three times, which is what gets CSE'd
into `__common_subplan_1`:

```sql
SELECT MOD(t1.c_int, 1), SUM(trunc(t1.c_big)), t1.c_int, …, MIN(t1.c_dec)
FROM t AS t1
WHERE (t1.c_int IN (t1.c_int)) IN (
        SELECT t2.c_flag FROM t AS t2
        LEFT OUTER JOIN t AS t3 ON t2.c_txt = t3.c_txt
        INNER JOIN     t AS t4 ON t3.c_ts >= t4.c_date
        WHERE coalesce(t4.c_flag, t4.c_flag)
        ORDER BY t2.c_flag NULLS FIRST, 1)
GROUP BY t1.c_int
HAVING MIN(t1.c_big) = t1.c_int
QUALIFY COUNT(*) OVER (…) < (t1.c_int & t1.c_int);
```

`reduced.sql` PART 1 is this construction with the chain collapsed to that last link and the real
workload query kept intact — it still SIGSEGVs.

**Mapping onto the distilled repro:** `t__base_table_4` → `big` (a table with far more rows than
the view yields); the collapsing `DISTINCT` + `MAX() OVER (PARTITION BY eq_key_1)` view → `v`
(`MAX(f) OVER (PARTITION BY s)`); the 3-way self-join of `t` in the `IN` subquery →
`v x JOIN v y ON x.s = y.s CROSS JOIN v z`; the outer `t AS t1` with the boolean probe key
`(t1.c_int IN (t1.c_int))` (= `TRUE` for 7 rows, `NULL` for the row where `c_int IS NULL`) → the
table `p` holding `(TRUE), (TRUE), (NULL)`.

### (2) The load-bearing construct — a four-way composition

No single construct is sufficient. All four are required:

1. **construct** — a view projecting a **window aggregate with `PARTITION BY`**. `GROUP BY` (C2),
   `OVER ()` without a partition (C3), `SELECT DISTINCT` alone, `ROW_NUMBER() OVER ()`, and the
   same rows materialised into a plain table (C4) all run clean. The window operator is what
   inflates the estimate to 354,375.
2. **construct × query feature** — the view must be **referenced ≥3 times in the subquery, with
   one equality self-join and one cross join**, so the planner CSEs it into a single
   `PIPELINE_DEPENDENT` CTE and multiplies the estimate. Dropping the cross-join reference (C8), or
   dropping the equality (leaving cross-joins only), stops the crash.
3. **query feature** — `IN (<subquery>)`, so a `SEMI` join exists to be flipped. The `EXISTS`
   rewrite (C9) never builds one and is clean.
4. **data** — the build side must have **very few distinct key values** (C7: many distinct BIGINT
   keys is clean; the same shape with only 2 distinct BIGINT keys crashes), and the probe side must
   have **≥2 matching rows and ≥1 `NULL`** (C5: no `NULL` → clean; C6: 1 matching row + `NULL` →
   clean).

### (3) Constructs reduced away

The predicate-tautology view, the CTAS/rename round-trip, the `POSITIONAL JOIN` split-rejoin, the
add/drop-column round-trip, the `FULL OUTER JOIN` split-rejoin, the cross-schema CTAS, the
`UNION ALL` doubling, and the second 101× `INSERT` are all irrelevant — a single `CROSS JOIN
generate_series` is enough to get past the row threshold (~25 rows in `big`). On the query side the
`GROUP BY`/`HAVING`/`QUALIFY`/`ORDER BY`, the `MOD`/`SUM`/`MIN` projections, the `coalesce` in the
subquery's `WHERE`, and the `LEFT OUTER` join type (`INNER` crashes too) were all removed.

## Minimal oracle exposure path

- **Object composition arity:** 4.
- **GCL builder path:** `KeyExplodeExpansionBuilder` [`TABLE` realization] → `KeyWindowAggregateReduceBuilder` [`VIEW` realization].
- **Confidence:** Exact against the emitted expansion/window-collapse SQL and current GCL.
- **Realization:** the expander materializes repeated keyed rows; the reducer exposes one row per key through `DISTINCT` window aggregates in a view.
- **Workload/data requirements (excluded from arity):** the view is referenced at least three times in an `IN` subquery with one equality self-join and one cross join; the build has very few distinct keys, the probe has at least two matches plus one `NULL`, and cardinality estimates must flip the join to `RIGHT_SEMI`.

**Exposure vs. intrinsic trigger:** The object path supplies both the inflated estimate and residual partitioned-window view that steer the planner onto dictionary-emitting `RIGHT_SEMI`. The intrinsic crash is the resulting hash-chain termination defect; query multiplicity, key distribution, probe rows, and plan threshold are workload/data requirements rather than extra object factors.

## Characterization

### Decisive EXPLAIN plan diff — buggy vs control C1

Buggy (default). Build side is the 354,375-estimated subquery, probe side is the 3-row table:

```
╭─ Hash Join ────────┴────────────────────╮
│ Join Type: RIGHT_SEMI                   │
│ Conditions: #0 = pb                     │
╰────────────────────┬────────────────────╯
                     ├───────────────────────────────────╮
╭─ Projection ───────┴────────────────────╮  ╭─ Seq Scan ┴───────────╮
│ Projections: f                          │  │ Table: mm.main.p      │
│ ~354,375 rows                           │  │ Projections: pb       │
╰────────────────────┬────────────────────╯  │ ~3 rows               │
╭─ Hash Join: INNER  ┴  #1 IS NOT DISTINCT FROM s  ~354,375 rows      ╯
╭─ CTE: __common_subplan_1  (Execution Mode: PIPELINE_DEPENDENT)
```

Control C1, `SET disabled_optimizers='build_side_probe_side'` — the *only* frame that changes is
the join type and the side assignment, and it returns `2`:

```
╭─ Hash Join ─┴────────────╮
│ Join Type: SEMI          │
│ Conditions: pb = #0      │
╰─────────────┬────────────╯
              ├───────────────────────────────────╮
╭─ Seq Scan ──┴────────────╮  ╭─ Projection ──────┴───────────────────╮
│ Table: mm.main.p         │  │ Projections: f                        │
│ ~3 rows                  │  │ ~354,375 rows                         │
```

(The estimate is ~52× the actual 6,780 rows in the minimal repro. In the original finding the CTE is
estimated at 7,456,479 rows against **19** actual rows with 2 distinct keys — a ~390,000×
overestimate.)

Two optimizer switches avoid the crash, and both do so by removing the `RIGHT_SEMI` join, which is
the plan-level discriminator:

| `disabled_optimizers` | top join type | result |
|---|---|---|
| *(none — default)* | `RIGHT_SEMI` | **SIGSEGV** |
| `build_side_probe_side` | `SEMI` | `2` |
| `filter_pushdown` | `MARK` | `2` |

`filter_pushdown` is the pass that converts the `IN` `MARK` join into a `SEMI` join, so disabling it
means there is no `SEMI` join for `build_side_probe_side` to flip. `build_side_probe_side` is the
narrower switch — it keeps the `SEMI` join and only suppresses the side swap, which is why it
isolates the trigger more precisely.

### What triggers it / what does not

| Variant | Result |
|---|---|
| minimal repro | **SIGSEGV** |
| `disabled_optimizers='build_side_probe_side'` (C1) | clean, `2` — join stays `SEMI` |
| `disabled_optimizers='filter_pushdown'` (C11) | clean, `2` — join stays `MARK` |
| `threads = 1` (C10) | **SIGSEGV** — not a race |
| window → `GROUP BY` (C2) | clean, `2` |
| `OVER (PARTITION BY s)` → `OVER ()` (C3) | clean, `2` |
| `SELECT DISTINCT` view instead of the window | clean |
| `ROW_NUMBER() OVER ()` in the view | clean |
| view materialised to a plain table, same rows (C4) | clean, `2` |
| probe side without `NULL` (C5) | clean, `3` |
| probe side = 1 match + `NULL` (C6) | clean, `1` |
| build key = 30 distinct BIGINTs (C7) | clean, `2` |
| build key = 2 distinct BIGINTs | **SIGSEGV** |
| build key = 2 distinct VARCHARs | clean |
| drop the `CROSS JOIN v z` (C8) | clean, `2` |
| `IN` → `EXISTS` (C9) | clean, `2` |
| cross joins only, no equality self-join | clean |
| `big` ≤ 20 rows | clean |
| `big` ≥ 30 rows | **SIGSEGV** |
| DuckDB 1.5.0, same SQL | clean, `2` |

### Faulting instruction and memory state

```
=> 0x852710 <NextRightSemiOrAntiJoin+496>:  ldrb  w3, [x5, x1]
   0x85270c <NextRightSemiOrAntiJoin+492>:  ldr   x5, [x22, x2]
   0x852708 <NextRightSemiOrAntiJoin+488>:  ldr   x1, [x4, #344]

x1  = 0x2                    # ht.tuple_size — 2 bytes, matching the single-BOOLEAN build layout
x2  = 0x8                    # byte offset into the pointer array => element [1]
x5  = 0xbf58476d1ce4e5b9     # ptrs[1] — dereferenced as a row pointer; not a mapped address
x22 = 0xfffe4b04c000         # base of the pointer array
```

This is `Load<bool>(ptr + ht.tuple_size)` inside the mark-chains-as-found loop. The pointer array
holds one null followed by the same non-pointer word repeated:

```
0xfffe4b04c000: 0x0000000000000000  0xbf58476d1ce4e5b9
0xfffe4b04c010: 0xbf58476d1ce4e5b9  0xbf58476d1ce4e5b9
0xfffe4b04c020: 0xbf58476d1ce4e5b9  0xbf58476d1ce4e5b9
0xfffe4b04c030: 0xbf58476d1ce4e5b9  0xbf58476d1ce4e5b9
…                (uniform for the rest of the vector)
```

A single repeated word is what a *constant/dictionary* vector of a hash or key value looks like,
not what a flat vector of row pointers looks like — consistent with
`FlatVector::GetDataMutable<data_ptr_t>(pointers)` at the top of `NextRightSemiOrAntiJoin` being
applied to a vector that is not flat on the dictionary-emission path.

### Full stack trace (untrimmed)

Build: DuckDB `v2.0.0-alpha36551 (Cyanoptera) 3958a013ed`, prebuilt nightly CLI, **assertions
off**, Linux aarch64. Captured with `gdb --batch -ex bt` against the core dumped by the minimal
repro (PART 2 of `reduced.sql`), single-threaded is not required — this trace is from the default
run where the main thread happened to execute the pipeline:

```
Core was generated by `duckdb min.db'.
Program terminated with signal SIGSEGV, Segmentation fault.
#0  0x0000000000852710 in duckdb::JoinHashTable::ScanStructure::NextRightSemiOrAntiJoin(duckdb::DataChunk&, duckdb::DataChunk&) ()
#1  0x000000000085287c in duckdb::JoinHashTable::ScanStructure::Next(duckdb::DataChunk&, duckdb::DataChunk&, duckdb::DataChunk&) ()
#2  0x0000000001a8523c in duckdb::PhysicalHashJoin::ExecuteInternal(duckdb::ExecutionContext&, duckdb::DataChunk&, duckdb::DataChunk&, duckdb::GlobalOperatorState&, duckdb::OperatorState&) const ()
#3  0x0000000000855c5c in duckdb::CachingPhysicalOperator::Execute(duckdb::ExecutionContext&, duckdb::DataChunk&, duckdb::DataChunk&, duckdb::GlobalOperatorState&, duckdb::OperatorState&) const ()
#4  0x00000000009997f0 in duckdb::PipelineExecutor::Execute(duckdb::DataChunk&, duckdb::DataChunk&, unsigned long) ()
#5  0x0000000000999a7c in duckdb::PipelineExecutor::ExecutePushInternal(duckdb::DataChunk&, duckdb::ExecutionBudget&, unsigned long) ()
#6  0x0000000000999e44 in duckdb::PipelineExecutor::TryFlushCachingOperators(duckdb::ExecutionBudget&) ()
#7  0x000000000099ba38 in duckdb::PipelineExecutor::FlushAndFinalize(duckdb::ExecutionBudget&) ()
#8  0x000000000099be60 in duckdb::PipelineExecutor::Execute(unsigned long) ()
#9  0x000000000099c170 in duckdb::PipelineTask::ExecuteTask(duckdb::TaskExecutionMode) ()
#10 0x0000000000990d3c in duckdb::ExecutorTask::Execute(duckdb::TaskExecutionMode) ()
#11 0x00000000009a20b0 in duckdb::Executor::ExecuteTask(bool) ()
#12 0x00000000009092c4 in duckdb::ClientContext::ExecuteTaskInternal(duckdb::ClientContextLock&, duckdb::BaseQueryResult&, bool) ()
#13 0x000000000095faf8 in duckdb::PendingQueryResult::ExecuteInternal(duckdb::ClientContextLock&) ()
#14 0x000000000095fcf0 in duckdb::PendingQueryResult::Execute() ()
#15 0x0000000000973f10 in duckdb::ClientContext::Query(duckdb::unique_ptr<duckdb::SQLStatement, std::default_delete<duckdb::SQLStatement>, true>, duckdb::QueryParameters) ()
#16 0x0000000000974acc in duckdb::Connection::SendQuery(duckdb::unique_ptr<duckdb::SQLStatement, std::default_delete<duckdb::SQLStatement>, true>, duckdb::QueryParameters) ()
#17 0x0000000000475c40 in duckdb_shell::ShellState::ExecuteStatement(duckdb::unique_ptr<duckdb::SQLStatement, std::default_delete<duckdb::SQLStatement>, true>) ()
#18 0x0000000000476010 in duckdb_shell::ShellState::ExecuteSQL(std::__cxx11::basic_string<char, std::char_traits<char>, std::allocator<char> > const&) ()
#19 0x0000000000476394 in duckdb_shell::ShellState::RunOneSqlLine(duckdb_shell::InputMode, char*) ()
#20 0x0000000000481300 in duckdb_shell::ShellState::ProcessInput(duckdb_shell::InputMode) ()
#21 0x000000000048221c in RunShell(int, char const**) ()
#22 0x000000000044d98c in main ()
```

The original finding (PART 1) faults in the same top two frames on a scheduler worker thread
instead:

```
Program terminated with signal SIGSEGV, Segmentation fault.
#0  0x0000000000852710 in duckdb::JoinHashTable::ScanStructure::NextRightSemiOrAntiJoin(duckdb::DataChunk&, duckdb::DataChunk&) ()
#1  0x000000000085287c in duckdb::JoinHashTable::ScanStructure::Next(duckdb::DataChunk&, duckdb::DataChunk&, duckdb::DataChunk&) ()
#2  0x0000000001a8523c in duckdb::PhysicalHashJoin::ExecuteInternal(duckdb::ExecutionContext&, duckdb::DataChunk&, duckdb::DataChunk&, duckdb::GlobalOperatorState&, duckdb::OperatorState&) const ()
#3  0x0000000000855c5c in duckdb::CachingPhysicalOperator::Execute(duckdb::ExecutionContext&, duckdb::DataChunk&, duckdb::DataChunk&, duckdb::GlobalOperatorState&, duckdb::OperatorState&) const ()
#4  0x00000000009997f0 in duckdb::PipelineExecutor::Execute(duckdb::DataChunk&, duckdb::DataChunk&, unsigned long) ()
#5  0x0000000000999a7c in duckdb::PipelineExecutor::ExecutePushInternal(duckdb::DataChunk&, duckdb::ExecutionBudget&, unsigned long) ()
#6  0x000000000099bdec in duckdb::PipelineExecutor::Execute(unsigned long) ()
#7  0x000000000099c0f0 in duckdb::PipelineTask::ExecuteTask(duckdb::TaskExecutionMode) ()
#8  0x0000000000990d3c in duckdb::ExecutorTask::Execute(duckdb::TaskExecutionMode) ()
#9  0x00000000009a2580 in duckdb::TaskScheduler::TryDequeueAndProcessTask(duckdb::DBConfig const&, duckdb::TaskSchedulerQueue&, duckdb::shared_ptr<duckdb::Task, true>&) ()
#10 0x00000000009a2988 in duckdb::TaskScheduler::ExecuteForever(std::atomic<bool>*, duckdb::TaskSchedulerType) ()
#11 0x0000ffff96db38fc in execute_native_thread_routine () from /lib64/libstdc++.so.6
#12 0x0000ffff96ae20d8 in start_thread () from /lib64/libc.so.6
#13 0x0000ffff96b4c85c in thread_start () from /lib64/libc.so.6
```

Caveat on trace resolution: this is a release nightly, so there is no line information — frames
resolve to symbol names only, and inlined helpers (e.g. `MarkChainsAsFoundLoop`) are folded into
frame #0. The instruction/register evidence above is what pins the faulting statement.

### Suspected origin (lead, not a verified conclusion)

[PR #22340 "Add small-build-side dictionary emission for hash joins"](https://github.com/duckdb/duckdb/pull/22340)
(merged 2026-04-30, `3ff79ea07cf07f05ecd49766ac266a7934e43cd3`) rewrote exactly this function,
replacing the inline chain-marking loop with a `use_dict_emission`-templated helper:

```diff
 void ScanStructure::NextRightSemiOrAntiJoin(DataChunk &keys, DataChunk &probe_data) {
 	const auto ptrs = FlatVector::GetDataMutable<data_ptr_t>(pointers);
 	…
 		if (ht.non_equality_predicates.empty() && !ht.residual_predicate) {
-			for (idx_t i = 0; i < result_count; i++) {
-				const auto idx = chain_match_sel_vector.get_index(i);
-				auto &ptr = ptrs[idx];
-				if (Load<bool>(ptr + ht.tuple_size)) { …
+			const auto dead_end_ptr = ht.dead_end.get();
+			if (ht.use_dict_emission) {
+				MarkChainsAsFoundLoop<true>(ht, ptrs, chain_match_sel_vector, result_count, dead_end_ptr);
+			} else {
+				MarkChainsAsFoundLoop<false>(ht, ptrs, chain_match_sel_vector, result_count, dead_end_ptr);
+			}
```

Three things line up with that: the crash is at the `Load<bool>(ptr + ht.tuple_size)` in that loop;
the build side satisfies "small build side" (one distinct key value) and widening it to many
distinct values makes the crash disappear; and 1.5.0 — which predates the change — is clean.

## How it was found

The eqgen v3 data-equivalence oracle, `duckdb_run21` round 7, seed 221459190. The oracle **holds
the query fixed and swaps in a row-identical relation**: it built an 11-link chain of
row-preserving rewrites over an 8-row base table and confirmed base `t` ≡ equivalent `t`
(8 rows, byte-identical — the admissibility check passed, see `replay.py` output), then ran the
same generated workload query against both. The base table answered `(0, 7, 7, 7, 7, 7, 7.77)`; the
equivalent relation killed the engine.

The mechanism is specifically what a *data*-swapping oracle can reach and a *query*-rewriting one
cannot. TLP / NoREC / EET hold the data fixed and perturb the query — but here the crash needs the
relation to be a **window-aggregate view over a much larger table**, because that is the only thing
that produces the ~52–1,100× cardinality overestimate that flips `SEMI` → `RIGHT_SEMI`. A
query-rewrite oracle running against the original 8-row base table can never reach that plan; worse,
the rewrites those oracles apply (splitting a predicate into a partition triple, un-nesting the
subquery into a join, wrapping the query in an aggregate) *dismantle* the exact trigger — the
`IN`-subquery whose three references get CSE'd into one overestimated CTE. Note that the equivalence
builder's row-multiply-then-collapse link is not a fuzzer artifact either; it is a plain
`GROUP BY`-free deduplication idiom that appears in real ETL views.

Also worth flagging for the generator: the harness recorded this as `SIGABRT` while a direct CLI
run reports `SIGSEGV` with no stderr, so the harness's signal attribution for duckdb crashes is
not reliable — the signal in a finding header should not be trusted for dedup.

- Reduced repro and controls: [`reduced.sql`](reduced.sql)
- Original finding: hunt log
