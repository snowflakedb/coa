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

-- Dolt 8.0.31 (VERSION()) / dolt 2.2.3, source v2.2.3-9-g95218a00a, commit
--   95218a00a973be43d84e5c60836cb3ffe8c34387 (2026-07-30), built 2026-07-31, assertions off,
--   dolthub/go-mysql-server. Binary: dolt-main/bin/dolt
-- Reference: MySQL 9.7.2 LTS, commit 008e09c2834b98143a8c067d4d225c90953050cf (branch 9.7),
--   RelWithDebInfo, assertions off. Binary: mysql-9.7/bin
-- Client: pymysql 2.2.8 / CPython 3.11.13, via the dialect's adapter.connect().
-- Session: sql_mode = ERROR_FOR_DIVISION_BY_ZERO,NO_BACKSLASH_ESCAPES,NO_ENGINE_SUBSTITUTION,
--   NO_ZERO_DATE,NO_ZERO_IN_DATE,ONLY_FULL_GROUP_BY,STRICT_ALL_TABLES (bug is sql_mode-independent);
--   dolt connection charset/collation utf8mb4 / utf8mb4_0900_ai_ci (server & db: utf8mb4_0900_bin);
--   MySQL connection utf8mb4 / utf8mb4_0900_bin. That collation asymmetry is NOT the cause -- see the
--   per-session table below, where both engines were measured under all seven settings.
--
-- RUN IT:
--   Run the statements below against dolt and mysql.
-- CAUTION: `SELECT RPAD('é', 1, 'x')` (no HEX) returns invalid UTF-8 and KILLS a utf8 client
--   connection -- that is the bug. Give each query its own connection, or wrap in HEX(), when
--   replaying this file top to bottom.
--
-- BUG: RPAD()/LPAD() count the target length in BYTES, not CHARACTERS. MySQL's RPAD/LPAD are
-- character-based. Two consequences:
--   (1) wrong length/result for any multibyte input, and
--   (2) when the byte count falls inside a multibyte character, the result is TRUNCATED
--       mid-character and is INVALID UTF-8 -- which is what the fuzzer's Python (utf8) client
--       could not decode, and the harness then mislabelled as "ENGINE CRASH (exited status 1)".
--       The dolt server does NOT crash; it returns a malformed byte string.
--
-- 'é' = C3 A9 (2 bytes, 1 char). '©' = C2 A9. Run against dolt; compare to MySQL in comments.

-- (1) INVALID UTF-8 (the "crash"): target length 1 keeps only the first byte of 'é'.
SELECT HEX(RPAD('é', 1, 'x'));      -- dolt: 'C3'   (lone lead byte = invalid UTF-8)
                                    -- MySQL: 'C3A9' ('é')
SELECT RPAD('é', 1, 'x');           -- dolt: raw 0xC3 -> a utf8 client raises
                                    --   UnicodeDecodeError 'utf-8' codec can't decode byte 0xc3
SELECT HEX(LPAD('©', 1, 'x'));      -- dolt: 'C2'    ;  MySQL: 'C2A9'

-- (2) WRONG LENGTH/RESULT for a valid (ASCII-padded) case -- byte-count vs char-count:
SELECT CHAR_LENGTH(RPAD('é', 7, 'ab'));  -- dolt: 6   ; MySQL: 7
SELECT HEX(RPAD('é', 7, 'ab'));          -- dolt: 'C3A96162616261'   (7 bytes = 'éababa', 6 chars)
                                         -- MySQL:'C3A9616261626162'  (8 bytes = 'éababab', 7 chars)
SELECT CHAR_LENGTH(LPAD('©', 5, 'ab'));  -- dolt: 4   ; MySQL: 5

-- CONTROL: character-based string functions are correct in dolt, so the bug is specific to
-- the pad functions, not general multibyte handling:
SELECT HEX(LEFT('é', 1));           -- dolt: 'C3A9' ('é')  == MySQL   ✓
SELECT HEX(SUBSTRING('é', 1, 1));   -- dolt: 'C3A9' ('é')  == MySQL   ✓

-- =====================================================================================
-- IS IT A COLLATION / CHARSET ISSUE?  No -- the pad-length bug is independent of both.
-- Run each block; dolt gives the same wrong answer every time, MySQL the right one.
-- =====================================================================================
SET NAMES utf8mb4 COLLATE utf8mb4_0900_ai_ci;
SELECT HEX(RPAD('é',1,'x')), CHAR_LENGTH(RPAD('é',7,'ab'));   -- dolt: 'C3', 6 ; MySQL: 'C3A9', 7
SET NAMES utf8mb4 COLLATE utf8mb4_0900_bin;
SELECT HEX(RPAD('é',1,'x')), CHAR_LENGTH(RPAD('é',7,'ab'));   -- dolt: 'C3', 6 ; MySQL: 'C3A9', 7
SET NAMES utf8mb4 COLLATE utf8mb4_general_ci;
SELECT HEX(RPAD('é',1,'x')), CHAR_LENGTH(RPAD('é',7,'ab'));   -- dolt: 'C3', 6 ; MySQL: 'C3A9', 7
SET NAMES utf8mb4 COLLATE utf8mb4_unicode_ci;
SELECT HEX(RPAD('é',1,'x')), CHAR_LENGTH(RPAD('é',7,'ab'));   -- dolt: 'C3', 6 ; MySQL: 'C3A9', 7
SET NAMES utf8mb3;
SELECT HEX(RPAD('é',1,'x')), CHAR_LENGTH(RPAD('é',7,'ab'));   -- dolt: 'C3', 6 ; MySQL: 'C3A9', 7
SET NAMES binary;
SELECT HEX(RPAD('é',1,'x')), CHAR_LENGTH(RPAD('é',7,'ab'));   -- dolt: 'C3', 6 ; MySQL: 'C3', 7
-- NOTE on `SET NAMES binary` and `SET NAMES latin1`: a UTF-8 client sends 'é' as C3 A9 regardless,
-- so under those settings MySQL sees TWO characters and RPAD(...,1,...) correctly returns 'C3' --
-- the HEX values agree with dolt there for a legitimate reason. CHAR_LENGTH still separates them
-- (MySQL 7, dolt 6), which is the part that matters. Do NOT read the agreeing HEX cells as MySQL
-- endorsing dolt's behaviour; they are about the client's encoding.
SET NAMES utf8mb4 COLLATE utf8mb4_0900_ai_ci;

-- Explicit introducers / conversions do not help either:
SELECT HEX(RPAD(_utf8mb4'é', 1, _utf8mb4'x'));                -- dolt: 'C3' ; MySQL: 'C3A9'
SELECT HEX(RPAD(CONVERT('é' USING utf8mb4), 1, 'x'));         -- dolt: 'C3' ; MySQL: 'C3A9'

-- THE TELL: for a VARBINARY column, MySQL returns byte-for-byte what dolt returns for EVERY type,
-- because a binary string's "characters" ARE its bytes. So dolt is not miscounting -- it is
-- ignoring the argument's charset, i.e. treating every string as binary.
CREATE TABLE bin_col (v VARBINARY(50));
INSERT INTO bin_col VALUES ('é');
SELECT HEX(RPAD(v,1,'x')), HEX(RPAD(v,7,'ab')) FROM bin_col;
--   dolt : 'C3', 'C3A96162616261'
--   MySQL: 'C3', 'C3A96162616261'   <-- identical: correct for binary, wrong for a charset string
SELECT CHAR_LENGTH(CAST('é' AS BINARY)), LENGTH(CAST('é' AS BINARY));  -- 2, 2 on BOTH -- CHAR_LENGTH
                                                                       -- itself is fine in dolt

-- =====================================================================================
-- SECOND, SEPARATE DEFECT -- a genuine charset bug: a latin1 column comes back as UTF-8
-- bytes while the result still claims CHARSET latin1. Broader than the pad functions.
-- =====================================================================================
CREATE TABLE lat (v VARCHAR(50) CHARACTER SET latin1 COLLATE latin1_bin);
INSERT INTO lat VALUES ('é');
SELECT HEX(v) FROM lat;                                  -- 'E9' on BOTH (stored correctly)
SELECT HEX(RPAD(v,7,'ab')) FROM lat;                     -- dolt: 'C3A96162616261' ; MySQL: 'E9616261626162'
SELECT HEX(CONCAT(v,'ab')) FROM lat;                     -- dolt: 'C3A96162'       ; MySQL: 'E96162'
SELECT HEX(LEFT(v,1)) FROM lat;                          -- dolt: 'C3A9'           ; MySQL: 'E9'
SELECT LENGTH(CONCAT(v,'')) FROM lat;                    -- dolt: 2                ; MySQL: 1
SELECT CHARSET(RPAD(v,7,'ab')) FROM lat;                 -- 'latin1' on BOTH -- but dolt's bytes are UTF-8
SELECT HEX(UPPER(v)), HEX(REVERSE(v)) FROM lat;          -- 'C9','E9' on BOTH  <- these are CORRECT,
                                                         --   so it is function-dependent, not a
                                                         --   blanket "latin1 unsupported"
-- Bounded impact: a wrong LENGTH, but comparisons still work on both engines --
SELECT v, CONCAT(v,'') = v AS eq FROM lat;               -- eq = 1 on BOTH
SELECT v FROM lat WHERE CONCAT(v,'') = v;                -- the row is returned on BOTH
-- NOTE: this contradicts this repro's earlier "LEFT is a clean control" line, which holds only for
-- utf8mb4. The pad-LENGTH bug is still specific to RPAD/LPAD; the re-encoding is not.

-- =====================================================================================
-- ASCII CONTROL (the dolt devs' own check) and TWO CASES IT DOES NOT COVER.
-- =====================================================================================
-- Pure ASCII can NEVER expose this: for 'e', LENGTH == CHAR_LENGTH == 1, so byte-counting and
-- character-counting are indistinguishable. This is the control that BOUNDS the bug, not a
-- counterexample to it:
SELECT RPAD('e', 7, 'ab');                    -- 'eababab', 7 chars, on BOTH engines -- correct
-- One query that shows the whole thing:
SELECT CHAR_LENGTH(RPAD('e', 7, 'ab')) AS ascii_len,
       CHAR_LENGTH(RPAD('é', 7, 'ab')) AS multibyte_len;
--   MySQL: (7, 7)      dolt: (7, 6)

-- (a) THE PAD STRING IS BYTE-COUNTED TOO -- the first argument does not have to be multibyte.
--     An ASCII input with a multibyte pad is still wrong, so a fix that only measures the input
--     string would not cover this:
SELECT HEX(RPAD('e', 7, 'é')), CHAR_LENGTH(RPAD('e', 7, 'é'));
--   MySQL: '65C3A9C3A9C3A9C3A9C3A9C3A9', 7   (7 characters: 'e' + 6x 'é')
--   dolt : '65C3A9C3A9C3A9',             4   (7 BYTES = 4 characters)
SELECT HEX(LPAD('e', 7, 'é')), CHAR_LENGTH(LPAD('e', 7, 'é'));
--   MySQL: 'C3A9C3A9C3A9C3A9C3A9C3A965', 7      dolt: 'C3A9C3A9C3A9C3A965', 4

-- (b) A THIRD FACE: multibyte input AND multibyte pad is an outright ERROR in dolt, not a
--     wrong result -- dolt's own length check notices the malformed string it just built:
SELECT RPAD('é', 7, 'é');
--   MySQL: 'ééééééé' (7 chars, 14 bytes)
--   dolt : ERROR 1105 (HY000): malformed string encountered while checking length
