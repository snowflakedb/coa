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

-- CrateDB 6.4.1 (built 45bfa80): a UNION ALL branch emptied by a filter, whose body is an
-- equi-join, corrupts the output column-slot mapping for the branch that DOES carry rows.
-- Two symptoms from the SAME view: a reordered full-width projection transposes values
-- (Symptom A), and an ORDER BY on a column outside the projection loses a row entirely
-- (Symptom B). See bug_report.md "Unification" for how these turned out to be one root cause.
--
-- Run each part against a fresh database.

------------------------------------------------------------------------------
-- Part 1 — SETUP, shared by everything below
------------------------------------------------------------------------------
CREATE TABLE a (c_pk BIGINT, id BIGINT, name TEXT, created_at TEXT)
  CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO a VALUES (1, 10, 'x', 'p');
INSERT INTO a VALUES (2, 20, 'y', 'q');
REFRESH TABLE a;

CREATE VIEW v AS
  SELECT * FROM (SELECT l.c_pk, l.id, l.name, l.created_at FROM a l JOIN a r ON l.c_pk = r.c_pk) j
   WHERE 1 = 0
  UNION ALL
  SELECT * FROM a;

-- Baseline: the data itself.
-- Expected: (1,10,'x','p'), (2,20,'y','q')     -- actual: correct
SELECT * FROM a;

------------------------------------------------------------------------------
-- Part 2 — SYMPTOM A: reordered full-width projection, no ORDER BY
------------------------------------------------------------------------------
-- Expected: (1,10,'p','x'), (2,20,'q','y')
-- Actual:   (1,10,'x','p'), (2,20,'y','q')     -- positions 3/4 (created_at, name) transposed
SELECT c_pk, id, created_at, name FROM v;

------------------------------------------------------------------------------
-- Part 3 — SYMPTOM B: ORDER BY on a column outside the projection, SAME view
------------------------------------------------------------------------------
-- Expected: ('x','p'), ('y','q')
-- Actual:   ('x','p'), (NULL,NULL)             -- second row's data is gone
SELECT name, created_at FROM v ORDER BY c_pk;

------------------------------------------------------------------------------
-- Part 4 — CONTROLS
------------------------------------------------------------------------------
-- (a) Table-order projection over v -> CORRECT. UNION ALL alone is not sufficient.
-- Expected/actual: (1,10,'x','p'), (2,20,'y','q')
SELECT c_pk, id, name, created_at FROM v;

-- (b) Subset reorder over v -> CORRECT. The projection must be full-width for Symptom A.
-- Expected/actual: ('p','x'), ('q','y')
SELECT created_at, name FROM v;

-- (c) CROSS JOIN instead of an equi-join in the empty branch -> CORRECT.
--     The ON condition is necessary, not merely the presence of a second relation.
CREATE VIEW v_cross AS
  SELECT * FROM (SELECT l.c_pk, l.id, l.name, l.created_at FROM a l, a r) j WHERE 1 = 0
  UNION ALL
  SELECT * FROM a;
-- Expected/actual: (1,10,'p','x'), (2,20,'q','y')
SELECT c_pk, id, created_at, name FROM v_cross;

-- (d) No empty branch at all, same reordered projection -> CORRECT.
CREATE VIEW v_plain AS SELECT * FROM a UNION ALL SELECT * FROM a WHERE 1 = 0;
-- Expected/actual: (1,10,'p','x'), (2,20,'q','y')
SELECT c_pk, id, created_at, name FROM v_plain;

-- (e) ORDER BY the projected column itself, same v -> CORRECT (Symptom B needs an
--     excluded sort key).
-- Expected/actual: ('x','p'), ('y','q')
SELECT name, created_at FROM v ORDER BY name;

-- (f) Dropping ORDER BY entirely on the same query as Symptom B -> CORRECT.
-- Expected/actual: ('x','p'), ('y','q')   (order unspecified without ORDER BY, but both rows
-- present and correct)
SELECT name, created_at FROM v;

------------------------------------------------------------------------------
-- Part 5 — JOIN PREDICATE IS IRRELEVANT: all four columns tested as the ON condition
------------------------------------------------------------------------------
-- Each of these reproduces Symptom A identically: (1,10,'x','p'), (2,20,'y','q')
-- CREATE VIEW v_pk  AS SELECT * FROM (SELECT l.c_pk,l.id,l.name,l.created_at FROM a l JOIN a r ON l.c_pk=r.c_pk) j WHERE 1=0 UNION ALL SELECT * FROM a;
-- CREATE VIEW v_id  AS SELECT * FROM (SELECT l.c_pk,l.id,l.name,l.created_at FROM a l JOIN a r ON l.id=r.id) j WHERE 1=0 UNION ALL SELECT * FROM a;
-- CREATE VIEW v_nm  AS SELECT * FROM (SELECT l.c_pk,l.id,l.name,l.created_at FROM a l JOIN a r ON l.name=r.name) j WHERE 1=0 UNION ALL SELECT * FROM a;
-- CREATE VIEW v_ca  AS SELECT * FROM (SELECT l.c_pk,l.id,l.name,l.created_at FROM a l JOIN a r ON l.created_at=r.created_at) j WHERE 1=0 UNION ALL SELECT * FROM a;
-- SELECT c_pk, id, created_at, name FROM v_*;  -- all four: (1,10,'x','p'), (2,20,'y','q')

------------------------------------------------------------------------------
-- Part 6 — UNIFICATION CHECK: the "reordered full-width projection" finding's own view,
-- under the "ORDER BY" finding's query shape, reproduces the ORDER BY finding's symptom.
-- (Confirms both findings are one root cause; see bug_report.md "Unification".)
------------------------------------------------------------------------------
-- Using the same `v` from Part 1 (built for Symptom A):
-- Expected: ('x','p'), ('y','q')   -- actual: ('x','p'), (NULL,NULL)   -- Symptom B, same view.
SELECT name, created_at FROM v ORDER BY c_pk;
