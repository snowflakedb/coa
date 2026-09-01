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

-- =====================================================================================
-- Dolt: a row-count-dependent window aggregate (COUNT/SUM) whose argument references no
-- column returns the CURRENT-ROW count instead of the partition count, whenever NO COLUMN
-- of the table is referenced anywhere in the query.
--
--   SELECT COUNT(*) OVER () FROM b     -- dolt: 1,1,1     MySQL 9.7: 3,3,3
--
-- The OVER clause does NOT have to be empty: a PARTITION BY / ORDER BY over *constants* is
-- equally affected (see PART 4). Only a column-dependent one rescues it.
--
-- Engine:  dolt 8.0.31, source v2.2.3-49-ga995f245c, commit a995f245c032, assertions off
--          go-mysql-server v0.20.1-0.20260805191915-e5eafe0da809
-- sql_mode: not load-bearing (reproduces under any); charset utf8mb4, db collation
--          utf8mb4_0900_bin
--
-- Row counts are NOT affected -- `SELECT 1 FROM b` correctly returns 3 rows. It is the
-- window frame that collapses to a single row, so the aggregate over it is wrong.
-- =====================================================================================

-- ============================ PART 1 -- distilled minimal repro =====================

CREATE DATABASE p1; USE p1;
CREATE TABLE b (x BIGINT);
INSERT INTO b VALUES (1),(2),(3);

-- Expected 3, 3, 3.  Actual on dolt: 1, 1, 1.
SELECT COUNT(*) OVER () FROM b;

-- ============================ PART 2 -- as the fuzzer generated it ==================
-- dolt_run10 round43. The window value feeds a join key, so a wrong count silently
-- changes which rows the query returns -- the oracle saw 2 rows go missing, not a wrong
-- number. `sq2` projects only a NULL constant and the window value, which is exactly the
-- shape that triggers it.

CREATE DATABASE p2; USE p2;
CREATE TABLE t (`id` BIGINT, `name` VARCHAR(255), `created_at` VARCHAR(255));
INSERT INTO t VALUES (-3,'a','a'),(-1,'',''),(0,'dup','dup'),(1,'dup','dup'),
                     (2,NULL,NULL),(2,'zzz','zzz'),(NULL,'b','b'),(7,'é','é');

-- Expected (MySQL): (NULL, 8).  Actual on dolt: (NULL, 1).
SELECT DISTINCT CEIL(CAST(NULL AS SIGNED)) AS expr_0_number,
                CAST(COUNT(*) OVER (ORDER BY '©' DESC) AS SIGNED) AS expr_1_number
FROM t AS t1;

-- ...which then selects different rows downstream, because expr_1_number is the join key:
--   ... FROM (<the above>) AS sq2 INNER JOIN t AS t3 ON sq2.expr_1_number = t3.id ...

-- ============================ PART 3 -- controls, one ingredient each ===============
-- Every one of these returns the correct 3 on dolt. Each changes exactly one thing.

USE p1;

-- C1  project any other column from the table        -> 3, 3, 3
SELECT x, COUNT(*) OVER () FROM b;

-- C2  a column-dependent aggregate argument          -> 3, 3, 3
SELECT COUNT(x) OVER () FROM b;

-- C3  ... even a trivial expression on the column    -> 3, 3, 3
SELECT COUNT(x + 0) OVER () FROM b;

-- C4  any WHERE, even always-true                    -> 3, 3, 3
SELECT COUNT(*) OVER () FROM b WHERE x >= 0;

-- C5  a PARTITION BY                                 -> 3, 3, 3
SELECT COUNT(*) OVER (PARTITION BY x IS NOT NULL) FROM b;

-- C6  an ORDER BY                                    -> 1, 2, 3 (cumulative; correct)
SELECT COUNT(*) OVER (ORDER BY x) FROM b;

-- C7  read through a VIEW                            -> 3, 3, 3
CREATE VIEW bv AS SELECT x FROM b;
SELECT COUNT(*) OVER () FROM bv;

-- C8  row counts themselves are fine                 -> 3 rows
SELECT 1 FROM b;

-- C9  the ordinary aggregate is fine                 -> 3
SELECT COUNT(*) FROM b;

-- ============================ PART 4 -- what else is affected =======================
-- Not COUNT-specific: any aggregate whose argument is constant.

SELECT COUNT(0)   OVER () FROM b;   -- dolt 1,1,1        MySQL 3,3,3
SELECT COUNT('s') OVER () FROM b;   -- dolt 1,1,1        MySQL 3,3,3
SELECT SUM(1)     OVER () FROM b;   -- dolt 1,1,1        MySQL 3,3,3

-- An explicit whole-partition frame does NOT help, so it is not frame defaulting:
SELECT COUNT(*) OVER (ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) FROM b;
                                    -- dolt 1,1,1        MySQL 3,3,3

-- Nor does a PARTITION BY / ORDER BY over CONSTANTS -- only a column-dependent one does (C5/C6).
-- This is what mismatch_round61_0 established: the OVER clause's shape is not the condition.
SELECT COUNT(*) OVER (PARTITION BY 'k') FROM b;                     -- dolt 1,1,1   MySQL 3,3,3
SELECT COUNT(*) OVER (ORDER BY 'k') FROM b;                         -- dolt 1,1,1   MySQL 3,3,3
SELECT COUNT(*) OVER (PARTITION BY 'k' ORDER BY 'j'
                      ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) FROM b;
                                                                    -- dolt 1,1,1   MySQL 3,3,3

-- But an aggregate whose value does not depend on the frame size is unaffected either way:
SELECT AVG('-12345678') OVER (ORDER BY 'k') FROM b;   -- both: -12345678 x3

-- A derived table and a CTE do not help either (they flatten into the scan):
SELECT COUNT(*) OVER () FROM (SELECT x FROM b) d;          -- dolt 1,1,1
WITH c AS (SELECT x FROM b) SELECT COUNT(*) OVER () FROM c; -- dolt 1,1,1

-- Independent of scale: with 100 rows the answer is still 1.
