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

-- MySQL 9.7.2 (docker mysql:9.7.2). VAR_SAMP() gives a different numeric answer for the exact same
-- input multiset depending on whether the optimizer picks a table scan or an index range scan.
--
-- sql_mode: STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES (not load-bearing).

CREATE TABLE t (c_pk BIGINT NOT NULL, c_big BIGINT, c_txt VARCHAR(255));
INSERT INTO t VALUES (1, NULL, NULL);
INSERT INTO t VALUES (2, 1, 'trailing ');
INSERT INTO t VALUES (3, 42, 'o''brien');
INSERT INTO t VALUES (4, 0, NULL);
INSERT INTO t VALUES (5, 2, 'Zed');
INSERT INTO t VALUES (6, 42, 'a');
INSERT INTO t VALUES (7, -7, 'abc');
INSERT INTO t VALUES (8, 1, 'trailing ');

-- No index yet: table scan.
SELECT VAR_SAMP((c_big | -1379963)) FROM t WHERE ('.iw#8[' <= c_txt);
-- => 317429959884.8

CREATE INDEX t_idx ON t (c_txt(10));

-- Same table, same 8 rows, same query. Optimizer now prefers the new index.
SELECT VAR_SAMP((c_big | -1379963)) FROM t WHERE ('.iw#8[' <= c_txt);
-- => 317580954828.8   <-- DIFFERENT ANSWER, same data

-- Controls that isolate the plan (not the data) as the variable:
SELECT VAR_SAMP((c_big | -1379963)) FROM t FORCE INDEX (t_idx)  WHERE ('.iw#8[' <= c_txt); -- 317580954828.8
SELECT VAR_SAMP((c_big | -1379963)) FROM t IGNORE INDEX (t_idx) WHERE ('.iw#8[' <= c_txt); -- 317429959884.8

-- Ground truth (exact rational arithmetic over the 6 matching rows' (c_big | -1379963) values):
-- variance = 317373688955.8667 (both engine answers are off by 300-650ppm; neither is exact, but
-- they must at least agree with EACH OTHER since the input multiset is identical either way).
