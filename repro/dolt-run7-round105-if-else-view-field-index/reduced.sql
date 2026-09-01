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
-- Dolt: `if(<cond>, <literal>, <column>)` in a WHERE over a view-backed derived table
-- raises `1105 unable to find field with index N in row of M columns` when the
-- condition evaluates to NULL (so the else branch is taken).
--
-- Engine:  dolt 8.0.31, source v2.2.3-49-ga995f245c, commit a995f245c032, assertions off
--          go-mysql-server v0.20.1-0.20260805191915-e5eafe0da809
-- sql_mode: STRICT_ALL_TABLES,NO_ZERO_DATE,NO_ZERO_IN_DATE,ERROR_FOR_DIVISION_BY_ZERO,
--           NO_ENGINE_SUBSTITUTION,ONLY_FULL_GROUP_BY,NO_BACKSLASH_ESCAPES,PIPES_AS_CONCAT
-- charset:  utf8mb4 / utf8mb4_0900_bin (database), utf8mb4_0900_ai_ci (connection)
--
-- MySQL 9.7 returns rows for every query below.
--
-- Found by the eqgen data-equivalence oracle: the workload query was held fixed while
-- the relation under it was swapped for a row-identical rewrite. The base table answers
-- fine; the view-backed rewrite raises the engine's own "This is a bug" error.
-- Original findings: hunt log (also error_round105_0,
-- and hunt log -- same class, larger chains).
-- =====================================================================================

-- ============================ PART 1 -- as the builder emits it ======================
-- The equivalent `t` in error_round105_1: a surrogate-key flag-table join round-trip
-- (drops eq_uid_1) stacked under a QUALIFY window-filter view (drops _qf). Both layers
-- turn out to be irrelevant -- see PART 2 -- but this is the shape the fuzzer produced.

CREATE DATABASE p1; USE p1;
CREATE TABLE t (`id` BIGINT, `name` VARCHAR(255), `created_at` VARCHAR(255));
INSERT INTO t VALUES (-3,'a','a'),(-1,'',''),(0,'dup','dup'),(1,'dup','dup'),
                     (2,NULL,NULL),(2,'zzz','zzz'),(NULL,'b','b'),(7,'é','é');
ALTER TABLE t RENAME TO t__base;
CREATE TABLE t__base_table_1 AS SELECT id, name, created_at, ROW_NUMBER() OVER (ORDER BY id) AS eq_uid_1 FROM t__base;
CREATE TABLE t__base_table_2 AS SELECT eq_uid_1, 1 AS eq_flag_2 FROM t__base_table_1;
CREATE TABLE t__base_table_3 AS SELECT l.id AS id, l.name AS name, l.created_at AS created_at
                               FROM t__base_table_1 l INNER JOIN t__base_table_2 r ON l.eq_uid_1 = r.eq_uid_1
                               WHERE r.eq_flag_2 = 1;
CREATE VIEW t AS SELECT id, name, created_at
                 FROM (SELECT id, name, created_at, ROW_NUMBER() OVER (ORDER BY id) >= 1 AS _qf
                       FROM t__base_table_3) AS _qw
                 WHERE _qf;

-- The workload query, verbatim from the finding.
-- Expected: 7 rows.  Actual: 1105 unable to find field with index 13 in row of 3 columns.
SELECT MAX(NULLIF(sq2.expr_2_boolean, sq2.expr_2_boolean)) AS expr_0_boolean,
       CASE WHEN CAST('yes' AS SIGNED) THEN sq2.expr_1_varchar
            ELSE least(LEFT(sq2.expr_1_varchar, sq2.expr_0_number), sq2.expr_1_varchar) END AS expr_1_varchar,
       sq2.expr_2_boolean AS expr_2_boolean
FROM (SELECT '-12345678' AS expr_0_number, t1.name AS expr_1_varchar, 1 AS expr_2_boolean FROM t AS t1) AS sq2
WHERE if(CAST(to_base64(DAYNAME('2014-04-04 10:10:10.100000')) AS CHAR(255)) <>
         LEAST(LTRIM(sq2.expr_1_varchar), sq2.expr_1_varchar, sq2.expr_1_varchar,
               sq2.expr_1_varchar, sq2.expr_1_varchar, 'HOUR'),
         1 AND (sq2.expr_0_number <=> sq2.expr_0_number),
         sq2.expr_2_boolean)
GROUP BY sq2.expr_2_boolean, sq2.expr_1_varchar, sq2.expr_0_number
HAVING MIN(sq2.expr_0_number) < sq2.expr_0_number;

-- ============================ PART 2 -- distilled minimal repro =====================
-- Neither window function, neither extra table, no GROUP BY, no HAVING, one NULL row.

CREATE DATABASE p2; USE p2;
CREATE TABLE base (name VARCHAR(255));
INSERT INTO base VALUES (NULL);
CREATE VIEW v AS SELECT name FROM base;

-- Expected: 1 row (1).  Actual: 1105 unable to find field with index N in row of 1 columns.
-- (N is not stable -- observed 2, 4, 13, 20 and 41 across runs of the same and related shapes.)
SELECT sq.b
FROM (SELECT t1.name AS v, 1 AS b FROM v AS t1) AS sq
WHERE if(sq.v <> 'x', 1, sq.b);

-- ============================ PART 3 -- controls, one ingredient each ===============
-- Each swaps exactly one token from PART 2 and returns rows.

-- C1  base TABLE instead of the view                     -> 1 row
SELECT sq.b FROM (SELECT t1.name AS v, 1 AS b FROM base AS t1) AS sq WHERE if(sq.v <> 'x', 1, sq.b);

-- C2  else branch is a literal, not a column             -> 1 row
SELECT sq.b FROM (SELECT t1.name AS v, 1 AS b FROM v AS t1) AS sq WHERE if(sq.v <> 'x', 1, 1);

-- C3  condition is a literal, not column-dependent       -> 1 row
SELECT sq.b FROM (SELECT t1.name AS v, 1 AS b FROM v AS t1) AS sq WHERE if(1, 1, sq.b);

-- C4  no if() at all                                     -> 1 row
SELECT sq.b FROM (SELECT t1.name AS v, 1 AS b FROM v AS t1) AS sq WHERE sq.b;

-- C5  no NULL in the data, so the else branch is never reached -> 1 row
CREATE TABLE nn (name VARCHAR(255)); INSERT INTO nn VALUES ('a');
CREATE VIEW vnn AS SELECT name FROM nn;
SELECT sq.b FROM (SELECT t1.name AS v, 1 AS b FROM vnn AS t1) AS sq WHERE if(sq.v <> 'x', 1, sq.b);
