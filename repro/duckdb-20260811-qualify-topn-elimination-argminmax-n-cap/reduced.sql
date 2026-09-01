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

-- DuckDB: TopNWindowElimination rewrites a large-but-valid ROW_NUMBER() <= k QUALIFY/WHERE bound
-- into an internal arg_min/arg_max (or MIN/MAX) list aggregate without checking k against that
-- aggregate's own MAX_N=1,000,000 heap-size guard, so a semantically-true filter throws instead
-- of returning all rows.
--
-- Engine: DuckDB CLI v2.0.0-alpha37464 (ea53ecdca1) -- artifacts.duckdb.org/latest, tip of main.
-- Also reproduces unchanged on released v1.5.0 (python duckdb wheel) -- not a recent regression.

-- ===========================================================================================
-- 1. Concrete form, as eqgen's DuckDBRowNumberBoundQualifyBuilder emits it (algebra rule
--    (Qualify): the filter is an identity for ANY table, since no real row count can reach the
--    literal bound -- so base and "equivalent" must return identical rows, for any bound.)
-- ===========================================================================================
CREATE TABLE t__base (c_pk INTEGER, c_int INTEGER, c_big BIGINT, c_dec DECIMAL(18,4), c_dbl DOUBLE,
                       c_txt VARCHAR, c_chr VARCHAR, c_date DATE, c_ts TIMESTAMP);
INSERT INTO t__base (c_pk, c_int) VALUES (1, 10), (2, 20), (3, 30);

CREATE VIEW t AS
SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts
FROM t__base
QUALIFY (ROW_NUMBER() OVER (ORDER BY c_pk)) <= 4611686018427387904;  -- 2**62, builder's literal

SELECT c_pk, c_int FROM t;
-- Expected: 3 rows (1,10) (2,20) (3,30) -- same as t__base, since the bound is never reached.
-- Actual:   Invalid Input Error: Invalid input for arg_min/arg_max: n value must be < 1000000

-- ===========================================================================================
-- 2. Distilled minimal repro -- no view, no eqgen scaffolding, 2 columns, 3 rows, one statement.
-- ===========================================================================================
CREATE TABLE t2 (c_pk INTEGER, c_int INTEGER);
INSERT INTO t2 VALUES (1,10),(2,20),(3,30);

SELECT c_pk, c_int FROM t2 QUALIFY (ROW_NUMBER() OVER (ORDER BY c_pk)) <= 1000000;
-- Expected: 3 rows. Actual: Invalid Input Error: Invalid input for arg_min/arg_max: n value must be < 1000000

-- ===========================================================================================
-- 3. Even smaller: single projected column collapses arg_min/arg_max to plain MIN/MAX, and the
--    sibling guard in minmax.cpp fires instead -- same MAX_N, different function name in the
--    message, confirming two independent call sites need the same fix.
-- ===========================================================================================
CREATE TABLE t3 (c_pk INTEGER);
INSERT INTO t3 VALUES (1);
SELECT c_pk FROM t3 QUALIFY (ROW_NUMBER() OVER (ORDER BY c_pk)) <= 1000000;
-- Expected: 1 row. Actual: Invalid Input Error: Invalid input for MIN/MAX: n value must be < 1000000

-- ===========================================================================================
-- Controls -- one ingredient changed per control, everything else held fixed.
-- ===========================================================================================

-- Control A: bound one below the cap succeeds (exact boundary is 1,000,000, not "a large number").
CREATE TABLE t4 (c_pk INTEGER, c_int INTEGER);
INSERT INTO t4 VALUES (1,10),(2,20),(3,30);
SELECT c_pk, c_int FROM t4 QUALIFY (ROW_NUMBER() OVER (ORDER BY c_pk)) <= 999999;
-- Expected/Actual: 3 rows. Confirms the cap is exactly MAX_N=1,000,000 from arg_min_max.cpp/minmax.cpp.

-- Control B: a plain ORDER BY ... LIMIT with the SAME huge constant is unaffected -- there is no
-- general "huge integer" restriction anywhere else in DuckDB; this is specific to the
-- row_number()<=k QUALIFY/WHERE shape that TopNWindowElimination pattern-matches.
CREATE TABLE t5 (c_pk INTEGER, c_int INTEGER);
INSERT INTO t5 VALUES (1,10),(2,20),(3,30);
SELECT c_pk, c_int FROM t5 ORDER BY c_pk LIMIT 4611686018427387904;
-- Expected/Actual: 3 rows, no error.

-- Control C: disabling the responsible optimizer makes the exact same query succeed --
-- localises the defect to TopNWindowElimination's rewrite, not to arg_min/arg_max/MIN-MAX
-- themselves (those guards are legitimate for direct user calls with a huge n).
SET disabled_optimizers='top_n_window_elimination';
CREATE TABLE t6 (c_pk INTEGER, c_int INTEGER);
INSERT INTO t6 VALUES (1,10),(2,20),(3,30);
SELECT c_pk, c_int FROM t6 QUALIFY (ROW_NUMBER() OVER (ORDER BY c_pk)) <= 1000000;
-- Expected/Actual: 3 rows, no error, with the optimizer disabled.
RESET disabled_optimizers;

-- Control D: zero rows still plans the same rewrite and still throws -- the guard fires on the
-- literal bound alone, never on the table's actual cardinality (confirms it cannot be masked by
-- "the table happens to be small").
CREATE TABLE t7 (c_pk INTEGER);
SELECT c_pk FROM t7 QUALIFY (ROW_NUMBER() OVER (ORDER BY c_pk)) <= 1000000;
-- Expected: 0 rows, no error (there is nothing to rank). Actual: 0 rows, no error -- this ONE
-- does succeed empty, because arg_min/arg_max's guard only fires on state INITIALIZATION, which
-- happens on the first non-null input row; see bug_report.md Characterization for the exact path.

-- EXPLAIN evidence that the rewrite happens at logical-plan time (visible without executing),
-- but the guard only fires at execution (state init), so "bare EXPLAIN reproduces it" is FALSE
-- here, unlike some of the join-family findings already filed in this repo:
CREATE TABLE t8 (c_pk INTEGER);
INSERT INTO t8 VALUES (1);
EXPLAIN SELECT c_pk FROM t8 QUALIFY (ROW_NUMBER() OVER (ORDER BY c_pk)) <= 1000000;
-- Plan shows: Ungrouped Aggregate  Aggregates: min(#0, #1)  -- confirms TopNWindowElimination
-- already rewrote the window+filter into a MIN(x, n)-style list aggregate at the logical level.
