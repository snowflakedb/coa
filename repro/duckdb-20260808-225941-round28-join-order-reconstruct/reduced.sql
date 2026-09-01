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

-- DuckDB: INTERNAL Error "Operator occurrence N was reconstructed more than once"
-- in QueryGraphManager::GenerateJoins (join-order plan reconstruction)
--
-- Engine      : duckdb v2.0.0-alpha37185 (Cyanoptera) e500d77864  (assertions/internal checks on)
-- Regression  : clean on v1.5.5 (Variegata, d8cdaa3); fails on v1.6.0-dev12322 (76dd1e7d6f)
--               and every v2.0.0-alpha build tested.
-- Session     : DuckDB CLI, in-memory, all defaults. No SET / PRAGMA required.
-- Findings    : duckdb_20260808-225941/error_round{7,18,28}_0.sql  (one bug, 3 hits)
--
-- Every block below is delimited by a `-- >>> BLOCK` marker and is meant to run in its OWN fresh
-- in-memory database. Each block was run and checked against the stated expectation, so nothing here
-- is asserted from reasoning alone.
--
--   expect=repro  ->  the statement fails with "reconstructed more than once"
--   expect=clean  ->  the statement succeeds
--
-- The failure is in the optimizer, not in execution: EXPLAIN fails identically (block `explain`).


-- >>> BLOCK: concrete-as-emitted  expect=repro
-- The finding as the eqgen equivalence builder actually emits it, trimmed to the relations the
-- workload touches. `t0` and `t2` are shown trivialised because the reduction proved they are not
-- load-bearing (see bug_report.md "Equivalence construction"); `t1`'s chain is verbatim.
-- Kept so the reader sees the real finding: self-aliases, tag/UNION-ALL round-trip, window
-- round-trip, ENUM round-trip and table macro all as generated.
CREATE TABLE t (c_pk BIGINT NOT NULL, c_int BIGINT, c_big DECIMAL(38, 0), c_dec DECIMAL(10, 2), c_dbl DOUBLE, c_txt VARCHAR, c_chr VARCHAR, c_date DATE, c_ts TIMESTAMP);
INSERT INTO t VALUES (1, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);
INSERT INTO t VALUES (2, -1, -7, 12.34, 0.0, NULL, 'abc', '1999-12-31', NULL);
INSERT INTO t VALUES (3, 1, 1, -5.5, 1000.125, 'trailing ', 'Zed', '2024-01-15', NULL);
INSERT INTO t VALUES (4, -7, -7, -5.5, 0.0, 'trailing ', NULL, '2024-01-15', NULL);
INSERT INTO t VALUES (5, 2, NULL, 12.34, NULL, 'a', '', NULL, '1999-12-31 23:59:59');
INSERT INTO t VALUES (6, 0, -7, 12.34, 0.0, 'trailing ', NULL, '2024-01-15', NULL);
INSERT INTO t VALUES (7, 2, 1, -5.5, -1.5, 'abc', NULL, '1999-12-31', NULL);
INSERT INTO t VALUES (8, -1, -7, 12.34, 0.0, NULL, 'abc', '1999-12-31', NULL);
ALTER TABLE t RENAME TO t__base;
CREATE VIEW t0 AS SELECT * FROM t__base;
CREATE VIEW t2 AS SELECT * FROM t__base;
CREATE VIEW t__base_view_6 AS SELECT * FROM t__base;
CREATE VIEW t__base_view_7 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts, CAST(NULL AS BIGINT) AS eq_tmp_col_1 FROM t__base_view_6;
CREATE VIEW t__base_view_8 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_view_7;
CREATE SCHEMA t__base_view_9_sch;
CREATE OR REPLACE TABLE t__base_view_9_sch.t__base_view_9_tbl AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_view_8;
CREATE VIEW t__base_view_9 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_view_9_sch.t__base_view_9_tbl;
CREATE MACRO t__base_view_10_macro() AS TABLE SELECT c_pk, c_int, c_big, c_dec, c_dbl, CASE WHEN c_txt IS NULL THEN CAST(NULL AS VARCHAR) ELSE decode(unhex(hex(encode(c_txt)))) END AS c_txt, CASE WHEN c_chr IS NULL THEN CAST(NULL AS VARCHAR) ELSE decode(unhex(hex(encode(c_chr)))) END AS c_chr, c_date, c_ts FROM t__base;
CREATE VIEW t__base_view_10 AS SELECT * FROM t__base_view_10_macro();
CREATE VIEW t__base_view_11 AS SELECT * FROM t__base_view_10 WHERE 1 = 0;
CREATE TYPE t__base_view_12_enum AS ENUM (SELECT DISTINCT c_txt FROM t__base_view_11 WHERE c_txt IS NOT NULL);
CREATE VIEW t__base_view_12 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, CAST(CAST(c_txt AS t__base_view_12_enum) AS VARCHAR) AS c_txt, c_chr, c_date, c_ts FROM t__base_view_11;
CREATE TABLE t__base_table_6 AS SELECT l.c_pk AS c_pk, l.c_int AS c_int, l.c_big AS c_big, l.c_dec AS c_dec, l.c_dbl AS c_dbl, l.c_txt AS c_txt, l.c_chr AS c_chr, l.c_date AS c_date, l.c_ts AS c_ts FROM t__base_view_9 l ANTI JOIN t__base_view_12 r ON TRUE;
CREATE TABLE t__base_table_7 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts, 1 AS eq_tag_1 FROM t__base_table_6;
CREATE TABLE t__base_table_8 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts, 0 AS eq_tag_1 FROM t__base_table_7;
CREATE TABLE t__base_table_9 AS SELECT * FROM t__base_table_7 UNION ALL SELECT * FROM t__base_table_8;
DELETE FROM t__base_table_9 WHERE eq_tag_1 <> 1;
CREATE VIEW t__base_view_13 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_table_9;
CREATE VIEW t__base_view_14 AS SELECT MAX(c_pk) OVER (PARTITION BY c_pk ORDER BY c_pk RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS c_pk, MAX(c_int) OVER (PARTITION BY c_int ORDER BY c_int RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS c_int, MAX(c_big) OVER (PARTITION BY c_big ORDER BY c_big RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS c_big, MAX(c_dec) OVER (PARTITION BY c_dec ORDER BY c_dec RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS c_dec, c_dbl, MAX(c_txt) OVER (PARTITION BY c_txt ORDER BY c_txt RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS c_txt, MAX(c_chr) OVER (PARTITION BY c_chr ORDER BY c_chr RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS c_chr, MAX(c_date) OVER (PARTITION BY c_date ORDER BY c_date RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS c_date, MAX(c_ts) OVER (PARTITION BY c_ts ORDER BY c_ts RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS c_ts FROM t__base_view_13;
CREATE TYPE t__base_view_15_enum AS ENUM (SELECT DISTINCT c_txt FROM t__base_view_14 WHERE c_txt IS NOT NULL);
CREATE VIEW t__base_view_15 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, CAST(CAST(c_txt AS t__base_view_15_enum) AS VARCHAR) AS c_txt, c_chr, c_date, c_ts FROM t__base_view_14;
CREATE MACRO t1_macro() AS TABLE SELECT * FROM t__base_view_15 WHERE (((NOT (c_txt IS NOT NULL)) OR ((c_txt <> 'abc') AND (c_dec IS NULL))) OR (NOT ((NOT (c_txt IS NOT NULL)) OR ((c_txt <> 'abc') AND (c_dec IS NULL))))) OR (((NOT (c_txt IS NOT NULL)) OR ((c_txt <> 'abc') AND (c_dec IS NULL))) IS NULL);
CREATE VIEW t1 AS SELECT * FROM t1_macro();
-- The workload query, verbatim from the finding. Expected: 0 rows (the ON predicate `c_chr > c_chr`
-- is never true). Actual: INTERNAL Error: Operator occurrence 2 was reconstructed more than once.
SELECT t1.c_big, t2.c_chr, t0.c_ts, t2.c_ts, t1.c_int, t0.c_date, t2.c_pk, t0.c_big, t0.c_pk FROM t2, t0, t1 INNER  JOIN  (SELECT PI() AS col0 FROM t2) AS sub0  ON ((t1.c_chr)>(t1.c_chr)) ORDER BY ((t2.c_txt)||(t2.c_chr));


-- >>> BLOCK: distilled  expect=repro
-- The whole equivalence construction reduces away: four plain one-column tables reproduce it.
-- Nothing about views, macros, ENUMs, window functions or UNION ALL is required -- the builders
-- only mattered because they changed `t1`'s estimated cardinality (see the `card` control).
-- `d` stands for the finding's `(SELECT PI() ... FROM t2) AS sub0` derived table; a base table works.
CREATE TABLE a AS SELECT i AS x FROM range(8) s(i);
CREATE TABLE b AS SELECT i AS x FROM range(8) s(i);
CREATE TABLE c AS SELECT i AS x FROM range(16) s(i);
CREATE TABLE d AS SELECT i AS x FROM range(8) s(i);
-- Expected: 7680 rows (8*8*15*8 -- `c.x > 0` keeps 15 of c's 16 rows).
-- Actual  : INTERNAL Error: Operator occurrence 2 was reconstructed more than once.
SELECT 1 FROM a, b, c INNER JOIN d ON (c.x > 0);


-- >>> BLOCK: explain  expect=repro
-- It is a planning failure, not an execution one: EXPLAIN alone is enough.
CREATE TABLE a AS SELECT i AS x FROM range(8) s(i);
CREATE TABLE b AS SELECT i AS x FROM range(8) s(i);
CREATE TABLE c AS SELECT i AS x FROM range(16) s(i);
CREATE TABLE d AS SELECT i AS x FROM range(8) s(i);
EXPLAIN SELECT 1 FROM a, b, c INNER JOIN d ON (c.x > 0);


-- ============================================================================================
-- Controls. Each swaps exactly ONE ingredient of the distilled repro and succeeds, so each names
-- something the bug requires. All four tables are as in `distilled` unless the comment says otherwise.
-- ============================================================================================

-- >>> BLOCK: control-card  expect=clean
-- CARDINALITY is load-bearing: c=8 instead of 16 and the same query plans fine. The threshold is
-- c>=10 with a=b=d=8. `d` is non-monotonic (d=1 clean, d=2..8 fail, d>=16 clean) -- the mark of the
-- plan the DP enumerator happens to pick, not of any one construct.
CREATE TABLE a AS SELECT i AS x FROM range(8) s(i);
CREATE TABLE b AS SELECT i AS x FROM range(8) s(i);
CREATE TABLE c AS SELECT i AS x FROM range(8) s(i);
CREATE TABLE d AS SELECT i AS x FROM range(8) s(i);
SELECT 1 FROM a, b, c INNER JOIN d ON (c.x > 0);

-- >>> BLOCK: control-pred-on-right  expect=clean
-- The single-sided ON predicate must reference the relation to the LEFT of the INNER JOIN.
-- Pointing it at the join's own right side (`d`) is fine.
CREATE TABLE a AS SELECT i AS x FROM range(8) s(i);
CREATE TABLE b AS SELECT i AS x FROM range(8) s(i);
CREATE TABLE c AS SELECT i AS x FROM range(16) s(i);
CREATE TABLE d AS SELECT i AS x FROM range(8) s(i);
SELECT 1 FROM a, b, c INNER JOIN d ON (d.x > 0);

-- >>> BLOCK: control-pred-far-left  expect=clean
-- ...and specifically the relation IMMEDIATELY left of the join. Aiming the predicate at `a`,
-- further up the comma list, is fine.
CREATE TABLE a AS SELECT i AS x FROM range(8) s(i);
CREATE TABLE b AS SELECT i AS x FROM range(8) s(i);
CREATE TABLE c AS SELECT i AS x FROM range(16) s(i);
CREATE TABLE d AS SELECT i AS x FROM range(8) s(i);
SELECT 1 FROM a, b, c INNER JOIN d ON (a.x > 0);

-- >>> BLOCK: control-equijoin  expect=clean
-- A genuine join condition connecting both sides is fine. The bug needs an ON clause that
-- constrains only one side, i.e. a join that is really a cross product plus a filter.
CREATE TABLE a AS SELECT i AS x FROM range(8) s(i);
CREATE TABLE b AS SELECT i AS x FROM range(8) s(i);
CREATE TABLE c AS SELECT i AS x FROM range(16) s(i);
CREATE TABLE d AS SELECT i AS x FROM range(8) s(i);
SELECT 1 FROM a, b, c INNER JOIN d ON (c.x = d.x);

-- >>> BLOCK: control-cross-join  expect=clean
-- Writing the same relationship as an explicit CROSS JOIN (no ON clause at all) is fine.
CREATE TABLE a AS SELECT i AS x FROM range(8) s(i);
CREATE TABLE b AS SELECT i AS x FROM range(8) s(i);
CREATE TABLE c AS SELECT i AS x FROM range(16) s(i);
CREATE TABLE d AS SELECT i AS x FROM range(8) s(i);
SELECT 1 FROM a, b, c CROSS JOIN d;

-- >>> BLOCK: control-left-join  expect=clean
-- INNER is required: the identical single-sided ON under LEFT JOIN is fine.
CREATE TABLE a AS SELECT i AS x FROM range(8) s(i);
CREATE TABLE b AS SELECT i AS x FROM range(8) s(i);
CREATE TABLE c AS SELECT i AS x FROM range(16) s(i);
CREATE TABLE d AS SELECT i AS x FROM range(8) s(i);
SELECT 1 FROM a, b, c LEFT JOIN d ON (c.x > 0);

-- >>> BLOCK: control-filter-in-where  expect=clean
-- The semantically identical query with the predicate in WHERE instead of ON is fine. This is the
-- sharpest control: same relations, same rows, same result -- only the syntactic position of the
-- predicate differs, which is what feeds the join-order operator descriptor.
CREATE TABLE a AS SELECT i AS x FROM range(8) s(i);
CREATE TABLE b AS SELECT i AS x FROM range(8) s(i);
CREATE TABLE c AS SELECT i AS x FROM range(16) s(i);
CREATE TABLE d AS SELECT i AS x FROM range(8) s(i);
SELECT 1 FROM a, b, c, d WHERE c.x > 0;

-- >>> BLOCK: control-three-relations  expect=clean
-- Four relations are needed; dropping `b` to leave three is fine.
CREATE TABLE a AS SELECT i AS x FROM range(8) s(i);
CREATE TABLE c AS SELECT i AS x FROM range(16) s(i);
CREATE TABLE d AS SELECT i AS x FROM range(8) s(i);
SELECT 1 FROM a, c INNER JOIN d ON (c.x > 0);

-- >>> BLOCK: control-extra-where  expect=clean
-- Adding one more WHERE conjunct on `b` changes the chosen plan and is fine.
CREATE TABLE a AS SELECT i AS x FROM range(8) s(i);
CREATE TABLE b AS SELECT i AS x FROM range(8) s(i);
CREATE TABLE c AS SELECT i AS x FROM range(16) s(i);
CREATE TABLE d AS SELECT i AS x FROM range(8) s(i);
SELECT 1 FROM a, b, c INNER JOIN d ON (c.x > 0) WHERE b.x > 0;

-- >>> BLOCK: control-no-join-order  expect=clean
-- Mechanism, half 1: the join order optimizer. Disabling it avoids the failure.
CREATE TABLE a AS SELECT i AS x FROM range(8) s(i);
CREATE TABLE b AS SELECT i AS x FROM range(8) s(i);
CREATE TABLE c AS SELECT i AS x FROM range(16) s(i);
CREATE TABLE d AS SELECT i AS x FROM range(8) s(i);
SET disabled_optimizers='join_order';
SELECT 1 FROM a, b, c INNER JOIN d ON (c.x > 0);

-- >>> BLOCK: control-no-filter-pushdown  expect=clean
-- Mechanism, half 2: filter pushdown. Disabling THAT avoids it too, so both stages are required --
-- pushdown rewrites the single-sided ON predicate, and reconstruction then double-assigns the
-- operator occurrence it left behind.
CREATE TABLE a AS SELECT i AS x FROM range(8) s(i);
CREATE TABLE b AS SELECT i AS x FROM range(8) s(i);
CREATE TABLE c AS SELECT i AS x FROM range(16) s(i);
CREATE TABLE d AS SELECT i AS x FROM range(8) s(i);
SET disabled_optimizers='filter_pushdown';
SELECT 1 FROM a, b, c INNER JOIN d ON (c.x > 0);
