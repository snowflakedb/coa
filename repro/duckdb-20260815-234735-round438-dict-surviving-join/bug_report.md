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

# DuckDB: dict-surviving hash join InternalException on a RIGHT join whose build is a nested-loop of two identity hash joins

## Summary

`JoinHashTable::PinDictSurvivingColumn` (`join_hashtable.cpp:551`) throws

`INTERNAL Error: dict-surviving join: narrowed column 3 received a non-global-dictionary chunk; build pipeline is not single-source`

when a hash-join build has already narrowed a payload slot from the first chunk's global dictionary and a later chunk is not a global dictionary. That is the intended safety net (casting a non-dict vector as dict indices is UB). The defect is that `CanUseDictSurvivingJoin` admitted the join: `BuildSideHasMultipleSources` (`physical_hash_join.cpp:301`) only treats `UNION` / recursive CTE as multi-source, and the comment claims it "never misses one". The distilled plan's build subtree is a **Nested Loop Join of two identity hash joins** — two producer pipelines, not a UNION — so the gate returns false, the first chunk narrows column 3, and the next chunk throws.

`SET disabled_optimizers='filter_pushdown'` restores the two-row result. Disabling `unused_columns` or `build_side_probe_side` also avoids the plan. DuckDB 1.5.0 does not have this optimizer and already returns the two rows.

## Environment

- **DuckDB v2.0.0-alpha37826 (Cyanoptera)** `a9f869b6a7` — eqgen CLI
  `duckdb`.
- Same InternalException on the older eqgen CLI `v2.0.0-alpha37080` `e85c4d27d7`.
- Access path: CLI `:memory:`. No `sql_mode`/collation.
- Python `duckdb` 1.5.0 wheel: distilled query returns `[(None, None), (None, None)]`.
- Local `duckdb` checkout at `2828abd8` still has the same `BuildSideHasMultipleSources` body (UNION / recursive CTE only). The tested CLI commit is not in that checkout.

## Minimal repro

See [`reduced.sql`](./reduced.sql). The INTERNAL Error invalidates the in-memory database; run the controls first, then the last `SELECT` in a fresh session.

```sql
CREATE TABLE t__base (
  c_pk BIGINT NOT NULL,
  c_int BIGINT,
  c_big DECIMAL(38, 0),
  c_txt VARCHAR,
  c_date DATE,
  c_ts TIMESTAMP
);
INSERT INTO t__base VALUES (3, NULL, -7, NULL, DATE '2030-06-01', NULL);
INSERT INTO t__base VALUES (8, -1, 1, 'x', DATE '2024-01-15', TIMESTAMP '1999-12-31 23:59:59');
CREATE TABLE t25 AS SELECT c_pk, 1 AS flag FROM t__base;
CREATE VIEW t AS
SELECT l.c_pk AS c_pk, l.c_int AS c_int, l.c_big AS c_big,
       l.c_txt AS c_txt, l.c_date AS c_date, l.c_ts AS c_ts
FROM t__base l
INNER JOIN t25 r ON l.c_pk = r.c_pk
WHERE r.flag = 1;

SELECT t3.c_txt, t3.c_big
FROM t t1
RIGHT OUTER JOIN t t2 ON t1.c_int <= t2.c_int
LEFT OUTER JOIN t t3 ON t1.c_date = t3.c_ts;
-- INTERNAL Error: dict-surviving join: narrowed column 3 received a
-- non-global-dictionary chunk; build pipeline is not single-source
```

`flag` is constantly 1, so `WHERE r.flag = 1` is tautological; `WHERE TRUE` on the same view is clean. `TRY_CAST(t3.c_txt AS TIME)` from the original finding is not required.

## Expected vs actual

| Query | Expected | Actual |
|---|---|---|
| distilled `SELECT t3.c_txt, t3.c_big` 3-way join over the identity-join **view** | 2 rows `(NULL, NULL)` | `INTERNAL Error` (column 3) |
| same + `disabled_optimizers='filter_pushdown'` | 2 rows `(NULL, NULL)` | 2 rows |
| same + `unused_columns` or `build_side_probe_side` off | 2 rows | 2 rows |
| heap table, same 2 rows, same query | 2 rows | 2 rows |
| `CREATE TABLE t AS` the identity join (not a view) | 2 rows | 2 rows |
| identity-join view with `WHERE TRUE` instead of `WHERE flag = 1` | 2 rows | 2 rows |
| `INTEGER` (or `DECIMAL(10,2)`) in place of `DECIMAL(38,0)` | 2 rows | 2 rows (plans as Hash Join **LEFT**, which dict-surviving refuses) |
| `HUGEINT` or `VARCHAR` in place of `DECIMAL(38,0)` | 2 rows | `INTERNAL Error` |
| project only `t3.c_txt` or only `t3.c_big` | rows | rows (clean) |
| `INNER`/`LEFT` in place of the `RIGHT OUTER` inequality join | rows | rows |
| DuckDB 1.5.0 wheel | 2 rows | 2 rows |

**Which side is wrong:** the **equivalent**. Base (heap) returns two `(NULL, NULL)` rows; the identity-join view throws. Ground truth is the heap / CTAS / `filter_pushdown` off / 1.5.0, which all agree. `SELECT * FROM t` is row-identical and type-identical (`c_pk BIGINT NOT NULL` included) on the distilled construction.

This is a one-sided **internal error**, not a crash: the process survives. The CLI backtrace is unresolved addresses on this release build; the actionable site is `join_hashtable.cpp:551` / `physical_hash_join.cpp:301`.

## Equivalence construction

**Round 438** (`error_round438_0.sql`): 8-row heap `t`, long equivalent (UNION ALL key-dedup, struct pack, ENUM, `EXCEPT ALL`, `ATTACH` mirrors, tag-delete, …) ending in the flag-table identity join the builders emit:

```sql
CREATE VIEW t AS
SELECT l.c_pk AS c_pk, … FROM t__base_table_24 l
INNER JOIN t__base_table_25 r ON l.eq_uid_1 = r.eq_uid_1
WHERE r.eq_flag_1 = 1;
```

`SELECT * FROM t` is row-identical (8 rows). Types match except the original chain dropped `NOT NULL` on `c_pk`; that is not load-bearing (the distilled view keeps `NOT NULL` and still throws). Workload:

```sql
SELECT TRY_CAST(t3.c_txt AS TIME), t3.c_big
FROM t t1
RIGHT OUTER JOIN t t2 ON t1.c_int <= t2.c_int
LEFT OUTER JOIN t t3 ON t1.c_date = t3.c_ts;
```

Load-bearing composition: **inlined identity hash-join view × tautological `WHERE flag = 1` × `RIGHT OUTER JOIN` on `<=` × `LEFT OUTER JOIN` on `date = ts` × projecting both a VARCHAR and a wide payload (`DECIMAL(38,0)` / `HUGEINT` / `VARCHAR`)**.

Reduced away: `TRY_CAST`, `ROW_NUMBER` uid (join on `c_pk` is enough), self-aliases are *not* required, 6 of 8 seed rows, `c_dec`/`c_dbl`/`c_chr`, struct/ENUM/`ATTACH`/`EXCEPT ALL`. A `SEMI JOIN` identity and a trivial `SELECT *` view are clean.

`DECIMAL(38,0)` vs `INTEGER` is a **plan** lever, not a type bug: INTEGER plans the `date = ts` join as Hash Join **LEFT** (`CanUseDictSurvivingJoin` returns false for `JoinType::LEFT`). DECIMAL/HUGEINT/VARCHAR plan it as Hash Join **RIGHT**, which is eligible.

## Minimal oracle exposure path

- **Object composition arity:** 4.
- **GCL builder path:** `CreateTableBuilder` [row key] → `FlagTableJoinQueryBuilder` [flag `TABLE`] → `CreateViewBuilder`.
- **Confidence:** Exact against the report SQL and current GCL.
- **Realization:** one CTAS materializes keyed rows, the join builder creates its flag CTAS, and the filtered identity join is exposed as an inlinable `VIEW`.
- **Workload/data requirements (excluded from arity):** the outer joins must produce an eligible RIGHT hash join with a multi-source nested-loop build; the projection needs both a `VARCHAR` and a wide payload, with enough rows/values to select that plan.

**Exposure vs. intrinsic trigger:** Here the flag-join view is part of the intrinsic failing plan, because inlining it creates the filtered multi-source build that the dict-surviving gate misclassifies. The outer join directions, payload widths, and data-dependent plan choice complete the trigger but are workload/data conditions rather than object factors.

## Characterization

**Trigger:** Hash Join **RIGHT** whose **build** is a Nested Loop Join of two identity `c_pk = c_pk` hash joins, with a filter still sitting on those identity joins (`WHERE r.flag = 1`). Distilled `EXPLAIN`:

```
Hash Join  Join Type: RIGHT  Conditions: c_ts = CAST(c_date AS TIMESTAMP)
  Empty Result                          -- probe
  Nested Loop Join  Join Type: RIGHT  Conditions: c_int <= c_int   -- build
    Hash Join INNER  t__base ⋈ t25 ON c_pk
    Hash Join INNER  t__base ⋈ t25 ON c_pk
```

`JoinType::RIGHT` is allowed (`physical_hash_join.cpp:578`); `LEFT` and `OUTER` are not. `BuildSideHasMultipleSources` walks the build subtree looking only for `UNION` / `RECURSIVE_CTE` / `RECURSIVE_KEY_CTE`. Nested Loop Join is invisible, so the first build chunk is allowed to narrow payload column 3. A later chunk from the other identity-join pipeline is not a global dictionary → `PinDictSurvivingColumn` throws rather than flatten (comment: the `DictionaryBuffer` cast would be UB).

The in-tree test `test/sql/join/hash_join/hash_join_dict_surviving.test` covers the UNION ALL exclusion ("multiple producers break the decide-once-on-first-chunk layout contract") and does not cover NLJ-as-build.

**Does not trigger (one control each):**

- Heap / trivial view / CTAS of the identity join (no inlined join in the outer plan).
- `WHERE TRUE` instead of `WHERE flag = 1` (no filter to push; `flag <> 0` still throws).
- `INNER` or `LEFT` for the inequality join; equality `c_int = c_int`; `ON TRUE`.
- Projecting a single t3 column, or `SELECT *`.
- `INTEGER` / `DECIMAL(10,2)` payload (LEFT hash join).
- `SET threads=1` still throws (not a race).

**Masks:** `filter_pushdown`, `unused_columns`, `build_side_probe_side`. On the distilled 2-row plan, `join_order` and `statistics_propagation` do **not** mask (they did on the original 8-row chain).

**DML:** `DELETE FROM t__base WHERE c_pk IN (<the SELECT>)` did not throw (different plan). The bug is the SELECT hash-join sink.

**Suggested fix:** treat nested-loop / any multi-child build operator as multi-source in `BuildSideHasMultipleSources`, *or* fall back to native slot width when a later chunk is not a global dictionary instead of throwing. The throw should stay as a last-resort assertion for a true producer bug.

## How it was found

eqgen data-equivalence oracle, `--rich --shuffle-corpus`, Duck shuffle3
`duck_rich_shuffle3/duckdb_20260815-234735/error_round438_0.sql`, seed 78528759.
Base ran; equivalent raised. `replay_eqgen.py`: divergence yes, `t` row-identical (8 rows), determinism skipped (one-sided error). Only `error_*` in that run; the other shuffle3 hits are known `SUBSTR` ASCII-stats, duplicate-`PARTITION BY` window-self-join, and `MEDIAN` partial-aggregate-pushdown.

A query-rewrite oracle that kept `t` as a heap table would miss it: the heap path is clean.

## Open items

- Not verified on a debug build of current `main` (local HEAD `2828abd8` still has the NLJ-blind gate in source; the tested CLI hash `a9f869b6a7` is not in that checkout).
- Whether `PhysicalOperatorType` for IEJoin / piecewise merge / cross product should also count as multi-source was not enumerated; NLJ is the one in this plan.
- No GitHub issue filed from this writeup.
