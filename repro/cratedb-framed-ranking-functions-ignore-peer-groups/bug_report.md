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

# CrateDB: `RANK()` / `DENSE_RANK()` derive their value from the window frame instead of the ORDER BY peer group — any explicit frame returns a wrong answer

## Summary

Per SQL:2011 a ranking function's value is fixed by the row's `ORDER BY` peer group within the
**partition** and is frame-independent. CrateDB 6.4.1 instead derives it from the **frame**, so any
explicit frame silently returns wrong ranks. The wrong value tracks the frame shape, which is the
tell: a frame covering the whole partition (`UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING`, `ROWS`
*or* `RANGE`) makes every row return `1`; a frame ending at `CURRENT ROW` degenerates into
`ROW_NUMBER`. Delete the frame and the same query is correct.

This is not a ties-only problem. With three **distinct** `ORDER BY` values — where peer-group
semantics cannot be argued about at all — framed `RANK()` returns `1, 1, 1` where `1, 2, 3` is the
only correct answer.

No error, no plan-shape dependence, identical with assertions on and off: a stock production node
returns these values. Three rows and one query are enough to show it.

## Environment

| | |
|---|---|
| Engine | CrateDB **6.4.1** (official release tarball) |
| Assertions | identical output with `-ea -esa` and `-da -dsa` |
| Session | all defaults; no setting is load-bearing |
| Shards | 1 shard; `PARTITION BY` in the window changes nothing |
| Determinism | deterministic |
| Not implemented | `PERCENT_RANK()` and `CUME_DIST()` do not exist in 6.4.1 (`0A000 Unknown function`), so the other two frame-sensitive ranking functions cannot be compared |

## Minimal repro

```sql
CREATE TABLE v (x TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO v VALUES ('a');
INSERT INTO v VALUES ('a');
INSERT INTO v VALUES ('b');
REFRESH TABLE v;

SELECT RANK()       OVER (ORDER BY x ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) FROM v;
SELECT DENSE_RANK() OVER (ORDER BY x ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) FROM v;
SELECT RANK()       OVER (ORDER BY x) FROM v;   -- same query, frame deleted
SELECT DENSE_RANK() OVER (ORDER BY x) FROM v;
```

And the version that removes any argument about peer semantics — three distinct values:

```sql
-- rows 'a','b','c'
SELECT RANK() OVER (ORDER BY x ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) FROM v;
```

## Expected vs actual

| Query (3 rows, `'a','a','b'` unless noted) | Expected | Actual |
|---|---|---|
| `RANK` `ROWS` full frame | `1,1,3` | **`1,1,1`** |
| `DENSE_RANK` `ROWS` full frame | `1,1,2` | **`1,1,1`** |
| `RANK` `RANGE` full frame | `1,1,3` | **`1,1,1`** |
| `DENSE_RANK` `RANGE` full frame | `1,1,2` | **`1,1,1`** |
| `RANK` `ROWS UNBOUNDED PRECEDING AND CURRENT ROW` | `1,1,3` | **`1,2,3`** |
| `RANK` `ROWS 1 PRECEDING AND CURRENT ROW` | `1,1,3` | **`1,2,3`** |
| `RANK` `PARTITION BY g` + `ROWS` full frame | `1,1,3` | **`1,1,1`** |
| **`RANK` `ROWS` full frame, rows `'a','b','c'` (no ties)** | **`1,2,3`** | **`1,1,1`** |
| `RANK` unframed | `1,1,3` | `1,1,3` |
| `DENSE_RANK` unframed | `1,1,2` | `1,1,2` |
| `ROW_NUMBER` `ROWS` full frame | `1,2,3` | `1,2,3` |
| `COUNT(*)` `ROWS` full frame | `3,3,3` | `3,3,3` |
| `COUNT(*)` `ROWS UNBOUNDED PRECEDING AND CURRENT ROW` | `1,2,3` | `1,2,3` |
| all of the above with assertions off | unchanged | unchanged |

## Minimal oracle exposure path

**Object composition arity:** `0`

**GCL builder path:** `none`

**Confidence:** verified

**Realization:** A standalone `TABLE`-backed hand probe is sufficient; there is no equivalent-object realization.

**Workload/data requirements (excluded from arity):**
- `RANK()` or `DENSE_RANK()` with any explicit frame.
- At least two rows; distinct `ORDER BY` values already demonstrate the defect.
- No session setting or object rewrite is required.

**Exposure vs. intrinsic trigger:** There is no object contrast to count: the report came from a hand probe, and stripping frames from the nearby oracle finding did not remove that finding's separate divergence. The ranking bug is intrinsic to the framed workload expression and returns the same deterministic wrong ranks on both oracle sides.

## Characterization

The frame-shape dependence is the diagnostic. Two distinct wrong answers come out of the same
function depending only on the frame clause:

- **frame = whole partition** → `1` for every row. Consistent with the rank being computed as the
  position of the row's peer group *within the frame*: with one all-encompassing frame there is one
  group, so every row is in group 1.
- **frame ends at `CURRENT ROW`** → `1,2,3`, i.e. `ROW_NUMBER`. Consistent with the same
  computation over a frame that grows by one row at a time.

Both readings say the same thing: the frame is being consulted where the partition should be.

**The defect is bounded to the ranking functions.** Under the identical frame:

- `ROW_NUMBER()` is correct (`1,2,3`) — it is defined to ignore peer groups, and does.
- `COUNT(*)` as a window aggregate is correct for both a full frame (`3,3,3`) and a growing frame
  (`1,2,3`). Aggregates *must* honour the frame, and do.

So framing is implemented correctly in general; only the functions that must ignore it don't.

**`ROWS` vs `RANGE` is not the axis.** This is worth stating explicitly because it is the axis for
the analogous ClickHouse bug: there, `arePeers` returns false for any two distinct rows when
`frame.type == ROWS`, so only `ROWS` frames break and every `RANGE` frame is fine. In CrateDB a
`RANGE` full-partition frame is equally wrong, so the two engines have the same *class* of defect via
different mechanisms — do not carry the ClickHouse analysis across.

### Comparison with other engines

| engine | `RANK` framed `ROWS UNBOUNDED..UNBOUNDED` over `'a','a','b'` |
|---|---|
| correct (SQL:2011) | `1,1,3` |
| CrateDB 6.4.1 | `1,1,1` |
| ClickHouse 25.3–26.8 | `1,2,3` (degenerates to `ROW_NUMBER`) |
| DuckDB 1.5.5 / 2.0-alpha | `1,1,3` — frame accepted and correctly ignored |
| PostgreSQL 20devel | `1,1,3` |

(The DuckDB and PostgreSQL rows are carried over from the sibling ClickHouse investigation,
`repro/clickhouse-run1-round12-rank-honours-frame/`; the CrateDB row was measured here.)

## How it was found

**Not an eqgen finding** — it came out of a control probe, and the honest version of the story is
worth recording because the near-miss is instructive.

While triaging `logs/cratedb_run3/mismatch_round14_0.sql` (a genuine, admissible wrong-result finding:
base 2 rows vs equivalent 6), I noticed its query contains a framed `DENSE_RANK`. Having just
established that all seven `clickhouse_run17` mismatches were the ClickHouse framed-ranking bug, the
attribution was tempting. So I ran the direct 3-row probe against CrateDB to see whether it shared the
bug — and it does, differently.

But **the frame-strip control on the actual finding refutes the attribution**: deleting all four `ROWS`
frames from round14's query leaves the divergence completely unchanged (base 2 / equivalent 6, before
and after). round14 contains this bug and diverges for some *other* reason, and remains untriaged.

So the differential oracle did not find this one, and could not easily have: a wrong-but-deterministic
rank is identical on both sides of the equivalence, so the oracle is structurally blind to it — the
same blindness recorded for [`clickhouse-correlated-in-returns-empty`]. It only became visible in
ClickHouse because the wrong ranks there are decided by physical arrival order, which *is*
plan-shape-dependent. CrateDB's version is stably wrong, so it hides from the oracle entirely and
needed a hand-written probe.

- Repro and control matrix: [`reduced.sql`](reduced.sql)
- The finding that prompted the probe (still untriaged, different cause):
  hunt log
