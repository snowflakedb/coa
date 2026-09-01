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

# Dolt: `OVER (ORDER BY …)` window aggregates use a ROWS default frame instead of RANGE — wrong SUM/AVG, and non-deterministic over tied keys

## Summary

For an aggregate window function with `OVER (ORDER BY <col>)` and **no explicit frame**, dolt
applies a `ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW` frame. The SQL standard and MySQL
default to **RANGE**, under which all peer rows (equal `ORDER BY` key) share one frame end and thus
one aggregate value. The consequence is twofold: (1) `SUM`/`AVG` are simply **wrong** versus MySQL
whenever the `ORDER BY` key has ties; and (2) because a ROWS frame over tied keys depends on an
undefined intra-peer row order, the result is **non-deterministic** — which is what the eqgen
equivalence oracle detected (base table vs a row-identical view produced different values). With an
explicit `RANGE` frame dolt is correct, confirming the fault is default-frame resolution.

## Environment

- **Engine**: Dolt 8.0.31 (`VERSION()`), source `v2.2.3-9-g95218a00a`, commit
  `95218a00a973be43d84e5c60836cb3ffe8c34387`, assertions off. Engine = dolthub/go-mysql-server.
- **Session**: sql_mode as in the finding; utf8mb4 / utf8mb4_0900_ai_ci. Bug is independent of these.

## Minimal repro

See [`reduced.sql`](./reduced.sql). All rows share `name='a'`, so every row is a peer:

```sql
CREATE TABLE t (id BIGINT, name VARCHAR(255));
INSERT INTO t VALUES (1,'a'),(2,'a'),(3,'a');
SELECT id, SUM(id) OVER (ORDER BY name) FROM t ORDER BY id;
```

## Expected vs actual

| Query (rows all `name='a'`) | MySQL 9.7 (expected) | Dolt (actual) |
|---|---|---|
| `SUM(id) OVER (ORDER BY name)` | `6, 6, 6` | **`4, 6, 3`** |
| `AVG(id) OVER (ORDER BY name)` | `2.0, 2.0, 2.0` | **`2.0, 2.0, 3.0`** |
| `COUNT(*) OVER (ORDER BY name)` | `3, 3, 3` | `3, 3, 3` ✓ |
| `SUM(id) OVER (ORDER BY name RANGE …CURRENT ROW)` | `6, 6, 6` | `6, 6, 6` ✓ (explicit RANGE correct) |
| `SUM(id) OVER (ORDER BY name ROWS …CURRENT ROW)` | `1, 3, 6`* | `4, 6, 3` (== dolt's bare default) |

MySQL column verified live against MySQL 9.7. *The `ROWS` row shows the default matches ROWS in dolt;
its exact per-row values are themselves order-dependent because the `ORDER BY` key is all ties.

## Equivalence construction

The 7 mismatch findings in this run use several row-preserving builders (the `eq_seq_key`
column-split-rejoin view — rounds 37/46/65_0/65_1; a `FIRST_VALUE(col) OVER (PARTITION BY col ORDER
BY col)` window round-trip view — round 64; a `UNION ALL` chain — round 92; a view chain — round
111). What they share is that each presents the same rows in a **different physical order** than the
base table. The workloads all contain `SUM`/`AVG`/`MIN`/`MAX`/`RANK` `OVER (ORDER BY …)` with tied
keys. Because dolt's default frame is ROWS (order-sensitive), base and equivalent disagree.

The bug does **not** require any equivalence construct — it reproduces on a single plain table
against MySQL (above). The equivalence oracle's role was to expose the *non-determinism*: a correct
(RANGE) engine would return the same values regardless of row order, so the query would be
admissible and the two sides would agree; dolt's ROWS default makes the query order-sensitive, and
the differing physical orders of base vs equivalent surface the divergence.

## Minimal oracle exposure path

- **Object composition arity:** `2`.
- **GCL builder path:** `SequenceOuterJoinQueryBuilder → CreateViewBuilder`.
- **Confidence:** high; both are exact current factory class names, and this path matches the
  report's split-rejoin-view findings.
- **Realization:** `CreateViewBuilder` persists the reordered, row-identical equivalent as a view.
- **Workload/data requirements (excluded from arity):** an aggregate window with implicit frame,
  duplicate `ORDER BY` keys, and values whose running aggregate changes are workload/data
  requirements.
- **Exposure vs. intrinsic trigger:** this is a representative minimal oracle exposure path: the
  split/rejoin view changes peer order and reveals non-determinism. The intrinsic default-frame bug
  needs no equivalence object and reproduces on a plain table against the reference engine.

## Characterization

- **Trigger**: `SUM`/`AVG` (any additive/averaging aggregate) with `OVER (ORDER BY <col>)`, no
  explicit frame, where `<col>` has duplicate values (peers).
- **Root cause**: default frame is `ROWS UNBOUNDED PRECEDING .. CURRENT ROW`; must be `RANGE`.
  Proven by controls — explicit `RANGE` gives the correct peer-equal `6,6,6`; explicit `ROWS` gives
  the same `4,6,3` as the bare `ORDER BY name`, i.e. the default resolves to ROWS.
- **Does NOT trigger / correct**: `COUNT(*)` (peer count is unaffected), explicit `RANGE`,
  `OVER ()` (no ORDER BY → whole partition).
- Not a crash; assertions-off build, irrelevant.

## How it was found

The eqgen **data-equivalence oracle** ran each window workload against the base table and a
row-identical equivalent (e.g. the `eq_seq_key` split-rejoin view). Under a correct RANGE default
the results would be identical regardless of physical row order; dolt's ROWS default makes them
order-sensitive, so the two relations returned different multisets and the oracle flagged a mismatch.
Comparing dolt to MySQL on a single table then showed the values are outright wrong, not merely
reordered. This is a case where the oracle's manufactured ground truth (two row-identical relations
that *must* agree) doubles as a non-determinism detector: it caught a semantic frame bug that a
single-query fuzzer would only notice with a reference engine.

- **Seeds / findings**: 1584990898 (round37_0), 1900985466 (round46_0), 1244563976 (round64_0),
  1498099867 (round65_0, round65_1), 835904821 (round92_0), 1709259289 (round111_0). The dominant
  root cause across these is the ROWS-vs-RANGE default-frame bug; individual findings may also
  involve related window mis-computation (e.g. round64's `FIRST_VALUE` round-trip view) — not
  separately reduced.
- Reduced repro: [`reduced.sql`](./reduced.sql).
- Original findings: hunt log
  and `mismatch_round{37_0,64_0,65_0,65_1,92_0,111_0}.sql`.
