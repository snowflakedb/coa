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

-- CrateDB: `col = ALL (empty subquery)` drops NULL rows when `col` is produced by a
-- window function (view or inline derived table). Silent wrong result.
--
-- Build      : CrateDB 6.4.1 (45bfa80) and 6.4.2 (1db6455). All-default session.
-- Origin     : crate_corpus/cratedb_20260813-233946/mismatch_round6_1.sql
--              (admissibility verified: base t == equivalent t, 8 identical rows)
--              The same two queries account for all 14 mismatches in that run.
--
-- SQL:2011:  `x = ALL (empty)` is TRUE for any x, including NULL (vacuous truth).
-- CrateDB's Collect path honours that and keeps the NULL row. The same predicate
-- as a Filter above WindowAgg treats `NULL = ALL (empty)` as UNKNOWN and drops it.
-- The window output is an identity (MAX over a singleton partition) — the rows
-- are present (`SELECT * FROM t` returns them); only the ALL filter is wrong.
--
-- HOW TO RUN: each PART is independent (fresh schema). CrateDB needs REFRESH
-- between write and read. Re-check by running the blocks below against `crate:6.4.1`.


-- =====================================================================================
-- PART 1 -- MINIMAL REPRO: 1 row, 2 columns. The NULL id is present on the view
--           (`SELECT *` returns it) and on the base table under the same ALL filter.
--           On the window view the ALL filter returns empty.
-- =====================================================================================
CREATE TABLE b (id BIGINT, x INT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (NULL, 1);
REFRESH TABLE b;
CREATE VIEW t AS SELECT id, x FROM (
  SELECT MAX(id) OVER (PARTITION BY x) AS id, x FROM b
) s;

SELECT * FROM t;
-- Expected / actual: (NULL, 1)     -- the row IS there

SELECT id FROM b WHERE id = ALL (SELECT id FROM b WHERE FALSE);
-- Expected / actual: (NULL)        -- Collect path, vacuous ALL is TRUE

SELECT id FROM t WHERE id = ALL (SELECT id FROM t WHERE FALSE);
-- Expected: (NULL)     Actual: empty     <<< WRONG


-- =====================================================================================
-- PART 2 -- CONCRETE BUILDER SHAPE (what eqgen emitted). 8-row seed + identity
--           window view (`MAX(created_at) OVER (PARTITION BY c_pk)`). Same ALL
--           predicate, with the original GROUP BY. Base 7 groups, view 6 — the
--           (NULL, NULL, NULL) group is the one that vanishes.
-- =====================================================================================
CREATE TABLE b (c_pk BIGINT, id BIGINT, name TEXT, created_at TEXT)
  CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (1, NULL, NULL, NULL);
INSERT INTO b VALUES (2, 0, NULL, 'trailing ');
INSERT INTO b VALUES (3, 42, 'Zed', NULL);
INSERT INTO b VALUES (4, -7, '', 'trailing ');
INSERT INTO b VALUES (5, 42, 'a', 'trailing ');
INSERT INTO b VALUES (6, 0, 'Zed', 'Zed');
INSERT INTO b VALUES (7, 2, '', NULL);
INSERT INTO b VALUES (8, 0, NULL, 'trailing ');
REFRESH TABLE b;
CREATE VIEW t AS SELECT c_pk, id, name, created_at FROM (
  SELECT c_pk, id, name, MAX(created_at) OVER (PARTITION BY c_pk) AS created_at FROM b
) s;

-- on a plain view of b this returns 7 rows (GROUP BY collapses the two identical
-- (id=0, created_at='trailing ', name=NULL) rows). On the window view, 6:
SELECT t1.name FROM t AS t1
WHERE t1.id = ALL (SELECT t2.id FROM t AS t2 WHERE FALSE)
GROUP BY t1.id, t1.created_at, t1.name;
-- Expected 7 rows (two '', two 'Zed', one 'a', two NULL)
-- Actual   6 rows (the extra NULL — c_pk=1 — is missing)

-- original corpus query, same split:
SELECT t1.name FROM t AS t1
WHERE t1.id = ALL (
  SELECT coalesce(greatest(t2.id, t2.id), CAST($$42$$ AS BIGINT)) FROM t AS t2
  WHERE CASE WHEN CAST(3 AS BOOLEAN) THEN t2.created_at NOT IN (t2.created_at) END
)
GROUP BY t1.id, t1.created_at, t1.name;
-- Expected 7 / actual 6. The CASE/NOT IN subquery is empty, i.e. WHERE FALSE.


-- =====================================================================================
-- PART 3 -- CONTROLS. Each changes ONE thing. Distilled 1-row schema of PART 1
--           unless noted.
-- =====================================================================================

-- C1 plain view, no window -> (NULL). Window is required.
CREATE TABLE b (id BIGINT, x INT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (NULL, 1);
REFRESH TABLE b;
CREATE VIEW t AS SELECT * FROM b;
SELECT id FROM t WHERE id = ALL (SELECT id FROM t WHERE FALSE);

-- C2 inline derived table, no VIEW -> empty. The view is not required; WindowAgg is.
CREATE TABLE b (id BIGINT, x INT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (NULL, 1);
REFRESH TABLE b;
SELECT id FROM (
  SELECT MAX(id) OVER (PARTITION BY x) AS id, x FROM b
) s
WHERE id = ALL (SELECT id FROM b WHERE FALSE);

-- C3 window a column the ALL predicate does not use -> (NULL). The ALL operand
--    must be the window output.
CREATE TABLE b (id BIGINT, x INT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (NULL, 1);
REFRESH TABLE b;
CREATE VIEW t AS SELECT id, x FROM (
  SELECT id, MAX(x) OVER (PARTITION BY id) AS x FROM b
) s;
SELECT id FROM t WHERE id = ALL (SELECT id FROM t WHERE FALSE);

-- C4 non-NULL id, same window -> (1). The loss is the NULL = ALL (empty) case.
CREATE TABLE b (id BIGINT, x INT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (1, 1);
REFRESH TABLE b;
CREATE VIEW t AS SELECT id, x FROM (
  SELECT MAX(id) OVER (PARTITION BY x) AS id, x FROM b
) s;
SELECT id FROM t WHERE id = ALL (SELECT id FROM t WHERE FALSE);

-- C5 IS NULL on the same window view -> (NULL). The row is there; only ALL is wrong.
CREATE TABLE b (id BIGINT, x INT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (NULL, 1);
REFRESH TABLE b;
CREATE VIEW t AS SELECT id, x FROM (
  SELECT MAX(id) OVER (PARTITION BY x) AS id, x FROM b
) s;
SELECT id FROM t WHERE id IS NULL;

-- C6 `= ALL (SELECT 1 WHERE FALSE)` — subquery does not even read t -> empty.
--    Emptiness of the ALL set is the query-side trigger, not self-correlation.
CREATE TABLE b (id BIGINT, x INT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (NULL, 1);
REFRESH TABLE b;
CREATE VIEW t AS SELECT id, x FROM (
  SELECT MAX(id) OVER (PARTITION BY x) AS id, x FROM b
) s;
SELECT id FROM t WHERE id = ALL (SELECT 1 WHERE FALSE);

-- C7 MAX / MIN / SUM OVER () or OVER (PARTITION BY x) all drop. ROW_NUMBER as an
--    extra unused column on this 2-col schema does not (the ALL operand stays the
--    source `id` and the window can be pruned). See bug_report.md.


-- =====================================================================================
-- PART 4 -- PLAN DIFF. Same 1-row data. Predicate text is the same ALL; only WHERE
--           it sits changes (Collect-merged vs Filter-above-WindowAgg). The window
--           plan estimates the Filter at 0 rows.
-- =====================================================================================
CREATE TABLE b (id BIGINT, x INT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (NULL, 1);
REFRESH TABLE b;
CREATE VIEW t AS SELECT * FROM b;
EXPLAIN SELECT id FROM t WHERE id = ALL (SELECT id FROM t WHERE FALSE);
-- Collect[b | [id] | (id = ALL((SELECT id FROM (b))))]     -- keeps the NULL row

CREATE TABLE b (id BIGINT, x INT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (NULL, 1);
REFRESH TABLE b;
CREATE VIEW t AS SELECT id, x FROM (
  SELECT MAX(id) OVER (PARTITION BY x) AS id, x FROM b
) s;
EXPLAIN SELECT id FROM t WHERE id = ALL (SELECT id FROM t WHERE FALSE);
-- Filter[(max(id) OVER (PARTITION BY x) AS id = ALL((SELECT id FROM (s))))]
--   └ WindowAgg[...]
--       └ Collect[b | [id, x] | true]
-- Filter estimated rows=0; result is empty.


-- =====================================================================================
-- PART 5 -- ORIGINAL FINDING, verbatim equivalent (key-expand + UNION ALL copies +
--           DISTINCT MAX() OVER collapse to a view). Query as harvested.
-- 5a BASE: 7 groups. 5b EQUIVALENT: 6 groups.
-- =====================================================================================
CREATE TABLE t (c_pk BIGINT NOT NULL, id BIGINT, name TEXT, created_at TEXT)
  CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t VALUES (1, NULL, NULL, NULL);
INSERT INTO t VALUES (2, 0, NULL, 'trailing ');
INSERT INTO t VALUES (3, 42, 'Zed', NULL);
INSERT INTO t VALUES (4, -7, '', 'trailing ');
INSERT INTO t VALUES (5, 42, 'a', 'trailing ');
INSERT INTO t VALUES (6, 0, 'Zed', 'Zed');
INSERT INTO t VALUES (7, 2, '', NULL);
INSERT INTO t VALUES (8, 0, NULL, 'trailing ');
REFRESH TABLE t;
SELECT t1.name AS expr_0_varchar FROM t AS t1
WHERE t1.id = ALL (SELECT coalesce(greatest(t2.id, t2.id), CAST($$42$$ AS BIGINT)) AS expr_0_number
                   FROM t AS t2
                   WHERE CASE WHEN CAST(3 AS BOOLEAN) THEN t2.created_at NOT IN (t2.created_at) END)
GROUP BY t1.id, t1.created_at, t1.name;

CREATE TABLE t (c_pk BIGINT NOT NULL, id BIGINT, name TEXT, created_at TEXT)
  CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t VALUES (1, NULL, NULL, NULL);
INSERT INTO t VALUES (2, 0, NULL, 'trailing ');
INSERT INTO t VALUES (3, 42, 'Zed', NULL);
INSERT INTO t VALUES (4, -7, '', 'trailing ');
INSERT INTO t VALUES (5, 42, 'a', 'trailing ');
INSERT INTO t VALUES (6, 0, 'Zed', 'Zed');
INSERT INTO t VALUES (7, 2, '', NULL);
INSERT INTO t VALUES (8, 0, NULL, 'trailing ');
ALTER TABLE t RENAME TO t__base;
CREATE TABLE t__base_table_1 (c_pk BIGINT, id BIGINT, name TEXT, created_at TEXT, eq_key_1 BIGINT)
  CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t__base_table_1 (c_pk, id, name, created_at, eq_key_1)
  SELECT c_pk, id, name, created_at, ROW_NUMBER() OVER (ORDER BY c_pk) AS eq_key_1 FROM t__base;
CREATE TABLE t__base_table_2 (c_pk BIGINT, id BIGINT, name TEXT, created_at TEXT, eq_key_1 BIGINT)
  CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t__base_table_2 (c_pk, id, name, created_at, eq_key_1)
  SELECT * FROM t__base_table_1 UNION ALL SELECT * FROM t__base_table_1;
INSERT INTO t__base_table_2 SELECT * FROM t__base_table_2;
CREATE VIEW t__base_view_1 AS
  SELECT DISTINCT eq_key_1,
    MAX(c_pk) OVER (PARTITION BY eq_key_1) AS c_pk,
    MAX(id) OVER (PARTITION BY eq_key_1) AS id,
    MAX(name) OVER (PARTITION BY eq_key_1) AS name,
    MAX(created_at) OVER (PARTITION BY eq_key_1) AS created_at
  FROM t__base_table_2;
CREATE VIEW t AS SELECT c_pk, id, name, created_at FROM t__base_view_1;
SELECT t1.name AS expr_0_varchar FROM t AS t1
WHERE t1.id = ALL (SELECT coalesce(greatest(t2.id, t2.id), CAST($$42$$ AS BIGINT)) AS expr_0_number
                   FROM t AS t2
                   WHERE CASE WHEN CAST(3 AS BOOLEAN) THEN t2.created_at NOT IN (t2.created_at) END)
GROUP BY t1.id, t1.created_at, t1.name;
