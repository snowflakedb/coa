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

# MariaDB: `MIN()`/`MAX()` over `BIGINT UNSIGNED` loses the UNSIGNED flag when converted to a float, wrapping values ≥ 2^63 to small negatives

## Summary

`SELECT CAST(MIN(sh) AS DOUBLE) FROM t` where `sh BIGINT UNSIGNED` holds `18446744073709551588`
(= 2^64 − 28) returns **`-28.0`**. `MIN(sh)` on its own returns the correct unsigned value, and casting
the *column* is correct — the UNSIGNED flag is lost on the **aggregate's result**, so the conversion to
`DOUBLE`/`FLOAT` reinterprets the 64-bit pattern as two's-complement signed. Any float context triggers
it (`CAST`, `+ 0e0`, a window `MIN() OVER ()`), and it silently leaks into other functions:
`SQRT(MIN(sh))` returns `NULL` instead of `4294967296` because `SQRT` sees `-28`. The threshold is
exactly the signed boundary: `2^63−1` is fine, `2^63` and above wrap. An explicit `GROUP BY`, a
`DECIMAL` cast, `SUM`, `COALESCE`, or materialising the aggregate in a derived table all avoid it.

**MariaDB-only among the four MySQL-protocol engines tested**: MySQL 9.7.2, TiDB v9.0.0-beta.2 and
Dolt 2.2.3 all return the correct `1.8446744073709552e+19`.

## Environment

| | |
|---|---|
| Affected | MariaDB `11.4.12-MariaDB-ubu2404` (docker `mariadb:11.4`) |
| Not affected | MySQL `9.7.2`, TiDB `8.0.11-TiDB-v9.0.0-beta.2.pre-2051-g3bea8196a5`, Dolt `2.2.3` |
| Session | defaults; `sql_mode` irrelevant |
| Regression window | not determined — only one MariaDB image available (see *Open items*) |

## Minimal repro

```sql
CREATE TABLE t (sh BIGINT UNSIGNED, g INT);
INSERT INTO t VALUES (18446744073709551588, 1);   -- 2^64 - 28

SELECT MIN(sh)                  FROM t;   -- 18446744073709551588        correct
SELECT CAST(sh AS DOUBLE)       FROM t;   -- 1.8446744073709552e+19      correct (no aggregate)
SELECT CAST(MIN(sh) AS DOUBLE)  FROM t;   -- -28.0                       WRONG
SELECT MIN(sh) + 0e0            FROM t;   -- -28.0                       WRONG (no CAST needed)
SELECT SQRT(MIN(sh))            FROM t;   -- NULL   (expected 4294967296) WRONG
```

## Expected vs actual

**MariaDB is the wrong side**, established against three independent reference engines and internally
(MariaDB's own `MIN(sh)`, `CAST(sh AS DOUBLE)`, and `CAST(MIN(sh) AS DECIMAL(30,0))` are all correct on
the same data, so the engine holds the right value and loses it only on the float conversion).

| Query | MariaDB | MySQL / TiDB / Dolt |
|---|---|---|
| `CAST(MIN(sh) AS DOUBLE)` | **`-28.0`** | `1.8446744073709552e+19` |
| `CAST(MAX(sh) AS DOUBLE)` | **`-28.0`** | `1.8446744073709552e+19` |
| `MIN(sh) + 0e0` | **`-28.0`** | `1.8446744073709552e+19` |
| `CAST(MIN(sh) AS FLOAT)` | **`-28.0`** | `1.84467e+19` / `1.8446744e+19` |
| `CAST(MIN(sh) OVER () AS DOUBLE)` | **`-28.0`** | `1.8446744073709552e+19` |
| `SQRT(MIN(sh))` | **`NULL`** | `4294967296.0` |
| `sh = 2^63` (`9223372036854775808`) | **`-9.223372036854776e+18`** | `9.223372036854776e+18` |
| `sh = 2^64−1` | **`-1.0`** | `1.8446744073709552e+19` |
| `MIN(sh)` (no conversion) | `18446744073709551588` | same — correct |
| `CAST(sh AS DOUBLE)` (no aggregate) | `1.8446744073709552e+19` | same — correct |
| `CAST(MIN(sh) AS DOUBLE) … GROUP BY g` | `1.8446744073709552e+19` | same — correct |
| `CAST(MIN(sh) AS DECIMAL(30,0))` | `18446744073709551588` | same — correct |
| `CAST(SUM(sh) AS DOUBLE)` | `1.8446744073709552e+19` | same — correct |
| `CAST(COALESCE(MIN(sh),0) AS DOUBLE)` | `1.8446744073709552e+19` | same — correct |
| `CAST(m AS DOUBLE) FROM (SELECT MIN(sh) AS m …) x` | `1.8446744073709552e+19` | same — correct |

The wrap is exact two's complement: `2^64−28 → −28`, `2^64−1 → −1`, `2^63 → −2^63`.

## Minimal oracle exposure path

- **Object composition arity:** **0**
- **GCL builder path:** none — diagnostic-only finding, with no base/equivalent object contrast
- **Confidence:** Verified — the report states that this was discovered by a diagnostic query while triaging another finding, not by an equivalence rewrite.
- **Realization:** None; the minimal repro is a plain table queried directly.
- **Workload/data requirements (excluded from arity):**
  - `MIN` or `MAX` over `BIGINT UNSIGNED` at or above `2^63`.
  - A direct floating-point conversion or context, without the documented masking boundary.
  - Implicit single-group aggregation for the reduced scalar case.

**Exposure vs. intrinsic trigger:** There is no oracle object exposure path to count. The intrinsic trigger is the aggregate result losing unsignedness during direct float conversion; arity is zero because this report is diagnostic-only, not because the workload has no requirements.

## Characterization

`reduced.sql` holds 16 blocks; each was run against **all four engines**, asserting
both halves of the claim (MariaDB returns the documented wrong value, the others the correct one). All
16 pass.

| Ingredient | Control that behaves correctly |
|---|---|
| the value must exceed signed range | `2^63−1` → correct; `2^63` → wraps. So any `BIGINT UNSIGNED` actually using the unsigned range is exposed |
| an aggregate must be in the way | `CAST(sh AS DOUBLE)` on the column → correct |
| the target must be floating point | `CAST(MIN(sh) AS DECIMAL(30,0))` → correct |
| the aggregate must be `MIN`/`MAX` | `CAST(SUM(sh) AS DOUBLE)` → correct (SUM yields DECIMAL) |
| the aggregate must be implicitly grouped | adding `GROUP BY g` → **correct** — the sharpest control |
| the aggregate must feed the conversion directly | `COALESCE(MIN(sh),0)`, or a derived table, → correct (workarounds) |

The `GROUP BY` control is the most suggestive for a fix: the same expression over the same row is right
when there is an explicit grouping and wrong for the implicit single-group aggregate, which points at
where the result's unsignedness is dropped.

**Severity.** Wrong reads, silently: no error, no warning, and the value is not merely imprecise but a
different number of the opposite sign. `SQRT(MIN(sh)) → NULL` shows it propagating into unrelated
functions. Not tested: whether the wrapped value can reach a `WHERE`/`HAVING` comparison or an index
lookup and change which rows are returned, or whether `UPDATE`/`DELETE` can be affected.

## How it was found

While triaging the sibling report
[`../mariadb-20260809-060545-round6-varsamp-unsigned-order/`](../mariadb-20260809-060545-round6-varsamp-unsigned-order/)
— specifically, when probing how each engine converts those two `BIGINT UNSIGNED` values to `DOUBLE`,
MariaDB answered `-1792.0` and `-28.0`. It was **not reported by the fuzzer**; it surfaced from a
diagnostic query written to explain a different finding.

**Relationship to that finding — adjacent, not established as its cause.** Both involve
`BIGINT UNSIGNED` near 2^64 and the integer→double path, so a shared root cause is plausible and worth
the maintainer's attention in one pass. But the numbers do not line up: if `VAR_SAMP` were computing
over the wrapped values (−28 and −1792, difference 1764) the sample variance would be
`1764²/2 = 1555848`, and MariaDB returns `4194304`. So the variance bug is not simply this bug applied
to the aggregate's inputs. Treat them as two reports that should be looked at together.

* Reduced repro: [`reduced.sql`](reduced.sql)
* Related: [`../mariadb-20260809-060545-round6-varsamp-unsigned-order/bug_report.md`](../mariadb-20260809-060545-round6-varsamp-unsigned-order/bug_report.md)

## Open items

* **Regression window not determined.** Only `mariadb:11.4` was available; worth checking 10.x and
  MariaDB `main`, and confirming against a second MySQL version since MySQL 9.7.2 is unaffected here.
* Whether the wrapped value can change row selection (`WHERE`/`HAVING`/index) or affect DML.
* Whether other implicitly-grouped aggregates that preserve the argument's integer type (e.g.
  `BIT_AND`, `BIT_OR`, `BIT_XOR`) share it — untested.

## Side observation (Dolt, not MariaDB)

A `GREATEST(sh, sh)` control was dropped from `reduced.sql` because **Dolt 2.2.3 raises
`1105 Unsigned int…` on `GREATEST` over `BIGINT UNSIGNED`**, while MariaDB, MySQL and TiDB all return
the value correctly. That is a separate Dolt limitation, noted here only so the observation is not lost;
it is not part of this bug and has not been triaged.
