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

-- MariaDB 11.4.12 (docker mariadb:11.4). A trivial MERGE/identity VIEW queried with TWO
-- window functions and `GROUP BY` on unprefixed columns raises
--   ERROR 1052: Column 'id' in GROUP BY is ambiguous
-- The same statement on the base table, on ALGORITHM=TEMPTABLE, on MySQL 9.7.2 (identity
-- view included), with a single window, without GROUP BY, or with GROUP BY t1.id, t1.name
-- succeeds. No join is required -- view merge of two windows appears to clone the base
-- table so that a one-relation GROUP BY id looks like two `id` columns.
--
-- sql_mode: STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES (not load-bearing).
-- charset/collation: utf8mb4 / utf8mb4_bin (finding); utf8mb4_0900_bin is not required.
--
-- Origin: 14 one-sided errors in mariadb_20260814-021538 (NATURAL JOIN + two windows +
-- GROUP BY id, name over a window/DISTINCT view). The NATURAL JOIN is not load-bearing.


-- =====================================================================================
-- PART 1 -- CONCRETE: compact rebuild of error_round72_5.sql / error_round2_9.sql.
-- The hunt's equivalent was a duplicate-and-reduce or DISTINCT view; an identity view
-- is enough. The workload had NATURAL LEFT JOIN + two windows + GROUP BY id, name.
-- =====================================================================================
CREATE TABLE b (c_pk BIGINT NOT NULL, id BIGINT, name VARCHAR(255), created_at VARCHAR(255));
INSERT INTO b VALUES (1, NULL, NULL, NULL);
INSERT INTO b VALUES (5, 2, 'abc', 'abc');
INSERT INTO b VALUES (7, 2, 'o''brien', NULL);
CREATE VIEW t AS SELECT * FROM b;

SELECT MIN(name) OVER (PARTITION BY id), MIN(id) OVER (ORDER BY name)
FROM t AS t1 NATURAL LEFT OUTER JOIN t AS t2
GROUP BY id, name;
-- Expected 3 rows (what the base table returns).
-- Actual   ERROR 1052 (23000): Column 'id' in GROUP BY is ambiguous


-- =====================================================================================
-- PART 2 -- DISTILLED. No join, no NATURAL, no SHA2, no extra SELECT items.
-- PART 1 and PART 2 each need a fresh database (both create b and t).
-- =====================================================================================
CREATE TABLE b (id BIGINT, name VARCHAR(255));
INSERT INTO b VALUES (NULL, NULL);
INSERT INTO b VALUES (2, 'abc');
INSERT INTO b VALUES (2, 'o''brien');
CREATE VIEW t AS SELECT * FROM b;

SELECT MIN(name) OVER (PARTITION BY id), MIN(id) OVER (ORDER BY name)
FROM t
GROUP BY id, name;
-- Expected 3 rows: (NULL, NULL), ('abc', 2), ('abc', 2)
-- Actual   ERROR 1052 (23000): Column 'id' in GROUP BY is ambiguous


-- =====================================================================================
-- PART 3 -- CONTROLS (fresh database each; all succeed with 3 rows unless noted).
-- =====================================================================================

-- (a) base table, same two windows + GROUP BY:
-- CREATE TABLE t AS SELECT * FROM b;
-- => 3 rows  ✓

-- (b) ALGORITHM=TEMPTABLE (blocks merge):
-- CREATE ALGORITHM=TEMPTABLE VIEW t AS SELECT * FROM b;
-- => 3 rows  ✓

-- (c) qualify the grouping columns:
-- SELECT MIN(t1.name) OVER (PARTITION BY t1.id), MIN(t1.id) OVER (ORDER BY t1.name)
-- FROM t t1 GROUP BY t1.id, t1.name;
-- => 3 rows  ✓

-- (d) a single window:
-- SELECT MIN(name) OVER (PARTITION BY id) FROM t GROUP BY id, name;
-- => 3 rows  ✓

-- (e) two windows, no GROUP BY:
-- SELECT MIN(name) OVER (PARTITION BY id), MIN(id) OVER (ORDER BY name) FROM t;
-- => 3 rows  ✓

-- (f) two ordinary aggregates, no windows:
-- SELECT MIN(name), MIN(id) FROM t GROUP BY id, name;
-- => 3 rows  ✓

-- (g) two windows with the same PARTITION BY (no ORDER BY on the second):
-- SELECT MIN(name) OVER (PARTITION BY id), MAX(name) OVER (PARTITION BY id)
-- FROM t t1 NATURAL LEFT OUTER JOIN t t2 GROUP BY id, name;
-- => 3 rows  ✓  (windows can be fused; no clone / no 1052)

-- (h) MySQL 9.7.2, identity view, PART 2 query:
-- => 3 rows  ✓  (MariaDB-specific)
