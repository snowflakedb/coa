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

# TiDB: `STDDEV_POP` / `VAR_*` over `BIGINT UNSIGNED` near `2^64` wrong on multi-way `UNION ALL`

## Summary

Eight consecutive `BIGINT UNSIGNED` values just below `2^64` all map to the **same** IEEE-754 DOUBLE (ULP at that magnitude is 2048). Therefore `STDDEV_POP` / `VAR_POP` / `VAR_SAMP` / `VARIANCE` / `STDDEV_SAMP` over that multiset must be `0.0`.

On a base table (or a materialized copy of a `UNION ALL`), TiDB correctly returns `0.0`. Over a **≥3-way `UNION ALL`** of the same rows (e.g. `MOD(sh,4)=0/1/2/3` partitions), the same aggregates return a **nonzero** value (`STDDEV_POP` ≈ `443.405`). A 2-way `UNION ALL` still returns `0.0`. MySQL 9.7.2 and MariaDB 11.4 return `0.0` for the 4-way form.

The same wrong nonzero appears when the values are produced as `(~ c_pk)` for signed `c_pk ∈ {1..8}` — MySQL-family bitneg of `SIGNED` yields `UNSIGNED` near `2^64`.

## Environment

| | |
|---|---|
| Engine | `tidb 8.0.11-TiDB-v8.5.0` (docker `pingcap/tidb:v8.5.0`) |
| Not reproduced on | MySQL 9.7.2, MariaDB 11.4.12 (4-way UNION → `0.0`) |
| Access path | pymysql via eqgen `TiDbAdapter` |
| Session | defaults / fuzzer `STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES` |
| Found by | eqgen sqlancerpp + typed predicates, hunt `tidb_hunt_20260809-175406/` |

## Minimal repro

```sql
CREATE TABLE u (sh BIGINT UNSIGNED);
INSERT INTO u VALUES
  (18446744073709551614),
  (18446744073709551613),
  (18446744073709551612),
  (18446744073709551611),
  (18446744073709551610),
  (18446744073709551609),
  (18446744073709551608),
  (18446744073709551607);

SELECT STDDEV_POP(sh) FROM u;
-- 0.0

SELECT STDDEV_POP(sh) FROM (
  SELECT sh FROM u WHERE MOD(sh, 4) = 0
  UNION ALL SELECT sh FROM u WHERE MOD(sh, 4) = 1
  UNION ALL SELECT sh FROM u WHERE MOD(sh, 4) = 2
  UNION ALL SELECT sh FROM u WHERE MOD(sh, 4) = 3
) AS x;
-- 443.40500673763256
```

Bitneg spelling (same numbers):

```sql
CREATE TABLE t (c_pk BIGINT NOT NULL);
INSERT INTO t VALUES (1),(2),(3),(4),(5),(6),(7),(8);
SELECT STDDEV_POP((~ c_pk)) FROM t;  -- 0.0

SELECT STDDEV_POP((~ c_pk)) FROM (
  SELECT c_pk FROM t WHERE MOD(c_pk, 4) = 0
  UNION ALL SELECT c_pk FROM t WHERE MOD(c_pk, 4) = 1
  UNION ALL SELECT c_pk FROM t WHERE MOD(c_pk, 4) = 2
  UNION ALL SELECT c_pk FROM t WHERE MOD(c_pk, 4) = 3
) AS x;
-- 443.40500673763256
```

## Why this is wrong

- The eight UNSIGNED inputs differ by at most 7; one DOUBLE ULP at magnitude `2^64` is 2048, so they are **identical** as `DOUBLE`. Population stddev/variance of a constant multiset is `0`.
- Materializing the 4-way `UNION ALL` into a table then aggregating yields `0.0` — only the **streaming HashAgg-over-Union** plan is wrong.
- `EXPLAIN` shows `HashAgg` over `Union` of four `TableReader` branches with `cast(..., double UNSIGNED BINARY)`.

## Minimal oracle exposure path

- **Object composition arity:** `2`.
- **GCL builder path:** `RankModUnionQueryBuilder → CreateViewBuilder`.
- **Confidence:** Inferred from the reduced multiway `MOD(rank, N)` partition and the report's
  RankMod/TLP attribution. Historical GCL AST metadata is not retained, so the exact original choice
  between RankMod and another partition-union builder is uncertain.
- **Realization:** a root view exposes the multiway rank/mod partition union.
- **Workload/data requirements (excluded from arity):** the variance-family aggregate, unsigned values
  near `2^64`, and the eight input rows are workload/data requirements and are not counted.
- **Exposure vs. intrinsic trigger:** the streaming multiway `UNION ALL` plan is intrinsic; the root
  view is exposure-only because an inline derived union reproduces, while table materialization masks
  the bug.

## Oracle notes (eqgen)

- Finding: `mismatch_round0_1.sql` (`SELECT STDDEV_POP((~ t1.c_pk)) FROM t1`).
- Forks `t0`/`t1`/`t2` are **row-identical** base vs equivalent; query still diverges → admissible.
- Many sibling mismatches in the same hunt (`VAR_SAMP(~…)`, `VARIANCE(~…)`, `STDDEV(~…)`) share this root cause (RankModUnion / TLP exposes multi-way `UNION ALL`).

## Verify

```bash
cd /path/to/eqgen
```
