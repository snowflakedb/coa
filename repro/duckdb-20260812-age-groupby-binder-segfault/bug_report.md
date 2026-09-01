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

# DuckDB: `Binder::BindSelectNode` segfaults on any two-argument `AGE(...)` call in `GROUP BY`

## Summary

`SELECT AGE(NULL, NULL) GROUP BY AGE(NULL, NULL);` — no table, no data, no other clause —
segfaults the DuckDB CLI. The crash is not specific to `NULL`, to a malformed string, or to
appearing in both the `SELECT` list and `GROUP BY`: any well-typed two-argument `AGE(x, y)` call
present in `GROUP BY` crashes the binder, whether or not it is also projected. This is a true
process death (`SIGSEGV`), not a caught `INTERNAL Error` — the engine never gets a chance to
report anything.

## Environment

- **DuckDB v2.0.0-alpha37464 (Cyanoptera)** `ea53ecdca1` — the `main`/CLI build fuzzed by eqgen,
  downloaded from `artifacts.duckdb.org/latest` at time of triage.
- Access path: CLI (`:memory:`), reproduced both via piped stdin and via `gdb --batch -ex run -ex
  bt --args ... -c "..."`. No `sql_mode`/charset/collation applicable.
- Build has no visible debug-symbol stripping for the binder's own call stack (see
  Characterization); `gdb` resolves real function names despite the release build.

## Minimal repro

```sql
SELECT AGE(NULL, NULL) GROUP BY AGE(NULL, NULL);
```

Full version with controls in `reduced.sql`.

## Expected vs actual

| Query | Expected | Actual |
|---|---|---|
| `SELECT AGE(NULL, NULL) GROUP BY AGE(NULL, NULL);` | 1 row, `NULL` | `Segmentation fault (core dumped)` |
| Same, `AGE(...)` only in `SELECT`, `GROUP BY` on an unrelated column (Control A) | 1 row | 1 row (correct) |
| Same, `AGE(...)` only in `GROUP BY`, not projected (Control B) | 1 row | `Segmentation fault` |
| Well-typed, non-`NULL` arguments: `AGE(DATE '2024-01-01', NULL::TIMESTAMP)` (Control C) | 1 row | `Segmentation fault` |
| Single-argument `AGE(x)` overload, same shape (Control D) | ambiguous-overload error | `Binder Error` (clean — different overload, no cast attempted) |
| Genuinely malformed string with an explicit, correctly-typed second arg (Control E) | conversion error | `Conversion Error` (clean — never reaches the crashing path) |

There is no "which side is wrong" question here — a crash is wrong regardless of what the query
should have returned. The engine should either return a row or raise a normal SQL error; it must
never terminate the process.

## Equivalence construction

Found via the same live eqgen campaign as the sibling findings, but **not object-shape-dependent**
— unlike bugs #2/#3, this crashes on a bare `SELECT` with no `FROM` clause at all, so it needed no
equivalence object, no base table, and no comparison to surface. The campaign's original finding
(`crash_round1021_0.sql`) carried the trigger inside a 40+ statement equivalence chain and a
workload query `SELECT VAR_POP(...), AGE('', NULL), AVG(RADIANS(...)) FROM t GROUP BY AGE('',
NULL) ORDER BY t.c_pk ASC` — none of that surrounding structure is load-bearing; the two-token
`AGE(NULL, NULL) GROUP BY AGE(NULL, NULL)` core reproduces identically standalone. (Two sibling
campaign findings, `crash_round1783_0.sql` and `crash_round1853_0.sql`, hit the identical crash
signature via `AGE(NULL, NULL)` and `AGE('>\', NULL)` respectively, inside their own differently-shaped
equivalence chains and workload queries — all three collapse to the same root cause.)

## Minimal oracle exposure path

- **Object composition arity:** 0 — no base/equivalent object contrast.
- **GCL builder path:** none; the equivalence chain is unrelated to the reduced crash.
- **Confidence:** Exact.
- **Realization:** none; the minimal exposure is a bare, no-`FROM` `SELECT`.
- **Workload/data requirements (excluded from arity):** a two-argument `AGE(...)` expression in `GROUP BY`; no table rows or data are required.

**Exposure vs. intrinsic trigger:** Eqgen happened to generate the expression inside a campaign workload, but no base/equivalent object contrast is part of the minimal exposure. The intrinsic trigger is binder handling of the grouped two-argument `AGE` call itself.

## Characterization

**Trigger:** any two-argument `AGE(x, y)` call — regardless of argument types, as long as the
call type-resolves to the `AGE(TIMESTAMP, TIMESTAMP)` (or `TIMESTAMPTZ` variant) overload —
appearing anywhere in a `GROUP BY` clause.

**Does NOT trigger it (controls, `reduced.sql`):**
- `AGE(...)` in the `SELECT` list only, with a different `GROUP BY` key (Control A).
- The single-argument `AGE(x)` overload in the identical shape (Control D) — overload resolution
  for that signature never reaches the crashing code path; it correctly reports an ambiguous-cast
  binder error instead.
- A genuinely invalid string argument *with an explicitly typed second argument* (Control E) —
  the conversion error fires before whatever mishandles `GROUP BY` gets involved.

**Mechanism:** `gdb --batch -ex run -ex bt` on the live crash resolves real function names despite
this being a release CLI build:

```
Thread 1 "duckdb" received signal SIGSEGV, Segmentation fault.
0x0000000000040000 in ?? ()
#0  0x0000000000040000 in ?? ()
#1  duckdb::Binder::BindSelectNode(duckdb::SelectNode&, duckdb::BoundStatement) ()
#2  duckdb::Binder::BindNode(duckdb::SelectNode&) ()
#3  duckdb::Binder::BindNode(duckdb::QueryNode&) ()
#4  duckdb::Binder::Bind(duckdb::QueryNode&) ()
#5  duckdb::Binder::Bind(duckdb::SelectStatement&) ()
#6  duckdb::Binder::Bind(duckdb::SQLStatement&) ()
#7  duckdb::Planner::CreatePlan(duckdb::SQLStatement&) ()
...
```

Frame `#0` is a call through an invalid/garbage address (`0x40000` in this run, `0x00466e2000000000`
in the original table-based finding — different each run, the classic signature of a dereferenced
uninitialized or dangling pointer rather than a fixed null-pointer check). The crash happens
**inside `BindSelectNode`**, which is where `GROUP BY` expressions are matched against `SELECT`-list
expressions (and, per Control B, matched against *themselves* even when not projected) — consistent
with a stale/dangling reference left over from binding the `AGE` call's argument-type resolution
(likely a temporary bound expression or a `unique_ptr`-owned node that the `GROUP BY`
common-expression matching logic then dereferences after its owner has already been freed or moved).
The precise line was not isolated within triage-time budget — see Open Items.

**DML impact:** not applicable — pure `SELECT`/binder-time crash, no data involved.

## How it was found

Surfaced organically by a live eqgen campaign (`--generator sqlancerpp --predicates sqlancerpp`).
Unlike every other finding in this repo, this one required **no equivalence machinery at all** —
it is a single-query, no-comparison, self-evident crash, findable by any fuzzer whose query
generator happens to produce a two-argument `AGE()` call inside a `GROUP BY`. eqgen's role here was
purely as a source of that specific expression shape (via the sqlancerpp fork's grammar), not as a
differential oracle.

- Three campaign findings share this root cause: `crash_round1021_0.sql`, `crash_round1783_0.sql`,
  `crash_round1853_0.sql`, confirmed by the identical `gdb` stack signature and the fact that all
  three collapse to the same two-token minimal repro.
- `reduced.sql` in this directory is the full, live-engine-verified repro plus every control
  listed above, run individually against the tip-of-main CLI (each crashing variant necessarily
  run in its own process, since a segfault terminates the session).

## Open items

- The exact line in `src/planner/binder/query_node/plan_select_node.cpp` (or wherever
  `BindSelectNode`'s `GROUP BY`-to-`SELECT`-list expression matching lives) that dereferences the
  stale pointer was not pinned within triage-time budget.
- Not established whether other 2-argument temporal functions with the same "constant-foldable,
  NULL-tolerant" shape (e.g. `DATEDIFF`, `DATE_SUB`) share the same crash — only `AGE` was tested.
- Not bisected to a specific commit or checked against a released version (v1.5.x).
- Recommend filing as a fresh issue (not a comment on an existing one) given no duplicate was found.
