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

-- MISMATCH
-- engine: postgres (PostgreSQL) 20devel (private cluster, socket /tmp/eqgen-pg-ewob6grn/sock)
-- seed: 1012693761
-- locale: C (initdb --locale=C)
-- standard_conforming_strings: on
-- statement_timeout: 60s
-- mismatch: 1 distinct only in base, 1 distinct only in equivalent

-- ============ database 1: the base table ============
CREATE TABLE t (c_pk INTEGER NOT NULL, c_int INTEGER, c_big NUMERIC(38, 0), c_dec NUMERIC(10, 2), c_dbl DOUBLE PRECISION, c_txt TEXT, c_chr TEXT, c_date DATE, c_ts TIMESTAMP);
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

-- ============ database 2: the equivalent ============
CREATE TABLE t (c_pk INTEGER NOT NULL, c_int INTEGER, c_big NUMERIC(38, 0), c_dec NUMERIC(10, 2), c_dbl DOUBLE PRECISION, c_txt TEXT, c_chr TEXT, c_date DATE, c_ts TIMESTAMP);
INSERT INTO t VALUES (1, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);
INSERT INTO t VALUES (2, NULL, 0, 0.0, NULL, 'Zed', 'trailing ', '1999-12-31', '2024-01-15 12:34:56');
INSERT INTO t VALUES (3, NULL, 2, -5.5, NULL, 'a', 'a', '1999-12-31', '1999-12-31 23:59:59');
INSERT INTO t VALUES (4, -7, 0, NULL, NULL, 'abc', '', '1999-12-31', '2024-01-15 12:34:56');
INSERT INTO t VALUES (5, 42, -7, 999.99, -1.5, 'Zed', '', NULL, '1999-12-31 23:59:59');
INSERT INTO t VALUES (6, -7, -7, 999.99, 1000.125, 'o''brien', 'o''brien', '2030-06-01', '2024-01-15 12:34:56');
INSERT INTO t VALUES (7, 42, 2, -5.5, 1000.125, 'Zed', NULL, '2024-01-15', NULL);
INSERT INTO t VALUES (8, NULL, 0, 0.0, NULL, 'Zed', 'trailing ', '1999-12-31', '2024-01-15 12:34:56');
ALTER TABLE t RENAME TO t__base;
CREATE UNLOGGED TABLE t__base_table_1 AS SELECT LAST_VALUE(c_pk) OVER (PARTITION BY c_pk ORDER BY c_pk) AS c_pk, LAST_VALUE(c_int) OVER (PARTITION BY c_int ORDER BY c_int) AS c_int, LAST_VALUE(c_big) OVER (PARTITION BY c_big ORDER BY c_big) AS c_big, LAST_VALUE(c_dec) OVER (PARTITION BY c_dec ORDER BY c_dec) AS c_dec, c_dbl, LAST_VALUE(c_txt) OVER (PARTITION BY c_txt ORDER BY c_txt) AS c_txt, LAST_VALUE(c_chr) OVER (PARTITION BY c_chr ORDER BY c_chr) AS c_chr, LAST_VALUE(c_date) OVER (PARTITION BY c_date ORDER BY c_date) AS c_date, LAST_VALUE(c_ts) OVER (PARTITION BY c_ts ORDER BY c_ts) AS c_ts FROM t__base;
CREATE TABLE t__base_table_2 AS WITH t__base_cte_1 AS (SELECT * FROM t__base_table_1) SELECT * FROM t__base_cte_1;
CREATE INDEX t__base_view_1_idx ON t__base_table_2 USING hash (c_pk);
ANALYZE t__base_table_2;
CREATE VIEW t__base_view_1 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_table_2;
CREATE TABLE t__base_table_3 AS SELECT l.c_pk AS c_pk, l.c_int AS c_int, l.c_big AS c_big, l.c_dec AS c_dec, l.c_dbl AS c_dbl, l.c_txt AS c_txt, l.c_chr AS c_chr, l.c_date AS c_date, l.c_ts AS c_ts FROM t__base_view_1 s, LATERAL (SELECT s.c_pk AS c_pk, s.c_int AS c_int, s.c_big AS c_big, s.c_dec AS c_dec, s.c_dbl AS c_dbl, s.c_txt AS c_txt, s.c_chr AS c_chr, s.c_date AS c_date, s.c_ts AS c_ts) AS l;
ALTER TABLE t__base_table_3 ADD PRIMARY KEY (c_pk);
CREATE VIEW t__base_view_2 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_table_3;
CREATE TABLE t__base_table_4 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts, ROW_NUMBER() OVER (ORDER BY c_pk) AS eq_dg_1 FROM t__base_view_2;
CREATE TABLE t__base_table_5 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_table_4 GROUP BY c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts, eq_dg_1;
CREATE INDEX t0_idx ON t__base_table_5 USING hash (c_pk);
ANALYZE t__base_table_5;
CREATE VIEW t0 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_table_5;
CREATE TABLE t__base_table_6 AS SELECT l.c_pk AS c_pk, l.c_int AS c_int, l.c_big AS c_big, l.c_dec AS c_dec, l.c_dbl AS c_dbl, l.c_txt AS c_txt, l.c_chr AS c_chr, l.c_date AS c_date, l.c_ts AS c_ts FROM t__base s, LATERAL (SELECT s.c_pk AS c_pk, s.c_int AS c_int, s.c_big AS c_big, s.c_dec AS c_dec, s.c_dbl AS c_dbl, s.c_txt AS c_txt, s.c_chr AS c_chr, s.c_date AS c_date, s.c_ts AS c_ts) AS l;
ALTER TABLE t__base_table_6 ADD PRIMARY KEY (c_pk);
CREATE VIEW t__base_view_3 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_table_6;
CREATE TEMPORARY VIEW t__base_view_4 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_view_3;
CREATE TEMPORARY TABLE t__base_table_7 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts, ROW_NUMBER() OVER (ORDER BY c_pk) AS eq_seq_1 FROM t__base_view_4;
CREATE VIEW t__base_view_5 AS SELECT c_pk, c_int, c_big, c_dec, eq_seq_1 FROM t__base_table_7;
CREATE VIEW t__base_view_6 AS SELECT c_dbl, c_txt, c_chr, c_date, c_ts, eq_seq_1 FROM t__base_table_7;
CREATE TEMPORARY TABLE t__base_table_8 AS SELECT l.c_pk AS c_pk, l.c_int AS c_int, l.c_big AS c_big, l.c_dec AS c_dec, r.c_dbl AS c_dbl, r.c_txt AS c_txt, r.c_chr AS c_chr, r.c_date AS c_date, r.c_ts AS c_ts FROM t__base_view_5 l FULL OUTER JOIN t__base_view_6 r ON l.eq_seq_1 = r.eq_seq_1;
CREATE TABLE t__base_table_9 AS SELECT * FROM t__base_table_8 WHERE ((MOD(c_pk, 2) = 0) AND (MOD(c_pk, 2) = 0)) AND (((c_int = 3) OR (NOT (c_int = 3))) OR ((c_int = 3) IS NULL));
CREATE STATISTICS t__base_view_7_st (ndistinct, dependencies, mcv) ON c_pk, c_int FROM t__base_table_9;
ANALYZE t__base_table_9;
CREATE VIEW t__base_view_7 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_table_9;
CREATE TEMPORARY VIEW t__base_view_8 AS SELECT * FROM t__base;
CREATE TABLE t__base_table_10 AS SELECT * FROM t__base_view_8 WHERE MOD(c_pk, 2) = 0;
ALTER TABLE t__base_table_10 ADD PRIMARY KEY (c_pk);
CREATE VIEW t__base_view_9 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_table_10;
CREATE TABLE t__base_table_11 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND (c_ts <> '1999-12-31 23:59:59');
CREATE TABLE t__base_table_12 AS SELECT * FROM t__base WHERE ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND (NOT (c_ts <> '1999-12-31 23:59:59'));
CREATE INDEX t__base_view_10_idx ON t__base_table_12 USING btree (c_pk);
ANALYZE t__base_table_12;
CREATE VIEW t__base_view_10 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_table_12;
CREATE TABLE t__base_table_13 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND ((c_ts <> '1999-12-31 23:59:59') IS NULL);
CREATE MATERIALIZED VIEW t__base_view_11 AS SELECT * FROM t__base_table_13;
CREATE UNLOGGED TABLE t__base_table_14 AS SELECT * FROM t__base_table_11 UNION ALL SELECT * FROM t__base_view_10 UNION ALL SELECT * FROM t__base_view_11;
CREATE TABLE t__base_table_15 AS SELECT * FROM t__base_view_9 UNION ALL SELECT * FROM t__base_table_14;
CREATE VIEW t__base_view_12 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM (SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts, ((ROW_NUMBER() OVER (ORDER BY c_pk)) >= 1) AS eq_q FROM t__base_table_15) AS eq_qsrc WHERE eq_q;
CREATE VIEW t__base_view_13 AS WITH t__base_cte_2 AS (SELECT * FROM t__base_view_12 WHERE (MOD(c_pk, 2) = 0) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL))) SELECT * FROM t__base_cte_2;
CREATE VIEW t__base_view_14 AS SELECT * FROM t__base_view_7 UNION ALL SELECT * FROM t__base_view_13;
CREATE TABLE t__base_table_16 AS SELECT * FROM t__base WHERE (INITCAP(c_chr) = 'trailing ') AND (((((INITCAP(c_chr) <= 'trailing ') OR (c_date = '1999-12-31')) AND (c_pk = c_dec)) OR (NOT (((INITCAP(c_chr) <= 'trailing ') OR (c_date = '1999-12-31')) AND (c_pk = c_dec)))) OR ((((INITCAP(c_chr) <= 'trailing ') OR (c_date = '1999-12-31')) AND (c_pk = c_dec)) IS NULL));
CREATE INDEX t__base_view_15_idx ON t__base_table_16 USING btree ((lower(c_txt)));
ANALYZE t__base_table_16;
CREATE VIEW t__base_view_15 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_table_16;
CREATE TABLE t__base_table_17 AS SELECT * FROM t__base;
CREATE INDEX t__base_view_16_idx ON t__base_table_17 USING btree (c_pk);
ANALYZE t__base_table_17;
CREATE VIEW t__base_view_16 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_table_17;
CREATE VIEW t__base_view_17 WITH (security_barrier = true) AS SELECT * FROM t__base_view_16 WHERE (NOT (INITCAP(c_chr) = 'trailing ')) AND (((NOT (NOT (c_chr BETWEEN '' AND 'trailing '))) OR (NOT (NOT (NOT (c_chr BETWEEN '' AND 'trailing '))))) OR ((NOT (NOT (c_chr BETWEEN '' AND 'trailing '))) IS NULL));
CREATE TABLE t__base_table_18 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE (INITCAP(c_chr) = 'trailing ') IS NULL;
CREATE INDEX t__base_view_18_idx ON t__base_table_18 USING brin (c_pk);
ANALYZE t__base_table_18;
CREATE VIEW t__base_view_18 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_table_18;
CREATE TABLE t__base_table_19 AS SELECT * FROM t__base_view_15 UNION ALL SELECT * FROM t__base_view_17 UNION ALL SELECT * FROM t__base_view_18;
CREATE TABLE t__base_table_20 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM (SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base ORDER BY c_pk) AS eq_ord;
ALTER TABLE t__base_table_20 ADD PRIMARY KEY (c_pk);
CREATE VIEW t__base_view_19 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_table_20;
CREATE TABLE t__base_table_21 AS SELECT CASE WHEN ((LEAST(c_dbl, c_dbl) >= -0.25) OR (NOT (LEAST(c_dbl, c_dbl) >= -0.25))) OR ((LEAST(c_dbl, c_dbl) >= -0.25) IS NULL) THEN c_pk ELSE CAST(NULL AS INTEGER) END AS c_pk, CASE WHEN (((GREATEST(c_txt, 'o''brien') > 'o''brien') AND (LEAST(c_dbl, c_dbl) <= 1.5)) OR (NOT ((GREATEST(c_txt, 'o''brien') > 'o''brien') AND (LEAST(c_dbl, c_dbl) <= 1.5)))) OR (((GREATEST(c_txt, 'o''brien') > 'o''brien') AND (LEAST(c_dbl, c_dbl) <= 1.5)) IS NULL) THEN c_int ELSE CAST(NULL AS INTEGER) END AS c_int, CASE WHEN ((FLOOR(c_dec) > 0.00) OR (NOT (FLOOR(c_dec) > 0.00))) OR ((FLOOR(c_dec) > 0.00) IS NULL) THEN c_big ELSE CAST(NULL AS NUMERIC(38, 0)) END AS c_big, CASE WHEN ((c_dec BETWEEN -5.50 AND 12.34) OR (NOT (c_dec BETWEEN -5.50 AND 12.34))) OR ((c_dec BETWEEN -5.50 AND 12.34) IS NULL) THEN c_dec ELSE CAST(NULL AS NUMERIC(10, 2)) END AS c_dec, CASE WHEN ((c_ts IS NOT NULL) OR (NOT (c_ts IS NOT NULL))) OR ((c_ts IS NOT NULL) IS NULL) THEN c_dbl ELSE CAST(NULL AS DOUBLE PRECISION) END AS c_dbl, CASE WHEN (((NOT (LEAST(c_dbl, c_dbl) <> 1.5)) OR (c_dec BETWEEN 999.99 AND -5.50)) OR (NOT ((NOT (LEAST(c_dbl, c_dbl) <> 1.5)) OR (c_dec BETWEEN 999.99 AND -5.50)))) OR (((NOT (LEAST(c_dbl, c_dbl) <> 1.5)) OR (c_dec BETWEEN 999.99 AND -5.50)) IS NULL) THEN c_txt ELSE CAST(NULL AS TEXT) END AS c_txt, CASE WHEN (((c_int < 2) OR (c_date >= '2030-06-01')) OR (NOT ((c_int < 2) OR (c_date >= '2030-06-01')))) OR (((c_int < 2) OR (c_date >= '2030-06-01')) IS NULL) THEN c_chr ELSE CAST(NULL AS TEXT) END AS c_chr, CASE WHEN ((((c_dbl IS NOT DISTINCT FROM 0.0) OR (ABS(c_dec) < -5.50)) AND (c_ts IS NULL)) OR (NOT (((c_dbl IS NOT DISTINCT FROM 0.0) OR (ABS(c_dec) < -5.50)) AND (c_ts IS NULL)))) OR ((((c_dbl IS NOT DISTINCT FROM 0.0) OR (ABS(c_dec) < -5.50)) AND (c_ts IS NULL)) IS NULL) THEN c_date ELSE CAST(NULL AS DATE) END AS c_date, CASE WHEN ((LEAST(c_txt, c_txt) > '') OR (NOT (LEAST(c_txt, c_txt) > ''))) OR ((LEAST(c_txt, c_txt) > '') IS NULL) THEN c_ts ELSE CAST(NULL AS TIMESTAMP) END AS c_ts FROM t__base_view_19;
CREATE INDEX t__base_view_20_idx ON t__base_table_21 USING brin (c_pk);
ANALYZE t__base_table_21;
CREATE VIEW t__base_view_20 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_table_21;
CREATE TEMPORARY TABLE t__base_table_22 AS SELECT * FROM t__base_view_20 WHERE 1 = 0;
CREATE TABLE t__base_table_23 AS SELECT l.c_pk AS c_pk, l.c_int AS c_int, l.c_big AS c_big, l.c_dec AS c_dec, l.c_dbl AS c_dbl, l.c_txt AS c_txt, l.c_chr AS c_chr, l.c_date AS c_date, l.c_ts AS c_ts FROM t__base_table_19 l LEFT OUTER JOIN t__base_table_22 r ON 1 = 1;
ALTER TABLE t__base_table_23 ADD PRIMARY KEY (c_pk);
CREATE VIEW t__base_view_21 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_table_23;
CREATE TABLE t__base_table_24 AS SELECT * FROM t__base_view_21 WHERE ((c_dec IN (-5.50, 0.00, 999.99)) OR (NOT (c_dec IN (-5.50, 0.00, 999.99)))) OR ((c_dec IN (-5.50, 0.00, 999.99)) IS NULL);
CREATE TEMPORARY TABLE t__base_table_25 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_table_24 WHERE (MOD(c_pk, 2) <> 0) OR (c_pk IS NULL);
CREATE UNLOGGED TABLE t1 AS SELECT * FROM t__base_view_14 UNION ALL SELECT * FROM t__base_table_25;
CREATE TABLE t__base_table_26 AS SELECT CASE WHEN (((NOT (c_date IS NOT NULL)) AND ((c_dbl <> 1000.125) OR (c_txt <= 'o''brien'))) OR (NOT ((NOT (c_date IS NOT NULL)) AND ((c_dbl <> 1000.125) OR (c_txt <= 'o''brien'))))) OR (((NOT (c_date IS NOT NULL)) AND ((c_dbl <> 1000.125) OR (c_txt <= 'o''brien'))) IS NULL) THEN c_pk ELSE CAST(NULL AS INTEGER) END AS c_pk, CASE WHEN ((c_pk IN (0, 42, 42)) OR (NOT (c_pk IN (0, 42, 42)))) OR ((c_pk IN (0, 42, 42)) IS NULL) THEN c_int ELSE CAST(NULL AS INTEGER) END AS c_int, CASE WHEN ((c_dbl NOT BETWEEN -0.25 AND 1000.125) OR (NOT (c_dbl NOT BETWEEN -0.25 AND 1000.125))) OR ((c_dbl NOT BETWEEN -0.25 AND 1000.125) IS NULL) THEN c_big ELSE CAST(NULL AS NUMERIC(38, 0)) END AS c_big, CASE WHEN ((ABS(c_dec) <= -5.50) OR (NOT (ABS(c_dec) <= -5.50))) OR ((ABS(c_dec) <= -5.50) IS NULL) THEN c_dec ELSE CAST(NULL AS NUMERIC(10, 2)) END AS c_dec, CASE WHEN ((c_int IS DISTINCT FROM 2) OR (NOT (c_int IS DISTINCT FROM 2))) OR ((c_int IS DISTINCT FROM 2) IS NULL) THEN c_dbl ELSE CAST(NULL AS DOUBLE PRECISION) END AS c_dbl, CASE WHEN ((NOT ((GREATEST(c_dbl, c_dbl) <= -0.25) AND (UPPER(c_chr) <> 'o''brien'))) OR (NOT (NOT ((GREATEST(c_dbl, c_dbl) <= -0.25) AND (UPPER(c_chr) <> 'o''brien'))))) OR ((NOT ((GREATEST(c_dbl, c_dbl) <= -0.25) AND (UPPER(c_chr) <> 'o''brien'))) IS NULL) THEN c_txt ELSE CAST(NULL AS TEXT) END AS c_txt, CASE WHEN ((FLOOR(c_dbl) >= 0.0) OR (NOT (FLOOR(c_dbl) >= 0.0))) OR ((FLOOR(c_dbl) >= 0.0) IS NULL) THEN c_chr ELSE CAST(NULL AS TEXT) END AS c_chr, CASE WHEN ((c_txt LIKE '%') OR (NOT (c_txt LIKE '%'))) OR ((c_txt LIKE '%') IS NULL) THEN c_date ELSE CAST(NULL AS DATE) END AS c_date, CASE WHEN ((NOT ((c_txt BETWEEN 'a' AND 'o''brien') AND (c_big BETWEEN 2 AND 1000))) OR (NOT (NOT ((c_txt BETWEEN 'a' AND 'o''brien') AND (c_big BETWEEN 2 AND 1000))))) OR ((NOT ((c_txt BETWEEN 'a' AND 'o''brien') AND (c_big BETWEEN 2 AND 1000))) IS NULL) THEN c_ts ELSE CAST(NULL AS TIMESTAMP) END AS c_ts FROM t__base;
ALTER TABLE t__base_table_26 ADD PRIMARY KEY (c_pk);
CREATE VIEW t__base_view_22 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_table_26;
CREATE VIEW t__base_view_23 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts, CAST(NULL AS INTEGER) AS eq_tmp_col_1 FROM t__base_view_22;
CREATE TEMPORARY TABLE t__base_table_27 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_view_23;
CREATE UNLOGGED TABLE t__base_table_28 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts, ROW_NUMBER() OVER (ORDER BY c_pk) AS eq_key_2 FROM t__base_table_27;
CREATE TABLE t__base_table_29 AS SELECT * FROM t__base_table_28 UNION ALL SELECT * FROM t__base_table_28;
CREATE VIEW t__base_view_24 AS SELECT DISTINCT eq_key_2, MAX(c_pk) OVER (PARTITION BY eq_key_2) AS c_pk, MAX(c_int) OVER (PARTITION BY eq_key_2) AS c_int, MAX(c_big) OVER (PARTITION BY eq_key_2) AS c_big, MAX(c_dec) OVER (PARTITION BY eq_key_2) AS c_dec, MAX(c_dbl) OVER (PARTITION BY eq_key_2) AS c_dbl, MAX(c_txt) OVER (PARTITION BY eq_key_2) AS c_txt, MAX(c_chr) OVER (PARTITION BY eq_key_2) AS c_chr, MAX(c_date) OVER (PARTITION BY eq_key_2) AS c_date, MAX(c_ts) OVER (PARTITION BY eq_key_2) AS c_ts FROM t__base_table_29;
CREATE VIEW t2 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_view_24;

-- ============ the query, run against each ============
SELECT DISTINCT COVAR_POP(LENGTH(t1.c_chr), COTD(t2.c_big)), COTD(t1.c_int) FROM t0, t1 LEFT  JOIN t2 ON ((t1.c_big)!=(t1.c_pk)) GROUP BY COTD(t1.c_int);

-- ============ mismatch results ============
-- only in base (1 distinct row(s), 1 row(s) counting multiplicity):
--   ×1 (('__nan__',), 1.1106125148291928)
-- only in equivalent (1 distinct row(s), 1 row(s) counting multiplicity):
--   ×1 (0.0, 1.1106125148291928)
