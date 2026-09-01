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

-- Dolt: `WHERE <col> <= <expr>` and `>=` return rows where <col> IS NULL
-- (ValueRow fast path collapses a NULL comparison to "equal", and <=/>= treat that as TRUE)
--
-- Engine      : dolt 2.2.3  (server reports VERSION() = 8.0.31, its MySQL compatibility string)
-- Access path : ONLY via `dolt sql-server`. The in-process `dolt sql` CLI is correct -- see the
--               `cli-vs-server` note at the bottom.
-- Session     : all defaults; sql_mode is irrelevant (verified against '' and the fuzzer's
--               STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES,PIPES_AS_CONCAT)
-- Findings    : all 70 across both dolt gate runs --
--               56 in dialect_gates_20260809/dolt_20b/dolt_20260809-004415/
--               14 in dialect_gates_20260809/dolt_20/dolt_20260809-003653/
--
-- Every block below is delimited by `-- >>> BLOCK: <name>  expect=<wrong|ok>` and runs in its own
-- fresh database. Each block was run and its row count checked against the stated expectation, so no
-- claim here rests on reasoning alone.
--
--   expect=wrong  -> the engine returns MORE rows than SQL three-valued logic allows
--   expect=ok     -> the engine returns the correct rows


-- >>> BLOCK: distilled  expect=wrong  rows=1  correct=0
-- The whole finding, distilled. One row, one NULL, one predicate.
-- `b` IS NULL, so `b <= 1` is NULL, not TRUE -- the row must not be returned.
CREATE TABLE t (a BIGINT NOT NULL, b BIGINT);
INSERT INTO t VALUES (1, NULL);
SELECT * FROM t WHERE b <= 1;


-- >>> BLOCK: distilled-gte  expect=wrong  rows=1  correct=0
-- `>=` is the same defect.
CREATE TABLE t (a BIGINT NOT NULL, b BIGINT);
INSERT INTO t VALUES (1, NULL);
SELECT * FROM t WHERE b >= 1;


-- >>> BLOCK: concrete-as-emitted  expect=wrong  rows=8  correct=7
-- The finding as eqgen emitted it (mismatch_round3_1.sql), base side, verbatim: the base database
-- seeds `t` and forks it into t0/t1/t2, then runs the workload query. `NOT (c_int > c_pk)` is the
-- planner's spelling of `c_int <= c_pk`. Row c_pk=1 has c_int IS NULL, so the predicate is NULL and
-- the row must not appear -- expected 7 rows, Dolt returns 8.
-- (The fork DDL below is re-added by hand: eqgen's repro writer omits it from the base block.)
CREATE TABLE t (c_pk BIGINT NOT NULL, c_int BIGINT, c_big BIGINT, c_dec DECIMAL(10, 2), c_dbl DOUBLE, c_txt VARCHAR(255), c_chr VARCHAR(255), c_date DATE, c_ts DATETIME(6));
INSERT INTO t VALUES (1, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);
INSERT INTO t VALUES (2, 2, -1, -5.5, -1.5, NULL, NULL, NULL, NULL);
INSERT INTO t VALUES (3, 1, NULL, 12.34, 0.0, '', 'abc', '2030-06-01', NULL);
INSERT INTO t VALUES (4, 0, 2, NULL, 1.5, 'trailing ', 'trailing ', '1999-12-31', '1999-12-31 23:59:59');
INSERT INTO t VALUES (5, -7, 2, -5.5, NULL, 'o''brien', '', '2030-06-01', NULL);
INSERT INTO t VALUES (6, 0, NULL, 999.99, 1000.125, NULL, 'trailing ', '2024-01-15', '1999-12-31 23:59:59');
INSERT INTO t VALUES (7, 2, -1, -5.5, NULL, 'abc', 'abc', '2024-01-15', '2024-01-15 12:34:56');
INSERT INTO t VALUES (8, 2, -1, -5.5, -1.5, NULL, NULL, NULL, NULL);
CREATE TABLE t0 AS SELECT * FROM t;
CREATE TABLE t1 AS SELECT * FROM t;
CREATE TABLE t2 AS SELECT * FROM t;
SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_date, c_ts FROM t1 WHERE NOT (c_int > c_pk);


-- ============================================================================================
-- Operator coverage. One row whose `b` IS NULL, so EVERY predicate below is NULL and the correct
-- answer is always 0 rows. Exactly the four operators that implement the ValueRow fast path are
-- involved, and exactly the two whose fast path defaults to TRUE are wrong.
-- ============================================================================================

-- >>> BLOCK: op-gt  expect=ok  rows=0  correct=0
CREATE TABLE t (a BIGINT NOT NULL, b BIGINT);
INSERT INTO t VALUES (1, NULL);
SELECT * FROM t WHERE b > 1;

-- >>> BLOCK: op-lt  expect=ok  rows=0  correct=0
CREATE TABLE t (a BIGINT NOT NULL, b BIGINT);
INSERT INTO t VALUES (1, NULL);
SELECT * FROM t WHERE b < 1;

-- >>> BLOCK: op-eq  expect=ok  rows=0  correct=0
-- `=` does not implement EvalValue, so it falls back to the correct row path.
CREATE TABLE t (a BIGINT NOT NULL, b BIGINT);
INSERT INTO t VALUES (1, NULL);
SELECT * FROM t WHERE b = 1;

-- >>> BLOCK: op-ne  expect=ok  rows=0  correct=0
CREATE TABLE t (a BIGINT NOT NULL, b BIGINT);
INSERT INTO t VALUES (1, NULL);
SELECT * FROM t WHERE b <> 1;

-- >>> BLOCK: op-not-gt  expect=wrong  rows=1  correct=0
-- `NOT (b > 1)` is normalised to `b <= 1` -> wrong.
CREATE TABLE t (a BIGINT NOT NULL, b BIGINT);
INSERT INTO t VALUES (1, NULL);
SELECT * FROM t WHERE NOT (b > 1);

-- >>> BLOCK: op-not-gte  expect=ok  rows=0  correct=0
-- `NOT (b >= 1)` normalises to `b < 1`, whose fast path defaults to FALSE -> correct.
-- The pair with `op-not-gt` is the sharpest statement of the asymmetry.
CREATE TABLE t (a BIGINT NOT NULL, b BIGINT);
INSERT INTO t VALUES (1, NULL);
SELECT * FROM t WHERE NOT (b >= 1);

-- >>> BLOCK: op-between  expect=ok  rows=0  correct=0
CREATE TABLE t (a BIGINT NOT NULL, b BIGINT);
INSERT INTO t VALUES (1, NULL);
SELECT * FROM t WHERE b BETWEEN 0 AND 2;

-- >>> BLOCK: op-column-column  expect=wrong  rows=1  correct=0
-- Column-vs-column, as in the finding, not just column-vs-literal.
CREATE TABLE t (a BIGINT NOT NULL, b BIGINT);
INSERT INTO t VALUES (1, NULL);
SELECT * FROM t WHERE b <= a;


-- ============================================================================================
-- Controls. Each keeps the same table, data and predicate and changes ONE other thing, so each
-- names something the bug requires.
-- ============================================================================================

-- >>> BLOCK: control-filter-is-applied  expect=wrong  rows=2  correct=1
-- The filter is NOT simply dropped: row (3,5) has 5 > 3, so `NOT (b > a)` is FALSE and it is
-- correctly excluded. Only the NULL row leaks. Expected {(2,2)}; Dolt returns {(2,2),(1,NULL)}.
CREATE TABLE t (a BIGINT NOT NULL, b BIGINT);
INSERT INTO t VALUES (1, NULL), (2, 2), (3, 5);
SELECT * FROM t WHERE NOT (b > a);

-- >>> BLOCK: control-index-on-b  expect=ok  rows=0  correct=0
-- An index on the filtered column is enough to avoid it: the plan becomes
-- `IndexedTableAccess(ti) index: [ti.b] filters: [{(NULL, 1]}]`, whose NULL-exclusive lower bound
-- is correct. No FilterIter is involved.
CREATE TABLE ti (a BIGINT NOT NULL PRIMARY KEY, b BIGINT, KEY(b));
INSERT INTO ti VALUES (1, NULL);
SELECT * FROM ti WHERE b <= 1;

-- >>> BLOCK: control-pk-only-still-wrong  expect=wrong  rows=1  correct=0
-- A primary key alone does not help -- only an index on the *filtered* column does.
CREATE TABLE tp (a BIGINT NOT NULL PRIMARY KEY, b BIGINT);
INSERT INTO tp VALUES (1, NULL);
SELECT * FROM tp WHERE b <= 1;

-- >>> BLOCK: control-projection-reordered  expect=ok  rows=0  correct=0
-- Reordering the projection forces a Project node above the Filter, which switches execution from
-- the ValueRow fast path to the row path -- and the row path is correct.
CREATE TABLE t (a BIGINT NOT NULL, b BIGINT);
INSERT INTO t VALUES (1, NULL);
SELECT b, a FROM t WHERE b <= 1;

-- >>> BLOCK: control-projection-subset  expect=ok  rows=0  correct=0
-- Projecting only a column the filter does not use also forces a Project (the scan must still read
-- `b`), so this is correct too.
CREATE TABLE t (a BIGINT NOT NULL, b BIGINT);
INSERT INTO t VALUES (1, NULL);
SELECT a FROM t WHERE b <= 1;

-- >>> BLOCK: control-order-by  expect=ok  rows=0  correct=0
-- Any node above the Filter has the same effect -- here a Sort.
CREATE TABLE t (a BIGINT NOT NULL, b BIGINT);
INSERT INTO t VALUES (1, NULL);
SELECT * FROM t WHERE b <= 1 ORDER BY a;

-- >>> BLOCK: control-limit  expect=ok  rows=0  correct=0
CREATE TABLE t (a BIGINT NOT NULL, b BIGINT);
INSERT INTO t VALUES (1, NULL);
SELECT * FROM t WHERE b <= 1 LIMIT 5;

-- >>> BLOCK: control-count-wrapped  expect=ok  rows=1  correct=1
-- The same filter under an AGGREGATION gives the CORRECT answer (0), which is how all 70 findings
-- were confirmed to share this root cause. Returns one row holding the count 0.
CREATE TABLE t (a BIGINT NOT NULL, b BIGINT);
INSERT INTO t VALUES (1, NULL);
SELECT COUNT(*) FROM (SELECT * FROM t WHERE b <= 1) AS wrap;

-- >>> BLOCK: trap-derived-table-flattens  expect=wrong  rows=1  correct=0
-- The obvious mask does NOT work, and this is a trap worth knowing: a plain `SELECT *` over a derived
-- table is flattened by the optimizer, so the plan is still a bare Filter over Table and the wrong row
-- still comes back. Any "wrap it in a subquery" check must add a node that cannot be flattened away
-- (an aggregation, a Sort, a LIMIT) or it silently proves nothing.
CREATE TABLE t (a BIGINT NOT NULL, b BIGINT);
INSERT INTO t VALUES (1, NULL);
SELECT * FROM (SELECT * FROM t WHERE b <= 1) AS wrap;

-- >>> BLOCK: control-is-true  expect=ok  rows=0  correct=0
-- Making the three-valued result explicit is correct, so the comparison itself is not what is
-- broken -- it is the filter's use of it on the fast path.
CREATE TABLE t (a BIGINT NOT NULL, b BIGINT);
INSERT INTO t VALUES (1, NULL);
SELECT * FROM t WHERE (b <= 1) IS TRUE;

-- >>> BLOCK: control-and-conjunct  expect=ok  rows=0  correct=0
-- Adding a second conjunct changes the expression shape and is correct.
CREATE TABLE t (a BIGINT NOT NULL, b BIGINT);
INSERT INTO t VALUES (1, NULL);
SELECT * FROM t WHERE b <= 1 AND a = 1;

-- >>> BLOCK: control-arithmetic  expect=ok  rows=0  correct=0
-- Wrapping the column in arithmetic is correct.
CREATE TABLE t (a BIGINT NOT NULL, b BIGINT);
INSERT INTO t VALUES (1, NULL);
SELECT * FROM t WHERE b + 1 <= 1;

-- >>> BLOCK: control-delete-unaffected  expect=ok  rows=1  correct=1
-- DELETE is NOT affected: it takes the row path. The row survives, so no data is lost by this bug.
-- Returns one row holding the surviving count 1.
CREATE TABLE t (a BIGINT NOT NULL, b BIGINT);
INSERT INTO t VALUES (1, NULL);
DELETE FROM t WHERE b <= 1;
SELECT COUNT(*) FROM t;

-- >>> BLOCK: control-update-unaffected  expect=ok  rows=1  correct=1
-- UPDATE is likewise unaffected: `a` stays 1, so the row is not silently modified.
CREATE TABLE t (a BIGINT NOT NULL, b BIGINT);
INSERT INTO t VALUES (1, NULL);
UPDATE t SET a = 99 WHERE b <= 1;
SELECT a FROM t;


-- ============================================================================================
-- cli-vs-server
-- ============================================================================================
-- The bug is only observable through the sql-server. Same binary, same data directory, same
-- database, same query:
--
--   $ dolt sql-server --host 127.0.0.1 --port 13366 --data-dir /tmp/dsrv &
--   $ mariadb -h127.0.0.1 -P13366 -uroot --skip-ssl d -e "SELECT * FROM t WHERE NOT (b > a);"
--   +---+------+
--   | a | b    |
--   +---+------+
--   | 2 |    2 |
--   | 1 | NULL |   <-- WRONG
--   | 4 |    0 |
--   +---+------+
--
--   $ dolt sql -q "USE d; SELECT * FROM t WHERE NOT (b > a);"     # same data dir, server stopped
--   +---+---+
--   | a | b |
--   +---+---+
--   | 2 | 2 |
--   | 4 | 0 |                                                      <-- correct
--   +---+---+
--
-- Reproduced through two independent clients (pymysql and the mariadb CLI), so it is a server-side
-- result, not a client decoding artefact.
