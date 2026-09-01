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

-- MySQL 9.7.2 @008e09c2 (release, assertions off, mysql-release/bin).
-- Session (finding): sql_mode ONLY_FULL_GROUP_BY,NO_BACKSLASH_ESCAPES,STRICT_ALL_TABLES,NO_ZERO_IN_DATE,
--   NO_ZERO_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION ; utf8mb4 / utf8mb4_0900_bin.
--   (Bug is independent of sql_mode/collation.)
--
-- BUG: for `SELECT DISTINCT <collapsing aggregate> ... GROUP BY g HAVING <aggregate cond>`, MySQL
-- applies HAVING AFTER the SELECT-DISTINCT deduplication (SQL order is GROUP BY -> HAVING -> SELECT
-- -> DISTINCT). EXPLAIN FORMAT=TREE on the query below:
--     -> Filter: (max(t.id) <= 100)                 <- HAVING, applied LAST
--        -> Temporary table with deduplication       <- SELECT DISTINCT, runs FIRST
--           -> Aggregate using temporary table        <- GROUP BY
-- DISTINCT collapses the surviving groups to one row, so the post-dedup HAVING re-reads MAX(id)
-- from an arbitrary surviving row -> the result depends on PHYSICAL ROW ORDER. With the group the
-- HAVING excludes (the NULL-id group) physically first, MySQL drops every row. MariaDB is correct
-- and order-independent.

-- ============ MINIMAL REPRO (2 rows, plain table) ============
-- (a) NULL-id row FIRST -> WRONG (0 rows):
CREATE TABLE t (id BIGINT, name VARCHAR(255));
INSERT INTO t VALUES (NULL, 'b'), (1, 'a');
SELECT DISTINCT CAST(MAX(1) AS SIGNED) AS e0
FROM t GROUP BY name HAVING MAX(id) <= 100;
-- Expected 1 row: (1)   [group 'a': MAX(id)=1<=100 kept, e0=1; group 'b': MAX(id)=NULL -> HAVING NULL -> excluded]
-- MySQL actual: 0 rows.

-- (b) CONTROL — same rows, NULL-id row LAST -> CORRECT (1 row):
DROP TABLE t; CREATE TABLE t (id BIGINT, name VARCHAR(255));
INSERT INTO t VALUES (1, 'a'), (NULL, 'b');
SELECT DISTINCT CAST(MAX(1) AS SIGNED) AS e0
FROM t GROUP BY name HAVING MAX(id) <= 100;
-- Expected 1 row: (1).  MySQL actual: (1).  <- correct for this order

-- ============ CONTROLS (each removes one necessary ingredient; NULL-first order, all CORRECT) ============
DROP TABLE t; CREATE TABLE t (id BIGINT, name VARCHAR(255));
INSERT INTO t VALUES (NULL, 'b'), (1, 'a');

-- (c) drop the CAST (bare MAX(1)) -> correct (1 row):
SELECT DISTINCT MAX(1) AS e0 FROM t GROUP BY name HAVING MAX(id) <= 100;

-- (d) drop DISTINCT -> correct (the kept group's row):
SELECT CAST(MAX(1) AS SIGNED) AS e0 FROM t GROUP BY name HAVING MAX(id) <= 100;

-- (e) non-collapsing aggregate (CAST(MAX(id)) differs per group, so DISTINCT can't collapse) -> correct:
SELECT DISTINCT CAST(MAX(id) AS SIGNED) AS e0 FROM t GROUP BY name HAVING MAX(id) <= 100;

-- (f) MariaDB 12.3.3 returns (1) for BOTH orders on query (a)/(b) — reference that the query is
--     order-independent, so MySQL's empty result is a wrong result, not an ambiguous query.

-- ============ ORIGIN: how the fuzzer's equivalence rewrite exposed it ============
-- The finding's equivalent `t` is the eq_my rebuild, whose load-bearing step reorders rows via
-- ROW_NUMBER() OVER (ORDER BY id) (NULL id sorts first), turning the base table's order into the
-- pathological NULL-first order above — while staying row-identical:
--   CREATE TABLE t__base_table_3 AS SELECT id,name,created_at, ROW_NUMBER() OVER (ORDER BY id) AS uk FROM t__base;
--   CREATE VIEW t AS SELECT id,name,created_at FROM t__base_table_3;
-- The original workload query (mismatch_round161_0.sql) is a longer instance of the same shape:
--   SELECT DISTINCT CAST(MAX(...) AND MIN(...) AS SIGNED), (scalar subq), MIN(least(...))
--   FROM t GROUP BY name HAVING MAX(RTRIM(CAST(id AS CHAR(255)))) <= name;
