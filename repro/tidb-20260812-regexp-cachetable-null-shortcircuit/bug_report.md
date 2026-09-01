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

# TiDB: reading from a `CACHE`d table disables `REGEXP_REPLACE`'s NULL short-circuit — same query, same unchanging data, flips from correct to a spurious error after 1-2 calls

## Summary

`REGEXP_REPLACE(subject, pattern, repl)` must return `NULL` (and never validate `pattern`) when
any argument is `NULL` — that is standard SQL NULL-propagation, and it is what TiDB's vectorized
evaluator does. But TiDB also has a second, row-at-a-time scalar evaluator for the same function
that checks `pattern` for emptiness **before** checking whether `repl` is `NULL`. Which evaluator
runs depends on the *read path* the table takes — and `ALTER TABLE ... CACHE` (TiDB's in-memory
small-table cache) switches a table onto the read path that hits the buggy scalar evaluator, but
only **after** its background cache-load finishes.

The result: the exact same query, against the exact same never-modified table, is **correct on
the first (or first two) execution(s) and then permanently wrong from then on — for every
connection, not just the one that ran it first** — a live query determinism violation, not a
one-off fuzzer fluke.

```sql
CREATE TABLE t_tbl (c_pk BIGINT, c_txt VARCHAR(255), c_big BIGINT, c_date DATE);
INSERT INTO t_tbl VALUES (1, '', NULL, '2030-06-01'), (2, 'abc', 5, '2024-01-15');
ALTER TABLE t_tbl CACHE;
CREATE VIEW t AS SELECT * FROM t_tbl;

SELECT c_date FROM t WHERE REGEXP_REPLACE(647356755, c_txt, c_big);
-- call 1:            1 row, (2024-01-15,)                                    -- correct
-- call 2 (or 3) on:   ERROR 1139: Got error 'Empty pattern is invalid' from regexp   -- wrong, forever
```

Row 1 has `c_txt = ''` (an invalid regexp pattern) **and** `c_big = NULL` (the replacement
argument). Because `c_big` is `NULL`, the whole call must short-circuit to `NULL` for that row —
the pattern is never supposed to be validated at all. The correct, permanent answer is 1 row,
`(2024-01-15,)`.

## Environment

| | |
|---|---|
| Engine | tidb `8.0.11-TiDB-v9.0.0-beta.2.pre-2051-g3bea8196a5` @ `3bea8196a5` (2026-07-30), `unistore`, built locally |
| Source | `pingcap/tidb` @ `3bea8196a5` (same commit), read directly to confirm the code paths below |
| `sql_mode` | `STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES` — **not load-bearing** |
| Collation / charset | `utf8mb4_0900_bin` / `utf8mb4` — not load-bearing |
| Access path | pymysql via eqgen `TiDbAdapter`, and directly via a second raw connection (Control D) |

## How it was found

Surfaced by an eqgen campaign (`--generator sqlancerpp --predicates sqlancerpp`) as a one-sided
`error_*` finding: base table succeeded, the equivalent (which routes its final exposed relation
through `TiDbCachedTableBuilder` — `ALTER TABLE ... CACHE` on the backing table, then a `VIEW` on
top, part of eqgen's TiDB-specific builder set) raised
`(1139, "Got error 'Empty pattern is invalid' from regexp")` on
`SELECT DISTINCT t.c_date FROM t WHERE REGEXP_REPLACE(647356755, t.c_txt, t.c_big)`. Two
independent campaign rounds hit the identical error text and mechanism
(`error_round43_0.sql`, `error_round125_0.sql` in
`log/tidb_20260812-030731/`), confirming one root cause, not a fluke.

Replaying `error_round43_0.sql` with the triage skill's `replay_adapter.py` reported **"did not
reproduce"** on a single fresh attempt — because the query only fails from the *second* call
onward per table. Re-running the same equivalent-side SQL block in a loop immediately showed the
flip: call 0 succeeded, calls 1–7 all failed. That is the finding.

## Reproduction and gates

- **Reproduces**: yes, reliably — 3 fresh trials of the minimal repro all showed at least one
  success followed by a permanent flip to error (flip point varies: after call 1 in two trials,
  after call 2 in the third — consistent with an async background load whose completion time is
  not fixed; see Characterization).
- **Admissibility**: not applicable to this class of finding in the usual base/equivalent sense —
  the divergence reproduces on a **single** cached table queried repeatedly, with no equivalence
  chain at all (see Control C). The original eqgen finding's base and equivalent tables were row-
  and type-identical before divergence (`replay_adapter.py`, both `error_round43_0.sql` gate
  checks).
- **Determinism**: the *query* is not "underdetermined" in the usual optimizer-choice sense (no
  ties, no unordered `LIMIT`) — the non-determinism is the engine's, tracked precisely to a state
  transition in the cached-table subsystem (below), not a property of the SQL.

## Minimal oracle exposure path

- **Object composition arity:** `2`.
- **GCL builder path:** `TiDbCachedTableBuilder → cached TABLE realization`.
- **Confidence:** Verified against the report, the current TiDB GCL allow-list, and
  `TiDbCachedTableBuilder`.
- **Realization:** the native builder creates and fills a backing table, applies `ALTER TABLE …
  CACHE`, and exposes it through a hardcoded view. The cached table is the counted realization; the
  cosmetic exposure view is not a separately selected GCL factor.
- **Workload/data requirements (excluded from arity):** the empty regexp pattern, NULL replacement,
  repeated calls needed for cache warm-up, and all row values are query/data requirements and are
  not counted.
- **Exposure vs. intrinsic trigger:** cached-table realization is intrinsic to the read-path flip;
  the view is only exposure, because querying the cached table directly reproduces.

## Characterization

**Necessary ingredients** (`reduced.sql` has every control):

- `ALTER TABLE ... CACHE` is required — Control A (identical data and view, no `CACHE`) never
  errors, over 5+ repeats.
- The row with the empty pattern must have a **`NULL`** value in the argument evaluated *after*
  the pattern (here, `repl`/`c_big`) — Control B (same shape, `c_big` non-`NULL`) correctly and
  consistently errors on **every** call, including the first; there is nothing to short-circuit on,
  so that is right.
- The `VIEW` is cosmetic — Control C (query the cached table directly, no view) shows the same
  flip.
- The flip is **not per-session**: Control D opens a brand-new connection after the flip has
  already happened on connection 1, and its very first query on that table already errors. The
  state that changed lives with the table's cache, not the client session.

**Mechanism, confirmed by reading `pkg/expression/builtin_regexp.go` at the build under test:**

TiDB has two independent evaluators for `REGEXP_REPLACE`, and they check argument-`NULL`s in
different orders:

- **Scalar, row-at-a-time** — `(*builtinRegexpReplaceFuncSig).evalString`, `builtin_regexp.go:1174`:
  ```go
  pat, isNull, err := re.args[1].EvalString(ctx, row)     // 1181
  if isNull || err != nil {
      return "", true, err
  } else if len(pat) == 0 {                                // 1184
      return "", true, ErrRegexp.GenWithStackByArgs(emptyPatternErr)   // 1185 -- validates pattern HERE
  }
  repl, isNull, err := re.args[2].EvalString(ctx, row)     // 1188 -- repl's NULL is checked AFTER
  if isNull || err != nil {
      return "", true, err
  }
  ```
  `repl` (arg 2, our `c_big`) is evaluated and NULL-checked **after** the pattern has already been
  validated and, if empty, already raised. A `NULL` `repl` can no longer save the row.

- **Vectorized, batch** — `(*builtinRegexpReplaceFuncSig).vecEvalString`, `builtin_regexp.go:1267`,
  the per-row loop starting at `builtin_regexp.go:1356`:
  ```go
  for i := range n {
      if isResultNull(buffers, i) {    // 1357 -- combined NULL bitmap across ALL args, checked FIRST
          result.AppendNull()
          continue
      }
      ...
      pattern := params[1].getStringVal(i)
      reg, err = re.buildRegexp(pattern, matchType)   // pattern validated only for rows that survive the check above
  ```
  Here the row is skipped — via a NULL bitmap that already merges every argument's null-ness —
  **before** the pattern is ever looked at. This is the correct order and it is what the fresh
  (uncached) read path uses, which is why Control A never errors.

**Why `CACHE` flips which evaluator runs.** `pkg/table/tables/cache.go`'s `TryReadFromCache`
returns `(data.MemBuffer, loading bool)`: while the async background load
(`updateLockForRead` → `loadDataFromOriginalTable`) is still in flight, `loading` is `true` and the
read falls through to the ordinary path (vectorized, correct). Once the load completes, every
subsequent read — from any session — is served from the populated in-memory `MemBuffer`, and the
logical query plan (confirmed identical via `EXPLAIN` before and after the flip — the swap happens
below the plan, at the snapshot/executor level) evaluates the `Selection` filter through the
scalar path instead of staying vectorized. That transition is exactly the observed flip point,
and its variable timing (1 vs 2 successful calls across trials) matches an async, not synchronous,
load.

**This is a recurring category, not a one-off**: TiDB has shipped and fixed several previous
"cached table returns wrong results" bugs with *different* mechanisms — e.g.
[#32422](https://github.com/pingcap/tidb/issues/32422) (a missing filter condition, 2022, closed),
[#32991](https://github.com/pingcap/tidb/issues/32991) ("sometimes return wrong results when
enable table-cache", 2022, closed), [#42928](https://github.com/pingcap/tidb/issues/42928)
(incorrect `NULL`s for newly-added columns, 2023, closed). None of those involve `REGEXP_REPLACE`
or a scalar/vectorized evaluator disagreement; this is a new mechanism in an area with a known
track record of read-path correctness gaps.

**Blast radius**: not checked against `DELETE`/`UPDATE` (a cached table's *write* path always
goes to the original table per `cache.go`'s `AddRecord`/`UpdateRecord`/`RemoveRecord`, so DML
correctness is a separate question from this read-path finding — likely unaffected, not
independently verified here). Not reproduced against a real TiKV cluster (`unistore` only, per
this session's setup) — the `MemBuffer`/scalar-evaluator mechanism above is store-agnostic
(pure Go, `pkg/table/tables` and `pkg/expression`), so it is expected to reproduce on TiKV too, but
that is not independently confirmed.

## Open items

- Not bisected against a real TiKV store (only `unistore` available in this environment).
- The exact executor that reads a cached `MemBuffer` and ends up calling the scalar `evalString`
  instead of staying vectorized was not pinned to a specific `file:line` — inferred from
  `cache.go`'s read-path split plus the observed identical `EXPLAIN` output across the flip
  (ruling out a plan-level cause) and the two evaluators' differing NULL-check order (confirmed by
  source). A maintainer with the executor's read-from-cache code path in front of them should be
  able to close this gap in minutes.
- Not checked whether other multi-arg, NULL-propagating builtins with a similar
  scalar-vs-vectorized split (`REGEXP_SUBSTR`, `REGEXP_INSTR`) have the same inconsistency — only
  `REGEXP_REPLACE` was tested.
- Recommend filing as a fresh issue.
