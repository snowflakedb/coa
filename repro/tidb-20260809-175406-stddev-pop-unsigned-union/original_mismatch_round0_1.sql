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
-- engine: tidb 8.0.11-TiDB-v8.5.0 (docker pingcap/tidb:v8.5.0, 127.0.0.1:37591)
-- seed: 877201401
-- sql_mode: STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES
-- collation: utf8mb4_0900_bin
-- character_set: utf8mb4
-- mismatch: 1 distinct only in base, 1 distinct only in equivalent

-- ============ database 1: the base table ============
CREATE TABLE t (c_pk BIGINT NOT NULL, c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10, 2), c_dbl DOUBLE, c_txt VARCHAR(255), c_chr VARCHAR(255), c_date DATE, c_ts DATETIME(6));
INSERT INTO t VALUES (1, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);
INSERT INTO t VALUES (2, 42, 1, 0.0, 1.5, 'o''brien', NULL, '1999-12-31', NULL);
INSERT INTO t VALUES (3, NULL, -1, NULL, 1.5, 'Zed', 'Zed', NULL, '1999-12-31 23:59:59');
INSERT INTO t VALUES (4, -7, -1, 999.99, 0.0, '', 'abc', NULL, NULL);
INSERT INTO t VALUES (5, 42, 2, 12.34, -1.5, 'abc', 'Zed', '1999-12-31', NULL);
INSERT INTO t VALUES (6, NULL, -7, 12.34, NULL, NULL, 'abc', '1999-12-31', '1999-12-31 23:59:59');
INSERT INTO t VALUES (7, 0, 2, NULL, 0.0, 'abc', 'o''brien', NULL, NULL);
INSERT INTO t VALUES (8, 42, 1, 0.0, 1.5, 'o''brien', NULL, '1999-12-31', NULL);
CREATE TABLE t0 LIKE t;
INSERT INTO t0 SELECT * FROM t;
CREATE TABLE t1 LIKE t;
INSERT INTO t1 SELECT * FROM t;
CREATE TABLE t2 LIKE t;
INSERT INTO t2 SELECT * FROM t;

-- ============ database 2: the equivalent ============
CREATE TABLE t (c_pk BIGINT NOT NULL, c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10, 2), c_dbl DOUBLE, c_txt VARCHAR(255), c_chr VARCHAR(255), c_date DATE, c_ts DATETIME(6));
INSERT INTO t VALUES (1, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);
INSERT INTO t VALUES (2, 42, 1, 0.0, 1.5, 'o''brien', NULL, '1999-12-31', NULL);
INSERT INTO t VALUES (3, NULL, -1, NULL, 1.5, 'Zed', 'Zed', NULL, '1999-12-31 23:59:59');
INSERT INTO t VALUES (4, -7, -1, 999.99, 0.0, '', 'abc', NULL, NULL);
INSERT INTO t VALUES (5, 42, 2, 12.34, -1.5, 'abc', 'Zed', '1999-12-31', NULL);
INSERT INTO t VALUES (6, NULL, -7, 12.34, NULL, NULL, 'abc', '1999-12-31', '1999-12-31 23:59:59');
INSERT INTO t VALUES (7, 0, 2, NULL, 0.0, 'abc', 'o''brien', NULL, NULL);
INSERT INTO t VALUES (8, 42, 1, 0.0, 1.5, 'o''brien', NULL, '1999-12-31', NULL);
ALTER TABLE t RENAME TO t__base;
CREATE VIEW t__base_view_1 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE (GREATEST(c_txt, 'Zed') > 'a');
CREATE VIEW t__base_view_2 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE NOT (GREATEST(c_txt, 'Zed') > 'a');
CREATE VIEW t__base_view_3 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE (GREATEST(c_txt, 'Zed') > 'a') IS NULL;
CREATE VIEW t__base_view_4 AS SELECT * FROM t__base_view_1 UNION ALL SELECT * FROM t__base_view_2 UNION ALL SELECT * FROM t__base_view_3;
CREATE TABLE t__base_table_1 (c_pk BIGINT, c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10, 2), c_dbl DOUBLE, c_txt VARCHAR(255), c_chr VARCHAR(255), c_date DATE, c_ts DATETIME(6), eq_dg_1 BIGINT);
INSERT INTO t__base_table_1 (c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts, eq_dg_1) SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts, ROW_NUMBER() OVER (ORDER BY c_pk) AS eq_dg_1 FROM t__base_view_4;
CREATE TABLE t__base_table_2 (c_pk BIGINT, c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10, 2), c_dbl DOUBLE, c_txt VARCHAR(255), c_chr VARCHAR(255), c_date DATE, c_ts DATETIME(6));
INSERT INTO t__base_table_2 (c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts) SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_table_1 GROUP BY c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts, eq_dg_1;
CREATE TABLE t__base_view_5_tbl (c_pk BIGINT, c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10, 2), c_dbl DOUBLE, c_txt VARCHAR(255), c_chr VARCHAR(255), c_date DATE, c_ts DATETIME(6));
INSERT INTO t__base_view_5_tbl (c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts) SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_table_2;
ALTER TABLE t__base_view_5_tbl CACHE;
CREATE VIEW t__base_view_5 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_view_5_tbl;
CREATE VIEW t__base_view_6 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM (SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts, ((ROW_NUMBER() OVER (ORDER BY c_pk)) >= 1) AS eq_q FROM t__base_view_5) AS eq_qsrc WHERE eq_q;
CREATE TABLE t__base_table_3 (c_pk BIGINT, c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10, 2), c_dbl DOUBLE, c_txt VARCHAR(255), c_chr VARCHAR(255), c_date DATE, c_ts DATETIME(6), eq_uid_1 BIGINT);
INSERT INTO t__base_table_3 (c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts, eq_uid_1) SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts, ROW_NUMBER() OVER (ORDER BY c_pk) AS eq_uid_1 FROM t__base_view_6;
CREATE TABLE t__base_table_4 (eq_uid_1 BIGINT, eq_flag_1 BIGINT);
INSERT INTO t__base_table_4 (eq_uid_1, eq_flag_1) SELECT eq_uid_1, 1 AS eq_flag_1 FROM t__base_table_3;
CREATE TABLE t0 (c_pk BIGINT, c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10, 2), c_dbl DOUBLE, c_txt VARCHAR(255), c_chr VARCHAR(255), c_date DATE, c_ts DATETIME(6));
INSERT INTO t0 (c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts) SELECT l.c_pk AS c_pk, l.c_int AS c_int, l.c_big AS c_big, l.c_dec AS c_dec, l.c_dbl AS c_dbl, l.c_txt AS c_txt, l.c_chr AS c_chr, l.c_date AS c_date, l.c_ts AS c_ts FROM t__base_table_3 l INNER JOIN t__base_table_4 r ON l.eq_uid_1 = r.eq_uid_1 WHERE r.eq_flag_1 = 1;
CREATE TABLE t__base_table_5 (c_pk BIGINT, c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10, 2), c_dbl DOUBLE, c_txt VARCHAR(255), c_chr VARCHAR(255), c_date DATE, c_ts DATETIME(6), eq_seq_1 BIGINT);
INSERT INTO t__base_table_5 (c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts, eq_seq_1) SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts, ROW_NUMBER() OVER (ORDER BY c_pk) AS eq_seq_1 FROM t__base;
CREATE VIEW t__base_view_7 AS SELECT c_pk, c_int, c_big, c_dec, eq_seq_1 FROM t__base_table_5;
CREATE VIEW t__base_view_8 AS SELECT c_dbl, c_txt, c_chr, c_date, c_ts, eq_seq_1 FROM t__base_table_5;
CREATE VIEW t__base_view_9 AS SELECT l.c_pk AS c_pk, l.c_int AS c_int, l.c_big AS c_big, l.c_dec AS c_dec, r.c_dbl AS c_dbl, r.c_txt AS c_txt, r.c_chr AS c_chr, r.c_date AS c_date, r.c_ts AS c_ts FROM t__base_view_7 l LEFT OUTER JOIN t__base_view_8 r ON l.eq_seq_1 = r.eq_seq_1;
CREATE TABLE t__base_table_6 (c_pk BIGINT, c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10, 2), c_dbl DOUBLE, c_txt VARCHAR(255), c_chr VARCHAR(255), c_date DATE, c_ts DATETIME(6));
INSERT INTO t__base_table_6 (c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts) SELECT * FROM t__base_view_9 WHERE (MOD(c_pk, 2) = 0) AND (((NOT (NOT (GREATEST(c_txt, c_txt) > 'a'))) OR (NOT (NOT (NOT (GREATEST(c_txt, c_txt) > 'a'))))) OR ((NOT (NOT (GREATEST(c_txt, c_txt) > 'a'))) IS NULL));
CREATE TABLE t__base_table_7 (c_pk BIGINT, c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10, 2), c_dbl DOUBLE, c_txt VARCHAR(255), c_chr VARCHAR(255), c_date DATE, c_ts DATETIME(6));
INSERT INTO t__base_table_7 (c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts) SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND (MOD(c_pk, 2) = 0)) AND (MOD(c_pk, 2) = 0)) AND (((c_chr < 'trailing ') AND (LEAST(c_chr, c_txt) < 'Zed')) AND (GREATEST(c_dec, c_dec) <= -5.50));
CREATE VIEW t__base_view_10 AS SELECT * FROM t__base WHERE (((((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND (MOD(c_pk, 2) = 0)) AND (MOD(c_pk, 2) = 0)) AND (NOT (((c_chr < 'trailing ') AND (LEAST(c_chr, c_txt) < 'Zed')) AND (GREATEST(c_dec, c_dec) <= -5.50)))) AND (((LEAST(c_dec, c_big) >= 12.34) OR (NOT (LEAST(c_dec, c_big) >= 12.34))) OR ((LEAST(c_dec, c_big) >= 12.34) IS NULL));
CREATE TABLE t__base_table_8 (c_pk BIGINT, c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10, 2), c_dbl DOUBLE, c_txt VARCHAR(255), c_chr VARCHAR(255), c_date DATE, c_ts DATETIME(6));
INSERT INTO t__base_table_8 (c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts) SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND (MOD(c_pk, 2) = 0)) AND (MOD(c_pk, 2) = 0)) AND ((((c_chr < 'trailing ') AND (LEAST(c_chr, c_txt) < 'Zed')) AND (GREATEST(c_dec, c_dec) <= -5.50)) IS NULL);
CREATE TABLE t__base_view_11_tbl (c_pk BIGINT, c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10, 2), c_dbl DOUBLE, c_txt VARCHAR(255), c_chr VARCHAR(255), c_date DATE, c_ts DATETIME(6));
INSERT INTO t__base_view_11_tbl (c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts) SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_table_8;
ALTER TABLE t__base_view_11_tbl CACHE;
CREATE VIEW t__base_view_11 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_view_11_tbl;
CREATE TABLE t__base_table_9 (c_pk BIGINT, c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10, 2), c_dbl DOUBLE, c_txt VARCHAR(255), c_chr VARCHAR(255), c_date DATE, c_ts DATETIME(6));
INSERT INTO t__base_table_9 (c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts) SELECT * FROM t__base_table_7 UNION ALL SELECT * FROM t__base_view_10 UNION ALL SELECT * FROM t__base_view_11;
CREATE VIEW t__base_view_12 AS SELECT * FROM t__base WHERE (((((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND (MOD(c_pk, 2) = 0)) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL))) AND ((ABS(c_dec) = 0.00) OR ((GREATEST(c_txt, c_chr) >= 'abc') OR (c_date IS NULL)))) AND ((((FLOOR(c_big) > -7) OR (c_dec < 999.99)) OR (NOT ((FLOOR(c_big) > -7) OR (c_dec < 999.99)))) OR (((FLOOR(c_big) > -7) OR (c_dec < 999.99)) IS NULL));
CREATE TABLE t__base_table_10 (c_pk BIGINT, c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10, 2), c_dbl DOUBLE, c_txt VARCHAR(255), c_chr VARCHAR(255), c_date DATE, c_ts DATETIME(6));
INSERT INTO t__base_table_10 (c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts) SELECT * FROM t__base WHERE (((((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND (MOD(c_pk, 2) = 0)) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL))) AND (NOT ((ABS(c_dec) = 0.00) OR ((GREATEST(c_txt, c_chr) >= 'abc') OR (c_date IS NULL))))) AND (((FLOOR(c_pk) <> 0) OR (NOT (FLOOR(c_pk) <> 0))) OR ((FLOOR(c_pk) <> 0) IS NULL));
CREATE TABLE t__base_view_13_tbl (c_pk BIGINT, c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10, 2), c_dbl DOUBLE, c_txt VARCHAR(255), c_chr VARCHAR(255), c_date DATE, c_ts DATETIME(6));
INSERT INTO t__base_view_13_tbl (c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts) SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_table_10;
ALTER TABLE t__base_view_13_tbl CACHE;
CREATE VIEW t__base_view_13 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_view_13_tbl;
CREATE TABLE t__base_table_11 (c_pk BIGINT, c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10, 2), c_dbl DOUBLE, c_txt VARCHAR(255), c_chr VARCHAR(255), c_date DATE, c_ts DATETIME(6));
INSERT INTO t__base_table_11 (c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts) SELECT * FROM t__base WHERE (((((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND (MOD(c_pk, 2) = 0)) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL))) AND (((ABS(c_dec) = 0.00) OR ((GREATEST(c_txt, c_chr) >= 'abc') OR (c_date IS NULL))) IS NULL)) AND ((((LOWER(c_txt) <= 'o''brien') AND ((c_big > c_pk) AND (c_dec > 0.00))) OR (NOT ((LOWER(c_txt) <= 'o''brien') AND ((c_big > c_pk) AND (c_dec > 0.00))))) OR (((LOWER(c_txt) <= 'o''brien') AND ((c_big > c_pk) AND (c_dec > 0.00))) IS NULL));
CREATE VIEW t__base_view_14 AS SELECT * FROM t__base_view_12 UNION ALL SELECT * FROM t__base_view_13 UNION ALL SELECT * FROM t__base_table_11;
CREATE TABLE t__base_table_12 (c_pk BIGINT, c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10, 2), c_dbl DOUBLE, c_txt VARCHAR(255), c_chr VARCHAR(255), c_date DATE, c_ts DATETIME(6));
INSERT INTO t__base_table_12 (c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts) SELECT * FROM t__base_table_9 UNION ALL SELECT * FROM t__base_view_14;
CREATE VIEW t__base_view_15 AS SELECT * FROM t__base WHERE ((c_dbl NOT BETWEEN 1000.125 AND -0.25) OR (NOT (c_dbl NOT BETWEEN 1000.125 AND -0.25))) OR ((c_dbl NOT BETWEEN 1000.125 AND -0.25) IS NULL);
CREATE TABLE t__base_table_13 (c_pk BIGINT, c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10, 2), c_dbl DOUBLE, c_txt VARCHAR(255), c_chr VARCHAR(255), c_date DATE, c_ts DATETIME(6));
INSERT INTO t__base_table_13 (c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts) SELECT CASE WHEN ((c_dbl IS NOT NULL) OR (NOT (c_dbl IS NOT NULL))) OR ((c_dbl IS NOT NULL) IS NULL) THEN c_pk ELSE CAST(NULL AS SIGNED) END AS c_pk, CASE WHEN ((c_big NOT IN (-7)) OR (NOT (c_big NOT IN (-7)))) OR ((c_big NOT IN (-7)) IS NULL) THEN c_int ELSE CAST(NULL AS SIGNED) END AS c_int, CASE WHEN (((c_int >= c_big) OR (NOT (LEAST(c_chr, c_chr) < 'o''brien'))) OR (NOT ((c_int >= c_big) OR (NOT (LEAST(c_chr, c_chr) < 'o''brien'))))) OR (((c_int >= c_big) OR (NOT (LEAST(c_chr, c_chr) < 'o''brien'))) IS NULL) THEN c_big ELSE CAST(NULL AS SIGNED) END AS c_big, CASE WHEN ((c_dbl NOT IN (1000.125)) OR (NOT (c_dbl NOT IN (1000.125)))) OR ((c_dbl NOT IN (1000.125)) IS NULL) THEN c_dec ELSE CAST(NULL AS DECIMAL(10, 2)) END AS c_dec, CASE WHEN (((c_int BETWEEN 42 AND 42) AND ((c_pk <=> 3))) OR (NOT ((c_int BETWEEN 42 AND 42) AND ((c_pk <=> 3))))) OR (((c_int BETWEEN 42 AND 42) AND ((c_pk <=> 3))) IS NULL) THEN c_dbl ELSE CAST(NULL AS DOUBLE) END AS c_dbl, CASE WHEN ((GREATEST(c_chr, 'o''brien') <= 'trailing ') OR (NOT (GREATEST(c_chr, 'o''brien') <= 'trailing '))) OR ((GREATEST(c_chr, 'o''brien') <= 'trailing ') IS NULL) THEN c_txt ELSE CAST(NULL AS CHAR(255)) END AS c_txt, CASE WHEN ((((c_chr <=> c_txt)) AND (NOT (c_txt = c_chr))) OR (NOT (((c_chr <=> c_txt)) AND (NOT (c_txt = c_chr))))) OR ((((c_chr <=> c_txt)) AND (NOT (c_txt = c_chr))) IS NULL) THEN c_chr ELSE CAST(NULL AS CHAR(255)) END AS c_chr, CASE WHEN ((NOT ((SIGN(c_int) > 2) OR (LOWER(c_txt) > 'abc'))) OR (NOT (NOT ((SIGN(c_int) > 2) OR (LOWER(c_txt) > 'abc'))))) OR ((NOT ((SIGN(c_int) > 2) OR (LOWER(c_txt) > 'abc'))) IS NULL) THEN c_date ELSE CAST(NULL AS DATE) END AS c_date, CASE WHEN ((c_dbl IS NOT NULL) OR (NOT (c_dbl IS NOT NULL))) OR ((c_dbl IS NOT NULL) IS NULL) THEN c_ts ELSE CAST(NULL AS DATETIME(6)) END AS c_ts FROM t__base_view_15;
CREATE TABLE t__base_table_14 (c_pk BIGINT, c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10, 2), c_dbl DOUBLE, c_txt VARCHAR(255), c_chr VARCHAR(255), c_date DATE, c_ts DATETIME(6));
INSERT INTO t__base_table_14 (c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts) SELECT * FROM t__base_table_13 WHERE (((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL))) AND (((((FLOOR(c_big) = 0) AND (c_pk = c_int)) AND ((SIGN(c_dbl) <= 3) AND (ASCII(c_chr) = -7))) OR (NOT (((FLOOR(c_big) = 0) AND (c_pk = c_int)) AND ((SIGN(c_dbl) <= 3) AND (ASCII(c_chr) = -7))))) OR ((((FLOOR(c_big) = 0) AND (c_pk = c_int)) AND ((SIGN(c_dbl) <= 3) AND (ASCII(c_chr) = -7))) IS NULL));
CREATE VIEW t__base_view_16 AS SELECT * FROM t__base_table_12 UNION ALL SELECT * FROM t__base_table_14;
CREATE VIEW t__base_view_17 AS SELECT * FROM t__base_table_6 UNION ALL SELECT * FROM t__base_view_16;
CREATE TABLE t__base_table_15 (c_pk BIGINT, c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10, 2), c_dbl DOUBLE, c_txt VARCHAR(255), c_chr VARCHAR(255), c_date DATE, c_ts DATETIME(6), eq_rank_1 BIGINT);
INSERT INTO t__base_table_15 (c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts, eq_rank_1) SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts, ROW_NUMBER() OVER (ORDER BY c_pk) AS eq_rank_1 FROM t__base_view_17;
CREATE VIEW t__base_view_18 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_table_15 WHERE MOD(eq_rank_1, 4) = 0;
CREATE VIEW t__base_view_19 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_table_15 WHERE MOD(eq_rank_1, 4) = 1;
CREATE VIEW t__base_view_20 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_table_15 WHERE MOD(eq_rank_1, 4) = 2;
CREATE VIEW t__base_view_21 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_table_15 WHERE MOD(eq_rank_1, 4) = 3;
CREATE VIEW t1 AS SELECT * FROM t__base_view_18 UNION ALL SELECT * FROM t__base_view_19 UNION ALL SELECT * FROM t__base_view_20 UNION ALL SELECT * FROM t__base_view_21;
CREATE VIEW t__base_view_22 AS SELECT CASE WHEN ((c_chr IS NULL) OR (NOT (c_chr IS NULL))) OR ((c_chr IS NULL) IS NULL) THEN c_pk ELSE CAST(NULL AS SIGNED) END AS c_pk, CASE WHEN (((c_int >= c_big) OR ((c_big < 0) OR (c_big IN (3, 1, 42)))) OR (NOT ((c_int >= c_big) OR ((c_big < 0) OR (c_big IN (3, 1, 42)))))) OR (((c_int >= c_big) OR ((c_big < 0) OR (c_big IN (3, 1, 42)))) IS NULL) THEN c_int ELSE CAST(NULL AS SIGNED) END AS c_int, CASE WHEN (((ABS(c_big) >= -7) OR ((c_chr BETWEEN 'Zed' AND 'Zed') OR (c_dbl <= 0.0))) OR (NOT ((ABS(c_big) >= -7) OR ((c_chr BETWEEN 'Zed' AND 'Zed') OR (c_dbl <= 0.0))))) OR (((ABS(c_big) >= -7) OR ((c_chr BETWEEN 'Zed' AND 'Zed') OR (c_dbl <= 0.0))) IS NULL) THEN c_big ELSE CAST(NULL AS SIGNED) END AS c_big, CASE WHEN ((((c_txt > 'o''brien') AND (c_date < '1999-12-31')) OR (NOT (c_ts >= '2024-01-15 12:34:56'))) OR (NOT (((c_txt > 'o''brien') AND (c_date < '1999-12-31')) OR (NOT (c_ts >= '2024-01-15 12:34:56'))))) OR ((((c_txt > 'o''brien') AND (c_date < '1999-12-31')) OR (NOT (c_ts >= '2024-01-15 12:34:56'))) IS NULL) THEN c_dec ELSE CAST(NULL AS DECIMAL(10, 2)) END AS c_dec, CASE WHEN ((SIGN(c_dbl) < -1) OR (NOT (SIGN(c_dbl) < -1))) OR ((SIGN(c_dbl) < -1) IS NULL) THEN c_dbl ELSE CAST(NULL AS DOUBLE) END AS c_dbl, CASE WHEN ((MOD(c_big, 2) < 0) OR (NOT (MOD(c_big, 2) < 0))) OR ((MOD(c_big, 2) < 0) IS NULL) THEN c_txt ELSE CAST(NULL AS CHAR(255)) END AS c_txt, CASE WHEN ((((c_pk BETWEEN 2 AND 0) AND (c_chr < c_txt)) OR (GREATEST(c_dbl, c_dbl) <= 1.5)) OR (NOT (((c_pk BETWEEN 2 AND 0) AND (c_chr < c_txt)) OR (GREATEST(c_dbl, c_dbl) <= 1.5)))) OR ((((c_pk BETWEEN 2 AND 0) AND (c_chr < c_txt)) OR (GREATEST(c_dbl, c_dbl) <= 1.5)) IS NULL) THEN c_chr ELSE CAST(NULL AS CHAR(255)) END AS c_chr, CASE WHEN ((UPPER(c_chr) = 'trailing ') OR (NOT (UPPER(c_chr) = 'trailing '))) OR ((UPPER(c_chr) = 'trailing ') IS NULL) THEN c_date ELSE CAST(NULL AS DATE) END AS c_date, CASE WHEN ((CEIL(c_dbl) >= 1.5) OR (NOT (CEIL(c_dbl) >= 1.5))) OR ((CEIL(c_dbl) >= 1.5) IS NULL) THEN c_ts ELSE CAST(NULL AS DATETIME(6)) END AS c_ts FROM t__base;
CREATE TABLE t__base_table_16 (c_pk BIGINT, c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10, 2), c_dbl DOUBLE, c_txt VARCHAR(255), c_chr VARCHAR(255), c_date DATE, c_ts DATETIME(6));
INSERT INTO t__base_table_16 (c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts) SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE 1 = 0;
CREATE TABLE t__base_table_17 (c_pk BIGINT, c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10, 2), c_dbl DOUBLE, c_txt VARCHAR(255), c_chr VARCHAR(255), c_date DATE, c_ts DATETIME(6));
INSERT INTO t__base_table_17 (c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts) SELECT * FROM t__base_view_22 UNION ALL SELECT * FROM t__base_table_16;
UPDATE t__base_table_17 SET c_pk = c_pk, c_int = c_int, c_big = c_big, c_dec = c_dec, c_dbl = c_dbl, c_txt = c_txt, c_chr = c_chr, c_date = c_date, c_ts = c_ts WHERE (c_int IS NULL);
CREATE VIEW t__base_view_23 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts, CAST(NULL AS SIGNED) AS eq_tmp_col_1 FROM t__base_table_17;
CREATE VIEW t__base_view_24 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_view_23;
CREATE TABLE t__base_table_18 (c_pk BIGINT, c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10, 2), c_dbl DOUBLE, c_txt VARCHAR(255), c_chr VARCHAR(255), c_date DATE, c_ts DATETIME(6));
INSERT INTO t__base_table_18 (c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts) SELECT CASE WHEN TRUE THEN c_pk ELSE c_pk END AS c_pk, CASE WHEN TRUE THEN c_int ELSE c_int END AS c_int, CASE WHEN TRUE THEN c_big ELSE c_big END AS c_big, CASE WHEN TRUE THEN c_dec ELSE c_dec END AS c_dec, CASE WHEN TRUE THEN c_dbl ELSE c_dbl END AS c_dbl, CASE WHEN TRUE THEN c_txt ELSE c_txt END AS c_txt, CASE WHEN TRUE THEN c_chr ELSE c_chr END AS c_chr, CASE WHEN TRUE THEN c_date ELSE c_date END AS c_date, CASE WHEN TRUE THEN c_ts ELSE c_ts END AS c_ts FROM t__base_view_24;
DELETE FROM t__base_table_18 WHERE (((c_pk BETWEEN -1 AND 2) AND (ABS(c_dbl) < 1.5)) AND ((c_date > '2030-06-01') OR (c_dbl <= -0.25)));
INSERT INTO t__base_table_18 SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE (((c_pk BETWEEN -1 AND 2) AND (ABS(c_dbl) < 1.5)) AND ((c_date > '2030-06-01') OR (c_dbl <= -0.25)));
CREATE TABLE t__base_table_19 (c_pk BIGINT, c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10, 2), c_dbl DOUBLE, c_txt VARCHAR(255), c_chr VARCHAR(255), c_date DATE, c_ts DATETIME(6), eq_xj_1 BIGINT);
INSERT INTO t__base_table_19 (c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts, eq_xj_1) SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts, ROW_NUMBER() OVER (ORDER BY c_pk) AS eq_xj_1 FROM t__base_table_18;
CREATE TABLE t__base_table_20 (eq_xj_1 BIGINT);
INSERT INTO t__base_table_20 (eq_xj_1) SELECT eq_xj_1 FROM t__base_table_19;
CREATE TABLE t__base_table_21 (c_pk BIGINT, c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10, 2), c_dbl DOUBLE, c_txt VARCHAR(255), c_chr VARCHAR(255), c_date DATE, c_ts DATETIME(6));
INSERT INTO t__base_table_21 (c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts) SELECT l.c_pk AS c_pk, l.c_int AS c_int, l.c_big AS c_big, l.c_dec AS c_dec, l.c_dbl AS c_dbl, l.c_txt AS c_txt, l.c_chr AS c_chr, l.c_date AS c_date, l.c_ts AS c_ts FROM t__base_table_19 l CROSS JOIN t__base_table_20 r WHERE l.eq_xj_1 = r.eq_xj_1;
CREATE TABLE t2 (c_pk BIGINT, c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10, 2), c_dbl DOUBLE, c_txt VARCHAR(255), c_chr VARCHAR(255), c_date DATE, c_ts DATETIME(6));
INSERT INTO t2 (c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts) SELECT CASE WHEN ((c_date IN ('1999-12-31', '2024-01-15')) OR (NOT (c_date IN ('1999-12-31', '2024-01-15')))) OR ((c_date IN ('1999-12-31', '2024-01-15')) IS NULL) THEN c_pk ELSE CAST(NULL AS SIGNED) END AS c_pk, CASE WHEN ((c_pk IN (42, 3, 0)) OR (NOT (c_pk IN (42, 3, 0)))) OR ((c_pk IN (42, 3, 0)) IS NULL) THEN c_int ELSE CAST(NULL AS SIGNED) END AS c_int, CASE WHEN ((c_pk IN (1, -1, 42)) OR (NOT (c_pk IN (1, -1, 42)))) OR ((c_pk IN (1, -1, 42)) IS NULL) THEN c_big ELSE CAST(NULL AS SIGNED) END AS c_big, CASE WHEN ((NOT (c_big IS NOT NULL)) OR (NOT (NOT (c_big IS NOT NULL)))) OR ((NOT (c_big IS NOT NULL)) IS NULL) THEN c_dec ELSE CAST(NULL AS DECIMAL(10, 2)) END AS c_dec, CASE WHEN (((FLOOR(c_dbl) <> -0.25) OR ((NOT (c_int <=> 42)))) OR (NOT ((FLOOR(c_dbl) <> -0.25) OR ((NOT (c_int <=> 42)))))) OR (((FLOOR(c_dbl) <> -0.25) OR ((NOT (c_int <=> 42)))) IS NULL) THEN c_dbl ELSE CAST(NULL AS DOUBLE) END AS c_dbl, CASE WHEN ((c_chr IS NOT NULL) OR (NOT (c_chr IS NOT NULL))) OR ((c_chr IS NOT NULL) IS NULL) THEN c_txt ELSE CAST(NULL AS CHAR(255)) END AS c_txt, CASE WHEN ((((c_txt <= 'o''brien') OR (MOD(c_int, 2) > 1)) AND ((c_int IS NULL) AND (c_chr IS NOT NULL))) OR (NOT (((c_txt <= 'o''brien') OR (MOD(c_int, 2) > 1)) AND ((c_int IS NULL) AND (c_chr IS NOT NULL))))) OR ((((c_txt <= 'o''brien') OR (MOD(c_int, 2) > 1)) AND ((c_int IS NULL) AND (c_chr IS NOT NULL))) IS NULL) THEN c_chr ELSE CAST(NULL AS CHAR(255)) END AS c_chr, CASE WHEN (((NOT (c_date IN ('1999-12-31'))) AND ((c_date <= '2030-06-01') OR (LEAST(c_dbl, -0.25) < 1.5))) OR (NOT ((NOT (c_date IN ('1999-12-31'))) AND ((c_date <= '2030-06-01') OR (LEAST(c_dbl, -0.25) < 1.5))))) OR (((NOT (c_date IN ('1999-12-31'))) AND ((c_date <= '2030-06-01') OR (LEAST(c_dbl, -0.25) < 1.5))) IS NULL) THEN c_date ELSE CAST(NULL AS DATE) END AS c_date, CASE WHEN ((c_big > c_pk) OR (NOT (c_big > c_pk))) OR ((c_big > c_pk) IS NULL) THEN c_ts ELSE CAST(NULL AS DATETIME(6)) END AS c_ts FROM t__base_table_21;

-- ============ the query, run against each ============
SELECT STDDEV_POP((~ t1.c_pk)) FROM t1 ORDER BY t1.c_pk ASC;

-- ============ mismatch results ============
-- only in base (1 distinct row(s), 1 row(s) counting multiplicity):
--   ×1 (0.0,)
-- only in equivalent (1 distinct row(s), 1 row(s) counting multiplicity):
--   ×1 (443.40500673763256,)
