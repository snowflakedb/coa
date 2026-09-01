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

# CrateDB: `ROUND(x, N)` with a large-magnitude negative `N` causes unbounded CPU/time consumption — a single scalar `SELECT`, no table, pins a core for minutes to (extrapolated) hours, invisible in `sys.jobs`, and does not stop when the client disconnects

## Summary

```sql
SELECT ROUND('-1479165877', -556375977);
```

This single, table-free `SELECT` never returns in practical time. Execution time against the
rounding precision's magnitude grows faster than linearly (measured: 1M → 0.1s, 2M → 0.25s,
5M → 0.8s, 10M → ~2.15s, 20M → 6.25s — roughly `O(n^1.3–1.5)`), so a precision in the hundreds of
millions (as SQLancer++'s fuzzer trivially generates via a random `INT` literal) extrapolates to
**minutes to hours**, with no result. Three things make this worse than "a slow query":

1. **It is not visible in `sys.jobs`.** While the query is running and burning a full CPU core,
   `SELECT * FROM sys.jobs` shows nothing — an operator has no way to see, diagnose, or `KILL` it
   through CrateDB's own introspection.
2. **It does not stop when the client disconnects.** Four separate client attempts that each hit
   their own timeout and closed the connection left **four** orphaned computations running
   concurrently — confirmed by `docker stats` climbing from ~97% to **388% CPU** on a container with
   no other traffic, with `sys.jobs` still empty throughout.
3. **No input validation** on the precision argument at all — this is a plain scalar function call,
   reachable by any client with `SELECT` access, no table or special privileges required.

## Environment

- **CrateDB 6.4.2**, `built 1db6455/NA`, and independently reproduced on **CrateDB 6.4.1**
  (`built 45bfa80/NA`) — both affected, timings consistent between versions (10M-magnitude case:
  2.15s on 6.4.2, 2.16s on 6.4.1).
- OpenJDK 25, Linux aarch64, Docker `crate:6.4.1` / `crate:6.4.2`, single node, default config.
- Access path: PostgreSQL wire protocol via `psycopg`. Not yet retried through HTTP `_sql` (see Open
  items).

## Minimal repro

```sql
-- Returns instantly (this magnitude is fine):
SELECT ROUND('-1479165877', -1000000);            -- 0.1s,  -> 0

-- Grows steeply with the magnitude of the (negative) second argument:
SELECT ROUND('-1479165877', -5000000);            -- 0.8s   -> 0
SELECT ROUND('-1479165877', -10000000);           -- 2.15s  -> 0
SELECT ROUND('-1479165877', -20000000);           -- 6.25s  -> 0

-- Never returned in any test (client gave up after its own timeout; server kept running):
SELECT ROUND('-1479165877', -50000000);
SELECT ROUND('-1479165877', -100000000);
SELECT ROUND('-1479165877', -300000000);
SELECT ROUND('-1479165877', -556375977);          -- the exact value the fuzzer generated
```

No table, no join, no data — a bare constant-expression `SELECT`. The first argument's exact value
does not appear to matter (tested as both a quoted string, as here, and as a bare integer); the
trigger is the magnitude of the **second** argument.

## Expected vs actual

| Query | Expected | Actual |
|---|---|---|
| `ROUND(x, N)` for reasonable `N` (say, ±20) | instant, correct rounding | correct, instant |
| `ROUND(x, -10000000)` | instant (or a documented precision limit / error) | 2.15s, then correct-looking `0` |
| `ROUND(x, -556375977)` | instant, or a clear rejection (`precision out of range`) | **never returns**; pins a CPU core; not visible in `sys.jobs`; survives client disconnect |

There is no dispute about "which side is wrong" here — this is a hang/resource-exhaustion finding,
not a wrong-result finding. `ROUND` returning the mathematically correct `0` for the in-range test
cases is exactly what the fast path should look like at every magnitude; the defect is purely in the
execution *cost*, and in the surrounding operational blind spot (no `sys.jobs` visibility, no
cancellation on disconnect).

## Minimal oracle exposure path

**Object composition arity:** `0`

**GCL builder path:** `none`

**Confidence:** verified

**Realization:** The trigger is an internal scalar-expression path in a table-free `SELECT`; no `VIEW` or `TABLE` realization is involved.

**Workload/data requirements (excluded from arity):**
- `ROUND(x, N)` with a very large-magnitude negative `N`.
- No rows, schema objects, joins, or special privileges.
- A client timeout does not cancel the server-side work.

**Exposure vs. intrinsic trigger:** There is no object contrast to count: the workload expression hangs identically without an equivalence relation, and the builder chain was removed in full. Eqgen exposed the issue only operationally when the generated workload stalled campaign progress.

## Characterization

- **Timing, both versions, same trigger**:

  | magnitude of `N` | 6.4.2 time | 6.4.1 time |
  |---|---|---|
  | 1,000,000 | 0.10s | — |
  | 2,000,000 | 0.25s | — |
  | 5,000,000 | 0.80s | — |
  | 10,000,000 | 2.15s | 2.16s |
  | 20,000,000 | 6.25s | — |
  | 50,000,000+ | >6–15s (client gave up) | — |
  | 556,375,977 (fuzzer's value) | never returned (16+ minutes observed) | — |

  Growth is super-linear (roughly `time ∝ N^1.3–1.5` in the measured range), consistent with an
  implementation that materializes a power-of-ten scale factor with a number of **decimal digits**
  proportional to `N` — for `N` ≈ 556 million, that value would need on the order of 500+ million
  decimal digits to represent exactly, which is consistent with unbounded CPU and eventual memory
  pressure rather than a fixed-cost operation.
- **Orphaned on disconnect, confirmed by `docker stats`**: four client attempts (each using its own
  large `N` and its own short client-side timeout) left the container's CPU climbing to 388% with no
  other load and `sys.jobs` empty the entire time. The container had to be force-removed to reclaim
  the CPU; there was no SQL-level way found to list or cancel the runaway executions.
- **Discovered via the equivalence oracle's *workload query*, not the equivalence construction** —
  see **How it was found**. The exact trigger, `SELECT ROUND('-1479165877', -556375977)`, is a
  `WHERE`-clause fragment generated by eqgen's SQLancer++ predicate/query generator (a randomly typed
  numeric literal landed in `ROUND`'s second argument slot); nothing about the equivalence-builder
  chain is implicated, and this reproduces with **no builder chain at all** — see Minimal repro.
- **Determinism / gates N/A**: this is a hang finding, not a mismatch — there is no result multiset to
  compare, and no "which side is right" question. The three admissibility gates in the standard
  triage flow (reproduce / row-identity / determinism) don't apply the same way; the relevant checks
  here are reproducibility (confirmed, deterministic magnitude-vs-time relationship across repeated
  runs and across two engine versions) and severity (confirmed via `docker stats` and the empty
  `sys.jobs`).
- **Not tested**: whether `TRUNCATE`/other rounding-family functions share the same code path and the
  same defect; whether a positive `N` of similar magnitude (rounding to N decimal *places* rather
  than *digits before the decimal point*) also blows up; DML impact; the HTTP `_sql` access path.

## How it was found

eqgen's data-equivalence oracle builds a row-equivalent object and re-runs the *same* workload query
against both sides — but the workload query itself, including its predicates, comes from an
external generator (here, SQLancer++'s CrateDB "general" fuzzer), and *that* generator drew `ROUND`
with two independently-typed random arguments and happened to draw a large negative integer for the
second. The equivalence oracle's own harness noticed the anomaly indirectly: the hunt's round
scheduler timed the query out, the harness's own retry/self-heal logic restarted the query-generator
subprocess to keep the campaign going, and the *hung round* itself (query throughput frozen at a
fixed count for 60s+, no error, no crash) was the signal that something was stuck — this is not the
"two relations disagree" signal the oracle exists for, but the harness surfaced it anyway because a
live hunt simply stopped making progress. `jstack` on the stuck Java client and `docker stats` on the
CrateDB container is what actually localized the hang to the server, and reducing the finding query
down to a single scalar `SELECT` (removing the surrounding `FROM t1, t2, t0 WHERE ... ORDER BY ...`)
confirmed `ROUND` alone is sufficient with no equivalence construction, no join, and no data at all.

Found live during a CrateDB 6.4.2 fuzzing campaign (workdir `crate7`, seed 91003, simple
catalog, round ~39): `SELECT * FROM t1, t2, t0 WHERE ROUND('-1479165877', -556375977) ORDER BY
t0.c_pk, t1.c_pk ASC;`.

## Open items

- **HTTP `_sql` access path untested** — confirm the hang is not specific to the PostgreSQL-wire
  client layer (unlikely, since the CPU cost is visibly server-side via `docker stats`, but not yet
  excluded through a second client).
- **`TRUNCATE` and positive-`N` `ROUND` untested** — establish whether this is specific to negative
  scale or a broader numeric-formatting code path.
- **Exact mechanism not confirmed against source** — CrateDB source was not available to inspect on
  this machine, so the "materializes a power-of-ten with N digits" explanation is inferred from the
  growth curve and the eventual-termination behavior at moderate N, not read from `file:line`.
- **DML impact untested.**
- **No SQL-level way to observe or cancel the runaway job was found** — `sys.jobs` was checked
  repeatedly while the query ran and stayed empty; no other introspection view was tried. Worth
  checking `sys.operations` / `sys.node_checks` or similar if the goal is operational mitigation
  rather than just the engine defect itself.
- **No suggested fix** — likely candidates, unconfirmed: reject `ROUND`/rounding-family calls whose
  effective scale would exceed a sane digit-count bound before allocating a `BigDecimal`/`BigInteger`
  scale factor, or use a closed-form scale computation that doesn't require materializing an N-digit
  value at all.
