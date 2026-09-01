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

# Composable Oracle Algebra for Testing DBMS Engines

Take a table. Build a second database object that holds exactly the same rows — a view, a chain of
views, a copy, two halves unioned back together, a table macro. Run the same query against both.

```sql
-- database 1                      -- database 2
CREATE TABLE t (c_int BIGINT);     CREATE TABLE t__base (c_int BIGINT);
                                   CREATE VIEW  t AS
                                     SELECT * FROM t__base WHERE MOD(c_int, 2) = 0
                                     UNION ALL
                                     SELECT * FROM t__base WHERE MOD(c_int, 2) <> 0
                                                                 OR c_int IS NULL;

SELECT c_int FROM t ORDER BY c_int;   -- the identical query, on each
```

Different answers mean an engine bug. The rows are the same by construction, and the query text is
byte-for-byte identical, so the only thing that differed is the object being read.

```
$ python -m eqgen.fuzz.cli --dialect duckdb --rich --rounds 12
engine   : duckdb v2.0.0-dev… (source_id)     # CLI from artifacts.duckdb.org/latest
run dir  : eqgen/log/duckdb_20260804-005406
seed     : 4242   (rerun with --seed 4242 to replay this sequence)
catalog  : rich  (rows re-sampled each round, n=8)
schedule : flat, query-phase 5–5s
round 0 (seed 1785390952): budget 5.0s, 328 queries -> 328 pass, 0 finding(s), 0 skipped
...
rounds 12, queries 1500, pass 1500, skipped 0
findings 0
```

No SQL translator, and nothing external required beyond `pip install` and a network fetch of the
DuckDB CLI. Nine engines supported: **DuckDB, PostgreSQL, MySQL, ClickHouse, SQLite, MariaDB, TiDB,
CrateDB, Dolt**. A query generator works on top of eqgen to generate predicates and workload queries.

## Why the results are trustworthy

The hard part of this kind of testing is checking the test itself. The usual approach generates **two
different queries** claimed to return the same rows:

```sql
-- claimed equal. Are they? You have to prove it, for every pair you generate.
SELECT COUNT(*) FROM t WHERE p
SELECT (SELECT COUNT(*) FROM t WHERE p) + (SELECT COUNT(*) FROM t WHERE NOT p) ...
```

Get that wrong and you report a bug that is not there. Proving it right is hard for one rewrite and
much harder for ten stacked together — every rewrite you add adds to what you have to prove.

Here the queries are identical, so there is nothing to prove about them. The thing that differs is
the object, and you can just **read both objects and compare their rows**:

```sql
SELECT c_int, c_txt FROM t   -- run on both databases, compare row by row, ignoring order
```

So a ten-layer object is not a ten-step proof. It is one more object, checked by reading it.

## Why the same query behaves differently against a different object

- **It compiles differently.** A view gets folded into the query reading it; a table does not. A
  macro is inlined. Which optimizations are even possible depends on what is being read.
- **It runs differently.** Different scan code, physical layout and statistics. A table that has
  been deleted from and refilled is not physically the same as one only ever created.

## Getting started

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m eqgen.fuzz.cli --help

python -m eqgen.fuzz.cli --dialect duckdb --rounds 20                     # run it (DuckDB by default)
```

`--dialect` takes `duckdb`, `postgres`, `mysql`, `mariadb`, `tidb`, `dolt`, `cratedb`, `sqlite` or
`clickhouse`.


Each run writes a directory under `eqgen/log/` (change it with `--workdir`): one log per round, plus
a runnable `.sql` file for every finding that rebuilds both databases from nothing.

A round's log holds both halves of what happened — the object that was built, then the queries:

```
-- ==== equivalence: CreateView, depth 3, 3 nodes [CreateView 1, SelectQuery 1, ...] ====
--   CreateView t
--     SelectQuery
--       BaseTableSource t__base
-- ==== equivalence DDL ====
--   CREATE VIEW t AS SELECT * FROM t__base;
-- => equivalence check: OK (rows and declared types agree)
SELECT c_int FROM t WHERE c_big > 0;
-- => PASS
```

The object is written as comments, so the same file replays as a list of queries
(`--corpus round3.log`) without re-running the `CREATE`.

## Using your own query generator

Two places take SQL as **text**, and [`plugins.py`](eqgen/plugins.py) is all of it:

```python
class PredicateSource(Protocol):
    def boolean_predicate(self, table: Table, *, seed: int) -> str | None: ...

class QuerySource(Protocol):
    def iter_queries(
        self, table: Table, *, seed: int, limit: int | None = None,
        exposed_names: Sequence[str] = (),
    ) -> Iterator[str]: ...
```

Strings, not syntax trees, so nothing outside needs to know this project's classes. Point SQLancer,
sqlsmith, or a captured production trace at it. `exposed_names` is the relation names installed this
round — `('t',)` normally, `('t0', 't1', …)` under `--forks`.

[`generators/example_generator/`](eqgen/generators/example_generator/) is a small `QuerySource` so
the tool runs out of the box. [`generators/typed_predicate/`](eqgen/generators/typed_predicate/) is
its `PredicateSource` counterpart — it builds a typed expression AST per dialect (own function
catalogs for Postgres/DuckDB/SQLite/MySQL-family/ClickHouse/CrateDB) and prints that to SQL, and is
the default (`--predicates typed`) rather than just a toy. 

An external generator can be plugged in
the same way to drive predicates and the workload — e.g.
[SQLancer++](https://github.com/sqlancer-plus-plus/SQLancerPlusPlus). Any plugged-in generator must be deterministic: the same predicate or query, given the same row,
always answers the same way. Otherwise a row can silently fall out of every branch, or into two, and results in false positives.

## Adding an engine

Subclass [`DialectAdapter`](eqgen/fuzz/adapter.py) — fifteen methods, all about your engine, of which
only **six** are abstract (`connect`, `base_table_ddl`, `literal`, `equivalence_config`,
`simple_catalog`, `rich_catalog`) — and list which rewrites it can run in a `.gcl` file that inherits
the shared one:

```
equivalence_generator_v3 = eqg3 {
    builder_weights: [Weighted] = [ ... ];   # weight 0 turns a rewrite off
};
```

**Weight 0 is a claim, so say which one.** Four different things get spelled `weight = 0` and only one
of them is right without further thought:

| Why it is off | What to do |
|---|---|
| The engine spells the construct differently | **Override the emitter**, don't disable. The semantics hold. |
| The engine lacks it, with no equivalent | Weight 0 — and write down which function is missing. |
| It has it, but the result is plan-dependent | Weight 0. The semantics genuinely do not hold. |
| Its paired builder is disabled | Neither: that is a config inconsistency, and a test now catches it. |

If your engine can express a rewrite no other can, add it — no change to shared code is needed. Read
[EXTENDING.md](EXTENDING.md) first: of the DuckDB-only rewrites in
[`dialects/duckdb/`](eqgen/dialects/duckdb/), two are written in a way that should not be copied.

Before turning a new rewrite on, run `--sweep`, which generates with that one builder plus the few
needed to produce anything, so a failure has one candidate rather than six. It reads the factory's own
record of which builders produced a node, so `not_exercised` means the builder really did decline —
pass `--predicates` to give predicate-dependent builders something to work with.

## Layout

```
core/               SQL types, catalog, statement — plain data, no SQL
ir/                 expression nodes, and how each engine writes them
builder/            the generic builder framework
config/             the .gcl config files and typed access to them
plugins.py          the two protocols you can implement (+ a file replayer)
equivalence/        the generator: nodes, constraints, builders, emitter
dialects/           one directory per engine: duckdb, postgres, mysql (+ mariadb),
                    clickhouse, sqlite, tidb, dolt, cratedb
generators/         query and predicate generators
fuzz/               database, comparison, round, report, sweep, CLI
tests/              including the rules that keep the layers apart
```

[ARCHITECTURE.md](ARCHITECTURE.md) is why it is shaped this way. [EXTENDING.md](EXTENDING.md) is how
to add a rewrite, an object kind, or an engine — including which of the three emitters to edit.

## Testing

```bash
pytest eqgen/ -m unit          # ~430 tests, about 90 seconds
```

The tests run things rather than asserting them. The identities are checked against a live DuckDB
over awkward rows — the three-way split really does cover every row, `p OR NOT p OR p IS NULL` really
is true for all of them — generated objects are built and compared row for row, and a full run is
asserted to find **nothing** on a correct engine. That last one matters: a tool that reports things
that are not bugs makes every real finding arguable.

## License

Copyright (c) Snowflake Inc. All rights reserved.

Licensed under the Apache 2.0 license.
