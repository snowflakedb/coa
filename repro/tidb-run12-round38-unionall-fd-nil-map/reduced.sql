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

-- TiDB, SELECT tidb_version():
--   Release Version: v9.0.0-beta.2.pre-2051-g3bea8196a5
--   Edition: Community
--   Git Commit Hash: 3bea8196a565ca01800b2d0807868f01139d8a30
--   Git Branch: master
--   UTC Build Time: 2026-07-30 16:56:32
--   GoVersion: go1.26.4
--   Race Enabled: false
--   Check Table Before Drop: false
--   Store: unistore
--   Kernel Type: Classic
-- SELECT VERSION(): 8.0.11-TiDB-v9.0.0-beta.2.pre-2051-g3bea8196a5
--
-- ERROR 1105 (HY000): assignment to entry in nil map
--   = a recovered Go runtime panic, surfaced as error 1105 by planner.optimizeNoCache.
--
-- Root cause: LogicalUnionAll.ExtractFD() builds its FDSet as `&fd.FDSet{}` with a NIL
-- HashCodeToUniqueID map (logical_union_all.go:209 -- the only plan-node FDSet construction
-- in the tree that omits `make(map[string]int)`). That FDSet becomes `fds` in
-- ExtractFDForInnerJoin, which calls ExtractEquivalenceCols -> FDSet.RegisterUniqueID, and
-- RegisterUniqueID writes the map without a nil check (fd_graph.go:1227). The function's own
-- nil-repair for that field sits 11 lines LATER (logical_join.go:890).
--
-- Fails during PHYSICAL PLANNING (GetMergeJoin asks for FDs), so plain EXPLAIN reproduces.
-- Needs NO ROWS -- an empty table is enough. sql_mode-INDEPENDENT (reproduces with sql_mode='').

-- ============================== MINIMAL REPRO ==============================
-- session: charset utf8mb4, collation utf8mb4_0900_bin; any sql_mode (incl. empty)
CREATE TABLE t0 (id BIGINT);
CREATE VIEW u AS SELECT * FROM t0 UNION ALL SELECT * FROM t0;

-- Expected 0 rows (t0 is empty); actual: ERROR 1105 'assignment to entry in nil map'
SELECT 1 FROM u AS t1 WHERE (t1.id < ALL (SELECT 1)) IN (SELECT t1.id);

-- Same failure at plan time, so no execution is needed:
--   Expected a plan; actual: ERROR 1105
EXPLAIN SELECT 1 FROM u AS t1 WHERE (t1.id < ALL (SELECT 1)) IN (SELECT t1.id);

-- ==================== REQUIRED INGREDIENTS (each verified) ====================
-- Every line below was run against the live server; "reproduces" = ERROR 1105,
-- "clean" = 0 rows returned.

-- (1) The outer relation must be a UNION ALL.
--     CREATE VIEW u AS SELECT * FROM t0 UNION ALL SELECT * FROM t0            reproduces
--     ... UNION ALL SELECT * FROM t0 WHERE FALSE                              reproduces
--     inline derived table (no view) with UNION ALL                           reproduces
--     UNION (distinct) instead of UNION ALL                                   CLEAN
--     plain view  CREATE VIEW u AS SELECT * FROM t0                           CLEAN
--     the base table t0 directly                                             CLEAN
--     UNION ALL of two constant SELECTs (no table scan)                       CLEAN
--   Only the OUTER relation matters. With the original 5-alias query, making just t1 the
--   UNION ALL reproduces; making only t6 / t7 / t8 / t9 the UNION ALL is CLEAN.

-- (2) An inequality-quantified ALL subquery as the LHS of the IN.
--     (t1.id <  ALL (SELECT 1)) IN (...)                                      reproduces
--     (t1.id >  ALL (SELECT 1)) IN (...)                                      reproduces
--     (t1.id <= ALL (SELECT 1)) IN (...)                                      reproduces
--     (t1.id <> ALL (SELECT 1)) IN (...)                                      CLEAN
--     (t1.id =  ALL (SELECT 1)) IN (...)                                      CLEAN
--     (t1.id <  ANY (SELECT 1)) IN (...)                                      CLEAN
--     (t1.id < 1)               IN (...)   -- no subquery                     CLEAN
--   The ALL subquery may be uncorrelated and table-free (`SELECT 1`); `< ALL (SELECT id
--   FROM t0)` also reproduces. What matters is that the IN's LHS is a ScalarFunction rather
--   than a bare Column -- ExtractEquivalenceCols only calls RegisterUniqueID in that case.

-- (3) A correlated IN subquery.
--     IN (SELECT t1.id)                          -- correlated, no table      reproduces
--     IN (SELECT 1 FROM t0 AS t7 WHERE t1.created_at LIKE '% %')              reproduces
--     IN (SELECT 1)                              -- uncorrelated              CLEAN
--     IN (1, 0)                                  -- value list, not subquery  CLEAN
--     = ANY (SELECT t1.id)                       -- same semantics as IN      CLEAN
--     EXISTS (...) instead of IN                                              CLEAN

-- (4) Position matters: the predicate must be in WHERE.
--     SELECT (t1.id < ALL (SELECT 1)) IN (SELECT t1.id) FROM u AS t1           CLEAN

-- (5) Data is irrelevant -- the empty table above reproduces; so does the original 8-row,
--     3-column table.

-- (6) sql_mode is irrelevant.
--     the fuzzer's 7-flag mode                                                reproduces
--     ONLY_FULL_GROUP_BY alone                                                reproduces
--     sql_mode=''                                                            reproduces

-- ================= AS-FOUND SHAPE (for reference) =================
-- The generated finding used a 29-view chain and a 5-alias query; both collapse to the
-- three statements above. Original:
--   logs/tidb_run12/error_round38_0.sql
