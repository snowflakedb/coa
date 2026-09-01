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

-- CrateDB: a correlated Filter on a CROSS JOIN / comma-join view whose equi-condition
-- sat in WHERE (rewritten to HashJoin) is dropped. COUNT/EXISTS/IN then see every
-- row. The same view written with INNER JOIN ON keeps the Filter.
--
-- Build      : CrateDB 6.4.2 (1db6455), docker crate:6.4.2. All-default session.
-- Origin     : eqgen/log/crate_simple_shuffle_keytag/cratedb_20260819-172428/mismatch_round41_0.sql
--              Admissibility verified: base t == equivalent t (8 identical rows, types equal).
--
-- Mechanism  : CROSS JOIN … WHERE l.x = r.x (or FROM a, b WHERE …) is rewritten to
--              HashJoin[INNER | (x = x)] under an Eval[]. MoveFilterBeneathEval
--              transposes Filter[w] through that Eval and the correlated predicate
--              vanishes (SubPlan Collect | true, no Filter[w]). INNER JOIN ON keeps
--              Filter[w] above the same HashJoin. Either
--              optimizer_move_filter_beneath_eval = false or
--              optimizer_move_filter_beneath_rename = false restores it.
--
-- Ground truth: WHERE FALSE (or a runtime-false outer boolean) over v is empty, so
--              COUNT is 0 / EXISTS is FALSE / IN-set is empty. The CROSS JOIN view
--              is the wrong side.
--
-- HOW TO RUN: each PART is independent (fresh schema). CrateDB needs REFRESH
-- between write and read. Re-check against `crate:6.4.2`.


-- =====================================================================================
-- PART 1 -- MINIMAL REPRO. 1 column, 1 row. v is an identity CROSS JOIN (WHERE,
--           not ON). Correlated WHERE FALSE must yield COUNT 0.
-- =====================================================================================
CREATE TABLE b (name TEXT)
  CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES ('abc');
REFRESH TABLE b;
CREATE VIEW v AS SELECT l.name FROM b l CROSS JOIN b r WHERE l.name = r.name;

SELECT (SELECT COUNT(*) FROM v WHERE q.w) FROM (SELECT FALSE AS w) q;
-- Expected: 0     Actual: 1     <<< WRONG

SELECT (SELECT COUNT(*) FROM b WHERE q.w) FROM (SELECT FALSE AS w) q;
-- Expected / actual: 0          -- same predicate on the heap table


-- =====================================================================================
-- PART 2 -- THE SAME VIEW WRITTEN WITH INNER JOIN ON. Byte-identical HashJoin,
--           but Filter[w] stays. COUNT 0.
-- =====================================================================================
CREATE TABLE b (name TEXT)
  CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES ('abc');
REFRESH TABLE b;
CREATE VIEW v AS SELECT l.name FROM b l INNER JOIN b r ON l.name = r.name;

SELECT (SELECT COUNT(*) FROM v WHERE q.w) FROM (SELECT FALSE AS w) q;
-- Expected / actual: 0


-- =====================================================================================
-- PART 3 -- EXISTS and IN are the same dropped Filter. 1-row, name = name so IN
--           fires once the IN-set is wrongly match-all.
-- =====================================================================================
CREATE TABLE b (name TEXT)
  CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES ('abc');
REFRESH TABLE b;
CREATE VIEW v AS SELECT l.name FROM b l CROSS JOIN b r WHERE l.name = r.name;

SELECT EXISTS (SELECT 1 FROM v WHERE q.w) FROM (SELECT FALSE AS w) q;
-- Expected: FALSE     Actual: TRUE     <<< WRONG

SELECT n FROM (SELECT name AS n, FALSE AS w FROM b) sq
WHERE n IN (SELECT name FROM v WHERE sq.w);
-- Expected: 0 rows    Actual: ('abc')  <<< WRONG


-- =====================================================================================
-- PART 4 -- CONTROLS.
-- =====================================================================================

-- C1 comma join is CROSS JOIN WHERE. COUNT 1 (wrong). Same rewrite.
CREATE TABLE b (name TEXT)
  CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES ('abc');
REFRESH TABLE b;
CREATE VIEW v AS SELECT l.name FROM b l, b r WHERE l.name = r.name;
SELECT (SELECT COUNT(*) FROM v WHERE q.w) FROM (SELECT FALSE AS w) q;
-- Actual: 1

-- C2 bare CROSS JOIN (no equi in WHERE) keeps Filter[w]. COUNT 0.
CREATE TABLE b (name TEXT)
  CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES ('abc');
REFRESH TABLE b;
CREATE VIEW v AS SELECT l.name FROM b l CROSS JOIN b r;
SELECT (SELECT COUNT(*) FROM v WHERE q.w) FROM (SELECT FALSE AS w) q;
-- Actual: 0

-- C3 either rule off restores COUNT 0 on the PART 1 view.
CREATE TABLE b (name TEXT)
  CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES ('abc');
REFRESH TABLE b;
CREATE VIEW v AS SELECT l.name FROM b l CROSS JOIN b r WHERE l.name = r.name;
SET optimizer_move_filter_beneath_eval = false;
SELECT (SELECT COUNT(*) FROM v WHERE q.w) FROM (SELECT FALSE AS w) q;
-- Actual: 0

-- C4 optimizer_merge_filter_and_collect = false does NOT mask it (not #19855).
-- C5 optimizer_rewrite_equi_join_to_hash_join = false does NOT mask it.
-- C6 2-row identity CROSS JOIN WHERE: dropped filter counts both rows (2 vs 0).


-- =====================================================================================
-- PART 5 -- PLAN DIFF. Same 1-row data, same COUNT query. Predicate text is
--           WHERE q.w. Only whether Filter[w] is in the SubPlan changes.
-- =====================================================================================
CREATE TABLE b (name TEXT)
  CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES ('abc');
REFRESH TABLE b;
CREATE VIEW v AS SELECT l.name FROM b l CROSS JOIN b r WHERE l.name = r.name;
EXPLAIN SELECT (SELECT COUNT(*) FROM v WHERE q.w) FROM (SELECT FALSE AS w) q;
-- SubPlan HashJoin[INNER | (name = name)], Collect | true
-- NO Filter[w]     -- COUNT 1

CREATE TABLE b (name TEXT)
  CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES ('abc');
REFRESH TABLE b;
CREATE VIEW v AS SELECT l.name FROM b l INNER JOIN b r ON l.name = r.name;
EXPLAIN SELECT (SELECT COUNT(*) FROM v WHERE q.w) FROM (SELECT FALSE AS w) q;
-- SubPlan Filter[w]
--           └ HashJoin[INNER | (name = name)]     -- COUNT 0

CREATE TABLE b (name TEXT)
  CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES ('abc');
REFRESH TABLE b;
CREATE VIEW v AS SELECT l.name FROM b l CROSS JOIN b r WHERE l.name = r.name;
SET optimizer_move_filter_beneath_eval = false;
EXPLAIN SELECT (SELECT COUNT(*) FROM v WHERE q.w) FROM (SELECT FALSE AS w) q;
-- SubPlan Filter[w]
--           └ Eval[]
--             └ HashJoin[INNER | (name = name)]   -- COUNT 0


-- =====================================================================================
-- PART 6 -- CONCRETE BUILDER SHAPE. eqgen join round-trip: ROW_NUMBER key,
--           CROSS JOIN WHERE l.eq = r.eq. Distilled query of PART 1 (not the
--           harvested 3-way join + window). Heap 0, join-view 1.
-- =====================================================================================
CREATE TABLE b (pk BIGINT, name TEXT, created_at TEXT)
  CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (1, 'abc', 'abc');
REFRESH TABLE b;
CREATE TABLE k AS SELECT *, ROW_NUMBER() OVER (ORDER BY pk) AS eq FROM b;
CREATE TABLE keys AS SELECT eq FROM k;
CREATE VIEW t AS
  SELECT l.pk, l.name, l.created_at FROM k l CROSS JOIN keys r WHERE l.eq = r.eq;

SELECT (SELECT COUNT(*) FROM t WHERE q.w) FROM (SELECT FALSE AS w) q;
-- Expected: 0     Actual: 1
