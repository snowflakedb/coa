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

-- TiDB: numeric comparison against ENUM/SET loses ordinal semantics once the
-- column has passed through UNION ALL (which widens ENUM/SET to VARCHAR).
-- Predicate pushdown rewrites `e = 1` as
--   cast(cast(e AS varchar), double) = 1
-- so every label becomes 0.0 and `e = 1` matches nothing while `e = 0` matches
-- every non-NULL row. MySQL 9.7.2 pushes the bare `e = 1` into the ENUM branch
-- and keeps ordinal semantics.
--
-- Build      : tidb 8.0.11-TiDB-v9.0.0-beta.2.pre-2051-g3bea8196a5 @3bea8196
-- Session    : defaults / fuzzer STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES
-- Determinism: fully deterministic; data-independent shape (3 enum labels).
--
-- HOW TO RUN: each PART is independent; run in a fresh database.


-- =====================================================================================
-- PART 1 -- ABSOLUTE MINIMUM (ENUM): inline UNION ALL, no view required.
-- =====================================================================================
CREATE TABLE tb (e ENUM('a','b','c'));
INSERT INTO tb VALUES ('a'),('b'),('c');

SELECT e FROM tb WHERE e = 1;
-- Expected / MySQL / TiDB-base: 1 row ('a')

SELECT e FROM (
  SELECT e FROM tb
  UNION ALL
  SELECT e FROM tb WHERE FALSE
) AS x
WHERE e = 1;
-- Expected / MySQL: 1 row ('a')
-- Actual   / TiDB:  0 rows

SELECT e FROM (
  SELECT e FROM tb
  UNION ALL
  SELECT e FROM tb WHERE FALSE
) AS x
WHERE e = 0;
-- Expected / MySQL: 0 rows
-- Actual   / TiDB:  3 rows ('a'),('b'),('c')


-- =====================================================================================
-- PART 2 -- Same shape as a VIEW (DESCRIBE shows varchar; EXPLAIN shows the bad cast).
-- =====================================================================================
CREATE TABLE tb2 (e ENUM('a','b','c'));
INSERT INTO tb2 VALUES ('a'),('b'),('c');
CREATE VIEW v AS SELECT e FROM tb2 UNION ALL SELECT e FROM tb2 WHERE FALSE;

-- DESCRIBE v;  -- e varchar(1)   (MySQL also reports varchar(1))
SELECT e FROM v WHERE e = 1;  -- TiDB: 0 rows; MySQL: ('a')
SELECT e FROM v WHERE e = 0;  -- TiDB: 3 rows; MySQL: 0 rows

-- EXPLAIN SELECT e FROM v WHERE e = 1;
-- TiDB pushes:
--   eq(cast(cast(tb2.e, varchar(1) ...), double BINARY), 1)
-- MySQL pushes:
--   Filter: (tb2.e = 1)   -- native ENUM ordinal compare on the branch


-- =====================================================================================
-- PART 3 -- SET has the same defect (bitmap ordinal to varchar to double).
-- =====================================================================================
CREATE TABLE sb (s SET('x','y','z'));
INSERT INTO sb VALUES ('x'),('y'),('x,y');

SELECT s FROM sb WHERE s = 1;
-- base: ('x')

SELECT s FROM (
  SELECT s FROM sb
  UNION ALL
  SELECT s FROM sb WHERE FALSE
) AS x
WHERE s = 1;
-- TiDB: 0 rows; MySQL: ('x')

SELECT s FROM (
  SELECT s FROM sb
  UNION ALL
  SELECT s FROM sb WHERE FALSE
) AS x
WHERE s = 0;
-- TiDB: 3 rows; MySQL: 0 rows


-- =====================================================================================
-- PART 4 -- Controls (clean on TiDB).
-- =====================================================================================
-- (a) plain derived table (no UNION) keeps ENUM and ordinal compare:
-- SELECT e FROM (SELECT e FROM tb) AS d WHERE e = 1;   -- ('a')
--
-- (b) string compare is unaffected:
-- SELECT e FROM (SELECT e FROM tb UNION ALL SELECT e FROM tb WHERE FALSE) x
-- WHERE e = 'a';  -- ('a')
--
-- (c) e+0 / CAST(e AS UNSIGNED) after UNION also collapse to 0 on BOTH
--     TiDB and MySQL (projection-side varchar to double). The bug here is specifically
--     WHERE/predicate pushdown, where MySQL preserves ENUM ordinals and TiDB does not.
