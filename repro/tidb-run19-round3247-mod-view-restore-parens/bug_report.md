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

# TiDB: `MOD(a, b)` in a stored view or generated column is re-serialized as infix `%` without parentheses, so the persisted definition re-parses with different operator precedence — silent wrong results

## Summary

TiDB's parser desugars the **function** form `MOD(a, b)` into a binary `%` node at parse time
(`pkg/parser/parser.y:8857-8859`), which discards the grouping the call parentheses provided. The
restore side can only *preserve* an existing `ParenthesesExpr` node, never *introduce* one, so any DDL
whose text TiDB persists and re-parses — a view definition, a generated-column expression — stores
`MOD(id - 2, 2)` as `` `id`-2%2 ``. Because `%` binds tighter than `-`, that re-parses as
`id - (2 % 2)` = `id - 0`. Queries against the object then return **silently wrong results**, with no
error and no plan-shape dependence.

**This is a duplicate of the open upstream issue
[pingcap/tidb#63289](https://github.com/pingcap/tidb/issues/63289)** (`type/bug`, `severity/major`,
`impact/wrong-result`, `fuzz/sqlancer`, filed 2025-08-31, **still open**). Two fix attempts,
[PR #66865](https://github.com/pingcap/tidb/pull/66865) and
[PR #66901](https://github.com/pingcap/tidb/pull/66901), are both **closed unmerged**; the source tree
at the build under test confirms `ddl.BuildViewInfo` still omits the flag they proposed adding. **Do
not file a new issue** — comment on #63289 with the new information below.

**What this finding adds to #63289.** (1) **Generated columns are affected too, including `STORED`** —
the corrupted expression is persisted in the table definition and the wrong value is written to disk;
neither the issue nor either PR mentions this, and the PRs' fix (a restore flag inside
`ddl.BuildViewInfo`) would not cover it. (2) The bug needs **no subquery at all** — `CREATE VIEW v AS
SELECT MOD(id-2,2) FROM t` on a one-row table is enough, where #63289's repro is a CTE plus a
correlated scalar subquery plus a join; its title ("VIEW containing correlated subquery returns NULL")
under-describes the bug by a wide margin, which plausibly contributes to it sitting open for eleven
months. (3) **Both operands** are affected, not just the right one in #63289's example.
(4) `MOD` is the **only** function form desugared into an operator node, so the bug class has exactly
one entry point.

## Environment

| | |
|---|---|
| Engine | tidb `8.0.11-TiDB-v9.0.0-beta.2.pre-2051-g3bea8196a5` @ `3bea8196`, unistore, assertions off |
| Source | `pingcap/tidb` @ `3bea8196a565ca01800b2d0807868f01139d8a30` (2026-07-30), read directly to confirm the code paths below |
| `sql_mode` | `STRICT_ALL_TABLES,NO_ZERO_DATE,NO_ZERO_IN_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION,ONLY_FULL_GROUP_BY,NO_BACKSLASH_ESCAPES` — **not load-bearing**, the bug is in DDL text serialization |
| Charset / collation | `utf8mb4` / `utf8mb4_0900_bin` — not load-bearing |
| Determinism | fully deterministic; no join, no subquery, no optimizer involvement, single row suffices |
| Store | `unistore`; the faulting code is in the SQL layer, so TiKV is not expected to matter (not verified — no TiKV cluster available) |

## Minimal repro

```sql
CREATE TABLE t (id BIGINT);
INSERT INTO t VALUES (7);
CREATE VIEW v AS SELECT MOD(id - 2, 2) AS m FROM t;

SELECT MOD(id - 2, 2) AS m FROM t;   -- 1  (correct)
SELECT m FROM v;                     -- 7  (WRONG)
SHOW CREATE VIEW v;                  -- ... AS SELECT `id`-2%2 AS `m` FROM `t`
```

And the same defect on a *persisted data* surface:

```sql
CREATE TABLE g2 (id BIGINT, m BIGINT AS (MOD(id - 2, 2)) STORED);
INSERT INTO g2 (id) VALUES (7);
SELECT m FROM g2;                    -- 7  (WRONG; correct is 1) -- and now on disk
SHOW CREATE TABLE g2;                -- ... `m` bigint GENERATED ALWAYS AS (`id` - 2 % 2) STORED
```

## Expected vs actual

All rows measured on the build above; table `t` holds the single row `id = 7`.

| Query | Expected | Actual |
|---|---|---|
| `SELECT MOD(id-2,2) FROM t` (inline) | `1` | `1` |
| `SELECT m FROM v` (same expression, via view) | `1` | **`7`** |
| `SELECT id FROM t WHERE MOD(id-2,2) = id` | 0 rows | 0 rows |
| `SELECT id FROM vf` (same predicate, via view) | 0 rows | **`(7)`** |
| `SELECT m FROM g` (generated column, `VIRTUAL`) | `1` | **`7`** |
| `SELECT m FROM g2` (generated column, `STORED`) | `1` | **`7`** |
| `MOD(id + 1, 3)` via view | `2` | **`8`** |
| `MOD(id, 2 + 3)` via view | `2` | **`4`** |
| `MOD(id, 6 - 1)` via view | `2` | **`0`** |
| `MOD(id, 3 * 2)` via view | `1` | **`2`** |
| `MOD(id - 1, 4 - 1)` via view | `0` | **`5`** |
| `MOD(id * 2, 2)` via view | `0` | `0` (safe — see below) |
| `MOD(-id, 3)` via view | `-1` | `-1` (safe) |
| `(id - 2) % 2` via view (operator form, explicit parens) | `1` | `1` |
| `MOD((id - 2), 2)` via view (parens inside the call) | `1` | `1` |
| derived table / CTE with `MOD(id-2,2)` | `1` | `1` |
| `PARTITION BY RANGE (MOD(id-2,2))` routing | row in `p1` | row in `p1` |
| the finding's 3-way predicate split, `COUNT(*)` over the union | `8` | **`9`** |

## Equivalence construction

### (1) The construct as the eqgen builder emits it

The equivalent `t` is a nine-link chain, but only its **first** link matters: the predicate-split
partitioning builder. It splits the base rows three ways on a generated predicate `P` — `P` true, `NOT
P`, `P IS NULL` — and `UNION ALL`s the halves back. That is a total partition by construction, so the
union must hold each base row exactly once. Verbatim from `logs/tidb_run19/mismatch_round3247_0.sql`,
with `P` abbreviated:

```sql
ALTER TABLE t RENAME TO t__base;
-- P = IFNULL(CAST(MOD(IFNULL(id, id) - OCTET_LENGTH(name), 2) AS SIGNED) NOT IN (id),
--            MONTHNAME(CAST(least(…) AS DATE)) LIKE '%a%')
CREATE TABLE t__base_table_1 (…);
INSERT INTO t__base_table_1 (…) SELECT * FROM t__base WHERE <P>;          -- executed, not stored
CREATE VIEW  t__base_view_1  AS SELECT * FROM t__base WHERE (NOT <P>);    -- text STORED
CREATE VIEW  t__base_view_2  AS SELECT * FROM t__base WHERE <P> IS NULL;  -- text STORED
CREATE VIEW  t__base_view_3  AS SELECT * FROM t__base_table_1
                             UNION ALL SELECT * FROM t__base_view_1
                             UNION ALL SELECT * FROM t__base_view_2;
-- … then window round-trip, duplicate-and-reduce, split-rejoin, uid/flag join, quantile-filter view
CREATE VIEW t AS SELECT id, name, created_at FROM (SELECT …, ROW_NUMBER() OVER (ORDER BY id) >= 1 AS _qf FROM t__base_table_9) AS _qw WHERE _qf;
```

and the workload query, which is as trivial as a query gets:

```sql
SELECT t1.id, CAST(t1.created_at AS CHAR(255)), CAST(t1.created_at AS CHAR(255)),
       '2016-05-04 10:10:10.100000', t1.created_at, t1.name, t1.name IN ('YEAR','MONTH')
FROM t AS t1;
```

The `INSERT … SELECT WHERE P` half is **correct** (7 rows) because that statement is executed and
never round-tripped through text. The `WHERE NOT P` half is **wrong** (2 rows instead of 1) because a
view's text is stored and re-parsed. `(7,'é','é')` therefore satisfies both halves and `t` ends up
with nine rows instead of eight. Counting through the chain pins the defect to the first link:

```
t__base 8 -> table_1 7 + view_1 2 + view_2 0 = view_3 9 -> … -> t 9
```

**Mapping onto the distilled repro:** `MOD(IFNULL(id,id) - OCTET_LENGTH(name), 2)` → `MOD(id - 2, 2)`
(`OCTET_LENGTH('é')` *is* 2, so this is the same arithmetic); the `IFNULL`/`CAST`/`NOT IN`/`MONTHNAME
… LIKE` wrapper and the `NOT` all reduce away; `WHERE`-position and `SELECT`-position both reproduce.
The eight rows reduce to one and the three columns to one.

### (2) The load-bearing construct — a single construct, no composition

**`CREATE VIEW` (or a generated column) × a `MOD(…)` call with a looser-binding operand.** That is the
whole trigger. Notably this is *not* a construct × query-feature composition: the query is not
involved at all — `SELECT * FROM v` is already wrong, and the object is wrong the moment it is
created. It is also the rare case where the defect is in **DDL text serialization** rather than in
planning or execution.

## Minimal oracle exposure path

- **Object composition arity:** `3`.
- **GCL builder path:** `TlpPartitionUnionQueryBuilder → CreateTableBuilder → CreateViewBuilder`.
- **Confidence:** Verified at the emitted-SQL and current-class level. The table and view
  realizations are sibling TLP branches rather than a strictly linear chain, so the arrows summarize
  the minimal composition of selected builder factors.
- **Realization:** one predicate branch is executed into a table while another is persisted as a
  view, and the branches are exposed through a union view.
- **Workload/data requirements (excluded from arity):** the `MOD` expression, its looser-binding
  operand, and the row value are expression/data requirements and are not counted; the workload query
  is not a trigger.
- **Exposure vs. intrinsic trigger:** arity 3 describes how the oracle exposed the disagreement
  between an executed predicate and its stored form. The intrinsic engine trigger is only one
  persisted view or generated column containing the affected `MOD`; TLP, the table branch, and union
  reassembly are not intrinsically required.

### (3) Constructs reduced away

Eight of the nine chain links: the `UNION ALL` re-assembly, the `ROW_NUMBER()` window round-trip, the
duplicate-100×-and-reduce-by-`MAX() OVER (PARTITION BY key)` pair (twice), the split-and-rejoin
column halves, the uid/flag left-join filter, and the final quantile-filter view. Also the entire
predicate wrapper (`IFNULL`, `CAST … AS SIGNED`, `NOT IN`, `MONTHNAME`/`least`/`GREATEST`/`LIKE`), the
`NOT`, seven of the eight rows, and two of the three columns. What remains is one table, one row, one
column, one view, one `MOD`. This is the intrinsic SQL reduction; the arity above separately records
the minimal GCL/oracle exposure composition.

## Characterization

### Mechanism, at source level

Two halves, both confirmed by reading the tree at `3bea8196`:

**1. The parser throws the grouping away.** `pkg/parser/parser.y:8857-8859`:

```
|	"MOD" '(' Expression ',' Expression ')'
	{
		$$ = &ast.BinaryOperationExpr{Op: opcode.Mod, L: $3, R: $5}
	}
```

The function call becomes a bare `BinaryOperationExpr` with **no `ParenthesesExpr` around either
operand**, and the operands are the unconstrained `Expression` nonterminal — so `id - 2` is admitted
where the operator form's `BitExpr` operands could never be. This is the *only* rule in `parser.y`
that desugars a function-call form into an operator node; every other `BinaryOperationExpr{…}`
construction is reached from genuine operator syntax whose operands are already precedence-constrained.

**2. The restore side can only keep parentheses, never add them.** `BinaryOperationExpr.Restore`
(`pkg/parser/ast/expressions.go:210-236`) writes `L op R` flat, adding brackets only when the caller
passed `format.RestoreBracketAroundBinaryOperation`. There *is* precedence machinery —
`canRestoreWithoutParentheses` / `canRestoreBinaryChildWithoutParentheses`
(`expressions.go:1111-1129`) computes exactly "may this child drop its parentheses under this parent
operator on this side" — but its only caller is `ParenthesesExpr.Restore` (`expressions.go:1056`),
i.e. it decides whether an **existing** paren node is redundant. With no paren node in the AST there
is nothing to consult.

**3. So it comes down to the restore flags at each call site.** `ddl.BuildViewInfo`
(`pkg/ddl/create_table.go:1769-1773`):

```go
restoreFlag := format.RestoreStringSingleQuotes | format.RestoreKeyWordUppercase | format.RestoreNameBackQuotes
if err := s.Select.Restore(format.NewRestoreCtx(restoreFlag, &sb)); err != nil {
```

No `RestoreBracketAroundBinaryOperation`. Partition-expression restore, by contrast, does pass it
(`pkg/ddl/partition.go:619`) — which is why `PARTITION BY RANGE (MOD(id-2,2))` correctly stores
`(((`id`-2)%2))` and routes the row to the right partition. The flag works; two call sites just don't
use it. That is precisely the change both closed PRs proposed, and it is still absent.

### Plan diff — the corruption is visible in the plan

`EXPLAIN` of the view versus the identical inline predicate. The view's `Selection` has lost the
`mod` call outright:

```
-- EXPLAIN SELECT id FROM vf            (vf = CREATE VIEW … WHERE MOD(id - 2, 2) = id)
TableReader_9        root       data:Selection_8
└─Selection_8        cop[tikv]  eq(minus(t.id, 0), t.id)                 <-- id - 0 = id : true for every non-NULL row
  └─TableFullScan_7  cop[tikv]  table:t

-- EXPLAIN SELECT id FROM t WHERE MOD(id - 2, 2) = id
TableReader_7        root       data:Selection_6
└─Selection_6        cop[tikv]  eq(mod(minus(t.id, 2), 2), t.id)         <-- correct
  └─TableFullScan_5  cop[tikv]  table:t
```

`2 % 2` was constant-folded to `0`, leaving the tautology `id - 0 = id`. On the finding's 8-row table
that admits every row with a non-NULL `id` — which is exactly the 7-rows-instead-of-2 shape observed
in the sibling reduction.

### Which operands break, and which survive

`%` has the precedence of `*` and `/` (tighter than `+`/`-`) and is left-associative, so an operand
survives only if its own top operator binds at least as tightly **and** left-associative regrouping
happens to land on the same tree:

| operand | stored as | correct | view | |
|---|---|---|---|---|
| `MOD(id - 2, 2)` | `` `id`-2%2 `` | 1 | **7** | left `-` → wrong |
| `MOD(id + 1, 3)` | `` `id`+1%3 `` | 2 | **8** | left `+` → wrong |
| `MOD(id * 2, 2)` | `` `id`*2%2 `` | 0 | 0 | left `*` → safe, `(id*2)%2` by left-assoc |
| `MOD(-id, 3)` | `` -`id`%3 `` | −1 | −1 | unary minus binds tighter → safe |
| `MOD(id, 2 + 3)` | `` `id`%2+3 `` | 2 | **4** | right `+` → wrong |
| `MOD(id, 6 - 1)` | `` `id`%6-1 `` | 2 | **0** | right `-` → wrong |
| `MOD(id, 3 * 2)` | `` `id`%3*2 `` | 1 | **2** | right `*` → **wrong**, left-assoc regroups the wrong way |
| `MOD(id - 1, 4 - 1)` | `` `id`-1%4-1 `` | 0 | **5** | both operands |
| `MOD(MOD(id,5) - 1, 3)` | `` `id`%5-1%3 `` | 1 | 1 | wrong expression, right answer **by coincidence** |

The right-operand `*` case is worth flagging for the triager: it shows the bug is not simply "`%` is
tighter than `+`/`-`" — same-precedence operands break too, on the right, purely from associativity.

### Controls — one per ingredient, each correct

| Control | Result |
|---|---|
| operator form with explicit parens, `(id - 2) % 2` | correct; stored `` (`id`-2)%2 `` — parens preserved |
| parens inside the call, `MOD((id - 2), 2)` | correct; stored `` (`id`-2)%2 `` |
| no looser-binding operand, `MOD(id, 2)` | correct |
| same expression inline (no DDL) | correct |
| same expression in a derived table | correct |
| same expression in a CTE | correct |
| same expression in `INSERT … SELECT` (the finding's other half) | correct |
| same expression in `PARTITION BY RANGE (…)` | correct — that path passes the bracket flag |
| all five `sql_mode` / charset / collation settings from the finding | irrelevant; reproduces without any of them |

The `MOD((id-2), 2)` control is the sharpest one: adding a redundant-looking pair of parentheses
*inside* the function call changes the stored text and fixes the result, which is only explicable if
grouping is being carried by AST paren nodes rather than by precedence logic.

## How it was found

eqgen v3 data-equivalence oracle, `tidb_run19` round 3247, seed 197630928. The oracle holds the query
fixed and swaps in a relation that is supposed to be row-identical, so the probe here was about as
weak as a query can be: `SELECT <columns> FROM t`, no `WHERE`, no join, no aggregate. All **24**
mismatches recorded in round 3247 are this one root cause — one chain, one corrupted view, 24 workload
queries that each noticed the extra row.

The instructive part is *where* the oracle caught it. This finding does not fail at the query
comparison so much as at the **admissibility check** — base `t` and equivalent `t` are not
row-identical (equivalent `t` has a ninth row). The triage rule "not row-identical → equivalence
builder bug, stop, do not file" would have discarded a `severity/major` wrong-result engine bug,
because here the builder's SQL is impeccable and the *engine* miscompiles it while creating the
object. The generalisable check is the one used above: run the builder's own predicate inline and as a
projection, and compare against the stored object. When the object disagrees with the predicate that
defines it, the engine is at fault, not the generator.

A query-rewrite oracle (TLP / NoREC / EET) is structurally blind to this. It holds the data fixed and
rewrites the query, so it never issues the `CREATE VIEW` that is the entire bug; and its rewrites are
built from inline predicates and derived tables, all of which take the correct path. What surfaced the
bug was the generator's *data*-side move of materialising a predicate split as stored views — and
then a trivial `SELECT`. #63289 was found by SQLancer, which does generate views, and it needed a CTE
plus a correlated scalar subquery to see it; the data-equivalence framing gets there with three
statements.

- Reduced repro and controls: [`reduced.sql`](reduced.sql)
- Original finding: hunt log (+ 23 siblings, same round)
