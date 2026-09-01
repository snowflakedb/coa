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

-- =====================================================================================
-- Dolt: SPACE(n) is super-linear (~quadratic) in n, so a generated SPACE with a large
-- argument hangs the server indefinitely. REPEAT('x', n) is linear and fine.
--
-- Engine:  dolt 8.0.31, source v2.2.3-49-ga995f245c, commit a995f245c032, assertions off
--          go-mysql-server v0.20.1-0.20260805191915-e5eafe0da809
-- max_allowed_packet: 1073741824 (1 GiB) -- so none of these exceed the packet limit
--
-- Measured wall-clock, one connection, nothing else running:
--
--     n            dolt SPACE(n)      MySQL 9.7 SPACE(n)
--     10,000       0.0 s              -
--     50,000       0.1 s              -
--     100,000      0.4 s              -
--     200,000      1.4 s              -
--     400,000      5.0 s              0.000 s
--     2,000,000    did not finish     0.001 s
--     24,691,356   did not finish     0.009 s
--
-- Doubling n multiplies dolt's time by ~3.6, i.e. O(n^1.85) -- consistent with building
-- the string by repeated concatenation rather than a single allocation. REPEAT('x', n)
-- is linear on dolt (400,000 in 0.0 s), so this is specific to SPACE.
--
-- Extrapolating 5.0 s at 400,000 quadratically puts SPACE(24,691,356) at ~5 hours for a
-- single call. Observed in the wild: a eqgen round evaluated one such call per row inside
-- greatest(); the server ran 2 h 01 m wall / 6 h 34 m CPU at 325% and 1.4 GiB RSS without
-- completing, and had to be killed.
-- =====================================================================================

-- ============================ PART 1 -- as the fuzzer generated it ===================
-- dolt_run9 round35. The argument is a constant-folding of ('1' - '3') * '-12345678'
-- = (-2) * (-12345678) = 24,691,356, evaluated once per row inside greatest().

CREATE TABLE t (id BIGINT, name VARCHAR(255));
INSERT INTO t VALUES (1,'a'),(2,'b'),(3,'a');

-- DO NOT RUN unless you are willing to kill the server: no completion observed in 2 h.
-- SELECT COUNT(*) FROM t
-- WHERE greatest(SPACE(('1' - '3') * '-12345678'), name, to_base64(COALESCE(name, name)), name)
--       BETWEEN '©' AND CAST(name AS CHAR(255));

-- ============================ PART 2 -- distilled, bounded ==========================
-- Each of these returns; the point is the shape of the curve, not any single number.

SELECT LENGTH(SPACE(10000));    -- dolt 0.0 s
SELECT LENGTH(SPACE(50000));    -- dolt 0.1 s
SELECT LENGTH(SPACE(100000));   -- dolt 0.4 s
SELECT LENGTH(SPACE(200000));   -- dolt 1.4 s
SELECT LENGTH(SPACE(400000));   -- dolt 5.0 s   <-- MySQL 9.7: 0.000 s

-- ============================ PART 3 -- controls =====================================

-- C1  REPEAT is linear: the same output size costs nothing.        -> 0.0 s, LENGTH=400000
SELECT LENGTH(REPEAT('x', 400000));

-- C2  Not a packet-size rejection: the limit is 1 GiB, far above these sizes.
SELECT @@max_allowed_packet;

-- C3  MySQL 9.7 computes the value the fuzzer asked for in 9 ms.   -> LENGTH=24691356
-- SELECT LENGTH(SPACE(24691356));   -- run this one on MySQL, not on dolt
