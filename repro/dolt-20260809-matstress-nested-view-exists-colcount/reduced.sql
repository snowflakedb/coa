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

-- Dolt 8.0.31 / DOLT_VERSION 2.2.3 (go-mysql-server).
--
-- BUG: a VIEW whose body is a multi-column projection over a derived table
--   CREATE VIEW t1 AS SELECT c_pk, c1 FROM (SELECT c_pk, c1 FROM t__base) AS eq_ns_1;
-- raises ERROR 1105
--   'In definition of view, derived table or common table expression,
--    SELECT list and column names list have different column counts'
-- when queried with a self-referential EXISTS:
--   SELECT * FROM t1 a WHERE EXISTS (SELECT 1 FROM t1 b WHERE b.c_pk = a.c_pk)
-- SELECT * / IN / JOIN over the same view succeed. A 1-column nested view is fine.
-- Flat views and inline derived tables (no VIEW) are fine.
--
-- Found by eqgen mat_stress (NestedSubqueryIdentityBuilder) on 2026-08-09.

-- ================= PART 1: MINIMAL FAILING CASE =================
CREATE TABLE t__base (c_pk BIGINT NOT NULL, c1 BIGINT);
INSERT INTO t__base VALUES (1, 10), (2, 20);
CREATE VIEW t1 AS SELECT c_pk, c1 FROM (SELECT c_pk, c1 FROM t__base) AS eq_ns_1;

SELECT * FROM t1;
-- OK: 2 rows

SELECT * FROM t1 a WHERE EXISTS (SELECT 1 FROM t1 b WHERE b.c_pk = a.c_pk);
-- Expected: 2 rows. Actual: ERROR 1105 column-count mismatch (above).

-- ================= PART 2: CONTROLS (each should succeed) =================
-- (a) same EXISTS over a flat (non-nested) view -> OK
DROP VIEW t1;
CREATE VIEW t1 AS SELECT c_pk, c1 FROM t__base;
SELECT * FROM t1 a WHERE EXISTS (SELECT 1 FROM t1 b WHERE b.c_pk = a.c_pk);
-- OK: 2 rows
DROP VIEW t1;

-- (b) nested view but only ONE output column -> OK
CREATE VIEW t1 AS SELECT c_pk FROM (SELECT c_pk FROM t__base) AS eq_ns_1;
SELECT * FROM t1 a WHERE EXISTS (SELECT 1 FROM t1 b WHERE b.c_pk = a.c_pk);
-- OK: 2 rows
DROP VIEW t1;

-- (c) nested CTAS (table, not view) + EXISTS -> OK
CREATE TABLE t1 AS SELECT c_pk, c1 FROM (SELECT c_pk, c1 FROM t__base) AS eq_ns_1;
SELECT * FROM t1 a WHERE EXISTS (SELECT 1 FROM t1 b WHERE b.c_pk = a.c_pk);
-- OK: 2 rows
DROP TABLE t1;

-- (d) nested multi-col view + IN (not EXISTS) -> OK
CREATE VIEW t1 AS SELECT c_pk, c1 FROM (SELECT c_pk, c1 FROM t__base) AS eq_ns_1;
SELECT * FROM t1 a WHERE a.c_pk IN (SELECT b.c_pk FROM t1 b);
-- OK: 2 rows

-- (e) nested multi-col view + self JOIN -> OK
SELECT * FROM t1 a JOIN t1 b ON a.c_pk = b.c_pk;
-- OK: 2 rows
DROP VIEW t1;

-- (f) inline derived (no persisted VIEW) + EXISTS -> OK
SELECT * FROM (SELECT c_pk, c1 FROM t__base) AS a
 WHERE EXISTS (SELECT 1 FROM (SELECT c_pk, c1 FROM t__base) AS b WHERE b.c_pk = a.c_pk);
-- OK: 2 rows
