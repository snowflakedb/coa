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

# MySQL / MariaDB: `x NOT IN (<set-operation subquery>)` drops rows when the set operation's result is empty but its input contains NULL

## Summary

`x NOT IN (<empty set>)` is TRUE for every `x`. But when the empty set is produced by a
**parenthesized set operation** whose *input* contains NULL — e.g. `(SELECT id FROM b) EXCEPT (SELECT
id FROM b)`, which is a set difference of a set with itself and therefore empty — MySQL 9.7.2 and
MariaDB 12.3.3 silently drop rows, as if the empty `IN`-list contained NULL and the predicate had
collapsed to UNKNOWN.

**TiDB v9.0.0-beta.2 and DuckDB 2.0-alpha both return the correct answer.** TiDB is MySQL-*compatible*
but an independent implementation, so this is not intended MySQL semantics that a compatibility-focused
engine would have copied — it localises the defect to the MySQL/MariaDB shared code lineage.

The two engines are wrong differently: **MySQL is physical-order dependent** (rows scanned before the
NULL survive; rows after it are lost), **MariaDB is unconditionally wrong** whenever the subquery's
input contains a NULL. One column, two rows, and no eqgen equivalence are enough to show it.

## Environment

| | |
|---|---|
| Wrong on | **MySQL 9.7.2** `@008e09c2` (release, assertions off); **MariaDB 12.3.3** (release, assertions off) |
| Correct on | **TiDB v9.0.0-beta.2** `@3bea8196`; **DuckDB 2.0.0-alpha** |
| `sql_mode` | `ONLY_FULL_GROUP_BY,NO_BACKSLASH_ESCAPES,STRICT_ALL_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION` — **no mode is load-bearing** |
| charset / collation | `utf8mb4` / `utf8mb4_0900_bin` — not load-bearing |
| Determinism | deterministic for a given physical row order |
| Origin | `logs/mysql_run6/mismatch_round108_0.sql`; admissibility verified (base `t` ≡ equivalent `t`, 8 identical rows) |

## Minimal repro

```sql
CREATE TABLE b (id BIGINT);
INSERT INTO b VALUES (NULL),(1);

SELECT id FROM b
WHERE id NOT IN ((SELECT t5.id FROM b AS t5) EXCEPT (SELECT t6.id FROM b AS t6));
```

The subquery is a set difference of a set with itself, so it is **empty** — measured as 0 rows on all
four engines:

```sql
SELECT COUNT(*) FROM ((SELECT t5.id FROM b AS t5) EXCEPT (SELECT t6.id FROM b AS t6)) x;  -- 0
```

so `id NOT IN (…)` must be TRUE for both rows and the correct answer is both rows.

## Expected vs actual

| | Expected | MySQL 9.7.2 | MariaDB 12.3.3 | TiDB | DuckDB |
|---|---|---|---|---|---|
| **minimal repro** | `(NULL), (1)` | **`(NULL)`** ✗ | **`(NULL)`** ✗ | `(NULL), (1)` | `(NULL), (1)` |
| the subquery's `COUNT(*)` | `0` | `0` | `0` | `0` | `0` |

### Order matrix — MySQL depends on where the NULL physically sits, MariaDB does not

| rows in `b` | expected | MySQL | MariaDB | TiDB | DuckDB |
|---|---|---|---|---|---|
| `(NULL,1)` | 2 | **1** ✗ | **1** ✗ | 2 | 2 |
| `(1,NULL)` | 2 | 2 | **1** ✗ | 2 | 2 |
| `(NULL,1,2)` | 3 | **1** ✗ | **1** ✗ | 3 | 3 |
| `(1,2,NULL)` | 3 | 3 | **1** ✗ | 3 | 3 |
| `(1,NULL,2)` | 3 | **2** ✗ | **1** ✗ | 3 | 3 |
| `(1,2)` — no NULL | 2 | 2 | 2 | 2 | 2 |

With the NULL in the middle MySQL returns the rows up to and including it and drops the rest, which is
the shape of a flag being latched mid-scan.

### On the original finding's own base table

| | rows |
|---|---|
| correct (TiDB) | **8** |
| MySQL, base table (insert order) | 7 |
| MySQL, equivalent (`ROW_NUMBER` order, NULL first) | 1 |
| MariaDB, base table | 1 |

Worth noting: the oracle flagged base 7 vs equivalent 1, but the *correct* answer is 8 — **both MySQL
sides were already wrong**, just wrong by different amounts. The differential oracle detected the bug
without either side being right.

## Suspected mechanism

The `IN`-subquery's "contains NULL" flag appears to be computed from the set operation's **input scan**
rather than from its (empty) **output**, so `NOT IN` short-circuits to UNKNOWN and the row is filtered.
That single hypothesis accounts for every measurement:

- it needs a NULL in the **subquery's** input — a NULL in the outer table only is fine (C2), and a NULL
  in a *different* table that the subquery scans still breaks it (C3);
- MySQL's order dependence follows from the flag being latched when the NULL row is reached during the
  scan, leaving earlier outer rows unaffected;
- anything that puts a materialisation boundary between the set operation and the `IN` fixes it —
  wrapping the set operation in a derived table (C4) or materialising it into a table first (C5);
- an empty subquery that is *not* a set operation is fine (C6), as is an empty set operation over
  constants with no table input and hence no NULL (C7).

I did not read either engine's source, so this is a hypothesis consistent with all controls rather
than a confirmed root cause.

## Equivalence construction

Unusually, **the equivalence construction is not part of the bug** — the repro needs no rewrite at all.
What the oracle contributed was *exposure*: MySQL's variant is physical-order dependent, and the
`eq_my` rebuild ends with

```sql
CREATE TABLE t__base_table_2 AS
  SELECT id, name, created_at, ROW_NUMBER() OVER (ORDER BY id) AS eq_my_pk_1 FROM t__base_table_1;
```

`ORDER BY id` sorts NULL first in MySQL, so the equivalent's physical order puts the NULL row at the
front — the worst case — while the base keeps insert order with the NULL late. Base 7 vs equivalent 1.

The finding's own `WHERE` is already the reported shape:

```sql
WHERE t1.id NOT IN ((SELECT CASE WHEN CAST(NULL AS SIGNED) THEN t5.id ELSE t5.id END FROM t AS t5)
                    EXCEPT
                    (SELECT CAST(t6.id AS SIGNED) FROM t AS t6))
```

**Mapping onto the repro:** the `CASE WHEN CAST(NULL AS SIGNED) THEN t5.id ELSE t5.id END` folds to
`t5.id` and the `CAST(t6.id AS SIGNED)` to `t6.id`, giving `(SELECT id FROM b) EXCEPT (SELECT id FROM
b)`. Reduced away: the whole 34-statement equivalence chain (a 3-way predicate split into three
`UNION ALL` groups over `PARTITION BY RANGE`, `MyISAM`, unique-index and `DESC`-index variants, then a
final `INTERSECT ALL` self-intersection), the `COUNT(LEAST(…))` aggregate, the four-expression
`GROUP BY`, and two of the three columns.

## Minimal oracle exposure path

- **Object composition arity:** **1**
- **GCL builder path:** `CreateTableBuilder[ROW_NUMBER-ordered CTAS]` (**inferred mapping**)
- **Confidence:** Inferred — the report records the ordering CTAS used for exposure, but not the historical GCL AST builder selection.
- **Realization:** A single CTAS materializes the same rows in NULL-first order; the remainder of the 34-statement chain is excluded from the minimal exposure path.
- **Workload/data requirements (excluded from arity):**
  - `NOT IN` over a parenthesized set operation whose result is empty.
  - A NULL in the set operation's input.
  - For MySQL's differential exposure, physical placement of that NULL early in the scan; MariaDB is wrong regardless of order.

**Exposure vs. intrinsic trigger:** The inferred CTAS path exposed MySQL's order-sensitive severity by moving NULL to the front. The intrinsic trigger is the `NOT IN` predicate over an empty set-operation result whose input contains NULL; it reproduces on a plain table, and MariaDB does not require reordering at all.

## Characterization

All measured; see [`reduced.sql`](reduced.sql) (18 queries, verified on MySQL, MariaDB and TiDB).

| control | MySQL | MariaDB | TiDB |
|---|---|---|---|
| **minimal repro** | **1** ✗ | **1** ✗ | 2 ✓ |
| C1 no NULL anywhere | 2 ✓ | 2 ✓ | 2 ✓ |
| C2 NULL in the outer table only; subquery scans a NULL-free table | 2 ✓ | 2 ✓ | 2 ✓ |
| C3 subquery scans a *different* table that contains NULL | **1** ✗ | **1** ✗ | 2 ✓ |
| C4 the set operation wrapped in a derived table | 2 ✓ | 2 ✓ | 2 ✓ |
| C5 the set operation materialised into a table first | 2 ✓ | 2 ✓ | n/a (no CTAS) |
| C6 a plainly-empty subquery instead of a set operation | 2 ✓ | 2 ✓ | 2 ✓ |
| C7 an empty set operation over constants (`(SELECT 99) EXCEPT (SELECT 99)`) | 2 ✓ | 2 ✓ | 2 ✓ |
| C8 `NOT EXISTS` instead of `NOT IN` | 2 ✓ | 2 ✓ | 2 ✓ |
| C9 `IN` instead of `NOT IN` | 0 ✓ | 0 ✓ | 0 ✓ |
| C10 `EXCEPT ALL` instead of `EXCEPT` | **1** ✗ | **1** ✗ | n/a (unsupported) |

So the necessary ingredients are: `NOT IN` (not `IN`, not `NOT EXISTS`) over a **parenthesized set
operation** (not a plain subquery, not a materialised table, not a derived-table wrapper) whose
**result is empty** and whose **input contains NULL**.

No `EXPLAIN` diff is offered: the failure is in predicate evaluation, and the plans for the correct and
incorrect variants differ structurally (derived table vs inline set operation), so a diff would not
isolate anything. The four-engine comparison is the stronger evidence here.

## How it was found

eqgen v3 data-equivalence oracle, `mysql_run6` round 108, seed 1031118865. Base `t` (a plain table)
and equivalent `t` (a 34-statement chain) hold the same 8 rows; the same workload query returned 7 rows
against the base and 1 against the equivalent.

This is an interesting case for the oracle because the bug is **not** relation-dependent — it
reproduces on a plain table, so a single-query fuzzer with a correctness oracle could have found it.
What the equivalence rewrite supplied was the *physical row order*: MySQL's variant only misbehaves
when the NULL is scanned early, and the rebuild's `ROW_NUMBER() OVER (ORDER BY id)` puts it first. And
because the oracle compares two sides rather than checking against a reference, it flagged the bug even
though **neither** side was correct (7 and 1, against a true answer of 8) — a comparison-only oracle
does not need to know the right answer.

Cross-engine contrast was what settled the semantics. Both MySQL and MariaDB agreeing initially looked
like evidence that I was misreading `NOT IN` over an empty set; TiDB and DuckDB agreeing on the other
answer resolved it, and TiDB in particular — MySQL-compatible by design, independently implemented —
is the reason this can be called a bug rather than a MySQL dialect quirk.

- Repro and controls: [`reduced.sql`](reduced.sql)
- Original finding: hunt log
