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

# Dolt: `STDDEV`/`VARIANCE`/`STDDEV_SAMP`/`VAR_SAMP` return the argument's type instead of `DOUBLE`, rounding the result

## Summary

Over an integer column, Dolt's `STDDEV`, `STDDEV_SAMP`, `VARIANCE` and `VAR_SAMP` return a
**LONGLONG** and round the result to a whole number: for `i = (1,2,5)`, `STDDEV(i)` returns `2` instead
of `1.6997`. MySQL/MariaDB return `DOUBLE` for these aggregates regardless of argument type. The
rounding is not just imprecise, it is **lossy in a way that erases meaning** — `STDDEV(i)` (1.6997) and
`STDDEV_SAMP(i)` (2.0817) are mathematically different but both round to `2`, so the
population/sample distinction disappears on integer columns. A string argument yields a **VAR_STRING**
result (`'0'`) where MySQL returns `DOUBLE` (`0.0`). Casting the argument to `DOUBLE` restores the
correct value, and `STDDEV` over a `DOUBLE` column is already correct — so the arithmetic is right and
only the result type (and the rounding it forces) is wrong.

## Environment

| | |
|---|---|
| Engine | `dolt version 2.2.3` (server reports `VERSION()` = `8.0.31`) |
| Reference | MariaDB `11.4.12-MariaDB-ubu2404` (docker `mariadb:11.4`) |
| Session | `sql_mode` matched on **both** engines to the fuzz run's `STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES,PIPES_AS_CONCAT` — see *Pitfall* below |
| Regression window | not determined (single build available) |

## Minimal repro

```sql
CREATE TABLE t (i BIGINT, d DOUBLE, s VARCHAR(50));
INSERT INTO t VALUES (1,1.0,'a'),(2,2.0,'b'),(5,5.0,'c');

SELECT STDDEV(i)      FROM t;   -- 1.6997 expected; Dolt returns 2   (wire type LONGLONG)
SELECT STDDEV_SAMP(i) FROM t;   -- 2.0817 expected; Dolt returns 2   (same value as STDDEV!)
SELECT VARIANCE(i)    FROM t;   -- 2.8889 expected; Dolt returns 3
SELECT VAR_SAMP(i)    FROM t;   -- 4.3333 expected; Dolt returns 4
SELECT VARIANCE(s)    FROM t;   -- 0.0 (DOUBLE) expected; Dolt returns '0' (VAR_STRING)
```

## Expected vs actual

`i = (1,2,5)`: population SD 1.6997, sample SD 2.0817, population VAR 2.8889, sample VAR 4.3333.

| Query | MariaDB 11.4 | Dolt 2.2.3 |
|---|---|---|
| `STDDEV(i)` | `1.6997` DOUBLE | **`2` LONGLONG** |
| `STDDEV_SAMP(i)` | `2.0817` DOUBLE | **`2` LONGLONG** |
| `VARIANCE(i)` | `2.8889` DOUBLE | **`3` LONGLONG** |
| `VAR_SAMP(i)` | `4.3333` DOUBLE | **`4` LONGLONG** |
| `VARIANCE(s)` (VARCHAR arg) | `0.0` DOUBLE | **`'0'` VAR_STRING** |
| `STDDEV(d)` (DOUBLE arg) | `1.699673171197595` | `1.699673171197595` — correct |
| `STDDEV(CAST(i AS DOUBLE))` | `1.699673171197595` | `1.699673171197595` — correct (workaround) |
| `AVG(i)` | `2.6667` NEWDECIMAL | `2.6666666666666665` DOUBLE — value right |

**Dolt is the wrong side**, established against MariaDB and internally: Dolt's own `STDDEV` over the
same values as `DOUBLE` gives the full result, so the engine can compute it correctly and only the
integer-typed return path loses it. `AVG` over the same column is not rounded, so this is specific to
the `STDDEV`/`VARIANCE` family rather than a general integer-aggregate rule.

## Minimal oracle exposure path

- **Object composition arity:** `2`.
- **GCL builder path:** `SelectStarQueryBuilder → CreateViewBuilder` *(historical/inferred)*.
- **Confidence:** inferred; both class names exist in the current factory, but this report preserves
  emitted SQL rather than the original finding AST/builder-selection metadata.
- **Realization:** inferred persisted view created by `CreateViewBuilder`.
- **Workload/data requirements (excluded from arity):** integer- or string-typed aggregate input,
  `STDDEV`/`VARIANCE` family selection, group membership, and plan-dependent summation order are
  workload/data conditions.
- **Exposure vs. intrinsic trigger:** the inferred view path changed plan order enough to expose the
  quantization as a mismatch. The intrinsic defect is the aggregate family's argument-derived return
  type; it reproduces without the equivalence object.

## How it was found

Two findings in `dolt_20260809-052933` (rounds 22 and 29) had the *same* group keys and row
counts on both sides but differing aggregate values — e.g. round 29 returned `('', 1)` on the base side
and `('', 0)` on the equivalent side for `STDDEV((+ t2.c_pk))`. Those are integers, which is the tell:
the pre-rounding `DOUBLE` differs slightly between the two plan shapes (different summation order over
the same rows), and rounding to a whole number turns a difference far below any tolerance into a
visibly different answer. eqgen's float comparison uses a relative tolerance precisely so ULP noise
across plans is not reported — but that tolerance cannot help once the engine has quantised the result
to an integer.

So the equivalence oracle did not find "STDDEV is rounded" directly; it found that **rounding makes an
otherwise-benign plan-order difference observable**, and the reference comparison against MariaDB is
what identified the underlying defect.

* Rounds 22 and 29, seeds in the finding headers
* Reduced repro: [`reduced.sql`](reduced.sql)
* Original findings: `dolt_20260809-052933/mismatch_round22_0.sql`, `mismatch_round29_0.sql`

## Pitfall for anyone reproducing this cross-engine

`||` is **logical OR** in MySQL/MariaDB unless `PIPES_AS_CONCAT` is set, and the fuzz run sets it for
Dolt. My first reference comparison ran MariaDB without it, so `t1.c_chr || t2.c_chr` evaluated as a
boolean, MariaDB returned 2 rows against Dolt's 8, and the whole comparison looked like a catastrophic
divergence. Match `sql_mode` on both engines before concluding anything; the verifier here does it
explicitly.

## Open items

* Source location not pinned — look at the `STDDEV`/`VARIANCE` aggregation implementations' return-type
  derivation in go-mysql-server; it should be `DOUBLE` unconditionally, as `AVG`'s effectively is here.
* Whether the rounding is round-half-up, truncation, or banker's rounding: `1.6997 → 2`, `2.8889 → 3`,
  `4.3333 → 4` are all consistent with round-to-nearest, but I did not test a `.5` case.
* Regression window not determined.

## Harness note (eqgen)

Worth a defensive tweak: when an aggregate's result comes back with an **integer or string** wire type
from a function that is specified to return `DOUBLE`, the comparison tolerance is meaningless and a
plan-order difference will be reported as a mismatch. Either treat such a finding as a type defect
rather than a value mismatch, or record the declared type alongside the value in the finding file so a
triager sees it immediately.
