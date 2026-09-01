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

-- MySQL 9.7.2 (docker mysql:9.7.2). A mergeable VIEW whose column is a CASE/CAST expression,
-- queried with a table alias in GROUP BY / HAVING, silently drops groups for
--   HAVING MIN(t3.created_at) >= IFNULL(t3.created_at, t3.created_at)
-- (and the as-found LEAST(IFNULL(...), literal, col) spelling). The same predicate in WHERE,
-- the same HAVING without the table alias, ALGORITHM=TEMPTABLE, a CTAS of the CASE, and an
-- identity view are all correct. Projecting the HAVING expression in the SELECT list reports
-- TRUE for the dropped groups -- HAVING evaluates it differently from SELECT.
--
-- sql_mode: STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES (not load-bearing).
-- charset/collation: utf8mb4 / utf8mb4_0900_bin. SHOW COLUMNS / COLLATION() of the CASE view
-- match the base table (varchar(255) utf8mb4_0900_bin) -- not a type-equivalence artefact.
--
-- Sibling of mysql-20260812-view-merge-having-and-count-drops-group (HAVING (col AND COUNT(col)))
-- and mysql-20260814-021542-round0-having-max-view-expr-1054 (1054 on HAVING col). Different
-- HAVING shape, different symptom (dropped groups, not 1054).


-- =====================================================================================
-- PART 1 -- CONCRETE: compact rebuild of mismatch_round0_1.sql. The hunt's equivalent
-- ended as a JSON_OBJECT / JSON_EXTRACT unpack VIEW (expression-valued created_at). The
-- workload's load-bearing fragment is the aliased GROUP BY + HAVING MIN >= LEAST(IFNULL…).
-- Cross joins and the window in the SELECT list are not required.
-- =====================================================================================
CREATE TABLE b (c_pk BIGINT NOT NULL, id BIGINT, name VARCHAR(255), created_at VARCHAR(255));
INSERT INTO b VALUES (1, NULL, NULL, NULL);
INSERT INTO b VALUES (2, -1, 'abc', 'trailing ');
INSERT INTO b VALUES (3, 2, '', 'o''brien');
INSERT INTO b VALUES (4, -7, 'trailing ', '');
INSERT INTO b VALUES (5, -7, 'Zed', NULL);
INSERT INTO b VALUES (6, 1, 'Zed', 'trailing ');
INSERT INTO b VALUES (7, -7, 'abc', 'Zed');
INSERT INTO b VALUES (8, -1, 'abc', 'trailing ');

CREATE VIEW t AS
SELECT
  CASE WHEN JSON_EXTRACT(j, '$.c_pk') = CAST('null' AS JSON) THEN NULL
       ELSE CAST(JSON_EXTRACT(j, '$.c_pk') AS SIGNED) END AS c_pk,
  CASE WHEN JSON_EXTRACT(j, '$.id') = CAST('null' AS JSON) THEN NULL
       ELSE CAST(JSON_EXTRACT(j, '$.id') AS SIGNED) END AS id,
  CASE WHEN JSON_EXTRACT(j, '$.name') = CAST('null' AS JSON) THEN NULL
       ELSE CAST(JSON_UNQUOTE(JSON_EXTRACT(j, '$.name')) AS CHAR(255)) COLLATE utf8mb4_0900_bin END AS name,
  CASE WHEN JSON_EXTRACT(j, '$.created_at') = CAST('null' AS JSON) THEN NULL
       ELSE CAST(JSON_UNQUOTE(JSON_EXTRACT(j, '$.created_at')) AS CHAR(255)) COLLATE utf8mb4_0900_bin END AS created_at
FROM (SELECT JSON_OBJECT('c_pk', c_pk, 'id', id, 'name', name, 'created_at', created_at) AS j FROM b) AS eq_json;

SELECT created_at FROM t t3
GROUP BY t3.id, t3.created_at
HAVING MIN(t3.created_at) >= LEAST(IFNULL(t3.created_at, t3.created_at), 'x', t3.created_at);
-- Expected 5 rows: ('trailing '), ("o'brien"), (''), ('trailing '), ('Zed')
-- Actual   2 rows: ('trailing '), ('trailing ')
-- (as-found literal was '𒀀'; 'x' is enough)


-- =====================================================================================
-- PART 2 -- DISTILLED. JSON pack is not load-bearing; CASE WHEN TRUE is. LEAST is not
-- load-bearing; IFNULL is. The table alias in GROUP BY/HAVING is load-bearing.
-- =====================================================================================
CREATE TABLE b (id BIGINT, created_at VARCHAR(255));
INSERT INTO b VALUES (NULL, NULL);
INSERT INTO b VALUES (-1, 'trailing ');
INSERT INTO b VALUES (2, 'o''brien');
INSERT INTO b VALUES (-7, '');
INSERT INTO b VALUES (-7, NULL);
INSERT INTO b VALUES (1, 'trailing ');
INSERT INTO b VALUES (-7, 'Zed');
INSERT INTO b VALUES (-1, 'trailing ');

CREATE VIEW t AS
SELECT id, CASE WHEN TRUE THEN created_at ELSE CAST(NULL AS CHAR(255)) END AS created_at
FROM b;

SELECT created_at FROM t t3
GROUP BY t3.id, t3.created_at
HAVING MIN(t3.created_at) >= IFNULL(t3.created_at, t3.created_at);
-- Expected 5 rows: ('trailing '), ("o'brien"), (''), ('trailing '), ('Zed')
-- Actual   2 rows: ('trailing '), ('trailing ')
--
-- Why 5 is correct: every non-NULL group is a singleton created_at, so MIN(created_at)
-- equals created_at and IFNULL(created_at, created_at) is that same non-NULL value.
-- MIN >= IFNULL is TRUE. The two all-NULL groups are UNKNOWN and are dropped on both
-- sides. The CASE view wrongly also drops "o'brien", '', and 'Zed'.


-- =====================================================================================
-- PART 3 -- CONTROLS (each removes one ingredient; all return the correct 5 rows).
-- Re-create t per control in a fresh database.
-- =====================================================================================

-- (a) base table, same HAVING:
-- CREATE TABLE t AS SELECT * FROM b;
-- SELECT created_at FROM t t3 GROUP BY t3.id, t3.created_at
-- HAVING MIN(t3.created_at) >= IFNULL(t3.created_at, t3.created_at);
-- => 5 rows  ✓

-- (b) identity view (bare column, still a VIEW):
-- CREATE VIEW t AS SELECT * FROM b;
-- => 5 rows  ✓

-- (c) ALGORITHM=TEMPTABLE (blocks view merge):
-- CREATE ALGORITHM=TEMPTABLE VIEW t AS
--   SELECT id, CASE WHEN TRUE THEN created_at ELSE CAST(NULL AS CHAR(255)) END AS created_at FROM b;
-- => 5 rows  ✓

-- (d) materialize the CASE as a TABLE:
-- CREATE TABLE t AS
--   SELECT id, CASE WHEN TRUE THEN created_at ELSE CAST(NULL AS CHAR(255)) END AS created_at FROM b;
-- => 5 rows  ✓

-- (e) drop the table alias (FROM t, unprefixed GROUP BY / HAVING) over the CASE view:
-- SELECT created_at FROM t GROUP BY id, created_at
-- HAVING MIN(created_at) >= IFNULL(created_at, created_at);
-- => 5 rows  ✓
-- EXPLAIN FORMAT=TREE of this control keeps bare `created_at` inside IFNULL/LEAST;
-- the buggy aliased form expands the CASE into every IFNULL/LEAST operand.

-- (f) same predicate in WHERE instead of HAVING (CASE view, alias kept):
-- SELECT created_at FROM t t3
-- WHERE t3.created_at >= IFNULL(t3.created_at, t3.created_at);
-- => 6 rows (the non-NULL created_at rows, including the duplicate trailing)  ✓

-- (g) HAVING MIN(t3.created_at) >= t3.created_at  (no IFNULL) over the CASE view:
-- => 5 rows  ✓
-- IFNULL / COALESCE / IF(col IS NULL, col, col) / LEAST(col, 'x', col) all trigger;
-- comparing MIN to the bare column does not.

-- (h) SELECT the HAVING predicate (no HAVING filter) over the CASE view:
-- SELECT t3.created_at,
--        MIN(t3.created_at) >= IFNULL(t3.created_at, t3.created_at) AS ok
-- FROM t t3 GROUP BY t3.id, t3.created_at;
-- => ok = 1 for every non-NULL group, including the three HAVING drops.
-- HAVING of the identical expression disagrees with SELECT.

-- CAST(created_at AS CHAR(255)) AS created_at is a sibling trigger (drops a different
-- subset: 3 rows instead of 2, with the same aliased HAVING). Parentheses-only
-- `(created_at) AS created_at` does not trigger.
