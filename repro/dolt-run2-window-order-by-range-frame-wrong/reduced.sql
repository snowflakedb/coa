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
-- BUG: an aggregate window function with `OVER (ORDER BY <col>)` and no explicit frame does NOT
-- use the SQL-standard / MySQL default RANGE frame (UNBOUNDED PRECEDING .. CURRENT ROW), under
-- which all peer rows (equal ORDER BY key) share one frame end and therefore one value. Dolt
-- instead produces per-row values that differ across peers and are not even a consistent running
-- total -- SUM/AVG are simply wrong. COUNT happens to be right.
--
-- All rows below share name='a', so every row is a peer: MySQL gives every row the full-group
-- aggregate.

CREATE TABLE t (id BIGINT, name VARCHAR(255));
INSERT INTO t VALUES (1,'a'),(2,'a'),(3,'a');

SELECT id, SUM(id)  OVER (ORDER BY name) FROM t ORDER BY id;
-- MySQL 9.7: (1,6),(2,6),(3,6)      -- RANGE peers: all get 1+2+3=6
-- dolt     : (1,4),(2,6),(3,3)      -- WRONG: three different, non-monotonic values

SELECT id, AVG(id)  OVER (ORDER BY name) FROM t ORDER BY id;
-- MySQL 9.7: (1,2.0),(2,2.0),(3,2.0)
-- dolt     : (1,2.0),(2,2.0),(3,3.0)   -- WRONG on the last peer

SELECT id, COUNT(*) OVER (ORDER BY name) FROM t ORDER BY id;
-- MySQL 9.7: (1,3),(2,3),(3,3)
-- dolt     : (1,3),(2,3),(3,3)         -- correct (COUNT unaffected)

-- CONTROLS proving the root cause is default-frame resolution (default should be RANGE, dolt uses ROWS):
SELECT id, SUM(id) OVER (ORDER BY name RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) FROM t ORDER BY id;
-- dolt: (1,6),(2,6),(3,6)   -- explicit RANGE is CORRECT (== MySQL default)
SELECT id, SUM(id) OVER (ORDER BY name ROWS  BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) FROM t ORDER BY id;
-- dolt: (1,4),(2,6),(3,3)   -- explicit ROWS == dolt's bare `ORDER BY name`  => the default frame is ROWS (bug)
