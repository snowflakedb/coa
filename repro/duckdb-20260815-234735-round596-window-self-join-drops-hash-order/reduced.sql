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

-- DuckDB v2.0.0-alpha37826 (a9f869b6a7).
-- WindowSelfJoinOptimizer rewrites
--   QUALIFY COUNT(*) OVER (PARTITION BY p ORDER BY <expr> ROWS UNBOUNDED) >= k
-- into a grouped COUNT join and TranslateAggregate drops ORDER BY because COUNT
-- is not order-dependent (window_self_join.cpp: "ORDER BY is a NOP, so drop it").
-- HASH()+HASH() overflows UINT64, so evaluating the ORDER BY throws. The rewrite
-- never evaluates it. A leftover LogicalWindow in the scanned relation (a view
-- that still contains COUNT(*) OVER ()) makes CanOptimize(child) return false,
-- the Window operator remains, and the same query throws.
--
-- Mask: SET disabled_optimizers='window_self_join';
-- 1.5.0 wheel has no this rewrite and already throws on the heap table.
-- CLI: duckdb

-- ========== original finding shape (round 596) ==========
-- Equivalent was UNION ALL doubled, then
--   DISTINCT k, MAX(col) OVER (PARTITION BY k) AS col
-- which leaves a LogicalWindow in the child and blocks the rewrite.
-- Distilled to a view whose SELECT list is the base columns over a
-- COUNT(*) OVER () leftover window — same CanOptimize(child) miss.

CREATE TABLE b (
  c_pk BIGINT NOT NULL,
  c_int BIGINT,
  c_big DECIMAL(38, 0),
  c_dec DECIMAL(10, 2),
  c_dbl DOUBLE,
  c_txt VARCHAR,
  c_chr VARCHAR,
  c_date DATE,
  c_ts TIMESTAMP
);
INSERT INTO b VALUES (1, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);
INSERT INTO b VALUES (2, 0, NULL, -5.5, 'Infinity'::DOUBLE, 'abc', 'abc', NULL, NULL);
INSERT INTO b VALUES (3, 1, -1, NULL, '-Infinity'::DOUBLE, '', 'a', NULL, NULL);
INSERT INTO b VALUES (4, -7, 42, 999.99, 'Infinity'::DOUBLE, 'Zed', 'Zed', NULL, '1999-12-31 23:59:59');
INSERT INTO b VALUES (5, NULL, 0, 0.0, NULL, 'o''brien', 'Zed', '1999-12-31', '2024-01-15 12:34:56');
INSERT INTO b VALUES (6, 42, NULL, 999.99, 1000.125, '', NULL, '2024-01-15', '2024-01-15 12:34:56');
INSERT INTO b VALUES (7, -7, NULL, 999.99, 1000.125, 'trailing ', NULL, NULL, NULL);

CREATE TABLE t AS SELECT * FROM b;

-- Heap: WindowSelfJoinOptimizer fires. HASH+HASH is never evaluated.
-- Expected if ORDER BY were evaluated: Out of Range Error (UINT64 add).
-- Actual: 4 rows.
SELECT DISTINCT t1.c_big NOT IN (t1.c_big), t1.c_dec, t1.c_txt
FROM t AS t1
QUALIFY COUNT(*) OVER (
  PARTITION BY CASE WHEN t1.c_chr IS NULL THEN IFNULL(t1.c_int, t1.c_big) ELSE t1.c_dec END
  ORDER BY HASH(sha256(t1.c_txt), t1.c_chr) + HASH(t1.c_dec, t1.c_dec) ASC,
           CASE WHEN True THEN t1.c_chr END DESC NULLS FIRST,
           t1.c_int ASC NULLS LAST
  ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
) >= t1.c_int;
-- => 4 rows (rewrite dropped the overflowing ORDER BY)

-- ========== distilled: leftover window in a view blocks the rewrite ==========
DROP TABLE t;
CREATE VIEW t AS
SELECT c_pk, c_int, c_big, c_dec, c_dbl, c_txt, c_chr, c_date, c_ts
FROM (SELECT *, COUNT(*) OVER () AS dummy FROM b);

-- Expected: 4 rows (same as heap), or both sides throwing.
-- Actual: Out of Range Error: Overflow in addition of UINT64
SELECT DISTINCT t1.c_big NOT IN (t1.c_big), t1.c_dec, t1.c_txt
FROM t AS t1
QUALIFY COUNT(*) OVER (
  PARTITION BY CASE WHEN t1.c_chr IS NULL THEN IFNULL(t1.c_int, t1.c_big) ELSE t1.c_dec END
  ORDER BY HASH(sha256(t1.c_txt), t1.c_chr) + HASH(t1.c_dec, t1.c_dec) ASC,
           CASE WHEN True THEN t1.c_chr END DESC NULLS FIRST,
           t1.c_int ASC NULLS LAST
  ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
) >= t1.c_int;
-- => Out of Range Error: Overflow in addition of UINT64 (…)

-- ========== controls (run each in a fresh session) ==========
-- HASH()+HASH() itself overflows on these values:
--   SELECT HASH(999.99::DECIMAL(10,2)) + HASH(999.99::DECIMAL(10,2));
--   -- Out of Range Error
--
-- Heap + SET disabled_optimizers='window_self_join' throws (rewrite off).
-- Identity view `CREATE VIEW t AS SELECT * FROM b` does NOT throw (rewrite still fires).
-- CTAS that materialises COUNT(*) OVER () then DROPs dummy does NOT throw
--   (no LogicalWindow left in the child).
-- 1.5.0 wheel throws on the heap table (no WindowSelfJoin COUNT rewrite).
