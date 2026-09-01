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

-- TiDB v9.0.0-beta.2.pre @ 3bea8196a5.
-- REPEAT of a grouped VARCHAR over a WITH CTE returns a string; the same query
-- on the base table returns NULL. Observe via CAST AS CHAR(255) so the 16MiB+
-- REPEAT is never printed: IS NULL / CHAR_LENGTH is (1, NULL) vs (0, 255).
--
-- Mechanism: vectorized REPEAT (builtin_string_vec.go) NULLs when
--   len(str) > Flen/num   with Flen = MaxBlobWidth = 16777216
-- so REPEAT('abc', 5592406) is NULL (3*5592406 = 16777218). Scalar REPEAT
-- (builtin_string.go evalString) only checks max_allowed_packet (64MiB here)
-- and produces the string. @@max_allowed_packet is 67108864 — not the cap that
-- fires. sql_mode / collation are not load-bearing.
--
-- Each PART is a fresh database (both define t / t__base). TiDB has no CTAS:
-- table copy is CREATE LIKE + INSERT SELECT.

-- =====================================================================================
-- PART 1 -- CONCRETE: NotMaterializedCteQueryBuilder as emitted, compact seed.
-- HAVING keeps only the 'abc' group (MAX(id)=2 <= COUNT(*)=2; the '' group has
-- MAX(id)=42 and is dropped).
-- =====================================================================================
CREATE TABLE t__base (id BIGINT, name VARCHAR(255));
INSERT INTO t__base VALUES (2, 'abc'), (42, '');
CREATE VIEW t AS WITH t__base_cte_1 AS (SELECT * FROM t__base) SELECT * FROM t__base_cte_1;

SELECT name,
       CAST(IF(name = name, REPEAT(name, 5592406), REGEXP_INSTR(name, '.')) AS CHAR(255)) IS NULL AS is_null,
       CHAR_LENGTH(CAST(IF(name = name, REPEAT(name, 5592406), REGEXP_INSTR(name, '.')) AS CHAR(255))) AS clen
FROM t
GROUP BY name
HAVING MAX(id) <= (SELECT COUNT(*) FROM t);
-- Expected: ('abc', 1, NULL)   -- same as the table (PART 2)
-- Actual:   ('abc', 0, 255)    -- WRONG


-- =====================================================================================
-- PART 2 -- DISTILLED TABLE (correct). Same rows, no CTE.
-- =====================================================================================
CREATE TABLE t__base (id BIGINT, name VARCHAR(255));
INSERT INTO t__base VALUES (2, 'abc'), (42, '');
CREATE TABLE t LIKE t__base;
INSERT INTO t SELECT * FROM t__base;

SELECT name,
       CAST(IF(name = name, REPEAT(name, 5592406), REGEXP_INSTR(name, '.')) AS CHAR(255)) IS NULL AS is_null,
       CHAR_LENGTH(CAST(IF(name = name, REPEAT(name, 5592406), REGEXP_INSTR(name, '.')) AS CHAR(255))) AS clen
FROM t
GROUP BY name
HAVING MAX(id) <= (SELECT COUNT(*) FROM t);
-- Expected: ('abc', 1, NULL)
-- Actual:   ('abc', 1, NULL)


-- =====================================================================================
-- PART 3 -- query-level WITH (no VIEW). Also WRONG — the VIEW wrapper is not required.
-- =====================================================================================
CREATE TABLE t__base (id BIGINT, name VARCHAR(255));
INSERT INTO t__base VALUES (2, 'abc'), (42, '');

WITH t AS (SELECT * FROM t__base)
SELECT name,
       CAST(IF(name = name, REPEAT(name, 5592406), REGEXP_INSTR(name, '.')) AS CHAR(255)) IS NULL AS is_null,
       CHAR_LENGTH(CAST(IF(name = name, REPEAT(name, 5592406), REGEXP_INSTR(name, '.')) AS CHAR(255))) AS clen
FROM t
GROUP BY name
HAVING MAX(id) <= (SELECT COUNT(*) FROM t);
-- Expected: ('abc', 1, NULL)
-- Actual:   ('abc', 0, 255)


-- =====================================================================================
-- PART 4 -- CONTROLS (each removes one ingredient; table and CTE then agree).
-- Fresh database per control. CTE setup = PART 1's CREATE VIEW; table setup = PART 2.
-- =====================================================================================

-- (a) identity VIEW, no WITH:
-- CREATE VIEW t AS SELECT * FROM t__base;
-- => ('abc', 1, NULL) on the view, same as the table  ✓

-- (b) n = 5592405  (3*n = 16777215 <= MaxBlobWidth):
-- REPEAT(name, 5592405) in both IF THEN branches
-- => ('abc', 0, 255) on table and CTE  ✓

-- (c) ELSE NULL instead of REGEXP_INSTR(name, '.'):
-- => ('abc', 1, NULL) on table and CTE  ✓
-- SPACE(-1), SPACE(id), UPPER(name), etc. also agree (both NULL).
-- SPACE(REGEXP_INSTR(name, '.')) still diverges — REGEXP_INSTR on the grouping
-- column is the load-bearing ELSE.

-- (d) IF(TRUE, REPEAT(...), REGEXP_INSTR(...)):
-- ifFoldHandler folds IF away; both sides take vectorized REPEAT
-- => ('abc', 1, NULL) on table and CTE  ✓
-- name = name / name <=> name / name LIKE name / GREATEST(name,name) IS NOT NULL
-- all still diverge. TRUE / 1 do not.

-- (e) drop HAVING (both groups survive):
-- => ('abc', 1, NULL), ('', 0, 0) on table and CTE (order may differ)  ✓
-- HAVING MAX(id) <= (SELECT COUNT(*) FROM t) is load-bearing because it drops
-- the '' group so only 'abc' remains.

-- (f) one row (2, 'abc') only, with or without HAVING TRUE:
-- => ('abc', 1, NULL) on table and CTE  ✓  — the extra '' row is required.

-- (g) derived table FROM (SELECT * FROM t__base) t, no WITH:
-- => ('abc', 1, NULL)  ✓
