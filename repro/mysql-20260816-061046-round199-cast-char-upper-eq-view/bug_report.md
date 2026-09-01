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

# MySQL: merged `CAST(col AS CHAR) COLLATE utf8mb4_0900_bin` makes `col = UPPER(col)` true while `UPPER(col)` is a different string

## Summary

A mergeable view (or derived table) whose column is `CAST(c_chr AS CHAR(255)) COLLATE utf8mb4_0900_bin` — including eqgen's JSON-unpack view, which emits exactly that CAST+COLLATE — evaluates

```sql
c_chr = UPPER(c_chr)     -- also c_chr <= UPPER(c_chr), c_chr = LOWER(c_chr), c_chr NOT IN (LOWER(c_chr))
```

as **TRUE** for the row `'a'`. `UPPER(c_chr)` **prints** as `'A'` (`HEX` `41`), `COLLATION(c_chr)` reports `utf8mb4_0900_bin`, and `c_chr = 'A'` is **FALSE**. Under a binary collation `'a' = 'A'` must be false, so `col = UPPER(col)` cannot be true if `UPPER(col)` is `'A'`. The optimizer is treating `UPPER`/`LOWER` as an identity for equality/comparison against the merged CAST item, while still computing the folded string for projection.

The base table, an identity view, `ALGORITHM=TEMPTABLE`, `derived_merge=off`, and a tautology-`CASE` view are all correct (`eq_upper = 0`). `EXPLAIN FORMAT=TREE` of the buggy form is a table scan on `b` (the CAST is inlined). This is execution/rewrite after derived-merge, not a collation reported wrongly by `COLLATION()`.

## Environment

- **Version:** MySQL `9.7.2` (Docker `mysql:9.7.2`).
- **`sql_mode`:** `STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES` — not load-bearing.
- **charset / collation:** `utf8mb4` / `utf8mb4_0900_bin`. The view column's `COLLATE utf8mb4_0900_bin` is load-bearing in the sense that it is what the JSON builder emits; a `CAST AS CHAR` *without* COLLATE becomes `utf8mb4_0900_ai_ci` (connection default) and then case-insensitive compares are *expected* — that is a type-equivalence issue, not this bug. This report is the case where `COLLATION()` **says bin** and compares still fold.

## Minimal repro

See [`reduced.sql`](./reduced.sql) PART 2:

```sql
CREATE TABLE b (c_chr VARCHAR(255) COLLATE utf8mb4_0900_bin);
INSERT INTO b VALUES ('a');
CREATE VIEW t AS
  SELECT CAST(c_chr AS CHAR(255)) COLLATE utf8mb4_0900_bin AS c_chr FROM b;

SELECT c_chr, HEX(c_chr), UPPER(c_chr), HEX(UPPER(c_chr)),
       c_chr = UPPER(c_chr) AS eq_upper,
       c_chr = 'A'          AS eq_lit
FROM t;
```

An inline derived table of the same CAST diverges the same way (no `VIEW`).

## Expected vs actual

For the single row `'a'` (`HEX` `61`). `UPPER('a')` is `'A'` (`HEX` `41`). `utf8mb4_0900_bin` is codepoint order, so `'a' = 'A'` is FALSE.

| Query | Expected `eq_upper` | Actual |
|---|---|---|
| CAST+COLLATE view / derived table (PART 2) | 0 | **1** |
| JSON-unpack view (PART 1), same CAST+COLLATE | 0 | **1** |
| `c_chr = 'A'` on that view | 0 | 0 (literal compare is fine) |
| `HEX(UPPER(c_chr))` on that view | `41` | `41` (projection is fine) |
| heap / identity view / `ALGORITHM=TEMPTABLE` / `derived_merge=off` / tautology CASE view | 0 | 0 |
| `CAST(c_chr <= UPPER(c_chr) AS SIGNED)` (275_203) | 0 | **1** |
| `COUNT(c_date) OVER (PARTITION BY c_chr NOT IN (LOWER(c_chr)))` (300_130) | NULL-chr group COUNT 0; `'A'` not in same group as `'a'` | **LOWER is treated as identity → one partition, COUNT 3** |

The **equivalent** (merged CAST CHAR) is the wrong side. The **base table** is correct.

## Equivalence construction

`mismatch_round199_1.sql` (seed 106570822) ended equivalent `t` as the JSON_OBJECT / JSON_EXTRACT unpack view with `CAST(JSON_UNQUOTE(…) AS CHAR(255)) COLLATE utf8mb4_0900_bin`. The workload projected `CAST(NULLIF((c_chr || IFNULL(c_chr,c_chr)) LIKE '%a%', c_txt >= REPEAT(…)) AS SIGNED)`: heap NULL, JSON 0. That fragment is a noisy boolean over the same CHAR column; the distilled `col = UPPER(col)` is the same merge.

- **Load-bearing construct:** derived-merge of `CAST(varchar AS CHAR(255)) COLLATE utf8mb4_0900_bin` (view or derived table). JSON packing is not required.
- **Composition:** that merged item **×** `UPPER`/`LOWER` (or `NOT IN (LOWER(…))`) in a **comparison with the column itself**. Comparing the column to a **literal** `'A'` does not fire. `LIKE` does not fire.
- **Reduced away:** the rest of the 199_1 SELECT list, `||` / `LIKE` / `REPEAT` / `NULLIF`, extra rows, JSON.

`EXPLAIN FORMAT=TREE` of `SELECT c_chr <= UPPER(c_chr) FROM t`: `-> Table scan on b`. View merge has already inlined `cast(b.c_chr as char(255))`.

## Minimal oracle exposure path

- **Object composition arity:** **2**
- **GCL builder path:** `MySqlJsonPackRoundTripBuilder` → `VIEW` realization
- **Confidence:** Verified — the report names the builder's JSON-unpack output and the GCL implementation confirms its hardcoded view realization.
- **Realization:** The builder internally CTASes the input and exposes the unpacked `CAST(... AS CHAR) COLLATE utf8mb4_0900_bin` column through a mergeable `VIEW`.
- **Workload/data requirements (excluded from arity):**
  - A merged `CAST(... AS CHAR) COLLATE utf8mb4_0900_bin` column.
  - A self-comparison against `UPPER`/`LOWER` of that column.
  - Data whose case-folded value differs, such as `'a'` versus `'A'`.

**Exposure vs. intrinsic trigger:** The JSON builder supplied the expression and merge boundary that exposed the defect, but JSON is not required. The intrinsic trigger is derived/view merge of the binary-collated CHAR cast combined with the self `UPPER`/`LOWER` comparison.

## Characterization

Verified against `mysql:9.7.2`.

**Required**

1. Derived-merge of `CAST(… AS CHAR(255)) COLLATE utf8mb4_0900_bin` (or the JSON_UNQUOTE form of the same). Identity view does not diverge. `CASE WHEN TRUE THEN c_chr ELSE CAST(NULL AS CHAR(255)) END COLLATE utf8mb4_0900_bin` does not diverge.
2. A comparison of the merged column **to `UPPER`/`LOWER` of itself** (`=`, `<=`, `NOT IN (LOWER(col))`).
3. `derived_merge` on. `SET optimizer_switch='derived_merge=off'` and `ALGORITHM=TEMPTABLE` restore the heap answer.

**Not required / not the bug**

- `COLLATION(c_chr)` already reports `utf8mb4_0900_bin` on the buggy view — this is not “CAST dropped the COLLATE clause”.
- `WHERE c_chr = 'A'` is 0 rows (correct). The defect is not “the column is secretly `ai_ci` for all compares”.
- `LIKE '%A%'` / `LIKE '%a%'` match the heap.
- `UCASE`/`LCASE` projection matches `UPPER`/`LOWER` projection (`'A'` / `'a'`).

**Likely mechanism.** After merge, equality (and `<=`) between the CAST item and `UPPER`/`LOWER` of that item is rewritten as a tautology (`col = col`), while the projection of `UPPER` still case-folds. That is why `HEX(UPPER(c_chr)) = 41` and `c_chr = UPPER(c_chr)` are both true. `derived_merge=off` keeps a materialization boundary and the rewrite does not fire.

DML: `WHERE c_chr = 'A'` is correct, so a search for `'A'` does not hit extra rows. `WHERE c_chr = UPPER(c_chr)` on this view would update/delete **every** non-NULL row (wrong). Not separately tested.

## How it was found

Eqgen data-equivalence oracle, `mysql_rich_shuffle2` / `mysql_20260816-061046`. After stripping the HAVING / regexp / LATERAL / DISTINCT+GB / SIGSEGV clusters, the leftover unique queries that still replayed were this CHAR CAST+COLLATE merge:

| Finding | Symptom | Same root |
|---|---|---|
| `mismatch_round199_1.sql` | `CAST(NULLIF(boolean, …) AS SIGNED)` NULL vs 0 on `'Zed'`/`'a'` | yes (boolean over the CHAR column) |
| `mismatch_round275_203.sql` (and 469_143, 602_201) | `CAST(c_chr <= UPPER(c_chr) AS SIGNED)` 0 vs 1 | yes |
| `mismatch_round300_130.sql` | window `COUNT` 3 vs 4 via `PARTITION BY c_chr NOT IN (LOWER(c_chr))` | yes (`LOWER` tautology merges partitions) |
| `mismatch_round35_94.sql` | `WHERE c_chr NOT IN (CASE WHEN c_chr IN (LEFT(UPPER(c_chr), …)) THEN … ELSE c_chr END)` — heap 0 rows, CAST CHAR view 1 row | yes (`UPPER` tautology) |
| `mismatch_round191_115.sql` | `REPLACE(c_chr, c_chr, MIN(REPLACE(REPEAT(c_txt, …), …)))` GROUP BY — JSON/CAST CHAR orig still disagree; identity matches. Distilled `c_chr = UPPER(c_chr)` fires on the same CAST view. The original `REPEAT`/`REPLACE` fragment was **not** reduced onto `col = UPPER(col)` (plain `MIN(REPEAT)` matches); treat as the same CAST CHAR merge family | same construct, original SELECT not fully distilled |
| `mismatch_round170_189.sql` | JSON equivalent chain, 8 vs 8 different values. Identity and CAST CHAR **original query** match the heap. Seed still exhibits `c_txt = UPPER(c_txt)` on CAST CHAR. Original query has no `UPPER`/`LOWER` | seed is this bug; original query not shown to be this bug |

Gates: row-identical equivalent, type-identical at `cursor.description`, deterministic, engine-unequal diffs. Collation of the *declared* column matches; the bug is the compare rewrite, which that gate cannot see.

## Open items

- Optimizer rule / `file:line` not named. Starting point: derived-merge of `Item_typecast_char` + `COLLATE utf8mb4_0900_bin`, then equality with `Item_func_ucase`/`Item_func_lcase` rewritten to tautology.
- `mismatch_round191_115.sql` original CAST CHAR query still disagrees; `col = UPPER(col)` fires on that view but the original `REPEAT`/`REPLACE` SELECT was not reduced onto it.
- `mismatch_round170_189.sql` original query matches heap on identity and CAST CHAR; only the finding's full JSON chain diverges. Not claimed as this bug's original query.
