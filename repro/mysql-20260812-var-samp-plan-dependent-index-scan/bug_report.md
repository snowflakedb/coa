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

# MySQL: VAR_SAMP() result depends on the query plan (table scan vs. index range scan), not just the data

## Summary

`VAR_SAMP()` (and by the same mechanism, likely `VARIANCE`/`VAR_POP`/`STDDEV*`) over a bitwise
expression that overflows into `BIGINT UNSIGNED` range gives **two different numeric answers** for
the *exact same 8-row table and the exact same query*, depending solely on whether MySQL's optimizer
picks a table scan or an index range scan. Aggregate functions are defined over an *unordered
multiset*; the access path is not part of the query's semantics, so a change in access path must
never change the numeric result. It does here.

## Environment

- **Engine**: MySQL 9.7.2 (docker image `mysql:9.7.2`).
- **Session**: `sql_mode = STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES`; `utf8mb4` /
  `utf8mb4_0900_bin`. Not load-bearing — the bug is purely numeric/plan-dependent, no string or
  NULL-handling semantics involved.

## Minimal repro

See [`reduced.sql`](./reduced.sql). Eight rows, one column expression, one optional secondary index:

```sql
CREATE TABLE t (c_pk BIGINT NOT NULL, c_big BIGINT, c_txt VARCHAR(255));
INSERT INTO t VALUES (1, NULL, NULL), (2, 1, 'trailing '), (3, 42, 'o''brien'), (4, 0, NULL),
                      (5, 2, 'Zed'), (6, 42, 'a'), (7, -7, 'abc'), (8, 1, 'trailing ');

SELECT VAR_SAMP((c_big | -1379963)) FROM t WHERE ('.iw#8[' <= c_txt);   -- 317429959884.8  (table scan)

CREATE INDEX t_idx ON t (c_txt(10));

SELECT VAR_SAMP((c_big | -1379963)) FROM t WHERE ('.iw#8[' <= c_txt);   -- 317580954828.8  (index range scan)
```

Same table, same 8 rows, same 6 rows matched by the `WHERE` filter, same query text — the *only*
difference between the two runs is that a secondary index now exists, and the optimizer switches
access paths. The second call answers **~151 million** higher (relative difference ≈ 4.75e-4, far
too large to be ordinary last-bit floating-point rounding — see Characterization).

## Expected vs. actual

| Access path (same data, same query) | `VAR_SAMP` result |
|---|---|
| Table scan (`IGNORE INDEX (t_idx)`, or no index exists yet) | `317429959884.8` |
| Index range scan (`FORCE INDEX (t_idx)`, or the optimizer's own unforced choice once the index exists) | `317580954828.8` |
| Exact rational-arithmetic ground truth (Python `fractions.Fraction` over the 6 matched `(c_big \| -1379963)` values) | `317373688955.8667` |

Both engine answers are wrong relative to the exact ground truth (300–650 ppm off), which on its own
might be dismissed as ordinary floating-point summation error — but the two paths **disagree with
each other**, which they must not: `VAR_SAMP` is defined over an unordered multiset with one correct
answer (up to genuinely tiny rounding), and the access path is not part of that multiset's identity.

## Why the magnitude is real, not noise

`c_big | -1379963` is a bitwise OR with a negative literal. MySQL's bit functions operate on and
return `BIGINT UNSIGNED`, so a negative-looking operand reinterprets via two's complement into a
value near 2⁶⁴ (e.g. `1 | -1379963` → `18446744073708171653`). All 6 matched rows' OR'd values sit in
this ~1.8×10¹⁹ range, differing from each other by only tens to low-thousands. A `DOUBLE` has ~15–17
significant decimal digits; values at 2⁶⁴ have 20, so each individual value is already rounded at the
low-thousands digit before `VAR_SAMP` ever sums it — and the two code paths (table scan vs. index
range scan) evidently accumulate/round these near-cancelling huge values in a different order or via
a genuinely different internal computation, producing measurably different sums of squares. This is
the standard mechanism by which summation order becomes visible for values pushed to floating-point's
precision floor, but it is not supposed to be *observable through* an access-path change on identical
SQL semantics — a correct implementation must compute the variance from the query's result rows using
one consistent (or at least consistently-rounding) algorithm regardless of how those rows were
fetched.

## Controls that isolate the plan as the sole variable

```sql
SELECT VAR_SAMP((c_big | -1379963)) FROM t FORCE INDEX (t_idx)  WHERE ('.iw#8[' <= c_txt); -- 317580954828.8
SELECT VAR_SAMP((c_big | -1379963)) FROM t IGNORE INDEX (t_idx) WHERE ('.iw#8[' <= c_txt); -- 317429959884.8
```

Same connection, same table, immediately back-to-back — `FORCE INDEX`/`IGNORE INDEX` alone flips the
answer, confirming the divergence tracks the access path and nothing else. Stable across 5 repeats
per path (verified via the triage skill's `replay_adapter.py`, which also confirmed the two `t`
relations built by the fuzz harness's builder chain were byte-identical to each other — the
divergence is not a row-preservation defect in the harness, it is the engine returning two different
numbers for one well-defined aggregate over one fixed multiset).

`EXPLAIN` for each path:

```
-- table scan
-> Aggregate: var_samp((t.c_big | -(1379963)))
    -> Filter: ('.iw#8[' <= t.c_txt)
        -> Table scan on t

-- index range scan
-> Aggregate: var_samp((t.c_big | -(1379963)))
    -> Filter: ('.iw#8[' <= t.c_txt)
        -> Index range scan on t using t_idx over ('.iw#8[' <= c_txt)
```

Both paths report the same 6 matched rows (`rows=6` once the index exists), so this is not a
row-inclusion difference — it is the aggregate computation itself disagreeing with itself.

## Minimal oracle exposure path

- **Object composition arity:** **1**
- **GCL builder path:** `MySqlPrefixIndexBuilder`
- **Confidence:** Verified — the report isolates the newly added prefix index and the GCL builder implements the matching CTAS-plus-index object.
- **Realization:** The builder internally materializes the query result and adds the text prefix index; no separate `Create*Builder` is needed in the minimal path.
- **Workload/data requirements (excluded from arity):**
  - `VAR_SAMP` over huge unsigned-valued bitwise results near `2^64`.
  - A selective text predicate for which the prefix index changes a table scan to an index range scan.
  - The same matched multiset under both access paths.

**Exposure vs. intrinsic trigger:** `MySqlPrefixIndexBuilder` exposes the defect by creating the alternative indexed access path on row-identical data. The intrinsic trigger is the aggregate's plan-dependent numeric computation; the equivalence chain and index builder are not needed once `FORCE INDEX`/`IGNORE INDEX` can select the two plans on one table.

## How it was found

eqgen's data-equivalence oracle built a row-identical equivalent `t` (a multi-step builder chain:
window-function tagging, expand/`ANY_VALUE`-reduce, a `CASE WHEN TRUE` identity pass, a union +
delete/reinsert round-trip, and — new this session — a no-op `INSERT … ON DUPLICATE KEY UPDATE`
self-upsert) whose final step added a secondary index (`CREATE INDEX … (c_txt(10))`) that the base
table never got. The random workload query (`SELECT DISTINCT VAR_SAMP(((t.c_big)|(-1379963))) FROM t
WHERE (('.iw#8[')<=(t.c_txt))`, generated by the newly-added window/aggregate generator on the
project's fresh `SQLancerPlusPlus-mysql` sqlancerpp fork) then diverged between base and equivalent.
Because the oracle guarantees the two `t` relations hold identical rows, any divergence in a
plan-agnostic aggregate is a contradiction — and bisecting away every builder step down to "plain
table, add one index" isolated the true, minimal trigger, which needs no equivalence machinery at
all to reproduce.

## Triage gates (per the project's triage skill)

- **Admissibility**: base `t` and the harness's equivalent `t` are row-identical (verified via
  `replay_adapter.py` and by direct row dump — both 8 rows, byte-identical).
- **Type equivalence**: identical column types on both sides.
- **Determinism**: each side (table-scan-path, index-scan-path) is individually stable across 5
  repeated runs on identical data — this is *not* a tie/underspecified-`ORDER BY` case (no ties are
  involved; the aggregate has a single well-defined mathematical answer for a fixed multiset), so it
  does not fall under the skill's "vary only the plan → underdetermined → comparability gap, don't
  file" exemption. That exemption covers queries the SQL standard permits multiple correct answers
  for (arbitrary-tie `ORDER BY`/`GROUP BY` representatives); `VAR_SAMP` of a closed multiset has
  exactly one standard-mandated answer, so an access-path-dependent result is a genuine wrong-result
  defect, not a legitimate ambiguity.
- **Blast radius**: read-only `SELECT`; not tested against `DELETE`/`UPDATE` (no rows are at risk —
  the defect is confined to a computed aggregate value).
