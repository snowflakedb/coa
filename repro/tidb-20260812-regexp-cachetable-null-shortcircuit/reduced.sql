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

-- TiDB: reading a CACHEd table flips REGEXP_REPLACE's NULL short-circuit off, turning a
-- correct per-row NULL into a spurious "Empty pattern is invalid" error -- for the SAME
-- query against the SAME unchanging data, on repeat execution.
--
-- Engine: tidb 8.0.11-TiDB-v9.0.0-beta.2.pre-2051-g3bea8196a5 @ 3bea8196a5 (2026-07-30), unistore.

-- ===========================================================================================
-- Minimal repro. Run the final SELECT MULTIPLE TIMES against the same session/table -- the
-- first one or two calls succeed, then it flips to a permanent error (any connection, not
-- just the one that issued the earlier calls).
-- ===========================================================================================
CREATE TABLE t_tbl (c_pk BIGINT, c_txt VARCHAR(255), c_big BIGINT, c_date DATE);
INSERT INTO t_tbl VALUES (1, '', NULL, '2030-06-01'), (2, 'abc', 5, '2024-01-15');
ALTER TABLE t_tbl CACHE;
CREATE VIEW t AS SELECT * FROM t_tbl;

SELECT c_date FROM t WHERE REGEXP_REPLACE(647356755, c_txt, c_big);
-- Call 1 (sometimes call 2 as well): 1 row, (2024-01-15,).  -- CORRECT
-- Call 2 (or 3) onward, forever after, from ANY connection:
--   ERROR 1139 (HY000): Got error 'Empty pattern is invalid' from regexp
--
-- Expected every time: 1 row, (2024-01-15,). Row 1 (c_txt='', c_big=NULL) must evaluate to
-- NULL (any-argument-NULL propagation) and never reach pattern validation at all.

-- ===========================================================================================
-- Control A: same 2 rows, same view, NO `ALTER TABLE ... CACHE` -- never errors, ever.
-- ===========================================================================================
-- CREATE TABLE a_tbl (c_pk BIGINT, c_txt VARCHAR(255), c_big BIGINT, c_date DATE);
-- INSERT INTO a_tbl VALUES (1, '', NULL, '2030-06-01'), (2, 'abc', 5, '2024-01-15');
-- CREATE VIEW a AS SELECT * FROM a_tbl;
-- SELECT c_date FROM a WHERE REGEXP_REPLACE(647356755, c_txt, c_big);  -- always 1 row, clean.

-- ===========================================================================================
-- Control B: CACHE the table, but give the empty-pattern row a NON-NULL c_big --
-- correctly errors EVERY time (nothing to short-circuit on -- this is the right answer).
-- ===========================================================================================
-- CREATE TABLE b_tbl (c_pk BIGINT, c_txt VARCHAR(255), c_big BIGINT);
-- INSERT INTO b_tbl VALUES (1, '', 99), (2, 'abc', 5);
-- ALTER TABLE b_tbl CACHE;
-- CREATE VIEW b AS SELECT * FROM b_tbl;
-- SELECT c_pk FROM b WHERE REGEXP_REPLACE(647356755, c_txt, c_big);  -- ERROR 1139, always. Correct.

-- ===========================================================================================
-- Control C: no VIEW at all, query the CACHEd table directly -- same flip (view is not
-- load-bearing; the cached-table read path is).
-- ===========================================================================================
-- CREATE TABLE c_tbl (c_pk BIGINT, c_txt VARCHAR(255), c_big BIGINT, c_date DATE);
-- INSERT INTO c_tbl VALUES (1, '', NULL, '2030-06-01'), (2, 'abc', 5, '2024-01-15');
-- ALTER TABLE c_tbl CACHE;
-- SELECT c_date FROM c_tbl WHERE REGEXP_REPLACE(647356755, c_txt, c_big);  -- same OK-then-ERROR flip.

-- ===========================================================================================
-- Control D: a brand-new connection, opened AFTER the flip has already happened on another
-- connection, still gets the error on its very FIRST query -- this is table-cache state
-- (server-wide), not per-session state.
-- ===========================================================================================
