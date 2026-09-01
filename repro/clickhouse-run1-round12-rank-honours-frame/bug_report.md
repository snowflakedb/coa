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

# ClickHouse: `RANK()` / `DENSE_RANK()` with an explicit `ROWS` frame silently degenerate into `ROW_NUMBER()`, giving ORDER BY peers distinct ranks

## Summary

`WindowTransform::arePeers` treats *every* pair of distinct rows as non-peers when the window frame
type is `ROWS`. `RANK()` returns `peer_group_start_row_number` and `DENSE_RANK()` returns
`peer_group_number`, so under any `ROWS` frame both collapse into `ROW_NUMBER()` — tied rows receive
distinct ranks. Per SQL:2011 a ranking function's value is determined by the ORDER BY peer group and
is frame-independent, and DuckDB 1.5.5 / 2.0-alpha both accept the same frame and correctly ignore
it. Beyond the wrong value, the consequence is that peer ranks are then decided by physical arrival
order, which makes any query using a framed `RANK` **plan-shape dependent** — that is how the eqgen
equivalence oracle surfaced it, as a mismatch between two row-identical relations. Three rows and no
tables are enough to show the wrong answer without reference to plan shape.

## Environment

Reproduces on **every build tested**, from the 25.3 LTS line through current master — this is
long-standing shipped behaviour, not a master regression. `SELECT version()`, all Linux aarch64
(6.1.166 / Amazon Linux 2023), official static builds:

| build | channel | `RANK` framed | `DENSE_RANK` framed | `RANK` unframed |
|---|---|---|---|---|
| 25.3.6.56 | LTS | `[1,2,3]` ✗ | `[1,2,3]` ✗ | `[1,1,3]` ✓ |
| 26.3.17.56 | LTS | `[1,2,3]` ✗ | `[1,2,3]` ✗ | `[1,1,3]` ✓ |
| 26.7.1.1315 | stable (current, 2026-07-22) | `[1,2,3]` ✗ | `[1,2,3]` ✗ | `[1,1,3]` ✓ |
| 26.8.1.440 | master nightly | `[1,2,3]` ✗ | `[1,2,3]` ✗ | `[1,1,3]` ✓ |

(correct: `RANK` `[1,1,3]`, `DENSE_RANK` `[1,1,2]`)

**The full differential finding — not just the 3-row wrong answer — reproduces on
26.7.1.1315-stable run as `clickhouse server`**, byte-identically to the master nightly it was found
on: base `t` and equivalent `t` are row-identical (8 rows), and the mismatch is the same two rows,
`only in BASE (…,15,1) (…,16,0)` vs `only in EQUIVALENT (…,15,0) (…,16,1)`. The decisive cut holds
there too (removing only the frame makes the sides agree), as does the rank-collapse count
(56 distinct ranks framed vs 49 unframed). So the bug is reachable through the normal server query
path on a current release, not merely through `clickhouse local` or a nightly.

- The finding was originally produced against **26.8.1.440**, fetched by the eqgen clickhouse dialect
  from `builds.clickhouse.com/master/<arch>/clickhouse` — i.e. a **master nightly**, not a release,
  and with no upstream commit hash recorded. The release rows above were added specifically to
  establish that the bug is not master-only; those binaries come from
  `packages.clickhouse.com/tgz/{stable,lts}/clickhouse-common-static-<version>-arm64.tgz`.
- Assertions off (official builds). 26.8.1.440 and 26.7.1.1315 were exercised as `clickhouse server`
  over HTTP; the 25.3.6.56 and 26.3.17.56 rows are the 3-row minimal case via `clickhouse local`
  (result values only — no error-reporting reliance).
- Session settings (the fuzzer's, from the finding header): `join_use_nulls=1`, `max_threads=1`,
  `default_table_engine=MergeTree`, `create_table_empty_primary_key_by_default=1`,
  `database_atomic_wait_for_drop_and_detach_synchronously=1`, `max_execution_time=60`. **None is
  required** — the minimal repro needs no settings at all (no `sql_mode`/collation analogue applies).
- Compared against **DuckDB 2.0.0-alpha36155** (`76361ce4fb`) and **DuckDB 1.5.5** (`d8cdaa33fd`).

## Is this expected behaviour? No — ClickHouse's own documentation says otherwise

Worth addressing head-on, since "ranking functions and frames" is a corner where engines do differ.

**1. What the frame means.** A window frame selects the subset of the partition a window function
computes over. The `ROWS`/`RANGE` distinction is precisely about ties: under `RANGE`, `CURRENT ROW`
as a boundary means the current row *and all its peers*; under `ROWS` it means literally that one
row. So `arePeers` returning false for `ROWS` is **correct for frame-boundary computation** — that
part is standard. The defect is that `RANK`/`DENSE_RANK` reuse that frame-specific notion of "peer"
as their *ranking* peer group, which is a different thing.

**2. The frame in the repro is the whole partition.** This is the argument that closes the
"expected behaviour" reading entirely. The frame is `ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED
FOLLOWING` — every row of the partition. Even granting the most generous "frame-aware ranking"
interpretation, peer groups computed over that frame are identical to peer groups over the
partition, so `RANK` must still be `[1,1,3]`. There is **no** reading of the frame under which
`[1,2,3]` is a defensible answer. The same holds for `ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT
ROW`: `RANK` is defined by rows that *precede* the current row, which that frame contains in full.

**3. ClickHouse documents the opposite.** `docs/sql-reference/window-functions/index.md` contains a
worked example whose comment reads:

```
-- row_number does not respect the frame, so rn_1 = rn_2 = rn_3 != rn_4
```

demonstrated with `w2 AS (PARTITION BY part_key ORDER BY order DESC ROWS BETWEEN 1 PRECEDING AND
CURRENT ROW)` — a `ROWS` frame — where `row_number() OVER w2` equals the unframed `row_number() OVER
w1`. Frame-independence of ranking functions is therefore a **documented ClickHouse invariant**, and
`rank`/`dense_rank` violate it. (The doc's example uses distinct `order` values 1…5, so it has no
ties and could never have exposed `rank`/`dense_rank`.) The same page also states "ClickHouse
supports the standard SQL grammar for windows and window functions" and gives the default frame as
`RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW` — which is why the unframed case is correct.

**4. Internally inconsistent.** `percent_rank`, `cume_dist` and `ntile` read the *same* peer
counters and **reject** a `ROWS` frame outright; `rank`/`dense_rank` silently change meaning.

**5. Other engines.** DuckDB 1.5.5 and 2.0-alpha accept the identical frame and correctly ignore it.
In the SQL standard a ranking function's value is fixed by the ORDER BY peer group with no frame
term at all (the standard does not permit a frame clause on ranking functions; permissive engines
accept and ignore one).

## Minimal repro

```sql
-- Expected [1,1,3] (the two 'a' rows are ORDER BY peers); actual [1,2,3]
SELECT groupArray(r) FROM (
  SELECT RANK() OVER (ORDER BY v ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS r
  FROM (SELECT arrayJoin(['a','a','b']) AS v));

-- Expected [1,1,2]; actual [1,2,3]
SELECT groupArray(r) FROM (
  SELECT DENSE_RANK() OVER (ORDER BY v ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS r
  FROM (SELECT arrayJoin(['a','a','b']) AS v));
```

Deleting the frame clause from either query yields the correct `[1,1,3]` / `[1,1,2]`.

## Expected vs actual

| Query | Expected | Actual |
|---|---|---|
| `RANK() OVER (ORDER BY v ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)` on `['a','a','b']` | `[1,1,3]` | **`[1,2,3]`** |
| `DENSE_RANK()` same frame | `[1,1,2]` | **`[1,2,3]`** |
| same, any other `ROWS` frame (`UNB..CURRENT`, `1 PRECEDING..CURRENT`) | `[1,1,3]` / `[1,1,2]` | **`[1,2,3]`** |
| same, no frame or any `RANGE` frame | `[1,1,3]` / `[1,1,2]` | `[1,1,3]` / `[1,1,2]` ✓ |
| same framed query on DuckDB 1.5.5 and 2.0-alpha | `[1,1,3]` / `[1,1,2]` | `[1,1,3]` / `[1,1,2]` ✓ |
| finding's query, BASE vs EQUIVALENT (row-identical relations) | identical multisets | differ in 2 rows: ranks 15/16 swap between `t3.id` 0 and 1 |
| finding's query with the frame clause deleted | identical multisets | identical ✓ |

## Equivalence construction

The equivalent `t` was built by a short chain: two plain CTAS copies
(`t__base_table_1`, `t__base_table_2`), a view adding and then dropping a throwaway
`eq_tmp_col_1` (`t__base_view_1` → `t__base_table_3`), and finally a **`FIRST_VALUE` window
round-trip** — `CREATE TABLE t AS SELECT FIRST_VALUE(id) OVER (PARTITION BY id ORDER BY id), …`.

**Load-bearing construct: the `FIRST_VALUE` window round-trip — but purely for its side effect on
row order, not for any semantic interaction.** Measured, all with identical 8-row content:

| construction | active parts | scan order of `name` (no `ORDER BY`) | mismatch? |
|---|---|---|---|
| BASE (8 separate `INSERT`s) | 3 | `['a','','dup','dup',NULL,'zzz','b','é']` | — (reference) |
| trivial CTAS copy | 1 | `['a','','dup','dup',NULL,'zzz','b','é']` | **no** |
| only the `FIRST_VALUE` round-trip | 1 | `['','a','b','dup','dup','zzz','é',NULL]` | **YES** |
| full 4-step chain (as found) | 1 | `['','a','b','dup','dup','zzz','é',NULL]` | **YES** |

The two CTAS copies and the add-then-drop-a-dummy-column view are inert. What matters is that
`FIRST_VALUE(name) OVER (PARTITION BY name ORDER BY name)` forces a **sort**, so the materialized
table's physical row order becomes name-sorted instead of insertion-ordered. Note the trivial CTAS
copy also collapses 3 parts into 1 yet does **not** reproduce — so the trigger is row order within
the data, not part count or part structure.

The interacting pair is therefore **query feature × physical row order**, not construct × query
feature: `RANK() OVER (… ROWS BETWEEN …)` × any two orderings of the same rows. This is what makes it
a different class from the usual finding, where a rewrite construct trips an optimizer rule; here the
builder contributes no semantics at all, only a permutation.

## Minimal oracle exposure path

**Object composition arity:** `2`

**GCL builder path:** `WindowRewriteQueryBuilder` → `CreateTableBuilder`

**Confidence:** verified

**Realization:** A window-rewritten `TABLE` materializes the same rows in a different physical order.

**Workload/data requirements (excluded from arity):**
- `RANK()` or `DENSE_RANK()` with an explicit `ROWS` frame.
- At least one tie in the workload window's `ORDER BY` keys.
- Two physical arrival orders that reverse members of a peer group for oracle exposure.

**Exposure vs. intrinsic trigger:** Neither builder is part of the intrinsic standalone wrong answer, which needs no table at all; the builder path is load-bearing only for differential exposure because its `FIRST_VALUE` rewrite and table materialization permute row order. Thus “the builder contributes no semantics” means it does not alter the row multiset or ranking rule, not that it was irrelevant to producing the contrasting oracle path.

## Characterization

**Triggers**
- `RANK()` or `DENSE_RANK()` with **any explicit `ROWS` frame** — `UNBOUNDED PRECEDING AND UNBOUNDED
  FOLLOWING`, `UNBOUNDED PRECEDING AND CURRENT ROW`, and `1 PRECEDING AND CURRENT ROW` all reproduce.
- Requires at least one ORDER BY tie (peer group of size > 1). Without ties the degenerate and correct
  answers coincide.

**Does NOT trigger (controls)**
- No frame clause — correct. (ClickHouse's default frame is `RANGE UNBOUNDED PRECEDING AND CURRENT
  ROW`, which takes the ORDER BY comparison path.)
- Any `RANGE` frame, including `RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING` — correct.
- `ROW_NUMBER()` — unaffected under every frame; it reads `current_row_number`, not a peer counter.
- `PERCENT_RANK()`, `CUME_DIST()`, `NTILE()` — these read the *same* peer counters but **reject** a
  `ROWS` frame outright (`BAD_ARGUMENTS: Unsupported window frame type for function '…'`), so they
  are protected by validation rather than wrong.
- A framed window *aggregate* (`COUNT(*) OVER (… ROWS BETWEEN …)`) in the finding's query — both sides
  agree; frame-honouring is correct for aggregates.

**Measured matrix** (on `['a','a','b']`, `ORDER BY v`; ✓ correct, ✗ wrong):

| frame | RANK | DENSE_RANK | PERCENT_RANK | CUME_DIST | ROW_NUMBER |
|---|---|---|---|---|---|
| (no frame) | `[1,1,3]` ✓ | `[1,1,2]` ✓ | `[0,0,1]` ✓ | REJECTED | `[1,2,3]` ✓ |
| `RANGE UNB..CURRENT` | `[1,1,3]` ✓ | `[1,1,2]` ✓ | REJECTED | REJECTED | `[1,2,3]` ✓ |
| `RANGE UNB..UNB` | `[1,1,3]` ✓ | `[1,1,2]` ✓ | `[0,0,1]` ✓ | REJECTED | `[1,2,3]` ✓ |
| `ROWS UNB..UNB` | **`[1,2,3]` ✗** | **`[1,2,3]` ✗** | REJECTED | REJECTED | `[1,2,3]` ✓ |
| `ROWS UNB..CURRENT` | **`[1,2,3]` ✗** | **`[1,2,3]` ✗** | REJECTED | REJECTED | `[1,2,3]` ✓ |
| `ROWS 1 PRECEDING..CURRENT` | **`[1,2,3]` ✗** | **`[1,2,3]` ✗** | REJECTED | REJECTED | `[1,2,3]` ✓ |

### Root cause (source, upstream master)

`src/Processors/Transforms/WindowTransform.cpp:790`

```cpp
bool WindowTransform::arePeers(const RowNumber & x, const RowNumber & y) const
{
    if (x == y) { return true; }                                  // a row is its own peer

    if (window_description.frame.type == WindowFrame::FrameType::ROWS)
    {
        // For ROWS frame, row is only peers with itself (checked above);
        return false;                                             // <-- every row its own peer group
    }

    // For RANGE and GROUPS frames, rows that compare equal w/ORDER BY are peers.
    ...
}
```

Peer-group *identity* is conflated with frame *type*. That is defensible for frame-extent purposes —
a `ROWS` frame counts rows, so peer grouping does not affect its boundaries — but the same counters
are the return values of the ranking functions:

- `WindowFunctionRank::windowInsertResultInto` pushes `transform->peer_group_start_row_number` (`:1635`)
- `WindowFunctionDenseRank::windowInsertResultInto` pushes `transform->peer_group_number` (`:1653`)

and the counters are advanced only when `arePeers` says the peer group changed (`:1312-1316`). With a
`ROWS` frame that is every row, so `RANK` → row number and `DENSE_RANK` → row number.

**The fix is already modelled elsewhere in the same file.** Three other peer-group-dependent
functions implement `checkWindowFrameType` and refuse a frame they cannot honour —
`WindowFunctionNtile` (`:2165`), `WindowFunctionPercentRank` (`:2305`), `WindowFunctionCumeDist`
(`:2419`). `WindowFunctionRank` and `WindowFunctionDenseRank` declare **no** override, so they
inherit the permissive default and silently return wrong values. Either give them the same
validation, or make `arePeers` compute peer identity from the ORDER BY columns irrespective of frame
type (which is what the other engines do, and what lets them accept-and-ignore the frame).

## How it was found

eqgen's differential/metamorphic equivalence oracle (v3 Data Equivalence Generator), clickhouse
dialect, `clickhouse_run1` round 12. Base `t` and the rewritten equivalent `t` are row-identical
(verified with grouped multiset comparison in both directions: 8 rows each, identical
`(id, name, created_at, count)` listings) — **admissibility passes**.

The mismatch is two rows: ranks 15 and 16 swap between `t3.id = 0` and `t3.id = 1`. Those two groups
are ORDER BY **peers** — inspected directly, both have `t3.name = 'dup'`, `t2.name = NULL`,
`t1.created_at = NULL`, and `t3.id` is not among the window's ORDER BY keys — so under correct `RANK`
semantics both must receive the *same* rank and the mismatch could not arise.

A blunter way to see the degeneration in this query: it returns 56 rows, and framed it assigns **56
distinct ranks** (1…56, all unique) despite many rows sharing `t3.name`/`t2.name`. With the frame
removed the same 56 rows get **49 distinct ranks** — the 7 collapsed values are exactly the peer
groups that `arePeers` refused to form.

Worth recording for anyone re-triaging this class: the BASE side alone is **stable** — identical over
6 repeated runs and across `max_threads` 1/2/4/8/16 — so a naive determinism check on one side does
not reveal the problem. What isolates it is the decisive cut: deleting *only* the frame clause makes
the two sides agree (confirmed for both `RANK` and `DENSE_RANK`).

**Admissibility note.** Had the query used `ROW_NUMBER()` instead, it would have been genuinely
inadmissible — `ROW_NUMBER` over a tie-incomplete ORDER BY is legitimately non-deterministic, and the
`ROW_NUMBER` variant of this query does diverge between the two sides for that innocent reason. With `RANK` the query *is*
deterministic, which is what makes this a real wrong answer rather than an oracle defect.

- seed `1690889973` (informational only — the generator is not seed-reproducible across processes)
- reduced repro: [`reduced.sql`](reduced.sql)
- original finding: hunt log
