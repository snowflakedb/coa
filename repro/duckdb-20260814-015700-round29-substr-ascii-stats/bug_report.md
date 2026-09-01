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

# DuckDB: `SUBSTR` ASCII fast path clamps a hugely negative start; Unicode path returns empty

## Summary

`SUBSTRING`/`SUBSTR` is bound to two implementations. `SubstringPropagateStats` swaps in
`SubstringFunctionASCII` when column statistics say the string cannot contain Unicode; otherwise
the default Unicode implementation runs. For a start offset whose magnitude exceeds the string
length (e.g. `SUBSTR(s, -12345678, 12)` on `'abc'`), those two paths **disagree**:

- ASCII (`substring.cpp` `SubstringStartEnd`): `start = max(len + offset, 0)` clamps to the front
  of the string and returns `'abc'`.
- Unicode: a from-the-end character scan never finds that position and returns `''`.

DuckDB's documented contract for negative `SUBSTRING` offsets is from-the-end (duckdb#10721,
intentional vs PostgreSQL). The constant-folded call `SUBSTR('abc', -12345678, 12)` and the same
heap column with `statistics_propagation` disabled both return `''`. The ASCII fast path is the
wrong side: a statistics-gated optimisation changes the result.

## Environment

- **DuckDB v2.0.0-alpha37730 (Cyanoptera)** `c6f7f3e250` — eqgen CLI
  `duckdb`.
- Access path: CLI `:memory:`. No `sql_mode`/collation.

## Minimal repro

See [`reduced.sql`](./reduced.sql):

```sql
CREATE TABLE t(s VARCHAR);
INSERT INTO t VALUES ('abc');

SELECT SUBSTR(s, -12345678, 12) FROM t;   -- 'abc'  WRONG (ASCII stats)
SELECT SUBSTR('abc', -12345678, 12);      -- ''     correct (Unicode / constant fold)
```

`SET disabled_optimizers='statistics_propagation'` makes the heap query return `''` as well.

## Expected vs actual

| Query | Expected | Actual |
|---|---|---|
| `SUBSTR(s, -12345678, 12)` on heap VARCHAR `'abc'` | `''` | `'abc'` |
| `SUBSTR('abc', -12345678, 12)` (constant) | `''` | `''` |
| heap + `disabled_optimizers='statistics_propagation'` | `''` | `''` |
| `SUBSTR` on `CAST(CAST(s AS ENUM) AS VARCHAR)` | `''` | `''` |
| `SUBSTR` on `ANY_VALUE(s) … GROUP BY key` view | `''` | `''` |
| `SUBSTR(s, n, 2)` for `n ∈ {-3,-2,-1,0,1}` heap vs constant | agree | agree |
| `SUBSTR(s, 15, -7)` on heap `'abc'` | `''` | `'abc'` |
| `SUBSTR('abc', 15, -7)` (constant) / heap + no stats | `''` | `''` |

**Which side is wrong:** the **base table** (ASCII-stats fast path). The equivalent in both
findings lost ASCII stats (round 29: `ANY_VALUE`+`UNION ALL` key-dedup view; round 47: `ENUM`
round-trip on `name`) and therefore ran the Unicode path, matching constants. Ground truth is
the Unicode / no-stats answer, which is also DuckDB's from-the-end contract.

## Equivalence construction

1. **Round 29** (`mismatch_round29_0.sql`): `ROW_NUMBER` key, `UNION ALL` self-duplicate,
   `ANY_VALUE … GROUP BY eq_key` (twice). Workload was a 3-way `FULL OUTER JOIN` whose only
   disagreeing column was `ASCII(sha256(SUBSTR(t3.name, t2.id, TRUNC(t1.id))))`. `sha256('')`
   starts with `e` (`ASCII` 101); that is exactly the equivalent's constant 101 vs the base's
   54/97/99. The joins are not load-bearing — `SUBSTR` on the rewritten VARCHAR already
   diverges.
2. **Round 47** (`mismatch_round47_0.sql`): a long builder chain ending in
   `CAST(CAST(name AS t_enum) AS VARCHAR)`. Query `SUBSTR(t3.name, bit_or(-12345678), 12)`
   grouped by `t3.name`: base returns the name, equivalent returns `''`. Same root.

Both reduce to "ASCII stats present vs absent" × `SUBSTR` with `|start| > length` (or a
negative length past the start). The `ENUM` / `ANY_VALUE` constructs are only how the fuzzer
*dropped* ASCII stats; they are not themselves buggy.

## Minimal oracle exposure path

- **Object composition arity:** 2.
- **GCL builder path:** `DuckDBEnumTypeRoundTripBuilder` → `VIEW` realization.
- **Confidence:** Exact for the current GCL and round-47 emitted SQL; round 29 used a longer key-dedup route to the same stats contrast.
- **Realization:** the builder creates the ENUM cast-through/cast-back catalog object and exposes the restored `VARCHAR` through a view.
- **Workload/data requirements (excluded from arity):** `SUBSTR`/`SUBSTRING` on a short `VARCHAR` with an out-of-range negative start or negative-length boundary; the compared paths must differ in ASCII statistics.

**Exposure vs. intrinsic trigger:** The ENUM/view path is only the minimal oracle device for dropping ASCII statistics and revealing the alternate implementation; it is not itself faulty. The intrinsic bug is the result disagreement between the statistics-selected ASCII fast path and the Unicode path.

## Characterization

**Trigger:** `SUBSTR`/`SUBSTRING` on a VARCHAR whose plan-time stats have
`!StringStats::CanContainUnicode`, with a start (or start+negative length) that does not land
inside the string under from-the-end indexing.

**Does NOT trigger:**
- Small negative starts that still land inside the string (`-1`, `-2`, `-3` on `'abc'`): both
  paths agree.
- Identity `CREATE VIEW v AS SELECT * FROM t` — ASCII stats survive, still wrong in the same
  direction (not a view-merge bug).
- Materializing an ENUM-cast column into a new table restores ASCII stats and the clamp.

**Mechanism** (`duckdb/src/function/scalar/string/substring.cpp`):

- `SubstringPropagateStats` (line 374): if `!CanContainUnicode(child_stats[0])`,
  `SetFunctionCallback(SubstringFunctionASCII)`.
- ASCII `SubstringStartEnd` (line 62–64): `start = MaxValue(input_size + offset, 0)` for
  `offset < 0` — a clamp.
- Unicode `offset < 0` branch (line 113–155): counts characters from the end; if the target
  character is never seen, returns `SubstringEmptyString`.

#23229 / #23233 are the *performance* sibling (ASCII vs Unicode cost inside lambdas) and do
**not** report a result difference. #10721 is the from-the-end vs PostgreSQL design choice,
not this split. #23429 is an OOB read on the Unicode backward scan, a different defect.

DML was not tested (read-only scalar).

## How it was found

eqgen corpus-shuffle hunt `duck_corpus_shuffle/duckdb_20260814-015700/`. Two
mismatches, one bug, confirmed by reducing both to `SUBSTR` on ASCII-stats vs no-stats
VARCHAR. Original findings: `mismatch_round29_0.sql`, `mismatch_round47_0.sql`.

Same bug in rich-shuffle2 `duck_rich_shuffle2/duckdb_20260815-183409/` rounds
104 and 171: `SUBSTR(c_txt, 15, c_int)` with `c_int ∈ {-1,-7}` (start past the end +
negative length). `SET disabled_optimizers='statistics_propagation'` on the base side
matches the equivalent; `window_self_join` does not silence them. Equivalent lost ASCII
stats via `ANY_VALUE` + `UNION ALL` key-dedup (same mask as round 29).

## Open items

- Regression window not bisected (single CLI build on this box).
- Negative *length* is the same split: `SUBSTR(s, 15, -7)` on heap `'abc'` returns `'abc'`
  (ASCII clamp); the constant and no-stats heap return `''`. Reduced in `reduced.sql`
  control 2b (rich-shuffle2 rounds 104/171).
- GitHub issue not opened (not requested).
