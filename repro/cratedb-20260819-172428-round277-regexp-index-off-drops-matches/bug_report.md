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

# CrateDB: REGEXP filter merged into `Collect` over an `INDEX OFF` text column matches no rows

## Summary

A `WHERE text_col ~ 'pattern'` predicate on a `TEXT INDEX OFF` column silently matches no rows.
The same predicate projected in the SELECT list evaluates correctly, and the same filter on a
normally indexed `TEXT` column returns the matching row. Disabling
`optimizer_merge_filter_and_collect` restores the correct result, isolating the defect to REGEXP
evaluation inside the shard-level `Collect`.

The same wrong filter is used by `DELETE` and `UPDATE`: both silently affect zero rows. This is not
limited to an empty pattern; the one-row repro uses `'a' ~ 'a'`.

## Environment

| | |
|---|---|
| Affected | CrateDB **6.4.2**, built `1db6455`; also reproduced on **6.4.1**, built `45bfa80` |
| Image | official Docker `crate:6.4.2` / `crate:6.4.1` |
| Session | defaults; `optimizer_merge_filter_and_collect = true` |
| Shards | 1 shard, `number_of_replicas = 0` |
| Access path | PostgreSQL wire via eqgen's psycopg adapter |
| Determinism | stable across repeated fresh databases |

## Minimal repro

```sql
CREATE TABLE t (s TEXT INDEX OFF)
  CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t VALUES ('a');
REFRESH TABLE t;

SELECT s FROM t WHERE s ~ 'a';
-- expected: ('a')
-- actual:   0 rows

SELECT s, s ~ 'a' AS matches FROM t;
-- ('a', TRUE)
```

The projection is the direct ground truth: the stored value is `'a'`, and CrateDB's own scalar
REGEXP implementation says `'a' ~ 'a'` is `TRUE`.

## Expected vs actual

| Variant | Expected | Actual |
|---|---:|---:|
| normally indexed `TEXT`, `WHERE s ~ 'a'` | 1 row | 1 row |
| `TEXT INDEX OFF`, `WHERE s ~ 'a'` | 1 row | **0 rows** |
| `TEXT INDEX OFF`, project `s ~ 'a'` | `TRUE` | `TRUE` |
| `INDEX OFF` + `optimizer_merge_filter_and_collect = false` | 1 row | 1 row |
| `DELETE ... WHERE s ~ 'a'` | deletes matching row | **deletes 0 rows** |
| `UPDATE ... WHERE s ~ 'a'` | updates matching row | **updates 0 rows** |

The `INDEX OFF` / merged-Collect path is wrong. Equality and `length(s) = 1` filters on the same
column work, so the table retains the value and can filter it through other scalar operators.

## Equivalence construction

The original finding is
`eqgen/log/crate_simple_shuffle_keytag/cratedb_20260819-172428/mismatch_round277_0.sql`.
Its equivalent ends with:

```sql
CREATE TABLE t__base_table_11 AS
SELECT l.c_pk, l.id, l.name, l.created_at
FROM keyed l LEFT OUTER JOIN flags r ON l.eq_uid = r.eq_uid
WHERE r.eq_flag = 1;

CREATE TABLE t__base_table_12 (
  c_pk BIGINT INDEX OFF,
  id BIGINT INDEX OFF,
  name TEXT INDEX OFF,
  created_at TEXT INDEX OFF
) ...;
INSERT INTO t__base_table_12 SELECT * FROM t__base_table_11;
CREATE VIEW t AS SELECT * FROM t__base_table_12;
```

Layer bisection is decisive: exposing `t__base_table_11` returns the same five rows as the base;
exposing `t__base_table_12` returns zero. The original query reduces from a large
`GROUP BY`/subquery expression to `WHERE created_at ~ ''`, and then to the non-empty one-row
`WHERE s ~ 'a'` repro above. The `ANY_VALUE` reducers, flag join, scalar subquery, joins,
`NOT IN`, `GROUP BY`, and empty-string pattern are not required.

## Minimal oracle exposure path

**Object composition arity:** `3`

**GCL builder path:** `CrateDbIndexOffBuilder[TABLE]` → `CreateViewBuilder`

**Confidence:** verified by layer bisection

**Realization:** The builder copies the row-identical relation into a physical `INDEX OFF` table;
the final view only exposes that table under the workload name.

**Workload/data requirements (excluded from arity):**
- A REGEXP predicate in filter position over an `INDEX OFF` text column.
- One matching non-NULL value; one row and pattern `'a'` suffice.

**Exposure vs. intrinsic trigger:** The `CrateDbIndexOffBuilder` is both the oracle exposure and
the intrinsic relation-side trigger. The final pass-through view is not intrinsically required,
but it is part of the emitted exposure path. Every preceding builder reduces away.

## Characterization

The decisive plan difference is whether the predicate is merged into `Collect`:

```text
-- default, wrong (0 rows)
Collect[t | [s] | (s ~ 'a')]

-- optimizer_merge_filter_and_collect = false, correct (1 row)
Filter[(s ~ 'a')]
  └ Collect[t | [s] | true]
```

The scalar path is correct:

```sql
SELECT s, s ~ 'a' FROM t;
-- ('a', TRUE)
```

Source inspection at CrateDB 6.4.2 points to the Lucene conversion boundary:

- `RegexpMatchOperator.evaluate` (`server/.../RegexpMatchOperator.java:71-89`) evaluates the
  source and pattern with Lucene's automaton and returns the correct boolean.
- `RegexpMatchOperator.toQuery` (`:94-101`) always creates a `RegexpQuery` for the field and does
  not check `ref.indexType()`.
- The analogous `LikeOperator.toQuery` (`LikeOperator.java:126-148`) passes
  `ref.indexType() != IndexType.NONE` into its query construction.

This suggests the merged path tries to execute a Lucene term/automaton query against a column with
no index instead of declining the conversion and retaining a scalar `Filter`. The exact fix is for
upstream to confirm.

### DML

Both read and write filters are affected. `DELETE` and `UPDATE` with the same predicate complete
successfully but affect zero rows. They do not target an incorrect row; they silently skip the row
that should match.

## How it was found

Eqgen's data-equivalence oracle ran the same generated query over a normally indexed base table and
a row- and type-identical equivalent whose final physical step was `CrateDbIndexOffBuilder`.
Round 277 returned five rows only on the base side. Replay on 6.4.2 passed all gates: both exposed
relations contain the same eight rows, types match, and each answer is stable.

Deduplication of all findings through round 277 first removed the existing
`starts_with(col, '')`, generated-partition, ALL-predicate/reducer, and throwing-scalar clusters.
Round 277 was the only remaining signature whose divergence switched exactly at the `INDEX OFF`
copy.

The two concurrent hunts produced 91 saved findings in total:

- 54 mismatches: existing ALL-predicate Collect-vs-Filter family;
- 7 mismatches: existing `starts_with(col, '')` indexed-prefix family;
- 2 mismatches: existing `CROSS JOIN ... WHERE` correlated-filter family;
- 5 rich-catalog mismatches: existing generated-partition Collect-merge family;
- 22 one-sided errors: non-short-circuit `split_part`/`substring` throwing-scalar comparability
  cases (not engine correctness bugs);
- 1 mismatch: this REGEXP/`INDEX OFF` bug.

## Duplicate search

GitHub searches of `crate/crate` (open and closed issues) for combinations of `REGEXP`, regex,
empty pattern, `INDEX OFF`, Lucene, and filter found no matching issue. The only broad REGEXP result
was #11421 (ReDoS), which is unrelated. The internal issue tracker was not available in this
environment, so that search remains outstanding.

This is not the existing empty-prefix `starts_with` bug: that bug is wrong on the normally indexed
Lucene path and correct on `INDEX OFF`; this REGEXP bug has the opposite polarity. It is also not
the NUMERIC `INDEX OFF` range report, which involves comparison operators and numeric encoding.

## Open items

- Search the internal tracker.
- Determine the regression window before 6.4.1.
- Confirm whether returning `null` from `RegexpMatchOperator.toQuery` for `IndexType.NONE` is the
  intended fallback, or whether the generic Collect query path needs the guard.
- Test additional text types and object subcolumns declared `INDEX OFF`.
