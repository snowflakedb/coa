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

-- MariaDB 12.3.3 @2883bccc (release, assertions off, mariadb-release/bin).
-- Session from the finding: sql_mode ONLY_FULL_GROUP_BY,NO_BACKSLASH_ESCAPES,STRICT_ALL_TABLES,
-- NO_ZERO_IN_DATE,NO_ZERO_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION; utf8mb4 /
-- utf8mb4_nopad_bin. (The bug is independent of sql_mode/charset -- the minimal repro is integer-only.)
--
-- BUG: when two window functions appear in one SELECT and the SECOND has OVER (PARTITION BY <constant>
-- ORDER BY …), MariaDB coalesces them into a SINGLE filesort keyed only by the second window's
-- (constant) ORDER BY and DROPS the first window's own ORDER BY. The first window's aggregate is then
-- computed over the wrong row order, giving wrong, run-order-dependent values. EXPLAIN FORMAT=JSON:
--   window_functions_computation.sorts == [ "'3', '3' desc" ]   -- window A's `id < 1 desc` sort is gone.
-- MySQL 8/9 keeps the two windows' sorts separate and returns the correct, order-independent result.

CREATE TABLE t (id BIGINT);
INSERT INTO t VALUES (-3),(-1),(0),(1),(2),(2),(NULL),(7);

-- (1) THE BUG: window A = SUM(41) OVER (ORDER BY id<1 DESC) is corrupted by the presence of window B.
--     Correct (RANGE peers over key id<1: 3 rows key=1 ->123, 4 rows key=0 ->287, 1 NULL ->328):
--         {123,123,123, 287,287,287,287, 328}   <- MySQL returns exactly this.
--     MariaDB actual: wrong AND run-order-dependent, e.g. {41,82,123, 246,246,246, 287, 328}.
SELECT id,
       SUM(41) OVER (ORDER BY id < 1 DESC)                        AS a,
       MAX(id) OVER (PARTITION BY '3' ORDER BY '3' DESC)          AS b
FROM t;
-- Expected column a multiset = {123 x3, 287 x4, 328}; MariaDB gives garbage (frame sizes 1..8).

-- (2) CONTROL -- window A ALONE is correct (and equals MySQL):
SELECT id, SUM(41) OVER (ORDER BY id < 1 DESC) AS a FROM t;
-- a = {123 x3, 287 x4, 328}  ✓

-- (3) CONTROL -- window B PARTITION BY a REAL column (not a constant): window A is correct again:
SELECT id,
       SUM(41) OVER (ORDER BY id < 1 DESC)          AS a,
       MAX(id) OVER (PARTITION BY id ORDER BY '3' DESC) AS b
FROM t;
-- a = {123 x3, 287 x4, 328}  ✓  (the constant partition is the trigger)

-- (4) CONTROL -- window B with NO ORDER BY: window A is correct again:
SELECT id,
       SUM(41) OVER (ORDER BY id < 1 DESC)  AS a,
       MAX(id) OVER (PARTITION BY '3')      AS b
FROM t;
-- a = {123 x3, 287 x4, 328}  ✓  (window B needs both PARTITION-BY-constant AND its own ORDER BY)
