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

-- DuckDB: Binder::BindSelectNode segfaults on a two-argument AGE(...) call in GROUP BY.
-- Not specific to a table, to NULL, or to a malformed string -- ANY well-typed 2-arg AGE() call
-- appearing in GROUP BY crashes, with or without also appearing in SELECT.
--
-- Engine: DuckDB CLI v2.0.0-alpha37464 (ea53ecdca1) -- artifacts.duckdb.org/latest, tip of main.

-- ===========================================================================================
-- 1. As found by the eqgen campaign (sqlancerpp-generated workload query, real equivalence
--    object -- see bug_report.md for the full 20+ statement chain). Collapses to the same
--    minimal trigger below; the object construction is not load-bearing.
-- ===========================================================================================
-- SELECT VAR_POP(((t.c_int)&(345374513))), AGE('', NULL), AVG(RADIANS(1042663571)) FROM t
-- GROUP BY AGE('', NULL) ORDER BY t.c_pk ASC;

-- ===========================================================================================
-- 2. Distilled minimal repro -- no table, no data, no other clause.
-- ===========================================================================================
SELECT AGE(NULL, NULL) GROUP BY AGE(NULL, NULL);
-- Expected: 1 row, NULL (age() of two NULLs is NULL; a query with no FROM and no aggregates in
-- the GROUP BY key is otherwise a constant-folds-to-one-row case).
-- Actual: Segmentation fault (core dumped). Full gdb backtrace in bug_report.md.

-- ===========================================================================================
-- Controls -- one ingredient changed per control.
-- ===========================================================================================

-- Control A: AGE() in SELECT only, NOT repeated in GROUP BY (a different GROUP BY key) -- clean.
SELECT AGE(NULL, NULL), 1 AS g FROM (SELECT 1) t GROUP BY g;
-- Expected/Actual: 1 row (NULL, 1). Clean.

-- Control B: GROUP BY only, not in SELECT at all -- still crashes. Confirms SELECT-list
-- duplication is not required; GROUP BY alone is sufficient.
-- SELECT 1 FROM (SELECT 1) t GROUP BY AGE(NULL, NULL);
-- Expected: 1 row (1). Actual: Segmentation fault.

-- Control C: well-typed, non-NULL, non-string arguments -- still crashes. Confirms this is not
-- about NULL specifically or about a malformed string cast; it is the two-argument AGE() call
-- shape itself, in GROUP BY.
-- SELECT AGE(DATE '2024-01-01', NULL::TIMESTAMP) GROUP BY AGE(DATE '2024-01-01', NULL::TIMESTAMP);
-- Expected: 1 row. Actual: Segmentation fault.

-- Control D: single-argument AGE(x) (a different overload, AGE(TIMESTAMP) -> INTERVAL) in the
-- same shape -- clean (correctly rejected as an ambiguous overload, no cast performed, no crash).
-- SELECT AGE('x') GROUP BY AGE('x');
-- Expected/Actual: Binder Error: Could not choose a best candidate function... (clean, no crash).

-- Control E: the ORIGINAL malformed-string hypothesis, ruled out -- a genuinely invalid string
-- with an explicit, correctly-typed second argument produces a clean Conversion Error, not a
-- crash, because it never reaches the two-untyped/inferred-argument GROUP BY path.
-- SELECT AGE('x', DATE '2024-01-01') GROUP BY AGE('x', DATE '2024-01-01');
-- Expected/Actual: Conversion Error: invalid timestamp field format: "x" ... (clean, no crash).
