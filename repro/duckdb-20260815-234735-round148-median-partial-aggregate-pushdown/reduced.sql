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
-- PartialAggregatePushdown's double-eager rewrite (TryDoubleEagerPushdown)
-- pushes MEDIAN below an inner join as EXPORT_STATE grouped by the join key,
-- then combine_aggr(state, opposite_side_count). That reconstructs SUM/COUNT/
-- AVG, but not MEDIAN / QUANTILE_CONT / MAD / LIST: dimension-side multiplicity
-- is lost, so the result is the median of the unweighted per-key bags.
--
-- Mask: SET disabled_optimizers='partial_aggregate_pushdown';
--       (join_order off also avoids the plan; that is not unspecified SQL.)
-- CLI: duckdb

-- ========== original finding shape (round 148, unused cols dropped) ==========
CREATE TABLE t (
  c_int BIGINT,
  c_big DECIMAL(38, 0),
  c_dec DECIMAL(10, 2),
  c_txt VARCHAR,
  c_chr VARCHAR
);
INSERT INTO t VALUES (NULL, NULL, NULL, NULL, NULL);
INSERT INTO t VALUES (1, 1, -5.5, 'a', '');
INSERT INTO t VALUES (42, 1, 0.0, 'abc', 'Zed');
INSERT INTO t VALUES (NULL, 42, 999.99, 'trailing ', 'Zed');
INSERT INTO t VALUES (42, 1, -5.5, 'o''brien', 'o''brien');
INSERT INTO t VALUES (NULL, -7, -5.5, '', '');
INSERT INTO t VALUES (42, NULL, 12.34, NULL, 'abc');
INSERT INTO t VALUES (1, 1, -5.5, 'a', '');

-- Expected: one distinct value, 1.0  (both groups' median is 1.0).
-- Actual:   1.0 and 21.5.
SELECT DISTINCT MEDIAN(t3.c_int)
FROM t AS t1
FULL OUTER JOIN t AS t2 ON t1.c_dec = t2.c_dec
INNER JOIN t AS t3 ON t2.c_txt = t3.c_chr
GROUP BY t1.c_big
ORDER BY 1;
-- => 1.0
-- => 21.5

-- FOJ is not required. INNER JOIN already diverges.
SELECT t1.c_big, MEDIAN(t3.c_int) AS m
FROM t AS t1
INNER JOIN t AS t2 ON t1.c_dec = t2.c_dec
INNER JOIN t AS t3 ON t2.c_txt = t3.c_chr
GROUP BY t1.c_big
ORDER BY 1 NULLS FIRST;
-- => -7, 1.0
-- =>  1, 21.5     WRONG (should be 1.0)

DROP TABLE t;

-- ========== distilled (6 rows, integer keys) ==========
CREATE TABLE t(i INT, g INT, d INT, txt INT, chr INT);
INSERT INTO t VALUES
  (1, 1, 1, 1, 0),
  (1, 1, 1, 1, 0),
  (42, 1, 2, 3, 9),
  (42, 1, 1, 2, 2),
  (NULL, -7, 1, 0, 0),
  (42, NULL, 3, NULL, 3);

-- Expected: g=1 median 1.0. Actual: 21.5.
SELECT t1.g, MEDIAN(t3.i) AS m
FROM t t1
INNER JOIN t t2 ON t1.d = t2.d
INNER JOIN t t3 ON t2.txt = t3.chr
GROUP BY t1.g
ORDER BY 1 NULLS FIRST;
-- => -7, 1.0
-- =>  1, 21.5     WRONG

-- Control 1: same query with the rewrite disabled. 1.0 is correct.
SET disabled_optimizers='partial_aggregate_pushdown';
SELECT t1.g, MEDIAN(t3.i) AS m
FROM t t1
INNER JOIN t t2 ON t1.d = t2.d
INNER JOIN t t3 ON t2.txt = t3.chr
GROUP BY t1.g
ORDER BY 1 NULLS FIRST;
-- => -7, 1.0
-- =>  1, 1.0      expected
RESET disabled_optimizers;

-- Control 2: materialize the join, then median. Same 1.0. COUNT/SUM already
-- match with PAP on; only the holistic aggregate is wrong.
CREATE TABLE j AS
SELECT t1.g, t3.i
FROM t t1
INNER JOIN t t2 ON t1.d = t2.d
INNER JOIN t t3 ON t2.txt = t3.chr;
SELECT g, MEDIAN(i) AS m, COUNT(*) AS n, SUM(i) AS s
FROM j
GROUP BY g
ORDER BY 1 NULLS FIRST;
-- => -7, 1.0, 4, 44
-- =>  1, 1.0, 13, 174     expected

-- Control 3: SUM/COUNT/AVG through the same join are already correct with PAP
-- on. LIST shows the lost multiplicity (5 values vs the real 13).
SELECT t1.g,
       LIST(t3.i ORDER BY t3.i NULLS LAST) AS xs,
       MEDIAN(t3.i) AS m,
       SUM(t3.i) AS s,
       COUNT(*) AS n
FROM t t1
INNER JOIN t t2 ON t1.d = t2.d
INNER JOIN t t3 ON t2.txt = t3.chr
GROUP BY t1.g
ORDER BY 1 NULLS FIRST;
-- g=1: xs=[1,1,42,42,NULL]  m=21.5  s=174  n=13
--      expected xs has six 1s, four 42s, three NULLs, m=1.0, same s/n

-- Control 4: QUANTILE_CONT(0.5) is the same bug; MAD also (20.5 vs 0.0).
SELECT t1.g, QUANTILE_CONT(t3.i, 0.5) AS q, MAD(t3.i) AS mad
FROM t t1
INNER JOIN t t2 ON t1.d = t2.d
INNER JOIN t t3 ON t2.txt = t3.chr
GROUP BY t1.g
ORDER BY 1 NULLS FIRST;
-- => -7, 1.0, 0.0
-- =>  1, 21.5, 20.5     WRONG (should be 1.0, 0.0)

-- Control 5: EXPLAIN of the buggy MEDIAN is double-eager PAP:
--   median EXPORT_STATE GROUP BY join key
--   JOIN
--   count_star GROUP BY join key, g
--   combine_aggr(#1, #2)
EXPLAIN SELECT t1.g, MEDIAN(t3.i)
FROM t t1
INNER JOIN t t2 ON t1.d = t2.d
INNER JOIN t t3 ON t2.txt = t3.chr
GROUP BY t1.g;
