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

-- MISMATCH (wrong result: DISTINCT + char_length(SPACE(AVG(…))) + HAVING is insertion-order-dependent)
-- engine=mariadb 11.4.12-MariaDB-ubu2404 (docker mariadb:11.4)
-- sql_mode: STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES (not load-bearing)
-- collation: utf8mb4_nopad_bin / utf8mb4
--
-- The same 8 rows, same query, two INSERT orders:
--   odd pk then even pk → 2 rows (correct: groups c_int=0 and c_int=2 pass HAVING)
--   even pk then odd pk → 0 rows (WRONG)
-- MySQL 9.7.2 returns 2 rows for both orders. PRIMARY KEY on c_pk, or dropping DISTINCT,
-- restores the 2-row answer on MariaDB. HAVING without the DISTINCT+char_length(AVG/SPACE)
-- SELECT item is order-independent (correct).
--
-- Covers eqgen mariadb_20260816-061046 leftovers whose identity view matched the heap
-- (same insert order) but the original equivalent reordered rows:
--   mismatch_round831_44.sql  DISTINCT c_int, char_length(SPACE(AVG(CAST(c_int AS SIGNED)))) … HAVING AVG(c_big)<=c_int
--   mismatch_round256_50.sql  DISTINCT CAST(MIN(c_int+c_big) AS TIME) … GROUP BY CONCAT(…)  (fwd 1 row, reversed INSERTs 0)


-- =====================================================================================
-- PART 1 -- CONCRETE: 831_44's 8 seeded rows. Heap INSERT order is pk 1..8 (works).
-- The equivalent built table_32 as view2 (even pks) UNION ALL view23 (odd pks), which
-- is the even-then-odd order below.
-- =====================================================================================

CREATE TABLE t (
  c_pk BIGINT NOT NULL, c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10,2),
  c_dbl DOUBLE, c_txt VARCHAR(255), c_chr VARCHAR(255), c_date DATE, c_ts DATETIME(6)
);

-- Even pks first (view2), then odd pks (view23). Expected 2 rows. Actual 0 rows.
INSERT INTO t VALUES (2, -7, 2, 999.99, 1000.125, 'trailing ', 'o''brien', '1999-12-31', '2024-01-15 12:34:56');
INSERT INTO t VALUES (4, 2, -1, 12.34, 1.5, 'abc', NULL, '1999-12-31', '1999-12-31 23:59:59');
INSERT INTO t VALUES (6, 0, 0, -5.50, NULL, 'o''brien', 'trailing ', '1999-12-31', '1999-12-31 23:59:59');
INSERT INTO t VALUES (8, -7, 2, 999.99, 1000.125, 'trailing ', 'o''brien', '1999-12-31', '2024-01-15 12:34:56');
INSERT INTO t VALUES (1, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);
INSERT INTO t VALUES (3, 2, -1, NULL, 1000.125, 'o''brien', '', '1999-12-31', '1999-12-31 23:59:59');
INSERT INTO t VALUES (5, 42, NULL, 0.00, NULL, '', 'trailing ', '2030-06-01', '2024-01-15 12:34:56');
INSERT INTO t VALUES (7, NULL, 2, -5.50, -1.5, 'abc', NULL, '2024-01-15', '1999-12-31 23:59:59');

SELECT DISTINCT t1.c_int,
       char_length(SPACE(AVG(CAST(t1.c_int AS SIGNED))))
FROM t t1
WHERE t1.c_int IS NOT NULL
GROUP BY t1.c_int, t1.c_ts, t1.c_dec, t1.c_big, t1.c_dbl
HAVING AVG(t1.c_big) <= t1.c_int;
-- Expected 2 rows (0,0), (2,2). Actual 0 rows.


-- =====================================================================================
-- PART 2 -- Same rows, odd-then-even INSERT order (control: CORRECT).
-- =====================================================================================

CREATE TABLE t (
  c_pk BIGINT NOT NULL, c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10,2),
  c_dbl DOUBLE, c_txt VARCHAR(255), c_chr VARCHAR(255), c_date DATE, c_ts DATETIME(6)
);
INSERT INTO t VALUES (1, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);
INSERT INTO t VALUES (3, 2, -1, NULL, 1000.125, 'o''brien', '', '1999-12-31', '1999-12-31 23:59:59');
INSERT INTO t VALUES (5, 42, NULL, 0.00, NULL, '', 'trailing ', '2030-06-01', '2024-01-15 12:34:56');
INSERT INTO t VALUES (7, NULL, 2, -5.50, -1.5, 'abc', NULL, '2024-01-15', '1999-12-31 23:59:59');
INSERT INTO t VALUES (2, -7, 2, 999.99, 1000.125, 'trailing ', 'o''brien', '1999-12-31', '2024-01-15 12:34:56');
INSERT INTO t VALUES (4, 2, -1, 12.34, 1.5, 'abc', NULL, '1999-12-31', '1999-12-31 23:59:59');
INSERT INTO t VALUES (6, 0, 0, -5.50, NULL, 'o''brien', 'trailing ', '1999-12-31', '1999-12-31 23:59:59');
INSERT INTO t VALUES (8, -7, 2, 999.99, 1000.125, 'trailing ', 'o''brien', '1999-12-31', '2024-01-15 12:34:56');

SELECT DISTINCT t1.c_int,
       char_length(SPACE(AVG(CAST(t1.c_int AS SIGNED))))
FROM t t1
WHERE t1.c_int IS NOT NULL
GROUP BY t1.c_int, t1.c_ts, t1.c_dec, t1.c_big, t1.c_dbl
HAVING AVG(t1.c_big) <= t1.c_int;
-- 2 rows (0,0), (2,2)  ✓


-- =====================================================================================
-- CONTROLS (even-then-odd data, which is the WRONG order for PART 1)
-- =====================================================================================

-- C1 drop DISTINCT — 3 rows (two copies of c_int=2). Order-independent, correct groups.
-- C2 DISTINCT but project AVG(c_int) instead of char_length(SPACE(AVG(…))) — 2 rows, correct.
-- C3 PRIMARY KEY (c_pk) on the even-then-odd table — 2 rows, correct.
-- C4 HAVING only (no DISTINCT, no SPACE) — 3 rows both orders.
-- C5 MySQL 9.7.2 even-then-odd — 2 rows, correct.
