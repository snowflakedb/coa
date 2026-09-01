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

# Motivating example

Take a table. Build a second object that holds exactly the same rows — not by copying them in
Python, but by stacking rewrites that each keep the bag: a view, a join that is an identity, a
filter that contributes nothing, a `UNION ALL` that puts the empty piece next to the live table.
Read both objects. If the rows match, run the **same** query on both.

A query-rewrite oracle (TLP, NoREC, two `SELECT`s claimed equal) cannot do this. It holds the
relation fixed and mutates the query, so it has to prove the two texts mean the same thing. Stack
ten rewrites and you have ten proofs. Here the query text never changes. The object does. A
ten-layer object is not a ten-step proof: it is one more relation, checked by reading it.

CrateDB 6.4.1 shows why that distinction is not academic. The engine agrees that the composed
object has the same rows as the heap. It then silently transposes columns — or deletes a live row
— the moment the query does anything other than `SELECT *` in table order. The trigger is a
property of the *relation* (an emptied equi-join sitting as the left arm of a `UNION ALL`). Query
oracles never build that relation.

## The algebra

`UnionEmptyRoundTripBuilder` is bag identity: \(R \cup (R \text{ WHERE FALSE}) = R\). The empty
arm is not required to be a bare `WHERE 1 = 0` on the heap. Any builder may sit under that
filter. In this case an identity equi-join does: self-join on a key, project the left side, same
rows as `R`. Three pieces, each row-preserving by shape:

```text
(a ⋈ a)          identity join — same rows, different plan
  WHERE 1 = 0    contributes nothing
UNION ALL
a                the live table
```

Empty ∪ all = all. That is the same move as the README's even/odd split, only one half is
literally empty. The original hunt stacked much more on top of this (EET `CASE` projections,
`ROW_NUMBER`-keyed rejoins of vertically split halves, `LAST_VALUE` windows, `OBJECT(STRICT)`
pack/unpack, three-way `UNION ALL`, `PARTITIONED BY` round-trips — twenty to thirty statements).
Reduction threw those away. The three pieces above are load-bearing. An identity
`CREATE VIEW v AS SELECT * FROM a` does **not** fire.

## The object, distilled

CrateDB 6.4.1 (`crate:6.4.1`, build `45bfa80`), one shard, no replicas. PostgreSQL wire via
`psycopg`. No session knobs.

```sql
CREATE TABLE a (c_pk BIGINT, id BIGINT, name TEXT, created_at TEXT)
  CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO a VALUES (1, 10, 'x', 'p');
INSERT INTO a VALUES (2, 20, 'y', 'q');
REFRESH TABLE a;

CREATE VIEW v AS
  SELECT * FROM (
    SELECT l.c_pk, l.id, l.name, l.created_at
    FROM a l JOIN a r ON l.c_pk = r.c_pk
  ) j
  WHERE 1 = 0
  UNION ALL
  SELECT * FROM a;
```

`v` is `UNION ALL` of an always-empty derived table and `a`. Its rows are exactly `a`'s rows.

## The check

eqgen does not trust the stack because the builders say so. It reads both sides:

```sql
SELECT * FROM a;
SELECT * FROM v;
```

Same two rows, `(1, 10, 'x', 'p')` and `(2, 20, 'y', 'q')`. Declared types match. Repeated runs
are stable. The object is admissible. A ten-layer chain would have been checked the same way —
one `SELECT *`, not a proof of each layer.

## The effect of stacking

The stack does **not** change the bag. That is the only fact the oracle needs, and it is checked
by the read above. Everything else about the object changes, and that is the point of stacking.

**What each rewrite does to the engine, not to the rows.** A heap table is one Collect. A view
is inlined; the query is planned against the view body. A CTAS is a different physical relation
(new Lucene index, new stats). An identity join is still a Join operator: two inputs, an `ON`,
an output that is left+right then a projection. A `WHERE 1 = 0` is a Filter the planner can
prove empty. A `UNION ALL` is two plans merged. `OBJECT` pack/unpack, windows, `ROW_NUMBER`
split/rejoin, `PARTITIONED BY` are the same idea: same bag, different operators, different
stats, different rules that are even *allowed* to fire.

**Composition is not additive.** The interesting object is not “a join” or “an empty filter” or
“a union.” It is a join *in the lineage of* a filter *that is the left arm of* a union. On this
bug:

| Stack | Rows | Engine |
|---|---|---|
| heap `a` | \(R\) | Collect. Both probe queries are correct. |
| identity `CREATE VIEW v AS SELECT * FROM a` | \(R\) | View merge onto Collect. Still correct. |
| `a UNION ALL (a WHERE 1=0)` — no join | \(R\) | Union of two table-shaped arms. Correct (`v_plain`). |
| `(a ⋈ a)` alone | \(R\) | Hash/NL join, then project left. Correct if you query it. |
| `(a ⋈ a) WHERE 1=0` alone | \(\emptyset\) | Empty join plan. Not the object `t`. |
| `(a ⋈ a) WHERE 1=0 UNION ALL a` | \(R\) | Union whose **left** child is an emptied join. **Wrong.** |
| CROSS JOIN in that empty arm | \(R\) | Different join plan, no leftover equi-join slots. Correct. |

The last row of the “wrong” line is the only illegal *engine* behaviour. Every other line is a
legal rewrite. Stacking is how you get the wrong line without writing it by hand: one builder
emits the join, another empties it, another unions it with the live table. None of them knows
about the CrateDB merge. The factory only knows each piece keeps the bag.

**What the original twenty-to-thirty-statement chain was doing.** The hunt did not emit the
distilled view. It stacked the CrateDB / shared builders until `t` was a deep DAG. Finding 1's
lineage, from the heap outward, was in essence:

```text
t__base
  → OBJECT(STRICT) pack / unpack          same rows; different column shape in the middle
  → LAST_VALUE window                     same rows; a Window in the plan
  → vertical split + ROW_NUMBER rejoin    same rows; an equi-join appears
  → EET per-column CASE                   same rows; computed outputs
  → always-false filter                   that branch is now empty
  → UNION ALL with other live branches    empty ∪ all = all
  → (PARTITIONED BY / extra views)        still all
```

Each step is a row-preserving rewrite. The *effect* of the whole stack on CrateDB is that the
left arm of `Union` is no longer a table. It is a Join that happens to produce zero rows, with
the join's output-slot layout still attached. The live arm still streams heap rows. The merge
believes the left layout. `SELECT *` still matches, because those bytes are the heap order. A
reorder or an `ORDER BY` on a dropped column uses the leftover join layout and lies.

The extra layers (window, `OBJECT`, three-way union, `PARTITIONED BY`) were not load-bearing
after reduction. They are why the hunt *reached* “join under empty under union” without aiming
at it. Early bisection showed the same thing from the other direction: flattening the
EET/`WHERE FALSE` wrapper made the bug disappear, but rebuilding *only* that wrapper did not
bring it back. The join several layers further down was still required. A one-rewrite fuzzer
that only wraps `CREATE VIEW t AS SELECT * FROM a`, or only emits `R UNION ALL empty` on the
heap, never puts a join in that lineage.

**What stacking does to the test.** Ten layers is still one `SELECT *` against the finished
object. Types are checked the same way. There is no extra proof per layer, and no Python-side
row reconstruction. If a layer had dropped a row or widened a type, the object gate would have
failed and the workload query would not have been blamed on CrateDB. The stack that shipped to
the engine was admitted as \(R\). The engine then compiled it as something that is not a table.

**What stacking does not do.** It does not make the query harder. The probes here are
`SELECT c_pk, id, created_at, name` and `SELECT name, created_at ORDER BY c_pk`. The stress is
the object. A query-rewrite oracle that stacked ten *query* rewrites would owe ten equivalence
proofs and would still be running them on `a`, where both probes are correct.

## The same query, two answers

Table-order full projection over `v` is still correct. That is why the object looked fine.

```sql
SELECT c_pk, id, name, created_at FROM v;
-- (1, 10, 'x', 'p'), (2, 20, 'y', 'q')     -- agrees with a
```

Ask for the **same four columns in another order** (Symptom A):

```sql
SELECT c_pk, id, created_at, name FROM v;
-- expected: (1, 10, 'p', 'x'), (2, 20, 'q', 'y')
-- actual:   (1, 10, 'x', 'p'), (2, 20, 'y', 'q')
--           created_at and name swapped; no error, no warning
```

Or project a subset and `ORDER BY` a column that is not in the select list (Symptom B):

```sql
SELECT name, created_at FROM v ORDER BY c_pk;
-- expected: ('x', 'p'), ('y', 'q')
-- actual:   ('x', 'p'), (NULL, NULL)     -- second live row is gone
```

Ground truth is `a`. `v` under `SELECT *` agrees with `a`. The two queries above disagree with
that ground truth in different ways (transposed values vs. a vanished row), not with each other.
CrateDB is the wrong side on both. Both shapes were re-run four times on fresh databases;
deterministic.

A third face (Symptom C) showed up on an 8-row finding and was not distilled to a from-scratch
minimal case: the same empty-join `UNION ALL` shape, later equi-joined to another table with the
condition in `WHERE` rather than `ON`, dropped five of eight rows and duplicated one survivor.

## Why a SELECT-query oracle misses it

TLP, NoREC, and “two `SELECT`s on one table” hold the relation fixed.

- On `a`, both queries are correct. There is nothing to disagree about.
- They never emit `CREATE VIEW v AS (join … WHERE 1=0) UNION ALL SELECT * FROM a`. The defect is
  not “this `SELECT` is wrong.” It is “this `SELECT` is wrong only after the optimizer inlines a
  composed object whose left `UNION ALL` arm is an emptied equi-join.”
- TLP on `v` is worse than silent on `a`. Symptom A returns the *table-order* bytes under every
  permutation of a full-width list that the oracle might try; the answers can look internally
  consistent while every one of them is the wrong mapping.

The probe queries here are trivial — `SELECT` four columns, or `SELECT` two and `ORDER BY` a
third. eqgen can use them because it varied the *object*. A query fuzzer that has to invent a
hard `SELECT` to stress the engine would not think to write these.

## What is load-bearing (controls)

Each control swaps one piece:

| Change | Symptom A / B |
|---|---|
| Table-order projection over `v` | correct |
| Subset reorder, no `ORDER BY` (`SELECT created_at, name FROM v`) | correct (A needs full width) |
| Empty arm is `WHERE 1=0` with **no** join (`v_plain`) | correct |
| Empty arm is a **CROSS JOIN** (no `ON`) | correct |
| `ORDER BY` a projected column (`ORDER BY name`) | correct (B needs an excluded sort key) |
| Drop `ORDER BY` on the Symptom B projection | correct |
| Join predicate is `c_pk` / `id` / `name` / `created_at` | all four reproduce A identically |
| Reorder the empty branch's own `SELECT` list | still transposes the last two slots of the *outer* reorder |

So: not “any `UNION ALL`,” not “any empty branch,” not “any join.” An **`ON`-qualified equi-join**
inside a branch that filters to zero rows, unioned with a live table. The join column and the
branch's projection order do not matter. A `CROSS JOIN` in the same position is clean.

The two original filings were reduced independently to that shape. The Symptom A view under the
Symptom B query produces Symptom B (vanished row, not a transposition). One `CREATE VIEW`, two
query masks, one root cause.

## How the hunt built it

eqgen's data-equivalence oracle, CrateDB simple catalog.

**Finding 1** (seed 31003, round 319): a `UNION ALL` of three branches. The load-bearing
arm was an EET per-column `CASE` projection emptied by an always-false filter, itself over a
`ROW_NUMBER`-keyed self-join that recombined two vertically-split halves of a `LAST_VALUE`-windowed,
`OBJECT(STRICT)`-packed source. Five of eight mismatches in that round went away if the workload
projection was rewritten to table order. A second run hit the same face on a 9-column table with a
shuffled `SELECT DISTINCT`. Early bisection was misleading: flattening the
EET/`WHERE FALSE` wrapper fixed the divergence, but rebuilding only that wrapper synthetically
did not reproduce. The equi-join several layers further down was the piece that had not been
reached yet.

**Finding 2** (seed 884601542, round 0): a 3-way `UNION ALL` whose branch 0 was a `RIGHT OUTER
JOIN` view emptied by an always-false EET filter. Three of four mismatches went away if `ORDER BY`
was dropped. Reduced, independently of Finding 1, to “empty branch containing an equi-join, unioned
with a live table, `ORDER BY` a column outside the projection.” Its remaining mismatch is Finding
1's symptom on that catalog.

The builders that *can* emit this shape, without claiming these were the only ones on the stack:

- `UnionEmptyRoundTripBuilder` — `R UNION ALL (R WHERE FALSE)`
- `PartitionUnionQueryBuilder` / `TlpPartitionUnionQueryBuilder` — same `UNION ALL` idea with
  non-empty partitions
- `EetCaseColumnQueryBuilder` / `EetDeterminedFilterQueryBuilder` — the always-false filter and
  per-column `CASE` that emptied a branch in the as-found SQL
- join / `ROW_NUMBER` rejoin builders — the identity equi-join in the empty arm's lineage

None of those builders is a bug. Each keeps the rows. The composition is legal. The engine's
handling of the composition is not.

## What the engine does

A `UNION ALL` is two plans merged. In CrateDB 6.4.1 the merge is wired from the **left** child
([`Union.java`](https://github.com/crate/crate/blob/6.4.1/server/src/main/java/io/crate/planner/operators/Union.java)
`build`):

- stream types ← `leftResultDesc.streamOutputs()`
- output width ← `lhs.outputs().size()`
- `ORDER BY` on the merge ← `leftResultDesc.orderBy()`

Here the left child is the emptied hash/NL join, not the table that emits rows. An equi-join's
physical row is not four heap columns. It is left+right, then a projection. That layout is still
on the left plan after the filter has proven the branch will produce **zero rows**.

The live arm streams ordinary table rows `(c_pk, id, name, created_at)`. The merge decodes them
as if they were join-output slots.

- `SELECT *` / table-order full projection happens to match the live row layout, so the object
  looks correct.
- A full-width **reorder** keeps those bytes and hangs different names on them (Symptom A). One
  reading is that a full-width Eval is treated as identity because the left (join) layout already
  claims to be “all the columns.”
- A subset plus `ORDER BY` an excluded column uses the left arm's sort/width; the second row
  lands as `(NULL, NULL)` (Symptom B) — consistent with a materialize/copy that uses the emptied
  join's row stride rather than the union's declared four slots.

The join is not “wrong data in the empty branch.” The empty branch has no data. It still **wins
the union's physical schema**.

That left-child wiring plus the controls is the evidence. The 6.4.1 tree was not stepped in a
debugger; `file:line` inside HashJoin / Eval pushdown is not claimed. The
`assert` that left and right `streamOutputs` are compatible is an `assert` — off in a stock
production node.

## Why this is the example

It is not a one-layer “identity view vs table” bug. Those are real, and eqgen finds them (MariaDB
window × `ANY` over `CREATE VIEW t AS SELECT * FROM b` is the clean short slide). They do not
show composition. This one does:

1. Each rewrite keeps the rows, so the stack does — empty ∪ all = all, even when the empty arm
   is itself a join.
2. eqgen checks that by reading `SELECT *`, not by proving the stack.
3. The same trivial query is then wrong, with no error.
4. A query-rewrite oracle cannot vary the thing that is wrong.
