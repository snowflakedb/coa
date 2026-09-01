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

-- MISMATCH (wrong result: mergeable VIEW / derived table returns zero groups)
-- engine=mariadb 11.4.12-MariaDB-ubu2404 (docker mariadb:11.4)
-- sql_mode: STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES (not load-bearing)
-- collation: utf8mb4_nopad_bin / utf8mb4
--
-- A GROUP BY that SELECTs an expression mixing an aggregate (MIN/MAX/SUM/AVG/COUNT)
-- with the grouping column, over a mergeable VIEW or derived table, returns ZERO rows
-- when WHERE uses col {>=,<} ANY/ALL (SELECT col) or col IN (SELECT col FROM t JOIN t …).
-- The same statement on the base table returns one row per group. ALGORITHM=TEMPTABLE
-- and derived_merge=off (derived tables) restore the heap answer. MySQL 9.7.2 is correct.
--
-- Covers eqgen mariadb_20260816-061046 leftovers whose original query already diverges
-- on CREATE VIEW t AS SELECT * FROM b, including:
--   mismatch_round1006_46.sql  CONCAT_WS(MIN(txt), …, txt) WHERE txt >= ANY … GROUP BY txt
--   mismatch_round1006_14.sql  LEFT(txt, MIN(int)) WHERE txt != ANY … GROUP BY txt
--   mismatch_round115_1.sql    MIN(int)+c_big WHERE c_big IN (join subquery) GROUP BY c_big
--   mismatch_round112_25.sql   GREATEST(dec, SUM(…)) + join / ANY (HAVING is not required)
--
-- Necessary ingredients (each verified by a control):
--   * mergeable VIEW or derived table (not the base table; not ALGORITHM=TEMPTABLE)
--   * GROUP BY the column
--   * SELECT mixes aggregate(col) with the grouping column in ONE expression
--     (MIN(id)+id, CONCAT(MIN(txt), txt), COUNT(*)+id, …). Two separate SELECT items do NOT.
--   * WHERE col {>=,<} ANY/ALL (SELECT col), or col IN (SELECT col FROM t JOIN t …).
--     Simple IN / = ANY / EXISTS / no subquery do NOT. Window is NOT required (unlike MDEV-40557).


-- =====================================================================================
-- PART 1 -- CONCRETE: 1006_46 seed (truncated to the load-bearing columns) + the
-- CONCAT_WS(MIN(txt), grouping txt) + >= ANY + GROUP BY fragment the oracle flagged.
-- Heap: 3 rows. Identity view: 0 rows.
-- =====================================================================================
CREATE TABLE b (
  c_txt VARCHAR(255),
  c_int BIGINT
);
INSERT INTO b VALUES (NULL, NULL);
INSERT INTO b VALUES ('', -1);
INSERT INTO b VALUES ('abc', 42);
INSERT INTO b VALUES ("o'brien", -1);

CREATE VIEW t AS SELECT * FROM b;

SELECT CONCAT_WS(',', MIN(t1.c_txt), 'YEAR', t1.c_txt, 'HOUR')
FROM t t1
WHERE t1.c_txt >= ANY (SELECT t2.c_txt FROM t t2 WHERE 1)
GROUP BY t1.c_txt;
-- Expected 3 rows (heap). Actual 0 rows (view).


-- =====================================================================================
-- PART 2 -- DISTILLED. Two integers, identity view. 1 row is already enough.
-- =====================================================================================
CREATE TABLE b (id BIGINT);
INSERT INTO b VALUES (1), (2);
CREATE VIEW t AS SELECT * FROM b;

-- Expected 2 rows (2), (4). Actual 0 rows.
SELECT MIN(id) + id
FROM t t1
WHERE t1.id >= ANY (SELECT t2.id FROM t t2)
GROUP BY t1.id;

-- Same bug, no VIEW keyword (derived-table merge):
-- Expected 2 rows. Actual 0 rows.
SELECT MIN(id) + id
FROM (SELECT * FROM b) t1
WHERE t1.id >= ANY (SELECT t2.id FROM (SELECT * FROM b) t2)
GROUP BY t1.id;

-- Same bug, join-IN instead of ANY (115_1):
-- Expected 2 rows. Actual 0 rows.
SELECT MIN(id) + id
FROM t t1
WHERE t1.id IN (SELECT t2.id FROM t t2 JOIN t t3 ON t2.id = t3.id)
GROUP BY t1.id;

-- Same bug, CONCAT of MIN + grouping column (1006_46 string shape).
-- Re-create with a text column:
CREATE TABLE b2 (txt VARCHAR(255));
INSERT INTO b2 VALUES ('a'), ('b');
CREATE VIEW t2 AS SELECT * FROM b2;
SELECT CONCAT(MIN(txt), txt)
FROM t2 t1
WHERE t1.txt >= ANY (SELECT t2.txt FROM t2 t2)
GROUP BY t1.txt;
-- Expected 2 rows ('aa'), ('bb'). Actual 0 rows.


-- =====================================================================================
-- PART 3 -- SAME ROOT, different WHERE: simple IN fires once the mergeable view is
-- `SELECT … FROM u WHERE tag = 1` and u also holds discarded tag=0 copies
-- (eqgen tag-split + extra INSERT, mismatch_round203_11.sql). Identity SELECT * does
-- NOT fire simple IN. FROM u WHERE tag=1 *without* a VIEW is correct.
-- =====================================================================================
CREATE TABLE b (
  c_pk BIGINT NOT NULL, c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10,2),
  c_dbl DOUBLE, c_txt VARCHAR(255), c_chr VARCHAR(255), c_date DATE, c_ts DATETIME(6)
);
INSERT INTO b VALUES (1, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);
INSERT INTO b VALUES (2, 1, -1, 12.34, 1000.125, 'Zed', 'Zed', '2024-01-15', '2024-01-15 12:34:56');
INSERT INTO b VALUES (3, -1, -7, NULL, 0.0, 'a', 'trailing ', '1999-12-31', NULL);
INSERT INTO b VALUES (4, 2, 1, 999.99, -1.5, 'trailing ', 'a', '2024-01-15', '2024-01-15 12:34:56');
INSERT INTO b VALUES (5, 2, NULL, 12.34, -1.5, '', 'Zed', NULL, '1999-12-31 23:59:59');
INSERT INTO b VALUES (6, 2, -7, NULL, 0.0, 'a', 'o''brien', '2030-06-01', '2024-01-15 12:34:56');
INSERT INTO b VALUES (7, 42, -1, -5.5, 1.5, 'o''brien', 'a', '2024-01-15', '1999-12-31 23:59:59');
INSERT INTO b VALUES (8, 1, -1, 12.34, 1000.125, 'Zed', 'Zed', '2024-01-15', '2024-01-15 12:34:56');
CREATE TABLE u AS SELECT *, 1 AS tag FROM b UNION ALL SELECT *, 0 AS tag FROM b;
INSERT INTO u SELECT c_pk,c_int,c_big,c_dec,c_dbl,c_txt,c_chr,c_date,c_ts, 0 FROM u WHERE tag = 1;
CREATE VIEW t AS SELECT c_pk,c_int,c_big,c_dec,c_dbl,c_txt,c_chr,c_date,c_ts FROM u WHERE tag = 1;

-- Expected 3 rows. Actual 0 rows.
SELECT COUNT(*) + t1.c_dec FROM t t1
WHERE t1.c_dec IN (SELECT t2.c_dec FROM t t2)
GROUP BY t1.c_dec;

-- Control: same u, no VIEW — not zero:
SELECT COUNT(*) + t1.c_dec FROM u t1
WHERE t1.tag = 1 AND t1.c_dec IN (SELECT t2.c_dec FROM u t2 WHERE t2.tag = 1)
GROUP BY t1.c_dec;


-- =====================================================================================
-- CONTROLS. Each swaps one ingredient; all match the heap.
-- =====================================================================================

-- C1 heap (no view)
CREATE TABLE t (id BIGINT);
INSERT INTO t VALUES (1), (2);
SELECT MIN(id) + id FROM t t1
WHERE t1.id >= ANY (SELECT t2.id FROM t t2) GROUP BY t1.id;
-- 2 rows: (2), (4)

-- C2 MIN only (no grouping column in the SELECT expression)
CREATE TABLE b (id BIGINT);
INSERT INTO b VALUES (1), (2);
CREATE VIEW t AS SELECT * FROM b;
SELECT MIN(id) FROM t t1
WHERE t1.id >= ANY (SELECT t2.id FROM t t2) GROUP BY t1.id;
-- 2 rows: (1), (2)

-- C3 two SELECT items (MIN, grouping col) — mixing must be in ONE expression
CREATE TABLE b (id BIGINT);
INSERT INTO b VALUES (1), (2);
CREATE VIEW t AS SELECT * FROM b;
SELECT MIN(id), id FROM t t1
WHERE t1.id >= ANY (SELECT t2.id FROM t t2) GROUP BY t1.id;
-- 2 rows: (1,1), (2,2)

-- C4 CONCAT(MIN, constant) — grouping column not in the expression
CREATE TABLE b (txt VARCHAR(255));
INSERT INTO b VALUES ('a'), ('b');
CREATE VIEW t AS SELECT * FROM b;
SELECT CONCAT(MIN(txt), 'x') FROM t t1
WHERE t1.txt >= ANY (SELECT t2.txt FROM t t2) GROUP BY t1.txt;
-- 2 rows: ('ax'), ('bx')

-- C5 simple IN / = ANY (semijoin, not quantified inequality)
CREATE TABLE b (id BIGINT);
INSERT INTO b VALUES (1), (2);
CREATE VIEW t AS SELECT * FROM b;
SELECT MIN(id) + id FROM t t1
WHERE t1.id IN (SELECT t2.id FROM t t2) GROUP BY t1.id;
-- 2 rows: (2), (4)
SELECT MIN(id) + id FROM t t1
WHERE t1.id = ANY (SELECT t2.id FROM t t2) GROUP BY t1.id;
-- 2 rows: (2), (4)

-- C6 EXISTS
CREATE TABLE b (id BIGINT);
INSERT INTO b VALUES (1), (2);
CREATE VIEW t AS SELECT * FROM b;
SELECT MIN(id) + id FROM t t1
WHERE EXISTS (SELECT 1 FROM t t2 WHERE t2.id = t1.id) GROUP BY t1.id;
-- 2 rows: (2), (4)

-- C7 no GROUP BY
CREATE TABLE b (id BIGINT);
INSERT INTO b VALUES (1), (2);
CREATE VIEW t AS SELECT * FROM b;
SELECT MIN(id) + id FROM t t1
WHERE t1.id >= ANY (SELECT t2.id FROM t t2);
-- 1 row (not zero)

-- C8 WHERE ANY without the mixed SELECT (predicate itself is fine)
CREATE TABLE b (id BIGINT);
INSERT INTO b VALUES (1), (2);
CREATE VIEW t AS SELECT * FROM b;
SELECT id FROM t t1
WHERE t1.id >= ANY (SELECT t2.id FROM t t2);
-- 2 rows: (1), (2)

-- C9 ALGORITHM=TEMPTABLE
CREATE TABLE b (id BIGINT);
INSERT INTO b VALUES (1), (2);
CREATE ALGORITHM=TEMPTABLE VIEW t AS SELECT * FROM b;
SELECT MIN(id) + id FROM t t1
WHERE t1.id >= ANY (SELECT t2.id FROM t t2) GROUP BY t1.id;
-- 2 rows: (2), (4)

-- C10 derived_merge=off on a derived table
CREATE TABLE b (id BIGINT);
INSERT INTO b VALUES (1), (2);
SET SESSION optimizer_switch='derived_merge=off';
SELECT MIN(id) + id
FROM (SELECT * FROM b) t1
WHERE t1.id >= ANY (SELECT t2.id FROM (SELECT * FROM b) t2)
GROUP BY t1.id;
-- 2 rows: (2), (4)
