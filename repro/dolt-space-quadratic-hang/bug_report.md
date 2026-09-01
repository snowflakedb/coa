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

# Dolt: `SPACE(n)` is super-linear in `n`, so a large argument hangs the server indefinitely

## Summary

`SPACE(n)` on Dolt costs ~`O(n^1.85)` — 0.4 s at `n=100,000`, 5.0 s at `n=400,000`, and no
completion at all at `n=2,000,000`. MySQL 9.7 answers `SPACE(24,691,356)` in 9 ms. The doubling
factor (~3.6× per 2× of `n`) is what a repeated-concatenation loop produces rather than a single
allocation; `REPEAT('x', n)`, which builds an identically sized string, is linear and returns
400,000 characters in 0.0 s, so the defect is specific to `SPACE`. Nothing bounds it —
`max_allowed_packet` is 1 GiB here, far above every size tested, so the packet limit never engages.

The practical consequence is a **hang, not a slow query**: a single generated call with
`n = 24,691,356` extrapolates to roughly 5 hours, and one was observed running **2 h 01 m wall /
6 h 34 m CPU at 325% and 1.4 GiB RSS** without completing.

## Environment

- **Engine**: Dolt 8.0.31 (`VERSION()`), source `v2.2.3-49-ga995f245c`, commit
  `a995f245c032bc412aed308194d81ee12bc74f19`, assertions off.
- **go-mysql-server**: `v0.20.1-0.20260805191915-e5eafe0da809`.
- `@@max_allowed_packet` = `1073741824` (1 GiB).
- **Contrast engine**: MySQL 9.7.2 release build (`@@max_allowed_packet` = 67108864).
- Not `sql_mode`-, charset- or collation-dependent.

## Minimal repro

```sql
SELECT LENGTH(SPACE(100000));   -- dolt 0.4 s
SELECT LENGTH(SPACE(200000));   -- dolt 1.4 s
SELECT LENGTH(SPACE(400000));   -- dolt 5.0 s ; MySQL 9.7 0.000 s
SELECT LENGTH(REPEAT('x', 400000));  -- dolt 0.0 s  <-- same output size, linear
```

## Expected vs actual

| `n` | dolt `SPACE(n)` | MySQL 9.7 `SPACE(n)` |
|---|---|---|
| 10,000 | 0.0 s | — |
| 50,000 | 0.1 s | — |
| 100,000 | 0.4 s | — |
| 200,000 | 1.4 s | — |
| 400,000 | **5.0 s** | 0.000 s |
| 2,000,000 | **did not finish** | 0.001 s |
| 24,691,356 | **did not finish** (≈5 h extrapolated) | 0.009 s |
| 400,000 via `REPEAT('x', n)` | 0.0 s | — |

## Equivalence construction

**None — this is not an equivalence-oracle finding.** It reproduces on a bare `SELECT` with no table,
no view and no rewrite. The oracle's only role was to keep generating expressions until one drew a
large `SPACE` argument by arithmetic rather than as a literal:

```sql
-- dolt_run9 round35, in a WHERE, evaluated per row inside greatest():
greatest(SPACE(('1' - '3') * '-12345678'), name, to_base64(COALESCE(name, name)), name)
  BETWEEN '©' AND CAST(name AS CHAR(255))
```

`('1' - '3') * '-12345678'` = `(-2) * (-12345678)` = **24,691,356**. That is the whole mechanism: two
small string literals and a multiply produce an eight-digit argument that no length check on the
*literals* would catch.

## Minimal oracle exposure path

- **Object composition arity:** `0`.
- **GCL builder path:** none — no equivalence object participates.
- **Confidence:** high; the report reduces the finding to a bare scalar expression.
- **Realization:** none; the probe is a bare `SELECT`.
- **Workload/data requirements (excluded from arity):** the `SPACE` call, arithmetic producing a large
  positive `n`, and the argument magnitude are workload conditions, not object builders.
- **Exposure vs. intrinsic trigger:** there is no base/equivalent object contrast because the bare
  workload hangs before an oracle result pair exists. The intrinsic trigger is `SPACE(n)`'s
  super-linear construction and lack of effective cancellation.

## Characterization

- **Trigger**: any `SPACE(n)` with `n` in the hundreds of thousands or above. There is no threshold or
  cliff — the curve is smooth and super-linear from ~50,000 up.
- **Does NOT trigger**: `REPEAT('x', n)` at the same output size (linear, 0.0 s). So this is not a
  general large-string problem in Dolt; it is `SPACE`'s construction specifically.
- **Not a packet-limit rejection**: `max_allowed_packet` is 1 GiB and 24,691,356 characters is ~24 MiB.
  MySQL also does not reject at this size — it just computes it.
- **A watchdog does not rescue it.** The eqgen run that hung had the harness's `KILL QUERY` watchdog
  armed at 60 s (`dolt/adapter.WATCHDOG_SECONDS`) and still stalled for 2 hours, with the harness
  process gone and the server left spinning. `KILL QUERY` was independently verified to cancel a long
  cross join on this engine (`1105 context canceled`, session survives), so the likely explanation is
  that a single builtin evaluation has no cancellation checkpoint — but either way the empirical
  result stands: the watchdog did not end it.
- **The server also outlives its client.** No harness process remained, yet the server kept burning
  3+ cores for ~2 hours; the disconnect did not cancel the statement. Worth confirming separately, as
  it turns a slow query into an orphaned resource leak.
- Not a crash; no panic, no stack trace. Assertions-off build; irrelevant here.

## How it was found

Incidentally, by the eqgen differential fuzzer — not by its oracle. The generator drew
`SPACE(('1' - '3') * '-12345678')` into a `WHERE` predicate; the query never returned, the round never
completed, and the run made no progress for two hours. The finding is the *hang*, so there is no
mismatch or one-sided error to report and nothing under `logs/` beyond the stalled journal entry.

- Journal: hunt log, last line `-- exec: running` (nothing after it).
- Also present in `dolt_run5` round 6 as a discarded round (`negative Repeat count`), and the same
  `SPACE(id * '-12345678')` shape appears in `dolt_run4` round 3.

## Harness-side mitigation

Independent of the upstream fix, the generator should not be able to ask for a 24-million-character
string: it costs a whole run and finds nothing. `SPACE` is reachable as a catalog function with a
numeric argument, so the argument's *value* is not something the existing name/arity/type levers can
bound — the gate has to be the function name. See `dolt/config.py`
(`DOLT_UNBOUNDED_STRING_BUILDERS`).
