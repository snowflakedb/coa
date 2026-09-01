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

-- Dolt: STDDEV / VARIANCE / STDDEV_SAMP / VAR_SAMP return the ARGUMENT's type instead of DOUBLE,
-- rounding the result to an integer (and to a string for a string argument)
--
-- Engine    : dolt 2.2.3 (server reports VERSION() = 8.0.31)
-- Reference : MariaDB 11.4.12 (docker mariadb:11.4) -- returns DOUBLE with the full value
-- Session   : sql_mode is matched to the fuzz run on BOTH engines by the verifier
--             (STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES,PIPES_AS_CONCAT) --
--             `||` is logical OR in MySQL without PIPES_AS_CONCAT, so an unmatched mode makes any
--             cross-engine comparison of these queries meaningless.
-- Findings  : dolt_20260809-052933/mismatch_round{22_0,29_0}.sql
--
-- Blocks are `-- >>> BLOCK: <name> dolt=<value> maria=<value> wire=<dolt wire type>`; each was run
-- against BOTH engines in a fresh database and all three checked.
--
-- Every block uses:  t(i BIGINT, d DOUBLE, s VARCHAR(50)) = (1,1.0,'a'),(2,2.0,'b'),(5,5.0,'c')
-- Truth for i = (1,2,5):  population SD 1.6997, sample SD 2.0817, population VAR 2.8889, sample VAR 4.3333


-- >>> BLOCK: stddev-int  dolt=2  maria=1.6997  wire=LONGLONG
-- THE BUG. Population standard deviation of (1,2,5) is 1.6997. Dolt returns the integer 2 and declares
-- the column LONGLONG; MariaDB returns 1.6997 as DOUBLE.
CREATE TABLE t (i BIGINT, d DOUBLE, s VARCHAR(50));
INSERT INTO t VALUES (1,1.0,'a'),(2,2.0,'b'),(5,5.0,'c');
SELECT STDDEV(i) FROM t;

-- >>> BLOCK: stddev-samp-int  dolt=2  maria=2.0817  wire=LONGLONG
-- Worse than a rounding error: STDDEV (1.6997) and STDDEV_SAMP (2.0817) are mathematically different,
-- but both round to 2, so Dolt makes the population/sample distinction disappear on integer columns.
CREATE TABLE t (i BIGINT, d DOUBLE, s VARCHAR(50));
INSERT INTO t VALUES (1,1.0,'a'),(2,2.0,'b'),(5,5.0,'c');
SELECT STDDEV_SAMP(i) FROM t;

-- >>> BLOCK: variance-int  dolt=3  maria=2.8889  wire=LONGLONG
CREATE TABLE t (i BIGINT, d DOUBLE, s VARCHAR(50));
INSERT INTO t VALUES (1,1.0,'a'),(2,2.0,'b'),(5,5.0,'c');
SELECT VARIANCE(i) FROM t;

-- >>> BLOCK: var-samp-int  dolt=4  maria=4.3333  wire=LONGLONG
CREATE TABLE t (i BIGINT, d DOUBLE, s VARCHAR(50));
INSERT INTO t VALUES (1,1.0,'a'),(2,2.0,'b'),(5,5.0,'c');
SELECT VAR_SAMP(i) FROM t;

-- >>> BLOCK: variance-string  dolt=0  maria=0.0  wire=VAR_STRING
-- A string argument yields a STRING-typed result in Dolt ('0'), DOUBLE (0.0) in MariaDB. Same numeric
-- value here, but the declared type is wrong, which is what made the fuzzer's rows differ by type.
CREATE TABLE t (i BIGINT, d DOUBLE, s VARCHAR(50));
INSERT INTO t VALUES (1,1.0,'a'),(2,2.0,'b'),(5,5.0,'c');
SELECT VARIANCE(s) FROM t;


-- ============================================================================================
-- Controls: the defect is the RESULT TYPE following the argument type, nothing else.
-- ============================================================================================

-- >>> BLOCK: control-stddev-double  dolt=1.699673171197595  maria=1.699673171197595  wire=DOUBLE
-- A DOUBLE argument gives the correct DOUBLE result on both engines -- so the maths is right and only
-- the result type (and the rounding it forces) is wrong.
CREATE TABLE t (i BIGINT, d DOUBLE, s VARCHAR(50));
INSERT INTO t VALUES (1,1.0,'a'),(2,2.0,'b'),(5,5.0,'c');
SELECT STDDEV(d) FROM t;

-- >>> BLOCK: control-cast-arg-to-double  dolt=1.699673171197595  maria=1.699673171197595  wire=DOUBLE
-- Casting the argument is the workaround.
CREATE TABLE t (i BIGINT, d DOUBLE, s VARCHAR(50));
INSERT INTO t VALUES (1,1.0,'a'),(2,2.0,'b'),(5,5.0,'c');
SELECT STDDEV(CAST(i AS DOUBLE)) FROM t;

-- >>> BLOCK: control-avg-int-ok  dolt=2.6666666666666665  maria=2.6667  wire=DOUBLE
-- AVG over the same integer column is NOT rounded on either engine (Dolt returns DOUBLE, MariaDB
-- NEWDECIMAL -- a declared-type difference but the value is right), so this is specific to the
-- STDDEV/VARIANCE family rather than a general integer-aggregate rule.
CREATE TABLE t (i BIGINT, d DOUBLE, s VARCHAR(50));
INSERT INTO t VALUES (1,1.0,'a'),(2,2.0,'b'),(5,5.0,'c');
SELECT AVG(i) FROM t;
