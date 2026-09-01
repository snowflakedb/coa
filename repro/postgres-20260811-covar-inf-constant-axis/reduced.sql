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

-- PostgreSQL 20devel: COVAR_POP / COVAR_SAMP / REGR_SXY return 0.0 instead of NaN
-- when one argument is constant and the other contains Inf that is NOT the first
-- non-null input. Root cause: float8_regr_accum's commonX/commonY optimization
-- (BUG #19340) leaves Sxy at 0 while Inf is only forced into Sxy on the *first*
-- input. Row order (DISTINCT / ORDER BY / HashAgg) therefore flips NaN ↔ 0.0.
--
-- Build: PostgreSQL 20devel, --enable-cassert --enable-debug
--   source HEAD 36f7330b8b2238c2093d7eac521f996b33e66121
-- locale: C; standard_conforming_strings: on
--
-- eqgen-found: postgres_hunt_20260809-175406/postgres_20260809-175452/mismatch_round135_0.sql
-- Load-bearing construction in the original finding: DISTINCT over UNION ALL
-- (window-dedup / tag round-trip) on the relation feeding COTD(...), which
-- reorders so COTD(0)=+Inf is not the first non-null X/Y value.

-- =============================================================================
-- Part A — concrete (builder-shaped): base plain tables vs DISTINCT∪UNION ALL view
-- =============================================================================
-- Expected: both sides NaN for the COTD(c_int)≈1.1106 group.
-- Actual:   base NaN, equivalent 0.0.

CREATE TABLE t (
  c_pk INTEGER NOT NULL, c_int INTEGER, c_big NUMERIC(38, 0),
  c_dec NUMERIC(10, 2), c_dbl DOUBLE PRECISION,
  c_txt TEXT, c_chr TEXT, c_date DATE, c_ts TIMESTAMP
);
INSERT INTO t VALUES (1, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);
INSERT INTO t VALUES (2, NULL, 0, 0.0, NULL, 'Zed', 'trailing ', '1999-12-31', '2024-01-15 12:34:56');
INSERT INTO t VALUES (3, NULL, 2, -5.5, NULL, 'a', 'a', '1999-12-31', '1999-12-31 23:59:59');
INSERT INTO t VALUES (4, -7, 0, NULL, NULL, 'abc', '', '1999-12-31', '2024-01-15 12:34:56');
INSERT INTO t VALUES (5, 42, -7, 999.99, -1.5, 'Zed', '', NULL, '1999-12-31 23:59:59');
INSERT INTO t VALUES (6, -7, -7, 999.99, 1000.125, 'o''brien', 'o''brien', '2030-06-01', '2024-01-15 12:34:56');
INSERT INTO t VALUES (7, 42, 2, -5.5, 1000.125, 'Zed', NULL, '2024-01-15', NULL);
INSERT INTO t VALUES (8, NULL, 0, 0.0, NULL, 'Zed', 'trailing ', '1999-12-31', '2024-01-15 12:34:56');

CREATE TABLE t0 AS SELECT * FROM t;
CREATE TABLE t1 AS SELECT * FROM t;
CREATE TABLE t2 AS SELECT * FROM t;

SELECT DISTINCT COVAR_POP(LENGTH(t1.c_chr), COTD(t2.c_big)), COTD(t1.c_int)
FROM t0, t1 LEFT JOIN t2 ON (t1.c_big != t1.c_pk)
GROUP BY COTD(t1.c_int);
-- Expected: (NaN, 1.1106125148291928) among the groups
-- Actual:   (NaN, 1.1106125148291928)   -- BASE is correct

-- Fresh DB for the equivalent side:
-- ALTER TABLE t RENAME TO t__base;
-- CREATE TABLE t0 AS SELECT * FROM t__base;
-- CREATE TABLE t1 AS SELECT * FROM t__base;
-- CREATE VIEW t2 AS SELECT DISTINCT * FROM (SELECT * FROM t__base UNION ALL SELECT * FROM t__base) s;
-- <same query>
-- Expected: (NaN, 1.1106125148291928)
-- Actual:   (0.0, 1.1106125148291928)   -- WRONG


-- =============================================================================
-- Part B — distilled minimal repro (no equivalence construction needed)
-- =============================================================================
-- Inf not first + constant other argument → 0.0 instead of NaN.

DROP TABLE IF EXISTS t CASCADE;
CREATE TABLE t (y double precision);
INSERT INTO t VALUES (3), ('Infinity'), (4);

SELECT COVAR_POP(0::float8, y) FROM t;
-- Expected 1 row: NaN
-- Actual:         0.0

SELECT COVAR_POP(y, 0::float8) FROM t;
-- Expected 1 row: NaN
-- Actual:         0.0

SELECT COVAR_SAMP(0::float8, y) FROM t;
-- Expected 1 row: NaN
-- Actual:         0.0

SELECT REGR_SXY(0::float8, y) FROM t;
-- Expected 1 row: NaN
-- Actual:         0.0


-- =============================================================================
-- Part C — controls (one ingredient flipped each)
-- =============================================================================

-- C1. Inf first → NaN (first-input branch in float8_regr_accum is correct)
DROP TABLE t;
CREATE TABLE t (y double precision);
INSERT INTO t VALUES ('Infinity'), (3), (4);
SELECT COVAR_POP(0::float8, y) FROM t;
-- Expected / actual: NaN

-- C2. All finite, constant other arg → 0.0 is correct
DROP TABLE t;
CREATE TABLE t (y double precision);
INSERT INTO t VALUES (3), (4), (5);
SELECT COVAR_POP(0::float8, y) FROM t;
-- Expected / actual: 0.0

-- C3. Inf present, neither arg constant → NaN (Sxy is updated)
DROP TABLE t;
CREATE TABLE t (y double precision);
INSERT INTO t VALUES (3), ('Infinity'), (4);
SELECT COVAR_POP(y, y) FROM t;
-- Expected / actual: NaN

-- C4. Unary VAR_POP still forces NaN for Inf (not on the regr commonX path)
SELECT VAR_POP(y) FROM t;
-- Expected / actual: NaN

-- C5. COTD(0)=+Inf, reordered by DISTINCT (the eqgen trigger shape)
DROP TABLE t;
CREATE TABLE t (y numeric);
INSERT INTO t VALUES (NULL), (0), (2), (-7);
SELECT COVAR_POP(0::float8, COTD(y)) FROM t;
-- Expected / actual: NaN   -- seq-scan order hits Inf first after NULL skip
SELECT COVAR_POP(0::float8, COTD(y)) FROM (SELECT DISTINCT y FROM t) s;
-- Expected: NaN
-- Actual:   0.0
SELECT COVAR_POP(0::float8, v) FROM (SELECT COTD(y) AS v FROM t WHERE y IS NOT NULL ORDER BY y) s;
-- Expected: NaN
-- Actual:   0.0
