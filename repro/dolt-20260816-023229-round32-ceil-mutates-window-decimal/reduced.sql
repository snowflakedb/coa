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

-- Dolt 8.0.31 / v2.2.3-49-ga995f245c @a995f245c, go-mysql-server
-- v0.20.1-0.20260805191915-e5eafe0da809.
--
-- CEIL()/FLOOR() of a DECIMAL mutate the *apd.Decimal in place
-- (sql.DecimalCtx.Ceil(num, num)). Window MAX/SUM reuse that same pointer
-- for every peer in the partition, so projecting CEIL(d) alongside d
-- overwrites d for every row after the first peer.
--
-- Origin: dolt_rich_shuffle/dolt_20260816-023229/mismatch_round32_10.sql
-- (identity MAX() OVER (PARTITION BY col) view; workload CEIL/ASCII/GREATEST).
--
-- HOW TO RUN: each PART redefines t/v; use a fresh database per PART.


-- =====================================================================================
-- PART 1 -- CONCRETE: identity window view as the eqgen builder emits it, plus CEIL.
-- =====================================================================================
CREATE TABLE t (c_pk BIGINT NOT NULL, c_dec DECIMAL(10,2), c_chr VARCHAR(255));
INSERT INTO t VALUES (1, 12.34, 'a');
INSERT INTO t VALUES (2, 12.34, '');
INSERT INTO t VALUES (3, 999.99, 'abc');

CREATE VIEW v AS
SELECT c_pk,
       MAX(c_dec) OVER (PARTITION BY c_dec) AS c_dec,
       c_chr
FROM t;

SELECT c_pk, c_dec, CEIL(c_dec) FROM v ORDER BY c_pk;
-- Expected: (1, 12.34, 13), (2, 12.34, 13), (3, 999.99, 1000)
-- Actual:   (1, 12.34, 13), (2, 13.00, 13), (3, 999.99, 1000)
--           peer 2's c_dec has become CEIL of the shared window value.


-- =====================================================================================
-- PART 2 -- DISTILLED. Two rows, one DECIMAL, identity MAX window, CEIL in the SELECT.
-- A view is not required: an inline derived table is enough.
-- =====================================================================================
CREATE TABLE t (id BIGINT, d DECIMAL(10,2));
INSERT INTO t VALUES (1, 12.34), (2, 12.34);

SELECT id, d, CEIL(d)
FROM (SELECT id, MAX(d) OVER (PARTITION BY d) AS d FROM t) s
ORDER BY id;
-- Expected: (1, 12.34, 13), (2, 12.34, 13)
-- Actual:   (1, 12.34, 13), (2, 13.00, 13)


-- =====================================================================================
-- PART 3 -- CONTROLS
-- =====================================================================================
-- (a) heap table, no window -- clean
CREATE TABLE t (id BIGINT, d DECIMAL(10,2));
INSERT INTO t VALUES (1, 12.34), (2, 12.34);
SELECT id, d, CEIL(d) FROM t ORDER BY id;
-- Expected/actual: (1, 12.34, 13), (2, 12.34, 13)

-- (b) windowed SELECT * without CEIL -- clean (shared pointer is not mutated)
CREATE TABLE t (id BIGINT, d DECIMAL(10,2));
INSERT INTO t VALUES (1, 12.34), (2, 12.34);
CREATE VIEW v AS SELECT id, MAX(d) OVER (PARTITION BY d) AS d FROM t;
SELECT * FROM v ORDER BY id;
-- Expected/actual: (1, 12.34), (2, 12.34)

-- (c) one row -- nothing left to overwrite
CREATE TABLE t (id BIGINT, d DECIMAL(10,2));
INSERT INTO t VALUES (1, 12.34);
CREATE VIEW v AS SELECT id, MAX(d) OVER (PARTITION BY d) AS d FROM t;
SELECT id, d, CEIL(d) FROM v;
-- Expected/actual: (1, 12.34, 13)

-- (d) BIGINT / DOUBLE windows -- CEIL does not take the *apd.Decimal path
CREATE TABLE t (id BIGINT, d BIGINT);
INSERT INTO t VALUES (1, 12), (2, 12);
CREATE VIEW v AS SELECT id, MAX(d) OVER (PARTITION BY d) AS d FROM t;
SELECT id, d, CEIL(d) FROM v ORDER BY id;
-- Expected/actual: (1, 12, 12), (2, 12, 12)

CREATE TABLE t (id BIGINT, d DOUBLE);
INSERT INTO t VALUES (1, 12.34), (2, 12.34);
CREATE VIEW v AS SELECT id, MAX(d) OVER (PARTITION BY d) AS d FROM t;
SELECT id, d, CEIL(d) FROM v ORDER BY id;
-- Expected/actual: (1, 12.34, 13), (2, 12.34, 13)

-- (e) ROUND keeps DECIMAL and does not mutate in place
CREATE TABLE t (id BIGINT, d DECIMAL(10,2));
INSERT INTO t VALUES (1, 12.34), (2, 12.34);
CREATE VIEW v AS SELECT id, MAX(d) OVER (PARTITION BY d) AS d FROM t;
SELECT id, d, ROUND(d) FROM v ORDER BY id;
-- Expected/actual: (1, 12.34, 12.00), (2, 12.34, 12.00)

-- (f) FLOOR has the same in-place bug
CREATE TABLE t (id BIGINT, d DECIMAL(10,2));
INSERT INTO t VALUES (1, 12.34), (2, 12.34);
CREATE VIEW v AS SELECT id, MAX(d) OVER (PARTITION BY d) AS d FROM t;
SELECT id, d, FLOOR(d) FROM v ORDER BY id;
-- Expected: (1, 12.34, 12), (2, 12.34, 12)
-- Actual:   (1, 12.34, 12), (2, 12.00, 12)
