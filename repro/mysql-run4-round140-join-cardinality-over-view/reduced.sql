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

-- MISMATCH (wrong result: semijoin miscounts over a LATERAL-derived-table view)
-- engine=mysql 26.7.0-debug @06a5c1c9 (assertions on, mysql-main/bin)
-- seed=369625159  (original finding: logs/mysql_run4/mismatch_round140_0.sql)
-- sql_mode: ONLY_FULL_GROUP_BY,NO_BACKSLASH_ESCAPES,STRICT_ALL_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION
-- charset utf8mb4 / collation utf8mb4_0900_bin
--
-- A COUNT(*) over `t1 CROSS JOIN t3 WHERE t3.name IN (<3-table non-equi subquery>)` returns
--   * 30 over the base table t
--   * 27 over a ROW-IDENTICAL view t defined with a LATERAL derived table
-- Join+filter cardinality is pure relational algebra (plan- and row-order-invariant), so 30 vs 27
-- over identical rows is an unambiguous WRONG-RESULT bug. The view side drops rows.
--
-- LOAD-BEARING COMPOSITION (each necessary; verified by control):
--   * the equivalent t is a LATERAL-derived-table view:
--       CREATE VIEW t AS SELECT ll.* FROM tb AS ls, LATERAL (SELECT ls.id, ls.name, ls.created_at) AS ll
--     -- a PLAIN view (SELECT * FROM tb) does NOT diverge (30 vs 30).
--   * the query's IN (...) is turned into a SEMIJOIN: `SET optimizer_switch='semijoin=off'` makes
--     both sides return 30 -> the bug is in semijoin (duplicate-weedout over the LATERAL-materialized
--     derived tables; see EXPLAIN in bug_report.md).
--   * a THIRD projected column is required: dropping created_at (2-col table/view) does NOT diverge.
--   * the IN subquery must be the 3-table NON-EQUI join (t4.id != t5.id, then t5.id = t6.id);
--     a 2-table subquery does NOT diverge.
--   * the outer CROSS JOIN is required; a single outer table does NOT diverge.
--   * a NULL name row + duplicate names are required; 6 rows is minimal (no 5-row subset diverges).
-- Reduced-away (all unnecessary, from the original 14-statement equivalence chain): the parity-split
-- CTAS, the UNION ALL, the ROW_NUMBER() window, the RIGHT-JOIN flag round-trip, the prefix index,
-- and the LENGTH(id) VIRTUAL generated column. Original query's DISTINCT + 4 window functions +
-- GROUP BY + HAVING + CASE were also reduced away.

-- ================= BASE (run in a fresh DB) =================
CREATE TABLE t (`id` BIGINT, `name` VARCHAR(255), `created_at` VARCHAR(255));
INSERT INTO t VALUES (1,'a','x'),(2,'b','x'),(3,'b','x'),(4,NULL,'x'),(4,'c','x'),(5,'d','x');
-- Expected 30 rows, actual 30:
SELECT COUNT(*) FROM t AS t1 CROSS JOIN t AS t3
WHERE t3.name IN (SELECT t6.name FROM t AS t4 JOIN t AS t5 ON t4.id != t5.id JOIN t AS t6 ON t5.id = t6.id);

-- ============== EQUIVALENT (run in a SEPARATE fresh DB) ==============
CREATE TABLE t (`id` BIGINT, `name` VARCHAR(255), `created_at` VARCHAR(255));
INSERT INTO t VALUES (1,'a','x'),(2,'b','x'),(3,'b','x'),(4,NULL,'x'),(4,'c','x'),(5,'d','x');
-- rebuild t as a row-identical LATERAL-derived-table view (the load-bearing construct):
ALTER TABLE t RENAME TO tb;
CREATE VIEW t AS SELECT ll.id, ll.name, ll.created_at
                 FROM tb AS ls,
                      LATERAL (SELECT ls.id AS id, ls.name AS name, ls.created_at AS created_at) AS ll;
-- Expected 30 rows, actual 27 (WRONG -- 3 rows dropped by the semijoin):
SELECT COUNT(*) FROM t AS t1 CROSS JOIN t AS t3
WHERE t3.name IN (SELECT t6.name FROM t AS t4 JOIN t AS t5 ON t4.id != t5.id JOIN t AS t6 ON t5.id = t6.id);

-- CONTROL (run in a third fresh DB): same equivalent view, semijoin disabled -> 30 (correct).
-- CREATE TABLE t ...; INSERT ...; ALTER/CREATE VIEW as above;
-- SET SESSION optimizer_switch='semijoin=off';
-- SELECT COUNT(*) ... ;  -- returns 30
