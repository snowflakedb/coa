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

-- DuckDB v2.0.0-alpha37730 (c6f7f3e250). SUBSTR/substring with a hugely
-- negative start is supposed to count from the end of the string (DuckDB's
-- documented contract, see duckdb#10721). When column statistics say the
-- VARCHAR is ASCII-only, SubstringPropagateStats swaps in SubstringFunctionASCII,
-- whose start = max(len+offset, 0) *clamps* to the front instead -- so the same
-- call returns the whole string on a heap table and '' everywhere ASCII stats
-- are absent (constants, ENUM round-trip, ANY_VALUE view, statistics_propagation off).
--
-- CLI: duckdb

CREATE TABLE t(s VARCHAR);
INSERT INTO t VALUES ('abc');

-- Expected: '' (negative start far before the first character → empty).
-- Actual:   'abc'   -- ASCII fast path clamped to offset 0.
SELECT SUBSTR(s, -12345678, 12) FROM t;
-- => abc

-- Control 1: the identical call on a constant uses the Unicode path.
SELECT SUBSTR('abc', -12345678, 12);
-- => ''   (empty)   -- Expected, and what the table *should* return.

-- Control 2: drop ASCII stats; the heap table now matches the constant.
SET disabled_optimizers='statistics_propagation';
SELECT SUBSTR(s, -12345678, 12) FROM t;
-- => ''   (empty)

RESET disabled_optimizers;

-- Control 2b: positive start past the end + negative length (rich-shuffle2
-- rounds 104/171). Same ASCII clamp vs Unicode empty split.
SELECT SUBSTR(s, 15, -7) FROM t;
-- => abc   WRONG
SELECT SUBSTR('abc', 15, -7);
-- => ''    expected
SET disabled_optimizers='statistics_propagation';
SELECT SUBSTR(s, 15, -7) FROM t;
-- => ''    expected
RESET disabled_optimizers;

-- Control 3: ENUM round-trip (the round-47 equivalent's last step) loses ASCII
-- stats the same way. Expected '' ; actual ''.
CREATE TYPE e AS ENUM ('abc');
CREATE VIEW v_enum AS SELECT CAST(CAST(s AS e) AS VARCHAR) AS s FROM t;
SELECT SUBSTR(s, -12345678, 12) FROM v_enum;
-- => ''   (empty)

-- Control 4: ANY_VALUE + UNION ALL key-dedup view (the round-29 equivalent).
-- Same empty answer as the Unicode path.
CREATE TABLE k AS SELECT s, ROW_NUMBER() OVER (ORDER BY 1) AS eqk FROM t;
CREATE TABLE d AS SELECT * FROM k UNION ALL SELECT * FROM k;
CREATE VIEW v_any AS SELECT ANY_VALUE(s) AS s FROM d GROUP BY eqk;
SELECT SUBSTR(s, -12345678, 12) FROM v_any;
-- => ''   (empty)

-- Control 5: small negative offsets still agree (the clamp/from-end split only
-- appears once |offset| exceeds the string length).
SELECT n, SUBSTR('abc', n, 2) AS cnst, SUBSTR(s, n, 2) AS heap
FROM t, range(-3, 2) r(n);
-- -3 ab,ab / -2 bc,bc / -1 c,c / 0 a,a / 1 ab,ab
