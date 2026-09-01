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

-- TiDB: a view whose body uses ANY_VALUE() + a scalar expression over that column in the left
-- operand of `IN (subquery)` = planner error 1105 "Can't find column Column#N in schema".
--
-- Build      : tidb 8.0.11-TiDB-v9.0.0-beta.2.pre-2051-g3bea8196a5 @3bea8196
--              (assertions off, unistore)
-- Session    : sql_mode=STRICT_ALL_TABLES,NO_ZERO_DATE,NO_ZERO_IN_DATE,
--              ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION,ONLY_FULL_GROUP_BY,
--              NO_BACKSLASH_ESCAPES; charset utf8mb4; collation utf8mb4_0900_bin.
--              NOTHING is load-bearing -- this is a planning failure.
-- Determinism: fully deterministic, DATA-INDEPENDENT (reproduces on an EMPTY table) and it
--              fails at bare EXPLAIN. No optimizer hint, no join, no rows.
-- Origin     : 226 of the 300 error findings in logs/tidb_run19 across 107 rounds. Sample:
--              logs/tidb_run19/error_round1001_0.sql (seed 178284896).
--
-- TRIGGER (all three required):
--   1. the relation is a VIEW whose body applies ANY_VALUE() to the column. MAX()/MIN() over the
--      same shape is CLEAN; GROUP BY without an aggregate is CLEAN; a plain view is CLEAN; and the
--      SAME BODY as an inline derived table (no view) is CLEAN.
--   2. the column referenced is the ANY_VALUE()-produced one, wrapped in ANY scalar expression --
--      UPPER / CONCAT / CAST / NULLIF / IFNULL / COALESCE / CASE / IF all trigger it. A BARE column
--      reference is CLEAN, so this is about the expression, not the function.
--   3. that expression is the left operand of `IN (subquery)` / `NOT IN (subquery)` /
--      `<> ALL (subquery)`. `= ANY (subquery)` -- semantically identical to IN -- is CLEAN, and so
--      is `IN (<literal list>)` and any comparison without a subquery.
--
-- HOW TO RUN: each PART is independent; run each in a fresh database.


-- =====================================================================================
-- PART 1 -- ABSOLUTE MINIMUM: 3 statements, one column, NO ROWS, fails at EXPLAIN.
-- =====================================================================================
CREATE TABLE k (c VARCHAR(9), g BIGINT);
CREATE VIEW t AS SELECT ANY_VALUE(c) AS c FROM k GROUP BY g;

SELECT 1 FROM t AS t1 WHERE UPPER(t1.c) IN (SELECT 'x');
--   Expected: 0 rows (the table is empty).
--   Actual:   ERROR 1105 (HY000): Can't find column Column#N in schema Column: [...]
--   NOTE: the `Column#N` and the bracketed schema list both vary with the plan context -- e.g.
--   `Column#5 in schema Column: [Column#15]` here. Only the error class is stable; do not
--   pattern-match the bracket contents.
EXPLAIN SELECT 1 FROM t AS t1 WHERE UPPER(t1.c) IN (SELECT 'x');
--   Same error -- it never gets a plan, so nothing about execution is involved.

-- Even the GROUP BY is unnecessary:
CREATE VIEW t2 AS SELECT ANY_VALUE(c) AS c FROM k;
SELECT 1 FROM t2 AS t1 WHERE UPPER(t1.c) IN (SELECT 'x');
--   Expected 0 rows; actual 1105 (here: Column#5 in schema Column: [<db>.k._tidb_rowid])


-- =====================================================================================
-- PART 2 -- CONCRETE: the finding as eqgen produced it (round 1001). The chain is five
--   links -- ROW_NUMBER keying, a UNION ALL x100 duplication, the ANY_VALUE reduce view, a
--   SELECT * wrapper, and an add-then-drop temp column -- but ONLY the ANY_VALUE view matters.
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
CREATE TABLE t__base_table_1 (`id` BIGINT, `name` VARCHAR(255), `created_at` VARCHAR(255), `eq_key_1` BIGINT);
INSERT INTO t__base_table_1 (`id`, `name`, `created_at`, `eq_key_1`) SELECT id, name, created_at, ROW_NUMBER() OVER (ORDER BY id) AS eq_key_1 FROM t__base;
CREATE TABLE t__base_table_2 (`id` BIGINT, `name` VARCHAR(255), `created_at` VARCHAR(255), `eq_key_1` BIGINT);
INSERT INTO t__base_table_2 (id, name, created_at, eq_key_1) SELECT id, name, created_at, eq_key_1 FROM t__base_table_1 UNION ALL SELECT id, name, created_at, eq_key_1 FROM t__base_table_1 CROSS JOIN (WITH RECURSIVE eq_gen_series (eq_gen_n) AS (SELECT 1 UNION ALL SELECT eq_gen_n + 1 FROM eq_gen_series WHERE eq_gen_n < 100) SELECT eq_gen_n FROM eq_gen_series) AS eq_gen;
CREATE VIEW t__base_view_1 AS SELECT ANY_VALUE(id) AS id, ANY_VALUE(name) AS name, ANY_VALUE(created_at) AS created_at FROM t__base_table_2 GROUP BY eq_key_1;
CREATE VIEW t__base_view_2 AS SELECT * FROM t__base_view_1;
CREATE VIEW t__base_view_3 AS SELECT id, name, created_at, CAST(NULL AS SIGNED) AS eq_tmp_col_2 FROM t__base_view_2;
CREATE VIEW t AS SELECT id, name, created_at FROM t__base_view_3;

-- `t` is row-identical to `t__base` -- but you cannot even ask, because:
SELECT COUNT(*) AS n FROM t;   -- Expected 8, actual 8  (a bare projection is fine)

-- the workload query (SELECT list trimmed; the full text is in the finding):
SELECT DISTINCT t1.id FROM t AS t1 WHERE CAST(NULLIF(t1.created_at, t1.created_at) AS CHAR(255)) NOT IN (SELECT t3.name AS expr_0_varchar FROM t AS t3 WHERE 0 GROUP BY t3.id, t3.name);
--   Expected 7 rows (what the base table returns), actual:
--   ERROR 1105: Can't find column Column#9 in schema Column: [<db>.t__base_table_2.…]
--   The schema named in the message belongs to t__base_table_2 -- the table UNDER the ANY_VALUE
--   view -- which is the diagnostic point. Its exact column list varies with the SELECT list, and
--   the finding as recorded shows all three columns; this trimmed query shows one.


-- =====================================================================================
-- PART 3 -- WHICH RELATION SHAPE. Query fixed; only the view body changes. All hold the
--   same rows.
-- =====================================================================================
CREATE TABLE k (c VARCHAR(9), g BIGINT);
INSERT INTO k VALUES ('a', 1);
INSERT INTO k VALUES ('b', 2);

CREATE VIEW r_anyvalue AS SELECT ANY_VALUE(c) AS c FROM k GROUP BY g;   -- TRIGGERS
CREATE VIEW r_max      AS SELECT MAX(c)       AS c FROM k GROUP BY g;   -- clean
CREATE VIEW r_min      AS SELECT MIN(c)       AS c FROM k GROUP BY g;   -- clean
CREATE VIEW r_groupby  AS SELECT c                 FROM k GROUP BY c;   -- clean
CREATE VIEW r_plain    AS SELECT c                 FROM k;              -- clean

SELECT 1 FROM r_anyvalue AS t1 WHERE UPPER(t1.c) IN (SELECT 'x');   -- Expected 0 rows, actual 1105
SELECT 1 FROM r_max      AS t1 WHERE UPPER(t1.c) IN (SELECT 'x');   -- Expected 0 rows, actual 0 rows
SELECT 1 FROM r_min      AS t1 WHERE UPPER(t1.c) IN (SELECT 'x');   -- Expected 0 rows, actual 0 rows
SELECT 1 FROM r_groupby  AS t1 WHERE UPPER(t1.c) IN (SELECT 'x');   -- Expected 0 rows, actual 0 rows
SELECT 1 FROM r_plain    AS t1 WHERE UPPER(t1.c) IN (SELECT 'x');   -- Expected 0 rows, actual 0 rows
-- The SAME BODY inline, as a derived table rather than a view -> CLEAN. It is view-specific.
SELECT 1 FROM (SELECT ANY_VALUE(c) AS c FROM k GROUP BY g) AS t1 WHERE UPPER(t1.c) IN (SELECT 'x');
--   Expected 0 rows, actual 0 rows
-- The expression must be over the ANY_VALUE column, not a passthrough column:
CREATE VIEW r_mixed AS SELECT ANY_VALUE(c) AS c, g FROM k GROUP BY g;
SELECT 1 FROM r_mixed AS t1 WHERE UPPER(t1.c) IN (SELECT 'x');   -- Expected 0, actual 1105  (agg col)
SELECT 1 FROM r_mixed AS t1 WHERE CAST(t1.g AS CHAR(9)) IN (SELECT 'x');  -- Expected 0, actual 0 (plain col)


-- =====================================================================================
-- PART 4 -- WHICH EXPRESSION, and WHICH PREDICATE. Relation fixed (the ANY_VALUE view).
--   It is NOT specific to NULLIF or to the CASE family: ANY scalar wrapper triggers it, and a
--   BARE column does not.
-- =====================================================================================
CREATE TABLE k (c VARCHAR(9), g BIGINT);
INSERT INTO k VALUES ('a', 1);
CREATE VIEW t AS SELECT ANY_VALUE(c) AS c FROM k GROUP BY g;

-- expression in the IN's left operand -- all of these ERROR:
SELECT 1 FROM t AS t1 WHERE UPPER(t1.c)                      IN (SELECT 'x');   -- 1105
SELECT 1 FROM t AS t1 WHERE CONCAT(t1.c, '')                 IN (SELECT 'x');   -- 1105
SELECT 1 FROM t AS t1 WHERE NULLIF(t1.c, t1.c)               IN (SELECT 'x');   -- 1105
SELECT 1 FROM t AS t1 WHERE IFNULL(t1.c, 'z')                IN (SELECT 'x');   -- 1105
SELECT 1 FROM t AS t1 WHERE COALESCE(t1.c, 'z')              IN (SELECT 'x');   -- 1105
SELECT 1 FROM t AS t1 WHERE CASE WHEN t1.c='a' THEN t1.c END IN (SELECT 'x');   -- 1105
SELECT 1 FROM t AS t1 WHERE IF(t1.c=t1.c, NULL, t1.c)        IN (SELECT 'x');   -- 1105
-- a BARE column is CLEAN -- so it is the expression, not any particular function:
SELECT 1 FROM t AS t1 WHERE t1.c                             IN (SELECT 'x');   -- Expected 0, actual 0

-- predicate form, same expression:
SELECT 1 FROM t AS t1 WHERE UPPER(t1.c) IN     (SELECT 'x');   -- 1105
SELECT 1 FROM t AS t1 WHERE UPPER(t1.c) NOT IN (SELECT 'x');   -- 1105
SELECT 1 FROM t AS t1 WHERE UPPER(t1.c) <> ALL (SELECT 'x');   -- 1105
SELECT 1 FROM t AS t1 WHERE UPPER(t1.c) =  ANY (SELECT 'x');   -- Expected 0, actual 0  <-- CLEAN,
--   although `x = ANY (subquery)` is by definition the same predicate as `x IN (subquery)`.
SELECT 1 FROM t AS t1 WHERE UPPER(t1.c) IN ('x', 'y');         -- Expected 0, actual 0  (literal list)
SELECT 1 FROM t AS t1 WHERE UPPER(t1.c) = 'x';                 -- Expected 0, actual 0  (no subquery)
SELECT UPPER(t1.c) FROM t AS t1;                               -- Expected 1 row, actual 1 row
--   (the expression alone is fine; it needs the IN-subquery context)
