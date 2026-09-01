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

-- ClickHouse -- WRONG RESULT: RANK() and DENSE_RANK() silently degenerate into ROW_NUMBER()
-- whenever an explicit ROWS frame is present, giving ORDER BY peers distinct ranks.
--
-- Affected builds (all official static, Linux aarch64) -- NOT a master regression:
--   25.3.6.56  (LTS)              RANK framed [1,2,3]  DENSE_RANK framed [1,2,3]
--   26.3.17.56 (LTS)              RANK framed [1,2,3]  DENSE_RANK framed [1,2,3]
--   26.7.1.1315 (stable 2026-07-22) RANK framed [1,2,3]  DENSE_RANK framed [1,2,3]
--   26.8.1.440 (master nightly)   RANK framed [1,2,3]  DENSE_RANK framed [1,2,3]   <- the finding
-- correct: RANK [1,1,3], DENSE_RANK [1,1,2]; every build is correct with no frame.
--
-- The full differential finding at the bottom of this file (not just the 3-row wrong answer) also
-- reproduces on 26.7.1.1315-stable run as `clickhouse server`, byte-identically to the nightly:
-- same row-identical inputs, same two mismatching rows, same 56-vs-49 distinct-rank collapse.
--
-- NOT expected behaviour -- ClickHouse's own docs
-- (docs/sql-reference/window-functions/index.md) assert the invariant for this function class:
--   "-- row_number does not respect the frame, so rn_1 = rn_2 = rn_3 != rn_4"
-- demonstrated with ROWS BETWEEN 1 PRECEDING AND CURRENT ROW. rank/dense_rank violate it.
-- The doc example uses distinct ORDER BY values, so it has no ties and could not have caught this.
-- Note also the frame below is ROWS UNBOUNDED PRECEDING..UNBOUNDED FOLLOWING = the WHOLE partition,
-- so no interpretation of the frame makes [1,2,3] defensible.
--
-- Root cause: WindowTransform::arePeers (src/Processors/Transforms/WindowTransform.cpp:798)
-- returns false for ANY two distinct rows when frame.type == ROWS, so every row becomes its own
-- peer group. RANK returns transform->peer_group_start_row_number (:1635) and DENSE_RANK returns
-- transform->peer_group_number (:1653), so both collapse to the row number. That ROWS-vs-RANGE
-- distinction is correct for FRAME BOUNDARY purposes (under ROWS, CURRENT ROW means just that row;
-- under RANGE it means the row and its peers) -- the bug is reusing it as the RANKING peer group.
-- Unlike percent_rank (:2305), cume_dist (:2419) and ntile (:2165) -- which read the same peer
-- counters and DO implement checkWindowFrameType -- rank and dense_rank declare no frame validation.
--
-- Per SQL:2011 a ranking function's value is fixed by the ORDER BY peer group and is
-- frame-independent. DuckDB 2.0-alpha and 1.5.5 both accept the same frame and correctly ignore it
-- (verified: RANK -> [1,1,3], DENSE_RANK -> [1,1,2]).

-- ====================== MINIMAL REPRO (3 rows, no tables) ======================
-- Expected [1,1,3] (the two 'a' rows are ORDER BY peers); actual [1,2,3]
SELECT groupArray(r) FROM (
  SELECT RANK() OVER (ORDER BY v ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS r
  FROM (SELECT arrayJoin(['a','a','b']) AS v));

-- Expected [1,1,2]; actual [1,2,3]
SELECT groupArray(r) FROM (
  SELECT DENSE_RANK() OVER (ORDER BY v ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS r
  FROM (SELECT arrayJoin(['a','a','b']) AS v));

-- Controls: the same query with no frame is CORRECT.
-- Expected [1,1,3]; actual [1,1,3]  (correct)
SELECT groupArray(r) FROM (
  SELECT RANK() OVER (ORDER BY v) AS r FROM (SELECT arrayJoin(['a','a','b']) AS v));
-- Expected [1,1,2]; actual [1,1,2]  (correct)
SELECT groupArray(r) FROM (
  SELECT DENSE_RANK() OVER (ORDER BY v) AS r FROM (SELECT arrayJoin(['a','a','b']) AS v));

-- ==================== FRAME x FUNCTION MATRIX (all measured) ====================
-- On ['a','a','b'] ORDER BY v. Correct: RANK [1,1,3], DENSE_RANK [1,1,2],
-- PERCENT_RANK [0,0,1], ROW_NUMBER [1,2,3].
--
--   frame                                     RANK      DENSE_RANK  PERCENT_RANK  CUME_DIST  ROW_NUMBER
--   (no frame)                                [1,1,3]v  [1,1,2]v    [0,0,1]v      REJECTED   [1,2,3]v
--   RANGE UNBOUNDED PRECEDING..CURRENT ROW    [1,1,3]v  [1,1,2]v    REJECTED      REJECTED   [1,2,3]v
--   RANGE UNBOUNDED PRECEDING..UNB FOLLOWING  [1,1,3]v  [1,1,2]v    [0,0,1]v      REJECTED   [1,2,3]v
--   ROWS  UNBOUNDED PRECEDING..UNB FOLLOWING  [1,2,3]X  [1,2,3]X    REJECTED      REJECTED   [1,2,3]v
--   ROWS  UNBOUNDED PRECEDING..CURRENT ROW    [1,2,3]X  [1,2,3]X    REJECTED      REJECTED   [1,2,3]v
--   ROWS  1 PRECEDING..CURRENT ROW            [1,2,3]X  [1,2,3]X    REJECTED      REJECTED   [1,2,3]v
--
--   v = correct, X = WRONG.  Any ROWS frame breaks it; every RANGE frame is fine.
--   ROW_NUMBER is unaffected (it reads current_row_number, not a peer counter).

-- ============ THE ORIGINAL FINDING (differential form, for context) ============
-- Why this matters beyond the wrong value: because peers now get distinct ranks decided by
-- physical arrival order, any query using a framed RANK becomes PLAN-SHAPE DEPENDENT. That is how
-- the eqgen equivalence oracle caught it -- two row-identical relations, different physical
-- layouts, different answers.
--
-- Session settings (from the finding header):
--   create_table_empty_primary_key_by_default=1, join_use_nulls=1, max_threads=1,
--   default_table_engine=MergeTree, database_atomic_wait_for_drop_and_detach_synchronously=1
--
-- BASE (fresh database):
CREATE TABLE t (id Nullable(Int64), name Nullable(String), created_at Nullable(String))
  ENGINE = MergeTree ORDER BY tuple();
INSERT INTO t VALUES (-3,'a','a'),(-1,'',''),(0,'dup','dup'),(1,'dup','dup'),
                     (2,NULL,NULL),(2,'zzz','zzz'),(NULL,'b','b'),(7,'é','é');

-- EQUIVALENT (a SEPARATE fresh database): same 8 rows, then t rebuilt through a
-- FIRST_VALUE window round-trip -- row-identical to BASE (verified both directions).
--   RENAME TABLE t TO t__base;
--   CREATE TABLE t AS SELECT FIRST_VALUE(id) OVER (PARTITION BY id ORDER BY id) AS id, ...
--                    FROM t__base;

-- Run against BOTH databases. Expected: identical result multisets. Actual: they differ in
-- exactly two rows -- ranks 15 and 16 swap between t3.id = 0 and t3.id = 1, which are ORDER BY
-- PEERS (both have t3.name='dup', t2.name=NULL, t1.created_at=NULL; t3.id is NOT in the window
-- ORDER BY, so under correct RANK semantics both must receive the same rank).
--
--   BASE : (2016-12-10, NULL, 15, 1) and (2016-12-10, NULL, 16, 0)
--   EQUIV: (2016-12-10, NULL, 15, 0) and (2016-12-10, NULL, 16, 1)
SELECT DISTINCT CAST('2016-12-10' AS Date32) AS expr_0_date, t1.created_at AS expr_1_varchar,
       RANK() OVER (ORDER BY t3.name DESC, t3.name ASC NULLS FIRST, t2.name DESC NULLS FIRST
                    ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS expr_2_number,
       t3.id AS expr_3_number
FROM t AS t1 FULL OUTER JOIN t AS t2 ON t1.name = t2.created_at CROSS JOIN t AS t3
GROUP BY t3.id, t1.created_at, t3.name, t2.name, t1.name;

-- DECISIVE CUT: delete ONLY the frame clause from the query above and the two sides AGREE.
-- (verified: framed -> base != equiv; unframed -> base == equiv. Same for DENSE_RANK.)
