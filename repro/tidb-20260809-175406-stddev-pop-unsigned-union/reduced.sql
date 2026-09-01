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

-- TiDB: STDDEV_POP / VAR_* over BIGINT UNSIGNED near 2^64 is wrong on multi-way UNION ALL
-- (base table / materialized union → 0.0; ≥3-way UNION ALL view → nonzero).
-- Engine: tidb 8.0.11-TiDB-v8.5.0 (docker pingcap/tidb:v8.5.0)

CREATE TABLE u (sh BIGINT UNSIGNED);
INSERT INTO u VALUES
  (18446744073709551614),
  (18446744073709551613),
  (18446744073709551612),
  (18446744073709551611),
  (18446744073709551610),
  (18446744073709551609),
  (18446744073709551608),
  (18446744073709551607);

-- Correct: all eight values sit in one DOUBLE ULP below 2^64, so population stddev is 0.
SELECT STDDEV_POP(sh) AS s FROM u;
-- → 0.0

-- Wrong: same multiset via a 4-way UNION ALL (RankMod-style partition).
SELECT STDDEV_POP(sh) AS s FROM (
  SELECT sh FROM u WHERE MOD(sh, 4) = 0
  UNION ALL SELECT sh FROM u WHERE MOD(sh, 4) = 1
  UNION ALL SELECT sh FROM u WHERE MOD(sh, 4) = 2
  UNION ALL SELECT sh FROM u WHERE MOD(sh, 4) = 3
) AS x;
-- → 443.40500673763256  (also wrong for VAR_POP / VAR_SAMP / STDDEV_SAMP / VARIANCE)

-- Same values via bitneg of SIGNED 1..8:
--   SELECT STDDEV_POP((~ c_pk)) FROM t;           -- 0.0 on base table
--   SELECT STDDEV_POP((~ c_pk)) FROM <4-way view>; -- 443.405...
