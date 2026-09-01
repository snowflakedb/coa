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

-- Dolt 8.0.31 / DOLT_VERSION 2.2.3 (go-mysql-server), commit a995f245c.
--
-- BUG: ORDER BY + LIMIT 18446744073709551615 (MySQL's "no limit" sentinel /
-- UINT64_MAX) panics inside GetTopNRows:
--   panic recovered: runtime error: makeslice: cap out of range
-- Plain LIMIT without ORDER BY, or LIMIT with a small bound, is fine.
-- eqgen emits this sentinel for OFFSET-without-LIMIT on MySQL-protocol dialects.
--
-- Found by eqgen mat_stress / LimitChunk+OffsetZero builders on 2026-08-10.

CREATE TABLE t (c_pk BIGINT NOT NULL);
INSERT INTO t VALUES (1), (2), (3);

-- BUG:
SELECT * FROM t ORDER BY c_pk LIMIT 18446744073709551615;
-- Expected: 3 rows. Actual: ERROR 1105 panic makeslice: cap out of range
--   (sql/sorters.GetTopNRows → topRowsIter)

-- Controls:
SELECT * FROM t LIMIT 18446744073709551615;              -- OK without ORDER BY
SELECT * FROM t ORDER BY c_pk LIMIT 3;                   -- OK small limit
SELECT * FROM t ORDER BY c_pk LIMIT 18446744073709551615 OFFSET 1;  -- OK in one probe
