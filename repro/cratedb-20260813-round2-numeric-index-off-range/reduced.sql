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

-- CrateDB: range predicates on a NUMERIC/DECIMAL column declared `INDEX OFF` return zero rows
--
-- Engine   : CrateDB 6.4.1 (built 45bfa80) and 6.4.2, docker crate:6.4.x, PostgreSQL wire
-- Found by : eqgen differential oracle (row-identical equivalent whose exposed table is INDEX OFF)
-- Symptom  : `<, >, <=, >=, BETWEEN` on a NUMERIC column with INDEX OFF match NOTHING, although the
--            same rows are plainly present on a full scan and `=`, `<>`, `IS NULL` all work.
--
-- The load-bearing ingredient is exactly one column attribute: `INDEX OFF` on a NUMERIC column.
-- Same values in a plain column (index on) filter correctly; same INDEX OFF on BIGINT / DOUBLE /
-- TEXT / TIMESTAMP filter correctly. Only NUMERIC + INDEX OFF + a range operator is wrong.

-- ============================================================================
-- (A) CONCRETE construction, as the eqgen builder (CrateDbIndexOffBuilder) emits it.
--     The exposed relation `t` is a view over a table whose every column is INDEX OFF.
-- ============================================================================
DROP TABLE IF EXISTS c_base;
CREATE TABLE c_base (c_pk BIGINT, c_dec NUMERIC(10, 2))
    CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO c_base (c_pk, c_dec) VALUES (2, -5.50), (3, 12.34), (4, 0.00), (5, -0.01);
REFRESH TABLE c_base;

DROP TABLE IF EXISTS c_idxoff;
CREATE TABLE c_idxoff (c_pk BIGINT INDEX OFF, c_dec NUMERIC(10, 2) INDEX OFF)
    CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO c_idxoff (c_pk, c_dec) SELECT c_pk, c_dec FROM c_base;
REFRESH TABLE c_idxoff;
CREATE VIEW c_view AS SELECT c_pk, c_dec FROM c_idxoff;

-- full scan agrees with the base: the -5.50 / 12.34 rows ARE there
SELECT c_pk, c_dec FROM c_view ORDER BY c_pk;
-- Expected 4 rows: (2,-5.50) (3,12.34) (4,0.00) (5,-0.01)

SELECT c_pk FROM c_view WHERE c_dec < 0.00 ORDER BY c_pk;
-- Expected [2, 5]   ACTUAL []   <<< WRONG
SELECT c_pk FROM c_view WHERE c_dec >= 12.34 ORDER BY c_pk;
-- Expected [3]      ACTUAL []   <<< WRONG

-- ============================================================================
-- (B) DISTILLED minimal repro — no view, no second table, one column.
-- ============================================================================
DROP TABLE IF EXISTS t;
CREATE TABLE t (c_pk BIGINT, c_dec NUMERIC(10, 2) INDEX OFF)
    CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t (c_pk, c_dec) VALUES (2, -5.50), (3, 12.34), (4, 0.00);
REFRESH TABLE t;

SELECT c_pk FROM t WHERE c_dec < 0.00;      -- Expected [2]     ACTUAL []   <<< WRONG
SELECT c_pk FROM t WHERE c_dec > 0.00;      -- Expected [3]     ACTUAL []   <<< WRONG
SELECT c_pk FROM t WHERE c_dec >= 12.34;    -- Expected [3]     ACTUAL []   <<< WRONG
SELECT c_pk FROM t WHERE c_dec <= 0.00;     -- Expected [2, 4]  ACTUAL []   <<< WRONG (0.00 also missing)
SELECT c_pk FROM t WHERE c_dec BETWEEN 0 AND 100;  -- Expected [3, 4]  ACTUAL []   <<< WRONG

-- ============================================================================
-- (C) CONTROLS — each swaps exactly one ingredient and behaves CORRECTLY.
-- ============================================================================
-- C1. Same column, same INDEX OFF, non-range operators: correct.
SELECT c_pk FROM t WHERE c_dec = 12.34;     -- [3]     correct
SELECT c_pk FROM t WHERE c_dec <> 12.34;    -- [2, 4]  correct
SELECT c_pk FROM t WHERE c_dec IS NULL;     -- []      correct

-- C2. Same NUMERIC values and range operator, but WITHOUT INDEX OFF: correct.
DROP TABLE IF EXISTS t_on;
CREATE TABLE t_on (c_pk BIGINT, c_dec NUMERIC(10, 2))
    CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t_on (c_pk, c_dec) VALUES (2, -5.50), (3, 12.34), (4, 0.00);
REFRESH TABLE t_on;
SELECT c_pk FROM t_on WHERE c_dec < 0.00;   -- [2]  correct

-- C3. Same INDEX OFF and range operator, but a BIGINT / DOUBLE / TEXT / TIMESTAMP column: correct.
DROP TABLE IF EXISTS t_types;
CREATE TABLE t_types (c_pk BIGINT, c_int BIGINT INDEX OFF, c_dbl DOUBLE PRECISION INDEX OFF, c_txt TEXT INDEX OFF)
    CLUSTERED INTO 1 SHARDS WITH (number_of_replicas = 0);
INSERT INTO t_types (c_pk, c_int, c_dbl, c_txt) VALUES (2, -7, -1.5, 'abc'), (3, 42, 1000.125, 'Zed');
REFRESH TABLE t_types;
SELECT c_pk FROM t_types WHERE c_int < 0;    -- [2]  correct
SELECT c_pk FROM t_types WHERE c_dbl < 0.0;  -- [2]  correct
SELECT c_pk FROM t_types WHERE c_txt < 'm';  -- [2]  correct
