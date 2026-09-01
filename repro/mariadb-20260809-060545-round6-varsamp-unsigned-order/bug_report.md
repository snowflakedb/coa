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

# MariaDB: `VAR_SAMP` / `VAR_POP` / `STDDEV_SAMP` over `BIGINT UNSIGNED` near 2^64 are insertion-order dependent (same multiset → `4194304.0` or `0.0`)

## Summary

For two `BIGINT UNSIGNED` values that sit near `2^64` and differ by only 1764 (one DOUBLE ULP at that magnitude is 2048), `VAR_SAMP` — and the sibling aggregates `VAR_POP` / `VARIANCE` / `STDDEV_SAMP` — return a result that depends on the physical scan order of those two rows. One order yields `4194304.0` (= `(2048.0)^2`); the reverse order yields `0.0` even though `MIN(sh) <> MAX(sh)`. An aggregate over a multiset must not depend on insertion order, and `0.0` is impossible whenever the two inputs are unequal. The same defect appears when the values are produced by `c_int << c_pk` on negatives (MariaDB types `<<` as `BIGINT UNSIGNED`). MySQL 9.7.2 shows identical behaviour.

## Environment

| | |
|---|---|
| Engine | `mariadb 11.4.12-MariaDB-ubu2404` (docker `mariadb:11.4`) |
| Also reproduces on | `mysql 9.7.2` (docker) — same numbers, same order flip |
| Access path | server via pymysql (eqgen adapter); not a client artefact |
| Session | defaults. `sql_mode` is **irrelevant** — verified under `''` and under the fuzzer's `STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES` |
| Collation / charset | `utf8mb4_bin` / `utf8mb4` (not load-bearing) |
| Regression window | **not determined** — only 11.4.12 / 9.7.2 exercised here |

## Minimal repro

```sql
CREATE TABLE t (sh BIGINT UNSIGNED);
INSERT INTO t VALUES (18446744073709551588), (18446744073709549824);
SELECT VAR_SAMP(sh) FROM t;   -- 4194304.0

CREATE TABLE t (sh BIGINT UNSIGNED);
INSERT INTO t VALUES (18446744073709549824), (18446744073709551588);
SELECT VAR_SAMP(sh) FROM t;   -- 0.0
```

Those two UNSIGNED literals are what MariaDB stores for `(-7)<<2` and `(-7)<<8`. Equivalent spelling via the shift:

```sql
CREATE TABLE t (c_pk BIGINT, c_int BIGINT);
INSERT INTO t VALUES (2, -7), (8, -7);
SELECT VAR_SAMP(c_int << c_pk) FROM t;   -- 4194304.0

CREATE TABLE t (c_pk BIGINT, c_int BIGINT);
INSERT INTO t VALUES (8, -7), (2, -7);
SELECT VAR_SAMP(c_int << c_pk) FROM t;   -- 0.0
```

## Expected vs actual

The two UNSIGNED inputs convert to DOUBLEs that differ by one ULP (`2048.0`). The IEEE sample variance of that pair is `2097152.0` (= `(2048)^2 / 2`); the population variance is `1048576.0`. Neither insertion order returns those.

| Query | Expected | Actual |
|---|---|---|
| `VAR_SAMP(sh)` with larger value inserted first | same answer as the other order; ≈ `2097152.0` | `4194304.0` |
| `VAR_SAMP(sh)` with smaller value inserted first | same answer as the other order; ≈ `2097152.0` | `0.0` |
| `VAR_POP(sh)` / larger first | ≈ `1048576.0`, order-invariant | `2097152.0` |
| `VAR_POP(sh)` / smaller first | same | `0.0` |
| `MIN(sh)=MAX(sh)` on either table | `0` (values differ by 1764) | `0` — so the `0.0` variance is not “equal inputs” |
| `VAR_SAMP(c_int<<c_pk)` for `(1,3),(2,3)` either order | `18.0` | `18.0` — small values are fine |
| finding group `c_int=-7 AND c_big=2` over RankModUnion CTAS | same as plain CTAS | `0.0` vs plain `4194304.0` |

**Which side of the finding was wrong.** The oracle compared a plain base table (scan order `c_pk=2` then `8` → `4194304.0`) against an equivalent whose RankModUnion CTAS reordered the same two group members (`8` then `2` → `0.0`). Both answers are wrong relative to the IEEE reference; the equivalent’s `0.0` is the clearly inadmissible one (`MIN≠MAX`). The divergence is real either way: a multiset aggregate may not change when only physical order changes.

## Equivalence construction

**(1) Concrete construct as emitted.** Finding
`mariadb_hunt_20260809-060545/mariadb_20260809-060558/mismatch_round6_0.sql`
(seed `265726473`). Same-base fork round; the workload is

```sql
SELECT VAR_SAMP(((t2.c_int)<<(t2.c_pk))), ((t2.c_int)%(t2.c_big)), (+ t2.c_int), (+ t2.c_big)
FROM t2
GROUP BY ((t2.c_int)%(t2.c_big)), (+ t2.c_int), (+ t2.c_big)
ORDER BY t2.c_pk;
```

Lineage bisection of `t2` pins the flip on

```sql
CREATE TABLE t__base_table_42 AS
  SELECT …, ROW_NUMBER() OVER (ORDER BY c_pk) AS eq_rank_3 FROM t__base;
-- four MOD(eq_rank_3, 4) = k views, then
CREATE TABLE t__base_table_43 AS
  SELECT * FROM v0 UNION ALL SELECT * FROM v1 UNION ALL SELECT * FROM v2 UNION ALL SELECT * FROM v3;
```

(`RankModUnionQueryBuilder`.) Exposing `t2` from `t__base_table_42` still yields `4194304.0`; exposing from `t__base_table_43` yields `0.0`. HEX dumps of `(c_int<<c_pk)` on both relations are identical (`FFFFFFFFFFFFFFE4`, `FFFFFFFFFFFFF900`); only scan order differs.

**(2) Load-bearing construct.** Not a construct×query composition in the optimizer sense — a **shape×cardinality that changes physical order**. The RankModUnion (or any `UNION ALL` / reverse `INSERT` order) is only the vehicle that reorders the two group members. Distilling away the whole equivalence chain to two `INSERT` orders on a one-column table still reproduces the bug. Small values (`3<<1`, `3<<2`) are a negative control: both orders return `18.0`.

**(3) Reduced away.** Indexes, `CASE WHEN TRUE` / `COALESCE` identity columns, InnoDB materialize-views, later COALESCE/CTAS wrappers, the `GROUP BY` / `ORDER BY` / `%` / unary `+` in the workload (the filtered two-row `VAR_SAMP` is enough), and `sql_mode`.

## Minimal oracle exposure path

- **Object composition arity:** **2**
- **GCL builder path:** `RankModUnionQueryBuilder` → `CreateTableBuilder`
- **Confidence:** Verified — the report names `RankModUnionQueryBuilder`, bisects its emitted rank-modulus union, and identifies the CTAS that materializes it.
- **Realization:** `CreateTableBuilder` materializes the residue-ordered `UNION ALL`, preserving the reordered scan sequence in the final table.
- **Workload/data requirements (excluded from arity):**
  - `VAR_SAMP`/related variance aggregation over `BIGINT UNSIGNED` values near `2^64`.
  - Values whose DOUBLE images are separated by roughly one ULP.
  - A physical order that changes which value the aggregate sees first.

**Exposure vs. intrinsic trigger:** The rank-modulus union plus CTAS exposes the defect by reordering a row-identical multiset. The intrinsic trigger is variance aggregation × physical row order over precision-edge unsigned values; reverse `INSERT`s on a plain table reproduce it after both builders are removed.

## Characterization

- **Triggers:** `VAR_SAMP` / `VAR_POP` / `VARIANCE` / `STDDEV_SAMP` over `BIGINT UNSIGNED` values whose DOUBLE images sit near `2^64` and differ by ~1 ULP; result depends on which row the aggregate sees first.
- **Does not trigger:** the same aggregate over small integers (mantissa-exact); `MIN`/`MAX`/`SUM`/`AVG`/`COUNT` on the same column (order-invariant and correct); `CAST(sh AS CHAR)` / `HEX(sh)` (stored bytes identical across orders).
- **Masks:** inserting in the “large then small” order, or `ORDER BY` that forces that scan — still returns the inflated `4194304.0`, not a correct answer; it only hides the `0.0` failure mode.
- **DML:** not applicable (read-only aggregate). No data loss.
- **Plan:** `EXPLAIN` is a plain `ALL` table scan on both orders — the defect is in the running aggregate’s floating-point update, not in plan shape.
- **Likely mechanism:** a one-pass `sum(x²) − (sum x)²/n` (or equivalent) in DOUBLE on values ~`1.84e19`. Catastrophic cancellation yields `0` for one accumulation order; the other order yields `(Δ_float)²` for `VAR_SAMP` and `(Δ_float)²/2` for `VAR_POP` — i.e. each is about 2× the IEEE reference. A compensated algorithm (Welford) would be order-invariant and land near `2097152` / `1048576`.

## How it was found

eqgen’s data-equivalence oracle: same workload query over a plain base table vs a row- and type-identical RankModUnion rewrite. Gates on the original finding (replay_adapter.py): divergence reproduces; `t0`/`t1`/`t2` row-identical and type-identical; stable across 4 repeats per side; differing cells are engine-unequal. A query-rewrite oracle that held the table fixed would miss it — the trigger is the rewrite’s change of physical order over values the aggregate cannot handle stably. One finding in the hunt run accounts for this bug.

- reduced: `repro/mariadb-20260809-060545-round6-varsamp-unsigned-order/reduced.sql`
- original: `mariadb_hunt_20260809-060545/mariadb_20260809-060558/mismatch_round6_0.sql`

## Open items

- Regression window across MariaDB 10.x / 11.x / 12.x and MySQL 8.0 / 8.4 not mapped.
- Engine source `file:line` for the variance accumulator not pinned (no MariaDB tree on this machine matching 11.4.12).
- Whether `CAST(sh AS DECIMAL(…))` before `VAR_SAMP` is a usable workaround was not checked.
- Upstream tickets not opened yet — this folder is the filing-ready package.
