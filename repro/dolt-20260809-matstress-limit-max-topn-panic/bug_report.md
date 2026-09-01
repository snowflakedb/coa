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

# Dolt: `ORDER BY` + huge `LIMIT` (≥ 2^62) panics in `GetTopNRows`

## Summary

```sql
SELECT * FROM t ORDER BY c_pk LIMIT 18446744073709551615;
```

raises a recovered panic:

```text
ERROR 1105: panic recovered: runtime error: makeslice: cap out of range
  sql/sorters.GetTopNRows → topRowsIter.Next
```

MySQL treats `LIMIT 18446744073709551615` (`UINT64_MAX`) as the “no limit”
sentinel when an `OFFSET` is present without a bound. Dolt’s top-N sorter
takes the limit as a signed capacity and blows up once the value is ≥ `2^62`.
Without `ORDER BY` the same limit is fine; with `ORDER BY` and a small limit
it is fine.

eqgen’s MySQL-protocol emitters use this sentinel for `OFFSET`-without-`LIMIT`
(and for remainder pages of `LimitChunkUnionQueryBuilder`), so mat-stress
hunts hit it immediately.

## Environment

| | |
|---|---|
| Affected | Dolt `8.0.31` / `DOLT_VERSION 2.2.3`, commit `a995f245c` (`v2.2.3-49-ga995f245c`), gms `v0.20.1-0.20260805191915-e5eafe0da809` |
| Source | eqgen mat_stress `dolt_matstress_20260810-000149` (build-time panic during LimitChunk / OffsetZero) |

## Minimal repro

See [`reduced.sql`](./reduced.sql):

```sql
CREATE TABLE t (c_pk BIGINT NOT NULL);
INSERT INTO t VALUES (1), (2), (3);
SELECT * FROM t ORDER BY c_pk LIMIT 18446744073709551615;
-- Expected: 3 rows
-- Actual:   ERROR 1105 makeslice: cap out of range
```

## Threshold

| `LIMIT` | `ORDER BY` | Result |
|---|---|---|
| ≤ `2^32−1` | yes | OK |
| `2^62` | yes | **PANIC** |
| `2^63−1` / `2^63` / `2^64−1` | yes | **PANIC** |
| `2^64−1` | no | OK |

## Minimal oracle exposure path

- **Object composition arity:** `0`.
- **GCL builder path:** none — no isolated equivalence-object chain is implicated.
- **Confidence:** high for arity 0; the report reduces the panic to one query over a plain table.
- **Realization:** none; no view/table rewrite is required beyond the ordinary input table.
- **Workload/data requirements (excluded from arity):** `ORDER BY`, a `LIMIT ≥ 2^62`, and the
  `UINT64_MAX` sentinel emitted by the mat-stress `LimitChunk`/`OffsetZero` workload path are query
  generation conditions, not counted object builders.
- **Exposure vs. intrinsic trigger:** no base/equivalent object contrast was established. The
  intrinsic trigger is allocating the top-N sorter from the huge limit; the mat-stress builder path
  only generated the exposing workload.

## Open items

- Harness: prefer a smaller “unbounded” sentinel on Dolt (or skip OffsetZero /
  LimitChunk remainder when it would emit `UINT64_MAX` with `ORDER BY`).
- File upstream against go-mysql-server `sql/sorters.GetTopNRows`.
