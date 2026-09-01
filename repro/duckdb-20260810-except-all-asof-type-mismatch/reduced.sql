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

-- DuckDB: INTERNAL Error when ASOF JOIN's left child is EXCEPT ALL / INTERSECT ALL
--
--   ExpressionExecutor::Execute called with a result vector of type INTEGER
--   that does not match expression type BIGINT
--
--   (on richer schemas the same plan also surfaces as)
--   Vector::Reference used on vector of different type
--   (source DECIMAL(38,0) referenced BIGINT)
--
-- Engine: duckdb v2.0.0-alpha37247 (Cyanoptera) e500d77864
-- Found:  eqgen corpus hunt duckdb_hunt4_.../corpus error_round37_1.sql
--         (oracle: base ASOF OK, equivalent EXCEPT-ALL view as left of ASOF INTERNALs;
--          relations row-identical)

-- ============================== MINIMAL REPRO ==============================
CREATE TABLE a(d INTEGER);
INSERT INTO a VALUES (1), (2);
CREATE TABLE b(d INTEGER);
CREATE TABLE r AS SELECT * FROM a;

-- Expected: 2 rows (ASOF match each left row to itself on r)
-- Actual:   INTERNAL Error: ExpressionExecutor::Execute called with a result
--           vector of type INTEGER that does not match expression type BIGINT
SELECT * FROM (SELECT * FROM a EXCEPT ALL SELECT * FROM b) t0
ASOF LEFT JOIN r t1 ON t0.d >= t1.d;

-- INTERSECT ALL hits the same assertion:
-- SELECT * FROM (SELECT * FROM a INTERSECT ALL SELECT * FROM a) t0
-- ASOF LEFT JOIN r t1 ON t0.d >= t1.d;

-- EXCEPT (no ALL) is clean:
-- SELECT * FROM (SELECT * FROM a EXCEPT SELECT * FROM b) t0
-- ASOF LEFT JOIN r t1 ON t0.d >= t1.d;

-- Materializing the set-op before the ASOF is clean:
-- CREATE TABLE t0 AS SELECT * FROM a EXCEPT ALL SELECT * FROM b;
-- SELECT * FROM t0 ASOF LEFT JOIN r t1 ON t0.d >= t1.d;

-- ============================== INGREDIENT NOTES ==============================
-- Empty table as EXCEPT ALL right child reproduces; `WHERE FALSE` does NOT —
-- the optimizer erases the set-op and ASOF runs clean:
--   (a EXCEPT ALL SELECT * FROM a WHERE FALSE) ASOF ...  -- CLEAN
--   (a EXCEPT ALL b) with b empty table                  -- INTERNAL
-- EXCEPT ALL on the RIGHT of ASOF also INTERNALs:
--   r ASOF LEFT JOIN (a EXCEPT ALL b) ON r.d >= t1.d
