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

# CrateDB: `UNION ALL` + `ORDER BY` on an unselected column leaks the sort column into the output row — regression of GH#17341, closed as fixed in 5.10.1, still broken in 6.4.1

## Summary

`SELECT <subset> FROM <UNION ALL relation> ORDER BY <column not in the select list>` fails with
`XX000` on CrateDB 6.4.1 whenever a third union output column is used by neither the select list nor
the `ORDER BY`. The `ORDER BY` column is retained in the result row and never projected away, so the
row carries one more value than the client was told to expect — the error message prints the leaked
value outright (`Row: RowN{[a, 1]} types: [VarCharType]`).

This is **GH#17341**, reported against 5.9.9 / 5.10.0, closed 2025-02-06 as fixed by PR #17365. Both
of CrateDB's own published artifacts for that fix still fail on 6.4.1: the example printed in the
**5.10.1 release notes as the fixed case**, and **#17341's verbatim reproduction**. PR #17365 changed
`EvalProjection.castValues` to tolerate a source wider than its target rather than removing the extra
column, so the aliased form still trips the original planner exception at the same node
(`Eval[id AS q]`), and the unaliased form now fails one layer later, in the PostgreSQL wire encoder.

**The two faces are not equally reachable, and this decides whether you can reproduce it at all.**
Measured on one 6.4.1 node, both transports, same statements (see *Transport dependence* under
Characterization):

| select item | HTTP `/_sql` (`crash`, Admin UI, `curl`, crate-python) | PostgreSQL wire, port 5432 (psycopg, `psql`, pgJDBC) |
|---|---|---|
| **aliased** (`SELECT c3 AS q …`) | **`XX000 Index 1 out of bounds`** | **`XX000 Index 1 out of bounds`** |
| **unaliased** (`SELECT c3 …`) | *succeeds, correct rows* | **`XX000` column-count mismatch** |

The unaliased face lives in the server's PostgreSQL wire encoder (`Messages.sendDataRow`), which the
HTTP endpoint never runs — the HTTP serializer emits by declared column name and drops the extra
value, so the same query returns the right answer there. **File and verify against the aliased form**:
it is transport-independent and it is the shape #17341 originally reported.

Reproduces with assertions **on and off**, so a stock production node is affected. No silent data
corruption: over HTTP the unaliased form returns correct rows, and `COUNT(*)` over the same shape and
`INSERT … SELECT` of it both produce correct results on both transports.

## Environment

| | |
|---|---|
| Engine | CrateDB **6.4.1** (official release tarball) |
| Assertions | fails with `-ea -esa` **and** with `-da -dsa` — not assertion-dependent |
| Transport | aliased form fails on **both** HTTP `/_sql` and the PostgreSQL wire; unaliased form fails **only** on the PostgreSQL wire (eqgen talks psycopg, which is why the finding wears the unaliased face) |
| Regressed from | GH#17341 fixed by PR #17365, merged 2025-02-06, shipped in 5.10.1 |
| Session | `search_path=<per-connection schema>`, `enable_hashjoin=true`, `error_on_unknown_object_key=true`, `insert_select_fail_fast=true`, `optimizer_equi_join_to_lookup_join=false`. **No setting is load-bearing.** |
| Shards | 1 shard; the failure is in projection/encoding, not distribution |
| Determinism | deterministic; requires ≥ 1 result row (empty relation returns 0 rows cleanly) |

## Minimal repro

Setup (both forms share it):

```sql
CREATE TABLE b (c1 BIGINT, c2 TEXT, c3 TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (1, 'n', 'a');
INSERT INTO b VALUES (2, 'm', 'z');
REFRESH TABLE b;
```

**Form A — aliased select item. Fails on every client; this is the one to file.** It is also
#17341's original message and node shape verbatim:

```sql
SELECT c3 AS q FROM (SELECT * FROM b UNION ALL SELECT * FROM b) x ORDER BY c1;
```

```
XX000  Couldn't create execution plan from logical plan because of:
       Index 1 out of bounds for length 1: Eval[c3 AS q] (rows=unknown)
         └ Rename[c3] AS x (rows=unknown)
           └ Union[c3] (rows=unknown)
             ├ OrderBy[c1 ASC] (rows=unknown)
             │  └ Collect[…b | [c3, c1] | true] (rows=unknown)
```

**Form B — unaliased select item. PostgreSQL wire only** (psycopg / `psql` / pgJDBC; over HTTP
`/_sql` this returns the correct four rows):

```sql
SELECT c3 FROM (SELECT * FROM b UNION ALL SELECT * FROM b) x ORDER BY c1;
```

```
XX000  Number of columns in the row must match number of columnTypes.
       Row: RowN{[a, 1]} types: [io.crate.protocols.postgres.types.VarCharType]
CONTEXT: io.crate.protocols.postgres.Messages.sendDataRow(...)
```

`c3 = 'a'` **and** the leaked sort key `c1 = 1` are both in the row; one column type was declared.
Form B is the more direct evidence of the mechanism — the error prints the leaked value — but it only
surfaces where the PG-wire encoder checks the row against the row description.

## Expected vs actual

| Query | Expected | Actual (HTTP `/_sql`) | Actual (PG wire) |
|---|---|---|---|
| Minimal repro **form A** (aliased) | 4 rows | **`XX000 Index 1 out of bounds for length 1`** | **`XX000 Index 1 out of bounds for length 1`** |
| Minimal repro **form B** (unaliased) | 4 rows: `a, a, z, z` | 4 rows: `a, a, z, z` | **`XX000` column-count mismatch** |
| Form A with assertions off (`-da -dsa`) | 4 rows | **`XX000 Index 1 out of bounds for length 1`** | **`XX000 Index 1 out of bounds for length 1`** |
| **5.10.1 release-note example, verbatim** (unaliased) | 4 rows | 4 rows | **`XX000`** |
| 5.10.1 release-note example, aliased | 4 rows | **`XX000`** | **`XX000`** |
| **GH#17341 verbatim repro** (unaliased) | 2 rows | 2 rows | **`XX000`** |
| **GH#17341 repro, aliased** — its originally reported symptom | 2 rows | **`XX000`** | **`XX000`** |
| Same shape, branches projecting only the used columns | 4 rows: `a, a, z, z` | 4 rows | 4 rows |
| `COUNT(*)` over the failing shape | `4` | `4` | `4` |
| `INSERT INTO dst (c3) SELECT …` of the failing shape | `a, a, z, z` | correct | correct |
| Original workload query on base `t` (plain table) | 7 rows | 7 rows | 7 rows |
| Original workload query on the row-identical equivalent `t` | 7 rows | **`XX000 Index 1 out of bounds`** | **`XX000 Index 1 out of bounds`** |

The original finding's own select item is aliased (`… AS expr_0_varchar`), so **the finding as eqgen
recorded it is the transport-independent face** — it is only the distillation that, by dropping the
alias, slid onto the PG-wire-only face.

## Equivalence construction

### (1) The construct as the eqgen builder emits it

The equivalent `t` is the **predicate-split partitioning builder**: split the base rows into an
even-`id` half and an odd-or-NULL-`id` half, route each half through a different physical
representation, and `UNION ALL` them back. Verbatim from `logs/cratedb_run4/error_round149_0.sql`:

```sql
ALTER TABLE t RENAME TO t__base;

-- half A: the even-id rows, as a view straight over the base table
CREATE VIEW t__base_view_1 AS SELECT id, name, created_at FROM t__base WHERE MOD(id, 2) = 0;

-- half B: the odd/NULL-id rows, via copy -> INDEX OFF copy -> FULLTEXT-indexed copy
CREATE TABLE t__base_table_1 (id BIGINT, name TEXT, created_at TEXT) …;
INSERT INTO t__base_table_1 (id, name, created_at) SELECT id, name, created_at FROM t__base;
CREATE TABLE t__base_table_2 (id BIGINT INDEX OFF, name TEXT INDEX OFF, created_at TEXT INDEX OFF) …;
INSERT INTO t__base_table_2 (id, name, created_at) SELECT id, name, created_at FROM t__base_table_1;
CREATE VIEW t__base_view_2 AS SELECT id, name, created_at FROM t__base_table_2;
CREATE TABLE t__base_table_3 (id BIGINT, name TEXT, created_at TEXT) …;
INSERT INTO t__base_table_3 (id, name, created_at)
  SELECT * FROM t__base_view_2 WHERE (MOD(id, 2) <> 0) OR (id IS NULL);
CREATE TABLE t__base_table_4 (id BIGINT, name TEXT, created_at TEXT,
  INDEX t__base_idx_2 USING FULLTEXT (name, created_at) WITH (analyzer='english')) …;
INSERT INTO t__base_table_4 (id, name, created_at) SELECT id, name, created_at FROM t__base_table_3;
CREATE VIEW t__base_view_3 AS SELECT id, name, created_at FROM t__base_table_4;

-- the load-bearing link
CREATE VIEW t AS SELECT * FROM t__base_view_1 UNION ALL SELECT * FROM t__base_view_3;
```

and the workload query:

```sql
SELECT t1.created_at AS expr_0_varchar
FROM t AS t1
WHERE t1.created_at <= ALL (SELECT CAST(CAST(NULL AS TIMESTAMP WITHOUT TIME ZONE) AS TEXT)
                                   || (CASE WHEN True THEN '𒀀' ELSE '' END) AS expr_0_text
                            FROM t AS t2 WHERE CAST(3 AS BIGINT) IS NULL)
ORDER BY t1.id;
```

It selects `created_at`, orders by `id` (**not** selected), and never references `name` at all —
which is exactly ingredients 2 and 3 of the trigger.

**Mapping onto the distilled repro:** `t` (the `UNION ALL` view) → the inline
`(SELECT * FROM b UNION ALL SELECT * FROM b) x`; the selected `created_at` → `c3`; the
`ORDER BY t1.id` → `ORDER BY c1`; the untouched `name` → `c2`, the column whose pruning is required.
The `<= ALL (…)` subquery, the `𒀀` literal, the `NULL::timestamp::text` concatenation, and the
always-false subquery filter are all dropped — none of them matters.

### (2) The load-bearing construct — a construct × query-feature composition

`UNION ALL` relation **×** `ORDER BY` an unselected column **×** a third column pruned away. All
three are necessary and none is sufficient:

- Drop the union (plain table, or a single-branch derived table, or a non-union view) → clean.
- Use `UNION` (distinct) instead of `UNION ALL` → clean.
- `ORDER BY` a *selected* column, or `ORDER BY 1`, or no `ORDER BY` → clean.
- Remove the unused column — either by selecting it, by adding it to the `ORDER BY`, or by having the
  union branches project only the used columns → clean. A 2-column table cannot reproduce it at all.

A view and an inline derived table behave identically, so despite the earlier CrateDB findings in
this repro folder this is **not** view-specific.

### (3) Constructs reduced away

The whole physical-representation ladder in half B: the plain copy, the `INDEX OFF` copy, the
`FULLTEXT`-index copy, and the two wrapper views. Also the predicate split itself — the two branches
do not need disjoint predicates, or even different relations; `SELECT * FROM b UNION ALL SELECT * FROM b`
suffices. On the query side, the entire `<= ALL (subquery)` construct went away: 16 setup statements
and a correlated-subquery workload query reduced to a 4-statement repro with no subquery.

## Minimal oracle exposure path

**Object composition arity:** `2`

**GCL builder path:** `PartitionUnionQueryBuilder` → `CreateViewBuilder`

**Confidence:** verified

**Realization:** A `VIEW` exposes the row-preserving `UNION ALL` relation.

**Workload/data requirements (excluded from arity):**
- `ORDER BY` a column omitted from the select list.
- At least one additional union column that is neither selected nor sorted.
- At least one result row.
- An aliased select item for the transport-independent failure face.

**Exposure vs. intrinsic trigger:** The predicate split chosen by `PartitionUnionQueryBuilder` reduces away, but its `UNION ALL` relation shape remains in the standalone trigger; the final view can be replaced by an inline derived table. The builders supplied the row-identical union contrast, while the intrinsic failure is union projection pruning combined with an unselected sort key.

## Characterization

### Mechanism, and why PR #17365 did not finish the job

PR #17365 ("Fix eval projection to support fewer outputs than its source", merged 2025-02-06) is a
two-line change:

```diff
 public static EvalProjection castValues(List<DataType<?>> targetTypes, List<Symbol> sources) {
     ArrayList<Symbol> casts = new ArrayList<>(targetTypes.size());
     boolean requiresCasts = false;
-    for (int i = 0; i < sources.size(); i++) {
+    assert sources.size() >= targetTypes.size() : "sources size must be >= targetTypes size";
+    for (int i = 0; i < targetTypes.size(); i++) {
         Symbol source = sources.get(i);
         DataType<?> targetType = targetTypes.get(i);
```

It stops `castValues` from indexing `targetTypes` past its end — but it does not remove the extra
`ORDER BY` symbol from the row. The row still arrives at the top of the plan one value too wide, so
the failure simply relocated:

- **unaliased select item** — no `Eval` cast node is built, the wide row flows to the top, and the
  PostgreSQL encoder rejects it in `Messages.sendDataRow` against the 1-column row description. This
  is the "Number of columns in the row must match number of columnTypes" face, and the message prints
  the leaked value.
- **aliased select item** — an `Eval[c3 AS q]` node *is* built on a path that still walks the source
  symbols, and the original `Index 1 out of bounds for length 1` fires at exactly the node shape
  shown in #17341. So the reported symptom was never actually fixed for this form.

Consistent with "the row is right, its shape is wrong": `COUNT(*)` over the failing shape returns the
correct `4`, and `INSERT INTO dst (c3) SELECT …` of the failing shape writes the correct four rows.
The defect is confined to producing an output row of the declared width.

### What triggers it / what does not

| Variant | Result |
|---|---|
| `SELECT c3 FROM (b UNION ALL b) ORDER BY c1`, `c2` unused | **`XX000`** wire column-count mismatch |
| same, select item aliased | **`XX000` `Index 1 out of bounds`** |
| same, as a `CREATE VIEW` instead of a derived table | **`XX000`** |
| same, assertions off | **`XX000` `Index 1 out of bounds`** |
| 4 columns, select 1, order by 1, 2 unused | **`XX000`** |
| 3-branch `UNION ALL` | **`XX000`** |
| branches project only the used columns (nothing pruned) | clean, correct |
| select the unused column too (`SELECT c2, c3`) | clean, correct |
| add the unused column to the `ORDER BY` (`ORDER BY c1, c2`) | clean, correct |
| `ORDER BY` a selected column | clean, correct |
| `ORDER BY 1` (positional → the selected column) | clean, correct |
| no `ORDER BY` | clean, correct |
| `UNION` (distinct) | clean, correct |
| single-branch derived table / non-union view / plain table | clean, correct |
| 2-column table (no column left to prune) | clean, correct |
| empty relation | clean, 0 rows |
| wrapped in `COUNT(*)` | clean, correct (`4`) |
| `INSERT INTO … SELECT` of the failing shape | clean, correct data |

All rows above were measured over the **PostgreSQL wire**. Re-measured over HTTP `/_sql`, every
*aliased* variant behaves identically and every *unaliased* variant that fails on the wire instead
returns the correct rows — see below.

### Transport dependence — which client you use decides whether you see it

Measured on one 6.4.1 node, same statements, both endpoints of the same process (`http.port` and
`psql.port`):

| Shape | HTTP `/_sql` | PostgreSQL wire (port 5432) |
|---|---|---|
| distilled repro, aliased (`SELECT c3 AS q …`) | `XX000 Index 1 out of bounds for length 1: Eval[c3 AS q]` | same |
| distilled repro, unaliased (`SELECT c3 …`) | **OK — `a, a, z, z`** | `XX000 Number of columns in the row must match number of columnTypes. Row: RowN{[a, 1]}` |
| 5.10.1 release-note example, unaliased | **OK — `1, 1, 2, 2`** | `XX000 … Row: RowN{[1, alice]}` |
| 5.10.1 release-note example, aliased | `XX000 Index 1 out of bounds` | same |
| GH#17341 verbatim repro, unaliased | **OK — 2 rows** | `XX000 … Row: RowN{[1, 1785861883320]}` |
| GH#17341 repro, aliased | `XX000 Index 1 out of bounds` | same |
| `COUNT(*)` control | OK `4` | OK `4` |

The mechanism explains the split exactly. The unaliased path builds no `Eval` node, so the too-wide
row survives to the top of the plan and is caught only where a row is checked against a declared row
description — `io.crate.protocols.postgres.Messages.sendDataRow`, which exists only on the PG-wire
path. The HTTP `_sql` serializer emits by declared column name and drops the extra value, so the
answer comes back correct. The aliased path fails during plan→executor conversion, before any
transport is involved, and therefore fails everywhere.

**Consequences for triage and for filing.** A triager reaching for `crash`, the Admin UI, `curl
/_sql`, or crate-python will conclude the unaliased repro "does not reproduce"; one reaching for
`psql`, psycopg, or the pg JDBC driver will see it. Quote the **aliased** form as the repro. It also
means the unaliased face carries no wrong-result exposure on HTTP — there, the leak is invisible and
harmless, which bounds the severity of that face to "PG-wire clients get an error".

### One unresolved sensitivity in the multi-view chain

While re-verifying, a hand-rebuild of the finding's chain in which **every relation inside the view
bodies was written schema-qualified** (`CREATE VIEW sch.t AS SELECT * FROM sch.t__base_view_1 UNION
ALL …`) did **not** reproduce on either transport, while the identical chain built with unqualified
names under `SET search_path` reproduced on both. For the distilled `b` shape, by contrast,
qualifying the *reference* (`FROM sch.v`, `FROM (… sch.b …) x`) changes nothing — it fails either way.
So something about name qualification inside a chained view body suppresses it in the chain form; I
did not isolate which layer. Session settings are *not* the variable here: `insert_select_fail_fast`
and `error_on_unknown_object_key` were toggled independently and made no difference, so
"no setting is load-bearing" still holds. Practical consequence: reproduce with unqualified names
under `search_path` (or in the default `doc` schema), the way an ordinary client connects.

### Plan diff

The logical plan is where the leak is visible, and CrateDB prints it in the error itself. From the
original finding, the outer branch of the `MultiPhase` plan:

```
Eval[created_at AS expr_0_varchar] (rows=unknown)
  └ Rename[created_at] AS t1 (rows=unknown)          <-- 1 column declared
    └ Rename[created_at] AS …t (rows=unknown)
      └ Union[created_at] (rows=unknown)              <-- 1 column declared
        ├ Rename[created_at, id] AS …t__base_view_1   <-- 2 columns actually produced
        │  └ OrderBy[id ASC]
        │    └ Collect[…t__base | [created_at, id] | …]
        └ Rename[created_at, id] AS …t__base_view_3   <-- 2 columns actually produced
           └ OrderBy[id ASC]
             └ Collect[…t__base_table_4 | [created_at, id] | …]
```

`OrderBy[id ASC]` is pushed **below** the `Union`, so each branch must carry `id` — and the branch
`Rename` nodes correctly say `[created_at, id]`. But `Union` and everything above it declare
`[created_at]` only. Two columns flow into a one-column contract; nothing ever drops `id`.

`EXPLAIN` succeeds for every variant above (the failure is at plan→executor conversion and at row
encoding), so there is no `EXPLAIN` diff to show — the plan dump in the error message is the
authoritative artifact, which is why it is quoted here instead.

## How it was found

eqgen v3 data-equivalence oracle, `cratedb_run4` round 149, seed 1477293808. The oracle **holds the
query fixed and swaps in a row-identical relation**: base `t` (a plain table) and equivalent `t` (two
predicate-split halves `UNION ALL`'d back together, one of them routed through an `INDEX OFF` copy and
a `FULLTEXT`-indexed copy) hold the same 8 rows. The same generated workload query returned 7 rows
against the base and `XX000` against the equivalent.

This is the archetypal case for a data-equivalence oracle. The trigger is a property of the
*relation* — that it is a `UNION ALL` of branches wider than the query's projection — and the base
table simply cannot express it. TLP / NoREC / EET hold the data fixed and rewrite the query, so they
would need the user to have already written the `UNION ALL`; and their own rewrites work against the
trigger, since partitioning a predicate into a three-way `UNION ALL` changes the projection width and
wrapping the query in an aggregate removes the client-facing row entirely (exactly the `COUNT(*)`
control that comes back clean). Here the oracle generated the `UNION ALL` itself, as an ordinary
row-preserving rewrite, and the query only had to `ORDER BY` a column it did not select.

Worth noting for the generator: this round's chain also contained `INDEX OFF` and `FULLTEXT` links,
the same trap that mis-attributed
[`cratedb-run3-round19`](../cratedb-run3-round19-window-order-by-duplicate-assert/bug_report.md). Both
are irrelevant again — the load-bearing link was the plain `UNION ALL` at the top.

- Reduced repro, regression evidence and controls: [`reduced.sql`](reduced.sql)
- Original finding: hunt log
