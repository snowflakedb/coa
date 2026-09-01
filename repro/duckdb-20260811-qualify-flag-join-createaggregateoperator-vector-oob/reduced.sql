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

-- DuckDB: TopNWindowElimination::CreateAggregateOperator throws
-- "INTERNAL Error: Attempted to access index N within vector of size N" (a type-vector /
-- column-count mismatch inside LogicalFilter::ResolveTypes -> LogicalOperator::MapTypes) for a
-- view built as "keyed relation INNER JOIN a single-column flag table WHERE flag = 1" (the
-- FlagTableJoinQueryBuilder shape), read by an outer whole-relation (no PARTITION BY) top-N
-- QUALIFY. Same stack-trace mechanism as the CLOSED issue duckdb/duckdb#21820, whose own repro
-- (PARTITION BY + IN-list filter, no join) is now fixed on this build -- so the underlying defect
-- in CreateAggregateOperator/MapTypes was not fully closed, only that one trigger shape.
--
-- Engine: DuckDB CLI v2.0.0-alpha37464 (ea53ecdca1) -- artifacts.duckdb.org/latest, tip of main.

-- ===========================================================================================
-- 1. Concrete form, close to how eqgen actually built it (FlagTableJoinQueryBuilder's keyed
--    relation + single-column flag table + INNER JOIN + WHERE flag = 1, read by a workload
--    query carrying the new DuckDBRowNumberBoundQualifyBuilder / sqlancerpp-generator outer
--    QUALIFY). The original finding's chain was 39 statements deep (partition-union, SEMI joins,
--    ATTACH mirrors, ENUM-free text codec round-trip, struct pack/unpack, array indexing,
--    EXCEPT/INTERSECT ALL via MACROs, a full-frame window aggregate, CHECKPOINT, ADD/DROP
--    COLUMN) -- none of that depth is load-bearing; see the distilled repro below.
-- ===========================================================================================
CREATE TABLE t__base_concrete (c_pk INTEGER NOT NULL, c_int INTEGER, c_big DECIMAL(38, 0),
                                c_dec DECIMAL(10, 2), c_dbl DOUBLE, c_txt VARCHAR, c_chr VARCHAR,
                                c_date DATE, c_ts TIMESTAMP);
INSERT INTO t__base_concrete VALUES (1, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);
INSERT INTO t__base_concrete VALUES (2, 1, 1, 999.99, 0.0, NULL, 'abc', '2030-06-01', NULL);
INSERT INTO t__base_concrete VALUES (3, 2, NULL, NULL, 1.5, 'a', 'a', '2030-06-01', NULL);

CREATE TABLE t__base_table_20 AS
SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts,
       ROW_NUMBER() OVER (ORDER BY c_pk) AS eq_uid_1
FROM t__base_concrete;
CREATE TABLE t__base_table_21 AS SELECT eq_uid_1, 1 AS eq_flag_1 FROM t__base_table_20;
CREATE VIEW t_concrete AS
SELECT l.c_pk AS c_pk, l.c_int AS c_int, l.c_big AS c_big, l.c_dec AS c_dec, l.c_dbl AS c_dbl,
       l.c_txt AS c_txt, l.c_chr AS c_chr, l.c_date AS c_date, l.c_ts AS c_ts
FROM t__base_table_20 l INNER JOIN t__base_table_21 r ON l.eq_uid_1 = r.eq_uid_1
WHERE r.eq_flag_1 = 1;

SELECT c_dbl, c_int FROM t_concrete WHERE (c_txt IS NULL) QUALIFY ROW_NUMBER() OVER (ORDER BY c_pk) <= 10;
-- Expected: 2 rows (c_pk=1: NULL,NULL and c_pk=2: 0.0,1 -- same as the base table).
-- Actual:   INTERNAL Error: Attempted to access index 5 within vector of size 5

-- ===========================================================================================
-- 2. Distilled minimal repro -- 4 columns, 4 statements past the base table.
-- ===========================================================================================
CREATE TABLE t__base (c_pk INTEGER NOT NULL, c_int INTEGER, c_dbl DOUBLE, c_txt VARCHAR);
INSERT INTO t__base VALUES (1, NULL, NULL, NULL);
INSERT INTO t__base VALUES (2, 1, 0.0, NULL);
INSERT INTO t__base VALUES (3, 2, 1.5, 'a');

CREATE TABLE t_uid AS SELECT c_pk, c_int, c_dbl, c_txt, ROW_NUMBER() OVER (ORDER BY c_pk) AS eq_uid_1 FROM t__base;
CREATE TABLE t_flag AS SELECT eq_uid_1, 1 AS eq_flag_1 FROM t_uid;
CREATE VIEW t AS
SELECT l.c_pk AS c_pk, l.c_int AS c_int, l.c_dbl AS c_dbl, l.c_txt AS c_txt
FROM t_uid l INNER JOIN t_flag r ON l.eq_uid_1 = r.eq_uid_1
WHERE r.eq_flag_1 = 1;

SELECT c_dbl, c_int FROM t WHERE (c_txt IS NULL) QUALIFY ROW_NUMBER() OVER (ORDER BY c_pk) <= 10;
-- Expected: 2 rows (NULL,NULL and 0.0,1). Actual: INTERNAL Error: Attempted to access index 5
-- within vector of size 5

-- ===========================================================================================
-- Controls -- one ingredient changed per control, everything else held fixed on the distilled shape.
-- ===========================================================================================

-- Control A: the SAME query against a plain table holding identical rows -- clean. Establishes
-- admissibility (one-sided error; base and equivalent are row-identical by construction).
CREATE TABLE t2 (c_pk INTEGER NOT NULL, c_int INTEGER, c_dbl DOUBLE, c_txt VARCHAR);
INSERT INTO t2 VALUES (1, NULL, NULL, NULL);
INSERT INTO t2 VALUES (2, 1, 0.0, NULL);
INSERT INTO t2 VALUES (3, 2, 1.5, 'a');
SELECT c_dbl, c_int FROM t2 WHERE (c_txt IS NULL) QUALIFY ROW_NUMBER() OVER (ORDER BY c_pk) <= 10;
-- Expected/Actual: 2 rows. Clean.

-- Control B: SEMI JOIN instead of INNER JOIN + WHERE flag = 1 (SemiJoinFlagRoundTripBuilder's
-- shape rather than FlagTableJoinQueryBuilder's) -- clean. The INNER-JOIN-plus-filter construct
-- specifically is necessary; the semantically equivalent SEMI JOIN is not enough.
CREATE TABLE t3__base (c_pk INTEGER NOT NULL, c_int INTEGER, c_dbl DOUBLE, c_txt VARCHAR);
INSERT INTO t3__base VALUES (1, NULL, NULL, NULL);
INSERT INTO t3__base VALUES (2, 1, 0.0, NULL);
INSERT INTO t3__base VALUES (3, 2, 1.5, 'a');
CREATE TABLE t3_uid AS SELECT c_pk, c_int, c_dbl, c_txt, ROW_NUMBER() OVER (ORDER BY c_pk) AS eq_sj_1 FROM t3__base;
CREATE TABLE t3_flag AS SELECT eq_sj_1 FROM t3_uid;
CREATE VIEW t3 AS
SELECT l.c_pk AS c_pk, l.c_int AS c_int, l.c_dbl AS c_dbl, l.c_txt AS c_txt
FROM t3_uid l SEMI JOIN t3_flag r ON l.eq_sj_1 = r.eq_sj_1;
SELECT c_dbl, c_int FROM t3 WHERE (c_txt IS NULL) QUALIFY ROW_NUMBER() OVER (ORDER BY c_pk) <= 10;
-- Expected/Actual: 2 rows. Clean.

-- Control C: drop the DOUBLE payload column (keep two INTEGERs instead) -- clean. Needs at
-- least one INTEGER, one DOUBLE, and one VARCHAR payload column together, not just "4 columns".
CREATE TABLE t4__base (c_pk INTEGER NOT NULL, c_int INTEGER, c_int2 INTEGER, c_txt VARCHAR);
INSERT INTO t4__base VALUES (1, NULL, NULL, NULL);
INSERT INTO t4__base VALUES (2, 1, 10, NULL);
INSERT INTO t4__base VALUES (3, 2, 20, 'a');
CREATE TABLE t4_uid AS SELECT c_pk, c_int, c_int2, c_txt, ROW_NUMBER() OVER (ORDER BY c_pk) AS eq_uid_1 FROM t4__base;
CREATE TABLE t4_flag AS SELECT eq_uid_1, 1 AS eq_flag_1 FROM t4_uid;
CREATE VIEW t4 AS
SELECT l.c_pk AS c_pk, l.c_int AS c_int, l.c_int2 AS c_int2, l.c_txt AS c_txt
FROM t4_uid l INNER JOIN t4_flag r ON l.eq_uid_1 = r.eq_uid_1
WHERE r.eq_flag_1 = 1;
SELECT c_pk FROM t4 WHERE (c_txt IS NULL) QUALIFY ROW_NUMBER() OVER (ORDER BY c_pk) <= 10;
-- Expected/Actual: 2 rows. Clean.

-- Control D: drop the VARCHAR payload column (predicate on c_int instead) -- clean.
CREATE TABLE t5__base (c_pk INTEGER NOT NULL, c_int INTEGER, c_dbl DOUBLE);
INSERT INTO t5__base VALUES (1, NULL, NULL);
INSERT INTO t5__base VALUES (2, 1, 0.0);
INSERT INTO t5__base VALUES (3, 2, 1.5);
CREATE TABLE t5_uid AS SELECT c_pk, c_int, c_dbl, ROW_NUMBER() OVER (ORDER BY c_pk) AS eq_uid_1 FROM t5__base;
CREATE TABLE t5_flag AS SELECT eq_uid_1, 1 AS eq_flag_1 FROM t5_uid;
CREATE VIEW t5 AS
SELECT l.c_pk AS c_pk, l.c_int AS c_int, l.c_dbl AS c_dbl
FROM t5_uid l INNER JOIN t5_flag r ON l.eq_uid_1 = r.eq_uid_1
WHERE r.eq_flag_1 = 1;
SELECT c_pk FROM t5 WHERE (c_int IS NULL) QUALIFY ROW_NUMBER() OVER (ORDER BY c_pk) <= 10;
-- Expected/Actual: 1 row. Clean.

-- Control E: SET disabled_optimizers='top_n_window_elimination' makes the ORIGINAL failing
-- distilled query succeed -- localises the defect to that one pass, matching #21820's stack.
SET disabled_optimizers='top_n_window_elimination';
CREATE TABLE t6__base (c_pk INTEGER NOT NULL, c_int INTEGER, c_dbl DOUBLE, c_txt VARCHAR);
INSERT INTO t6__base VALUES (1, NULL, NULL, NULL);
INSERT INTO t6__base VALUES (2, 1, 0.0, NULL);
INSERT INTO t6__base VALUES (3, 2, 1.5, 'a');
CREATE TABLE t6_uid AS SELECT c_pk, c_int, c_dbl, c_txt, ROW_NUMBER() OVER (ORDER BY c_pk) AS eq_uid_1 FROM t6__base;
CREATE TABLE t6_flag AS SELECT eq_uid_1, 1 AS eq_flag_1 FROM t6_uid;
CREATE VIEW t6 AS
SELECT l.c_pk AS c_pk, l.c_int AS c_int, l.c_dbl AS c_dbl, l.c_txt AS c_txt
FROM t6_uid l INNER JOIN t6_flag r ON l.eq_uid_1 = r.eq_uid_1
WHERE r.eq_flag_1 = 1;
SELECT c_dbl, c_int FROM t6 WHERE (c_txt IS NULL) QUALIFY ROW_NUMBER() OVER (ORDER BY c_pk) <= 10;
-- Expected/Actual: 2 rows (NULL,NULL and 0.0,1). Clean with the optimizer disabled.
RESET disabled_optimizers;

-- Control F: duckdb/duckdb#21820's own original repro, run verbatim against this build --
-- clean. Confirms that issue's specific trigger shape (PARTITION BY + IN-list/boolean filter,
-- no join) IS fixed; the distilled repro above reaches the same CreateAggregateOperator/
-- MapTypes assertion through a shape that issue's fix did not cover (no PARTITION BY, an
-- INNER JOIN + flag filter instead of a WHERE IN-list).
CREATE TABLE i21820_t1 (t INTEGER, d VARCHAR, v VARCHAR, b BOOLEAN, v2 VARCHAR);
INSERT INTO i21820_t1 VALUES (5, 'd', 'v', FALSE, 'a');
CREATE TABLE i21820_t2 (c1 VARCHAR, c2 VARCHAR);
INSERT INTO i21820_t2
SELECT t, v2 FROM i21820_t1
WHERE v IN ('n', 'v') AND b = FALSE
QUALIFY ROW_NUMBER() OVER (PARTITION BY d ORDER BY t DESC) = 1;
-- Expected/Actual: INSERT succeeds (0 rows matched the WHERE, so 0 rows inserted). Clean --
-- this exact issue's repro no longer reproduces on this build.
