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

-- TiDB v9.0.0-beta.2.pre-2051-g3bea8196a5 @3bea8196 (unistore).
-- MIN(GREATEST(<datetime string>, <DATE col>)) GROUP BY over a view whose
-- body filters on (ROW_NUMBER() OVER (...) = 1) returns DATETIME
-- '2016-05-04 10:10:10.100000'. The same aggregate over the heap table, an
-- identity view, or WHERE rn = 1 with rn an integer alias, returns DATE
-- '2016-05-04'. Ungrouped MIN on the buggy view is already DATE.
--
-- Origin: tidb_rich_shuffle/tidb_20260816-012710/mismatch_round12_3.sql
--         (same query: mismatch_round108_0.sql)
-- Session: sql_mode / collation not load-bearing.
--
-- HOW TO RUN: each PART redefines t/v; use a fresh database per PART.


-- =====================================================================================
-- PART 1 -- CONCRETE: last link of the eqgen chain (boolean ROW_NUMBER filter view).
-- The rest of the tag/UNION ALL/ROW_NUMBER-key chain is not required.
-- =====================================================================================
CREATE TABLE t (
  c_pk BIGINT NOT NULL,
  c_big BIGINT,
  c_dbl DOUBLE,
  c_txt VARCHAR(255),
  c_chr VARCHAR(255),
  c_date DATE,
  c_ts DATETIME(6)
);
INSERT INTO t VALUES (1, NULL, NULL, NULL, NULL, NULL, NULL);
INSERT INTO t VALUES (2, 0, NULL, 'abc', '', '1999-12-31', '2024-01-15 12:34:56');
INSERT INTO t VALUES (3, 42, 1.5, 'a', 'trailing ', '2030-06-01', NULL);
INSERT INTO t VALUES (8, 0, NULL, 'abc', '', '1999-12-31', '2024-01-15 12:34:56');

CREATE VIEW v AS
SELECT c_pk, c_big, c_dbl, c_txt, c_chr, c_date, c_ts FROM (
  SELECT c_pk, c_big, c_dbl, c_txt, c_chr, c_date, c_ts,
         (ROW_NUMBER() OVER (PARTITION BY c_pk ORDER BY c_pk) = 1) AS q
  FROM t
) x WHERE q;

-- Expected: DATE '2016-05-04' (what the heap table returns).
-- Actual:   DATETIME '2016-05-04 10:10:10.100000'
SELECT MIN(GREATEST('2016-05-04 10:10:10.100000', t1.c_date))
FROM v AS t1
GROUP BY t1.c_big, t1.c_ts, t1.c_txt, t1.c_dbl, t1.c_chr
HAVING COUNT(*) > t1.c_big;


-- =====================================================================================
-- PART 2 -- DISTILLED. One DATE column, one row, GROUP BY the key.
-- HAVING / extra grouping columns / UNION ALL / duplicate keys are not required.
-- =====================================================================================
CREATE TABLE t (id BIGINT, d DATE);
INSERT INTO t VALUES (1, '1999-12-31');
CREATE VIEW v AS SELECT id, d FROM (
  SELECT id, d, (ROW_NUMBER() OVER (PARTITION BY id ORDER BY id) = 1) AS q FROM t
) x WHERE q;

SELECT MIN(GREATEST('2016-05-04 10:10:10.100000', d)) FROM v GROUP BY id;
-- Expected: '2016-05-04'
-- Actual:   '2016-05-04 10:10:10.100000'


-- =====================================================================================
-- PART 3 -- CONTROLS (each DATE '2016-05-04', i.e. the heap answer).
-- =====================================================================================
-- (a) same aggregate on the heap table
CREATE TABLE t (id BIGINT, d DATE);
INSERT INTO t VALUES (1, '1999-12-31');
SELECT MIN(GREATEST('2016-05-04 10:10:10.100000', d)) FROM t GROUP BY id;
-- Expected/actual: '2016-05-04'

-- (b) identity view
CREATE TABLE t (id BIGINT, d DATE);
INSERT INTO t VALUES (1, '1999-12-31');
CREATE VIEW v AS SELECT id, d FROM t;
SELECT MIN(GREATEST('2016-05-04 10:10:10.100000', d)) FROM v GROUP BY id;
-- Expected/actual: '2016-05-04'

-- (c) integer ROW_NUMBER alias, WHERE rn = 1  -- boolean equality is load-bearing
CREATE TABLE t (id BIGINT, d DATE);
INSERT INTO t VALUES (1, '1999-12-31');
CREATE VIEW v AS SELECT id, d FROM (
  SELECT id, d, ROW_NUMBER() OVER (PARTITION BY id ORDER BY id) AS rn FROM t
) x WHERE rn = 1;
SELECT MIN(GREATEST('2016-05-04 10:10:10.100000', d)) FROM v GROUP BY id;
-- Expected/actual: '2016-05-04'

-- (d) project the boolean but do not filter on it
CREATE TABLE t (id BIGINT, d DATE);
INSERT INTO t VALUES (1, '1999-12-31');
CREATE VIEW v AS SELECT id, d, (ROW_NUMBER() OVER (PARTITION BY id ORDER BY id) = 1) AS q FROM t;
SELECT MIN(GREATEST('2016-05-04 10:10:10.100000', d)) FROM v GROUP BY id;
-- Expected/actual: '2016-05-04'

-- (e) the buggy view, but ungrouped MIN -- already DATE
CREATE TABLE t (id BIGINT, d DATE);
INSERT INTO t VALUES (1, '1999-12-31');
CREATE VIEW v AS SELECT id, d FROM (
  SELECT id, d, (ROW_NUMBER() OVER (PARTITION BY id ORDER BY id) = 1) AS q FROM t
) x WHERE q;
SELECT MIN(GREATEST('2016-05-04 10:10:10.100000', d)) FROM v;
-- Expected/actual: '2016-05-04'
SELECT GREATEST('2016-05-04 10:10:10.100000', d) FROM v;
-- Expected/actual: '2016-05-04'
