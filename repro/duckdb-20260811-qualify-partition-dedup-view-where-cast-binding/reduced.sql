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

-- DuckDB: an outer QUALIFY over a view whose OWN body already filters via
-- QUALIFY ROW_NUMBER() OVER (PARTITION BY ...) = 1 throws an INTERNAL column-binding assertion
-- as soon as the outer WHERE clause contains ANY expression needing an implicit CAST -- including
-- the most ordinary ones (a bare non-boolean column, ORDER BY-key comparisons across differently
-- typed columns, a scalar function call). SET disabled_optimizers='top_n_window_elimination' makes
-- every case below succeed, so the responsible pass is the same one bug
-- repro/duckdb-20260811-qualify-topn-elimination-argminmax-n-cap names -- but this is a different
-- code path and symptom (INTERNAL assertion vs a guarded InvalidInputException).
--
-- Engine: DuckDB CLI v2.0.0-alpha37464 (ea53ecdca1) -- artifacts.duckdb.org/latest, tip of main.

-- ===========================================================================================
-- 1. Concrete form, close to how eqgen actually built it (KeyQualifyDedupReduceBuilder's
--    per-key dedup view, read by a workload query carrying the new
--    DuckDBRowNumberBoundQualifyBuilder-style outer QUALIFY plus an ordinary WHERE clause).
-- ===========================================================================================
CREATE TABLE t__base (c_pk BIGINT NOT NULL, c_int BIGINT, c_big DECIMAL(38, 0), c_dec DECIMAL(10, 2),
                       c_dbl DOUBLE, c_txt VARCHAR, c_chr VARCHAR, c_date DATE, c_ts TIMESTAMP);
INSERT INTO t__base VALUES (1, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);
INSERT INTO t__base VALUES (2, NULL, -7, NULL, 'Infinity'::DOUBLE, 'a', 'abc', '2030-06-01', '2024-01-15 12:34:56');
INSERT INTO t__base VALUES (3, 0, NULL, NULL, '-Infinity'::DOUBLE, 'a', 'a', '2030-06-01', NULL);

CREATE TABLE t__base_table_8 AS
SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts,
       ROW_NUMBER() OVER (ORDER BY c_pk) AS eq_key_1
FROM t__base;
CREATE VIEW t AS
SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts
FROM t__base_table_8
QUALIFY (ROW_NUMBER() OVER (PARTITION BY eq_key_1 ORDER BY eq_key_1)) = 1;

SELECT * FROM t WHERE (CASE t.c_pk WHEN t.c_big THEN false END) QUALIFY ROW_NUMBER() OVER (ORDER BY c_pk) <= 1;
-- Expected: 0 rows (as on the base table -- t holds identical rows by construction).
-- Actual:   INTERNAL Error: Failed to bind column reference ""#[0.1]"" [0.1] (bindings: {#[N.0], #[N.1]})

-- ===========================================================================================
-- 2. Distilled minimal repro -- single type, two columns, one statement past the base table,
--    and an EXPLICIT cast in the WHERE clause (::BOOLEAN on a BIGINT column) so the trigger is
--    visible in the SQL text itself, rather than relying on WHERE's implicit non-boolean-to-
--    BOOLEAN coercion. No cross-type table columns needed at all.
-- ===========================================================================================
CREATE TABLE t2__base (c_pk BIGINT NOT NULL, c_int BIGINT);
INSERT INTO t2__base VALUES (1, 5), (2, -7), (3, 0);

CREATE TABLE t2_keyed AS SELECT c_pk, c_int, ROW_NUMBER() OVER (ORDER BY c_pk) AS eq_key_1 FROM t2__base;
CREATE VIEW t2 AS
SELECT c_pk, c_int FROM t2_keyed QUALIFY (ROW_NUMBER() OVER (PARTITION BY eq_key_1 ORDER BY eq_key_1)) = 1;

SELECT * FROM t2 WHERE c_int::BOOLEAN QUALIFY ROW_NUMBER() OVER (ORDER BY c_pk) <= 1;
-- Expected: 1 row (c_pk=1, c_int=5 -- the only truthy, nonzero, non-null c_int).
-- Actual:   INTERNAL Error: Failed to bind column reference ""#[0.1]"" [0.1] (bindings: {#[18.0]})

-- Control A0: the cast doesn't have to be written explicitly -- WHERE's own implicit non-boolean
-- to BOOLEAN coercion reaches the same CAST node and reproduces identically.
SELECT * FROM t2 WHERE c_int QUALIFY ROW_NUMBER() OVER (ORDER BY c_pk) <= 1;
-- Expected: 1 row. Actual: same INTERNAL Error, byte-identical.

-- ===========================================================================================
-- Controls -- one ingredient changed per control, everything else held fixed on the t2 shape.
-- ===========================================================================================

-- Control A: the SAME query against a plain table holding identical rows (no view, no inner
-- QUALIFY) is exactly the harness's own "base" side, and it is clean -- establishes admissibility:
-- the divergence is one-sided (equivalent throws, base doesn't), not a rows mismatch.
CREATE TABLE t3 (c_pk BIGINT NOT NULL, c_int BIGINT);
INSERT INTO t3 VALUES (1, 5), (2, -7), (3, 0);
SELECT * FROM t3 WHERE c_int::BOOLEAN QUALIFY ROW_NUMBER() OVER (ORDER BY c_pk) <= 1;
-- Expected/Actual: 1 row (c_pk=1, c_int=5). Clean.

-- Control B: drop the outer QUALIFY, keep the WHERE and the inner dedup view -- clean.
CREATE TABLE t4__base (c_pk BIGINT NOT NULL, c_int BIGINT);
INSERT INTO t4__base VALUES (1, 5), (2, -7), (3, 0);
CREATE TABLE t4_keyed AS SELECT c_pk, c_int, ROW_NUMBER() OVER (ORDER BY c_pk) AS eq_key_1 FROM t4__base;
CREATE VIEW t4 AS
SELECT c_pk, c_int FROM t4_keyed QUALIFY (ROW_NUMBER() OVER (PARTITION BY eq_key_1 ORDER BY eq_key_1)) = 1;
SELECT * FROM t4 WHERE c_int::BOOLEAN;
-- Expected/Actual: 2 rows (c_pk=1, c_pk=2 -- both c_int=5 and c_int=-7 are nonzero/truthy; only
-- c_int=0 is filtered out). Clean -- the outer QUALIFY is necessary, not just any outer clause.

-- Control C: drop the inner view's QUALIFY (materialize the dedup instead, or use a plain view
-- with no window filter at all), keep everything else -- clean. The inner window-filtered VIEW
-- specifically is necessary; a materialized TABLE with the same rows is not enough.
CREATE TABLE t5__base (c_pk BIGINT NOT NULL, c_int BIGINT);
INSERT INTO t5__base VALUES (1, 5), (2, -7), (3, 0);
CREATE VIEW t5 AS SELECT c_pk, c_int FROM t5__base;
SELECT * FROM t5 WHERE c_int::BOOLEAN QUALIFY ROW_NUMBER() OVER (ORDER BY c_pk) <= 1;
-- Expected/Actual: 1 row. Clean.

-- Control D: keep everything, but make the inner QUALIFY unpartitioned (no PARTITION BY) --
-- clean. PARTITION BY specifically is load-bearing, not "any window filter in the view".
CREATE TABLE t6__base (c_pk BIGINT NOT NULL, c_int BIGINT);
INSERT INTO t6__base VALUES (1, 5), (2, -7), (3, 0);
CREATE VIEW t6 AS SELECT c_pk, c_int FROM t6__base QUALIFY ROW_NUMBER() OVER (ORDER BY c_pk) >= 1;
SELECT * FROM t6 WHERE c_int::BOOLEAN QUALIFY ROW_NUMBER() OVER (ORDER BY c_pk) <= 1;
-- Expected/Actual: 1 row. Clean.

-- Control E: replace the cast-requiring WHERE with one that needs NO cast at all
-- (a same-type, direct comparison against a literal) on the full t2 shape -- clean.
SELECT * FROM t2 WHERE c_pk > 0 QUALIFY ROW_NUMBER() OVER (ORDER BY c_pk) <= 1;
-- Expected/Actual: 1 row (c_pk=1). Clean -- confirms the WHERE clause must itself force a CAST.

-- Control F: SET disabled_optimizers='top_n_window_elimination' makes the ORIGINAL failing query
-- (t2, WHERE c_int::BOOLEAN) succeed -- localises the defect to that one pass, same as the
-- arg_min/arg_max finding, but via a different mechanism (this one never touches arg_min/arg_max).
SET disabled_optimizers='top_n_window_elimination';
SELECT * FROM t2 WHERE c_int::BOOLEAN QUALIFY ROW_NUMBER() OVER (ORDER BY c_pk) <= 1;
-- Expected/Actual: 1 row. Clean with the optimizer disabled.
RESET disabled_optimizers;

-- Control G: a wider survey of WHERE-clause shapes against the t2 fixture (uncomment/run
-- individually) -- some cast-inducing shapes reproduce, some clean same-type comparisons don't;
-- see bug_report.md Characterization for the accounting across the campaign's 131 findings.
--   WHERE c_int                             -- reproduces (implicit cast, same as ::BOOLEAN)
--   WHERE TAN(c_int)                        -- reproduces
--   WHERE (c_int OR c_int)                  -- reproduces
--   WHERE (c_pk < c_int)                    -- clean (same-type, no cast needed)
--   WHERE (c_pk = c_int)                    -- clean
