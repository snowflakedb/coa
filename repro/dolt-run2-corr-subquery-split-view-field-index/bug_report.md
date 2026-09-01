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

# Dolt: correlated subquery over a column-split-rejoin view → internal "unable to find field with index" planner error

## Summary

When the queried relation is a view that splits a surrogate-keyed relation into two disjoint column
groups and LEFT JOINs them back on the key (so the view's output columns come from both sides of
the join), **any correlated subquery** over that view raises an internal planner assertion:
`1105 unable to find field with index 3 in row of 2 columns. This is a bug. Please file an issue
here: https://github.com/dolthub/dolt/issues`. The engine mis-computes a column index during
correlated-subquery decorrelation/planning against the split view. The identical query over a plain
table (or a plain self-join view) returns correctly.

## Environment

- **Engine**: Dolt 8.0.31 (`VERSION()`), source `v2.2.3-9-g95218a00a`, commit
  `95218a00a973be43d84e5c60836cb3ffe8c34387`, assertions off. Engine = dolthub/go-mysql-server.
- **Session**: sql_mode as in the finding; utf8mb4 / utf8mb4_0900_ai_ci. Bug is independent of these.

## Minimal repro

See [`reduced.sql`](./reduced.sql). Core:

```sql
CREATE TABLE base (id BIGINT, name VARCHAR(255), created_at VARCHAR(255));
INSERT INTO base VALUES (1,'a','x'),(2,'b','y'),(3,'a','z');
CREATE TABLE k AS SELECT id, name, created_at, ROW_NUMBER() OVER (ORDER BY id) AS sk FROM base;
CREATE VIEW vl AS SELECT id, sk FROM k;
CREATE VIEW vr AS SELECT name, created_at, sk FROM k;
CREATE VIEW t AS SELECT l.id AS id, r.name AS name, r.created_at AS created_at
                 FROM vl l LEFT JOIN vr r ON l.sk = r.sk;

SELECT t1.id FROM t t1 WHERE EXISTS (SELECT 1 FROM t t5 WHERE t5.id = t1.id);
```

## Expected vs actual

| Query (over the split-rejoin view `t`) | Expected | Actual |
|---|---|---|
| `… WHERE EXISTS (SELECT 1 FROM t t5 WHERE t5.id = t1.id)` | 3 rows `(1,2,3)` | **ERROR 1105 "unable to find field with index 3 in row of 2 columns"** |
| `… WHERE t1.id NOT IN (SELECT t5.id FROM t t5 WHERE t5.id = t1.id)` | rows | same ERROR |
| control (a): same query over a **plain table** | 3 rows | 3 rows ✓ |
| control (b): same query over a **plain self-join** view (`base⋈base ON id=id`) | 3 rows | 3 rows ✓ |
| control (c): split view + **uncorrelated** subquery | rows | rows ✓ |

## Equivalence construction

### Concrete, as the builder emits it

The eqgen equivalent `t` for these 7 findings is the **`eq_seq_key` column-split-and-rejoin view**:

```sql
CREATE TABLE t__base_table_1 AS SELECT id, name, created_at, ROW_NUMBER() OVER (ORDER BY id) AS eq_seq_key_1 FROM t__base;
CREATE VIEW  t__base_view_1  AS SELECT id, eq_seq_key_1 FROM t__base_table_1;              -- left group
CREATE VIEW  t__base_view_2  AS SELECT name, created_at, eq_seq_key_1 FROM t__base_table_1; -- right group
CREATE VIEW  t AS SELECT l.id AS id, r.name AS name, r.created_at AS created_at
                  FROM t__base_view_1 l LEFT OUTER JOIN t__base_view_2 r ON l.eq_seq_key_1 = r.eq_seq_key_1;
```

`reduced.sql` PART 1 is a faithful, compact rebuild (`base → k(+sk) → vl/vr → LEFT JOIN`). This view
is row-identical to the base table (oracle admissibility passed).

### The load-bearing composition

Two ingredients, both necessary:

1. **the split-rejoin view shape** — two projections of the *same* surrogate-keyed relation LEFT
   JOIN'd on the key, so the view's columns are drawn from both join sides. A plain table and a
   plain self-join view (`base ⋈ base`) do **not** trigger it (controls a, b). The distinguishing
   factor is that the columns are partitioned across the two join inputs.
2. **a correlated subquery** in the workload — `EXISTS`, `= `, and `NOT IN` correlations all trigger
   it; an uncorrelated subquery does not (control c). `GROUP BY` in the subquery is not required.

Reduced away: the double-nested `NOT IN (…) NOT IN (…)`, the `RIGHT JOIN` chains, and the `GROUP BY`
present in the original workloads.

## Minimal oracle exposure path

- **Object composition arity:** `2`.
- **GCL builder path:** `SequenceOuterJoinQueryBuilder → CreateViewBuilder`.
- **Confidence:** high; `SequenceOuterJoinQueryBuilder` is the current class that emits the report's
  surrogate-key column split/rejoin, and `CreateViewBuilder` is the final registered realization.
- **Realization:** the reconstructed relation is persisted as the workload-facing view.
- **Workload/data requirements (excluded from arity):** the correlated subquery and its outer-column
  reference are workload requirements; table width and the chosen correlation values are data/schema
  conditions.
- **Exposure vs. intrinsic trigger:** the GCL path supplies the split-rejoin view that exposes the
  defect. The intrinsic trigger is that relation shape combined with workload correlation; neither
  factor alone reproduces it.

## Characterization

- **Trigger**: correlated subquery whose outer reference resolves against the split-rejoin view;
  the planner computes a field index (`3`) exceeding the width of the left sub-relation (`2` cols,
  `vl = (id, sk)`), i.e. a column-index/scope-resolution error introduced by the split.
- **Does NOT trigger**: plain table, plain self-join view, or an uncorrelated subquery.
- The error is a returned SQL error (`1105`), not a panic — no server crash, no stack trace (the
  engine's own message points to the issue tracker). Assertions-off build; irrelevant here.

## How it was found

The eqgen **data-equivalence oracle** replaced the base table `t` with the row-identical
`eq_seq_key` split-rejoin view and ran the same workload against both. The workload's correlated
`NOT IN`/`EXISTS` subquery executes fine over the base table but errors over the equivalent view —
a one-sided error finding, i.e. a genuine engine divergence. This is precisely the oracle's strength:
it holds a fixed query and slides a row-identical but structurally different relation underneath it,
so an ordinary correlated subquery becomes a probe of the planner's column-index handling for
join-of-projections views. A query-rewrite oracle (fixed data, rewritten query) would not manufacture
the split-rejoin relation and so would not reach this path.

- **Seeds / findings** (all `1105 unable to find field with index N in row of M columns`):
  1172259338 (round56, idx 9/4), 1498099867 (round65_2, idx 3/2), 1024966282 (round86_0, idx 3/3),
  899764046 (round87_0, idx 3/3), 1567556447 (round97_0, idx 3/3), 1284972640 (round109_0, idx 13/3),
  1709259289 (round111_1, idx 5/3). All 7 are this one bug (index/width vary with the query).
- Reduced repro: [`reduced.sql`](./reduced.sql).
- Original findings: hunt log
  and `error_round{56_0,86_0,87_0,97_0,109_0,111_1}.sql`.
