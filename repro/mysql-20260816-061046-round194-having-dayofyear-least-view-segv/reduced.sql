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

-- MySQL 9.7.2 (docker mysql:9.7.2). A mergeable CAST/CASE/JSON DATE view (or a
-- derived table of the same CAST), queried with a table alias, SIGSEGVs on
--   HAVING MIN(dec) <= DAYOFYEAR(LEAST(date, date))
-- (GREATEST of two DATEs too). The harness recorded SIGABRT on lost connection;
-- mysqld's own handler reports signal 11 / SIGSEGV in Date_val::day_number
-- called from Item_func_dayofyear::val_int.
--
-- mysql:8.4.10 does NOT crash on PART 2: it returns 0 rows (wrong; heap is 1).
-- The no-LEAST dropped-group sibling and the SELECT 1 → 1054 sibling fire on
-- both 8.4.10 and 9.7.2. The SIGSEGV is a 9.x execution failure of a rewrite
-- 8.4 already gets wrong.
--
-- sql_mode: STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES (not load-bearing).
-- charset/collation: utf8mb4 / utf8mb4_0900_bin.
--
-- Crash cousin of the view-merge HAVING neighbourhood:
--   mysql-20260814-021542-round0-having-max-view-expr-1054  (1054 if SELECT omits date)
--   mysql-20260814-021542-round0-case-view-having-ifnull    (dropped groups for IFNULL)
-- Same CAST view + aliased HAVING MIN(dec) <= DAYOFYEAR(date) (no LEAST) drops the
-- group (0 rows) instead of crashing — wrong result, not this SIGSEGV.


-- =====================================================================================
-- PART 1 -- CONCRETE: compact rebuild of crash_round194_0.sql. The hunt's equivalent
-- ended as a JSON_OBJECT / JSON_EXTRACT unpack VIEW (expression-valued c_date). The
-- workload's window SUM / WHERE / extra SELECT items are not required; GROUP BY +
-- aliased HAVING MIN <= DAYOFYEAR(LEAST(date, date)) is.
-- Expected: 1 row. Actual: SIGSEGV (Lost connection, mysqld signal 11).
-- =====================================================================================
CREATE TABLE b (
  c_date DATE,
  c_dec DECIMAL(10,2),
  c_int BIGINT,
  c_ts DATETIME(6)
);
INSERT INTO b VALUES ('1999-12-31', -5.5, -7, '1999-12-31 23:59:59');

CREATE VIEW t AS
SELECT
  CAST(JSON_UNQUOTE(JSON_EXTRACT(j, '$.c_date')) AS DATE) AS c_date,
  CAST(JSON_EXTRACT(j, '$.c_dec') AS DECIMAL(10,2)) AS c_dec,
  CAST(JSON_EXTRACT(j, '$.c_int') AS SIGNED) AS c_int,
  CAST(JSON_UNQUOTE(JSON_EXTRACT(j, '$.c_ts')) AS DATETIME(6)) AS c_ts
FROM (SELECT JSON_OBJECT('c_date', c_date, 'c_dec', c_dec, 'c_int', c_int, 'c_ts', c_ts) AS j FROM b) x;

SELECT t1.c_date
FROM t t1
GROUP BY t1.c_date, t1.c_ts
HAVING MIN(t1.c_dec) <= DAYOFYEAR(LEAST(t1.c_date, t1.c_date));


-- =====================================================================================
-- PART 2 -- DISTILLED. JSON is not required: CAST(c_date AS DATE) is enough, as is a
-- CASE WHEN TRUE THEN c_date ELSE CAST(NULL AS DATE) END view, as is an inline derived
-- table of the same CAST (no VIEW). One row, two columns, GROUP BY the date column.
-- Expected: 1 row ('1999-12-31'). Actual 9.7.2: SIGSEGV. Actual 8.4.10: 0 rows.
-- =====================================================================================
CREATE TABLE b (c_date DATE, c_dec DECIMAL(10,2));
INSERT INTO b VALUES ('1999-12-31', -5.5);
CREATE VIEW t AS
  SELECT CAST(c_date AS DATE) AS c_date,
         CAST(c_dec AS DECIMAL(10,2)) AS c_dec
  FROM b;

SELECT t1.c_date
FROM t t1
GROUP BY t1.c_date
HAVING MIN(t1.c_dec) <= DAYOFYEAR(LEAST(t1.c_date, t1.c_date));
-- Expected 1 row, actual 9.7.2 SIGSEGV / 8.4.10 0 rows


-- =====================================================================================
-- CONTROLS (same data). Each swaps one ingredient; none crash.
-- =====================================================================================

-- C1 heap (no view) — CORRECT, 1 row
CREATE TABLE t (c_date DATE, c_dec DECIMAL(10,2));
INSERT INTO t VALUES ('1999-12-31', -5.5);
SELECT t1.c_date FROM t t1 GROUP BY t1.c_date
HAVING MIN(t1.c_dec) <= DAYOFYEAR(LEAST(t1.c_date, t1.c_date));

-- C2 identity view — CORRECT, 1 row
CREATE TABLE b (c_date DATE, c_dec DECIMAL(10,2));
INSERT INTO b VALUES ('1999-12-31', -5.5);
CREATE VIEW t AS SELECT * FROM b;
SELECT t1.c_date FROM t t1 GROUP BY t1.c_date
HAVING MIN(t1.c_dec) <= DAYOFYEAR(LEAST(t1.c_date, t1.c_date));

-- C3 ALGORITHM=TEMPTABLE — CORRECT, 1 row (view is materialized; merge does not fire)
CREATE TABLE b (c_date DATE, c_dec DECIMAL(10,2));
INSERT INTO b VALUES ('1999-12-31', -5.5);
CREATE ALGORITHM=TEMPTABLE VIEW t AS
  SELECT CAST(c_date AS DATE) AS c_date, CAST(c_dec AS DECIMAL(10,2)) AS c_dec FROM b;
SELECT t1.c_date FROM t t1 GROUP BY t1.c_date
HAVING MIN(t1.c_dec) <= DAYOFYEAR(LEAST(t1.c_date, t1.c_date));

-- C4 derived_merge=off — CORRECT, 1 row
SET SESSION optimizer_switch='derived_merge=off';
CREATE TABLE b (c_date DATE, c_dec DECIMAL(10,2));
INSERT INTO b VALUES ('1999-12-31', -5.5);
CREATE VIEW t AS
  SELECT CAST(c_date AS DATE) AS c_date, CAST(c_dec AS DECIMAL(10,2)) AS c_dec FROM b;
SELECT t1.c_date FROM t t1 GROUP BY t1.c_date
HAVING MIN(t1.c_dec) <= DAYOFYEAR(LEAST(t1.c_date, t1.c_date));

-- C5 no table alias — CORRECT, 1 row (same load-bearing alias as the IFNULL dropped-group sibling)
CREATE TABLE b (c_date DATE, c_dec DECIMAL(10,2));
INSERT INTO b VALUES ('1999-12-31', -5.5);
CREATE VIEW t AS
  SELECT CAST(c_date AS DATE) AS c_date, CAST(c_dec AS DECIMAL(10,2)) AS c_dec FROM b;
SELECT c_date FROM t GROUP BY c_date
HAVING MIN(c_dec) <= DAYOFYEAR(LEAST(c_date, c_date));

-- C6 YEAR(LEAST) / MONTH(LEAST) — CORRECT, 1 row. Only DAYOFYEAR of that LEAST crashes.
CREATE TABLE b (c_date DATE, c_dec DECIMAL(10,2));
INSERT INTO b VALUES ('1999-12-31', -5.5);
CREATE VIEW t AS
  SELECT CAST(c_date AS DATE) AS c_date, CAST(c_dec AS DECIMAL(10,2)) AS c_dec FROM b;
SELECT t1.c_date FROM t t1 GROUP BY t1.c_date
HAVING MIN(t1.c_dec) <= YEAR(LEAST(t1.c_date, t1.c_date));

-- C7 no LEAST: DAYOFYEAR(t1.c_date) — does NOT crash, but WRONG (0 rows; heap returns 1).
--    Same neighbourhood, different symptom.
CREATE TABLE b (c_date DATE, c_dec DECIMAL(10,2));
INSERT INTO b VALUES ('1999-12-31', -5.5);
CREATE VIEW t AS
  SELECT CAST(c_date AS DATE) AS c_date, CAST(c_dec AS DECIMAL(10,2)) AS c_dec FROM b;
SELECT t1.c_date FROM t t1 GROUP BY t1.c_date
HAVING MIN(t1.c_dec) <= DAYOFYEAR(t1.c_date);

-- C8 SELECT 1 (do not project t1.c_date) — 1054 Unknown column 't1.c_date' in 'having clause'
--    (the already-filed MAX/HAVING 1054 sibling).
CREATE TABLE b (c_date DATE, c_dec DECIMAL(10,2));
INSERT INTO b VALUES ('1999-12-31', -5.5);
CREATE VIEW t AS
  SELECT CAST(c_date AS DATE) AS c_date, CAST(c_dec AS DECIMAL(10,2)) AS c_dec FROM b;
SELECT 1 FROM t t1 GROUP BY t1.c_date
HAVING MIN(t1.c_dec) <= DAYOFYEAR(LEAST(t1.c_date, t1.c_date));
