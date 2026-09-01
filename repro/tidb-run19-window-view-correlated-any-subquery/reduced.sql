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

-- TiDB: a view containing an AGGREGATE WINDOW FUNCTION (`MAX(col) OVER (PARTITION BY …)`) makes a
-- correlated quantified subquery (`< ANY` / `!= ANY` / `> ALL` / `IN (SELECT …)`) mis-evaluate --
-- the correlated predicate stops filtering. Silent wrong results.
--
-- Build      : tidb 8.0.11-TiDB-v9.0.0-beta.2.pre-2051-g3bea8196a5 @3bea8196
--              (assertions off, unistore)
-- Session    : sql_mode=STRICT_ALL_TABLES,NO_ZERO_DATE,NO_ZERO_IN_DATE,
--              ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION,ONLY_FULL_GROUP_BY,
--              NO_BACKSLASH_ESCAPES; charset utf8mb4; collation utf8mb4_0900_bin.
--              None of it is load-bearing.
-- Determinism: deterministic. 2 rows suffice.
-- Origin     : 5 findings, all one relation shape --
--                hunt log  (seed 2132979842)
--                hunt log  (seed 806554086)
--                hunt log  (seed 1332445482)
--                hunt log  (seed 1566574447)
--                hunt log  (seed 560221390)
--
-- TRIGGER (all three required):
--   1. the relation is a VIEW whose body computes an AGGREGATE window function
--      (`MAX(col) OVER (PARTITION BY k)`). `GROUP BY k` + `MAX(col)` -- same semantics, same
--      rows -- is CLEAN. A RANKING window function (`ROW_NUMBER() OVER (ORDER BY id)`) is CLEAN.
--      `DISTINCT` is NOT needed. A plain table, a trivial view, a derived-table view, a
--      `DISTINCT` view and an inline derived table are all CLEAN.
--   2. an OUTER JOIN of that relation to itself
--   3. a QUANTIFIED comparison subquery (`< ANY`, `= ALL`, `IN (SELECT …)`) whose WHERE
--      correlates on a column of the outer join. `EXISTS` is CLEAN. A correlated SCALAR
--      subquery is CLEAN.
--
-- HOW TO RUN: every PART is independent, AND every control inside PART 3 is independent -- each
--   one redefines `t`, so give each its own fresh database. Running PART 3 top-to-bottom in one
--   session fails with "Table 't' already exists" and silently answers later controls against
--   the FIRST control's relation.


-- =====================================================================================
-- PART 1 -- CONCRETE: the finding as eqgen produced it (round 1795). The equivalence chain
--   is five links -- ROW_NUMBER keying, duplicate-100x twice, and a reduce-by-ANY_VALUE view --
--   but only the LAST link matters, and only because it is a window function in a view.
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
CREATE TABLE t__base_table_4 (`id` BIGINT, `name` VARCHAR(255), `created_at` VARCHAR(255), `eq_key_1` BIGINT);
INSERT INTO t__base_table_4 (`id`, `name`, `created_at`, `eq_key_1`) SELECT id, name, created_at, ROW_NUMBER() OVER (ORDER BY id) AS eq_key_1 FROM t__base;
CREATE VIEW t AS SELECT id, name, created_at FROM (SELECT DISTINCT eq_key_1, MAX(id) OVER (PARTITION BY eq_key_1) AS id, MAX(name) OVER (PARTITION BY eq_key_1) AS name, MAX(created_at) OVER (PARTITION BY eq_key_1) AS created_at FROM t__base_table_4) AS eq_reduced;

-- `t` is row-identical to `t__base` (admissibility verified by replay.py for all 5 findings):
SELECT COUNT(*) AS n FROM t;                                 -- Expected 8, actual 8

-- the workload query. The only join group passing `t3.id IS NULL` has t2 null-padded, so the
-- left operand of `< ANY` is 1 and `1 < 1` is false: the correct answer is ZERO rows, which is
-- what the base table returns.
SELECT t2.id AS expr_0_number FROM t AS t1 RIGHT OUTER JOIN t AS t2 ON t1.name = t2.created_at RIGHT OUTER JOIN t AS t3 ON t2.id >= t3.id WHERE if(CAST('2014-04-04T10:10:10.10' AS CHAR(255)) IS NULL, (CAST(t2.id AS SIGNED) BETWEEN t2.id AND (CEIL(t2.id) + (t2.id + t2.id))), (CASE WHEN t2.id IS NULL THEN '2016-04-04 10:10:10.100000' IS NOT NULL ELSE 'HOUR' LIKE '__x__%' END) AND 1) < ANY (SELECT DISTINCT IFNULL(1, 0) AND (1 OR 0) AS expr_0_boolean FROM t AS t4 LEFT OUTER JOIN t AS t5 ON t4.name >= t5.name INNER JOIN t AS t6 ON t4.created_at = t6.name WHERE t3.id IS NULL) GROUP BY t2.id;
-- Expected 0 rows, actual 6 rows: (-3), (-1), (0), (1), (2), (7)


-- =====================================================================================
-- PART 2 -- DISTILLED minimal repro. Two rows, one view, one self outer join, one
--   quantified subquery. No `if`/`CASE`/`IFNULL`, no DISTINCT, no third join, no chain.
-- =====================================================================================
CREATE TABLE k (`id` BIGINT, `name` VARCHAR(255), `created_at` VARCHAR(255), `eq_key_1` BIGINT);
INSERT INTO k (id, name, created_at, eq_key_1) VALUES (1, 'a', 'a', 1);
INSERT INTO k (id, name, created_at, eq_key_1) VALUES (NULL, 'b', 'b', 2);
CREATE VIEW t AS SELECT id, name, created_at FROM (SELECT eq_key_1, MAX(id) OVER (PARTITION BY eq_key_1) AS id, MAX(name) OVER (PARTITION BY eq_key_1) AS name, MAX(created_at) OVER (PARTITION BY eq_key_1) AS created_at FROM k) AS w;

SELECT t2.id FROM t AS t2 RIGHT OUTER JOIN t AS t3 ON t2.id >= t3.id
WHERE (t2.id IS NULL) < ANY (SELECT 1 FROM t AS t4 WHERE t3.id IS NULL) GROUP BY t2.id;
-- Expected 0 rows, actual 1 row: (1)
--
-- Why 0 is correct: t3 is preserved by the RIGHT JOIN. For t3.id = 1 the subquery
-- (`WHERE t3.id IS NULL`) is EMPTY, and `x < ANY (empty)` is FALSE -- so no row with
-- t3.id = 1 may survive. For t3.id = NULL the subquery yields {1} but t2 is null-padded,
-- so the left operand is `(NULL IS NULL)` = 1 and `1 < 1` is false. Nothing survives.
-- Returning (1) means the correlated `t3.id IS NULL` did not filter the subquery.


-- =====================================================================================
-- PART 3 -- CONTROLS. Each changes exactly ONE thing from PART 2 and returns 0 rows.
-- =====================================================================================

-- C1  RELATION: plain table, same rows -> correct.
CREATE TABLE t (`id` BIGINT, `name` VARCHAR(255), `created_at` VARCHAR(255));
INSERT INTO t VALUES (1, 'a', 'a');
INSERT INTO t VALUES (NULL, 'b', 'b');
SELECT t2.id FROM t AS t2 RIGHT OUTER JOIN t AS t3 ON t2.id >= t3.id WHERE (t2.id IS NULL) < ANY (SELECT 1 FROM t AS t4 WHERE t3.id IS NULL) GROUP BY t2.id;   -- Expected 0, actual 0

-- C2  RELATION: `GROUP BY k` + `MAX(col)` aggregate instead of the window function.
--     Identical semantics, identical rows -> correct. This is the decisive control:
--     the window function, not the view and not the MAX, is what breaks it.
CREATE TABLE k (`id` BIGINT, `name` VARCHAR(255), `created_at` VARCHAR(255), `eq_key_1` BIGINT);
INSERT INTO k (id, name, created_at, eq_key_1) VALUES (1, 'a', 'a', 1);
INSERT INTO k (id, name, created_at, eq_key_1) VALUES (NULL, 'b', 'b', 2);
CREATE VIEW t AS SELECT id, name, created_at FROM (SELECT eq_key_1, MAX(id) AS id, MAX(name) AS name, MAX(created_at) AS created_at FROM k GROUP BY eq_key_1) AS w;
SELECT t2.id FROM t AS t2 RIGHT OUTER JOIN t AS t3 ON t2.id >= t3.id WHERE (t2.id IS NULL) < ANY (SELECT 1 FROM t AS t4 WHERE t3.id IS NULL) GROUP BY t2.id;   -- Expected 0, actual 0

-- C3  RELATION: a RANKING window function instead of an aggregate one -> correct.
CREATE TABLE k (`id` BIGINT, `name` VARCHAR(255), `created_at` VARCHAR(255), `eq_key_1` BIGINT);
INSERT INTO k (id, name, created_at, eq_key_1) VALUES (1, 'a', 'a', 1);
INSERT INTO k (id, name, created_at, eq_key_1) VALUES (NULL, 'b', 'b', 2);
CREATE VIEW t AS SELECT id, name, created_at FROM (SELECT id, name, created_at, ROW_NUMBER() OVER (ORDER BY id) AS rn FROM k) AS w;
SELECT t2.id FROM t AS t2 RIGHT OUTER JOIN t AS t3 ON t2.id >= t3.id WHERE (t2.id IS NULL) < ANY (SELECT 1 FROM t AS t4 WHERE t3.id IS NULL) GROUP BY t2.id;   -- Expected 0, actual 0

-- C4..C7 all keep the PART 2 window view and change only the QUERY.
CREATE TABLE k (`id` BIGINT, `name` VARCHAR(255), `created_at` VARCHAR(255), `eq_key_1` BIGINT);
INSERT INTO k (id, name, created_at, eq_key_1) VALUES (1, 'a', 'a', 1);
INSERT INTO k (id, name, created_at, eq_key_1) VALUES (NULL, 'b', 'b', 2);
CREATE VIEW t AS SELECT id, name, created_at FROM (SELECT eq_key_1, MAX(id) OVER (PARTITION BY eq_key_1) AS id, MAX(name) OVER (PARTITION BY eq_key_1) AS name, MAX(created_at) OVER (PARTITION BY eq_key_1) AS created_at FROM k) AS w;

-- C4  SELECT the correlated column in the outer query -> correct. A load-bearing "cosmetic"
--     token: adding `t3.id` to the select list makes the same filter work.
SELECT t2.id, t3.id FROM t AS t2 RIGHT OUTER JOIN t AS t3 ON t2.id >= t3.id WHERE (t2.id IS NULL) < ANY (SELECT 1 FROM t AS t4 WHERE t3.id IS NULL) GROUP BY t2.id, t3.id;   -- Expected 0, actual 0
-- C5  EXISTS instead of the quantified comparison -> correct.
SELECT t2.id FROM t AS t2 RIGHT OUTER JOIN t AS t3 ON t2.id >= t3.id WHERE (t2.id IS NULL) < 1 AND EXISTS (SELECT 1 FROM t AS t4 WHERE t3.id IS NULL) GROUP BY t2.id;   -- Expected 0, actual 0
-- C6  INNER JOIN instead of RIGHT OUTER JOIN -> correct.
SELECT t2.id FROM t AS t2 INNER JOIN t AS t3 ON t2.id >= t3.id WHERE (t2.id IS NULL) < ANY (SELECT 1 FROM t AS t4 WHERE t3.id IS NULL) GROUP BY t2.id;   -- Expected 0, actual 0
-- C7  a correlated SCALAR subquery over the same window view -> correct on both shapes.
SELECT t3.id, (SELECT COUNT(*) FROM t AS t4 WHERE t3.id IS NULL) AS n FROM t AS t3;   -- same on plain table and window view


-- =====================================================================================
-- PART 4 -- THE OTHER FOUR FINDINGS, each with its whole chain replaced by the single
--   PART 2 window view. All four reproduce, which is what makes them one root cause.
--   Row-identity of the one-view relation vs a plain table is verified for each.
--   (8 rows here, because these queries need the wider base data.)
-- =====================================================================================
CREATE TABLE k (`id` BIGINT, `name` VARCHAR(255), `created_at` VARCHAR(255), `eq_key_1` BIGINT);
INSERT INTO k (id, name, created_at, eq_key_1) VALUES (-3, 'a', 'a', 1);
INSERT INTO k (id, name, created_at, eq_key_1) VALUES (-1, '', '', 2);
INSERT INTO k (id, name, created_at, eq_key_1) VALUES (0, 'dup', 'dup', 3);
INSERT INTO k (id, name, created_at, eq_key_1) VALUES (1, 'dup', 'dup', 4);
INSERT INTO k (id, name, created_at, eq_key_1) VALUES (2, NULL, NULL, 5);
INSERT INTO k (id, name, created_at, eq_key_1) VALUES (2, 'zzz', 'zzz', 6);
INSERT INTO k (id, name, created_at, eq_key_1) VALUES (NULL, 'b', 'b', 7);
INSERT INTO k (id, name, created_at, eq_key_1) VALUES (7, 'é', 'é', 8);
CREATE VIEW t AS SELECT id, name, created_at FROM (SELECT eq_key_1, MAX(id) OVER (PARTITION BY eq_key_1) AS id, MAX(name) OVER (PARTITION BY eq_key_1) AS name, MAX(created_at) OVER (PARTITION BY eq_key_1) AS created_at FROM k) AS w;

-- round 2990 -- correlated `t3.created_at NOT IN (…)` inside `1 != ANY (…)`
SELECT (SELECT DISTINCT MAX(t5.id) AS expr_0_number FROM t AS t5 WHERE NULLIF('𒀀' LIKE '__x__%', 1 AND 1)) AS expr_0_number, MIN(t2.id) AS expr_1_number, t2.created_at AS expr_2_varchar FROM t AS t1 LEFT OUTER JOIN t AS t2 ON t1.id = t2.id LEFT OUTER JOIN t AS t3 ON t1.created_at != t3.name WHERE 1 != ANY (SELECT CAST('0' * '11' AS SIGNED) AS expr_0_boolean FROM t AS t4 WHERE t3.created_at NOT IN (REPEAT(t4.created_at, '12')) GROUP BY t4.name, t4.created_at) GROUP BY t3.name, t2.created_at, t3.id;
-- Expected 36 rows (what the plain table returns), actual 0

-- round 3188 -- correlated `t2.name LIKE '%_%'` inside `… IN (SELECT DISTINCT …)`
SELECT DISTINCT CAST(NULL AS SIGNED) AS expr_0_boolean, (SELECT MAX(IFNULL('2016-04-04 10:16:10.100000', '2016-04-04 10:16:10.100000')) AS expr_0_timestamp FROM t AS t8 RIGHT OUTER JOIN t AS t9 ON t8.created_at = t9.name RIGHT OUTER JOIN t AS t10 ON t9.name = t10.created_at WHERE CAST('2016-10-10' AS DATE) < NULLIF('2016-12-10', CAST(NULL AS DATE))) AS expr_1_timestamp FROM t AS t1 CROSS JOIN t AS t2 RIGHT OUTER JOIN t AS t3 ON t1.name < t3.created_at WHERE (CASE WHEN IFNULL(CAST('-12345678' AS SIGNED), 0 AND 0) AND (('HOUR' || '𒀀') != IFNULL(CAST(NULL AS CHAR(255)), '©')) THEN least(CAST('2016-10-10' AS DATE), CAST(CAST('2016-04-04 10:16:10.100000' AS DATE) AS DATE), least(CAST('2016-10-10' AS DATE), GREATEST('2016-12-10', '2016-10-10')), GREATEST('2016-10-10', greatest('2016-12-10', '2016-04-10')), NULLIF(CAST('2016-12-10' AS DATE), '2016-04-10'), CAST('2016-12-10' AS DATE), '2016-10-10', CAST('2016-04-04 10:10:10.100000' AS DATE)) ELSE CAST(CAST(CAST('2016-04-04 10:16:10.100000' AS DATE) AS DATE) AS DATE) END) IN (SELECT DISTINCT CAST(NULLIF('2016-04-04 10:16:10.100000', '2016-04-10') AS DATE) AS expr_0_date FROM t AS t7 WHERE t2.name LIKE '%_%');
-- Expected 1 row (NULL, '2016-04-04 10:16:10.100000'), actual 0

-- round 3621 -- correlated `t1.created_at IS NOT NULL` inside `t1.id > ALL (…)`
SELECT '2016-10-10' AS expr_0_date, MIN(t1.id) AS expr_1_number, REGEXP_REPLACE((SELECT DISTINCT MIN(t6.name) AS expr_0_varchar FROM t AS t6 WHERE 1 AND 1), '[0-9]+', CONCAT_WS(',', CAST(t1.id AS CHAR(255)), LEAST(CAST(t3.id != t3.id AS CHAR(255)), CAST(1 OR 0 AS CHAR(255)), MAX(t3.name), '𒀀', '©', t1.name, t1.name, t1.name, t1.name), CASE WHEN 1 THEN t1.name ELSE t1.name END, LTRIM(COALESCE(GREATEST(t1.name, t1.name), t1.name)))) AS expr_2_varchar, t1.id AS expr_3_number, CAST(t1.name AS CHAR(255)) AS expr_4_varchar FROM t AS t1 INNER JOIN t AS t2 ON t1.name > t2.created_at LEFT OUTER JOIN t AS t3 ON t2.created_at <= t3.name WHERE (t1.id > ALL (SELECT DISTINCT t5.id AS expr_0_number FROM t AS t5 WHERE t1.created_at IS NOT NULL GROUP BY t5.id)) IN (LEAST(1, (GREATEST(t3.id, t3.id) BETWEEN ABS(CAST(t3.id AS SIGNED)) AND CAST(t3.id AS SIGNED)), ('12:30:10.123456' <=> IFNULL(CAST('2016-04-04 10:16:10.100000' AS TIME(6)), CAST('2016-04-04 10:16:10.100000' AS TIME(6)))), least(t3.id, t1.id) IN (t3.id), CAST(t3.id + '12' AS SIGNED), ((0 OR 0) OR COALESCE(1, CAST(NULL AS SIGNED))) AND CAST(t1.id AS SIGNED))) GROUP BY t1.name, t3.id, t1.id;
-- Expected 30 rows, actual 0

-- round 3654 -- correlated `'0' <> t3.id` inside `… < ANY (…)`, wrapped in `= ALL (…)`.
--   Here the filter fails to REMOVE rows: COUNT(*) comes back 240 (the unfiltered join
--   cardinality) instead of 72. Same signature, opposite direction.
SELECT COALESCE(1, CASE WHEN 1 THEN ((IFNULL('0', '11') * LEAST('11', '3')) <=> '-12345678') END, 0, (CASE WHEN COALESCE(CAST(NULL AS SIGNED), MAX(1)) THEN MONTHNAME(CAST('2016-05-04T10:10:10.10' AS CHAR(255))) ELSE greatest(SUBSTRING('MONTH', '12'), SUBSTR('MONTH', CAST(NULL AS SIGNED))) END) IN (SHA2(LOWER(COALESCE('©', '')), '224'), MIN(SHA2(t1.created_at, '512')), if(CAST('2014-04-04T10:10:10.10' AS CHAR(255)) IS NULL, IFNULL(MIN(t3.name), RTRIM('')), NULLIF(CASE WHEN 0 THEN '©' ELSE 'MONTH' END, '©')), 'HOUR', REPEAT('𒀀', '15')), CASE WHEN CAST(COALESCE(1, 1) AS SIGNED) NOT IN (MAX(t1.id)) THEN MAX((NOT (0 AND 1))) ELSE CAST('𒀀' || '©' AS CHAR(255)) >= SHA2(MIN(''), '384') END, (SELECT DISTINCT MAX(1) AS expr_0_boolean FROM t AS t7 WHERE 0 < 0), (SELECT MIN(0) AS expr_0_boolean FROM t AS t8)) AS expr_0_boolean, (SELECT MAX(UPPER(t11.created_at)) AS expr_0_varchar FROM t AS t9 CROSS JOIN t AS t10 RIGHT OUTER JOIN t AS t11 ON t10.id = t11.id WHERE CAST(CASE WHEN 0 THEN '-12345678' END AS SIGNED)) AS expr_1_varchar, (SELECT MIN(t12.created_at) AS expr_0_varchar FROM t AS t12 WHERE (0 >= 1) AND CAST('15' AS SIGNED)) AS expr_2_varchar, COUNT(*) AS expr_3_number, NULLIF(greatest(TRIM(IFNULL(LEAST('𒀀', 'YEAR'), CASE WHEN 0 THEN 'YEAR' WHEN 0 THEN 'HOUR' WHEN 0 THEN '©' END)), MAX(t2.created_at), 'MONTH', '©', REGEXP_SUBSTR(REPEAT(MAX(t3.name), ABS('0')), '.*'), MAX(COALESCE(REVERSE(t3.created_at), t2.name)), CONCAT_WS(',', 'YEAR', 'YEAR'), 'MONTH', 'YEAR'), CONCAT(if(CEIL('12') IS NOT NULL, greatest(NULLIF('𒀀', ''), LEAST('YEAR', '𒀀')), LEAST(REVERSE('©'), CASE WHEN 1 THEN 'HOUR' END)), MAX(t2.name), SUBSTRING('𒀀', IFNULL(CAST(0 AS SIGNED), '3')))) AS expr_4_text FROM t AS t1 CROSS JOIN t AS t2 LEFT OUTER JOIN t AS t3 ON t1.created_at <= t3.name WHERE (RIGHT(REGEXP_SUBSTR(CAST(NULL AS CHAR(255)), '[0-9]+'), ROUND(REGEXP_INSTR('HOUR', '^[A-Za-z]+$'))) < ANY (SELECT DISTINCT SHA2('©', '256') AS expr_0_text FROM t AS t5 WHERE '0' <> t3.id)) = ALL (SELECT '2016-04-04 10:10:10.100000' IS NULL AS expr_0_boolean FROM t AS t6);
-- Expected COUNT(*) = 72, actual 240
