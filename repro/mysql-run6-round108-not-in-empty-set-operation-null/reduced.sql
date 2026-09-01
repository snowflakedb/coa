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

-- MySQL / MariaDB: `x NOT IN (<set-operation subquery>)` drops rows when the set operation's
-- RESULT is empty but its INPUT contains NULL. The empty IN-list is treated as if it contained
-- NULL, so NOT IN collapses to UNKNOWN and rows are silently discarded.
--
-- Builds     : MySQL 9.7.2 @008e09c2 (release, assertions off)
--              MariaDB 12.3.3 (release, assertions off)
-- Correct on : TiDB v9.0.0-beta.2 @3bea8196  and  DuckDB 2.0.0-alpha
--              -- TiDB is MySQL-*compatible* but an independent implementation, so this is NOT
--              intended MySQL semantics; it localises the defect to the MySQL/MariaDB lineage.
-- sql_mode   : ONLY_FULL_GROUP_BY,NO_BACKSLASH_ESCAPES,STRICT_ALL_TABLES,NO_ZERO_IN_DATE,
--              NO_ZERO_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION
--              charset utf8mb4 / collation utf8mb4_0900_bin.  NO mode is load-bearing.
-- Origin     : hunt log (round 108, seed 1031118865)
--              admissibility verified: base t == equivalent t, 8 identical rows.
--
-- NOTE: this reproduces on a PLAIN TABLE with no eqgen equivalence at all. The oracle only
-- surfaced it because MySQL's variant is *physical-order dependent* (see PART 3), and the
-- equivalence chain's `ROW_NUMBER() OVER (ORDER BY id)` moves the NULL row to the front.
--
-- SEMANTICS: `x NOT IN (<empty set>)` is TRUE for every x, including NULL. So every row of `b`
-- must be returned. The subquery here IS empty -- `(SELECT id FROM b) EXCEPT (SELECT id FROM b)`
-- is a set difference of a set with itself -- and PART 2 measures it as empty on all four engines.
--
-- SUSPECTED MECHANISM: the IN-subquery's "contains NULL" flag is computed from the set
-- operation's INPUT scan rather than from its (empty) OUTPUT, so `NOT IN` short-circuits to
-- UNKNOWN. That also explains MySQL's order dependence: the flag is set when the NULL row is
-- reached during the scan, so outer rows evaluated before that point survive and later ones do
-- not (PART 3, O5). MariaDB sets it unconditionally, so it is wrong for every order.


-- =====================================================================================
-- PART 1 -- MINIMAL REPRO. One column, two rows, no views, no equivalence.
-- Expected 2 rows: (NULL) and (1).
--   MySQL 9.7.2   -> 1 row  (NULL)      WRONG -- the id=1 row is dropped
--   MariaDB 12.3.3-> 1 row  (NULL)      WRONG
--   TiDB / DuckDB -> 2 rows (NULL), (1) correct
-- =====================================================================================
CREATE TABLE b (id BIGINT);
INSERT INTO b VALUES (NULL),(1);
SELECT id FROM b WHERE id NOT IN ((SELECT t5.id FROM b AS t5) EXCEPT (SELECT t6.id FROM b AS t6));

-- =====================================================================================
-- PART 2 -- THE SUBQUERY IS EMPTY. Measured 0 on MySQL, MariaDB, TiDB and DuckDB, so the
-- expected answer above is not in question.
-- =====================================================================================
CREATE TABLE b (id BIGINT);
INSERT INTO b VALUES (NULL),(1);
SELECT COUNT(*) FROM ((SELECT t5.id FROM b AS t5) EXCEPT (SELECT t6.id FROM b AS t6)) x;          -- 0 on all four engines

-- =====================================================================================
-- PART 3 -- ORDER MATRIX. MySQL's answer depends on where the NULL physically sits;
-- MariaDB's does not. Correct answer is always "every row".
--   rows in b        expected | MySQL | MariaDB | TiDB | DuckDB
--   (NULL,1)             2    |   1 X |    1 X  |  2   |  2
--   (1,NULL)             2    |   2   |    1 X  |  2   |  2
--   (NULL,1,2)           3    |   1 X |    1 X  |  3   |  3
--   (1,2,NULL)           3    |   3   |    1 X  |  3   |  3
--   (1,NULL,2)           3    |   2 X |    1 X  |  3   |  3   <- rows AFTER the NULL are lost
--   (1,2)  [no NULL]     2    |   2   |    2    |  2   |  2
-- =====================================================================================
CREATE TABLE b (id BIGINT);
INSERT INTO b VALUES (NULL),(1);
SELECT id FROM b WHERE id NOT IN ((SELECT t5.id FROM b AS t5) EXCEPT (SELECT t6.id FROM b AS t6));

CREATE TABLE b (id BIGINT);
INSERT INTO b VALUES (1),(NULL);
SELECT id FROM b WHERE id NOT IN ((SELECT t5.id FROM b AS t5) EXCEPT (SELECT t6.id FROM b AS t6));

CREATE TABLE b (id BIGINT);
INSERT INTO b VALUES (NULL),(1),(2);
SELECT id FROM b WHERE id NOT IN ((SELECT t5.id FROM b AS t5) EXCEPT (SELECT t6.id FROM b AS t6));

CREATE TABLE b (id BIGINT);
INSERT INTO b VALUES (1),(2),(NULL);
SELECT id FROM b WHERE id NOT IN ((SELECT t5.id FROM b AS t5) EXCEPT (SELECT t6.id FROM b AS t6));

CREATE TABLE b (id BIGINT);
INSERT INTO b VALUES (1),(NULL),(2);
SELECT id FROM b WHERE id NOT IN ((SELECT t5.id FROM b AS t5) EXCEPT (SELECT t6.id FROM b AS t6));

-- =====================================================================================
-- PART 4 -- CONTROLS. Each changes exactly ONE thing from PART 1.
-- =====================================================================================

-- C1 no NULL in the table -> 2 rows, correct on all engines. The NULL is required.
CREATE TABLE b (id BIGINT);
INSERT INTO b VALUES (1),(2);
SELECT id FROM b WHERE id NOT IN ((SELECT t5.id FROM b AS t5) EXCEPT (SELECT t6.id FROM b AS t6));

-- C2 the NULL is in the OUTER table only; the subquery scans a NULL-free table `c`
--    -> 2 rows, correct. So the NULL must be in the SUBQUERY's input, not the outer table.
CREATE TABLE b (id BIGINT);
INSERT INTO b VALUES (NULL),(1);
CREATE TABLE c (id BIGINT);
INSERT INTO c VALUES (1);
SELECT id FROM b WHERE id NOT IN ((SELECT t5.id FROM c AS t5) EXCEPT (SELECT t6.id FROM c AS t6));

-- C3 the subquery scans a DIFFERENT table that DOES contain NULL -> 1 row, STILL WRONG.
--    Confirms C2's reading: it is the subquery's input that matters.
CREATE TABLE b (id BIGINT);
INSERT INTO b VALUES (NULL),(1);
CREATE TABLE c (id BIGINT);
INSERT INTO c VALUES (NULL),(1);
SELECT id FROM b WHERE id NOT IN ((SELECT t5.id FROM c AS t5) EXCEPT (SELECT t6.id FROM c AS t6));

-- C4 the same EXCEPT wrapped in a derived table -> 2 rows, correct.
CREATE TABLE b (id BIGINT);
INSERT INTO b VALUES (NULL),(1);
SELECT id FROM b WHERE id NOT IN (SELECT x.id FROM ((SELECT t5.id FROM b AS t5) EXCEPT (SELECT t6.id FROM b AS t6)) x);

-- C5 the EXCEPT materialised into a table first, then NOT IN -> 2 rows, correct.
--    (Not runnable on TiDB, which has no CREATE TABLE ... AS SELECT.)
CREATE TABLE b (id BIGINT);
INSERT INTO b VALUES (NULL),(1);
CREATE TABLE e AS SELECT * FROM ((SELECT t5.id FROM b AS t5) EXCEPT (SELECT t6.id FROM b AS t6)) x;
SELECT id FROM b WHERE id NOT IN (SELECT id FROM e);

-- C6 a plainly-empty subquery instead of an empty set operation -> 2 rows, correct.
CREATE TABLE b (id BIGINT);
INSERT INTO b VALUES (NULL),(1);
SELECT id FROM b WHERE id NOT IN (SELECT t5.id FROM b AS t5 WHERE 0);

-- C7 an empty set operation over CONSTANTS (no table, so no NULL in the input) -> 2 rows, correct.
CREATE TABLE b (id BIGINT);
INSERT INTO b VALUES (NULL),(1);
SELECT id FROM b WHERE id NOT IN ((SELECT 99) EXCEPT (SELECT 99));

-- C8 NOT EXISTS instead of NOT IN -> 2 rows, correct.
CREATE TABLE b (id BIGINT);
INSERT INTO b VALUES (NULL),(1);
SELECT id FROM b WHERE NOT EXISTS (SELECT 1 FROM ((SELECT t5.id FROM b AS t5) EXCEPT (SELECT t6.id FROM b AS t6)) y WHERE y.id = b.id);

-- C9 IN instead of NOT IN -> 0 rows, correct (nothing is in an empty set).
CREATE TABLE b (id BIGINT);
INSERT INTO b VALUES (NULL),(1);
SELECT id FROM b WHERE id IN ((SELECT t5.id FROM b AS t5) EXCEPT (SELECT t6.id FROM b AS t6));

-- C10 EXCEPT ALL instead of EXCEPT -> 1 row on MySQL, STILL WRONG.
CREATE TABLE b (id BIGINT);
INSERT INTO b VALUES (NULL),(1);
SELECT id FROM b WHERE id NOT IN ((SELECT t5.id FROM b AS t5) EXCEPT ALL (SELECT t6.id FROM b AS t6));


-- =====================================================================================
-- PART 5 -- THE ORIGINAL FINDING, for provenance. Its WHERE clause is
--   t1.id NOT IN ((SELECT ... FROM t t5) EXCEPT (SELECT CAST(t6.id AS SIGNED) FROM t t6))
-- i.e. exactly PART 1's shape. BASE (insert order, NULL late) returns 7 groups; the EQUIVALENT
-- (rebuilt with ROW_NUMBER() OVER (ORDER BY id), which sorts NULL first) returns 1.
-- =====================================================================================
CREATE TABLE t (`id` BIGINT, `name` VARCHAR(255), `created_at` VARCHAR(255));
INSERT INTO t VALUES (-3, 'a', 'a');
INSERT INTO t VALUES (-1, '', '');
INSERT INTO t VALUES (0, 'dup', 'dup');
INSERT INTO t VALUES (1, 'dup', 'dup');
INSERT INTO t VALUES (2, NULL, NULL);
INSERT INTO t VALUES (2, 'zzz', 'zzz');
INSERT INTO t VALUES (NULL, 'b', 'b');
INSERT INTO t VALUES (7, 'é', 'é');
SELECT t1.id AS expr_0_number, COUNT(LEAST(t1.name, CASE WHEN 0 THEN t1.name ELSE t1.created_at END, REVERSE(t1.created_at) || (CASE WHEN GREATEST(1, 1) THEN UPPER(t1.name) ELSE SPACE(t1.id) END), CAST(t1.name AS CHAR(255)))) AS expr_1_number FROM t AS t1 WHERE t1.id NOT IN ((SELECT CASE WHEN CAST(NULL AS SIGNED) THEN t5.id ELSE t5.id END AS expr_0_number FROM t AS t5) EXCEPT (SELECT CAST(t6.id AS SIGNED) AS expr_0_number FROM t AS t6)) GROUP BY t1.id, if(t1.created_at IN (LPAD(t1.created_at, ASCII(COALESCE(t1.created_at, t1.created_at)), t1.name), REVERSE(t1.name), IFNULL(NULLIF(t1.name, t1.name), t1.name), RPAD(t1.created_at, NULLIF(t1.id - t1.id, LEAST(t1.id, t1.id)), t1.created_at), t1.created_at), CASE WHEN ((t1.created_at NOT IN (t1.name, t1.created_at, t1.name, t1.created_at, t1.name)) OR (t1.created_at IS NOT NULL)) IN (CAST(t1.id AS SIGNED) AND (t1.name IS NULL), t1.created_at IN (t1.created_at), t1.created_at REGEXP t1.name) THEN t1.created_at ELSE t1.name END, t1.created_at);
