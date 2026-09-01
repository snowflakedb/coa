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

-- MySQL 9.7.2 (docker mysql:9.7.2). A VIEW whose column is a genuine expression (not a bare column
-- reference) -- e.g. CAST(c_pk AS SIGNED), or c_pk + 0 -- queried with
-- `GROUP BY <that column> HAVING (<that column> AND COUNT(<that column>))` silently drops a group
-- that should pass the HAVING filter, even though each half of the AND, evaluated alone, correctly
-- returns true for that group.
--
-- sql_mode: STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES (not load-bearing).

CREATE TABLE t__base (c_pk BIGINT NOT NULL, c_int BIGINT);
INSERT INTO t__base VALUES (1, NULL);
INSERT INTO t__base VALUES (2, -7);

CREATE VIEW t AS SELECT CAST(c_pk AS SIGNED) AS c_pk, c_int FROM t__base;

SELECT t.c_pk, COUNT(t.c_pk) FROM t GROUP BY t.c_pk HAVING (t.c_pk AND COUNT(t.c_pk)) ORDER BY t.c_pk;
-- => (2, 1)                      -- WRONG: the c_pk=1 group is missing
-- expected (and what every control below gives): (1, 1), (2, 1)

-- Equally minimal alternative view body (arithmetic instead of CAST):
-- CREATE VIEW t AS SELECT (c_pk + 0) AS c_pk, c_int FROM t__base;   -- same wrong result

-- Control 1: materialize the identical SELECT as a TABLE instead of a VIEW -- correct.
-- CREATE TABLE t AS SELECT CAST(c_pk AS SIGNED) AS c_pk, c_int FROM t__base;
-- SELECT t.c_pk, COUNT(t.c_pk) FROM t GROUP BY t.c_pk HAVING (t.c_pk AND COUNT(t.c_pk)) ORDER BY t.c_pk;
-- => (1, 1), (2, 1)   correct

-- Control 2: a view column that is a BARE reference (no expression) -- correct, even though it is
-- still a view:
-- CREATE VIEW t AS SELECT c_pk AS c_pk, c_int FROM t__base;   -- identity alias: correct
-- CREATE VIEW t AS SELECT (+c_pk) AS c_pk, c_int FROM t__base; -- unary plus: correct

-- Control 3: keep the CAST view, but change only the HAVING clause -- every one of these is
-- correct over the SAME (broken-with-AND-and-COUNT) view:
--   HAVING (t.c_pk AND 1)                    -- constant instead of COUNT(): (1), (2)
--   HAVING (COUNT(t.c_pk) AND COUNT(t.c_pk))  -- COUNT on both sides: (1,1), (2,1)
--   HAVING (t.c_pk AND t.c_pk)                -- t.c_pk on both sides: (1), (2)
--   HAVING TRUE                                -- no filter: (1), (2)
--   HAVING t.c_pk                              -- t.c_pk alone: (1), (2)
-- Only the specific pairing "a view-computed GROUP BY column AND COUNT() of that same column,
-- both in HAVING" drops the group.
