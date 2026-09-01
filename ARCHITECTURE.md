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

# Architecture

Why the code is shaped this way. What it does and how to run it is in [README.md](README.md); how to
add things is in [EXTENDING.md](EXTENDING.md).

---

## 1. Four layers

```
plugins.py              where generated SQL text comes in, and nowhere else
    ↓
equivalence/            put rewrites together into one object
    ↓
ir/ + dialects/         write that object as SQL for one engine
    ↓
fuzz/                   build both databases, run queries, compare
```

Each layer only imports from the ones above it — enforced by
`test_the_equivalence_core_does_not_import_the_harness`, not just convention. That's what keeps the
generator runnable with no database present, which is how every unit test runs it.

## 2. What the design rests on

**Each rewrite keeps the rows, so any stack of them does too.**

```sql
CREATE VIEW  t_view_1  AS SELECT * FROM t__base WHERE MOD(c_int, 2) = 0    -- keeps rows
CREATE TABLE t_table_1 AS SELECT * FROM t_view_1                           -- keeps rows
CREATE VIEW  t         AS SELECT * FROM t_table_1 UNION ALL ...            -- keeps rows
```

Ten layers deep is still the same rows, and nothing has to reason about the stack to know it.

**Rows are not enough — the declared types have to match too.** `ObjectComparison.equal`
(`fuzz/compare.py`) checks `rows.equal AND types_agree`, *before* any workload query runs:

```python
@property
def equal(self) -> bool:
    return self.rows.equal and self.types_agree     # base_types == equivalent_types
```

A rewrite can hold every value the base table holds and still change what a column is *declared*
as — a window-function CTAS dropping a `NUMERIC`'s scale, a computed column widening `INTEGER` to
`BIGINT`. Type-identical is what tells that apart from a real engine bug: row-identical but
type-changed is the generator's own mistake, not a finding, and this check catches it before a
workload query ever runs and gets blamed on the engine instead.

**A rewrite is correct because of its shape, checked by running it — never by comparing rows in
Python.** Each shape is a fact that holds whatever is in the table:

```sql
-- PartitionUnionQueryBuilder: the even rows plus the odd rows are all the rows
SELECT * FROM t WHERE MOD(c_int, 2) = 0
UNION ALL
SELECT * FROM t WHERE MOD(c_int, 2) <> 0 OR c_int IS NULL
```

So a builder assumes nothing about its children's data, and `meets_constraint` only ever checks
structure.

**Requirements go down as constraints; finished objects come back up.** A builder is never handed
the rows it must produce, only a description of them:

```
down:  "a relation holding only rows where MOD(c_int, 2) = 0"      <- a constraint
up:    CREATE VIEW t_view_1 AS SELECT * FROM t__base WHERE MOD(c_int, 2) = 0
```

An earlier design passed the target relation down directly. It could check a child had the right
columns, but couldn't say "the odd half" — only a constraint can — so it was removed.

**Generated SQL comes in as text, in one place: `plugins.py`.**

```python
PredicateSource.boolean_predicate(...)  ->  "c_int > 3"
QuerySource.iter_queries(...)           ->  "SELECT c_int FROM t"
```

Everything else the core builds is fixed structure — which is what lets a third-party generator meet
the boundary by emitting strings, nothing more.

**A predicate must answer the same way every time it's evaluated.** With `p = random() < 0.5`, on
one row:

```sql
SELECT * FROM t WHERE (p)            -- evaluates p: false, row not here
UNION ALL
SELECT * FROM t WHERE NOT (p)        -- evaluates p again: true, so NOT p is false, row not here
UNION ALL
SELECT * FROM t WHERE (p) IS NULL    -- not null either, row not here
```

The row is in no branch, the object silently loses it, and a bug gets reported that isn't there —
so determinism is the first thing `PredicateSource` requires.

## 3. Node kinds

| Kind | What it is |
|---|---|
| **Relations** | the objects — named, emit `CREATE` |
| **Queries** | the part after `AS` — never named, written inline |
| **Steps** | objects created, and actions done to them |
| **Expressions** | predicates, projections, `CASE` conditions |

Relations versus queries is *named versus inline*:

```sql
CREATE VIEW t_view_1 AS SELECT * FROM t__base
--          ^^^^^^^^    ^^^^^^^^^^^^^^^^^^^^^
--          a relation: has a name, so something else can read it
--                      a query: written here, never given a name of its own
```

An `EquivalentRelation` **is** an `EquivalentSource`, so "read the base table" and "read another
rewrite" are the same request — that one subclass relationship is where all the composability comes
from.

Steps exist because one object often takes several statements:

```sql
CREATE TABLE t_table_1 AS SELECT * FROM t__base    -- step 1, an Object
DELETE FROM t_table_1 WHERE MOD(c_int, 2) = 0      -- step 2, an Action
INSERT INTO t_table_1 SELECT ... WHERE MOD(...)    -- step 3, an Action
```

A flat ordered list means writing to an object needs no special machinery in the emitter.

Expressions are one class per shape (`Comparison`, `Case`, `WindowCall`, …), with **no
`FunctionCall(name, args)` catch-all**. One class per shape means everything that can reach generated
SQL is readable in one file, and an engine that spells one differently overrides one method — a
catch-all node makes both impossible.

`GeneratedPredicate` is the one exception: plugin text, wrapped in `AND`/`NOT`/`IS NULL` without
being understood, which is exactly why the boundary can be strings. It always renders inside
parentheses — given `a = 1 OR b = 2`, without them `NOT a = 1 OR b = 2` applies `NOT` to the first
comparison only, and rows fall out of every branch.

## 4. Builders, factory, constraints

A builder never names a child class. It asks for a *kind* of thing:

```python
build_subtree(QueryNode, ConstraintSet([RowFilterConstraint(even)]), context)
```

and the factory offers only the builders that return a `QueryNode`, list `RowFilterConstraint`, and
aren't too deep. Any of those may be picked, by weight:

```sql
SELECT * FROM t__base WHERE MOD(c_int, 2) = 0                         -- SelectStarQueryBuilder
SELECT CASE WHEN <always true> THEN c_int ... END FROM t WHERE MOD..  -- EetCaseColumnQueryBuilder
```

The factory enforces both halves, so neither is a builder's job:

```
constraint in the set      -> every builder that doesn't list it is skipped
constraint not in the set  -> nobody is skipped
```

So adding a new constraint disturbs no existing builder. The `required_` half — how one builder is
confined to appearing underneath another — is used by the expand/reduce pairs in
`builders/expansion.py`: a reducer mints a channel, an expander requires it, and a reducer never
supports its own channel, so the two can never nest inside each other.

## 5. Three emitters, not one

By the time an object reaches an emitter it's finished — names minted, steps decided. So an emitter
reads fields, changes nothing, and can run twice with the same output.

```
emit_equivalence   equivalence/emitter.py   walks the tree, decides statement order
  SqlEmitter        equivalence/emitter.py   a step → statements
    QueryRenderer    equivalence/emitter.py   a query node → str
      Spelling        ir/render.py           an expression or type name → str
```

An engine subclasses whichever it changes, in `dialects/<d>/emitter.py`, and each is passed into the
one above (`SqlEmitter(query_renderer=…)`, `QueryRenderer(spelling=…)`) — so changing the innermost
changes every expression in every statement without touching the other two. Which layer a given
change belongs in is a table in [EXTENDING.md](EXTENDING.md#1-the-three-emitters).

The tree is a **graph, not a tree** — one child can feed several parents, as a table read by every
branch of a union does. The walk is depth-first, each node visited once, so every object is created
before whatever reads it, exactly once.

Three principles here exist because violating them once produced silent wrongness:

- **SQL is never built during generation** — only an emitter writes text. Text written in `build()`
  is fixed to one engine before any dialect gets a say.
- **An emitter never looks inside an expression.** Whether a projection needs `AS` is decided by
  comparing rendered text against the alias, so plugin text behaves like anything else.
- **There is no translation step and no marker.** Each engine emits its own SQL from the same tree,
  rather than one dialect's SQL being converted to another's after the fact.

The default `Spelling` is PostgreSQL's, not an abstract "ANSI" — nobody can run ANSI, so a default
nobody can execute can't be checked. Every other dialect's `Spelling` subclasses it and overrides
only what genuinely differs.

## 6. Where to add things

| To add | Do this | Shared code changed |
|---|---|---|
| a rewrite | one builder, one config entry | none |
| an object kind | one node + one `build()` (+ a `visit_*` only if its statement shape is new) | none |
| an engine | one `DialectAdapter` + one `.gcl` | none |
| a rewrite only one engine can run | a node, a visitor method on that engine's subclass, a builder | none |
| a query or predicate generator | one protocol method | none |

DuckDB and PostgreSQL each carry a growing set of dialect-only rewrites and native emitter methods
(macros, indexes, `MERGE` upserts, generated columns, …) without any of it touching shared code —
that's the payoff row 4 is describing. [EXTENDING.md](EXTENDING.md) has the recipes and the ladder to
work down before adding a node.

## 7. Things not to do

Each of these is a line of code, so here is the line:

**Comparing rows in Python.**

```python
if set(child_rows) == set(base_rows):   # no. Ask the database; this reimplements the engine.
```

**A catch-all expression node.**

```python
FunctionCall("to_base64", [arg])   # no. Now "what can appear in generated SQL" is unanswerable
                                   # and every engine override becomes a name-mapping table.
```

**Building SQL text before the emitter.**

```python
def build(...):
    return Delete(where_sql=f"MOD({col}, 2) = 1")   # no. Fixed to one engine already; see §5.
```

**Creating an object the tree doesn't contain.**

```python
def visit_my_object(self, node):
    return [Statement(f"CREATE MACRO {node.macro_name}() AS TABLE ..."),
            Statement(f"CREATE VIEW {node.name} AS SELECT * FROM {node.macro_name}()")]
            #         ^ a second object, with no node. Nothing can compose with it and every
            #           tree walk under-counts it.
```

**Filtering generated queries instead of generating usable ones.**

```python
if "LIMIT" in query: continue    # no. Have the generator not produce one; then it's a property
                                  # of the code rather than a check that might have a gap.
```

**Broad known-issue patterns.**

```python
if "Error" in str(exc): return "known"      # no. Matches real bugs too, and they vanish quietly.
if "Conversion Error" in str(exc): return "duckdb-conversion-error"     # narrow, and labelled.
```

**Shipping a node no test runs.** A node kept without a builder still needs node-level tests, or it's
a claim with nothing behind it.
