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

-- CrateDB 6.4.1 / 6.4.2: ROUND(x, N) with a large-magnitude negative N never returns in
-- practical time. No table needed. See bug_report.md for the full characterization.

------------------------------------------------------------------------------
-- Part 1 -- fine at moderate magnitude (all instant, all correct)
------------------------------------------------------------------------------
SELECT ROUND('-1479165877', -1000000);    -- 0.10s -> 0
SELECT ROUND('-1479165877', -2000000);    -- 0.25s -> 0
SELECT ROUND('-1479165877', -5000000);    -- 0.80s -> 0
SELECT ROUND('-1479165877', -10000000);   -- 2.15s -> 0   (confirmed 2.16s on CrateDB 6.4.1 too)
SELECT ROUND('-1479165877', -20000000);   -- 6.25s -> 0

------------------------------------------------------------------------------
-- Part 2 -- THE BUG: never returns; DO NOT run without a client-side timeout
------------------------------------------------------------------------------
-- Each of these was left running (client gave up, server did not) in isolation:
-- SELECT ROUND('-1479165877', -50000000);
-- SELECT ROUND('-1479165877', -100000000);
-- SELECT ROUND('-1479165877', -300000000);
-- SELECT ROUND('-1479165877', -556375977);   -- the exact value the fuzzer generated; 16+ min observed

------------------------------------------------------------------------------
-- Part 3 -- the original finding, for provenance (reduces to Part 2 above; the
-- surrounding join/ORDER BY/table structure is not required)
------------------------------------------------------------------------------
-- Found live in crate7 (seed 91003, simple catalog, round ~39):
--   SELECT * FROM t1, t2, t0
--   WHERE ROUND('-1479165877', -556375977)
--   ORDER BY t0.c_pk, t1.c_pk ASC;
-- (WHERE ROUND(...) here is itself a pre-existing type-checking gap in the harness's query
--  generator -- ROUND returns a number, not a boolean, so this WHERE clause should have been
--  rejected before ever reaching the server; CrateDB apparently accepts it, or coerces it, and
--  hangs regardless. Not investigated further here -- the point of interest is the hang itself.)

------------------------------------------------------------------------------
-- Part 4 -- operational blind spot: while Part 2's query runs, this returns NOTHING
------------------------------------------------------------------------------
-- Run concurrently, from a second connection, while a Part 2 query is in flight:
SELECT id, EXTRACT(EPOCH FROM (now() - started)) AS secs, stmt FROM sys.jobs ORDER BY started;
-- Expected: the running ROUND(...) query, with a large `secs`.
-- Actual: empty result set -- the runaway query is invisible to CrateDB's own job introspection.
