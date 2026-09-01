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

-- CrateDB: merging a filter that contains an uncorrelated sub-select into the shard-level
-- Collect of a PARTITIONED table drops rows -- silent wrong result.
--
-- Build      : CrateDB 6.4.1 (release tarball). All-default session; deterministic; no error.
-- Origin     : hunt log
--              (admissibility verified: base t == equivalent t, 8 identical rows)
--
-- DECISIVE EVIDENCE -- two optimizer rules, either of which fixes it:
--     SET optimizer_merge_filter_and_collect  = false;   -- correct
--     SET optimizer_move_filter_beneath_rename = false;  -- correct
--   They chain: the Filter is moved beneath `Rename[…] AS t1` and then merged into the
--   `Collect` on the partitioned table. The predicate text is BYTE-IDENTICAL either way --
--   only WHERE it is evaluated changes (see PART 4's plan diff). Evaluating it inside the
--   shard-level Collect of a partitioned table gives the wrong answer.
--
--   The predicate contains an uncorrelated sub-select (`<= ALL (SELECT False FROM … GROUP BY id)`,
--   the `MultiPhase` branch of the plan). Merging a filter carrying such a sub-select into a
--   per-shard Collect is the suspected unsound step -- consistent with all controls, though the
--   internal cause is upstream's to confirm.
--
-- HOW TO RUN: CrateDB needs REFRESH between write and read. Each PART is independent.


-- =====================================================================================
-- PART 1 -- MINIMAL REPRO: 2 rows, 3 columns. Expected ['zzz','é']; actual ['zzz'].
--           The row with id = 7 (bucket = 3) is silently lost.
-- =====================================================================================
CREATE TABLE tb (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO tb VALUES (2, 'zzz', 'zzz');
INSERT INTO tb VALUES (7, 'é', 'é');
REFRESH TABLE tb;
CREATE TABLE p (id BIGINT, name TEXT, created_at TEXT, bucket INTEGER GENERATED ALWAYS AS (id % 4)) PARTITIONED BY (bucket) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO p (id, name, created_at) SELECT id, name, created_at FROM tb;
REFRESH TABLE p;
-- the SAME query on the plain table returns BOTH rows:
SELECT t1.name FROM tb AS t1 WHERE
  (((t1.id != 15) OR (CASE WHEN True AND True THEN coalesce(True, False)
                        WHEN CAST('y' AS BOOLEAN) THEN t1.name IS NOT NULL
                        ELSE coalesce(True, False) END))
     NOT IN (CASE WHEN (True AND CAST(NULL AS BOOLEAN)) IS NULL
                  THEN coalesce(False, True) AND (t1.created_at <= t1.created_at) END,
             LENGTH(t1.name) NOT IN ((trunc(0) / 10), coalesce(t1.id - t1.id, MOD(t1.id, 4)))))
  <= ALL (SELECT False FROM tb AS t3 GROUP BY t3.id);
-- ... and on the partitioned table returns ONE:
SELECT t1.name FROM p AS t1 WHERE
  (((t1.id != 15) OR (CASE WHEN True AND True THEN coalesce(True, False)
                        WHEN CAST('y' AS BOOLEAN) THEN t1.name IS NOT NULL
                        ELSE coalesce(True, False) END))
     NOT IN (CASE WHEN (True AND CAST(NULL AS BOOLEAN)) IS NULL
                  THEN coalesce(False, True) AND (t1.created_at <= t1.created_at) END,
             LENGTH(t1.name) NOT IN ((trunc(0) / 10), coalesce(t1.id - t1.id, MOD(t1.id, 4)))))
  <= ALL (SELECT False FROM p AS t3 GROUP BY t3.id);

-- =====================================================================================
-- PART 2 -- THE TWO RULE TOGGLES. Same data, same query, on the partitioned table.
--           Each returns the CORRECT ['zzz','é'].
-- =====================================================================================
CREATE TABLE tb (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO tb VALUES (2, 'zzz', 'zzz');
INSERT INTO tb VALUES (7, 'é', 'é');
REFRESH TABLE tb;
CREATE TABLE p (id BIGINT, name TEXT, created_at TEXT, bucket INTEGER GENERATED ALWAYS AS (id % 4)) PARTITIONED BY (bucket) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO p (id, name, created_at) SELECT id, name, created_at FROM tb;
REFRESH TABLE p;
SET optimizer_merge_filter_and_collect = false;
SELECT t1.name FROM p AS t1 WHERE
  (((t1.id != 15) OR (CASE WHEN True AND True THEN coalesce(True, False)
                        WHEN CAST('y' AS BOOLEAN) THEN t1.name IS NOT NULL
                        ELSE coalesce(True, False) END))
     NOT IN (CASE WHEN (True AND CAST(NULL AS BOOLEAN)) IS NULL
                  THEN coalesce(False, True) AND (t1.created_at <= t1.created_at) END,
             LENGTH(t1.name) NOT IN ((trunc(0) / 10), coalesce(t1.id - t1.id, MOD(t1.id, 4)))))
  <= ALL (SELECT False FROM p AS t3 GROUP BY t3.id);

CREATE TABLE tb (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO tb VALUES (2, 'zzz', 'zzz');
INSERT INTO tb VALUES (7, 'é', 'é');
REFRESH TABLE tb;
CREATE TABLE p (id BIGINT, name TEXT, created_at TEXT, bucket INTEGER GENERATED ALWAYS AS (id % 4)) PARTITIONED BY (bucket) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO p (id, name, created_at) SELECT id, name, created_at FROM tb;
REFRESH TABLE p;
SET optimizer_move_filter_beneath_rename = false;
SELECT t1.name FROM p AS t1 WHERE
  (((t1.id != 15) OR (CASE WHEN True AND True THEN coalesce(True, False)
                        WHEN CAST('y' AS BOOLEAN) THEN t1.name IS NOT NULL
                        ELSE coalesce(True, False) END))
     NOT IN (CASE WHEN (True AND CAST(NULL AS BOOLEAN)) IS NULL
                  THEN coalesce(False, True) AND (t1.created_at <= t1.created_at) END,
             LENGTH(t1.name) NOT IN ((trunc(0) / 10), coalesce(t1.id - t1.id, MOD(t1.id, 4)))))
  <= ALL (SELECT False FROM p AS t3 GROUP BY t3.id);

-- =====================================================================================
-- PART 3 -- RELATION-SIDE CONTROLS (8-row data, expected 5 rows). Each changes ONE thing.
-- =====================================================================================

-- C1 plain table -> 5 rows, correct.
CREATE TABLE tb (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO tb VALUES (-3, 'a', 'a');
INSERT INTO tb VALUES (-1, '', '');
INSERT INTO tb VALUES (0, 'dup', 'dup');
INSERT INTO tb VALUES (1, 'dup', 'dup');
INSERT INTO tb VALUES (2, NULL, NULL);
INSERT INTO tb VALUES (2, 'zzz', 'zzz');
INSERT INTO tb VALUES (NULL, 'b', 'b');
INSERT INTO tb VALUES (7, 'é', 'é');
REFRESH TABLE tb;
SELECT t1.name FROM tb AS t1 WHERE
  (((t1.id != 15) OR (CASE WHEN True AND True THEN coalesce(True, False)
                        WHEN CAST('y' AS BOOLEAN) THEN t1.name IS NOT NULL
                        ELSE coalesce(True, False) END))
     NOT IN (CASE WHEN (True AND CAST(NULL AS BOOLEAN)) IS NULL
                  THEN coalesce(False, True) AND (t1.created_at <= t1.created_at) END,
             LENGTH(t1.name) NOT IN ((trunc(0) / 10), coalesce(t1.id - t1.id, MOD(t1.id, 4)))))
  <= ALL (SELECT False FROM tb AS t3 GROUP BY t3.id);

-- C2 PARTITIONED BY a GENERATED (id % 4) column -> 4 rows, WRONG.
CREATE TABLE tb (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO tb VALUES (-3, 'a', 'a');
INSERT INTO tb VALUES (-1, '', '');
INSERT INTO tb VALUES (0, 'dup', 'dup');
INSERT INTO tb VALUES (1, 'dup', 'dup');
INSERT INTO tb VALUES (2, NULL, NULL);
INSERT INTO tb VALUES (2, 'zzz', 'zzz');
INSERT INTO tb VALUES (NULL, 'b', 'b');
INSERT INTO tb VALUES (7, 'é', 'é');
REFRESH TABLE tb;
CREATE TABLE p (id BIGINT, name TEXT, created_at TEXT, bucket INTEGER GENERATED ALWAYS AS (id % 4)) PARTITIONED BY (bucket) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO p (id, name, created_at) SELECT id, name, created_at FROM tb;
REFRESH TABLE p;
SELECT t1.name FROM p AS t1 WHERE
  (((t1.id != 15) OR (CASE WHEN True AND True THEN coalesce(True, False)
                        WHEN CAST('y' AS BOOLEAN) THEN t1.name IS NOT NULL
                        ELSE coalesce(True, False) END))
     NOT IN (CASE WHEN (True AND CAST(NULL AS BOOLEAN)) IS NULL
                  THEN coalesce(False, True) AND (t1.created_at <= t1.created_at) END,
             LENGTH(t1.name) NOT IN ((trunc(0) / 10), coalesce(t1.id - t1.id, MOD(t1.id, 4)))))
  <= ALL (SELECT False FROM p AS t3 GROUP BY t3.id);

-- C3 PARTITIONED BY a PLAIN INTEGER column holding the identical id % 4 values -> 5 rows.
--    Decisive: same partition values, same physical split; only the declared generation differs.
CREATE TABLE tb (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO tb VALUES (-3, 'a', 'a');
INSERT INTO tb VALUES (-1, '', '');
INSERT INTO tb VALUES (0, 'dup', 'dup');
INSERT INTO tb VALUES (1, 'dup', 'dup');
INSERT INTO tb VALUES (2, NULL, NULL);
INSERT INTO tb VALUES (2, 'zzz', 'zzz');
INSERT INTO tb VALUES (NULL, 'b', 'b');
INSERT INTO tb VALUES (7, 'é', 'é');
REFRESH TABLE tb;
CREATE TABLE p (id BIGINT, name TEXT, created_at TEXT, bucket INTEGER) PARTITIONED BY (bucket) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO p (id, name, created_at, bucket) SELECT id, name, created_at, id % 4 FROM tb;
REFRESH TABLE p;
SELECT t1.name FROM p AS t1 WHERE
  (((t1.id != 15) OR (CASE WHEN True AND True THEN coalesce(True, False)
                        WHEN CAST('y' AS BOOLEAN) THEN t1.name IS NOT NULL
                        ELSE coalesce(True, False) END))
     NOT IN (CASE WHEN (True AND CAST(NULL AS BOOLEAN)) IS NULL
                  THEN coalesce(False, True) AND (t1.created_at <= t1.created_at) END,
             LENGTH(t1.name) NOT IN ((trunc(0) / 10), coalesce(t1.id - t1.id, MOD(t1.id, 4)))))
  <= ALL (SELECT False FROM p AS t3 GROUP BY t3.id);

-- C4 the GENERATED column present but NOT partitioned by -> 5 rows.
CREATE TABLE tb (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO tb VALUES (-3, 'a', 'a');
INSERT INTO tb VALUES (-1, '', '');
INSERT INTO tb VALUES (0, 'dup', 'dup');
INSERT INTO tb VALUES (1, 'dup', 'dup');
INSERT INTO tb VALUES (2, NULL, NULL);
INSERT INTO tb VALUES (2, 'zzz', 'zzz');
INSERT INTO tb VALUES (NULL, 'b', 'b');
INSERT INTO tb VALUES (7, 'é', 'é');
REFRESH TABLE tb;
CREATE TABLE p (id BIGINT, name TEXT, created_at TEXT, bucket INTEGER GENERATED ALWAYS AS (id % 4)) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO p (id, name, created_at) SELECT id, name, created_at FROM tb;
REFRESH TABLE p;
SELECT t1.name FROM p AS t1 WHERE
  (((t1.id != 15) OR (CASE WHEN True AND True THEN coalesce(True, False)
                        WHEN CAST('y' AS BOOLEAN) THEN t1.name IS NOT NULL
                        ELSE coalesce(True, False) END))
     NOT IN (CASE WHEN (True AND CAST(NULL AS BOOLEAN)) IS NULL
                  THEN coalesce(False, True) AND (t1.created_at <= t1.created_at) END,
             LENGTH(t1.name) NOT IN ((trunc(0) / 10), coalesce(t1.id - t1.id, MOD(t1.id, 4)))))
  <= ALL (SELECT False FROM p AS t3 GROUP BY t3.id);

-- C5 PARTITIONED BY (id) directly, no expression -> 5 rows.
CREATE TABLE tb (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO tb VALUES (-3, 'a', 'a');
INSERT INTO tb VALUES (-1, '', '');
INSERT INTO tb VALUES (0, 'dup', 'dup');
INSERT INTO tb VALUES (1, 'dup', 'dup');
INSERT INTO tb VALUES (2, NULL, NULL);
INSERT INTO tb VALUES (2, 'zzz', 'zzz');
INSERT INTO tb VALUES (NULL, 'b', 'b');
INSERT INTO tb VALUES (7, 'é', 'é');
REFRESH TABLE tb;
CREATE TABLE p (id BIGINT, name TEXT, created_at TEXT) PARTITIONED BY (id) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO p (id, name, created_at) SELECT id, name, created_at FROM tb;
REFRESH TABLE p;
SELECT t1.name FROM p AS t1 WHERE
  (((t1.id != 15) OR (CASE WHEN True AND True THEN coalesce(True, False)
                        WHEN CAST('y' AS BOOLEAN) THEN t1.name IS NOT NULL
                        ELSE coalesce(True, False) END))
     NOT IN (CASE WHEN (True AND CAST(NULL AS BOOLEAN)) IS NULL
                  THEN coalesce(False, True) AND (t1.created_at <= t1.created_at) END,
             LENGTH(t1.name) NOT IN ((trunc(0) / 10), coalesce(t1.id - t1.id, MOD(t1.id, 4)))))
  <= ALL (SELECT False FROM p AS t3 GROUP BY t3.id);

-- C6 generated `id + 1` (also 7 partitions) -> 5 rows. So partition COUNT is not the axis.
CREATE TABLE tb (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO tb VALUES (-3, 'a', 'a');
INSERT INTO tb VALUES (-1, '', '');
INSERT INTO tb VALUES (0, 'dup', 'dup');
INSERT INTO tb VALUES (1, 'dup', 'dup');
INSERT INTO tb VALUES (2, NULL, NULL);
INSERT INTO tb VALUES (2, 'zzz', 'zzz');
INSERT INTO tb VALUES (NULL, 'b', 'b');
INSERT INTO tb VALUES (7, 'é', 'é');
REFRESH TABLE tb;
CREATE TABLE p (id BIGINT, name TEXT, created_at TEXT, bucket INTEGER GENERATED ALWAYS AS (id + 1)) PARTITIONED BY (bucket) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO p (id, name, created_at) SELECT id, name, created_at FROM tb;
REFRESH TABLE p;
SELECT t1.name FROM p AS t1 WHERE
  (((t1.id != 15) OR (CASE WHEN True AND True THEN coalesce(True, False)
                        WHEN CAST('y' AS BOOLEAN) THEN t1.name IS NOT NULL
                        ELSE coalesce(True, False) END))
     NOT IN (CASE WHEN (True AND CAST(NULL AS BOOLEAN)) IS NULL
                  THEN coalesce(False, True) AND (t1.created_at <= t1.created_at) END,
             LENGTH(t1.name) NOT IN ((trunc(0) / 10), coalesce(t1.id - t1.id, MOD(t1.id, 4)))))
  <= ALL (SELECT False FROM p AS t3 GROUP BY t3.id);

-- C7 generated `0` -- a single partition -> 5 rows.
CREATE TABLE tb (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO tb VALUES (-3, 'a', 'a');
INSERT INTO tb VALUES (-1, '', '');
INSERT INTO tb VALUES (0, 'dup', 'dup');
INSERT INTO tb VALUES (1, 'dup', 'dup');
INSERT INTO tb VALUES (2, NULL, NULL);
INSERT INTO tb VALUES (2, 'zzz', 'zzz');
INSERT INTO tb VALUES (NULL, 'b', 'b');
INSERT INTO tb VALUES (7, 'é', 'é');
REFRESH TABLE tb;
CREATE TABLE p (id BIGINT, name TEXT, created_at TEXT, bucket INTEGER GENERATED ALWAYS AS (0)) PARTITIONED BY (bucket) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO p (id, name, created_at) SELECT id, name, created_at FROM tb;
REFRESH TABLE p;
SELECT t1.name FROM p AS t1 WHERE
  (((t1.id != 15) OR (CASE WHEN True AND True THEN coalesce(True, False)
                        WHEN CAST('y' AS BOOLEAN) THEN t1.name IS NOT NULL
                        ELSE coalesce(True, False) END))
     NOT IN (CASE WHEN (True AND CAST(NULL AS BOOLEAN)) IS NULL
                  THEN coalesce(False, True) AND (t1.created_at <= t1.created_at) END,
             LENGTH(t1.name) NOT IN ((trunc(0) / 10), coalesce(t1.id - t1.id, MOD(t1.id, 4)))))
  <= ALL (SELECT False FROM p AS t3 GROUP BY t3.id);

-- C8 plain table CLUSTERED INTO 7 SHARDS -> 5 rows. Shard count is not the axis either.
CREATE TABLE tb (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 7 SHARDS WITH (number_of_replicas = 0);
INSERT INTO tb VALUES (-3, 'a', 'a');
INSERT INTO tb VALUES (-1, '', '');
INSERT INTO tb VALUES (0, 'dup', 'dup');
INSERT INTO tb VALUES (1, 'dup', 'dup');
INSERT INTO tb VALUES (2, NULL, NULL);
INSERT INTO tb VALUES (2, 'zzz', 'zzz');
INSERT INTO tb VALUES (NULL, 'b', 'b');
INSERT INTO tb VALUES (7, 'é', 'é');
REFRESH TABLE tb;
SELECT t1.name FROM tb AS t1 WHERE
  (((t1.id != 15) OR (CASE WHEN True AND True THEN coalesce(True, False)
                        WHEN CAST('y' AS BOOLEAN) THEN t1.name IS NOT NULL
                        ELSE coalesce(True, False) END))
     NOT IN (CASE WHEN (True AND CAST(NULL AS BOOLEAN)) IS NULL
                  THEN coalesce(False, True) AND (t1.created_at <= t1.created_at) END,
             LENGTH(t1.name) NOT IN ((trunc(0) / 10), coalesce(t1.id - t1.id, MOD(t1.id, 4)))))
  <= ALL (SELECT False FROM tb AS t3 GROUP BY t3.id);

-- C9 outer = plain table, subquery = the partitioned table -> 5 rows. Only the OUTER matters.
CREATE TABLE tb (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO tb VALUES (-3, 'a', 'a');
INSERT INTO tb VALUES (-1, '', '');
INSERT INTO tb VALUES (0, 'dup', 'dup');
INSERT INTO tb VALUES (1, 'dup', 'dup');
INSERT INTO tb VALUES (2, NULL, NULL);
INSERT INTO tb VALUES (2, 'zzz', 'zzz');
INSERT INTO tb VALUES (NULL, 'b', 'b');
INSERT INTO tb VALUES (7, 'é', 'é');
REFRESH TABLE tb;
CREATE TABLE p (id BIGINT, name TEXT, created_at TEXT, bucket INTEGER GENERATED ALWAYS AS (id % 4)) PARTITIONED BY (bucket) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO p (id, name, created_at) SELECT id, name, created_at FROM tb;
REFRESH TABLE p;
SELECT t1.name FROM tb AS t1 WHERE
  (((t1.id != 15) OR (CASE WHEN True AND True THEN coalesce(True, False)
                        WHEN CAST('y' AS BOOLEAN) THEN t1.name IS NOT NULL
                        ELSE coalesce(True, False) END))
     NOT IN (CASE WHEN (True AND CAST(NULL AS BOOLEAN)) IS NULL
                  THEN coalesce(False, True) AND (t1.created_at <= t1.created_at) END,
             LENGTH(t1.name) NOT IN ((trunc(0) / 10), coalesce(t1.id - t1.id, MOD(t1.id, 4)))))
  <= ALL (SELECT False FROM p AS t3 GROUP BY t3.id);


-- =====================================================================================
-- PART 4 -- PREDICATE-SIDE CONTROLS, on the 2-row minimum. The left operand of the outer
--           NOT IN is load-bearing; the other two operands simplify freely.
--           NB: it is semantically constant TRUE yet references a column -- replacing it
--           with the literal True, or with a genuinely non-constant predicate, both FIX it.
-- =====================================================================================

-- C10 left operand -> literal True  => CORRECT ['zzz','é']
CREATE TABLE tb (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO tb VALUES (2, 'zzz', 'zzz');
INSERT INTO tb VALUES (7, 'é', 'é');
REFRESH TABLE tb;
CREATE TABLE p (id BIGINT, name TEXT, created_at TEXT, bucket INTEGER GENERATED ALWAYS AS (id % 4)) PARTITIONED BY (bucket) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO p (id, name, created_at) SELECT id, name, created_at FROM tb;
REFRESH TABLE p;
SELECT t1.name FROM p AS t1 WHERE (True
     NOT IN (CASE WHEN (True AND CAST(NULL AS BOOLEAN)) IS NULL THEN coalesce(False, True) AND (t1.created_at <= t1.created_at) END,
             LENGTH(t1.name) NOT IN ((trunc(0) / 10), coalesce(t1.id - t1.id, MOD(t1.id, 4)))))
  <= ALL (SELECT False FROM p AS t3 GROUP BY t3.id);

-- C11 left operand -> (t1.id != 15)  => CORRECT ['zzz','é']
CREATE TABLE tb (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO tb VALUES (2, 'zzz', 'zzz');
INSERT INTO tb VALUES (7, 'é', 'é');
REFRESH TABLE tb;
CREATE TABLE p (id BIGINT, name TEXT, created_at TEXT, bucket INTEGER GENERATED ALWAYS AS (id % 4)) PARTITIONED BY (bucket) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO p (id, name, created_at) SELECT id, name, created_at FROM tb;
REFRESH TABLE p;
SELECT t1.name FROM p AS t1 WHERE ((t1.id != 15)
     NOT IN (CASE WHEN (True AND CAST(NULL AS BOOLEAN)) IS NULL THEN coalesce(False, True) AND (t1.created_at <= t1.created_at) END,
             LENGTH(t1.name) NOT IN ((trunc(0) / 10), coalesce(t1.id - t1.id, MOD(t1.id, 4)))))
  <= ALL (SELECT False FROM p AS t3 GROUP BY t3.id);

-- C12 the middle operand simplified to CAST(NULL AS BOOLEAN) => STILL WRONG ['zzz']
CREATE TABLE tb (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO tb VALUES (2, 'zzz', 'zzz');
INSERT INTO tb VALUES (7, 'é', 'é');
REFRESH TABLE tb;
CREATE TABLE p (id BIGINT, name TEXT, created_at TEXT, bucket INTEGER GENERATED ALWAYS AS (id % 4)) PARTITIONED BY (bucket) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO p (id, name, created_at) SELECT id, name, created_at FROM tb;
REFRESH TABLE p;
SELECT t1.name FROM p AS t1 WHERE (((t1.id != 15) OR (CASE WHEN True AND True THEN coalesce(True, False) WHEN CAST('y' AS BOOLEAN) THEN t1.name IS NOT NULL ELSE coalesce(True, False) END))
     NOT IN (CAST(NULL AS BOOLEAN),
             LENGTH(t1.name) NOT IN ((trunc(0) / 10), coalesce(t1.id - t1.id, MOD(t1.id, 4)))))
  <= ALL (SELECT False FROM p AS t3 GROUP BY t3.id);

-- C13 the sub-predicate `LENGTH(name) <> 0` alone, on both tables -> identical. The loss is
--     NOT in that comparison.
CREATE TABLE tb (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO tb VALUES (2, 'zzz', 'zzz');
INSERT INTO tb VALUES (7, 'é', 'é');
REFRESH TABLE tb;
CREATE TABLE p (id BIGINT, name TEXT, created_at TEXT, bucket INTEGER GENERATED ALWAYS AS (id % 4)) PARTITIONED BY (bucket) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO p (id, name, created_at) SELECT id, name, created_at FROM tb;
REFRESH TABLE p;
SELECT name FROM tb WHERE LENGTH(name) <> 0;
SELECT name FROM p  WHERE LENGTH(name) <> 0;


-- =====================================================================================
-- PART 5 -- THE PLAN DIFF. Run both; the predicate text is identical, and the ONLY
-- structural difference is Collect-merged (wrong) vs Filter-above-Collect (right):
--
--   rules on  (WRONG):  Collect[p | [name, created_at, id] | (<predicate>)]
--   rule off  (RIGHT):  Filter[(<predicate>)]
--                         └ Collect[p | [name, created_at, id] | true]
-- =====================================================================================
CREATE TABLE tb (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO tb VALUES (2, 'zzz', 'zzz');
INSERT INTO tb VALUES (7, 'é', 'é');
REFRESH TABLE tb;
CREATE TABLE p (id BIGINT, name TEXT, created_at TEXT, bucket INTEGER GENERATED ALWAYS AS (id % 4)) PARTITIONED BY (bucket) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO p (id, name, created_at) SELECT id, name, created_at FROM tb;
REFRESH TABLE p;
EXPLAIN SELECT t1.name FROM p AS t1 WHERE
  (((t1.id != 15) OR (CASE WHEN True AND True THEN coalesce(True, False)
                        WHEN CAST('y' AS BOOLEAN) THEN t1.name IS NOT NULL
                        ELSE coalesce(True, False) END))
     NOT IN (CASE WHEN (True AND CAST(NULL AS BOOLEAN)) IS NULL
                  THEN coalesce(False, True) AND (t1.created_at <= t1.created_at) END,
             LENGTH(t1.name) NOT IN ((trunc(0) / 10), coalesce(t1.id - t1.id, MOD(t1.id, 4)))))
  <= ALL (SELECT False FROM p AS t3 GROUP BY t3.id);
SET optimizer_merge_filter_and_collect = false;
EXPLAIN SELECT t1.name FROM p AS t1 WHERE
  (((t1.id != 15) OR (CASE WHEN True AND True THEN coalesce(True, False)
                        WHEN CAST('y' AS BOOLEAN) THEN t1.name IS NOT NULL
                        ELSE coalesce(True, False) END))
     NOT IN (CASE WHEN (True AND CAST(NULL AS BOOLEAN)) IS NULL
                  THEN coalesce(False, True) AND (t1.created_at <= t1.created_at) END,
             LENGTH(t1.name) NOT IN ((trunc(0) / 10), coalesce(t1.id - t1.id, MOD(t1.id, 4)))))
  <= ALL (SELECT False FROM p AS t3 GROUP BY t3.id);

-- =====================================================================================
-- PART 6 -- THE ORIGINAL FINDING, verbatim, for provenance.
-- 6a: BASE (plain table) -- 5 rows.
-- =====================================================================================
CREATE TABLE t (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t VALUES (-3, 'a', 'a');
INSERT INTO t VALUES (-1, '', '');
INSERT INTO t VALUES (0, 'dup', 'dup');
INSERT INTO t VALUES (1, 'dup', 'dup');
INSERT INTO t VALUES (2, NULL, NULL);
INSERT INTO t VALUES (2, 'zzz', 'zzz');
INSERT INTO t VALUES (NULL, 'b', 'b');
INSERT INTO t VALUES (7, 'é', 'é');
REFRESH TABLE t;
SELECT t1.name AS expr_0_varchar, t1.created_at AS expr_1_varchar, t1.created_at AS expr_2_varchar, NULLIF(t1.id, MAX(t1.id) - MAX(t1.id)) AS expr_3_number, t1.created_at AS expr_4_varchar FROM t AS t1 WHERE (((t1.id != 15) OR (CASE WHEN True AND True THEN coalesce(True, False) WHEN CAST($$y$$ AS BOOLEAN) THEN t1.name IS NOT NULL ELSE coalesce(True, False) END)) NOT IN (CASE WHEN (True AND CAST(NULL AS BOOLEAN)) IS NULL THEN coalesce(False, True) AND (t1.created_at <= t1.created_at) END, LENGTH(t1.name) NOT IN ((trunc(0) / 10), coalesce(t1.id - t1.id, MOD(t1.id, 4))))) <= ALL (SELECT False AS expr_0_boolean FROM t AS t3 GROUP BY t3.id) GROUP BY t1.name, t1.created_at, t1.id;

-- 6b: EQUIVALENT (window-collapse link + the partitioned round-trip) -- 4 rows.
CREATE TABLE t (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t VALUES (-3, 'a', 'a');
INSERT INTO t VALUES (-1, '', '');
INSERT INTO t VALUES (0, 'dup', 'dup');
INSERT INTO t VALUES (1, 'dup', 'dup');
INSERT INTO t VALUES (2, NULL, NULL);
INSERT INTO t VALUES (2, 'zzz', 'zzz');
INSERT INTO t VALUES (NULL, 'b', 'b');
INSERT INTO t VALUES (7, 'é', 'é');
ALTER TABLE t RENAME TO t__base;
CREATE TABLE t__base_table_1 (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t__base_table_1 (id, name, created_at) SELECT FIRST_VALUE(id) OVER (PARTITION BY id ORDER BY id RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS id, FIRST_VALUE(name) OVER (PARTITION BY name ORDER BY name RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS name, FIRST_VALUE(created_at) OVER (PARTITION BY created_at ORDER BY created_at RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS created_at FROM t__base;
CREATE TABLE t__base_table_2 (id BIGINT, name TEXT, created_at TEXT, t__base_bucket_1 INTEGER GENERATED ALWAYS AS (id % 4)) PARTITIONED BY (t__base_bucket_1) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t__base_table_2 (id, name, created_at) SELECT id, name, created_at FROM t__base_table_1;
CREATE VIEW t AS SELECT id, name, created_at FROM t__base_table_2;
SELECT t1.name AS expr_0_varchar, t1.created_at AS expr_1_varchar, t1.created_at AS expr_2_varchar, NULLIF(t1.id, MAX(t1.id) - MAX(t1.id)) AS expr_3_number, t1.created_at AS expr_4_varchar FROM t AS t1 WHERE (((t1.id != 15) OR (CASE WHEN True AND True THEN coalesce(True, False) WHEN CAST($$y$$ AS BOOLEAN) THEN t1.name IS NOT NULL ELSE coalesce(True, False) END)) NOT IN (CASE WHEN (True AND CAST(NULL AS BOOLEAN)) IS NULL THEN coalesce(False, True) AND (t1.created_at <= t1.created_at) END, LENGTH(t1.name) NOT IN ((trunc(0) / 10), coalesce(t1.id - t1.id, MOD(t1.id, 4))))) <= ALL (SELECT False AS expr_0_boolean FROM t AS t3 GROUP BY t3.id) GROUP BY t1.name, t1.created_at, t1.id;
