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

-- DuckDB v2.0.0-alpha37826 (a9f869b6a7).
-- WindowSelfJoinOptimizer treats two OVER clauses as the same partition when
-- one list is a *set-subset* of the other and the *vector* lengths match.
-- Duplicate PARTITION BY keys (created_at, created_at) make that succeed
-- against a different key list of the same length (id, created_at), so both
-- aggregates are computed in one GROUP BY on the first window's keys.
--
-- CLI: duckdb
-- Mask: SET disabled_optimizers='window_self_join';

-- ========== distilled (round 832 shape) ==========
CREATE TABLE t(id BIGINT, name VARCHAR, created_at VARCHAR);
INSERT INTO t VALUES (NULL, 'a', ''), (2, 'Zed', '');

-- Expected: bit_or over created_at='' is bit_or(NULL, 2) = 2 for both rows.
-- Actual:   the NULL-id row gets bo=NULL (bit_or computed per (id, created_at)).
SELECT name, id,
       bit_and(id) OVER (PARTITION BY id, created_at) AS ba,
       bit_or(id) OVER (PARTITION BY created_at, created_at) AS bo
FROM t
ORDER BY name;
-- a    NULL  NULL  NULL     WRONG (bo should be 2)
-- Zed  2     2     2

-- Control 1: drop the duplicate key. PartitionsAreEquivalent is false (sizes 2 vs 1),
-- two Window ops remain, bit_or is correct.
SELECT name, id,
       bit_and(id) OVER (PARTITION BY id, created_at) AS ba,
       bit_or(id) OVER (PARTITION BY created_at) AS bo
FROM t
ORDER BY name;
-- a    NULL  NULL  2        expected
-- Zed  2     2     2

-- Control 2: same duplicate-key query with the rewrite disabled.
SET disabled_optimizers='window_self_join';
SELECT name, id,
       bit_and(id) OVER (PARTITION BY id, created_at) AS ba,
       bit_or(id) OVER (PARTITION BY created_at, created_at) AS bo
FROM t
ORDER BY name;
-- a    NULL  NULL  2        expected
RESET disabled_optimizers;

-- Control 3: EXPLAIN of the buggy spelling is one Hash Join + Hash Group By
-- with *both* bit_and and bit_or as aggregates on groups (id, created_at).
-- The good spelling (one created_at) is two stacked Window operators.
EXPLAIN SELECT bit_and(id) OVER (PARTITION BY id, created_at),
               bit_or(id) OVER (PARTITION BY created_at, created_at)
FROM t;

-- ========== same root, GREATEST/MAX (round 37) ==========
-- SELECT-list order matters: w_expr0 is the first window. Duplicate `id` in
-- the second partition list is a set-subset of (name IS NOT NULL, id, created_at).
DROP TABLE t;
CREATE TABLE t(id BIGINT, name VARCHAR, created_at VARCHAR);
INSERT INTO t VALUES (NULL, NULL, NULL), (NULL, 'o''brien', NULL);

SELECT name,
       MAX(TRUE) OVER (PARTITION BY (name IS NOT NULL), id, created_at) AS w1,
       GREATEST(MAX(name) OVER (PARTITION BY id, created_at, id), name) AS g
FROM t
ORDER BY name NULLS FIRST;
-- NULL     true  NULL       WRONG (g should be o'brien)
-- o'brien  true  o'brien

-- Control: no duplicate id → partitions not same length → not combined.
SELECT name,
       MAX(TRUE) OVER (PARTITION BY (name IS NOT NULL), id, created_at) AS w1,
       GREATEST(MAX(name) OVER (PARTITION BY id, created_at), name) AS g
FROM t
ORDER BY name NULLS FIRST;
-- NULL     true  o'brien    expected
-- o'brien  true  o'brien

-- ========== same root, bit_xor(12) (round 419) ==========
DROP TABLE t;
CREATE TABLE t(id BIGINT, name VARCHAR, created_at VARCHAR);
INSERT INTO t VALUES (NULL, NULL, NULL), (42, NULL, 'a');

SELECT name, created_at,
       MAX(id) OVER (PARTITION BY name, created_at) AS mx,
       bit_xor(12) OVER (PARTITION BY name, name) AS x
FROM t
ORDER BY created_at NULLS FIRST;
-- NULL  NULL  NULL  12      WRONG (two NULL names → 12 xor 12 = 0)
-- NULL  a     42    12

SELECT name, created_at,
       MAX(id) OVER (PARTITION BY name, created_at) AS mx,
       bit_xor(12) OVER (PARTITION BY name) AS x
FROM t
ORDER BY created_at NULLS FIRST;
-- NULL  NULL  NULL  0       expected
-- NULL  a     42    0

-- ========== same root, REPEAT(MEDIAN) (rich-shuffle2 rounds 83/383/406) ==========
-- bit_and is first in the expression tree, so the combined GROUP BY uses
-- (c_int, c_chr, c_dec). MEDIAN over (c_chr, c_dec, c_dec) is then computed per
-- (c_int, c_chr, c_dec): singleton median -7, REPEAT(..., -7) = ''.
-- True median of (Zed, 999.99) = {-7, 42, 42} is 42.
DROP TABLE t;
CREATE TABLE t(c_int BIGINT, c_chr VARCHAR, c_dec DECIMAL(10,2), c_big DECIMAL(38,0));
INSERT INTO t VALUES
  (NULL, NULL, NULL, NULL),
  (42, 'Zed', 999.99, -7),
  (-7, '', 12.34, 0),
  (-1, 'o''brien', -5.5, 1),
  (-7, 'Zed', 999.99, 0),
  (-1, 'Zed', NULL, 2),
  (2, '', NULL, 42),
  (42, 'Zed', 999.99, -7);

SELECT c_int, length(r) AS n
FROM (
  SELECT c_int, c_chr,
    REPEAT(CAST(LEAST(c_dec, bit_and(c_int) OVER (PARTITION BY c_int, c_chr, c_dec),
                      c_big, c_int, CAST(NULL AS BIGINT)) AS VARCHAR),
           CAST(MEDIAN(c_int) OVER (PARTITION BY c_chr, c_dec, c_dec) AS BIGINT)) AS r
  FROM t
) s
WHERE c_chr='Zed' AND c_int=-7;
-- -7  0     WRONG (should be 84 = REPEAT('-7', 42))

SET disabled_optimizers='window_self_join';
SELECT c_int, length(r) AS n
FROM (
  SELECT c_int, c_chr,
    REPEAT(CAST(LEAST(c_dec, bit_and(c_int) OVER (PARTITION BY c_int, c_chr, c_dec),
                      c_big, c_int, CAST(NULL AS BIGINT)) AS VARCHAR),
           CAST(MEDIAN(c_int) OVER (PARTITION BY c_chr, c_dec, c_dec) AS BIGINT)) AS r
  FROM t
) s
WHERE c_chr='Zed' AND c_int=-7;
-- -7  84    expected
RESET disabled_optimizers;
