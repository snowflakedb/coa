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

-- CrateDB: a UNION ALL branch reading a relation with an extra COMPUTED column shifts
-- column positions above the union -- silent WRONG ROWS, or XX000 "Cannot cast value ..."
--
-- Build      : CrateDB 6.4.1 (release tarball). Reproduces with assertions ON *and OFF*
--              ('-da -dsa') -- a stock production node is affected.
-- Severity   : DATA CORRECTNESS. Two faces of one defect, decided only by whether the
--              shifted column's type happens to be compatible:
--                * incompatible -> XX000 "Cannot cast value `dup` to type `bigint`" (loud)
--                * compatible   -> SILENTLY WRONG ROWS (PART 3: 4 rows where 2 are correct,
--                                  with c1's values appearing in the c0 output position)
-- Session    : search_path=<per-connection schema>; enable_hashjoin=true;
--              error_on_unknown_object_key=true; insert_select_fail_fast=true;
--              optimizer_equi_join_to_lookup_join=false.  No setting is load-bearing.
-- Determinism: deterministic; needs >= 1 row.
-- Origin     : hunt log (round 143, seed 600899756)
--              -- SIX findings, one byte-identical equivalence chain, six different queries.
--
-- TRIGGER (all three required):
--   1. UNION ALL  (UNION DISTINCT is clean)
--   2. one branch reads a VIEW or DERIVED TABLE that projects an extra *computed* column the
--      union does not output.  An extra *stored* column on a plain table is CLEAN -- it is the
--      computed/literal projection that shifts positions, not the width.
--   3. the query has GROUP BY plus an aggregate over a column NOT in the GROUP BY
--      (in HAVING or in the select list -- both fail; no aggregate is clean)
--   Also needs 3 columns: the 2-column analogue is clean.
--
-- HOW TO RUN: CrateDB needs a REFRESH between a write and a read. Each PART is independent;
--   run each in its own schema (or via the triage driver, one fresh schema per candidate).


-- =====================================================================================
-- PART 1 -- CONCRETE: the finding as eqgen produced it (error_round143_1, the simplest of
--           the six queries over the shared chain).
-- 1a: BASE relation (a plain table) -- the query SUCCEEDS.  Expected 7 rows.
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
SELECT t1.id AS expr_0_number, MIN(t1.name IS NOT NULL) IS NULL AS expr_1_boolean FROM t AS t1 GROUP BY t1.name, t1.id HAVING MIN(t1.created_at) <= t1.name;

-- =====================================================================================
-- 1b: EQUIVALENT relation -- row-identical to base t (8 identical rows, admissibility
--     verified). 36-statement chain; the culprit link, found by bisecting it, is
--       CREATE VIEW t__base_view_7 AS SELECT id, name, created_at,
--              CAST(NULL AS BIGINT) AS eq_tmp_col_1 FROM t__base_view_6;
--     i.e. the builders' add-then-drop-a-column round-trip, feeding the ODD branch of the
--     final UNION ALL. Pointing the odd branch at t__base_view_6 (one layer below view_7)
--     is clean; at view_7 or anything above it, it fails.
-- Expected 7 rows, actual: ERROR "Cannot cast value `dup` to type `bigint`"
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
ALTER TABLE t RENAME TO t__base;
CREATE TABLE t__base_table_1 (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t__base_table_1 (id, name, created_at) SELECT * FROM t__base;
CREATE TABLE t__base_table_2 (id BIGINT, name TEXT, created_at TEXT, INDEX t__base_idx_1 USING FULLTEXT (name, created_at) WITH (analyzer='english')) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t__base_table_2 (id, name, created_at) SELECT id, name, created_at FROM t__base_table_1;
CREATE VIEW t__base_view_1 AS SELECT id, name, created_at FROM t__base_table_2;
CREATE TABLE t__base_table_3 (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t__base_table_3 (id, name, created_at) SELECT * FROM t__base_view_1 WHERE MOD(id, 2) = 0;
CREATE TABLE t__base_table_4 (id BIGINT INDEX OFF, name TEXT INDEX OFF, created_at TEXT INDEX OFF) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t__base_table_4 (id, name, created_at) SELECT id, name, created_at FROM t__base_table_3;
CREATE VIEW t__base_view_2 AS SELECT id, name, created_at FROM t__base_table_4;
CREATE TABLE t__base_table_5 (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t__base_table_5 (id, name, created_at) SELECT * FROM t__base;
CREATE TABLE t__base_table_6 (id BIGINT INDEX OFF, name TEXT INDEX OFF, created_at TEXT INDEX OFF) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t__base_table_6 (id, name, created_at) SELECT id, name, created_at FROM t__base_table_5;
CREATE VIEW t__base_view_3 AS SELECT id, name, created_at FROM t__base_table_6;
CREATE TABLE t__base_table_7 (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t__base_table_7 (id, name, created_at) SELECT * FROM t__base_view_3;
CREATE TABLE t__base_table_8 (id BIGINT, name TEXT, created_at TEXT, t__base_bucket_1 INTEGER GENERATED ALWAYS AS (id % 4)) PARTITIONED BY (t__base_bucket_1) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t__base_table_8 (id, name, created_at) SELECT id, name, created_at FROM t__base_table_7;
CREATE VIEW t__base_view_4 AS SELECT id, name, created_at FROM t__base_table_8;
CREATE VIEW t__base_view_5 AS SELECT * FROM t__base_view_4;
CREATE VIEW t__base_view_6 AS SELECT id, name, created_at FROM t__base_view_5;
CREATE VIEW t__base_view_7 AS SELECT id, name, created_at, CAST(NULL AS BIGINT) AS eq_tmp_col_1 FROM t__base_view_6;
CREATE VIEW t__base_view_8 AS SELECT id, name, created_at FROM t__base_view_7;
CREATE VIEW t__base_view_9 AS SELECT * FROM t__base_view_8 WHERE (MOD(id, 2) <> 0) OR (id IS NULL);
CREATE VIEW t AS SELECT * FROM t__base_view_2 UNION ALL SELECT * FROM t__base_view_9;
SELECT t1.id AS expr_0_number, MIN(t1.name IS NOT NULL) IS NULL AS expr_1_boolean FROM t AS t1 GROUP BY t1.name, t1.id HAVING MIN(t1.created_at) <= t1.name;

-- =====================================================================================
-- PART 2 -- DISTILLED minimal repro of the LOUD face. One table, one row, no view.
-- Expected 1 row (id = 0), actual: ERROR "Cannot cast value `dup` to type `bigint`"
--   ('dup' is a TEXT value being cast to `id`'s BIGINT type -- a one-position shift.)
-- =====================================================================================
CREATE TABLE b (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (0, 'dup', 'dup');
REFRESH TABLE b;
SELECT id FROM (SELECT * FROM b
                UNION ALL
                SELECT id, name, created_at FROM (SELECT id, name, created_at, CAST(NULL AS BIGINT) AS extra FROM b) s) t
GROUP BY name, id HAVING MIN(created_at) <= name;

-- =====================================================================================
-- PART 3 -- THE SAME DEFECT AS A SILENT WRONG RESULT. All three columns are TEXT, so no
--           cast can fail and nothing is reported -- the shift just produces extra groups
--           built from the neighbouring column.
-- Expected 2 rows: ('aaa','zzz'), ('bbb','yyy')
-- Actual   4 rows: ('aaa','zzz'), ('bbb','yyy'), ('mmm','zzz'), ('nnn','yyy')
--                                               ^^^^^ these are c1 values in the c0 slot
-- =====================================================================================
CREATE TABLE b (c0 TEXT, c1 TEXT, c2 TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES ('aaa', 'mmm', 'zzz');
INSERT INTO b VALUES ('bbb', 'nnn', 'yyy');
REFRESH TABLE b;
CREATE VIEW t AS SELECT * FROM b UNION ALL SELECT c0, c1, c2 FROM (SELECT c0, c1, c2, CAST(NULL AS BIGINT) AS extra FROM b) s;
SELECT c0, MIN(c2) FROM t GROUP BY c1, c0;


-- =====================================================================================
-- PART 4 -- CONTROLS. Each changes exactly ONE thing from PART 2 / PART 3.
-- =====================================================================================

-- C1 CORRECT ANSWER for PART 3: identical union, extra computed column removed.  2 rows.
CREATE TABLE b (c0 TEXT, c1 TEXT, c2 TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES ('aaa', 'mmm', 'zzz');
INSERT INTO b VALUES ('bbb', 'nnn', 'yyy');
REFRESH TABLE b;
CREATE VIEW t AS SELECT * FROM b UNION ALL SELECT c0, c1, c2 FROM (SELECT c0, c1, c2 FROM b) s;
SELECT c0, MIN(c2) FROM t GROUP BY c1, c0;

-- C2 CORRECT ANSWER for PART 3: plain table, no union at all.  2 rows.
CREATE TABLE b (c0 TEXT, c1 TEXT, c2 TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES ('aaa', 'mmm', 'zzz');
INSERT INTO b VALUES ('bbb', 'nnn', 'yyy');
REFRESH TABLE b;
SELECT c0, MIN(c2) FROM b GROUP BY c1, c0;

-- C3 extra column removed (PART 2 shape) -> clean.  1 row.
CREATE TABLE b (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (0, 'dup', 'dup');
REFRESH TABLE b;
SELECT id FROM (SELECT * FROM b UNION ALL SELECT id, name, created_at FROM (SELECT id, name, created_at FROM b) s) t
GROUP BY name, id HAVING MIN(created_at) <= name;

-- C4 the extra column is a STORED column on a plain 4-col table -> clean. Width is not the
--    trigger; a computed projection is.  1 row.
CREATE TABLE b (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (0, 'dup', 'dup');
REFRESH TABLE b;
CREATE TABLE b4 (id BIGINT, name TEXT, created_at TEXT, extra BIGINT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b4 (id, name, created_at) SELECT id, name, created_at FROM b;
REFRESH TABLE b4;
SELECT id FROM (SELECT * FROM b UNION ALL SELECT id, name, created_at FROM b4) t
GROUP BY name, id HAVING MIN(created_at) <= name;

-- C5 UNION DISTINCT instead of UNION ALL -> clean.  1 row.
CREATE TABLE b (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (0, 'dup', 'dup');
REFRESH TABLE b;
SELECT id FROM (SELECT * FROM b UNION SELECT id, name, created_at FROM (SELECT id, name, created_at, CAST(NULL AS BIGINT) AS extra FROM b) s) t
GROUP BY name, id HAVING MIN(created_at) <= name;

-- C6 no union (t = the wide derived table alone) -> clean.  1 row.
CREATE TABLE b (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (0, 'dup', 'dup');
REFRESH TABLE b;
SELECT id FROM (SELECT id, name, created_at FROM (SELECT id, name, created_at, CAST(NULL AS BIGINT) AS extra FROM b) s) t
GROUP BY name, id HAVING MIN(created_at) <= name;

-- C7 aggregate over a GROUPED column instead -> clean.  1 row.
CREATE TABLE b (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (0, 'dup', 'dup');
REFRESH TABLE b;
SELECT id FROM (SELECT * FROM b UNION ALL SELECT id, name, created_at FROM (SELECT id, name, created_at, CAST(NULL AS BIGINT) AS extra FROM b) s) t
GROUP BY name, id HAVING MIN(name) <= name;

-- C8 no aggregate at all -> clean.  1 row.
CREATE TABLE b (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (0, 'dup', 'dup');
REFRESH TABLE b;
SELECT id FROM (SELECT * FROM b UNION ALL SELECT id, name, created_at FROM (SELECT id, name, created_at, CAST(NULL AS BIGINT) AS extra FROM b) s) t
GROUP BY name, id;

-- C9 aggregate over a non-grouped column in the SELECT LIST rather than HAVING -> STILL FAILS.
--    So the ingredient is "aggregate over a non-grouped column", not "HAVING".
CREATE TABLE b (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (0, 'dup', 'dup');
REFRESH TABLE b;
SELECT id, MIN(created_at) FROM (SELECT * FROM b UNION ALL SELECT id, name, created_at FROM (SELECT id, name, created_at, CAST(NULL AS BIGINT) AS extra FROM b) s) t
GROUP BY name, id;

-- C10 extra computed column typed TEXT rather than BIGINT -> STILL FAILS, with the same
--     "to type `bigint`" message. The target type comes from `id`, not from the extra column.
CREATE TABLE b (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (0, 'dup', 'dup');
REFRESH TABLE b;
SELECT id FROM (SELECT * FROM b UNION ALL SELECT id, name, created_at FROM (SELECT id, name, created_at, CAST(NULL AS TEXT) AS extra FROM b) s) t
GROUP BY name, id HAVING MIN(created_at) <= name;

-- C11 extra computed column FIRST rather than last -> STILL FAILS.
CREATE TABLE b (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (0, 'dup', 'dup');
REFRESH TABLE b;
SELECT id FROM (SELECT * FROM b UNION ALL SELECT id, name, created_at FROM (SELECT CAST(NULL AS BIGINT) AS extra, id, name, created_at FROM b) s) t
GROUP BY name, id HAVING MIN(created_at) <= name;

-- C12 two columns instead of three -> clean. Three columns are required.
CREATE TABLE b2 (c0 TEXT, c1 TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b2 VALUES ('aaa', 'zzz');
REFRESH TABLE b2;
SELECT c0, MIN(c1) FROM (SELECT * FROM b2 UNION ALL SELECT c0, c1 FROM (SELECT c0, c1, CAST(NULL AS BIGINT) AS extra FROM b2) s) t
GROUP BY c0;

-- C13 GH#13779's verbatim repro (the 2023 "UNION attribute mixup" cast bug, closed as fixed
--     in 5.2.x) -> CLEAN on 6.4.1.  So this finding is a surviving SIBLING of that class,
--     not a regression of it.  1 row.
CREATE TABLE tu01 ("o" OBJECT(STRICT) AS (a text, l text, ts BIGINT, t text, v integer)) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO tu01 (o) VALUES ({a='a',l='l',ts=1700000000000,t='t',v=5});
INSERT INTO tu01 (o) VALUES ({a='a',l='l',ts=1700000000000,t='t2',v=5});
INSERT INTO tu01 (o) VALUES ({a='a',l='l',ts=1700000000000,t='t3',v=5});
REFRESH TABLE tu01;
CREATE OR REPLACE VIEW tu01v AS SELECT o['l'] AS l, o['ts']::timestamp ts, o['a'] AS a, o['v'] AS v, o['t'] AS t FROM tu01;
SELECT ts::date dt, avg(v) AS value, l, a FROM tu01v WHERE t='t' GROUP BY 1,3,4
UNION SELECT ts::date dt, avg(v) AS value, l, a FROM tu01v WHERE t='t2' GROUP BY 1,3,4
UNION SELECT ts::date dt, avg(v) AS value, l, a FROM tu01v WHERE t='t3' GROUP BY 1,3,4;
