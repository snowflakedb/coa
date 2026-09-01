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

-- Dolt 8.0.31 @95218a00 (v2.2.3-9-g95218a00a, assertions off), go-mysql-server.
--
-- BUG: a correlated subquery over a "column-split-and-rejoin" view raises an internal planner
-- assertion:
--   1105  unable to find field with index 3 in row of 2 columns.
--   This is a bug. Please file an issue here: https://github.com/dolthub/dolt/issues
-- (the engine itself declares it a bug). The same query over a plain table returns correctly.
--
-- The view splits a surrogate-keyed relation into two disjoint column groups and LEFT JOINs them
-- back on the key, so the view's output columns are drawn from BOTH sides of the join. This is
-- exactly what the eqgen `eq_seq_key` row-preserving builder emits for the equivalent `t`.
--
-- ================= PART 1: CONCRETE, as the eq_seq_key builder emits it =================
CREATE TABLE base (id BIGINT, name VARCHAR(255), created_at VARCHAR(255));
INSERT INTO base VALUES (1,'a','x'),(2,'b','y'),(3,'a','z');

CREATE TABLE k AS SELECT id, name, created_at, ROW_NUMBER() OVER (ORDER BY id) AS sk FROM base;
CREATE VIEW vl AS SELECT id, sk FROM k;                 -- left column group  (2 cols)
CREATE VIEW vr AS SELECT name, created_at, sk FROM k;   -- right column group
CREATE VIEW t AS
  SELECT l.id AS id, r.name AS name, r.created_at AS created_at
  FROM vl l LEFT JOIN vr r ON l.sk = r.sk;              -- row-identical to base

-- BUG: any correlated subquery over the view errors.
SELECT t1.id FROM t t1 WHERE EXISTS (SELECT 1 FROM t t5 WHERE t5.id = t1.id);
-- Expected 3 rows (1,2,3).  Actual: ERROR 1105 "unable to find field with index 3 in row of 2 columns".

-- ================= PART 2: CONTROLS (each returns 3 rows correctly) =================
-- (a) same query over a PLAIN table -> OK:
DROP VIEW t; DROP VIEW vl; DROP VIEW vr; DROP TABLE k;
CREATE TABLE t AS SELECT id, name, created_at FROM base;
SELECT t1.id FROM t t1 WHERE EXISTS (SELECT 1 FROM t t5 WHERE t5.id = t1.id);   -- OK, 3 rows
DROP TABLE t;

-- (b) a plain self-join view (NOT a column split) -> OK:
CREATE VIEW t AS SELECT l.id AS id, r.name AS name, r.created_at AS created_at
                 FROM base l LEFT JOIN base r ON l.id = r.id;
SELECT t1.id FROM t t1 WHERE EXISTS (SELECT 1 FROM t t5 WHERE t5.id = t1.id);   -- OK, 3 rows
DROP VIEW t;

-- (c) the split view but a NON-correlated subquery -> OK:
CREATE TABLE k AS SELECT id, name, created_at, ROW_NUMBER() OVER (ORDER BY id) AS sk FROM base;
CREATE VIEW vl AS SELECT id, sk FROM k;
CREATE VIEW vr AS SELECT name, created_at, sk FROM k;
CREATE VIEW t AS SELECT l.id AS id, r.name AS name, r.created_at AS created_at
                 FROM vl l LEFT JOIN vr r ON l.sk = r.sk;
SELECT t1.id FROM t t1 WHERE t1.id NOT IN (SELECT t5.id FROM t t5 GROUP BY t5.id, t5.name);  -- OK, uncorrelated
