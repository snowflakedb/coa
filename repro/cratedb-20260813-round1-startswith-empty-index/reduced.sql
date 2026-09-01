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

-- CrateDB: `starts_with(col, '')` on a default-indexed TEXT column matches only
-- the empty string, not every non-NULL string. Silent wrong result.
--
-- Build      : CrateDB 6.4.1 (45bfa80) and 6.4.2 (1db6455). All-default session.
-- Origin     : crate_corpus_full/cratedb_20260814-001112/mismatch_round1_9.sql
--              (admissibility verified: base t == equivalent t, 8 identical rows)
--
-- SQL: every string starts with the empty prefix. `SELECT starts_with('a', '')`
-- is TRUE. `WHERE starts_with(name, '')` on INDEX OFF (or a Filter above
-- WindowAgg) returns every non-NULL name. The same WHERE on a default-indexed
-- TEXT column goes through Collect's lucene `toQuery` path and returns only `''`.
--
-- Cousin of closed #15743 / #16567 (LIKE '' / LIKE empty pattern) — those were
-- LIKE-specific. starts_with was added later with its own toQuery (#17877) and
-- the empty-prefix case is still wrong on the indexed Collect path.
--
-- HOW TO RUN: each PART is independent (fresh schema). Re-check by running them against
-- `crate:6.4.1`.


-- =====================================================================================
-- PART 1 -- MINIMAL REPRO: 4 rows, 1 column. Scalar is TRUE; WHERE on the indexed
--           column returns only the empty string.
-- =====================================================================================
CREATE TABLE t (name TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t VALUES ('a');
INSERT INTO t VALUES ('');
INSERT INTO t VALUES ('abc');
INSERT INTO t VALUES (NULL);
REFRESH TABLE t;

SELECT starts_with('a', '');
-- TRUE

SELECT name FROM t WHERE starts_with(name, '');
-- Expected: 'a', '', 'abc'     (NULL is UNKNOWN → dropped)
-- Actual:   ''                 <<< WRONG


-- =====================================================================================
-- PART 2 -- CONTROLS
-- =====================================================================================

-- C1 INDEX OFF → all three non-NULL names. The default index is required.
CREATE TABLE t (name TEXT INDEX OFF) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t VALUES ('a');
INSERT INTO t VALUES ('');
INSERT INTO t VALUES ('abc');
INSERT INTO t VALUES (NULL);
REFRESH TABLE t;
SELECT name FROM t WHERE starts_with(name, '');
-- Expected / actual: 'a', '', 'abc'

-- C2 LIKE '%' on the same indexed column → all three. Not a general "empty
--    pattern matches only empty string" Collect bug; it is starts_with-specific.
CREATE TABLE t (name TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t VALUES ('a');
INSERT INTO t VALUES ('');
INSERT INTO t VALUES ('abc');
INSERT INTO t VALUES (NULL);
REFRESH TABLE t;
SELECT name FROM t WHERE name LIKE '%';
-- Expected / actual: 'a', '', 'abc'

-- C3 LIKE '' on the indexed column → only ''. That IS correct SQL (LIKE ''
--    matches the empty string only). starts_with('',) is not LIKE ''.
CREATE TABLE t (name TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t VALUES ('a');
INSERT INTO t VALUES ('');
INSERT INTO t VALUES ('abc');
INSERT INTO t VALUES (NULL);
REFRESH TABLE t;
SELECT name FROM t WHERE name LIKE '';
-- Expected / actual: ''

-- C4 identity window view (MAX(name) OVER PARTITION BY pk) → all three.
--    Filter above WindowAgg does not use the lucene toQuery path.
CREATE TABLE b (pk INT, name TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (1, 'a');
INSERT INTO b VALUES (2, '');
INSERT INTO b VALUES (3, 'abc');
INSERT INTO b VALUES (4, NULL);
REFRESH TABLE b;
CREATE VIEW t AS SELECT pk, name FROM (
  SELECT pk, MAX(name) OVER (PARTITION BY pk) AS name FROM b
) s;
SELECT name FROM t WHERE starts_with(name, '');
-- Expected / actual: 'a', '', 'abc'


-- =====================================================================================
-- PART 3 -- PLAN. Indexed Collect pushes starts_with into the lucene query;
--           INDEX OFF / WindowAgg evaluate the scalar instead.
-- =====================================================================================
CREATE TABLE t (name TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t VALUES ('a');
INSERT INTO t VALUES ('');
INSERT INTO t VALUES ('abc');
REFRESH TABLE t;
EXPLAIN SELECT name FROM t WHERE starts_with(name, '');
-- Collect[t | [name] | starts_with(name, '')]


-- =====================================================================================
-- PART 4 -- HOW THE FUZZER SAW IT. Base (default index) and equivalent (INDEX OFF
--           + window collapse) disagree on
--           (name != ANY (SELECT DISTINCT name WHERE starts_with(name, ''))) IN (FALSE)
--           because the ANY-set is {''} on Collect and {all non-NULL names} on the
--           window/INDEX OFF path. The wrapped != ANY is a consequence; PART 1 is
--           the bug.
-- =====================================================================================
