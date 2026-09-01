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

-- MISMATCH (reduced)  engine=mysql 26.7.0-debug @06a5c1c9 (assertions on, mysql-main/bin)  seed=249296182  table=t
-- sql_mode: NO_BACKSLASH_ESCAPES  (required -- see below)
-- character_set_connection: utf8mb4
-- collation_connection: utf8mb4_0900_bin
--
-- Reduced from mismatch_round13_0.sql (13-view equivalent chain, 6-expression GROUP BY query,
-- 8 rows) to the minimal trigger by execution-guided delta-debugging against the live server.
--
-- BUG: a REGEXP backslash character-class escape (\w \d \s \W \S \D) used in a WHERE over a
-- UNION ALL derived table drops every matching row, while the SAME predicate over the base table
-- (or a plain, non-UNION derived table) matches correctly. The base scan honors NO_BACKSLASH_ESCAPES
-- (pattern stays '\w', matches a word char); the UNION ALL derived branch behaves as if the mode
-- were off ('\w' collapses to 'w', matching nothing). Same session, same sql_mode, same data.
--
-- NO_BACKSLASH_ESCAPES is required only so the pattern reaches the regex engine as '\w' rather than
-- 'w'; the defect is the inconsistent handling of that pattern across the plan, not the mode itself.
-- Literals, anchors, alternation and POSIX classes ([[:alpha:]]) pass the UNION correctly -- only
-- the backslash-class escapes break.

SET SESSION sql_mode = 'NO_BACKSLASH_ESCAPES';

CREATE TABLE t (name VARCHAR(255));
INSERT INTO t VALUES ('a');

-- Expected 1 row, actual 1 row (correct):
SELECT name FROM t WHERE name REGEXP '\w';

-- Expected 1 row, actual 0 rows (WRONG):
SELECT name FROM (SELECT * FROM t UNION ALL SELECT * FROM t WHERE 0) x WHERE name REGEXP '\w';
