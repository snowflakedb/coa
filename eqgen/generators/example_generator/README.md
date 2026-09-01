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

# `generators/example_generator` — not the project's query generator

This package is a **deliberately minimal reference implementation** of the two plugin
contracts in [`eqgen/plugins.py`](../../plugins.py). It exists for two reasons:

1. So the fuzzer runs immediately after install, with no JVM, no jar and no corpus.
2. So the contracts have a worked example that fits on one screen.

It is **not** what you should use to hunt bugs seriously. Use `--generator sqlancerpp` (Postgres
or DuckDB specialised forks — see the main README), sqlsmith, a captured production trace, or your
own generator — that is what the boundary is for. Nothing in `eqgen/equivalence/`, `eqgen/ir/` or
the dialects imports this package; the harness selects it as a runtime default from the CLI, and a
test enforces that the dependency only ever points this way.

## Why minimal is enough here

The **object** is the variable under test, not the query. A base table and its generated
equivalent hold the same rows by construction, so a workload query only has to reach those
rows by a *different route* on each side — a view folds into the query, a CTAS copy does
not; an indexed table prunes, a macro inlines. Plan-shape variety is what surfaces engine
bugs, and the small template space here produces plenty:

```sql
SELECT [DISTINCT] <items> FROM <t> [WHERE p] [GROUP BY g [HAVING h]] [ORDER BY o]
SELECT <cols> FROM (SELECT <cols> FROM <t> [WHERE p]) AS sub [ORDER BY o]
```

Spending effort on expression richness would be improving the half of the experiment that
is already controlled.

## Admissible by construction

Every workload query must be **invariant under row permutation and plan shape**, because
the two sides reach the same rows differently. Rather than generate freely and filter, the
grammar simply has no production for the things that would break it:

| Never emitted | Why |
|---|---|
| `LIMIT` / `OFFSET` | Without a total order they select an arbitrary subset, and the two sides may choose differently. |
| Nondeterministic functions (`random()`, `now()`, …) | Two evaluations disagree, so the comparison is meaningless. |
| `SUM`/`AVG` over `DOUBLE` | Adding floats in a different order can differ in the last bit, and comparing exactly calls that a mismatch. |
| Physical pseudo-columns (`rowid`) | Storage-dependent, so a table and its view disagree for identical rows. |
| Anything that writes | Would make the two databases diverge for reasons that are not findings. |
| More than one table | The equivalent is exposed under the base's name in its own database. |

Filtering after generation is what you are forced into when you cannot control the
generator. When you can, the contract becomes a property of the code — and
`eqgen/tests/example_generator_test.py` asserts it over a few thousand generated queries.

Predicates carry a related contract — deterministic, boolean, row-local, bare column names
— because the three-way row split covers every row only for a predicate that answers the same way
each time, and the always-true `CASE`'s
`p OR NOT p OR p IS NULL` is a tautology only for one. Sub-expressions are therefore fully
parenthesised: an unparenthesised `NOT` over a top-level `AND` binds to the wrong operand,
and rows then fall out of every branch.

## Writing your own

Implement one or both protocols — `iter_queries(table, *, seed, limit)` and
`boolean_predicate(table, *, seed)` — returning **SQL text for your target engine**. There
is no AST to learn and no conversion layer. Be deterministic in `seed` (a finding has to be
replayable) and draw from your own `random.Random` rather than the global module, since the
equivalence generator seeds the global RNG to make a round reproducible.
