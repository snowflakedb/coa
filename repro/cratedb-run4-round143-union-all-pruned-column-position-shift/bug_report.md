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

# CrateDB: a `UNION ALL` branch reading a relation with an extra *computed* column shifts column positions above the union — silent wrong rows, or `XX000 Cannot cast value ... to type bigint`

## Summary

When one branch of a `UNION ALL` reads a view or derived table that projects an extra **computed**
column which the union does not output, column positions above the union shift by one. A query with
`GROUP BY` plus an aggregate over a non-grouped column then reads the wrong column. If the shifted
column's type is incompatible the query fails loudly with `XX000 Cannot cast value 'dup' to type
'bigint'`; if the types happen to be compatible it **silently returns wrong rows** — extra groups
built from the neighbouring column's values.

An extra *stored* column on a plain table is fine, so this is about the computed projection, not
about the branch being wider. `UNION DISTINCT` is unaffected. Reproduces with assertions on **and
off**, so a stock production node is affected.

This is the same "attribute mixup in a UNION" class as
[GH#13779](https://github.com/crate/crate/issues/13779) (fixed in 5.2.x), but a **surviving sibling**,
not a regression: #13779's own repro is clean on 6.4.1.

## Environment

| | |
|---|---|
| Engine | CrateDB **6.4.1** (official release tarball) |
| Assertions | fails identically with `-ea -esa` **and** `-da -dsa`, including the wrong-result face |
| Session | `search_path=<per-connection schema>`, `enable_hashjoin=true`, `error_on_unknown_object_key=true`, `insert_select_fail_fast=true`, `optimizer_equi_join_to_lookup_join=false`. **No setting is load-bearing.** |
| Shards | 1 shard |
| Determinism | deterministic; needs ≥ 1 row |

## Minimal repro

**Loud face** — one table, one row, no view:

```sql
CREATE TABLE b (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (0, 'dup', 'dup');
REFRESH TABLE b;

SELECT id FROM (SELECT * FROM b
                UNION ALL
                SELECT id, name, created_at FROM (SELECT id, name, created_at, CAST(NULL AS BIGINT) AS extra FROM b) s) t
GROUP BY name, id HAVING MIN(created_at) <= name;
-- XX000  Cannot cast value `dup` to type `bigint`
```

`'dup'` is a TEXT value being cast to `id`'s BIGINT type — a one-position shift.

**Silent face** — same shape with all three columns TEXT, so no cast can fail:

```sql
CREATE TABLE b (c0 TEXT, c1 TEXT, c2 TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES ('aaa', 'mmm', 'zzz');
INSERT INTO b VALUES ('bbb', 'nnn', 'yyy');
REFRESH TABLE b;

CREATE VIEW t AS SELECT * FROM b
                 UNION ALL
                 SELECT c0, c1, c2 FROM (SELECT c0, c1, c2, CAST(NULL AS BIGINT) AS extra FROM b) s;

SELECT c0, MIN(c2) FROM t GROUP BY c1, c0;
```

## Expected vs actual

| Query | Expected | Actual |
|---|---|---|
| Loud repro | 1 row (`id = 0`) | **`XX000 Cannot cast value 'dup' to type 'bigint'`** |
| Loud repro, assertions off | 1 row | **same error** |
| Silent repro | 2 rows: `('aaa','zzz')`, `('bbb','yyy')` | **4 rows** — plus `('mmm','zzz')`, `('nnn','yyy')` |
| Silent repro, assertions off | 2 rows | **same 4 rows** |
| Silent repro with the extra computed column removed | 2 rows | 2 rows |
| Silent repro against the plain table (no union) | 2 rows | 2 rows |
| Original workload query on base `t` | 7 rows | 7 rows |
| Original workload query on the row-identical equivalent `t` | 7 rows | **`XX000`** |

In the silent face `'mmm'` and `'nnn'` are **`c1` values appearing in the `c0` output position** — the
grouping read a shifted column, so two spurious groups were manufactured.

## Equivalence construction

### (1) The construct as the eqgen builder emits it

Round 143 produced **six** findings — six different workload queries over one byte-identical
36-statement chain (`md5 e6b12b2b03c3` over all six files). The chain ends in the predicate-split
partitioning builder's `UNION ALL`, whose odd-id branch sits on top of a deep view stack:

```sql
CREATE VIEW  t__base_view_6 AS SELECT id, name, created_at FROM t__base_view_5;
-- the culprit link: add a column …
CREATE VIEW  t__base_view_7 AS SELECT id, name, created_at, CAST(NULL AS BIGINT) AS eq_tmp_col_1
                               FROM t__base_view_6;
-- … then drop it again
CREATE VIEW  t__base_view_8 AS SELECT id, name, created_at FROM t__base_view_7;
CREATE VIEW  t__base_view_9 AS SELECT * FROM t__base_view_8 WHERE (MOD(id, 2) <> 0) OR (id IS NULL);
CREATE VIEW  t            AS SELECT * FROM t__base_view_2 UNION ALL SELECT * FROM t__base_view_9;
```

and the simplest of the six queries:

```sql
SELECT t1.id AS expr_0_number, MIN(t1.name IS NOT NULL) IS NULL AS expr_1_boolean
FROM t AS t1 GROUP BY t1.name, t1.id HAVING MIN(t1.created_at) <= t1.name;
```

`t__base_view_7` is the **add-then-drop-a-column round-trip builder**. Bisecting the chain layer by
layer pins it exactly: pointing the odd branch at `t__base_view_6` (one layer below) is clean;
pointing it at `view_7`, `view_8` or `view_9` fails.

**Mapping onto the distilled repro:** the `UNION ALL` of `t__base_view_2` (even half) and
`t__base_view_9` (odd half) → `SELECT * FROM b UNION ALL SELECT id, name, created_at FROM (…) s`;
`CAST(NULL AS BIGINT) AS eq_tmp_col_1` in `view_7` → the `CAST(NULL AS BIGINT) AS extra` in the
derived table; `GROUP BY t1.name, t1.id HAVING MIN(t1.created_at) <= t1.name` → the same, verbatim.
The predicate split, the `FULLTEXT` index, the `INDEX OFF` copies, the `PARTITIONED BY` generated
column and 30 other statements are all irrelevant.

### (2) The load-bearing construct — a construct × query-feature composition

Three ingredients, each proved necessary by a control that changes only it:

1. **`UNION ALL`** — `UNION DISTINCT` is clean (C5); no union at all is clean (C6).
2. **a branch reading a relation with an extra *computed* column** — removing the extra column is
   clean (C3); an extra **stored** column on a plain 4-column table is clean (C4). So width is not
   the trigger, the computed projection is. Its type is irrelevant (`CAST(NULL AS TEXT)` fails the
   same way, C10) and so is its position (extra column first also fails, C11).
3. **`GROUP BY` plus an aggregate over a column not in the `GROUP BY`** — aggregating a *grouped*
   column is clean (C7); no aggregate is clean (C8); the aggregate in the select list instead of
   `HAVING` still fails (C9), so the ingredient is the non-grouped aggregate, not `HAVING`.

Three columns are also required — the two-column analogue is clean (C12).

### (3) Constructs reduced away

Thirty of the chain's 36 statements: both `INSERT … SELECT` row copies, the `FULLTEXT`-indexed copy,
the two `INDEX OFF` copies, the `PARTITIONED BY (id % 4)` generated-column table, the predicate
split itself, and six wrapper views. On the query side, the boolean `MIN(name IS NOT NULL) IS NULL`
select item went away, leaving `SELECT id … GROUP BY name, id HAVING MIN(created_at) <= name`. Eight
rows reduced to one, and the view to an inline derived table.

## Minimal oracle exposure path

**Object composition arity:** `4`

**GCL builder path:** `AddDropColumnQueryBuilder[VIEW]` → `PartitionUnionQueryBuilder` → `CreateViewBuilder`

**Confidence:** verified

**Realization:** Nested `VIEW` projections feed the widened-then-pruned branch into the exposed `UNION ALL` view.

**Workload/data requirements (excluded from arity):**
- A computed extra column in one union branch's lineage.
- `UNION ALL`, not `UNION` distinct.
- `GROUP BY` plus an aggregate over a non-grouped column.
- At least three visible columns.

**Exposure vs. intrinsic trigger:** The add/drop projection and union relation both remain in the standalone trigger, while the predicate split, storage-layout ladder, and many wrapper views reduce away; the outer view can be inlined. The builders provided the row-identical contrast and the exact widened-branch lineage that causes the positional shift.

## Characterization

### What triggers it / what does not

| Variant | Result |
|---|---|
| `UNION ALL` + computed extra column + non-grouped aggregate | **`XX000`** (or silent wrong rows) |
| all-TEXT columns (no cast can fail) | **silently wrong rows** |
| assertions off, either face | **unchanged** |
| extra computed column removed | clean, correct |
| extra column is a *stored* column on a plain table | clean, correct |
| extra computed column typed `TEXT` instead of `BIGINT` | **`XX000 … to type bigint`** (target type comes from `id`) |
| extra computed column placed first | **`XX000`** |
| `UNION` (distinct) | clean, correct |
| no union (the wide derived table alone) | clean, correct |
| aggregate over a *grouped* column | clean, correct |
| no aggregate | clean, correct |
| aggregate in the select list rather than `HAVING` | **`XX000`** |
| two columns instead of three | clean, correct |
| a view vs an inline derived table | both fail — not view-specific |
| GH#13779's verbatim repro | **clean on 6.4.1** |

### Plan evidence

`EXPLAIN` succeeds for every variant; the failure is at plan→executor conversion or, in the silent
face, produces a plan that simply reads the wrong input index. The error message is the direct
evidence of the shift: the value quoted (`dup`, a `name`/`created_at` value) and the target type
(`bigint`, `id`'s type) are one position apart, and in the silent face the wrong output values are
literally the neighbouring column's.

## How it was found

eqgen v3 data-equivalence oracle, `cratedb_run4` round 143, seed 600899756 — **six findings from one
round**, all sharing one equivalence chain. Base `t` (a plain table) and equivalent `t` hold the same
8 rows (admissibility verified for all six). The base answered 7 rows; the equivalent raised `XX000`.

The oracle's contribution here is that it *builds* the trigger. The add-then-drop-a-column view
round-trip is a canonical row-preserving rewrite — precisely the kind of thing an equivalence
generator emits and a human would never write by hand — and the query needed nothing exotic at all
(`GROUP BY` + `HAVING MIN(...)`). A query-rewrite oracle (TLP / NoREC / EET) holds the data fixed, so
it would have to be handed the `UNION ALL`-over-a-widened-view relation to begin with; and its own
rewrites work against the trigger, since wrapping the query in an aggregate or splitting the
predicate changes exactly the projection width and grouping that are load-bearing.

Worth flagging: the loud face is what the harness caught, but the **silent** face is the same defect
and the oracle is only partly able to see it — for a query where the shifted column's type is
compatible, both sides must still disagree for the harness to notice. The wrong-result case in PART 3
was constructed during triage, not found by the fuzzer.

- Reduced repro, wrong-result case and controls: [`reduced.sql`](reduced.sql)
- Original findings: hunt log `error_round143_{0..5}.sql`
