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
-- engine: dolt 8.0.31 (dolt-main/bin, 127.0.0.1:49431)
-- seed: 1360946956
-- sql_mode: STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES,PIPES_AS_CONCAT
-- collation: utf8mb4_0900_bin
-- character_set: utf8mb4
-- mismatch: 1 distinct only in base, 1 distinct only in equivalent

-- ============ database 1: the base table ============
CREATE TABLE t (c_pk BIGINT NOT NULL, c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10, 2), c_dbl DOUBLE, c_txt VARCHAR(255), c_chr VARCHAR(255), c_date DATE, c_ts DATETIME(6));
INSERT INTO t VALUES (1, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);
INSERT INTO t VALUES (2, 1, NULL, 999.99, -1.5, '', 'Zed', '1999-12-31', NULL);
INSERT INTO t VALUES (3, -7, -1, 999.99, 1.5, 'Zed', 'o''brien', '2030-06-01', '2024-01-15 12:34:56');
INSERT INTO t VALUES (4, NULL, 42, 12.34, NULL, 'trailing ', 'trailing ', '1999-12-31', '2024-01-15 12:34:56');
INSERT INTO t VALUES (5, NULL, -7, -5.5, -1.5, NULL, 'abc', '1999-12-31', '2024-01-15 12:34:56');
INSERT INTO t VALUES (6, -7, -1, 999.99, 1.5, 'o''brien', 'a', '2030-06-01', '1999-12-31 23:59:59');
INSERT INTO t VALUES (7, 0, 2, 12.34, 1.5, '', 'trailing ', NULL, NULL);
INSERT INTO t VALUES (8, 1, NULL, 999.99, -1.5, '', 'Zed', '1999-12-31', NULL);
CREATE TABLE t0 AS SELECT * FROM t;
CREATE TABLE t1 AS SELECT * FROM t;
CREATE TABLE t2 AS SELECT * FROM t;

-- ============ database 2: the equivalent ============
CREATE TABLE t (c_pk BIGINT NOT NULL, c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10, 2), c_dbl DOUBLE, c_txt VARCHAR(255), c_chr VARCHAR(255), c_date DATE, c_ts DATETIME(6));
INSERT INTO t VALUES (1, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);
INSERT INTO t VALUES (2, 1, NULL, 999.99, -1.5, '', 'Zed', '1999-12-31', NULL);
INSERT INTO t VALUES (3, -7, -1, 999.99, 1.5, 'Zed', 'o''brien', '2030-06-01', '2024-01-15 12:34:56');
INSERT INTO t VALUES (4, NULL, 42, 12.34, NULL, 'trailing ', 'trailing ', '1999-12-31', '2024-01-15 12:34:56');
INSERT INTO t VALUES (5, NULL, -7, -5.5, -1.5, NULL, 'abc', '1999-12-31', '2024-01-15 12:34:56');
INSERT INTO t VALUES (6, -7, -1, 999.99, 1.5, 'o''brien', 'a', '2030-06-01', '1999-12-31 23:59:59');
INSERT INTO t VALUES (7, 0, 2, 12.34, 1.5, '', 'trailing ', NULL, NULL);
INSERT INTO t VALUES (8, 1, NULL, 999.99, -1.5, '', 'Zed', '1999-12-31', NULL);
ALTER TABLE t RENAME TO t__base;
CREATE VIEW t__base_view_1 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((MOD(c_pk, 2) = 0) AND (c_ts <= '2024-01-15 12:34:56')) AND (MOD(c_pk, 2) = 0);
CREATE TABLE t__base_table_1 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts, ROW_NUMBER() OVER (ORDER BY c_pk) AS eq_xj_1 FROM t__base;
CREATE TABLE t__base_table_2 AS SELECT eq_xj_1 FROM t__base_table_1;
CREATE TABLE t__base_table_3 AS SELECT l.c_pk AS c_pk, l.c_int AS c_int, l.c_big AS c_big, l.c_dec AS c_dec, l.c_dbl AS c_dbl, l.c_txt AS c_txt, l.c_chr AS c_chr, l.c_date AS c_date, l.c_ts AS c_ts FROM t__base_table_1 l CROSS JOIN t__base_table_2 r WHERE l.eq_xj_1 = r.eq_xj_1;
CREATE VIEW t__base_view_2 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_table_3 WHERE ((MOD(c_pk, 2) = 0) AND (c_ts <= '2024-01-15 12:34:56')) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL));
CREATE TABLE t__base_table_4 AS SELECT * FROM t__base_view_1 UNION ALL SELECT * FROM t__base_view_2;
CREATE VIEW t__base_view_3 AS SELECT COALESCE(c_pk, c_pk) AS c_pk, COALESCE(c_int, c_int) AS c_int, COALESCE(c_big, c_big) AS c_big, COALESCE(c_dec, c_dec) AS c_dec, COALESCE(c_dbl, c_dbl) AS c_dbl, COALESCE(c_txt, c_txt) AS c_txt, COALESCE(c_chr, c_chr) AS c_chr, COALESCE(c_date, c_date) AS c_date, COALESCE(c_ts, c_ts) AS c_ts FROM t__base;
CREATE TABLE t__base_table_5 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE (1 = 0) AND (c_int IS NULL);
CREATE VIEW t__base_view_4 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE (1 = 0) AND (NOT (c_int IS NULL));
CREATE TABLE t__base_table_6 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE (1 = 0) AND ((c_int IS NULL) IS NULL);
CREATE TABLE t__base_table_7 AS SELECT * FROM t__base_table_5 UNION ALL SELECT * FROM t__base_view_4 UNION ALL SELECT * FROM t__base_table_6;
CREATE TABLE t__base_table_8 AS SELECT * FROM t__base_view_3 UNION ALL SELECT * FROM t__base_table_7;
CREATE TABLE t__base_table_9 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_table_8 WHERE ((MOD(c_pk, 2) = 0) AND (NOT (c_ts <= '2024-01-15 12:34:56'))) AND (MOD(c_pk, 2) = 0);
CREATE TABLE t__base_table_10 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((MOD(c_pk, 2) = 0) AND (NOT (c_ts <= '2024-01-15 12:34:56'))) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL));
CREATE TABLE t__base_table_11 AS SELECT * FROM t__base_table_9 UNION ALL SELECT * FROM t__base_table_10;
CREATE TABLE t__base_table_12 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE (MOD(c_pk, 2) = 0) AND (((c_big = 1000) AND (c_txt = 'trailing ')) AND (c_int IS NOT NULL));
CREATE TABLE t__base_table_13 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE (MOD(c_pk, 2) = 0) AND (NOT (((c_big = 1000) AND (c_txt = 'trailing ')) AND (c_int IS NOT NULL)));
CREATE VIEW t__base_view_5 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE (MOD(c_pk, 2) = 0) AND ((((c_big = 1000) AND (c_txt = 'trailing ')) AND (c_int IS NOT NULL)) IS NULL);
CREATE TABLE t__base_table_14 AS SELECT * FROM t__base_table_12 UNION ALL SELECT * FROM t__base_table_13 UNION ALL SELECT * FROM t__base_view_5;
CREATE TABLE t__base_table_15 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND (MOD(c_pk, 2) = 0);
CREATE VIEW t__base_view_6 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL));
CREATE TABLE t__base_table_16 AS SELECT * FROM t__base_table_15 UNION ALL SELECT * FROM t__base_view_6;
CREATE VIEW t__base_view_7 AS SELECT * FROM t__base_table_14 UNION ALL SELECT * FROM t__base_table_16;
CREATE VIEW t__base_view_8 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((1 = 0) AND (c_chr < 'abc')) AND (c_int IS NULL);
CREATE VIEW t__base_view_9 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((1 = 0) AND (c_chr < 'abc')) AND (NOT (c_int IS NULL));
CREATE VIEW t__base_view_10 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((1 = 0) AND (c_chr < 'abc')) AND ((c_int IS NULL) IS NULL);
CREATE TABLE t__base_table_17 AS SELECT * FROM t__base_view_8 UNION ALL SELECT * FROM t__base_view_9 UNION ALL SELECT * FROM t__base_view_10;
CREATE TABLE t__base_table_18 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE (1 = 0) AND (NOT (c_chr < 'abc'));
CREATE TABLE t__base_table_19 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((1 = 0) AND ((c_chr < 'abc') IS NULL)) AND (NOT ((c_chr IS NULL) AND (c_big IS NOT NULL)));
CREATE TABLE t__base_table_20 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((1 = 0) AND ((c_chr < 'abc') IS NULL)) AND (NOT (NOT ((c_chr IS NULL) AND (c_big IS NOT NULL))));
CREATE VIEW t__base_view_11 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((1 = 0) AND ((c_chr < 'abc') IS NULL)) AND ((NOT ((c_chr IS NULL) AND (c_big IS NOT NULL))) IS NULL);
CREATE TABLE t__base_table_21 AS SELECT * FROM t__base_table_19 UNION ALL SELECT * FROM t__base_table_20 UNION ALL SELECT * FROM t__base_view_11;
CREATE VIEW t__base_view_12 AS SELECT * FROM t__base_table_17 UNION ALL SELECT * FROM t__base_table_18 UNION ALL SELECT * FROM t__base_table_21;
CREATE TABLE t__base_table_22 AS SELECT * FROM t__base_view_7 UNION ALL SELECT * FROM t__base_view_12;
CREATE TABLE t__base_table_23 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_table_22 WHERE (MOD(c_pk, 2) = 0) AND ((c_ts <= '2024-01-15 12:34:56') IS NULL);
CREATE TABLE t__base_table_24 AS SELECT * FROM t__base_table_4 UNION ALL SELECT * FROM t__base_table_11 UNION ALL SELECT * FROM t__base_table_23;
CREATE VIEW t__base_view_13 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((((((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND (c_date > '1999-12-31')) AND (MOD(c_pk, 2) = 0)) AND (MOD(c_pk, 2) = 0)) AND (((c_int = c_big) AND (c_txt IS NULL)) AND ((c_pk IS NULL) AND (c_pk IS NOT NULL)))) AND (MOD(c_pk, 2) = 0);
CREATE TABLE t__base_table_25 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((((((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND (c_date > '1999-12-31')) AND (MOD(c_pk, 2) = 0)) AND (MOD(c_pk, 2) = 0)) AND (((c_int = c_big) AND (c_txt IS NULL)) AND ((c_pk IS NULL) AND (c_pk IS NOT NULL)))) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL));
CREATE VIEW t__base_view_14 AS SELECT * FROM t__base_view_13 UNION ALL SELECT * FROM t__base_table_25;
CREATE VIEW t__base_view_15 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((((((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND (c_date > '1999-12-31')) AND (MOD(c_pk, 2) = 0)) AND (MOD(c_pk, 2) = 0)) AND (NOT (((c_int = c_big) AND (c_txt IS NULL)) AND ((c_pk IS NULL) AND (c_pk IS NOT NULL))))) AND ((NOT (c_int = 0)) AND (c_dec <= 12.34));
CREATE TABLE t__base_table_26 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((((((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND (c_date > '1999-12-31')) AND (MOD(c_pk, 2) = 0)) AND (MOD(c_pk, 2) = 0)) AND (NOT (((c_int = c_big) AND (c_txt IS NULL)) AND ((c_pk IS NULL) AND (c_pk IS NOT NULL))))) AND (NOT ((NOT (c_int = 0)) AND (c_dec <= 12.34)));
CREATE TABLE t__base_table_27 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((((((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND (c_date > '1999-12-31')) AND (MOD(c_pk, 2) = 0)) AND (MOD(c_pk, 2) = 0)) AND (NOT (((c_int = c_big) AND (c_txt IS NULL)) AND ((c_pk IS NULL) AND (c_pk IS NOT NULL))))) AND (((NOT (c_int = 0)) AND (c_dec <= 12.34)) IS NULL);
CREATE VIEW t__base_view_16 AS SELECT * FROM t__base_view_15 UNION ALL SELECT * FROM t__base_table_26 UNION ALL SELECT * FROM t__base_table_27;
CREATE VIEW t__base_view_17 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((((((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND (c_date > '1999-12-31')) AND (MOD(c_pk, 2) = 0)) AND (MOD(c_pk, 2) = 0)) AND ((((c_int = c_big) AND (c_txt IS NULL)) AND ((c_pk IS NULL) AND (c_pk IS NOT NULL))) IS NULL)) AND (((c_pk < 2) OR (c_date > '2024-01-15')) OR (NOT (c_pk < 3)));
CREATE VIEW t__base_view_18 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((((((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND (c_date > '1999-12-31')) AND (MOD(c_pk, 2) = 0)) AND (MOD(c_pk, 2) = 0)) AND ((((c_int = c_big) AND (c_txt IS NULL)) AND ((c_pk IS NULL) AND (c_pk IS NOT NULL))) IS NULL)) AND (NOT (((c_pk < 2) OR (c_date > '2024-01-15')) OR (NOT (c_pk < 3))));
CREATE TABLE t__base_table_28 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((((((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND (c_date > '1999-12-31')) AND (MOD(c_pk, 2) = 0)) AND (MOD(c_pk, 2) = 0)) AND ((((c_int = c_big) AND (c_txt IS NULL)) AND ((c_pk IS NULL) AND (c_pk IS NOT NULL))) IS NULL)) AND ((((c_pk < 2) OR (c_date > '2024-01-15')) OR (NOT (c_pk < 3))) IS NULL);
CREATE TABLE t__base_table_29 AS SELECT * FROM t__base_view_17 UNION ALL SELECT * FROM t__base_view_18 UNION ALL SELECT * FROM t__base_table_28;
CREATE TABLE t__base_table_30 AS SELECT * FROM t__base_view_14 UNION ALL SELECT * FROM t__base_view_16 UNION ALL SELECT * FROM t__base_table_29;
CREATE TABLE t__base_table_31 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((((((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND (c_date > '1999-12-31')) AND (MOD(c_pk, 2) = 0)) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL))) AND (MOD(c_pk, 2) = 0)) AND (c_big IS NULL);
CREATE VIEW t__base_view_19 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((((((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND (c_date > '1999-12-31')) AND (MOD(c_pk, 2) = 0)) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL))) AND (MOD(c_pk, 2) = 0)) AND (NOT (c_big IS NULL));
CREATE VIEW t__base_view_20 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((((((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND (c_date > '1999-12-31')) AND (MOD(c_pk, 2) = 0)) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL))) AND (MOD(c_pk, 2) = 0)) AND ((c_big IS NULL) IS NULL);
CREATE TABLE t__base_table_32 AS SELECT * FROM t__base_table_31 UNION ALL SELECT * FROM t__base_view_19 UNION ALL SELECT * FROM t__base_view_20;
CREATE TABLE t__base_table_33 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((((((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND (c_date > '1999-12-31')) AND (MOD(c_pk, 2) = 0)) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL))) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL))) AND (MOD(c_pk, 2) = 0);
CREATE TABLE t__base_table_34 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((((((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND (c_date > '1999-12-31')) AND (MOD(c_pk, 2) = 0)) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL))) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL))) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL));
CREATE TABLE t__base_table_35 AS SELECT * FROM t__base_table_33 UNION ALL SELECT * FROM t__base_table_34;
CREATE VIEW t__base_view_21 AS SELECT * FROM t__base_table_32 UNION ALL SELECT * FROM t__base_table_35;
CREATE VIEW t__base_view_22 AS SELECT * FROM t__base_table_30 UNION ALL SELECT * FROM t__base_view_21;
CREATE TABLE t__base_table_36 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((((((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND (c_date > '1999-12-31')) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL))) AND (MOD(c_pk, 2) = 0)) AND (MOD(c_pk, 2) = 0)) AND (MOD(c_pk, 2) = 0);
CREATE TABLE t__base_table_37 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((((((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND (c_date > '1999-12-31')) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL))) AND (MOD(c_pk, 2) = 0)) AND (MOD(c_pk, 2) = 0)) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL));
CREATE TABLE t__base_table_38 AS SELECT * FROM t__base_table_36 UNION ALL SELECT * FROM t__base_table_37;
CREATE TABLE t__base_table_39 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((((((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND (c_date > '1999-12-31')) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL))) AND (MOD(c_pk, 2) = 0)) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL))) AND (MOD(c_pk, 2) = 0);
CREATE TABLE t__base_table_40 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((((((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND (c_date > '1999-12-31')) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL))) AND (MOD(c_pk, 2) = 0)) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL))) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL));
CREATE TABLE t__base_table_41 AS SELECT * FROM t__base_table_39 UNION ALL SELECT * FROM t__base_table_40;
CREATE VIEW t__base_view_23 AS SELECT * FROM t__base_table_38 UNION ALL SELECT * FROM t__base_table_41;
CREATE TABLE t__base_table_42 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((((((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND (c_date > '1999-12-31')) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL))) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL))) AND (MOD(c_pk, 2) = 0)) AND (MOD(c_pk, 2) = 0);
CREATE TABLE t__base_table_43 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((((((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND (c_date > '1999-12-31')) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL))) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL))) AND (MOD(c_pk, 2) = 0)) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL));
CREATE VIEW t__base_view_24 AS SELECT * FROM t__base_table_42 UNION ALL SELECT * FROM t__base_table_43;
CREATE VIEW t__base_view_25 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM (SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts, ((ROW_NUMBER() OVER (ORDER BY c_pk)) >= 1) AS eq_q FROM t__base) AS eq_qsrc WHERE eq_q;
CREATE TABLE t__base_table_44 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_view_25 WHERE (((((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND (c_date > '1999-12-31')) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL))) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL))) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL));
CREATE VIEW t__base_view_26 AS SELECT * FROM t__base_view_24 UNION ALL SELECT * FROM t__base_table_44;
CREATE VIEW t__base_view_27 AS SELECT * FROM t__base_view_23 UNION ALL SELECT * FROM t__base_view_26;
CREATE VIEW t__base_view_28 AS SELECT * FROM t__base_view_22 UNION ALL SELECT * FROM t__base_view_27;
CREATE VIEW t__base_view_29 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND (NOT (c_date > '1999-12-31'));
CREATE VIEW t__base_view_30 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND ((c_date > '1999-12-31') IS NULL)) AND (MOD(c_pk, 2) = 0)) AND (MOD(c_pk, 2) = 0);
CREATE TABLE t__base_table_45 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND ((c_date > '1999-12-31') IS NULL)) AND (MOD(c_pk, 2) = 0)) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL));
CREATE VIEW t__base_view_31 AS SELECT * FROM t__base_view_30 UNION ALL SELECT * FROM t__base_table_45;
CREATE TABLE t__base_table_46 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((NOT (c_ts IS NULL)) AND ((c_dbl IS NOT NULL) AND (c_dbl < 0.0))) AND ((c_date >= '2030-06-01') OR (c_date IS NOT NULL));
CREATE VIEW t__base_view_32 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((NOT (c_ts IS NULL)) AND ((c_dbl IS NOT NULL) AND (c_dbl < 0.0))) AND (NOT ((c_date >= '2030-06-01') OR (c_date IS NOT NULL)));
CREATE TABLE t__base_table_47 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((NOT (c_ts IS NULL)) AND ((c_dbl IS NOT NULL) AND (c_dbl < 0.0))) AND (((c_date >= '2030-06-01') OR (c_date IS NOT NULL)) IS NULL);
CREATE TABLE t__base_table_48 AS SELECT * FROM t__base_table_46 UNION ALL SELECT * FROM t__base_view_32 UNION ALL SELECT * FROM t__base_table_47;
CREATE TABLE t__base_table_49 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE (NOT ((NOT (c_ts IS NULL)) AND ((c_dbl IS NOT NULL) AND (c_dbl < 0.0)))) AND (MOD(c_pk, 2) = 0);
CREATE VIEW t__base_view_33 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE (NOT ((NOT (c_ts IS NULL)) AND ((c_dbl IS NOT NULL) AND (c_dbl < 0.0)))) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL));
CREATE TABLE t__base_table_50 AS SELECT * FROM t__base_table_49 UNION ALL SELECT * FROM t__base_view_33;
CREATE VIEW t__base_view_34 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE (((NOT (c_ts IS NULL)) AND ((c_dbl IS NOT NULL) AND (c_dbl < 0.0))) IS NULL) AND (((c_ts <> '2024-01-15 12:34:56') OR (c_dec <> 0.00)) OR (c_txt > 'a'));
CREATE TABLE t__base_table_51 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE (((NOT (c_ts IS NULL)) AND ((c_dbl IS NOT NULL) AND (c_dbl < 0.0))) IS NULL) AND (NOT (((c_ts <> '2024-01-15 12:34:56') OR (c_dec <> 0.00)) OR (c_txt > 'a')));
CREATE TABLE t__base_table_52 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE (((NOT (c_ts IS NULL)) AND ((c_dbl IS NOT NULL) AND (c_dbl < 0.0))) IS NULL) AND ((((c_ts <> '2024-01-15 12:34:56') OR (c_dec <> 0.00)) OR (c_txt > 'a')) IS NULL);
CREATE VIEW t__base_view_35 AS SELECT * FROM t__base_view_34 UNION ALL SELECT * FROM t__base_table_51 UNION ALL SELECT * FROM t__base_table_52;
CREATE TABLE t__base_table_53 AS SELECT * FROM t__base_table_48 UNION ALL SELECT * FROM t__base_table_50 UNION ALL SELECT * FROM t__base_view_35;
CREATE TABLE t__base_table_54 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_table_53 WHERE (((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND ((c_date > '1999-12-31') IS NULL)) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL));
CREATE TABLE t__base_table_55 AS SELECT * FROM t__base_view_31 UNION ALL SELECT * FROM t__base_table_54;
CREATE TABLE t__base_table_56 AS SELECT * FROM t__base_view_28 UNION ALL SELECT * FROM t__base_view_29 UNION ALL SELECT * FROM t__base_table_55;
CREATE TABLE t0 AS SELECT * FROM t__base_table_24 UNION ALL SELECT * FROM t__base_table_56;
CREATE TABLE t__base_table_57 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base;
CREATE VIEW t__base_view_36 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE 1 = 0;
CREATE TABLE t__base_table_58 AS SELECT * FROM t__base_table_57 UNION ALL SELECT * FROM t__base_view_36;
CREATE TABLE t__base_table_59 AS SELECT COALESCE(c_pk, c_pk) AS c_pk, COALESCE(c_int, c_int) AS c_int, COALESCE(c_big, c_big) AS c_big, COALESCE(c_dec, c_dec) AS c_dec, COALESCE(c_dbl, c_dbl) AS c_dbl, COALESCE(c_txt, c_txt) AS c_txt, COALESCE(c_chr, c_chr) AS c_chr, COALESCE(c_date, c_date) AS c_date, COALESCE(c_ts, c_ts) AS c_ts FROM t__base_table_58;
CREATE TABLE t__base_table_60 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts, ROW_NUMBER() OVER (ORDER BY c_pk) AS eq_xj_2 FROM t__base_table_59;
CREATE TABLE t__base_table_61 AS SELECT eq_xj_2 FROM t__base_table_60;
CREATE VIEW t__base_view_37 AS SELECT l.c_pk AS c_pk, l.c_int AS c_int, l.c_big AS c_big, l.c_dec AS c_dec, l.c_dbl AS c_dbl, l.c_txt AS c_txt, l.c_chr AS c_chr, l.c_date AS c_date, l.c_ts AS c_ts FROM t__base_table_60 l CROSS JOIN t__base_table_61 r WHERE l.eq_xj_2 = r.eq_xj_2;
CREATE TABLE t__base_table_62 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts, ROW_NUMBER() OVER (ORDER BY c_pk) AS eq_rank_1 FROM t__base_view_37;
CREATE VIEW t__base_view_38 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_table_62 WHERE MOD(eq_rank_1, 3) = 0;
CREATE VIEW t__base_view_39 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_table_62 WHERE MOD(eq_rank_1, 3) = 1;
CREATE VIEW t__base_view_40 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_table_62 WHERE MOD(eq_rank_1, 3) = 2;
CREATE TABLE t1 AS SELECT * FROM t__base_view_38 UNION ALL SELECT * FROM t__base_view_39 UNION ALL SELECT * FROM t__base_view_40;
CREATE VIEW t__base_view_41 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE MOD(c_pk, 2) = 0;
CREATE VIEW t__base_view_42 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND (MOD(c_pk, 2) = 0);
CREATE VIEW t__base_view_43 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM (SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts, ((ROW_NUMBER() OVER (ORDER BY c_pk)) >= 1) AS eq_q FROM t__base) AS eq_qsrc WHERE eq_q;
CREATE VIEW t__base_view_44 AS SELECT COALESCE(c_pk, c_pk) AS c_pk, COALESCE(c_int, c_int) AS c_int, COALESCE(c_big, c_big) AS c_big, COALESCE(c_dec, c_dec) AS c_dec, COALESCE(c_dbl, c_dbl) AS c_dbl, COALESCE(c_txt, c_txt) AS c_txt, COALESCE(c_chr, c_chr) AS c_chr, COALESCE(c_date, c_date) AS c_date, COALESCE(c_ts, c_ts) AS c_ts FROM t__base_view_43;
CREATE TABLE t__base_table_63 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE (1 = 0) AND (c_ts <= '2024-01-15 12:34:56');
CREATE TABLE t__base_table_64 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE (1 = 0) AND (NOT (c_ts <= '2024-01-15 12:34:56'));
CREATE TABLE t__base_table_65 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base WHERE (1 = 0) AND ((c_ts <= '2024-01-15 12:34:56') IS NULL);
CREATE VIEW t__base_view_45 AS SELECT * FROM t__base_table_63 UNION ALL SELECT * FROM t__base_table_64 UNION ALL SELECT * FROM t__base_table_65;
CREATE TABLE t__base_table_66 AS SELECT * FROM t__base_view_44 UNION ALL SELECT * FROM t__base_view_45;
CREATE VIEW t__base_view_46 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM t__base_table_66 WHERE ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL)) AND ((MOD(c_pk, 2) <> 0) OR (c_pk IS NULL));
CREATE VIEW t__base_view_47 AS SELECT * FROM t__base_view_42 UNION ALL SELECT * FROM t__base_view_46;
CREATE TABLE t__base_table_67 AS SELECT * FROM t__base_view_41 UNION ALL SELECT * FROM t__base_view_47;
CREATE VIEW t2 AS SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM (SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts, ((ROW_NUMBER() OVER (ORDER BY c_pk)) >= 1) AS eq_q FROM t__base_table_67) AS eq_qsrc WHERE eq_q;

-- ============ the query, run against each ============
SELECT COUNT(t2.c_chr) FROM t2 WHERE (NOT false);

-- ============ mismatch results ============
-- only in base (1 distinct row(s), 1 row(s) counting multiplicity):
--   ×1 (6,)
-- only in equivalent (1 distinct row(s), 1 row(s) counting multiplicity):
--   ×1 (7,)
