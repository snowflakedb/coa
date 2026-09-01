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

-- MySQL 9.7.2 (docker mysql:9.7.2). A mergeable CAST(col AS CHAR(255)) COLLATE utf8mb4_0900_bin
-- view (or JSON_UNQUOTE unpack with the same CAST+COLLATE — what eqgen's JSON builder emits)
-- makes `col = UPPER(col)` TRUE for 'a' while UPPER(col) is the bytes of 'A' and `col = 'A'` is
-- FALSE. Heap / identity view / ALGORITHM=TEMPTABLE / derived_merge=off are correct (FALSE).
-- COLLATION(col) reports utf8mb4_0900_bin on both sides; HEX(col) is 61; HEX(UPPER(col)) is 41.
--
-- Covers mysql_20260816-061046 leftovers that are not the HAVING/SIGSEGV/regexp cluster:
--   mismatch_round199_1.sql  CAST(NULLIF((c_chr || …) LIKE '%a%', …) AS SIGNED) NULL vs 0
--   mismatch_round275_203.sql / 469_143 / 602_201  CAST(c_chr <= UPPER(c_chr) AS SIGNED) 0 vs 1
--   mismatch_round300_130.sql  COUNT OVER (PARTITION BY c_chr NOT IN (LOWER(c_chr))) 0 vs 3
--   mismatch_round35_94.sql    c_chr NOT IN (CASE … UPPER(c_chr) …) 0 vs 1 rows
--   mismatch_round191_115.sql  CAST CHAR orig still disagrees; col=UPPER(col) fires on same view
--
-- sql_mode: STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES (not load-bearing).
-- charset/collation: utf8mb4 / utf8mb4_0900_bin.


-- =====================================================================================
-- PART 1 -- CONCRETE: 199_1's JSON-unpack view (builder-emitted CAST+COLLATE) + the
-- BOOLEAN/CAST fragment that the oracle flagged. Expected expr = NULL (heap); actual 0.
-- =====================================================================================
CREATE TABLE b (
  c_txt VARCHAR(255) COLLATE utf8mb4_0900_bin,
  c_chr VARCHAR(255) COLLATE utf8mb4_0900_bin,
  c_dec DECIMAL(10,2),
  c_big BIGINT
);
INSERT INTO b VALUES ('Zed', 'a', -5.5, -7);

CREATE VIEW t AS SELECT
  CAST(JSON_UNQUOTE(JSON_EXTRACT(j, '$.c_txt')) AS CHAR(255)) COLLATE utf8mb4_0900_bin AS c_txt,
  CAST(JSON_UNQUOTE(JSON_EXTRACT(j, '$.c_chr')) AS CHAR(255)) COLLATE utf8mb4_0900_bin AS c_chr,
  CAST(JSON_EXTRACT(j, '$.c_dec') AS DECIMAL(10,2)) AS c_dec,
  CAST(JSON_EXTRACT(j, '$.c_big') AS SIGNED) AS c_big
FROM (SELECT JSON_OBJECT('c_txt', c_txt, 'c_chr', c_chr, 'c_dec', c_dec, 'c_big', c_big) AS j FROM b) x;

SELECT CAST(NULLIF(
         (t1.c_chr || IFNULL(t1.c_chr, t1.c_chr)) LIKE '%a%',
         t1.c_txt >= REPEAT(t1.c_txt, t1.c_dec - t1.c_big)
       ) AS SIGNED)
FROM t t1;
-- Expected 1 row (NULL). Actual 1 row (0).


-- =====================================================================================
-- PART 2 -- DISTILLED. JSON is not required. One letter, CAST+COLLATE mergeable view.
-- Expected: col = UPPER(col) is FALSE ('a' vs 'A' under utf8mb4_0900_bin).
-- Actual:   TRUE, even though UPPER(col) prints as 'A' and col = 'A' is FALSE.
-- =====================================================================================
CREATE TABLE b (c_chr VARCHAR(255) COLLATE utf8mb4_0900_bin);
INSERT INTO b VALUES ('a');
CREATE VIEW t AS
  SELECT CAST(c_chr AS CHAR(255)) COLLATE utf8mb4_0900_bin AS c_chr FROM b;

SELECT c_chr,
       HEX(c_chr),
       UPPER(c_chr),
       HEX(UPPER(c_chr)),
       c_chr = UPPER(c_chr) AS eq_upper,   -- Expected 0, actual 1
       c_chr = 'A' AS eq_lit,              -- 0 on both sides (control)
       c_chr <= UPPER(c_chr) AS le_upper,  -- Expected 0, actual 1
       COLLATION(c_chr)
FROM t;
-- Expected: ('a','61','A','41', 0, 0, 0, utf8mb4_0900_bin)
-- Actual:   ('a','61','A','41', 1, 0, 1, utf8mb4_0900_bin)


-- Inline derived table of the same CAST also diverges (no VIEW keyword):
SELECT c_chr = UPPER(c_chr) FROM (
  SELECT CAST(c_chr AS CHAR(255)) COLLATE utf8mb4_0900_bin AS c_chr FROM b
) t;
-- Expected 0, actual 1


-- =====================================================================================
-- CONTROLS. Each swaps one ingredient; all return eq_upper = 0.
-- =====================================================================================

-- C1 heap
CREATE TABLE t (c_chr VARCHAR(255) COLLATE utf8mb4_0900_bin);
INSERT INTO t VALUES ('a');
SELECT c_chr = UPPER(c_chr) FROM t;  -- 0

-- C2 identity view
CREATE TABLE b (c_chr VARCHAR(255) COLLATE utf8mb4_0900_bin);
INSERT INTO b VALUES ('a');
CREATE VIEW t AS SELECT * FROM b;
SELECT c_chr = UPPER(c_chr) FROM t;  -- 0

-- C3 ALGORITHM=TEMPTABLE
CREATE TABLE b (c_chr VARCHAR(255) COLLATE utf8mb4_0900_bin);
INSERT INTO b VALUES ('a');
CREATE ALGORITHM=TEMPTABLE VIEW t AS
  SELECT CAST(c_chr AS CHAR(255)) COLLATE utf8mb4_0900_bin AS c_chr FROM b;
SELECT c_chr = UPPER(c_chr) FROM t;  -- 0

-- C4 derived_merge=off
SET SESSION optimizer_switch='derived_merge=off';
CREATE TABLE b (c_chr VARCHAR(255) COLLATE utf8mb4_0900_bin);
INSERT INTO b VALUES ('a');
CREATE VIEW t AS
  SELECT CAST(c_chr AS CHAR(255)) COLLATE utf8mb4_0900_bin AS c_chr FROM b;
SELECT c_chr = UPPER(c_chr) FROM t;  -- 0

-- C5 tautology CASE view (expression column, but not CAST+COLLATE) — CORRECT
CREATE TABLE b (c_chr VARCHAR(255) COLLATE utf8mb4_0900_bin);
INSERT INTO b VALUES ('a');
CREATE VIEW t AS
  SELECT CASE WHEN TRUE THEN c_chr ELSE CAST(NULL AS CHAR(255)) END COLLATE utf8mb4_0900_bin AS c_chr FROM b;
SELECT c_chr = UPPER(c_chr) FROM t;  -- 0

-- C6 WHERE c_chr = 'A' on the crashing CAST view — CORRECT 0 rows (literal compare is fine)
CREATE TABLE b (c_chr VARCHAR(255) COLLATE utf8mb4_0900_bin);
INSERT INTO b VALUES ('a');
CREATE VIEW t AS
  SELECT CAST(c_chr AS CHAR(255)) COLLATE utf8mb4_0900_bin AS c_chr FROM b;
SELECT * FROM t WHERE c_chr = 'A';  -- 0 rows
