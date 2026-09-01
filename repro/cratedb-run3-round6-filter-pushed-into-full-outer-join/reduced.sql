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

-- CrateDB: `optimizer_rewrite_filter_on_outer_join_to_inner_join` pushes a
-- NON-null-rejecting filter into the left input of a FULL OUTER JOIN *without* downgrading the
-- join type, so the rows it removes come back null-extended -- silent WRONG ROWS.
--
-- Build      : CrateDB 6.4.1 (release tarball). All-default session; deterministic; no error.
-- Origin     : hunt log (round 6, seed 649806657)
--              admissibility verified: base t == equivalent t, 8 identical rows.
--
-- DECISIVE EVIDENCE -- one optimizer rule, and it fixes both the minimal repro and the original
-- 8-row finding (which then returns the base's 6 rows):
--     SET optimizer_rewrite_filter_on_outer_join_to_inner_join = false;
--
-- WHY IT IS WRONG: the rule's job is to downgrade an outer join to an inner/one-sided join when
-- the filter above it is null-rejecting, which then licenses pushing that filter into an input.
-- Here the filter is `coalesce(l.id, 15) NOT IN (...)`, which is NOT null-rejecting -- it is TRUE
-- when `l.id IS NULL`, because coalesce(NULL,15) = 15 and 15 is not in the subquery result. The
-- rule pushes the filter into the LEFT input anyway and leaves the join as FULL, so the join
-- still null-extends exactly the rows the pushed filter removed, and the retained Filter above
-- then passes them (15 NOT IN {2} is true). Net effect: a row that must not appear, appears with
-- every left-side column NULL.
--
-- HOW TO RUN: CrateDB needs REFRESH between write and read. Each PART is independent.


-- =====================================================================================
-- PART 1 -- MINIMAL REPRO. One table, one column, one row.
-- Expected 0 rows (id=2, so coalesce(2,15)=2 which IS in the subquery result {2}).
-- Actual   1 row: (NULL).
-- =====================================================================================
CREATE TABLE b (id BIGINT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (2);
REFRESH TABLE b;
SELECT l.id FROM b l FULL OUTER JOIN b r ON l.id = r.id WHERE coalesce(l.id, 15) NOT IN (SELECT t2.id FROM b t2 WHERE t2.id = 2);

-- =====================================================================================
-- PART 2 -- THE RULE TOGGLE. Same data, same query -> 0 rows, correct.
-- =====================================================================================
CREATE TABLE b (id BIGINT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (2);
REFRESH TABLE b;
SET optimizer_rewrite_filter_on_outer_join_to_inner_join = false;
SELECT l.id FROM b l FULL OUTER JOIN b r ON l.id = r.id WHERE coalesce(l.id, 15) NOT IN (SELECT t2.id FROM b t2 WHERE t2.id = 2);

-- =====================================================================================
-- PART 3 -- THE PLAN DIFF. The ONLY difference is the left Collect's filter. The
-- `Filter[...]` above the join and `NestedLoopJoin[FULL | (id = id)]` are present in BOTH --
-- i.e. the rule pushed the filter down but did NOT downgrade the join:
--
--   default (WRONG):   NestedLoopJoin[FULL | (id = id)]
--                        |- Collect[b | [id] | (NOT (coalesce(id, 15) = ANY((SELECT id FROM (t2)))))]
--                        \- Collect[b | [id] | true]
--
--   rule = false (OK):  NestedLoopJoin[FULL | (id = id)]
--                        |- Collect[b | [id] | true]
--                        \- Collect[b | [id] | true]
-- =====================================================================================
CREATE TABLE b (id BIGINT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (2);
REFRESH TABLE b;
EXPLAIN SELECT l.id FROM b l FULL OUTER JOIN b r ON l.id = r.id WHERE coalesce(l.id, 15) NOT IN (SELECT t2.id FROM b t2 WHERE t2.id = 2);
SET optimizer_rewrite_filter_on_outer_join_to_inner_join = false;
EXPLAIN SELECT l.id FROM b l FULL OUTER JOIN b r ON l.id = r.id WHERE coalesce(l.id, 15) NOT IN (SELECT t2.id FROM b t2 WHERE t2.id = 2);


-- =====================================================================================
-- PART 4 -- CONTROLS. Each changes exactly ONE thing from PART 1. Every one was measured.
-- =====================================================================================

-- C1 the FULL OUTER JOIN alone, unfiltered -> (2). The join is correct until the filter arrives.
CREATE TABLE b (id BIGINT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (2);
REFRESH TABLE b;
SELECT l.id FROM b l FULL OUTER JOIN b r ON l.id = r.id;

-- C2 INNER JOIN -> 0 rows, correct.
CREATE TABLE b (id BIGINT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (2);
REFRESH TABLE b;
SELECT l.id FROM b l INNER JOIN b r ON l.id = r.id WHERE coalesce(l.id, 15) NOT IN (SELECT t2.id FROM b t2 WHERE t2.id = 2);

-- C3 LEFT OUTER JOIN -> 0 rows, correct (pushing into the preserved side is legal here).
CREATE TABLE b (id BIGINT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (2);
REFRESH TABLE b;
SELECT l.id FROM b l LEFT OUTER JOIN b r ON l.id = r.id WHERE coalesce(l.id, 15) NOT IN (SELECT t2.id FROM b t2 WHERE t2.id = 2);

-- C4 RIGHT OUTER JOIN -> 0 rows, correct.
CREATE TABLE b (id BIGINT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (2);
REFRESH TABLE b;
SELECT l.id FROM b l RIGHT OUTER JOIN b r ON l.id = r.id WHERE coalesce(l.id, 15) NOT IN (SELECT t2.id FROM b t2 WHERE t2.id = 2);

-- C5 no coalesce -> 0 rows, correct. The filter is now null-rejecting, so the pushdown is
--    legitimate AND the null-extended row would be filtered anyway. Damage hidden, not absent.
CREATE TABLE b (id BIGINT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (2);
REFRESH TABLE b;
SELECT l.id FROM b l FULL OUTER JOIN b r ON l.id = r.id WHERE l.id NOT IN (SELECT t2.id FROM b t2 WHERE t2.id = 2);

-- C6 coalesce with a nullable default -> 0 rows, correct.
CREATE TABLE b (id BIGINT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (2);
REFRESH TABLE b;
SELECT l.id FROM b l FULL OUTER JOIN b r ON l.id = r.id WHERE coalesce(l.id, l.id) NOT IN (SELECT t2.id FROM b t2 WHERE t2.id = 2);

-- C7 IN instead of NOT IN -> (2), correct.
CREATE TABLE b (id BIGINT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (2);
REFRESH TABLE b;
SELECT l.id FROM b l FULL OUTER JOIN b r ON l.id = r.id WHERE coalesce(l.id, 15) IN (SELECT t2.id FROM b t2 WHERE t2.id = 2);

-- C8 NOT EXISTS instead of NOT IN -> 0 rows, correct.
CREATE TABLE b (id BIGINT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (2);
REFRESH TABLE b;
SELECT l.id FROM b l FULL OUTER JOIN b r ON l.id = r.id WHERE NOT EXISTS (SELECT 1 FROM b t2 WHERE t2.id = 2 AND t2.id = coalesce(l.id, 15));

-- C9 `<> ALL` instead of NOT IN -> STILL WRONG, (NULL). So it is the quantified/anti-join form,
--    not the `NOT IN` spelling.
CREATE TABLE b (id BIGINT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (2);
REFRESH TABLE b;
SELECT l.id FROM b l FULL OUTER JOIN b r ON l.id = r.id WHERE coalesce(l.id, 15) <> ALL (SELECT t2.id FROM b t2 WHERE t2.id = 2);

-- C10 a CONSTANT IN-list instead of a subquery -> 0 rows, correct.
CREATE TABLE b (id BIGINT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (2);
REFRESH TABLE b;
SELECT l.id FROM b l FULL OUTER JOIN b r ON l.id = r.id WHERE coalesce(l.id, 15) NOT IN (2);

-- C11 a constant IN-list that also contains coalesce's default -> 0 rows, correct.
CREATE TABLE b (id BIGINT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (2);
REFRESH TABLE b;
SELECT l.id FROM b l FULL OUTER JOIN b r ON l.id = r.id WHERE coalesce(l.id, 15) NOT IN (2, 15);

-- C12 the subquery UNFILTERED -> STILL WRONG, (NULL). A subquery is required; its filter is not.
CREATE TABLE b (id BIGINT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (2);
REFRESH TABLE b;
SELECT l.id FROM b l FULL OUTER JOIN b r ON l.id = r.id WHERE coalesce(l.id, 15) NOT IN (SELECT t2.id FROM b t2);

-- C13 a benign (non-null-rejecting, non-subquery) filter over the same join -> (2), correct.
CREATE TABLE b (id BIGINT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (2);
REFRESH TABLE b;
SELECT l.id FROM b l FULL OUTER JOIN b r ON l.id = r.id WHERE coalesce(l.id, 15) >= -100;

-- C14 two DIFFERENT tables rather than a self-join -> STILL WRONG, (NULL).
CREATE TABLE b (id BIGINT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (2);
REFRESH TABLE b;
CREATE TABLE b2 (id BIGINT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b2 VALUES (2);
REFRESH TABLE b2;
SELECT l.id FROM b l FULL OUTER JOIN b2 r ON l.id = r.id WHERE coalesce(l.id, 15) NOT IN (SELECT t2.id FROM b t2 WHERE t2.id = 2);

-- C15 optimizer_merge_filter_and_collect = false -> STILL WRONG, (NULL). Note this is the rule
--     that fixes the sibling finding cratedb-run4-round341; it does NOT fix this one.
CREATE TABLE b (id BIGINT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (2);
REFRESH TABLE b;
SET optimizer_merge_filter_and_collect = false;
SELECT l.id FROM b l FULL OUTER JOIN b r ON l.id = r.id WHERE coalesce(l.id, 15) NOT IN (SELECT t2.id FROM b t2 WHERE t2.id = 2);

-- C16 optimizer_move_filter_beneath_join = false -> STILL WRONG, (NULL).
CREATE TABLE b (id BIGINT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (2);
REFRESH TABLE b;
SET optimizer_move_filter_beneath_join = false;
SELECT l.id FROM b l FULL OUTER JOIN b r ON l.id = r.id WHERE coalesce(l.id, 15) NOT IN (SELECT t2.id FROM b t2 WHERE t2.id = 2);


-- =====================================================================================
-- PART 5 -- THE ORIGINAL FINDING, verbatim, for provenance.
-- 5a: BASE (a plain table) -- 6 rows.
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
SELECT (SELECT MAX(CAST($$2014-04-04 10:10:10.100000$$ AS TIMESTAMP WITHOUT TIME ZONE)) AS expr_0_timestamp FROM t AS t3 WHERE (CASE WHEN False THEN CAST($$2016-10-10$$ AS TIMESTAMP WITHOUT TIME ZONE) ELSE CAST($$2016-12-10$$ AS TIMESTAMP WITHOUT TIME ZONE) END) <> coalesce(CAST($$2016-12-10$$ AS TIMESTAMP WITHOUT TIME ZONE), CAST($$2016-04-10$$ AS TIMESTAMP WITHOUT TIME ZONE))) AS expr_0_timestamp, CEIL(t1.id) AS expr_1_number, (SELECT SUM(11) AS expr_0_number FROM t AS t4 CROSS JOIN t AS t5 LEFT OUTER JOIN t AS t6 ON t5.created_at = t6.name WHERE True) AS expr_2_number, (SELECT MIN(t7.id) AS expr_0_number FROM t AS t7 WHERE False) AS expr_3_number, CAST($$2014-04-04 10:10:10.100000$$ AS TIMESTAMP WITHOUT TIME ZONE) AS expr_4_timestamp, t1.created_at AS expr_5_varchar, MAX(CAST($$$$ || SUBSTR(SUBSTR($$©$$, t1.id), t1.id) AS TEXT)) OVER (PARTITION BY NULLIF(t1.name, t1.name) ORDER BY t1.name DESC) AS expr_6_text, MIN(least(t1.id, t1.id, t1.id, t1.id - t1.id, (t1.id / 2), t1.id + COALESCE(CASE WHEN False THEN t1.id ELSE -12345678 END, t1.id), t1.id, t1.id, 11)) OVER (PARTITION BY $$MONTH$$ ORDER BY t1.name IN (t1.name, LTRIM($$HOUR$$, t1.name), CASE WHEN NULLIF(CAST(t1.id AS BOOLEAN), True) THEN t1.name END, t1.name, least(INITCAP(least($$YEAR$$, t1.name)), t1.created_at, GREATEST(t1.name, t1.name), SUBSTR(NULLIF($$MONTH$$, t1.name), t1.id), t1.created_at, COALESCE($$©$$, SUBSTRING($$©$$, t1.id)), $$YEAR$$, greatest(t1.created_at, LEAST(t1.created_at, t1.name)))) ASC NULLS FIRST) AS expr_7_number, t1.name AS expr_8_varchar, True AS expr_9_boolean FROM t AS t1 WHERE coalesce(CEIL(t1.id), 15) NOT IN (SELECT DISTINCT CASE WHEN t2.name IS NOT NULL THEN t2.id + t2.id ELSE CAST(13.89822 AS DOUBLE PRECISION) END AS expr_0_float FROM t AS t2 WHERE CAST(t2.id AS BOOLEAN)) GROUP BY t1.name, t1.id, t1.created_at;

-- 5b: EQUIVALENT (the column-split + FULL-OUTER-JOIN-rejoin builder) -- 8 rows, WRONG.
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
CREATE TABLE t__base_table_1 (id BIGINT, name TEXT, created_at TEXT, eq_seq_key_1 BIGINT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t__base_table_1 (id, name, created_at, eq_seq_key_1) SELECT id, name, created_at, ROW_NUMBER() OVER (ORDER BY id) AS eq_seq_key_1 FROM t__base;
CREATE VIEW t__base_view_1 AS SELECT id, eq_seq_key_1 FROM t__base_table_1;
CREATE VIEW t__base_view_2 AS SELECT name, created_at, eq_seq_key_1 FROM t__base_table_1;
CREATE VIEW t AS SELECT l.id AS id, r.name AS name, r.created_at AS created_at FROM t__base_view_1 l FULL OUTER JOIN t__base_view_2 r ON l.eq_seq_key_1 = r.eq_seq_key_1;
SELECT (SELECT MAX(CAST($$2014-04-04 10:10:10.100000$$ AS TIMESTAMP WITHOUT TIME ZONE)) AS expr_0_timestamp FROM t AS t3 WHERE (CASE WHEN False THEN CAST($$2016-10-10$$ AS TIMESTAMP WITHOUT TIME ZONE) ELSE CAST($$2016-12-10$$ AS TIMESTAMP WITHOUT TIME ZONE) END) <> coalesce(CAST($$2016-12-10$$ AS TIMESTAMP WITHOUT TIME ZONE), CAST($$2016-04-10$$ AS TIMESTAMP WITHOUT TIME ZONE))) AS expr_0_timestamp, CEIL(t1.id) AS expr_1_number, (SELECT SUM(11) AS expr_0_number FROM t AS t4 CROSS JOIN t AS t5 LEFT OUTER JOIN t AS t6 ON t5.created_at = t6.name WHERE True) AS expr_2_number, (SELECT MIN(t7.id) AS expr_0_number FROM t AS t7 WHERE False) AS expr_3_number, CAST($$2014-04-04 10:10:10.100000$$ AS TIMESTAMP WITHOUT TIME ZONE) AS expr_4_timestamp, t1.created_at AS expr_5_varchar, MAX(CAST($$$$ || SUBSTR(SUBSTR($$©$$, t1.id), t1.id) AS TEXT)) OVER (PARTITION BY NULLIF(t1.name, t1.name) ORDER BY t1.name DESC) AS expr_6_text, MIN(least(t1.id, t1.id, t1.id, t1.id - t1.id, (t1.id / 2), t1.id + COALESCE(CASE WHEN False THEN t1.id ELSE -12345678 END, t1.id), t1.id, t1.id, 11)) OVER (PARTITION BY $$MONTH$$ ORDER BY t1.name IN (t1.name, LTRIM($$HOUR$$, t1.name), CASE WHEN NULLIF(CAST(t1.id AS BOOLEAN), True) THEN t1.name END, t1.name, least(INITCAP(least($$YEAR$$, t1.name)), t1.created_at, GREATEST(t1.name, t1.name), SUBSTR(NULLIF($$MONTH$$, t1.name), t1.id), t1.created_at, COALESCE($$©$$, SUBSTRING($$©$$, t1.id)), $$YEAR$$, greatest(t1.created_at, LEAST(t1.created_at, t1.name)))) ASC NULLS FIRST) AS expr_7_number, t1.name AS expr_8_varchar, True AS expr_9_boolean FROM t AS t1 WHERE coalesce(CEIL(t1.id), 15) NOT IN (SELECT DISTINCT CASE WHEN t2.name IS NOT NULL THEN t2.id + t2.id ELSE CAST(13.89822 AS DOUBLE PRECISION) END AS expr_0_float FROM t AS t2 WHERE CAST(t2.id AS BOOLEAN)) GROUP BY t1.name, t1.id, t1.created_at;

-- 5c: 5b with the rule disabled -- 6 rows, matching the base.
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
CREATE TABLE t__base_table_1 (id BIGINT, name TEXT, created_at TEXT, eq_seq_key_1 BIGINT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t__base_table_1 (id, name, created_at, eq_seq_key_1) SELECT id, name, created_at, ROW_NUMBER() OVER (ORDER BY id) AS eq_seq_key_1 FROM t__base;
CREATE VIEW t__base_view_1 AS SELECT id, eq_seq_key_1 FROM t__base_table_1;
CREATE VIEW t__base_view_2 AS SELECT name, created_at, eq_seq_key_1 FROM t__base_table_1;
CREATE VIEW t AS SELECT l.id AS id, r.name AS name, r.created_at AS created_at FROM t__base_view_1 l FULL OUTER JOIN t__base_view_2 r ON l.eq_seq_key_1 = r.eq_seq_key_1;
SET optimizer_rewrite_filter_on_outer_join_to_inner_join = false;
SELECT (SELECT MAX(CAST($$2014-04-04 10:10:10.100000$$ AS TIMESTAMP WITHOUT TIME ZONE)) AS expr_0_timestamp FROM t AS t3 WHERE (CASE WHEN False THEN CAST($$2016-10-10$$ AS TIMESTAMP WITHOUT TIME ZONE) ELSE CAST($$2016-12-10$$ AS TIMESTAMP WITHOUT TIME ZONE) END) <> coalesce(CAST($$2016-12-10$$ AS TIMESTAMP WITHOUT TIME ZONE), CAST($$2016-04-10$$ AS TIMESTAMP WITHOUT TIME ZONE))) AS expr_0_timestamp, CEIL(t1.id) AS expr_1_number, (SELECT SUM(11) AS expr_0_number FROM t AS t4 CROSS JOIN t AS t5 LEFT OUTER JOIN t AS t6 ON t5.created_at = t6.name WHERE True) AS expr_2_number, (SELECT MIN(t7.id) AS expr_0_number FROM t AS t7 WHERE False) AS expr_3_number, CAST($$2014-04-04 10:10:10.100000$$ AS TIMESTAMP WITHOUT TIME ZONE) AS expr_4_timestamp, t1.created_at AS expr_5_varchar, MAX(CAST($$$$ || SUBSTR(SUBSTR($$©$$, t1.id), t1.id) AS TEXT)) OVER (PARTITION BY NULLIF(t1.name, t1.name) ORDER BY t1.name DESC) AS expr_6_text, MIN(least(t1.id, t1.id, t1.id, t1.id - t1.id, (t1.id / 2), t1.id + COALESCE(CASE WHEN False THEN t1.id ELSE -12345678 END, t1.id), t1.id, t1.id, 11)) OVER (PARTITION BY $$MONTH$$ ORDER BY t1.name IN (t1.name, LTRIM($$HOUR$$, t1.name), CASE WHEN NULLIF(CAST(t1.id AS BOOLEAN), True) THEN t1.name END, t1.name, least(INITCAP(least($$YEAR$$, t1.name)), t1.created_at, GREATEST(t1.name, t1.name), SUBSTR(NULLIF($$MONTH$$, t1.name), t1.id), t1.created_at, COALESCE($$©$$, SUBSTRING($$©$$, t1.id)), $$YEAR$$, greatest(t1.created_at, LEAST(t1.created_at, t1.name)))) ASC NULLS FIRST) AS expr_7_number, t1.name AS expr_8_varchar, True AS expr_9_boolean FROM t AS t1 WHERE coalesce(CEIL(t1.id), 15) NOT IN (SELECT DISTINCT CASE WHEN t2.name IS NOT NULL THEN t2.id + t2.id ELSE CAST(13.89822 AS DOUBLE PRECISION) END AS expr_0_float FROM t AS t2 WHERE CAST(t2.id AS BOOLEAN)) GROUP BY t1.name, t1.id, t1.created_at;
