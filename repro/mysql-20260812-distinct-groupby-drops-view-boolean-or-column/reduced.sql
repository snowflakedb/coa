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

-- MySQL 9.7.2 (docker mysql:9.7.2). `SELECT DISTINCT ... GROUP BY <same list>`, where one of the
-- projected/grouped expressions is a boolean `||` (logical OR, no PIPES_AS_CONCAT) over a column
-- read through a merged VIEW and combined with a CROSS JOIN, silently drops every group whose
-- boolean expression is FALSE(0) -- keeping only the NULL-valued groups.
--
-- sql_mode: STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES (not load-bearing; the bug
-- is about `||` == PIPES_AS_CONCAT-off logical OR, which is the default and what NO_BACKSLASH_ESCAPES
-- does not change).

CREATE TABLE t (c_pk BIGINT NOT NULL, c_txt VARCHAR(255), c_chr VARCHAR(255));
INSERT INTO t VALUES (1, NULL, NULL);
INSERT INTO t VALUES (2, 'a', 'a');
INSERT INTO t VALUES (3, 'o''brien', '');
INSERT INTO t VALUES (4, NULL, 'Zed');

CREATE VIEW t0 AS SELECT * FROM t;

-- Each of DISTINCT alone and GROUP BY alone is correct -- 8 rows, both a 0-group and a NULL-group
-- for every a.c_pk (t is cross-joined against t0's 4 rows, and t0's (c_txt||c_chr) values are
-- {NULL, 0, 0, NULL} -- both values present for every a.c_pk pairing):
SELECT DISTINCT a.c_pk, (t0.c_txt||t0.c_chr) FROM t a, t0 ORDER BY a.c_pk;
SELECT a.c_pk, (t0.c_txt||t0.c_chr) FROM t a, t0 GROUP BY a.c_pk, (t0.c_txt||t0.c_chr) ORDER BY a.c_pk;

-- DISTINCT + GROUP BY together on the exact same list: WRONG. Every row whose boolean-OR value is
-- 0 vanishes -- only the NULL rows survive.
SELECT DISTINCT a.c_pk, (t0.c_txt||t0.c_chr) FROM t a, t0 GROUP BY a.c_pk, (t0.c_txt||t0.c_chr) ORDER BY a.c_pk;
-- => (1,NULL),(2,NULL),(3,NULL),(4,NULL)      -- 4 rows, all NULL
-- expected, and what the two queries above give:
-- => (1,NULL),(1,0),(2,NULL),(2,0),(3,NULL),(3,0),(4,NULL),(4,0)   -- 8 rows

-- Control: replace the VIEW with an equivalent materialized TABLE. Same rows, same query -- correct.
-- CREATE TABLE t0 AS SELECT * FROM t;   (instead of CREATE VIEW t0 ...)
