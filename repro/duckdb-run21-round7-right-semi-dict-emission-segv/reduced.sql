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

-- DuckDB: SIGSEGV in JoinHashTable::ScanStructure::NextRightSemiOrAntiJoin
-- (RIGHT_SEMI hash join, small-build-side dictionary emission)
--
-- Build      : DuckDB v2.0.0-alpha36551 (Cyanoptera) 3958a013ed, prebuilt CLI (assertions OFF)
-- Clean on   : DuckDB 1.5.0 (Python wheel) -- returns the correct answer, no crash
-- Session    : all defaults. NOT a race: reproduces with SET threads=1.
-- Determinism: 8/8 runs crash. SIGSEGV, core dumped (harness recorded SIGABRT for the same input).
-- Origin     : hunt log  (round 7, seed 221459190)
--
-- Load-bearing composition (all four are required):
--   (1) a view whose projection is a WINDOW aggregate with PARTITION BY  (the eqgen
--       "row-multiply then collapse" equivalence builder emits exactly this)
--   (2) that view self-joined on an equality AND cross-joined a third time, so the
--       planner CSEs it into one CTE and overestimates it by ~5 orders of magnitude
--   (3) `<bool expr> IN (<that subquery>)`, which the optimizer flips SEMI -> RIGHT_SEMI
--       because the subquery side is estimated far larger than the outer relation
--   (4) a build side with very few DISTINCT key values (=> small-build-side dictionary
--       emission) and a probe side with >= 2 matching rows AND >= 1 NULL
--
-- HOW TO RUN: each PART below must be run in a SEPARATE fresh database. Parts 1 and 2
-- kill the process, so they cannot be concatenated with anything after them.
--   duckdb /tmp/p1.db < <part>          # crashing parts: exit 139 (SIGSEGV)


-- =====================================================================================
-- PART 1 -- CONCRETE: the equivalence construction as the eqgen builder emits it,
--                     with the real column names and the real workload query.
--                     The intermediate view chain of the original finding is collapsed
--                     to the load-bearing link (the ROW_NUMBER key + the row multiply +
--                     the collapsing MAX() OVER (PARTITION BY) view).
-- Expected: 1 row -> (0, 7, 7, 7, 7, 7, 7.77)   -- what base `t` returns
-- Actual  : SIGSEGV
-- =====================================================================================
CREATE TABLE t__base (c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10, 2), c_real DOUBLE, c_txt VARCHAR, c_chr VARCHAR, c_flag BOOLEAN, c_date DATE, c_ts TIMESTAMP);
INSERT INTO t__base VALUES (-3, -3, -1.5, -1.5, 'a', 'a', FALSE, '2020-01-01', '2020-01-01 00:00:00');
INSERT INTO t__base VALUES (-1, -1, 0.0, 0.0, '', '', TRUE, '1999-12-31', NULL);
INSERT INTO t__base VALUES (0, 0, 12.34, 1.25, 'dup', 'dup', TRUE, NULL, '2020-06-15 12:30:00');
INSERT INTO t__base VALUES (1, 1, 12.34, 1.25, 'dup', 'dup', FALSE, '2020-01-01', '2020-01-01 00:00:00');
INSERT INTO t__base VALUES (2, 2, NULL, NULL, NULL, NULL, NULL, '2021-03-03', '1999-12-31 23:59:59');
INSERT INTO t__base VALUES (2, 2, -0.01, 3.14, 'zzz', 'zzz', TRUE, '2000-02-29', '2021-03-03 09:00:00');
INSERT INTO t__base VALUES (NULL, NULL, 100.0, -2.5, 'b', 'b', FALSE, '2022-11-11', '2000-01-01 01:01:01');
INSERT INTO t__base VALUES (7, 7, 7.77, 9.99, 'é', 'é', TRUE, '2020-06-15', '2022-11-11 11:11:11');
CREATE TABLE t__base_table_3 AS SELECT c_int, c_big, c_dec, c_real, c_txt, c_chr, c_flag, c_date, c_ts, ROW_NUMBER() OVER (ORDER BY c_int) AS eq_key_1 FROM t__base;
CREATE TABLE t__base_table_4 AS SELECT c_int, c_big, c_dec, c_real, c_txt, c_chr, c_flag, c_date, c_ts, eq_key_1 FROM t__base_table_3 CROSS JOIN generate_series(1, 10);
CREATE VIEW t AS SELECT c_int, c_big, c_dec, c_real, c_txt, c_chr, c_flag, c_date, c_ts FROM (SELECT DISTINCT eq_key_1, MAX(c_int) OVER (PARTITION BY eq_key_1) AS c_int, MAX(c_big) OVER (PARTITION BY eq_key_1) AS c_big, MAX(c_dec) OVER (PARTITION BY eq_key_1) AS c_dec, MAX(c_real) OVER (PARTITION BY eq_key_1) AS c_real, MAX(c_txt) OVER (PARTITION BY eq_key_1) AS c_txt, MAX(c_chr) OVER (PARTITION BY eq_key_1) AS c_chr, MAX(c_flag) OVER (PARTITION BY eq_key_1) AS c_flag, MAX(c_date) OVER (PARTITION BY eq_key_1) AS c_date, MAX(c_ts) OVER (PARTITION BY eq_key_1) AS c_ts FROM t__base_table_4);
SELECT MOD(t1.c_int, 1) AS expr_0_integer, SUM(trunc(t1.c_big)) AS expr_1_number38__0, t1.c_int AS expr_2_integer, t1.c_int AS expr_3_integer, t1.c_int AS expr_4_integer, t1.c_int AS expr_5_integer, MIN(t1.c_dec) AS expr_6_number10__2 FROM t AS t1 WHERE (t1.c_int IN (t1.c_int)) IN (SELECT t2.c_flag AS expr_0_boolean FROM t AS t2 LEFT OUTER JOIN t AS t3 ON t2.c_txt = t3.c_txt INNER JOIN t AS t4 ON t3.c_ts >= t4.c_date WHERE coalesce(t4.c_flag, t4.c_flag) ORDER BY t2.c_flag NULLS FIRST, 1) GROUP BY t1.c_int HAVING MIN(t1.c_big) = t1.c_int QUALIFY COUNT(*) OVER (ORDER BY False DESC NULLS FIRST, False DESC NULLS FIRST ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) < (t1.c_int & t1.c_int);

-- =====================================================================================
-- PART 2 -- DISTILLED minimal repro (6 statements, 30 rows, no eqgen vocabulary).
--           `big`      : 30 rows, 4 distinct `s` values
--           `v`        : the window-aggregate view -- 30 rows, ONE distinct `f` value (true)
--           subquery   : v JOIN v ON s CROSS JOIN v -> 6780 rows, 1 distinct key
--           `p`        : the probe side -- 2 matching rows + 1 NULL
-- Expected: 1 row -> 2
-- Actual  : SIGSEGV
-- =====================================================================================
CREATE TABLE big (f BOOLEAN, s VARCHAR);
INSERT INTO big SELECT (i % 3) = 0, ['a', '', 'dup', 'zzz'][(i % 4) + 1] FROM generate_series(1, 30) t(i);
CREATE VIEW v AS SELECT MAX(f) OVER (PARTITION BY s) AS f, s FROM big;
CREATE TABLE p (pb BOOLEAN);
INSERT INTO p VALUES (TRUE), (TRUE), (NULL);
SELECT COUNT(*) FROM p WHERE p.pb IN (SELECT x.f FROM v x JOIN v y ON x.s = y.s CROSS JOIN v z);


-- =====================================================================================
-- PART 3 -- CONTROLS. Each is PART 2 with exactly ONE token changed. Every control
--           below runs clean and returns the correct answer, which is what makes each
--           changed token a necessary ingredient. Run each in a fresh database.
-- =====================================================================================

-- C1  the decisive one: disable only the SEMI -> RIGHT_SEMI build/probe-side flip.
--     Expected 2, actual 2 (no crash) -- so the RIGHT_SEMI plan is the trigger.
CREATE TABLE big (f BOOLEAN, s VARCHAR);
INSERT INTO big SELECT (i % 3) = 0, ['a', '', 'dup', 'zzz'][(i % 4) + 1] FROM generate_series(1, 30) t(i);
CREATE VIEW v AS SELECT MAX(f) OVER (PARTITION BY s) AS f, s FROM big;
CREATE TABLE p (pb BOOLEAN);
INSERT INTO p VALUES (TRUE), (TRUE), (NULL);
SET disabled_optimizers = 'build_side_probe_side';
SELECT COUNT(*) FROM p WHERE p.pb IN (SELECT x.f FROM v x JOIN v y ON x.s = y.s CROSS JOIN v z);  -- 2

-- C2  window aggregate -> GROUP BY aggregate. Same rows, no window operator.  -> 2, no crash
CREATE TABLE big (f BOOLEAN, s VARCHAR);
INSERT INTO big SELECT (i % 3) = 0, ['a', '', 'dup', 'zzz'][(i % 4) + 1] FROM generate_series(1, 30) t(i);
CREATE VIEW v AS SELECT MAX(f) AS f, s FROM big GROUP BY s;
CREATE TABLE p (pb BOOLEAN);
INSERT INTO p VALUES (TRUE), (TRUE), (NULL);
SELECT COUNT(*) FROM p WHERE p.pb IN (SELECT x.f FROM v x JOIN v y ON x.s = y.s CROSS JOIN v z);  -- 2

-- C3  window kept but PARTITION BY dropped.  -> 2, no crash
CREATE TABLE big (f BOOLEAN, s VARCHAR);
INSERT INTO big SELECT (i % 3) = 0, ['a', '', 'dup', 'zzz'][(i % 4) + 1] FROM generate_series(1, 30) t(i);
CREATE VIEW v AS SELECT MAX(f) OVER () AS f, s FROM big;
CREATE TABLE p (pb BOOLEAN);
INSERT INTO p VALUES (TRUE), (TRUE), (NULL);
SELECT COUNT(*) FROM p WHERE p.pb IN (SELECT x.f FROM v x JOIN v y ON x.s = y.s CROSS JOIN v z);  -- 2

-- C4  the same 30 view rows materialised into a plain table (kills the overestimate). -> 2, no crash
CREATE TABLE big (f BOOLEAN, s VARCHAR);
INSERT INTO big SELECT (i % 3) = 0, ['a', '', 'dup', 'zzz'][(i % 4) + 1] FROM generate_series(1, 30) t(i);
CREATE VIEW v AS SELECT MAX(f) OVER (PARTITION BY s) AS f, s FROM big;
CREATE TABLE vt AS SELECT * FROM v;
CREATE TABLE p (pb BOOLEAN);
INSERT INTO p VALUES (TRUE), (TRUE), (NULL);
SELECT COUNT(*) FROM p WHERE p.pb IN (SELECT x.f FROM vt x JOIN vt y ON x.s = y.s CROSS JOIN vt z);  -- 2

-- C5  probe side loses its NULL.  -> 3, no crash
CREATE TABLE big (f BOOLEAN, s VARCHAR);
INSERT INTO big SELECT (i % 3) = 0, ['a', '', 'dup', 'zzz'][(i % 4) + 1] FROM generate_series(1, 30) t(i);
CREATE VIEW v AS SELECT MAX(f) OVER (PARTITION BY s) AS f, s FROM big;
CREATE TABLE p (pb BOOLEAN);
INSERT INTO p VALUES (TRUE), (TRUE), (TRUE);
SELECT COUNT(*) FROM p WHERE p.pb IN (SELECT x.f FROM v x JOIN v y ON x.s = y.s CROSS JOIN v z);  -- 3

-- C6  probe side down to ONE matching row + the NULL.  -> 1, no crash
CREATE TABLE big (f BOOLEAN, s VARCHAR);
INSERT INTO big SELECT (i % 3) = 0, ['a', '', 'dup', 'zzz'][(i % 4) + 1] FROM generate_series(1, 30) t(i);
CREATE VIEW v AS SELECT MAX(f) OVER (PARTITION BY s) AS f, s FROM big;
CREATE TABLE p (pb BOOLEAN);
INSERT INTO p VALUES (TRUE), (NULL);
SELECT COUNT(*) FROM p WHERE p.pb IN (SELECT x.f FROM v x JOIN v y ON x.s = y.s CROSS JOIN v z);  -- 1

-- C7  build key gains many DISTINCT values (no small-build-side dictionary). -> 2, no crash
CREATE TABLE big (f BIGINT, s VARCHAR);
INSERT INTO big SELECT i, ['a', '', 'dup', 'zzz'][(i % 4) + 1] FROM generate_series(1, 30) t(i);
CREATE VIEW v AS SELECT MAX(f) OVER (PARTITION BY s) AS f, s FROM big;
CREATE TABLE p (pb BIGINT);
INSERT INTO p VALUES (29), (30), (NULL);
SELECT COUNT(*) FROM p WHERE p.pb IN (SELECT x.f FROM v x JOIN v y ON x.s = y.s CROSS JOIN v z);  -- 2
-- (the same shape with only 2 DISTINCT BIGINT build keys DOES crash -- see bug_report.md)

-- C8  the third (CROSS JOIN) reference to v removed.  -> 2, no crash
CREATE TABLE big (f BOOLEAN, s VARCHAR);
INSERT INTO big SELECT (i % 3) = 0, ['a', '', 'dup', 'zzz'][(i % 4) + 1] FROM generate_series(1, 30) t(i);
CREATE VIEW v AS SELECT MAX(f) OVER (PARTITION BY s) AS f, s FROM big;
CREATE TABLE p (pb BOOLEAN);
INSERT INTO p VALUES (TRUE), (TRUE), (NULL);
SELECT COUNT(*) FROM p WHERE p.pb IN (SELECT x.f FROM v x JOIN v y ON x.s = y.s);  -- 2

-- C9  IN rewritten as EXISTS (no MARK/SEMI join).  -> 2, no crash
CREATE TABLE big (f BOOLEAN, s VARCHAR);
INSERT INTO big SELECT (i % 3) = 0, ['a', '', 'dup', 'zzz'][(i % 4) + 1] FROM generate_series(1, 30) t(i);
CREATE VIEW v AS SELECT MAX(f) OVER (PARTITION BY s) AS f, s FROM big;
CREATE TABLE p (pb BOOLEAN);
INSERT INTO p VALUES (TRUE), (TRUE), (NULL);
SELECT COUNT(*) FROM p WHERE EXISTS (SELECT 1 FROM (SELECT x.f FROM v x JOIN v y ON x.s = y.s CROSS JOIN v z) q WHERE q.f = p.pb);  -- 2

-- C10 NOT a control -- this one STILL CRASHES. Single-threaded, so the bug is not a race.
CREATE TABLE big (f BOOLEAN, s VARCHAR);
INSERT INTO big SELECT (i % 3) = 0, ['a', '', 'dup', 'zzz'][(i % 4) + 1] FROM generate_series(1, 30) t(i);
CREATE VIEW v AS SELECT MAX(f) OVER (PARTITION BY s) AS f, s FROM big;
CREATE TABLE p (pb BOOLEAN);
INSERT INTO p VALUES (TRUE), (TRUE), (NULL);
SET threads = 1;
SELECT COUNT(*) FROM p WHERE p.pb IN (SELECT x.f FROM v x JOIN v y ON x.s = y.s CROSS JOIN v z);  -- SIGSEGV

-- C11 filter_pushdown disabled also masks it -- but only because that pass is what converts the
--     IN MARK join into a SEMI join, so there is nothing left for build_side_probe_side to flip.
--     Join type stays MARK.  -> 2, no crash.  (C1 is the narrower switch: it keeps SEMI.)
CREATE TABLE big (f BOOLEAN, s VARCHAR);
INSERT INTO big SELECT (i % 3) = 0, ['a', '', 'dup', 'zzz'][(i % 4) + 1] FROM generate_series(1, 30) t(i);
CREATE VIEW v AS SELECT MAX(f) OVER (PARTITION BY s) AS f, s FROM big;
CREATE TABLE p (pb BOOLEAN);
INSERT INTO p VALUES (TRUE), (TRUE), (NULL);
SET disabled_optimizers = 'filter_pushdown';
SELECT COUNT(*) FROM p WHERE p.pb IN (SELECT x.f FROM v x JOIN v y ON x.s = y.s CROSS JOIN v z);  -- 2
