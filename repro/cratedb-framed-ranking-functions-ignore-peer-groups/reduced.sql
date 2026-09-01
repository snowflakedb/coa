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

-- CrateDB: RANK() / DENSE_RANK() compute their value relative to the WINDOW FRAME instead of
-- the ORDER BY peer group in the partition. Any explicit frame gives a wrong answer.
--
-- Build      : CrateDB 6.4.1 (release tarball). Identical with assertions ON and OFF
--              ('-da -dsa'), so a stock production node returns these wrong values.
-- Severity   : WRONG ANSWER, silent. No error, no plan-shape dependence, no oracle needed --
--              three rows and one query show it.
-- Session    : all defaults. No setting is load-bearing.
-- Determinism: deterministic.
-- Provenance : NOT an eqgen finding. Found 2026-08-03 as a control probe while triaging
--              logs/cratedb_run3/mismatch_round14_0.sql (which does contain a framed
--              DENSE_RANK -- but frame-stripping does NOT change that finding's divergence,
--              so this bug is NOT its cause and round14 remains separately untriaged).
--
-- THE RULE: per SQL:2011 a ranking function's value is fixed by the row's ORDER BY peer group
-- within the PARTITION and is frame-independent. CrateDB instead derives it from the frame:
--   * frame covering the whole partition (UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING,
--     ROWS *or* RANGE)          -> every row returns 1
--   * frame ending at CURRENT ROW -> degenerates to ROW_NUMBER (1,2,3)
--   * no frame at all             -> CORRECT
-- It is NOT a ties-only problem: K14 has three distinct ORDER BY values and still returns
-- 1,1,1 where 1,2,3 is correct.
--
-- HOW TO RUN: each block is independent; CrateDB needs REFRESH between write and read.

-- =====================================================================================
-- PART 1 -- MINIMAL REPRO. Two queries, one table, three rows.
-- =====================================================================================
CREATE TABLE v (x TEXT, g TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO v VALUES ('a', 'p');
INSERT INTO v VALUES ('a', 'p');
INSERT INTO v VALUES ('b', 'p');
REFRESH TABLE v;
-- Expected 1,1,3 -- actual 1,1,1
SELECT RANK() OVER (ORDER BY x ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) r FROM v;
-- Expected 1,1,2 -- actual 1,1,1
SELECT DENSE_RANK() OVER (ORDER BY x ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) r FROM v;
-- Expected 1,1,3 -- actual 1,1,3  (the same query with the frame deleted is CORRECT)
SELECT RANK() OVER (ORDER BY x) r FROM v;
-- Expected 1,1,2 -- actual 1,1,2  (correct)
SELECT DENSE_RANK() OVER (ORDER BY x) r FROM v;

-- =====================================================================================
-- PART 2 -- THE STRONGEST CASE: NO TIES AT ALL. Three distinct ORDER BY values, so peer
--           groups are irrelevant and the correct answer is unambiguously 1,2,3.
-- Expected 1,2,3 -- actual 1,1,1
-- =====================================================================================
CREATE TABLE v (x TEXT, g TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO v VALUES ('a', 'p');
INSERT INTO v VALUES ('b', 'p');
INSERT INTO v VALUES ('c', 'p');
REFRESH TABLE v;
SELECT RANK() OVER (ORDER BY x ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) r FROM v;

-- =====================================================================================
-- PART 3 -- FRAME-SHAPE MATRIX. The wrong value depends on the frame, which is the tell:
--           the frame is being used to compute the rank.
-- =====================================================================================
CREATE TABLE v (x TEXT, g TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO v VALUES ('a', 'p');
INSERT INTO v VALUES ('a', 'p');
INSERT INTO v VALUES ('b', 'p');
REFRESH TABLE v;
-- RANGE full frame is ALSO wrong, so this is not a ROWS-vs-RANGE issue.
-- Expected 1,1,3 -- actual 1,1,1
SELECT RANK() OVER (ORDER BY x RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) r FROM v;
-- Expected 1,1,2 -- actual 1,1,1
SELECT DENSE_RANK() OVER (ORDER BY x RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) r FROM v;
-- A frame ending at CURRENT ROW degenerates into ROW_NUMBER instead.
-- Expected 1,1,3 -- actual 1,2,3
SELECT RANK() OVER (ORDER BY x ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) r FROM v;
-- Expected 1,1,3 -- actual 1,2,3
SELECT RANK() OVER (ORDER BY x ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) r FROM v;
-- PARTITION BY changes nothing.
-- Expected 1,1,3 -- actual 1,1,1
SELECT RANK() OVER (PARTITION BY g ORDER BY x ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) r FROM v;

-- =====================================================================================
-- PART 4 -- CONTROLS: the neighbouring function classes are CORRECT under the same frame,
--           which bounds the defect to the ranking functions.
-- =====================================================================================
CREATE TABLE v (x TEXT, g TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO v VALUES ('a', 'p');
INSERT INTO v VALUES ('a', 'p');
INSERT INTO v VALUES ('b', 'p');
REFRESH TABLE v;
-- ROW_NUMBER must ignore peer groups and does: 1,2,3.  Correct.
SELECT ROW_NUMBER() OVER (ORDER BY x ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) r FROM v;
-- An AGGREGATE window function must HONOUR the frame, and does: 3,3,3.  Correct.
SELECT COUNT(*) OVER (ORDER BY x ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) r FROM v;
-- A growing ROWS frame on an aggregate counts literal rows, so 1,2,3 is correct (a RANGE
-- frame would give 2,2,3 because CURRENT ROW then includes peers).
SELECT COUNT(*) OVER (ORDER BY x ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) r FROM v;

-- NOTE: PERCENT_RANK() and CUME_DIST() -- the other two frame-sensitive ranking functions,
-- and the ones ClickHouse explicitly rejects a ROWS frame for -- are NOT IMPLEMENTED in
-- CrateDB 6.4.1 (`0A000 Unknown function: percent_rank()` / `cume_dist()`), so they cannot
-- be compared here.
