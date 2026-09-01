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

-- MariaDB / MySQL: VAR_SAMP (and VAR_POP / STDDEV_SAMP) over BIGINT UNSIGNED values near 2^64
-- is insertion-order dependent — same multiset yields 4194304.0 or 0.0.
--
-- Engine      : mariadb 11.4.12-MariaDB-ubu2404 (docker mariadb:11.4) — also mysql 9.7.2
-- Session     : defaults; sql_mode irrelevant (reproduced under '' and under the fuzzer's
--               STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES)
-- Finding     : mariadb_hunt_20260809-060545/mariadb_20260809-060558/mismatch_round6_0.sql
--
-- The two UNSIGNED values below are what MariaDB stores for (-7)<<2 and (-7)<<8.
-- As DOUBLEs they differ by one ULP (2048.0); the IEEE sample variance is 2097152.0.
-- Neither insertion order returns that. One order returns (Δ)^2 = 4194304.0; the other
-- cancels to 0.0 even though MIN(sh) <> MAX(sh).
--
-- Every block runs in its own fresh database, checked against the documented `value=` (or that two
-- orders disagree, for the invariant block).

-- >>> BLOCK: distilled-order-large-first  expect=wrong  value=4194304.0
-- Larger UNSIGNED first → non-zero (but still not the IEEE sample variance).
CREATE TABLE t (sh BIGINT UNSIGNED);
INSERT INTO t VALUES (18446744073709551588), (18446744073709549824);
SELECT VAR_SAMP(sh) FROM t;

-- >>> BLOCK: distilled-order-small-first  expect=wrong  value=0.0
-- Same two values, opposite insertion order → 0.0.
CREATE TABLE t (sh BIGINT UNSIGNED);
INSERT INTO t VALUES (18446744073709549824), (18446744073709551588);
SELECT VAR_SAMP(sh) FROM t;

-- >>> BLOCK: shift-order-pk-asc  expect=wrong  value=4194304.0
-- Same defect via bit-shift (<< yields BIGINT UNSIGNED). Rows scanned as c_pk=2 then 8.
CREATE TABLE t (c_pk BIGINT, c_int BIGINT);
INSERT INTO t VALUES (2, -7), (8, -7);
SELECT VAR_SAMP(c_int << c_pk) FROM t;

-- >>> BLOCK: shift-order-pk-desc-insert  expect=wrong  value=0.0
-- Insert (8,-7) before (2,-7) — physical scan order flips, VAR_SAMP becomes 0.
CREATE TABLE t (c_pk BIGINT, c_int BIGINT);
INSERT INTO t VALUES (8, -7), (2, -7);
SELECT VAR_SAMP(c_int << c_pk) FROM t;

-- >>> BLOCK: control-small-values  expect=ok  value=18.0
-- Small shifts stay inside the DOUBLE mantissa; both orders agree.
CREATE TABLE t (c_pk BIGINT, c_int BIGINT);
INSERT INTO t VALUES (1, 3), (2, 3);
SELECT VAR_SAMP(c_int << c_pk) FROM t;

-- >>> BLOCK: control-small-values-reversed  expect=ok  value=18.0
CREATE TABLE t (c_pk BIGINT, c_int BIGINT);
INSERT INTO t VALUES (2, 3), (1, 3);
SELECT VAR_SAMP(c_int << c_pk) FROM t;

-- >>> BLOCK: var-pop-order-large-first  expect=wrong  value=2097152.0
-- VAR_POP / VARIANCE / STDDEV_SAMP share the same order dependence.
CREATE TABLE t (sh BIGINT UNSIGNED);
INSERT INTO t VALUES (18446744073709551588), (18446744073709549824);
SELECT VAR_POP(sh) FROM t;

-- >>> BLOCK: var-pop-order-small-first  expect=wrong  value=0.0
CREATE TABLE t (sh BIGINT UNSIGNED);
INSERT INTO t VALUES (18446744073709549824), (18446744073709551588);
SELECT VAR_POP(sh) FROM t;

-- >>> BLOCK: concrete-rank-mod-union  expect=wrong  value=0.0
-- Concrete shape from the finding: RankModUnionQueryBuilder (ROW_NUMBER + MOD + UNION ALL
-- CTAS) over the seeded 8-row table reorders the two group members so the aggregate sees
-- c_pk=8 before c_pk=2 and returns 0. A plain CTAS of the same rows returns 4194304.
CREATE TABLE t (
  c_pk BIGINT NOT NULL,
  c_int BIGINT,
  c_big BIGINT,
  c_dec DECIMAL(10, 2),
  c_dbl DOUBLE,
  c_txt VARCHAR(255),
  c_chr VARCHAR(255),
  c_date DATE,
  c_ts DATETIME(6)
);
INSERT INTO t VALUES (1, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);
INSERT INTO t VALUES (2, -7, 2, 0.0, 1000.125, '', 'Zed', '2024-01-15', '1999-12-31 23:59:59');
INSERT INTO t VALUES (3, -7, NULL, 0.0, 0.0, 'o''brien', 'Zed', '2030-06-01', '1999-12-31 23:59:59');
INSERT INTO t VALUES (4, NULL, 42, 999.99, NULL, 'trailing ', NULL, '2024-01-15', '1999-12-31 23:59:59');
INSERT INTO t VALUES (5, 42, 42, 999.99, 1.5, 'trailing ', '', NULL, '2024-01-15 12:34:56');
INSERT INTO t VALUES (6, 1, -7, 0.0, 1.5, 'a', 'Zed', NULL, '2024-01-15 12:34:56');
INSERT INTO t VALUES (7, -7, -1, 12.34, NULL, 'abc', 'a', '1999-12-31', '1999-12-31 23:59:59');
INSERT INTO t VALUES (8, -7, 2, 0.0, 1000.125, '', 'Zed', '2024-01-15', '1999-12-31 23:59:59');
CREATE TABLE ranked AS
  SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts,
         ROW_NUMBER() OVER (ORDER BY c_pk) AS eq_rank
  FROM t;
CREATE TABLE t2 AS
  SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM ranked WHERE MOD(eq_rank, 4) = 0
  UNION ALL
  SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM ranked WHERE MOD(eq_rank, 4) = 1
  UNION ALL
  SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM ranked WHERE MOD(eq_rank, 4) = 2
  UNION ALL
  SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts FROM ranked WHERE MOD(eq_rank, 4) = 3;
SELECT VAR_SAMP(c_int << c_pk)
FROM t2
WHERE c_int = -7 AND c_big = 2;

-- >>> BLOCK: control-plain-ctas-same-rows  expect=wrong  value=4194304.0
-- Same filter over a plain CTAS of the seed (insertion order 2 then 8) — the other wrong answer.
-- Documents that the RankModUnion did not change the multiset, only the physical order.
CREATE TABLE t (
  c_pk BIGINT NOT NULL,
  c_int BIGINT,
  c_big BIGINT,
  c_dec DECIMAL(10, 2),
  c_dbl DOUBLE,
  c_txt VARCHAR(255),
  c_chr VARCHAR(255),
  c_date DATE,
  c_ts DATETIME(6)
);
INSERT INTO t VALUES (1, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);
INSERT INTO t VALUES (2, -7, 2, 0.0, 1000.125, '', 'Zed', '2024-01-15', '1999-12-31 23:59:59');
INSERT INTO t VALUES (3, -7, NULL, 0.0, 0.0, 'o''brien', 'Zed', '2030-06-01', '1999-12-31 23:59:59');
INSERT INTO t VALUES (4, NULL, 42, 999.99, NULL, 'trailing ', NULL, '2024-01-15', '1999-12-31 23:59:59');
INSERT INTO t VALUES (5, 42, 42, 999.99, 1.5, 'trailing ', '', NULL, '2024-01-15 12:34:56');
INSERT INTO t VALUES (6, 1, -7, 0.0, 1.5, 'a', 'Zed', NULL, '2024-01-15 12:34:56');
INSERT INTO t VALUES (7, -7, -1, 12.34, NULL, 'abc', 'a', '1999-12-31', '1999-12-31 23:59:59');
INSERT INTO t VALUES (8, -7, 2, 0.0, 1000.125, '', 'Zed', '2024-01-15', '1999-12-31 23:59:59');
CREATE TABLE t2 AS SELECT * FROM t;
SELECT VAR_SAMP(c_int << c_pk)
FROM t2
WHERE c_int = -7 AND c_big = 2;
