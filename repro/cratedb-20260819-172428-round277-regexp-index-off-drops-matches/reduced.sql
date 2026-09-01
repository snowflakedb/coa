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

-- CrateDB: a REGEXP predicate merged into Collect over an INDEX OFF TEXT
-- column matches no rows, even though projection evaluates the same predicate
-- to TRUE. A separate Filter above Collect is correct.
--
-- Engine: CrateDB 6.4.2 (1db6455), also reproduced on 6.4.1 (45bfa80).
-- Origin: eqgen/log/crate_simple_shuffle_keytag/cratedb_20260819-172428/
--         mismatch_round277_0.sql
--
-- Each PART is independent and should run in a fresh schema.


-- ============================================================================
-- PART 1 -- minimal wrong result and scalar/projection control.
-- ============================================================================
CREATE TABLE t (s TEXT INDEX OFF)
  CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t VALUES ('a');
REFRESH TABLE t;

SELECT s FROM t WHERE s ~ 'a';
-- Expected: ('a')
-- Actual:   0 rows  <<< WRONG

SELECT s, s ~ 'a' AS matches FROM t;
-- Expected / actual: ('a', TRUE)

SELECT s FROM t WHERE s = 'a';
-- Expected / actual: ('a')


-- ============================================================================
-- PART 2 -- indexed control and optimizer mask.
-- ============================================================================
CREATE TABLE indexed (s TEXT)
  CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO indexed VALUES ('a');
REFRESH TABLE indexed;

SELECT s FROM indexed WHERE s ~ 'a';
-- Expected / actual: ('a')

CREATE TABLE unindexed (s TEXT INDEX OFF)
  CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO unindexed VALUES ('a');
REFRESH TABLE unindexed;

SET optimizer_merge_filter_and_collect = false;
SELECT s FROM unindexed WHERE s ~ 'a';
-- Expected / actual: ('a')

EXPLAIN SELECT s FROM unindexed WHERE s ~ 'a';
-- Filter[(s ~ 'a')]
--   └ Collect[unindexed | [s] | true]

SET optimizer_merge_filter_and_collect = true;


-- ============================================================================
-- PART 3 -- DML uses the same wrong filter and silently affects zero rows.
-- ============================================================================
CREATE TABLE d (id BIGINT, s TEXT INDEX OFF)
  CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO d VALUES (1, 'a'), (2, 'b');
REFRESH TABLE d;

DELETE FROM d WHERE s ~ 'a';
REFRESH TABLE d;
SELECT id, s FROM d ORDER BY id;
-- Expected after DELETE: (2, 'b')
-- Actual: (1, 'a'), (2, 'b')  <<< matching row was not deleted

CREATE TABLE u (id BIGINT, s TEXT INDEX OFF)
  CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO u VALUES (1, 'a'), (2, 'b');
REFRESH TABLE u;

UPDATE u SET id = 10 WHERE s ~ 'a';
REFRESH TABLE u;
SELECT id, s FROM u ORDER BY id;
-- Expected: (2, 'b'), (10, 'a')
-- Actual:   (1, 'a'), (2, 'b')  <<< matching row was not updated


-- ============================================================================
-- PART 4 -- default plan showing the faulty merge.
-- ============================================================================
CREATE TABLE p (s TEXT INDEX OFF)
  CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO p VALUES ('a');
REFRESH TABLE p;

EXPLAIN SELECT s FROM p WHERE s ~ 'a';
-- Collect[p | [s] | (s ~ 'a')]
-- Actual query result: 0 rows
