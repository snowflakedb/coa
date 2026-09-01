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

-- DuckDB v2.0.0-alpha37826 (a9f869b6a7).
-- Dict-surviving hash join (#23360) narrows a payload slot from the first
-- build chunk's global dictionary, then throws InternalException when a later
-- chunk is not a global dictionary. BuildSideHasMultipleSources only treats
-- UNION / recursive CTE as multi-source; a Nested Loop Join of two identity
-- hash joins is two producer pipelines and is missed.
--
-- The INTERNAL Error invalidates the in-memory DB: run the controls first,
-- then the distilled SELECT last (or each section in a fresh session).
-- Mask: SET disabled_optimizers='filter_pushdown'
--       (unused_columns and build_side_probe_side also avoid the plan).
-- CLI: duckdb

-- ========== control: heap table. Expected = actual: two (NULL, NULL) rows. ==========
CREATE TABLE t (
  c_pk BIGINT NOT NULL,
  c_int BIGINT,
  c_big DECIMAL(38, 0),
  c_txt VARCHAR,
  c_date DATE,
  c_ts TIMESTAMP
);
INSERT INTO t VALUES (3, NULL, -7, NULL, DATE '2030-06-01', NULL);
INSERT INTO t VALUES (8, -1, 1, 'x', DATE '2024-01-15', TIMESTAMP '1999-12-31 23:59:59');
SELECT t3.c_txt, t3.c_big
FROM t t1
RIGHT OUTER JOIN t t2 ON t1.c_int <= t2.c_int
LEFT OUTER JOIN t t3 ON t1.c_date = t3.c_ts;
-- Expected 2 rows (NULL, NULL). Actual 2 rows (NULL, NULL).
DROP TABLE t;

-- ========== control: identity join materialized (CTAS). Clean. ==========
CREATE TABLE t__base (
  c_pk BIGINT NOT NULL,
  c_int BIGINT,
  c_big DECIMAL(38, 0),
  c_txt VARCHAR,
  c_date DATE,
  c_ts TIMESTAMP
);
INSERT INTO t__base VALUES (3, NULL, -7, NULL, DATE '2030-06-01', NULL);
INSERT INTO t__base VALUES (8, -1, 1, 'x', DATE '2024-01-15', TIMESTAMP '1999-12-31 23:59:59');
CREATE TABLE t25 AS SELECT c_pk, 1 AS flag FROM t__base;
CREATE TABLE t AS
SELECT l.c_pk AS c_pk, l.c_int AS c_int, l.c_big AS c_big,
       l.c_txt AS c_txt, l.c_date AS c_date, l.c_ts AS c_ts
FROM t__base l
INNER JOIN t25 r ON l.c_pk = r.c_pk
WHERE r.flag = 1;
SELECT t3.c_txt, t3.c_big
FROM t t1
RIGHT OUTER JOIN t t2 ON t1.c_int <= t2.c_int
LEFT OUTER JOIN t t3 ON t1.c_date = t3.c_ts;
-- Expected 2 rows (NULL, NULL). Actual 2 rows (NULL, NULL).
DROP TABLE t;
DROP TABLE t25;
DROP TABLE t__base;

-- ========== control: WHERE TRUE instead of WHERE flag = 1. Clean. ==========
CREATE TABLE t__base (
  c_pk BIGINT NOT NULL,
  c_int BIGINT,
  c_big DECIMAL(38, 0),
  c_txt VARCHAR,
  c_date DATE,
  c_ts TIMESTAMP
);
INSERT INTO t__base VALUES (3, NULL, -7, NULL, DATE '2030-06-01', NULL);
INSERT INTO t__base VALUES (8, -1, 1, 'x', DATE '2024-01-15', TIMESTAMP '1999-12-31 23:59:59');
CREATE TABLE t25 AS SELECT c_pk, 1 AS flag FROM t__base;
CREATE VIEW t AS
SELECT l.c_pk AS c_pk, l.c_int AS c_int, l.c_big AS c_big,
       l.c_txt AS c_txt, l.c_date AS c_date, l.c_ts AS c_ts
FROM t__base l
INNER JOIN t25 r ON l.c_pk = r.c_pk
WHERE TRUE;
SELECT t3.c_txt, t3.c_big
FROM t t1
RIGHT OUTER JOIN t t2 ON t1.c_int <= t2.c_int
LEFT OUTER JOIN t t3 ON t1.c_date = t3.c_ts;
-- Expected 2 rows (NULL, NULL). Actual 2 rows (NULL, NULL).
DROP VIEW t;
DROP TABLE t25;
DROP TABLE t__base;

-- ========== control: INTEGER payload plans Hash Join LEFT (refused). Clean. ==========
CREATE TABLE t__base (
  c_pk BIGINT NOT NULL,
  c_int BIGINT,
  c_big INTEGER,
  c_txt VARCHAR,
  c_date DATE,
  c_ts TIMESTAMP
);
INSERT INTO t__base VALUES (3, NULL, -7, NULL, DATE '2030-06-01', NULL);
INSERT INTO t__base VALUES (8, -1, 1, 'x', DATE '2024-01-15', TIMESTAMP '1999-12-31 23:59:59');
CREATE TABLE t25 AS SELECT c_pk, 1 AS flag FROM t__base;
CREATE VIEW t AS
SELECT l.c_pk AS c_pk, l.c_int AS c_int, l.c_big AS c_big,
       l.c_txt AS c_txt, l.c_date AS c_date, l.c_ts AS c_ts
FROM t__base l
INNER JOIN t25 r ON l.c_pk = r.c_pk
WHERE r.flag = 1;
SELECT t3.c_txt, t3.c_big
FROM t t1
RIGHT OUTER JOIN t t2 ON t1.c_int <= t2.c_int
LEFT OUTER JOIN t t3 ON t1.c_date = t3.c_ts;
-- Expected 2 rows (NULL, NULL). Actual 2 rows (NULL, NULL).
DROP VIEW t;
DROP TABLE t25;
DROP TABLE t__base;

-- ========== control: distilled construction + filter_pushdown off. Clean. ==========
CREATE TABLE t__base (
  c_pk BIGINT NOT NULL,
  c_int BIGINT,
  c_big DECIMAL(38, 0),
  c_txt VARCHAR,
  c_date DATE,
  c_ts TIMESTAMP
);
INSERT INTO t__base VALUES (3, NULL, -7, NULL, DATE '2030-06-01', NULL);
INSERT INTO t__base VALUES (8, -1, 1, 'x', DATE '2024-01-15', TIMESTAMP '1999-12-31 23:59:59');
CREATE TABLE t25 AS SELECT c_pk, 1 AS flag FROM t__base;
CREATE VIEW t AS
SELECT l.c_pk AS c_pk, l.c_int AS c_int, l.c_big AS c_big,
       l.c_txt AS c_txt, l.c_date AS c_date, l.c_ts AS c_ts
FROM t__base l
INNER JOIN t25 r ON l.c_pk = r.c_pk
WHERE r.flag = 1;
SET disabled_optimizers='filter_pushdown';
SELECT t3.c_txt, t3.c_big
FROM t t1
RIGHT OUTER JOIN t t2 ON t1.c_int <= t2.c_int
LEFT OUTER JOIN t t3 ON t1.c_date = t3.c_ts;
-- Expected 2 rows (NULL, NULL). Actual 2 rows (NULL, NULL).
SET disabled_optimizers='';
DROP VIEW t;
DROP TABLE t25;
DROP TABLE t__base;

-- ========== distilled (throws; run last / in a fresh session) ==========
-- Flag-table identity join as the builder emits it (`l.col AS col`, `WHERE flag = 1`).
-- Original query used TRY_CAST(t3.c_txt AS TIME); projecting t3.c_txt, t3.c_big is enough.
CREATE TABLE t__base (
  c_pk BIGINT NOT NULL,
  c_int BIGINT,
  c_big DECIMAL(38, 0),
  c_txt VARCHAR,
  c_date DATE,
  c_ts TIMESTAMP
);
INSERT INTO t__base VALUES (3, NULL, -7, NULL, DATE '2030-06-01', NULL);
INSERT INTO t__base VALUES (8, -1, 1, 'x', DATE '2024-01-15', TIMESTAMP '1999-12-31 23:59:59');
CREATE TABLE t25 AS SELECT c_pk, 1 AS flag FROM t__base;
CREATE VIEW t AS
SELECT l.c_pk AS c_pk, l.c_int AS c_int, l.c_big AS c_big,
       l.c_txt AS c_txt, l.c_date AS c_date, l.c_ts AS c_ts
FROM t__base l
INNER JOIN t25 r ON l.c_pk = r.c_pk
WHERE r.flag = 1;

-- Expected 2 rows (NULL, NULL). Actual: INTERNAL Error.
SELECT t3.c_txt, t3.c_big
FROM t t1
RIGHT OUTER JOIN t t2 ON t1.c_int <= t2.c_int
LEFT OUTER JOIN t t3 ON t1.c_date = t3.c_ts;
-- INTERNAL Error: dict-surviving join: narrowed column 3 received a
-- non-global-dictionary chunk; build pipeline is not single-source
