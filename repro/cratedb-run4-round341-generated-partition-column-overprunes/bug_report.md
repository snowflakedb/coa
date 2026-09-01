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

# CrateDB: merging a compound filter into the shard-level `Collect` of a generated-column `PARTITIONED` table drops rows

## Summary

On a table `PARTITIONED BY` a `GENERATED` column, evaluating certain compound boolean filters inside
the shard-level `Collect` silently loses rows. The original repro uses an uncorrelated sub-select
(`… <= ALL (SELECT … FROM t GROUP BY id)`), but CrateDB 6.4.2 continuation findings show that a
sub-select is **not required**: conjunctions/disjunctions of ordinary column predicates reproduce
the same bug and share the same optimizer mask. Two optimizer rules produce the bad plan, and
disabling **either** one gives the correct answer:

```sql
SET optimizer_merge_filter_and_collect  = false;   -- correct
SET optimizer_move_filter_beneath_rename = false;  -- correct
```

They chain: the `Filter` is moved beneath `Rename[…] AS t1` and then merged into the `Collect` on the
partitioned table. **The predicate text is byte-identical either way** — only *where* it is evaluated
changes (plan diff below). Evaluating it inside the per-shard `Collect` of a partitioned table is what
loses the row.

Two rows are enough to show the original face. No error is raised.

## Environment

| | |
|---|---|
| Engine | CrateDB **6.4.1** (official release tarball); broader predicate faces confirmed on **6.4.2** (`1db6455`) |
| Session | all defaults. The two rules above are `true` by default; **no other setting is load-bearing** |
| Determinism | deterministic |
| Origin | `logs/cratedb_run4/mismatch_round341_0.sql`; admissibility verified (base `t` ≡ equivalent `t`, 8 identical rows) |

## Minimal repro

```sql
CREATE TABLE tb (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO tb VALUES (2, 'zzz', 'zzz');
INSERT INTO tb VALUES (7, 'é',   'é');
REFRESH TABLE tb;

CREATE TABLE p (id BIGINT, name TEXT, created_at TEXT,
                bucket INTEGER GENERATED ALWAYS AS (id % 4))
  PARTITIONED BY (bucket) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO p (id, name, created_at) SELECT id, name, created_at FROM tb;
REFRESH TABLE p;

SELECT t1.name FROM p AS t1 WHERE
  (((t1.id != 15) OR (CASE WHEN True AND True THEN coalesce(True, False)
                          WHEN CAST('y' AS BOOLEAN) THEN t1.name IS NOT NULL
                          ELSE coalesce(True, False) END))
     NOT IN (CASE WHEN (True AND CAST(NULL AS BOOLEAN)) IS NULL
                  THEN coalesce(False, True) AND (t1.created_at <= t1.created_at) END,
             LENGTH(t1.name) NOT IN ((trunc(0) / 10), coalesce(t1.id - t1.id, MOD(t1.id, 4)))))
  <= ALL (SELECT False FROM p AS t3 GROUP BY t3.id);
```

The identical query against `tb` returns both rows. `SELECT id, name` from either table returns the
same two rows, so the data is identical.

## Expected vs actual

| | Expected | Actual |
|---|---|---|
| minimal repro on the plain table `tb` | `['zzz', 'é']` | `['zzz', 'é']` |
| **minimal repro on the partitioned table `p`** | `['zzz', 'é']` | **`['zzz']`** |
| `p` + `SET optimizer_merge_filter_and_collect = false` | `['zzz', 'é']` | `['zzz', 'é']` |
| `p` + `SET optimizer_move_filter_beneath_rename = false` | `['zzz', 'é']` | `['zzz', 'é']` |
| original 8-row finding on base `t` | 5 rows | 5 rows |
| original 8-row finding on the row-identical equivalent `t` | 5 rows | **4 rows** |

The lost row is always the one whose partition differs from the surviving row's.

## The decisive plan diff

Same data, same query, on `p`. The predicate is character-for-character identical in both plans; the
only difference is the node it lives in.

**Default (wrong — 1 row):** the filter is merged into the `Collect`.

```
GroupHashAggregate[name, created_at, id]
  └ Rename[name, created_at, id] AS t1
    └ Collect[p | [name, created_at, id] | ((NOT ((… ) = ANY([false, …]))) <= ALL((SELECT false FROM (t3))))]
  └ Eval[false]                                    <-- the MultiPhase sub-select branch
    └ GroupHashAggregate[id]
      └ Collect[p | [id] | true]
```

**`optimizer_merge_filter_and_collect = false` (correct — 2 rows):** a separate `Filter` above it.

```
GroupHashAggregate[name, created_at, id]
  └ Rename[name, created_at, id] AS t1
    └ Filter[((NOT ((… ) = ANY([false, …]))) <= ALL((SELECT false FROM (t3))))]
      └ Collect[p | [name, created_at, id] | true]
  └ Eval[false]
    └ GroupHashAggregate[id]
      └ Collect[p | [id] | true]
```

The merged filter carries `<= ALL((SELECT false FROM (t3)))` — a reference to the plan's `MultiPhase`
sub-select branch — down into a per-shard `Collect`. On a partitioned table each partition is a
separate index, and the row is lost. That the same merge is harmless on an unpartitioned table (even
at 7 shards, control C8) points at the partition-level `Collect` as the unsound context. The internal
cause is upstream's to confirm; the rule names and this diff localise it.

## Minimal oracle exposure path

**Object composition arity:** `2`

**GCL builder path:** `CrateDbPartitionedBuilder` → partitioned `TABLE` realization

**Confidence:** verified

**Realization:** A generated-column-partitioned `TABLE` holds the row-identical equivalent.

**Workload/data requirements (excluded from arity):**
- Partitioning by a generated integer expression such as `id % 4`.
- At least two populated partitions.
- An uncorrelated sub-select carried by the outer filter.
- The measured half-folded `OR`/`CASE` predicate shape.

**Exposure vs. intrinsic trigger:** The builder's generated partitioned-table layout remains intrinsic to the standalone trigger; no separate view or deeper builder chain is needed. The builder both provided the contrasting row-identical object and selected the physical context in which filter merge into the per-partition `Collect` becomes unsound.

## Characterization

### Relation side — the partitioning must be by a *generated* column

| variant (8-row data, expected 5) | result |
|---|---|
| plain table | 5 ✓ |
| **`PARTITIONED BY` generated `id % 4`** | **4 ✗** |
| `PARTITIONED BY` a **plain** `INTEGER` column holding the identical `id % 4` values | 5 ✓ |
| generated `id % 4` column present but **not** partitioned by | 5 ✓ |
| `PARTITIONED BY (id)` directly, no expression | 5 ✓ |
| `PARTITIONED BY` generated `id + 1` (also 7 partitions) | 5 ✓ |
| `PARTITIONED BY` generated `LENGTH(name) % 4` | 5 ✓ |
| `PARTITIONED BY` generated `id % 5` | 4 ✗ |
| `PARTITIONED BY` generated `0` (a single partition) | 5 ✓ |
| plain table `CLUSTERED INTO 2 / 4 / 7 SHARDS` | 5 ✓ |
| outer = partitioned, subquery = plain table | 4 ✗ |
| outer = plain table, subquery = partitioned | 5 ✓ |
| window-collapse link alone (the chain's other half) | 5 ✓ |

The **plain-column** control is the sharpest: identical partition values, identical physical split, only
the declared generation differs. Also note partition *count* is not the axis (`id + 1` yields the same
7 partitions and is correct), and shard count is not either.

### Query side — the left operand of the outer `NOT IN` is load-bearing

| variant (2-row data, expected `['zzz','é']`) | result |
|---|---|
| as-generated | `['zzz']` ✗ |
| left operand → literal `True` | ✓ |
| left operand → `(t1.id != 15)` | ✓ |
| left operand → `(t1.name IS NOT NULL)` | ✓ |
| middle operand → `CAST(NULL AS BOOLEAN)` | `['zzz']` ✗ |
| middle operand → `False` | ✗ |
| right operand → `LENGTH(t1.name) NOT IN (0)` | ✗ |
| the sub-predicate `LENGTH(name) <> 0` alone, both tables | identical ✓ |

Curiously the load-bearing operand is semantically **constant `TRUE`** (the `CASE` reduces to `TRUE`,
so `… OR CASE …` is `TRUE`) yet references a column — and replacing it with the literal `True` *or*
with a genuinely non-constant predicate both fix it. So it is neither "constant" nor "column-referencing"
per se but this particular half-folded shape that survives into the merged filter.

**Reduction honesty:** the predicate is close to as-generated. Further cuts flip its truth value —
`<= ALL (SELECT False …)` is satisfied only when the left side evaluates to exactly `FALSE`, so most
simplifications collapse *both* sides to zero rows and stop discriminating. Every cut recorded above was
re-tested; the ones that fix the divergence are listed as such rather than applied.

### Hypotheses refuted along the way

Recorded so nobody re-derives them:

1. **Partition pruning.** `EXPLAIN` shows no partition list and no pruning; the plan is a plain
   `Collect` with the predicate attached.
2. **Matching the query's `MOD(t1.id, 4)` against the generation expression.** Fails with mismatched
   moduli in either direction and with no modulo in the query at all.
3. **Shard count** (partitioned = 1 shard per partition = 7 vs base 1). A plain table at 2/4/7 shards
   is correct.
4. **The non-ASCII `'é'` in the lost row.** ASCII-only data still loses a row; `LENGTH` and
   `OCTET_LENGTH` agree exactly between the two tables.

## How it was found

eqgen v3 data-equivalence oracle, `cratedb_run4` round 341. The equivalent relation differs from the
base *only* in on-disk partitioning, so a different answer admits no semantic argument — the oracle's
cleanest possible case. A query-rewrite oracle (TLP / NoREC / EET) cannot reach this at all: no query
rewrite turns an unpartitioned table into a partitioned one, and its rewrites would dismantle the
half-folded `OR`/`CASE` operand that turns out to be load-bearing.

- Repro, controls and plan diff: [`reduced.sql`](reduced.sql) — 22 queries, each verified against 6.4.1
- Original finding:
  hunt log

### 6.4.2 continuation findings

All five rich-catalog findings in
`eqgen/log/crate_rich_gen_keytag/cratedb_20260819-172428` are this same root cause:

- round 12: two tautological `OR` expressions joined by `AND` lose partition-bucket rows;
- round 34 (two queries): a tautological `OR` composed with a text range predicate loses one row;
- round 71 (two queries): a dead/UNKNOWN left arm composed with `OR c_pk <> 1` loses one row.

Layer bisection is exact in all three chains: every intermediate relation is row-identical and
answers correctly; the first wrong answer appears only after `CrateDbPartitionedBuilder` creates the
generated-column partitioned table. `SET optimizer_merge_filter_and_collect = false` restores every
answer. Each constituent predicate tested alone is correct, so these are compound-filter faces of
the existing Collect-merge bug rather than three new partition bugs.
