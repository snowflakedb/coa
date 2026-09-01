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

-- Load-bearing: a VIEW whose body is an INNER/CROSS JOIN, then a 3-way
-- comma (or CROSS) join that includes that view and a WHERE equi-predicate
-- across view and another table. New analyzer + join-order optimizer.
-- Expected: 2 rows (same as SETTINGS enable_analyzer=0 / query_plan_optimize_join_order_limit=0).
-- Actual: Code: 49 LOGICAL_ERROR Left and right columns have same names
--         (both sides cite __table2.*).

CREATE TABLE L (c_pk Int64, c_int Int64, c_big Int64, eq Int64) ENGINE = MergeTree ORDER BY tuple();
CREATE TABLE R (eq Int64) ENGINE = MergeTree ORDER BY tuple();
INSERT INTO L VALUES (1, 100, 1, 1), (2, 200, 2, 1);
INSERT INTO R VALUES (1);

CREATE VIEW t0 AS
SELECT l.c_pk AS c_pk, l.c_int AS c_int, l.c_big AS c_big
FROM L AS l INNER JOIN R AS r ON l.eq = r.eq;

CREATE TABLE t1 (c_pk Int64, c_int Int64, c_big Int64) ENGINE = MergeTree ORDER BY tuple();
CREATE TABLE t2 (c_pk Int64, c_int Int64, c_big Int64) ENGINE = MergeTree ORDER BY tuple();
INSERT INTO t1 VALUES (10, 10, 10);
INSERT INTO t2 VALUES (1, 1, 100), (2, 2, 200);

-- BUGGY (new analyzer default):
SELECT * FROM t0, t1, t2 WHERE t0.c_int = t2.c_big;
-- Code: 49. Left and right columns have same names: [__table2..., __table3...], [__table2...]

-- CONTROLS (all return the two expected rows):
SELECT * FROM t0, t1, t2 WHERE t0.c_int = t2.c_big SETTINGS enable_analyzer = 0;
SELECT * FROM t0, t1, t2 WHERE t0.c_int = t2.c_big SETTINGS query_plan_optimize_join_order_limit = 0;
SELECT * FROM t0, t2 WHERE t0.c_int = t2.c_big;  -- drop t1: OK
-- Wrap the join view in a derived table (forces a boundary): also OK
CREATE VIEW t0s AS SELECT * FROM (
  SELECT l.c_pk AS c_pk, l.c_int AS c_int, l.c_big AS c_big
  FROM L AS l INNER JOIN R AS r ON l.eq = r.eq
);
SELECT * FROM t0s, t1, t2 WHERE t0s.c_int = t2.c_big;
