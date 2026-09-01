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

-- Dolt: REVERSE(<numeric column>) advertises the numeric type in the result-set metadata but sends
-- a string, so any strictly-typed client fails to decode the row
--
-- Engine      : dolt 2.2.3 (server reports VERSION() = 8.0.31, its MySQL compatibility string)
-- Access path : `dolt sql-server` (wire protocol). Text-only clients such as `dolt sql` and the
--               mariadb CLI print the value happily -- they never apply the declared type.
-- Findings    : dolt_20260809-052933/crash_round{0,6}_0.sql
--               Those are recorded as CRASHES but the engine never died -- see bug_report.md.
--
-- Reference: MariaDB 11.4.12 declares VAR_STRING for the same expression and decodes fine.
--
-- Blocks are `-- >>> BLOCK: <name> expect=<fail|ok> wire=<type>`; run each in a fresh database, read
-- the declared wire type from a zero-row probe, then attempt the real fetch.


-- >>> BLOCK: reverse-bigint  expect=fail  wire=LONGLONG
-- REVERSE(-1) is the string '1-'. Dolt declares the column LONGLONG (integer) and sends '1-', so a
-- typed client raises: pymysql -> ValueError: invalid literal for int() with base 10: '1-'.
-- MariaDB declares VAR_STRING here and returns '1-' cleanly.
CREATE TABLE t (c_int BIGINT);
INSERT INTO t VALUES (-1), (0), (NULL);
SELECT REVERSE(c_int) FROM t;


-- >>> BLOCK: reverse-cast-to-char  expect=ok  wire=BLOB
-- Casting the argument to CHAR first gives a string wire type and works. This is the workaround.
CREATE TABLE t (c_int BIGINT);
INSERT INTO t VALUES (-1), (0), (NULL);
SELECT REVERSE(CAST(c_int AS CHAR)) FROM t;


-- >>> BLOCK: concat-bigint  expect=ok  wire=BLOB
-- CONCAT over the same column DOES declare a string type, so the defect is specific to how REVERSE
-- derives its return type -- not a general rule about string functions over numerics.
CREATE TABLE t (c_int BIGINT);
INSERT INTO t VALUES (-1), (0), (NULL);
SELECT CONCAT(c_int) FROM t;


-- >>> BLOCK: lower-bigint  expect=ok  wire=LONGLONG
-- LOWER over a numeric keeps LONGLONG *and* returns the number unchanged (-1), so it is
-- self-consistent and does not break the client -- but MariaDB returns the string '-1' here, so this
-- is a separate, milder type-derivation difference worth mentioning in the same report.
CREATE TABLE t (c_int BIGINT);
INSERT INTO t VALUES (-1), (0), (NULL);
SELECT LOWER(c_int) FROM t;


-- >>> BLOCK: reverse-only-nonnumeric-breaks  expect=ok  wire=LONGLONG
-- Why the fuzzer needed a negative value: REVERSE(0) is '0', which still parses as an integer, so
-- the mismatch is invisible. Only reversals that are not valid integers (from a '-' sign, or a
-- trailing zero as in 10 -> '01' which parses too) surface it. Data with a negative value is the
-- reliable trigger.
CREATE TABLE t (c_int BIGINT);
INSERT INTO t VALUES (0), (NULL);
SELECT REVERSE(c_int) FROM t;
