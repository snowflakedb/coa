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

# Extending eqgen

How to add a rewrite, an object kind, or an engine. For *why*, see [ARCHITECTURE.md](ARCHITECTURE.md).

## 0. Where does my change go?

| I want to add | Write | Register in | Config |
|---|---|---|---|
| a rewrite every engine can run | `equivalence/builders/<theme>.py` | `builders/__init__.py` + `PORTABLE_BUILDERS` in `factory.py` | portable `.gcl` **and every dialect `.gcl`** |
| a rewrite one engine can run | `dialects/<d>/builders.py` (try not to add a node — §3) | that dialect's `extra_builders()` | that dialect's `.gcl` only |
| a new `CREATE …` object | AST node in `equivalence/ast.py`; `visit_*` only if the shape is new (§4) | `equivalence/visitor.py` if new | — |
| a new expression shape | node in `ir/expr.py` + branch in `Spelling.expr` | `ir/render.py` | — |
| an engine | `dialects/<d>/{adapter,emitter,ast,builders}.py` + `<d>.gcl` | `load_adapter` in `fuzz/cli.py` | own `.gcl` |
| a query/predicate generator | one class satisfying a protocol in `plugins.py` | passed at the CLI | — |

Only row 1 touches shared code. Everything else is additive.

## 1. The three emitters

```
emit_equivalence(root, emitter)      equivalence/emitter.py   walk tree → ordered statements
  SqlEmitter(SetupVisitor)           equivalence/emitter.py   setup step  → [Statement]
    QueryRenderer(QueryVisitor)      equivalence/emitter.py   query node  → str
      Spelling                      ir/render.py             expr/type   → str
```

Each is passed into the one above (`SqlEmitter(query_renderer=…)`), so changing `Spelling` changes
every expression without touching the other two. DuckDB:

```python
class DuckDBSpelling(PostgresSpelling):
    def type_sql(self, t): return duckdb_type(t)

class DuckDBQueryRenderer(QueryRenderer, DuckDBQueryVisitor[str]):
    def __init__(self): super().__init__(spelling=DuckDBSpelling())
    def visit_duckdb_anti_join_query(self, q): ...

class DuckDBEmitter(SqlEmitter, DuckDBSetupVisitor[list[Statement]]):
    def __init__(self): super().__init__(query_renderer=DuckDBQueryRenderer())
    def visit_duckdb_macro_object(self, n): ...
```

Which one do I edit?

| New SQL is | Edit |
|---|---|
| a whole statement (`CREATE MACRO …`, `MERGE …`) | `SqlEmitter` subclass |
| the body after `CREATE … AS` (a join shape, `QUALIFY`, `PIVOT`) | `QueryRenderer` subclass |
| part of an expression (`x % n`, a function, a type name) | `Spelling` subclass |

Rules:
- **Never build SQL text in `build()`.** A builder assembles nodes; only an emitter writes text.
- **`type_sql` is the only source of a type name.** Column DDL and `CAST` both go through it.

## 2. Adding a portable rewrite

Must return the same rows for *any* table:

```sql
SELECT * FROM t WHERE MOD(c_int, 2) = 0
UNION ALL
SELECT * FROM t WHERE MOD(c_int, 2) <> 0 OR c_int IS NULL     -- every row, once, always
```

Not "it gave the same rows when I tried it." Never compare rows in Python — the database does that.

A builder that can't do what's asked returns `None`; another builder gets the request.

```python
# equivalence/builders/<theme>.py
class DeterminedTrueFilterQueryBuilder(EquivalenceBuilder[SelectQuery]):
    """SELECT * FROM <src> WHERE <inbound filter> AND (p OR NOT p OR p IS NULL)."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return [RowFilterConstraint]

    def _build(self, constraint_set, context) -> Optional[SelectQuery]:
        predicate = self._generated_predicate(context)
        if predicate is None:
            return None                        # no plugin configured — decline
        source = self._dispatch_source(context, constraint_set)
        if source is None:
            return None
        where = expr.conjoin(self._current_filter(constraint_set), expr.determined_true(predicate))
        return SelectQuery(source, None, where)   # None = SELECT *
```

`conjoin`, not assignment — replacing the inbound filter would silently widen the row set.
Advertise a constraint only if you honour it.

Helpers on the base class (use these, don't subclass around them):

| Helper | Gives you |
|---|---|
| `_dispatch_source(context, cs)` | something for `FROM` — base table or another rewrite |
| `self.builder_factory.build_subtree(T, cs, context)` | any child, by return type |
| `_passthrough_items(context)` | one projection item per base column |
| `_current_filter(cs)` | the filter you were asked for |
| `_exposed_name(cs)` | name the outermost object must take, or `None` |
| `_generated_predicate(context)` | a predicate from the plugin, or `None` |
| `_select_key(candidates, context)` | one column, chosen per config |

**Never name a child class.** `_dispatch_source` returning the base table *or* a 3-deep chain is what
makes your rewrite compose with every other one for free.

Registration checklist (skipping one is the usual first bug):

1. Export from `equivalence/builders/__init__.py` (`__all__` too).
2. Add to `PORTABLE_BUILDERS` in `equivalence/factory.py`.
3. Weight it in `config/gcl/equivalence_generator_v3.gcl`.
4. Weight it in **every dialect `.gcl`** — lists replace, not merge. `test_the_dialect_declares_its_builder_set_in_gcl` fails if you forget.

An unlisted builder does **not** default to off — it defaults to weight 1 and fires unasked.
If it produces a named relation the workload can query, add it to `root_builder_weights` too;
if it produces a bare query node, don't.

Name it after what it does, not an engine — `test_portable_builders_do_not_claim_a_dialect` checks.

## 3. Adding a rewrite only one engine can run

**Try not to add a node.** Ladder, stop at the first rung that works:

1. **Existing node, different argument:**
   ```python
   JoinQuery(left, right, cond, "ANTI", projection, "l", "r")   # a keyword, not a new class
   ```
2. **Stack existing nodes** — a wrapper is a relation asked for above a query asked for
   (`_dispatch_source` / `build_subtree`).
3. **One small portable addition** unlocks rung 1 — an optional field, an IR leaf, a `Spelling` hook.
4. **Only now:** a dialect node — for a genuinely new *shape*, not a new keyword in an old one.

Example of rung 1, no core change needed — `ON 1=1` already exists:

```python
class LeftJoinEmptyQueryBuilder(EquivalenceBuilder[JoinQuery]):
    """LEFT OUTER JOIN with a provably-empty right side emits every left row once."""

    def supported_constraint_types(self): return []

    def _build(self, constraint_set, context) -> Optional[JoinQuery]:
        del constraint_set
        base_items = self._passthrough_items(context)
        if not base_items:
            return None
        left  = self.builder_factory.build_subtree(EquivalentRelation, ConstraintSet([]), context)
        right = self.builder_factory.build_subtree(
            EquivalentRelation, ConstraintSet([RowFilterConstraint(expr.eq(expr.int_lit(1), expr.int_lit(0)))]), context)
        if left is None or right is None:
            return None
        projection = tuple(
            ProjectionItem(i.alias, expr.qualified_col("l", i.alias, i.data_type), i.data_type)
            for i in base_items
        )
        return JoinQuery(left, right, expr.eq(expr.int_lit(1), expr.int_lit(1)),
                         "LEFT OUTER", projection, "l", "r")
```

Emits:

```sql
CREATE VIEW t__base_view_3 AS
SELECT l.c_int AS c_int, …, l.c_ts AS c_ts
FROM t__base_view_1 l LEFT OUTER JOIN t__base_view_2 r ON 1 = 1
```

One portable builder, every engine, no new node.

**Ask for a required child as a constraint, not a hard-coded literal:**

```python
# right side needs to be provably empty
build_subtree(EquivalentRelation, ConstraintSet([RowFilterConstraint(<1=0>)]), context)   # yes
CreateTable.build(namer, "SELECT * FROM t WHERE 1=0")                                     # no — loses variety
```
Any builder that applies filters can then serve that slot, so the empty side gets the same
composability as the live side.

**Column rule:**
```
asked for     -> always has the base table's columns and rows. _passthrough_items() is safe.
built by you  -> whatever you gave it. Track that yourself (e.g. KeyedRelation.base_items).
```

**Two anti-patterns to avoid:**
- **An emitter creating an object no node models** — e.g. one node that silently also emits
  `CREATE MACRO …` alongside its `CREATE VIEW …`. The macro is invisible to every tree walk. If a
  rewrite makes two objects, it needs two nodes.
- **`_passthrough_items(context)` over a child *you built* with different columns** — safe only
  over an *asked-for* child. Nothing catches the mismatch; it just emits invalid SQL.

If you do need a node, four steps in `dialects/<d>/`:

```python
# 1. ast.py — subclass, and fail loud in accept()
def accept(self, visitor: QueryVisitor[_T]) -> _T:
    if isinstance(visitor, DuckDBQueryVisitor):
        return cast(_T, visitor.visit_duckdb_my_shape(self))
    _unsupported(visitor, self)

# 2. emitter.py — implement visit_duckdb_my_shape, render only this node
# 3. builders.py — DuckDBFooBuilder (dialect-prefixed name, enforced by test)
# 4. duckdb.gcl + extra_builders() — weight it, return it from the adapter
```

Declare the dialect's `visit_*` with `raise NotImplementedError`, not `@abc.abstractmethod` —
abstract on the shared visitor breaks every other emitter.

## 4. Adding an object kind

**A new keyword in `CREATE … AS <query>`** (`TEMPORARY VIEW`, `MATERIALIZED VIEW`, `UNLOGGED TABLE`):
one node, one `build()`, no emitter/visitor change — existing `visit_view_object`/`visit_table_object`
already render it.

```python
class CreateMaterializedView(CreateFromQuery):
    @classmethod
    def build(cls, namer, query, *, exposed_name=None) -> "CreateMaterializedView":
        created, steps = cls._create_as_select(namer, query, "MATERIALIZED VIEW", "view", exposed_name)
        return cls(ObjectKind.MATERIALIZED_VIEW, query, steps=steps, exposed=created)
```

**A genuinely different statement shape** (a macro, `MERGE`): also needs an `Object` subclass +
a `visit_*`. Where you declare it:

| Kind | Declare `visit_*` | Cost |
|---|---|---|
| portable | `@abc.abstractmethod` on `SetupVisitor` | shared — every emitter must implement it |
| one engine's | `raise NotImplementedError` on that dialect's sub-protocol | additive |

Mint every name (including secondary ones) from `context.namer` at construction — there's no
second naming phase. One node can render several `Statement`s for **one** object
(create → delete → insert); a second *object* needs a second node.

## 5. Adding an engine

One `DialectAdapter` (`fuzz/adapter.py`) + one `.gcl`. Nothing shared changes.

```python
class MyEngineAdapter(DialectAdapter):
    name = "myengine"
    def equivalence_config(self) -> EquivalenceConfig: ...
    def emitter(self) -> SqlEmitter: ...
    def extra_builders(self) -> tuple[type, ...]: ...
    def connect(self) -> Connection: ...
    def base_table_ddl(self, table: Table) -> str: ...
    def literal(self, value: object) -> str: ...
    def engine_banner(self) -> str: ...
    def known_issue_label(self, exc: Exception) -> Optional[str]: ...
```

Then add it to `load_adapter` in `fuzz/cli.py`.

- **`known_issue_label` must stay narrow** — it demotes an error to "skipped, and counted". A broad
  match hides real bugs. Match specific text, one label per cause.
- **Catalogs are test design, not sample data** — include a type only because a rewrite needs a
  *reason* to touch it (parity key, a decimal that must be rejected, a text column for codecs).

## 6. Validating what you added

```bash
python -m eqgen.fuzz.cli --sweep --predicates typed --dialect duckdb
```

Runs with **one builder** plus the minimum needed to produce anything
(`CreateViewBuilder`, `SelectStarQueryBuilder`, `BaseTableSourceBuilder`), so a failure is
attributable.

| Verdict | Meaning |
|---|---|
| `ok` | built, ran, row-equivalent |
| `NOT EQUIVALENT` | **your builder is wrong** — a full run would have blamed the engine |
| `failed` | wouldn't build/run — usually a missing feature, harmless |
| `not_exercised` | declined every draw — not a failure, but don't enable it on this claim |

Always pass `--predicates` — without it, any builder calling `_generated_predicate` declines every
seed and reports `not_exercised` instead of `ok`.

Then:

```bash
pytest -m unit eqgen/tests/
mypy eqgen/
```

| Failing test | What you forgot |
|---|---|
| `test_every_registered_builder_is_listed_in_the_config` | weight in the portable `.gcl` |
| `test_the_config_lists_no_builder_that_does_not_exist` | removed a builder, left its weight |
| `test_the_dialect_declares_its_builder_set_in_gcl` | weight in a dialect `.gcl` |
| `test_every_configured_duckdb_builder_is_registered` | `extra_builders()` on the adapter |
| `test_every_dialect_builder_is_prefixed_with_its_dialect` | the `DuckDB…` prefix |
| `test_portable_builders_do_not_claim_a_dialect` | named a portable builder after an engine |
| `test_a_dialect_node_reaching_the_portable_emitter_fails_loud` | `accept` not checking the visitor |
| `test_the_equivalence_core_does_not_import_the_harness` | imported `fuzz/`/`dialects/` from `equivalence/` |
| `test_nothing_imports_the_monorepo_it_came_from` | a `yeti`/`common`/`snowflake` import |
| `test_no_sql_translator_is_reintroduced` | reached for `sqlglot` |

A full run's `eqgen/log/<run>/round<N>.log` records the object tree per round:

```
-- ==== equivalence: CreateView, depth 5, 11 nodes [CreateTable 3, MyNewQuery 1, ...] ====
```
