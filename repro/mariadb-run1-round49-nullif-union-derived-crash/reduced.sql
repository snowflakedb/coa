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

-- ENGINE CRASH (SIGABRT / debug-build assertion)
-- engine=mariadb 13.1.0-MariaDB-debug @cded2b25 (assertions on, mariadb-main/bin)
-- source revision: cded2b25e65853a75c2213cfe0832819832708bd   seed=1088414189
-- sql_mode/charset/collation: harness defaults (STRICT_ALL_TABLES,...; utf8mb4 / utf8mb4_nopad_bin) -- immaterial; the crash is in the optimizer's fix_length_and_dec, before execution.
--
-- Assertion:
--   sql/item_cmpfunc.cc:2758: virtual bool Item_func_nullif::fix_length_and_dec(THD*):
--   Assertion `args[0] == args[2] || thd->stmt_arena->is_stmt_execute()' failed.
--
-- Full stack trace captured from the debug server's error log — see bug_report.md
-- (top frame: Item_func_nullif::fix_length_and_dec @ item_cmpfunc.cc:2758, via
--  Item_func::fix_fields <- JOIN::optimize_inner <- st_select_lex_unit::optimize <- mysql_derived_optimize).
-- DUPLICATE of MDEV-19091 (Confirmed / Unresolved; fix versions 10.11, 11.4, 11.8, 12.3).
-- Reduced from crash_round49.sql (3 self-joins, DISTINCT/GROUP BY/HAVING/window + 6-view
-- FULL-JOIN-emulation chain, 8 rows) by execution-guided delta-debugging.
--
-- Crashes the server on the SELECT. All three ingredients are required (verified):
--   * NULLIF               (COALESCE in its place → no crash)
--   * a UNION ALL derived table / materialized view  (a plain derived table → no crash)
--   * NULLIF applied over that derived relation        (NULLIF on the base table → no crash)

CREATE TABLE r (id BIGINT);
INSERT INTO r VALUES (0);

SELECT id FROM (SELECT id FROM r UNION ALL SELECT id FROM r WHERE 0) t
WHERE NULLIF('12', id) = id;
