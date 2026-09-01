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

-- Dolt: accepts HAVING referencing a non-grouped, non-aggregated column that MySQL/MariaDB reject (1054)
--
-- Engine    : dolt 2.2.3 (server reports VERSION() = 8.0.31)
-- Reference : MariaDB 11.4.12 -- rejects every `wrong` block below with ERROR 1054
-- Session   : all defaults; also accepted under sql_mode='ONLY_FULL_GROUP_BY'
-- Findings  : dolt_20260809-052933/ -- 11 of 19 mismatches, listed in bug_report.md
--
-- Blocks are `-- >>> BLOCK: <name> dolt=<accept|reject> maria=<accept|reject>`; each was run against
-- BOTH engines in a fresh database and the two verdicts checked, so the compatibility claim is
-- re-runnable rather than asserted.
--
-- Every block uses the same table:
--   t(g,b) = (1,10),(1,20),(2,30)     -- b is neither grouped nor aggregated in the `wrong` blocks


-- >>> BLOCK: having-bare-col-with-aggregate  dolt=accept  maria=reject
-- THE BUG. `b` is neither in GROUP BY nor aggregated. MySQL/MariaDB: ERROR 1054 Unknown column 'b' in
-- 'HAVING'. Dolt accepts and evaluates it against some row of the group.
CREATE TABLE t (g BIGINT, b BIGINT);
INSERT INTO t VALUES (1,10),(1,20),(2,30);
SELECT g, COUNT(*) FROM t GROUP BY g HAVING MAX(b) > b;


-- >>> BLOCK: having-bare-col-with-aggregate-expr-groupby  dolt=accept  maria=reject
-- Same with GROUP BY on an expression, which is the shape the generator produced.
CREATE TABLE t (g BIGINT, b BIGINT);
INSERT INTO t VALUES (1,10),(1,20),(2,30);
SELECT g+0, COUNT(*) FROM t GROUP BY g+0 HAVING MAX(b) > b;


-- >>> BLOCK: having-bare-col-alone  dolt=reject  maria=reject
-- Without an aggregate beside it in HAVING, Dolt rejects too (1105 vs MariaDB's 1054). This is the
-- control that isolates the accepting ingredient: the aggregate in the HAVING predicate.
CREATE TABLE t (g BIGINT, b BIGINT);
INSERT INTO t VALUES (1,10),(1,20),(2,30);
SELECT g, COUNT(*) FROM t GROUP BY g HAVING b > 15;


-- >>> BLOCK: having-bare-col-alone-expr-groupby  dolt=reject  maria=reject
CREATE TABLE t (g BIGINT, b BIGINT);
INSERT INTO t VALUES (1,10),(1,20),(2,30);
SELECT g+0, COUNT(*) FROM t GROUP BY g+0 HAVING b > 15;


-- >>> BLOCK: having-grouped-col-legal  dolt=accept  maria=accept
-- Legal: HAVING on a grouped column. Both engines agree, returning (2,1).
CREATE TABLE t (g BIGINT, b BIGINT);
INSERT INTO t VALUES (1,10),(1,20),(2,30);
SELECT g, COUNT(*) FROM t GROUP BY g HAVING g > 1;


-- >>> BLOCK: having-aggregate-legal  dolt=accept  maria=accept
-- Legal: HAVING on an aggregate. Both engines agree, returning (1,2).
CREATE TABLE t (g BIGINT, b BIGINT);
INSERT INTO t VALUES (1,10),(1,20),(2,30);
SELECT g, COUNT(*) FROM t GROUP BY g HAVING COUNT(*) > 1;


-- >>> BLOCK: select-bare-col-arbitrary  dolt=accept  maria=accept
-- NOT part of the bug, kept as a contrast: a bare non-grouped column in the SELECT list is allowed with
-- ONLY_FULL_GROUP_BY off, and the two engines legitimately pick different rows --
-- MariaDB (1,10),(2,30) vs Dolt (1,20),(2,30). Documented-arbitrary in both; do not file this.
CREATE TABLE t (g BIGINT, b BIGINT);
INSERT INTO t VALUES (1,10),(1,20),(2,30);
SELECT g, b FROM t GROUP BY g;
