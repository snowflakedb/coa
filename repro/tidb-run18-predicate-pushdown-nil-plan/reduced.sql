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

-- TiDB planner panic: nil plan from predicate_push_down, unmasked as a nil-pointer dereference
--
--   ERROR 1105 (HY000): runtime error: invalid memory address or nil pointer dereference
--
-- Build: v9.0.0-beta.2.pre-2051-g3bea8196a5 @3bea8196 (master, unistore, assertions off)
--
-- Minimality, each established by removing exactly one ingredient and watching it stop:
--   * table is EMPTY -- no rows needed
--   * bare EXPLAIN is enough -- this is planning, not execution
--   * no view needed: the inline derived table below reproduces it identically. (The fuzzer found it
--     through a 3-view split-rejoin chain; the views were incidental.)
--   * the LEFT OUTER JOIN *is* required -- a plain `SELECT id, id AS name FROM b` source plans fine
--   * the `IS NULL` on the join's null-padded side is required
--   * the `GROUP BY` including that same column is required
--   * the 3-way CROSS JOIN is required -- a 2-way one plans fine
--   * `x = ALL (subquery)` as the IN's left operand is required -- `x = 1` plans fine, and so does
--     dropping the ALL and keeping everything else
--
-- Attribution: `admin reload opt_rule_blacklist` with 'predicate_push_down' blacklisted makes the
-- panic disappear and the query plan cleanly. Blacklisting decorrelate / correlate /
-- aggregation_push_down does not. So predicate pushdown is the rule that fails.

DROP DATABASE IF EXISTS repro;
CREATE DATABASE repro;
USE repro;

CREATE TABLE b (id BIGINT, k BIGINT);

-- A LEFT OUTER JOIN relation, referenced five times. `name` is the null-padded side.
-- (SELECT l.id AS id, r.name AS name FROM b l LEFT OUTER JOIN (SELECT id AS name, k FROM b) r ON l.k = r.k)

EXPLAIN
SELECT t1.id
FROM (SELECT l.id AS id, r.name AS name FROM b l LEFT OUTER JOIN (SELECT id AS name, k FROM b) r ON l.k = r.k) AS t1
WHERE (t1.id = ALL (
          SELECT t2.id
          FROM (SELECT l.id AS id, r.name AS name FROM b l LEFT OUTER JOIN (SELECT id AS name, k FROM b) r ON l.k = r.k) AS t2
      ))
      IN (
          SELECT t5.id
          FROM (SELECT l.id AS id, r.name AS name FROM b l LEFT OUTER JOIN (SELECT id AS name, k FROM b) r ON l.k = r.k) AS t3
          CROSS JOIN (SELECT l.id AS id, r.name AS name FROM b l LEFT OUTER JOIN (SELECT id AS name, k FROM b) r ON l.k = r.k) AS t4
          CROSS JOIN (SELECT l.id AS id, r.name AS name FROM b l LEFT OUTER JOIN (SELECT id AS name, k FROM b) r ON l.k = r.k) AS t5
          WHERE t4.name IS NULL
          GROUP BY t5.id, t4.name
      );

-- Expected: a plan.
-- Actual:   ERROR 1105 (HY000): runtime error: invalid memory address or nil pointer dereference

-- ---------------------------------------------------------------------------
-- Confirming the rule, and that the panic is a *mask* over it:
--
--   INSERT INTO mysql.opt_rule_blacklist VALUES ('predicate_push_down');
--   admin reload opt_rule_blacklist;
--   -- re-run the EXPLAIN above: it now plans successfully
--   DELETE FROM mysql.opt_rule_blacklist;
--   admin reload opt_rule_blacklist;
--   -- re-run: panics again
