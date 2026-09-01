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

-- CrateDB: UNION ALL + ORDER BY on an unselected column leaks the sort column into the
-- output row -- REGRESSION of GH#17341, closed as fixed in 5.10.1, still broken in 6.4.1.
--
-- Build      : CrateDB 6.4.1 (release tarball). Reproduces with assertions ON *and OFF*
--              ('-da -dsa'), so this hits a stock production node -- unlike the sibling
--              finding repro/cratedb-run3-round19-window-order-by-duplicate-assert.
-- Session    : search_path=<per-connection schema>; enable_hashjoin=true;
--              error_on_unknown_object_key=true; insert_select_fail_fast=true;
--              optimizer_equi_join_to_lookup_join=false.  No setting is load-bearing.
-- Determinism: deterministic. Data-dependent only in that the relation must yield >= 1 row
--              (on an empty table it returns 0 rows cleanly).
-- Origin     : hunt log  (round 149, seed 1477293808)
--
-- TRIGGER (all four required):
--   1. a multi-branch UNION ALL relation (view OR inline derived table -- UNION DISTINCT is safe)
--   2. an ORDER BY on a column that is NOT in the outer select list
--   3. at least one further union output column used by NEITHER the select list nor the ORDER BY
--      (i.e. real column pruning has to happen)
--   4. >= 1 result row
--
-- TWO FACES, ONE CAUSE -- the ORDER BY column stays in the row and is never projected away.
-- *** THE FACE YOU SEE DEPENDS ON YOUR CLIENT. Measured on one 6.4.1 node, both endpoints: ***
--   * aliased select item   -> "Couldn't create execution plan from logical plan because of:
--                              Index 1 out of bounds for length 1: Eval[c3 AS q]"  <- the exact
--                              message and node shape from GH#17341.  FAILS ON EVERY CLIENT
--                              (plan->executor conversion, before any transport).  USE THIS ONE.
--   * plain select item     -> "Number of columns in the row must match number of columnTypes.
--                              Row: RowN{[a, 1]} types: [VarCharType]"   <- the row literally
--                              carries both the selected value AND the leaked sort key.
--                              *** PostgreSQL-WIRE ONLY *** (psql / psycopg / pgJDBC): the check
--                              is in the server's PG-wire encoder, Messages.sendDataRow. Over
--                              HTTP /_sql -- crash, Admin UI, curl, crate-python -- the same
--                              query RETURNS THE CORRECT ROWS, because the HTTP serializer emits
--                              by declared column name and drops the leaked value. If you run
--                              PART 2 / 3a / 3b through crash and see 4 rows, that is expected:
--                              add `AS q` to the select item and it fails there too.
--
-- NAME QUALIFICATION: reproduce with UNQUALIFIED relation names under `SET search_path` (or in the
--   default `doc` schema), the way an ordinary client connects. A hand-rebuild of PART 1b with every
--   relation inside the view bodies written schema-qualified did NOT reproduce; the distilled PART 2
--   shape is insensitive to it. Not isolated further -- just do not schema-qualify the chain.

-- HOW TO RUN: CrateDB needs a REFRESH between a write and a read, so INSERTs are followed by
--   explicit `REFRESH TABLE`. Each PART/control is independent; run each in its own schema.


-- =====================================================================================
-- PART 1 -- CONCRETE: the finding exactly as eqgen produced it.
-- 1a: BASE relation (a plain table) -- the same query SUCCEEDS.  Expected 7 rows.
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
SELECT t1.created_at AS expr_0_varchar FROM t AS t1 WHERE t1.created_at <= ALL (SELECT CAST(CAST(NULL AS TIMESTAMP WITHOUT TIME ZONE) AS TEXT) || (CASE WHEN True THEN $$𒀀$$ ELSE $$$$ END) AS expr_0_text FROM t AS t2 WHERE CAST(3 AS BIGINT) IS NULL) ORDER BY t1.id;;

-- =====================================================================================
-- 1b: EQUIVALENT relation -- row-identical to base t (8 identical rows, admissibility
--     verified). The chain is the predicate-split partitioning builder: an even-id view and
--     an odd/NULL-id table (via copy -> INDEX OFF copy -> FULLTEXT-index copy), UNION ALL'd
--     back together. Note the workload query ORDER BYs t1.id, which it does not select, and
--     never touches `name` at all -- ingredients 2 and 3.
-- Expected 7 rows, actual: ERROR "Index 1 out of bounds for length 1"
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
CREATE VIEW t__base_view_1 AS SELECT id, name, created_at FROM t__base WHERE MOD(id, 2) = 0;
CREATE TABLE t__base_table_1 (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t__base_table_1 (id, name, created_at) SELECT id, name, created_at FROM t__base;
CREATE TABLE t__base_table_2 (id BIGINT INDEX OFF, name TEXT INDEX OFF, created_at TEXT INDEX OFF) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t__base_table_2 (id, name, created_at) SELECT id, name, created_at FROM t__base_table_1;
CREATE VIEW t__base_view_2 AS SELECT id, name, created_at FROM t__base_table_2;
CREATE TABLE t__base_table_3 (id BIGINT, name TEXT, created_at TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t__base_table_3 (id, name, created_at) SELECT * FROM t__base_view_2 WHERE (MOD(id, 2) <> 0) OR (id IS NULL);
CREATE TABLE t__base_table_4 (id BIGINT, name TEXT, created_at TEXT, INDEX t__base_idx_2 USING FULLTEXT (name, created_at) WITH (analyzer='english')) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t__base_table_4 (id, name, created_at) SELECT id, name, created_at FROM t__base_table_3;
CREATE VIEW t__base_view_3 AS SELECT id, name, created_at FROM t__base_table_4;
CREATE VIEW t AS SELECT * FROM t__base_view_1 UNION ALL SELECT * FROM t__base_view_3;
SELECT t1.created_at AS expr_0_varchar FROM t AS t1 WHERE t1.created_at <= ALL (SELECT CAST(CAST(NULL AS TIMESTAMP WITHOUT TIME ZONE) AS TEXT) || (CASE WHEN True THEN $$𒀀$$ ELSE $$$$ END) AS expr_0_text FROM t AS t2 WHERE CAST(3 AS BIGINT) IS NULL) ORDER BY t1.id;;

-- =====================================================================================
-- PART 2 -- DISTILLED minimal repro. No subquery, no view, no eqgen vocabulary: one
--           3-column table, an inline UNION ALL, select c3, order by c1, never use c2.
-- 2a (PRIMARY, fails on every client): aliased select item.
-- Expected 4 rows ('a','a','z','z'), actual: ERROR "Index 1 out of bounds for length 1"
-- =====================================================================================
CREATE TABLE b (c1 BIGINT, c2 TEXT, c3 TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (1, 'n', 'a');
INSERT INTO b VALUES (2, 'm', 'z');
REFRESH TABLE b;
SELECT c3 AS q FROM (SELECT * FROM b UNION ALL SELECT * FROM b) x ORDER BY c1;

-- =====================================================================================
-- 2b: same, plain (unaliased) select item -- the face that prints the leaked value.
--     *** PG WIRE ONLY: over HTTP /_sql this correctly returns 4 rows. ***
-- Expected 4 rows ('a','a','z','z'), actual on PG wire: ERROR (column-count mismatch),
--                                    actual over HTTP: 4 rows, correct.
-- =====================================================================================
CREATE TABLE b (c1 BIGINT, c2 TEXT, c3 TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (1, 'n', 'a');
INSERT INTO b VALUES (2, 'm', 'z');
REFRESH TABLE b;
SELECT c3 FROM (SELECT * FROM b UNION ALL SELECT * FROM b) x ORDER BY c1;

-- =====================================================================================
-- PART 3 -- REGRESSION EVIDENCE. Both of these are CrateDB's own published artifacts for
--           the bug it closed as fixed in 5.10.1. Both still fail on 6.4.1 -- and both are
--           written UNALIASED, so on 6.4.1 they fail on the PG wire and RETURN CORRECT ROWS
--           over HTTP. Add `AS q` to either (3a', 3c) and the originally reported planner
--           exception comes back on every client. #17341 *reported* that planner exception,
--           so in 5.9.9/5.10.0 the unaliased form took the planner path too: PR #17365 moved
--           the unaliased form off it and left the aliased form on it.
-- 3a: the example printed verbatim in the 5.10.1 release notes as the FIXED case
--     (docs/appendices/release-notes/5.10.1.rst, added by PR #17365).
-- Expected 4 rows; actual: PG wire ERROR, HTTP 4 rows (correct)
-- =====================================================================================
CREATE TABLE users (id BIGINT, other_id BIGINT, name TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO users VALUES (1, 10, 'alice');
INSERT INTO users VALUES (2, 20, 'bob');
REFRESH TABLE users;
SELECT id FROM (
    SELECT id, other_id, name FROM users
    UNION ALL
    SELECT id, other_id, name FROM users
    ) u
ORDER BY name;

-- 3a': 3a with an output alias -> ERROR "Index 1 out of bounds for length 1" on EVERY client.
--      The release note's own "fixed" example, one token away from the original exception.
-- Expected 4 rows, actual: ERROR on both HTTP and PG wire.
SELECT id AS q FROM (
    SELECT id, other_id, name FROM users
    UNION ALL
    SELECT id, other_id, name FROM users
    ) u
ORDER BY name;

-- =====================================================================================
-- 3b: GH#17341's verbatim reproduction (reported against 5.9.9 / 5.10.0, closed 2025-02-06).
-- Expected 2 rows; actual: PG wire ERROR, HTTP 2 rows (correct) -- unaliased, see PART 3 note.
-- =====================================================================================
CREATE TABLE t1 ("id" INTEGER NOT NULL, "propertyName" TEXT NOT NULL, "valueDatetime" TIMESTAMP WITHOUT TIME ZONE) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
CREATE TABLE t2 ("id" INTEGER NOT NULL, "propertyName" TEXT NOT NULL, "valueDatetime" TIMESTAMP WITHOUT TIME ZONE) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t1 VALUES (1, 'test', NOW());
INSERT INTO t2 VALUES (1, 'test', NOW());
REFRESH TABLE t1;
REFRESH TABLE t2;
CREATE OR REPLACE VIEW v1 AS SELECT "id", "propertyName", "valueDatetime" FROM t1 UNION ALL SELECT "id", "propertyName", "valueDatetime" FROM t2;
SELECT "id" FROM v1 v ORDER BY v."valueDatetime" LIMIT 100;

-- =====================================================================================
-- 3c: 3b with an output alias -- reproduces GH#17341's ORIGINAL message and node shape,
--     i.e. the planner path that PR #17365 was supposed to fix. Fails on EVERY client
--     (HTTP and PG wire alike), which is why this is the form to file.
-- Expected 2 rows, actual: ERROR "Index 1 out of bounds for length 1: Eval[id AS q]"
-- =====================================================================================
CREATE TABLE t1 ("id" INTEGER NOT NULL, "propertyName" TEXT NOT NULL, "valueDatetime" TIMESTAMP WITHOUT TIME ZONE) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
CREATE TABLE t2 ("id" INTEGER NOT NULL, "propertyName" TEXT NOT NULL, "valueDatetime" TIMESTAMP WITHOUT TIME ZONE) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t1 VALUES (1, 'test', NOW());
INSERT INTO t2 VALUES (1, 'test', NOW());
REFRESH TABLE t1;
REFRESH TABLE t2;
CREATE OR REPLACE VIEW v1 AS SELECT "id", "propertyName", "valueDatetime" FROM t1 UNION ALL SELECT "id", "propertyName", "valueDatetime" FROM t2;
SELECT "id" AS q FROM v1 v ORDER BY v."valueDatetime" LIMIT 100;

-- =====================================================================================
-- PART 4 -- CONTROLS. Each changes exactly ONE thing from PART 2 and behaves correctly.
-- =====================================================================================

-- C1  the correct answer: branches project only the used columns, so nothing is pruned
--     above the union.  4 rows: a, a, z, z.
CREATE TABLE b (c1 BIGINT, c2 TEXT, c3 TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (1, 'n', 'a');
INSERT INTO b VALUES (2, 'm', 'z');
REFRESH TABLE b;
SELECT c3 FROM (SELECT c1, c3 FROM b UNION ALL SELECT c1, c3 FROM b) x ORDER BY c1;

-- C2  ORDER BY a SELECTED column -> clean.  4 rows.
CREATE TABLE b (c1 BIGINT, c2 TEXT, c3 TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (1, 'n', 'a');
INSERT INTO b VALUES (2, 'm', 'z');
REFRESH TABLE b;
SELECT c3 FROM (SELECT * FROM b UNION ALL SELECT * FROM b) x ORDER BY c3;

-- C3  no ORDER BY at all -> clean, even though c1 and c2 are both unused.  4 rows.
CREATE TABLE b (c1 BIGINT, c2 TEXT, c3 TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (1, 'n', 'a');
INSERT INTO b VALUES (2, 'm', 'z');
REFRESH TABLE b;
SELECT c3 FROM (SELECT * FROM b UNION ALL SELECT * FROM b) x;

-- C4  no UNUSED column (select c2 as well) -> clean.  4 rows.
CREATE TABLE b (c1 BIGINT, c2 TEXT, c3 TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (1, 'n', 'a');
INSERT INTO b VALUES (2, 'm', 'z');
REFRESH TABLE b;
SELECT c2, c3 FROM (SELECT * FROM b UNION ALL SELECT * FROM b) x ORDER BY c1;

-- C5  no UNUSED column (order by c1 AND c2) -> clean.  4 rows.
CREATE TABLE b (c1 BIGINT, c2 TEXT, c3 TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (1, 'n', 'a');
INSERT INTO b VALUES (2, 'm', 'z');
REFRESH TABLE b;
SELECT c3 FROM (SELECT * FROM b UNION ALL SELECT * FROM b) x ORDER BY c1, c2;

-- C6  UNION DISTINCT instead of UNION ALL -> clean.  2 rows.
CREATE TABLE b (c1 BIGINT, c2 TEXT, c3 TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (1, 'n', 'a');
INSERT INTO b VALUES (2, 'm', 'z');
REFRESH TABLE b;
SELECT c3 FROM (SELECT * FROM b UNION SELECT * FROM b) x ORDER BY c1;

-- C7  single-branch derived table (no union) -> clean.  2 rows.
CREATE TABLE b (c1 BIGINT, c2 TEXT, c3 TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (1, 'n', 'a');
INSERT INTO b VALUES (2, 'm', 'z');
REFRESH TABLE b;
SELECT c3 FROM (SELECT * FROM b) x ORDER BY c1;

-- C8  plain table, no union -> clean.  2 rows.
CREATE TABLE b (c1 BIGINT, c2 TEXT, c3 TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (1, 'n', 'a');
INSERT INTO b VALUES (2, 'm', 'z');
REFRESH TABLE b;
SELECT c3 FROM b ORDER BY c1;

-- C9  a VIEW instead of a derived table -> STILL FAILS. Not view-specific.
CREATE TABLE b (c1 BIGINT, c2 TEXT, c3 TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (1, 'n', 'a');
INSERT INTO b VALUES (2, 'm', 'z');
REFRESH TABLE b;
CREATE VIEW v AS SELECT * FROM b UNION ALL SELECT * FROM b;
SELECT c3 FROM v ORDER BY c1;

-- C10 EMPTY table -> clean (0 rows). The leak only bites once a row is serialized.
CREATE TABLE b (c1 BIGINT, c2 TEXT, c3 TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
SELECT c3 FROM (SELECT * FROM b UNION ALL SELECT * FROM b) x ORDER BY c1;

-- C11 wrapped in COUNT(*) so no row reaches the client -> clean, and the count is
--     correct (4).  So the plan computes the right rows; only the output row shape is wrong.
CREATE TABLE b (c1 BIGINT, c2 TEXT, c3 TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (1, 'n', 'a');
INSERT INTO b VALUES (2, 'm', 'z');
REFRESH TABLE b;
SELECT count(*) FROM (SELECT c3 FROM (SELECT * FROM b UNION ALL SELECT * FROM b) x ORDER BY c1) y;

-- C12 INSERT INTO ... SELECT of the failing shape -> SUCCEEDS with correct data.
--     No silent data corruption on the write path.  4 rows: a, a, z, z.
CREATE TABLE b (c1 BIGINT, c2 TEXT, c3 TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (1, 'n', 'a');
INSERT INTO b VALUES (2, 'm', 'z');
REFRESH TABLE b;
CREATE TABLE dst (c3 TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO dst (c3) SELECT c3 FROM (SELECT * FROM b UNION ALL SELECT * FROM b) x ORDER BY c1;
REFRESH TABLE dst;
SELECT c3 FROM dst ORDER BY c3;

-- C13 ORDER BY 1 (positional -> the selected column) -> clean.  4 rows.
CREATE TABLE b (c1 BIGINT, c2 TEXT, c3 TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (1, 'n', 'a');
INSERT INTO b VALUES (2, 'm', 'z');
REFRESH TABLE b;
SELECT c3 FROM (SELECT * FROM b UNION ALL SELECT * FROM b) x ORDER BY 1;

-- C14 2-column table (select c3, order c1, nothing unused) -> clean. Shows that a
--     pruned-away third column is genuinely required.  2 rows.
CREATE TABLE b (c1 BIGINT, c3 TEXT) CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO b VALUES (1, 'a');
REFRESH TABLE b;
SELECT c3 FROM (SELECT * FROM b UNION ALL SELECT * FROM b) x ORDER BY c1;
