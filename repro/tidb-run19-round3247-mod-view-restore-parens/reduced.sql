-- Copyright 2026 Snowflake Inc.
-- SPDX-License-Identifier: Apache-2.0
--
-- Licensed under the Apache License, Version 2.0 (the "License");
-- you may not use this file except in compliance with the License.
-- You may obtain a copy of the License at
--
-- http://www.apache.org/licenses/LICENSE-2.0
--
-- Unless required by applicable law or agreed to in writing, software
-- distributed under the License is distributed on an "AS IS" BASIS,
-- WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
-- See the License for the specific language governing permissions and
-- limitations under the License.

-- TiDB: `MOD(a, b)` in a stored view / generated column is re-serialized as the infix `%`
-- operator WITHOUT parentheses, so the persisted definition re-parses with different
-- operator precedence -- silent wrong results.
--
-- STATUS     : DUPLICATE of open upstream issue pingcap/tidb#63289 (type/bug,
--              severity/major, impact/wrong-result, filed 2025-08-31, still OPEN).
--              Two fix attempts, PR #66865 and PR #66901, are both CLOSED UNMERGED.
--              Do NOT file a new issue -- comment on #63289. See bug_report.md for the
--              new information this finding adds (generated columns, incl. STORED).
-- Build      : tidb 8.0.11-TiDB-v9.0.0-beta.2.pre-2051-g3bea8196a5 @3bea8196
--              (assertions off, unistore). Source tree at the same commit confirms
--              pkg/ddl/create_table.go:1771 still lacks RestoreBracketAroundBinaryOperation.
-- Session    : sql_mode=STRICT_ALL_TABLES,NO_ZERO_DATE,NO_ZERO_IN_DATE,
--              ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION,ONLY_FULL_GROUP_BY,
--              NO_BACKSLASH_ESCAPES; charset utf8mb4; collation utf8mb4_0900_bin.
--              NOTHING here is load-bearing -- the bug is in DDL text serialization.
-- Determinism: fully deterministic. No optimizer, no plan shape, no join, no subquery.
-- Origin     : hunt log (round 3247, seed 197630928)
--              and 23 sibling mismatch_round3247_*.sql files -- all one root cause.
--
-- TRIGGER: write `MOD(x, y)` (function syntax) inside any DDL whose text TiDB persists and
--   re-parses, where x or y is an expression whose top operator binds LOOSER than `%`.
--   The parser desugars the function form into a binary `%` node at parse time
--   (pkg/parser/parser.y:8857-8859), discarding the grouping the call parentheses gave;
--   the restore side can only PRESERVE a ParenthesesExpr node, never introduce one.
--
--   MOD(id - 2, 2)   is stored as   `id`-2%2   ==  id - (2%2)  ==  id - 0
--   MOD(id, 2 + 3)   is stored as   `id`%2+3   ==  (id%2) + 3
--
-- HOW TO RUN: each PART is independent; run each in a fresh database. Nothing needs a
--   specific sql_mode. `SHOW CREATE VIEW` / `SHOW CREATE TABLE` show the corrupted text.


-- =====================================================================================
-- PART 1 -- CONCRETE: the finding as eqgen produced it. The equivalence builder splits
--   the base rows three ways on a predicate P (P true / NOT P / P IS NULL) and UNION ALLs
--   the halves back -- a partition that is total by construction, so the union must hold
--   each row exactly once. P contains MOD(IFNULL(id,id) - OCTET_LENGTH(name), 2), so in
--   the two VIEW halves it becomes `id`-OCTET_LENGTH(`name`)%2, and both P and NOT P
--   evaluate TRUE for (7,'é','é') -- the row is duplicated and `t` gains a 9th row.
--   The INSERT..SELECT half (t__base_table_1) is correct: it is executed, never stored.
-- =====================================================================================
CREATE TABLE t__base (`id` BIGINT, `name` VARCHAR(255), `created_at` VARCHAR(255));
INSERT INTO t__base VALUES (-3, 'a', 'a');
INSERT INTO t__base VALUES (-1, '', '');
INSERT INTO t__base VALUES (0, 'dup', 'dup');
INSERT INTO t__base VALUES (1, 'dup', 'dup');
INSERT INTO t__base VALUES (2, NULL, NULL);
INSERT INTO t__base VALUES (2, 'zzz', 'zzz');
INSERT INTO t__base VALUES (NULL, 'b', 'b');
INSERT INTO t__base VALUES (7, 'é', 'é');

-- half 1: P, as a table -- INSERT..SELECT is executed, not persisted -> CORRECT (7 rows)
CREATE TABLE t__base_table_1 (`id` BIGINT, `name` VARCHAR(255), `created_at` VARCHAR(255));
INSERT INTO t__base_table_1 (`id`, `name`, `created_at`) SELECT * FROM t__base WHERE IFNULL(CAST(MOD(IFNULL(id, id) - OCTET_LENGTH(name), 2) AS SIGNED) NOT IN (id), MONTHNAME(CAST(least(CAST(IFNULL('2016-05-04 10:10:10.100000', '2016-04-04 10:16:10.100000') AS DATE), CAST(GREATEST('2016-10-10', '2016-04-10') AS DATE), '2016-10-10', '2016-10-10', '2016-12-10') AS DATE)) LIKE '%a%');
-- half 2: NOT P, as a VIEW -- text is persisted and re-parsed -> WRONG (2 rows, not 1)
CREATE VIEW t__base_view_1 AS SELECT * FROM t__base WHERE (NOT IFNULL(CAST(MOD(IFNULL(id, id) - OCTET_LENGTH(name), 2) AS SIGNED) NOT IN (id), MONTHNAME(CAST(least(CAST(IFNULL('2016-05-04 10:10:10.100000', '2016-04-04 10:16:10.100000') AS DATE), CAST(GREATEST('2016-10-10', '2016-04-10') AS DATE), '2016-10-10', '2016-10-10', '2016-12-10') AS DATE)) LIKE '%a%'));
-- half 3: P IS NULL, as a VIEW -> 0 rows (correct here)
CREATE VIEW t__base_view_2 AS SELECT * FROM t__base WHERE IFNULL(CAST(MOD(IFNULL(id, id) - OCTET_LENGTH(name), 2) AS SIGNED) NOT IN (id), MONTHNAME(CAST(least(CAST(IFNULL('2016-05-04 10:10:10.100000', '2016-04-04 10:16:10.100000') AS DATE), CAST(GREATEST('2016-10-10', '2016-04-10') AS DATE), '2016-10-10', '2016-10-10', '2016-12-10') AS DATE)) LIKE '%a%') IS NULL;
CREATE VIEW t__base_view_3 AS SELECT * FROM t__base_table_1 UNION ALL SELECT * FROM t__base_view_1 UNION ALL SELECT * FROM t__base_view_2;

-- The three halves must sum to the 8 base rows. Expected 8, actual 9.
SELECT COUNT(*) AS n FROM t__base_view_3;                                -- Expected 8, actual 9
SELECT COUNT(*) AS n FROM t__base_table_1;                               -- Expected 7, actual 7  (correct)
SELECT COUNT(*) AS n FROM t__base_view_1;                                -- Expected 1, actual 2  (WRONG)
SELECT COUNT(*) AS n FROM t__base_view_2;                                -- Expected 0, actual 0  (correct)
-- (7,'é','é') is in BOTH halves, though NOT P evaluates to 0 for it:
SELECT id FROM t__base_view_1;                                           -- Expected (-1), actual (-1),(7)
SELECT id FROM t__base WHERE (NOT IFNULL(CAST(MOD(IFNULL(id, id) - OCTET_LENGTH(name), 2) AS SIGNED) NOT IN (id), MONTHNAME(CAST(least(CAST(IFNULL('2016-05-04 10:10:10.100000', '2016-04-04 10:16:10.100000') AS DATE), CAST(GREATEST('2016-10-10', '2016-04-10') AS DATE), '2016-10-10', '2016-10-10', '2016-12-10') AS DATE)) LIKE '%a%'));  -- same predicate INLINE: (-1) only -- correct
-- and the corrupted stored text -- `MOD(IFNULL(id,id) - OCTET_LENGTH(name), 2)` came back as
-- `IFNULL(id,id)-OCTET_LENGTH(name)%2`, i.e. IFNULL(id,id) - (OCTET_LENGTH(name) % 2):
SHOW CREATE VIEW t__base_view_1;
--   ... WHERE (NOT IFNULL(CAST(IFNULL(`id`, `id`)-OCTET_LENGTH(`name`)%2 AS SIGNED) NOT IN (`id`), ...))
--   For (7,'é','é'): correct  MOD(7 - 2, 2) = 1, 1 NOT IN (7) -> true  -> NOT P = 0 -> excluded.
--   As stored:              7 - (2 % 2) = 7,   7 NOT IN (7) -> false -> NOT P = 1 -> INCLUDED.


-- =====================================================================================
-- PART 2 -- DISTILLED minimal repro. One column, one row, no join, no subquery, no
--   IFNULL/CAST/NOT IN/OCTET_LENGTH -- just MOD with a `-` in its left operand.
-- =====================================================================================
CREATE TABLE t (id BIGINT);
INSERT INTO t VALUES (7);
CREATE VIEW v AS SELECT MOD(id - 2, 2) AS m FROM t;

SELECT MOD(id - 2, 2) AS m FROM t;   -- Expected 1, actual 1   (inline: correct)
SELECT m FROM v;                     -- Expected 1, actual 7   (through the view: WRONG)
SHOW CREATE VIEW v;                  -- ... AS SELECT `id`-2%2 AS `m` FROM `t`   <- `-` and `%` transposed

-- as a filter rather than a projection, which is the shape the finding takes:
CREATE VIEW vf AS SELECT * FROM t WHERE MOD(id - 2, 2) = id;
SELECT id FROM t WHERE MOD(id - 2, 2) = id;   -- Expected 0 rows, actual 0 rows  (correct)
SELECT id FROM vf;                            -- Expected 0 rows, actual (7)     (WRONG)
EXPLAIN SELECT id FROM vf;                    -- Selection: eq(minus(t.id, 0), t.id)   <- `id - 0 = id`
EXPLAIN SELECT id FROM t WHERE MOD(id - 2, 2) = id;  -- eq(mod(minus(t.id, 2), 2), t.id)  <- correct


-- =====================================================================================
-- PART 3 -- GENERATED COLUMNS. Same restore path, WORSE surface: the corrupted expression
--   is persisted in the table definition, and with STORED the wrong value is written to
--   disk. Neither #63289 nor either of its closed PRs mentions this; the PRs' fix (a
--   restore flag in ddl.BuildViewInfo) would not cover it.
-- =====================================================================================
CREATE TABLE g (id BIGINT, m BIGINT AS (MOD(id - 2, 2)) VIRTUAL);
INSERT INTO g (id) VALUES (7);
SELECT m FROM g;             -- Expected 1, actual 7
SHOW CREATE TABLE g;         -- ... `m` bigint GENERATED ALWAYS AS (`id` - 2 % 2) VIRTUAL

CREATE TABLE g2 (id BIGINT, m BIGINT AS (MOD(id - 2, 2)) STORED);
INSERT INTO g2 (id) VALUES (7);
SELECT m FROM g2;            -- Expected 1, actual 7   -- wrong value now ON DISK


-- =====================================================================================
-- PART 4 -- WHICH OPERANDS BREAK. `%` binds like `*` and `/` (tighter than `+`/`-`) and
--   is left-associative, so an operand survives only if its own top operator binds at
--   least as tight AND the associativity happens to regroup correctly.
--   All rows below: table `t` holds a single row id = 7.
-- =====================================================================================
CREATE TABLE t (id BIGINT);
INSERT INTO t VALUES (7);

-- LEFT operand
CREATE VIEW l1 AS SELECT MOD(id - 2, 2) AS m FROM t;   -- stored `id`-2%2    correct 1, view 7   WRONG
CREATE VIEW l2 AS SELECT MOD(id + 1, 3) AS m FROM t;   -- stored `id`+1%3    correct 2, view 8   WRONG
CREATE VIEW l3 AS SELECT MOD(id * 2, 2) AS m FROM t;   -- stored `id`*2%2    correct 0, view 0   safe (same precedence, left-assoc regroups correctly)
CREATE VIEW l4 AS SELECT MOD(-id, 3) AS m FROM t;      -- stored -`id`%3     correct -1, view -1 safe (unary binds tighter)
SELECT (SELECT m FROM l1) AS l1, (SELECT m FROM l2) AS l2, (SELECT m FROM l3) AS l3, (SELECT m FROM l4) AS l4;
-- Expected (1, 2, 0, -1); actual (7, 8, 0, -1)

-- RIGHT operand
CREATE VIEW r1 AS SELECT MOD(id, 2 + 3) AS m FROM t;   -- stored `id`%2+3    correct 2, view 4   WRONG
CREATE VIEW r2 AS SELECT MOD(id, 6 - 1) AS m FROM t;   -- stored `id`%6-1    correct 2, view 0   WRONG
CREATE VIEW r3 AS SELECT MOD(id, 3 * 2) AS m FROM t;   -- stored `id`%3*2    correct 1, view 2   WRONG (left-assoc regroups the WRONG way on the right)
SELECT (SELECT m FROM r1) AS r1, (SELECT m FROM r2) AS r2, (SELECT m FROM r3) AS r3;
-- Expected (2, 2, 1); actual (4, 0, 2)

-- BOTH operands at once
CREATE VIEW b1 AS SELECT MOD(id - 1, 4 - 1) AS m FROM t;  -- stored `id`-1%4-1   correct 0, view 5   WRONG
SELECT m FROM b1;                                          -- Expected 0, actual 5


-- =====================================================================================
-- PART 5 -- CONTROLS. Each changes exactly ONE thing and is correct.
-- =====================================================================================
CREATE TABLE t (id BIGINT);
INSERT INTO t VALUES (7);

-- C1  write the operator form with explicit parentheses -> the restore PRESERVES them.
CREATE VIEW c1 AS SELECT (id - 2) % 2 AS m FROM t;
SELECT m FROM c1;            -- Expected 1, actual 1     stored: (`id`-2)%2
-- C2  parenthesise inside the function call -> also preserved.
CREATE VIEW c2 AS SELECT MOD((id - 2), 2) AS m FROM t;
SELECT m FROM c2;            -- Expected 1, actual 1     stored: (`id`-2)%2
-- C3  no looser-binding operand at all -> nothing to lose.
CREATE VIEW c3 AS SELECT MOD(id, 2) AS m FROM t;
SELECT m FROM c3;            -- Expected 1, actual 1     stored: `id`%2
-- C4  inline / derived table / CTE -- no DDL text is persisted, so all correct.
SELECT MOD(id - 2, 2) AS m FROM t;                                  -- 1
SELECT m FROM (SELECT MOD(id - 2, 2) AS m FROM t) d;                -- 1
WITH cte AS (SELECT MOD(id - 2, 2) AS m FROM t) SELECT m FROM cte;  -- 1
-- C5  a PARTITION expression restores CORRECTLY -- pkg/ddl/partition.go:619 already passes
--     format.RestoreBracketAroundBinaryOperation, which is the exact flag BuildViewInfo omits.
CREATE TABLE p (id BIGINT) PARTITION BY RANGE (MOD(id - 2, 2)) (PARTITION p0 VALUES LESS THAN (1), PARTITION p1 VALUES LESS THAN (MAXVALUE));
INSERT INTO p VALUES (7);
SHOW CREATE TABLE p;                    -- PARTITION BY RANGE (((`id`-2)%2))   <- parenthesised
SELECT id FROM p PARTITION (p0);        -- Expected 0 rows, actual 0 rows
SELECT id FROM p PARTITION (p1);        -- Expected (7), actual (7)   -- MOD(7-2,2)=1 -> p1, correct
