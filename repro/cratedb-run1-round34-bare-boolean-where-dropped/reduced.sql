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

-- CrateDB 6.4.1 (release tarball, commit 45bfa80, assertions on)
--
-- BUG: a top-level `WHERE <bare boolean column>` predicate is SILENTLY DROPPED when the relation
-- it filters is a subquery/view whose projection ALIASES that column (`... AS c_flag`) and which
-- also contains a join and its own WHERE clause. The optimizer pushes the outer boolean predicate
-- down into the base-table scan and, past the aliased projection, degenerates it to the literal
-- `true` (EXPLAIN: `Collect[... | true]`), so the filter has no effect and every row is returned.
--
-- Single database. Run all statements in order (psql / crate-cli). REFRESH after each INSERT is
-- CrateDB's eventual-visibility requirement, not part of the bug.
--
-- =====================================================================================
-- PART 1 — CONCRETE, as the eqgen equivalence builder emits it (this is what was found).
-- The equivalent `t` is the base table rebuilt as an `eq_uid` join-reattachment view: tag each
-- base row with a unique key (ROW_NUMBER), split the key/flag into a companion table, then rejoin
-- FULL OUTER and keep `eq_flag = 1`. This view is row-identical to the base table. Note every
-- column is projected as a self-alias `l.X AS X` — that alias is load-bearing (see PART 3).
-- =====================================================================================

CREATE TABLE t (c_int BIGINT, c_flag BOOLEAN) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t VALUES (-3,false),(-1,true),(0,true),(1,false),(2,NULL),(3,true),(4,false),(7,true);
REFRESH TABLE t;
ALTER TABLE t RENAME TO t__base;

CREATE TABLE t__ids (c_int BIGINT, c_flag BOOLEAN, eq_uid BIGINT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t__ids (c_int, c_flag, eq_uid) SELECT c_int, c_flag, ROW_NUMBER() OVER (ORDER BY c_int) FROM t__base;
REFRESH TABLE t__ids;

CREATE TABLE t__keys (eq_uid BIGINT, eq_flag BIGINT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t__keys (eq_uid, eq_flag) SELECT eq_uid, 1 FROM t__ids;
REFRESH TABLE t__keys;

CREATE VIEW t AS
  SELECT l.c_int AS c_int, l.c_flag AS c_flag
  FROM   t__ids l FULL OUTER JOIN t__keys r ON l.eq_uid = r.eq_uid
  WHERE  r.eq_flag = 1;

SELECT c_flag FROM t WHERE c_flag;
-- Expected 4 rows, all (true).   Actual 8 rows: (true)x4, (false)x3, (null)x1  -- filter dropped.

-- =====================================================================================
-- PART 2 — DISTILLED minimal repro (no ROW_NUMBER, no UNION-ALL plumbing, 2 columns).
-- =====================================================================================

CREATE TABLE lft (c_flag BOOLEAN, u BIGINT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO lft VALUES (true, 1), (false, 2);
REFRESH TABLE lft;
CREATE TABLE rgt (u BIGINT, f BIGINT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO rgt VALUES (1, 1), (2, 1);
REFRESH TABLE rgt;

-- BUG: outer `WHERE c_flag` dropped; the (false) row leaks through.
SELECT c_flag FROM (SELECT l.c_flag AS c_flag FROM lft l JOIN rgt r ON l.u = r.u WHERE r.f = 1) x WHERE c_flag;
-- Expected 1 row (true).   Actual 2 rows (true, false).

-- =====================================================================================
-- PART 3 — CONTROLS: each removes exactly one of the four necessary ingredients and is CORRECT.
-- =====================================================================================

-- (a) remove the projection alias (`l.c_flag` instead of `l.c_flag AS c_flag`):
SELECT c_flag FROM (SELECT l.c_flag        FROM lft l JOIN rgt r ON l.u = r.u WHERE r.f = 1) x WHERE c_flag;
-- Expected 1 row (true).   Actual 1 row (true).  ✓

-- (b) remove the subquery's inner WHERE:
SELECT c_flag FROM (SELECT l.c_flag AS c_flag FROM lft l JOIN rgt r ON l.u = r.u) x WHERE c_flag;
-- Expected 1 row (true).   Actual 1 row (true).  ✓

-- (c) remove the join (single-table subquery):
SELECT c_flag FROM (SELECT c_flag AS c_flag FROM lft WHERE u >= 1) x WHERE c_flag;
-- Expected 1 row (true).   Actual 1 row (true).  ✓

-- (d) wrap the bare boolean (`= TRUE` instead of the bare column):
SELECT c_flag FROM (SELECT l.c_flag AS c_flag FROM lft l JOIN rgt r ON l.u = r.u WHERE r.f = 1) x WHERE c_flag = TRUE;
-- Expected 1 row (true).   Actual 1 row (true).  ✓
