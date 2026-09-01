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

-- MISMATCH (wrong result: the view-backed query returns fewer/zero rows)
-- engine=mariadb 13.1.0-MariaDB-debug @cded2b25 (assertions on, mariadb-main/bin)
-- source revision: cded2b25e65853a75c2213cfe0832819832708bd
-- Covers findings: mismatch_round45_0.sql (seed=1400813873, `< ANY`) and
--                  mismatch_round38_0.sql (seed=333914030, `>= ALL`) -- same root cause.
-- sql_mode/charset/collation: harness defaults (utf8mb4 / utf8mb4_nopad_bin); immaterial.
--
-- A window function combined with a quantified subquery predicate (col < ANY / >= ALL (subquery))
-- in the WHERE returns WRONG (missing) rows when the relation is a mergeable VIEW instead of the
-- base table. base t and the view are row-identical; the query is permutation-invariant on the base
-- (so this is not order-sensitivity). Reduced by execution-guided delta-debugging against the engine.
--
-- Necessary ingredients (each verified by a control):
--   * an AGGREGATE window: AVG/SUM/MAX ... OVER ()  -- COUNT()/ROW_NUMBER() OVER () do NOT trigger; no window does NOT trigger
--   * a quantified subquery predicate: col {< ANY | > ANY | >= ALL | <= ALL} (SELECT ...)
--       -- the scalar-equivalent col < (SELECT MAX(...)), and IN / = ANY, do NOT trigger
--       -- NOT IN (subquery) DOES trigger (same missing-row symptom; anti-semijoin path)
--       -- (col IN (SELECT …)) <= (col2 IN (SELECT …)) DOES trigger; plain col IN (SELECT col) does NOT
--   * the relation referenced as a mergeable VIEW (not the base table)
--   * at least 2 rows
--
-- Upstream: MDEV-40557 (this bug). Eqgen mariadb_20260816-061046 adds NOT IN and (IN)<=(IN).

CREATE TABLE b (id BIGINT);
INSERT INTO b VALUES (0),(1);
CREATE VIEW t AS SELECT id FROM b;

-- Expected 1 row, actual 1 row (correct -- base table):
SELECT AVG(id) OVER () FROM b WHERE id < ANY (SELECT id FROM b);

-- Expected 1 row, actual 0 rows (WRONG -- same query over the view):
SELECT AVG(id) OVER () FROM t WHERE id < ANY (SELECT id FROM t);

-- The `>= ALL` form (round38) is the same bug:
SELECT AVG(id) OVER () FROM t WHERE id >= ALL (SELECT id FROM t);   -- WRONG: 0 rows (base: 1 row)

-- NOT IN is the same bug (`IN` is not). Heap returns 1 row; view returns 0:
SELECT AVG(id) OVER () FROM t WHERE id NOT IN (SELECT id+1 FROM t);

-- Comparing two IN-subquery booleans is the same bug (`col IN (SELECT col)` alone is not).
-- Heap returns 2 rows; view returns 0. Confirmed on mariadb:11.4.12 / eqgen mismatch_round1003_1.sql.
CREATE TABLE b2 (id BIGINT, d DECIMAL(10,2));
INSERT INTO b2 VALUES (1, 1.00), (2, 2.00);
CREATE VIEW t2 AS SELECT * FROM b2;
SELECT AVG(id) OVER () FROM t2
WHERE (id IN (SELECT id FROM t2)) <= (d IN (SELECT id FROM t2));
