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

-- MariaDB: MIN()/MAX() over BIGINT UNSIGNED loses the UNSIGNED flag when its result is converted to
-- a floating-point type, so values above 2^63 come back as small negatives (two's-complement wrap)
--
-- Engine       : mariadb 11.4.12-MariaDB-ubu2404 (docker mariadb:11.4)
-- NOT affected : mysql 9.7.2, tidb v9.0.0-beta.2 (8.0.11-TiDB), dolt 2.2.3 -- all return the
--                correct 1.8446744073709552e+19. This is MariaDB-only among the four.
-- Session      : defaults; sql_mode irrelevant.
-- Found        : while triaging ../mariadb-20260809-060545-round6-varsamp-unsigned-order/ -- probing
--                that finding's data, not reported by the fuzzer itself. See bug_report.md for how
--                the two relate (adjacent, but NOT established as its cause).
--
-- The wrap is exact two's complement: stored 18446744073709551588 = 2^64 - 28 comes back as -28.0.
--
-- Blocks are `-- >>> BLOCK: <name> mariadb=<value> correct=<value>`; each was run against every
-- available engine, checking that MariaDB returns the documented wrong value while
-- mysql/tidb/dolt return the correct one.
--
-- Every block uses:  t(sh BIGINT UNSIGNED, g INT) = (18446744073709551588, 1)


-- >>> BLOCK: cast-min-to-double  mariadb=-28.0  correct=1.8446744073709552e+19
-- THE BUG. MIN() returns the value correctly (see control-min-bare), but converting that result to
-- DOUBLE reinterprets the 64-bit pattern as signed: 2^64-28 -> -28.0.
CREATE TABLE t (sh BIGINT UNSIGNED, g INT);
INSERT INTO t VALUES (18446744073709551588, 1);
SELECT CAST(MIN(sh) AS DOUBLE) FROM t;

-- >>> BLOCK: cast-max-to-double  mariadb=-28.0  correct=1.8446744073709552e+19
-- MAX() behaves identically.
CREATE TABLE t (sh BIGINT UNSIGNED, g INT);
INSERT INTO t VALUES (18446744073709551588, 1);
SELECT CAST(MAX(sh) AS DOUBLE) FROM t;

-- >>> BLOCK: implicit-float-context  mariadb=-28.0  correct=1.8446744073709552e+19
-- No CAST needed -- any float context does it. `+ 0e0` forces DOUBLE arithmetic.
CREATE TABLE t (sh BIGINT UNSIGNED, g INT);
INSERT INTO t VALUES (18446744073709551588, 1);
SELECT MIN(sh) + 0e0 FROM t;

-- >>> BLOCK: cast-to-float  mariadb=-28.0  correct=1.8446744e+19
-- FLOAT too, so it is the integer->floating conversion generally, not the DOUBLE cast specifically.
-- Each engine prints FLOAT to a different precision (mysql/tidb 1.84467e+19, dolt 1.8446744e+19), so
-- the verifier compares numerically with a relative tolerance rather than textually.
CREATE TABLE t (sh BIGINT UNSIGNED, g INT);
INSERT INTO t VALUES (18446744073709551588, 1);
SELECT CAST(MIN(sh) AS FLOAT) FROM t;

-- >>> BLOCK: window-min-over  mariadb=-28.0  correct=1.8446744073709552e+19
-- The window form wraps as well.
CREATE TABLE t (sh BIGINT UNSIGNED, g INT);
INSERT INTO t VALUES (18446744073709551588, 1);
SELECT CAST(MIN(sh) OVER () AS DOUBLE) FROM t;

-- >>> BLOCK: sqrt-of-min  mariadb=NULL  correct=4294967296.0
-- A user-visible consequence with no CAST in sight: SQRT sees -28 and returns NULL where the correct
-- answer is 4294967296. Any function with a restricted domain turns the wrap into a silent NULL.
CREATE TABLE t (sh BIGINT UNSIGNED, g INT);
INSERT INTO t VALUES (18446744073709551588, 1);
SELECT SQRT(MIN(sh)) FROM t;


-- ============================================================================================
-- Controls. Each isolates one ingredient; all are CORRECT on every engine including MariaDB.
-- ============================================================================================

-- >>> BLOCK: control-min-bare  mariadb=18446744073709551588  correct=18446744073709551588
-- MIN() itself is right -- the value only breaks on conversion to floating point.
CREATE TABLE t (sh BIGINT UNSIGNED, g INT);
INSERT INTO t VALUES (18446744073709551588, 1);
SELECT MIN(sh) FROM t;

-- >>> BLOCK: control-cast-column-no-aggregate  mariadb=1.8446744073709552e+19  correct=1.8446744073709552e+19
-- Casting the COLUMN is correct -- an aggregate has to be in the way.
CREATE TABLE t (sh BIGINT UNSIGNED, g INT);
INSERT INTO t VALUES (18446744073709551588, 1);
SELECT CAST(sh AS DOUBLE) FROM t;

-- >>> BLOCK: control-group-by  mariadb=1.8446744073709552e+19  correct=1.8446744073709552e+19
-- Sharpest control: with an explicit GROUP BY the same expression is CORRECT. Only the implicit
-- single-group aggregate loses the flag.
CREATE TABLE t (sh BIGINT UNSIGNED, g INT);
INSERT INTO t VALUES (18446744073709551588, 1);
SELECT CAST(MIN(sh) AS DOUBLE) FROM t GROUP BY g;

-- >>> BLOCK: control-cast-to-decimal  mariadb=18446744073709551588  correct=18446744073709551588
-- A DECIMAL cast of the same aggregate is correct, so only the float path is affected.
CREATE TABLE t (sh BIGINT UNSIGNED, g INT);
INSERT INTO t VALUES (18446744073709551588, 1);
SELECT CAST(MIN(sh) AS DECIMAL(30,0)) FROM t;

-- >>> BLOCK: control-sum-to-double  mariadb=1.8446744073709552e+19  correct=1.8446744073709552e+19
-- SUM is unaffected (it yields DECIMAL, so the float conversion goes through a different path).
CREATE TABLE t (sh BIGINT UNSIGNED, g INT);
INSERT INTO t VALUES (18446744073709551588, 1);
SELECT CAST(SUM(sh) AS DOUBLE) FROM t;

-- >>> BLOCK: control-subquery-then-cast  mariadb=1.8446744073709552e+19  correct=1.8446744073709552e+19
-- Materialising the aggregate in a derived table first is correct -- the workaround.
CREATE TABLE t (sh BIGINT UNSIGNED, g INT);
INSERT INTO t VALUES (18446744073709551588, 1);
SELECT CAST(m AS DOUBLE) FROM (SELECT MIN(sh) AS m FROM t) x;

-- >>> BLOCK: control-coalesce-wrapped  mariadb=1.8446744073709552e+19  correct=1.8446744073709552e+19
-- Wrapping the aggregate in COALESCE is also enough to keep it correct.
CREATE TABLE t (sh BIGINT UNSIGNED, g INT);
INSERT INTO t VALUES (18446744073709551588, 1);
SELECT CAST(COALESCE(MIN(sh),0) AS DOUBLE) FROM t;

-- (A `GREATEST(sh,sh)` block was dropped from this file: MariaDB/MySQL/TiDB all return the value
--  correctly, but Dolt raises `1105 Unsigned int...` on GREATEST over BIGINT UNSIGNED. That is a
--  separate Dolt limitation, noted in bug_report.md, and it would make this MariaDB-focused verifier
--  fail for an unrelated reason. `control-cast-column-no-aggregate` already establishes the point
--  that non-aggregate expressions convert correctly.)

-- >>> BLOCK: control-below-signed-range  mariadb=9.223372036854776e+18  correct=9.223372036854776e+18
-- Threshold: 2^63-1 still fits in signed BIGINT, so there is nothing to wrap and the answer is right.
CREATE TABLE t (sh BIGINT UNSIGNED, g INT);
INSERT INTO t VALUES (9223372036854775807, 1);
SELECT CAST(MIN(sh) AS DOUBLE) FROM t;

-- >>> BLOCK: at-signed-boundary  mariadb=-9.223372036854776e+18  correct=9.223372036854776e+18
-- ...and 2^63 exactly, the first value that does not fit, wraps. So the trigger is
-- "value >= 2^63", i.e. any BIGINT UNSIGNED that actually uses the unsigned range.
CREATE TABLE t (sh BIGINT UNSIGNED, g INT);
INSERT INTO t VALUES (9223372036854775808, 1);
SELECT CAST(MIN(sh) AS DOUBLE) FROM t;

-- >>> BLOCK: at-max-unsigned  mariadb=-1.0  correct=1.8446744073709552e+19
-- 2^64-1 comes back as -1.0, which is the clearest single demonstration of the wrap.
CREATE TABLE t (sh BIGINT UNSIGNED, g INT);
INSERT INTO t VALUES (18446744073709551615, 1);
SELECT CAST(MIN(sh) AS DOUBLE) FROM t;
