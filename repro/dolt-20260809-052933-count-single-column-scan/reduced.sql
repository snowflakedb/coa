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

-- Dolt: COUNT(<column>) returns the count of the WRONG column when the scan is pruned to one column
--
-- Engine      : dolt 2.2.3  (server reports VERSION() = 8.0.31, its MySQL compatibility string)
-- Access path : `dolt sql-server`. The in-process `dolt sql` CLI is CORRECT -- see block `cli-note`.
-- Clients     : reproduced through pymysql AND the mariadb CLI against the same server+database,
--               so it is a server-side wrong result, not a driver decoding artefact.
-- Session     : all defaults; sql_mode / collation are not load-bearing.
-- Finding     : dolt_20260809-052933/mismatch_round18_0.sql
--
-- Every block is delimited by `-- >>> BLOCK: <name> expect=<wrong|ok> value=<v> correct=<c>` and runs
-- in its own fresh database. Each block was run and its returned scalar checked against `value=`, so
-- nothing here is asserted from reasoning alone.
--
-- THE RULE: when the plan is `GroupBy` directly over a `Table` scan whose projected column list holds
-- exactly ONE column, COUNT(col) counts the wrong thing: for column index i>0 it returns the count of
-- the column PRECEDING it in the table's declared order, and for index 0 (including a one-column
-- table) it degenerates to COUNT(*), i.e. it counts rows and ignores the column's NULLs entirely.
-- Any node between GroupBy and Table (a Filter from a real WHERE, a Sort from ORDER BY), a GROUP BY,
-- a derived table, or a second column in the scan all avoid it.


-- >>> BLOCK: distilled-second-column  expect=wrong  value=3  correct=2
-- q holds 2 non-NULL values, so COUNT(q) must be 2. Dolt returns 3, which is COUNT(p) -- the
-- preceding column. (p=3, q=2, r=1 non-NULL; COUNT(*)=4.)
CREATE TABLE u (p BIGINT, q BIGINT, r BIGINT);
INSERT INTO u VALUES (1,NULL,NULL),(2,20,NULL),(3,30,300),(NULL,NULL,NULL);
SELECT COUNT(q) FROM u;


-- >>> BLOCK: distilled-third-column  expect=wrong  value=2  correct=1
-- Same table: COUNT(r) must be 1, Dolt returns 2 = COUNT(q), again the preceding column.
CREATE TABLE u (p BIGINT, q BIGINT, r BIGINT);
INSERT INTO u VALUES (1,NULL,NULL),(2,20,NULL),(3,30,300),(NULL,NULL,NULL);
SELECT COUNT(r) FROM u;


-- >>> BLOCK: distilled-first-column  expect=wrong  value=4  correct=3
-- The first column has no predecessor, so it degenerates to COUNT(*): 4 instead of 3.
-- This is why the bug hides on tables whose first column is NOT NULL -- there COUNT(*) is the
-- right answer by accident.
CREATE TABLE u (p BIGINT, q BIGINT, r BIGINT);
INSERT INTO u VALUES (1,NULL,NULL),(2,20,NULL),(3,30,300),(NULL,NULL,NULL);
SELECT COUNT(p) FROM u;


-- >>> BLOCK: self-inconsistent  expect=wrong  value=3  correct=2
-- The same engine, same session, disagrees with itself: COUNT(*) minus the NULLs says 2.
CREATE TABLE u (p BIGINT, q BIGINT, r BIGINT);
INSERT INTO u VALUES (1,NULL,NULL),(2,20,NULL),(3,30,300),(NULL,NULL,NULL);
-- COUNT(*) = 4 and SUM(q IS NULL) = 2, so COUNT(q) must be 4 - 2 = 2. It returns 3.
SELECT COUNT(q) FROM u;


-- >>> BLOCK: concrete-as-emitted  expect=wrong  value=6  correct=7
-- The finding as eqgen emitted it (mismatch_round18_0.sql), base side: the 9-column base table with
-- its fork copy, and the workload query verbatim. `NOT false` folds to TRUE, so no Filter node is
-- left above the scan and the pruned scan projects only [c_chr]. c_chr has 7 non-NULL values of 8;
-- Dolt returns 6, which is COUNT(c_txt) -- the column declared immediately before c_chr.
-- (The fork DDL is re-added by hand: eqgen's repro writer omits it from the base block.)
CREATE TABLE t (c_pk BIGINT NOT NULL, c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10, 2), c_dbl DOUBLE, c_txt VARCHAR(255), c_chr VARCHAR(255), c_date DATE, c_ts DATETIME(6));
INSERT INTO t VALUES (1,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL);
INSERT INTO t VALUES (2,2,1,0.0,0.0,'','Zed','2030-06-01',NULL);
INSERT INTO t VALUES (3,0,NULL,NULL,NULL,'a','o''brien',NULL,NULL);
INSERT INTO t VALUES (4,NULL,2,999.99,1000.125,'trailing ','trailing ','2024-01-15','2024-01-15 12:34:56');
INSERT INTO t VALUES (5,1,0,12.34,-1.5,'abc','abc','1999-12-31',NULL);
INSERT INTO t VALUES (6,0,1,-5.5,1.5,NULL,'a',NULL,'1999-12-31 23:59:59');
INSERT INTO t VALUES (7,2,NULL,999.99,0.0,'Zed','trailing ','2030-06-01',NULL);
INSERT INTO t VALUES (8,2,1,0.0,0.0,'','Zed','2030-06-01',NULL);
CREATE TABLE t2 AS SELECT * FROM t;
SELECT COUNT(t2.c_chr) FROM t2 WHERE (NOT false);


-- ============================================================================================
-- Controls. Each changes exactly ONE thing about the distilled repro and is CORRECT, so each names
-- something the bug requires. Table and data identical to `distilled-second-column` throughout.
-- ============================================================================================

-- >>> BLOCK: control-two-aggregates  expect=ok  value=2  correct=2
-- A second aggregate over a DIFFERENT column makes the scan project two columns -> correct.
-- (Returns COUNT(q) as the first of two columns; the verifier checks the first.)
CREATE TABLE u (p BIGINT, q BIGINT, r BIGINT);
INSERT INTO u VALUES (1,NULL,NULL),(2,20,NULL),(3,30,300),(NULL,NULL,NULL);
SELECT COUNT(q), COUNT(r) FROM u;

-- >>> BLOCK: control-count-star-present  expect=ok  value=4  correct=4
-- COUNT(*) alongside is fine, and COUNT(q) beside it becomes correct too (4, 2).
CREATE TABLE u (p BIGINT, q BIGINT, r BIGINT);
INSERT INTO u VALUES (1,NULL,NULL),(2,20,NULL),(3,30,300),(NULL,NULL,NULL);
SELECT COUNT(*), COUNT(q) FROM u;

-- >>> BLOCK: control-duplicate-aggregate  expect=wrong  value=3  correct=2
-- Repeating the SAME column does NOT help: the scan still projects one column. This is the control
-- that shows it is the scan's column count that matters, not the number of SELECT items.
CREATE TABLE u (p BIGINT, q BIGINT, r BIGINT);
INSERT INTO u VALUES (1,NULL,NULL),(2,20,NULL),(3,30,300),(NULL,NULL,NULL);
SELECT COUNT(q), COUNT(q) FROM u;

-- >>> BLOCK: control-literal-beside  expect=wrong  value=3  correct=2
-- Nor does an extra literal -- it adds a SELECT item but no column to the scan.
CREATE TABLE u (p BIGINT, q BIGINT, r BIGINT);
INSERT INTO u VALUES (1,NULL,NULL),(2,20,NULL),(3,30,300),(NULL,NULL,NULL);
SELECT COUNT(q), 1 FROM u;

-- >>> BLOCK: control-real-where  expect=ok  value=2  correct=2
-- A real WHERE puts a Filter between GroupBy and Table -> correct. Note the predicate may name the
-- SAME column; it is the extra plan node that matters, not which column it mentions.
CREATE TABLE u (p BIGINT, q BIGINT, r BIGINT);
INSERT INTO u VALUES (1,NULL,NULL),(2,20,NULL),(3,30,300),(NULL,NULL,NULL);
SELECT COUNT(q) FROM u WHERE q IS NOT NULL;

-- >>> BLOCK: control-folded-where  expect=wrong  value=3  correct=2
-- ...but a WHERE the optimizer folds away leaves no Filter node, so it stays wrong. `WHERE 1=1` is
-- the reduced form of the finding's own `WHERE (NOT false)`.
CREATE TABLE u (p BIGINT, q BIGINT, r BIGINT);
INSERT INTO u VALUES (1,NULL,NULL),(2,20,NULL),(3,30,300),(NULL,NULL,NULL);
SELECT COUNT(q) FROM u WHERE 1=1;

-- >>> BLOCK: control-order-by  expect=ok  value=2  correct=2
-- ORDER BY adds a Sort above the GroupBy -> correct.
CREATE TABLE u (p BIGINT, q BIGINT, r BIGINT);
INSERT INTO u VALUES (1,NULL,NULL),(2,20,NULL),(3,30,300),(NULL,NULL,NULL);
SELECT COUNT(q) FROM u ORDER BY q;

-- >>> BLOCK: control-derived-table  expect=ok  value=2  correct=2
-- A derived table with ORDER BY (not flattenable) -> correct.
CREATE TABLE u (p BIGINT, q BIGINT, r BIGINT);
INSERT INTO u VALUES (1,NULL,NULL),(2,20,NULL),(3,30,300),(NULL,NULL,NULL);
SELECT COUNT(q) FROM (SELECT * FROM u ORDER BY p) z;

-- >>> BLOCK: control-group-by  expect=ok  value=2  correct=2
-- A real GROUP BY -> correct. Grouping on `p IS NULL` yields (2) then (0); the verifier checks the first.
CREATE TABLE u (p BIGINT, q BIGINT, r BIGINT);
INSERT INTO u VALUES (1,NULL,NULL),(2,20,NULL),(3,30,300),(NULL,NULL,NULL);
SELECT COUNT(q) FROM u GROUP BY p IS NULL ORDER BY p IS NULL;

-- >>> BLOCK: control-count-distinct  expect=ok  value=2  correct=2
-- COUNT(DISTINCT q) takes a different path and is correct (q has 2 distinct non-NULL values).
CREATE TABLE u (p BIGINT, q BIGINT, r BIGINT);
INSERT INTO u VALUES (1,NULL,NULL),(2,20,NULL),(3,30,300),(NULL,NULL,NULL);
SELECT COUNT(DISTINCT q) FROM u;

-- >>> BLOCK: control-other-aggregates-fine  expect=ok  value=50  correct=50
-- Only COUNT is affected: SUM/MIN/MAX/AVG over the same single-column scan are correct.
-- SUM(q) = 20 + 30 = 50.
CREATE TABLE u (p BIGINT, q BIGINT, r BIGINT);
INSERT INTO u VALUES (1,NULL,NULL),(2,20,NULL),(3,30,300),(NULL,NULL,NULL);
SELECT SUM(q) FROM u;

-- >>> BLOCK: control-min-fine  expect=ok  value=20  correct=20
CREATE TABLE u (p BIGINT, q BIGINT, r BIGINT);
INSERT INTO u VALUES (1,NULL,NULL),(2,20,NULL),(3,30,300),(NULL,NULL,NULL);
SELECT MIN(q) FROM u;

-- >>> BLOCK: single-column-table-also-wrong  expect=wrong  value=4  correct=2
-- A ONE-column table is wrong too, and this is the broadest form of the bug: with no preceding
-- column it degenerates to COUNT(*), so `SELECT COUNT(q) FROM s` counts rows instead of non-NULL
-- values. q has 2 non-NULL of 4 rows; Dolt returns 4.
CREATE TABLE s (q BIGINT);
INSERT INTO s VALUES (NULL),(20),(30),(NULL);
SELECT COUNT(q) FROM s;


-- ============================================================================================
-- cli-note
-- ============================================================================================
-- Only the sql-server is affected. Same binary, same data, `dolt sql` returns the correct answer:
--
--   $ dolt sql -q "USE d; SELECT COUNT(x) cx, COUNT(y) cy, COUNT(*) star FROM t;"
--   +----+----+------+
--   | cx | cy | star |
--   +----+----+------+
--   | 2  | 1  | 3    |     <-- correct
--
-- Decisive plan diff (both via `EXPLAIN PLAN` on the server):
--
--   WRONG  SELECT COUNT(q) FROM u                    correct  SELECT COUNT(q) FROM u WHERE q IS NOT NULL
--   Project                                          Project
--    └─ GroupBy                                       └─ GroupBy
--        ├─ select: COUNT(u.q)                            ├─ select: COUNT(u.q)
--        └─ Table  columns: [q]                           └─ Filter (NOT(u.q IS NULL))
--                                                             └─ Table  columns: [q]
--
--   correct  SELECT COUNT(q), COUNT(r) FROM u   ->   GroupBy over Table columns: [q r]   (two columns)
--
-- GroupBy sitting DIRECTLY on a one-column Table scan is the trigger.
