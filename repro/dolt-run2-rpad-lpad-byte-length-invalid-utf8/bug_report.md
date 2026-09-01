<!--
Copyright 2026 Snowflake Inc.
SPDX-License-Identifier: Apache-2.0

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
-->

# Dolt: RPAD()/LPAD() count length in bytes, not characters — wrong length and invalid UTF-8 on multibyte input

## Summary

Dolt's `RPAD()`/`LPAD()` measure the target length in **bytes** instead of **characters**. MySQL's
pad functions are character-based. For any multibyte argument this yields the wrong length and
result; and when the byte boundary lands inside a multibyte character, the result is truncated
mid-character and is **invalid UTF-8**. A UTF-8 client (the fuzzer uses pymysql) cannot decode the
malformed bytes and raises `UnicodeDecodeError`; the dolt server itself does **not** crash. The
eqgen harness mislabelled that client-side decode failure as `ENGINE CRASH (exited status 1)` —
all 5 "crash" findings in this run are this one bug (see the harness note at the end).

**The behaviour is independent of charset and collation** — measured across seven
`SET NAMES … COLLATE …` sessions and five column charsets, every one gives the same wrong answer (see
*Is this a collation issue?* below). The crispest statement of the defect: **the pad functions behave
as though their argument were `VARBINARY`.** For a `VARBINARY` column MySQL produces byte-for-byte
what dolt produces for *every* type, because for a binary string "character" *is* "byte" — so dolt is
not computing something arbitrary, it is skipping the charset of its argument.

A **second, distinct charset defect** turned up while checking that: for a non-UTF-8 column
(`CHARACTER SET latin1`), dolt's `RPAD`/`LPAD`/`CONCAT`/`LEFT` return **UTF-8 bytes while declaring the
result charset `latin1`**, where MySQL returns latin1 bytes. That one is a genuine charset bug rather
than a length bug, and it is broader than the pad functions — it partially contradicts this report's
original "`LEFT` is a clean control" claim, which held only for `utf8mb4`. Details and its bounded
impact are below.

## Environment

Read off the live servers, not recited.

**Subject — Dolt**

| | |
|---|---|
| `VERSION()` | `8.0.31` (the MySQL protocol version dolt advertises) |
| `dolt version` | `2.2.3`; source `v2.2.3-9-g95218a00a` |
| Commit | `95218a00a973be43d84e5c60836cb3ffe8c34387`, authored 2026-07-30, built 2026-07-31 |
| Repo / branch | `github.com/dolthub/dolt`, `main` |
| Assertions | off (`"assertions": false` in the build marker) |
| Engine | dolthub/go-mysql-server |
| Binary | `dolt` |
| `@@sql_mode` | `ERROR_FOR_DIVISION_BY_ZERO,NO_BACKSLASH_ESCAPES,NO_ENGINE_SUBSTITUTION,NO_ZERO_DATE,NO_ZERO_IN_DATE,ONLY_FULL_GROUP_BY,STRICT_ALL_TABLES` |
| connection charset / collation | `utf8mb4` / `utf8mb4_0900_ai_ci` |
| server & database charset / collation | `utf8mb4` / `utf8mb4_0900_bin` |

**Reference — MySQL**

| | |
|---|---|
| `VERSION()` | `9.7.2` (LTS), aarch64 |
| Commit | `008e09c2834b98143a8c067d4d225c90953050cf`, branch `9.7`, authored 2026-07-13, built 2026-07-30 |
| Build type | `RelWithDebInfo`, assertions off |
| Binary | `mysql` 9.7 |
| `@@sql_mode` | same seven modes as dolt (different order) |
| connection charset / collation | `utf8mb4` / **`utf8mb4_0900_bin`** |

**Client / harness**: pymysql 2.2.8 on CPython 3.11.13; connections opened through the dialect's own
`adapter.connect()`, run with
`PYTHONPATH=.` and `source pyenv.sh`.

**One asymmetry worth naming, because it is exactly what would make you suspect collation:** the two
adapters do not default to the same `collation_connection` — dolt's is `utf8mb4_0900_ai_ci`, MySQL's is
`utf8mb4_0900_bin`. That difference is *not* what produces the divergence: dolt gives the identical
wrong answer under `utf8mb4_0900_bin`, and MySQL gives the identical correct answer under
`utf8mb4_0900_ai_ci` — both engines were measured across all seven sessions in the table below, so
every cell there is a like-for-like comparison.

**Not verified**: only this one dolt build and one MySQL build; no bisect, no other dolt releases, and
no non-pymysql client.

## Minimal repro

See [`reduced.sql`](./reduced.sql). No table needed:

```sql
SELECT HEX(RPAD('é', 1, 'x'));           -- dolt 'C3' (invalid UTF-8);   MySQL 'C3A9' ('é')
SELECT CHAR_LENGTH(RPAD('é', 7, 'ab'));  -- dolt 6;                      MySQL 7
```

`'é'` is `C3 A9` (2 bytes, 1 character).

## Expected vs actual

| Query | MySQL 9.7 (expected) | Dolt (actual) |
|---|---|---|
| `HEX(RPAD('é',1,'x'))` | `C3A9` (`'é'`) | `C3` — **invalid UTF-8** |
| `HEX(LPAD('©',1,'x'))` | `C2A9` (`'©'`) | `C2` — **invalid UTF-8** |
| `CHAR_LENGTH(RPAD('é',7,'ab'))` | `7` | `6` |
| `HEX(RPAD('é',7,'ab'))` | `C3A9616261626162` (`éababab`, 7 chars) | `C3A96162616261` (`éababa`, 6 chars) |
| `CHAR_LENGTH(LPAD('©',5,'ab'))` | `5` | `4` |
| `HEX(LEFT('é',1))` (control) | `C3A9` | `C3A9` ✓ |
| `HEX(SUBSTRING('é',1,1))` (control) | `C3A9` | `C3A9` ✓ |

MySQL column verified live against MySQL 9.7 (`mysql-9.7`).

## Is this a collation issue?

**No.** The pad-length defect is independent of both collation and charset. Measured on one dolt
server, same probes, one connection per row:

| Session | `HEX(RPAD('é',1,'x'))` — dolt | …MySQL 9.7 | `CHAR_LENGTH(RPAD('é',7,'ab'))` — dolt | …MySQL 9.7 |
|---|---|---|---|---|
| `utf8mb4` / `utf8mb4_0900_ai_ci` (as found) | `C3` | `C3A9` | 6 | **7** |
| `utf8mb4` / `utf8mb4_0900_bin` | `C3` | `C3A9` | 6 | **7** |
| `utf8mb4` / `utf8mb4_general_ci` | `C3` | `C3A9` | 6 | **7** |
| `utf8mb4` / `utf8mb4_unicode_ci` | `C3` | `C3A9` | 6 | **7** |
| `utf8mb3` | `C3` | `C3A9` | 6 | **7** |
| `latin1` | `C3` | `C3` † | 6 | **7** |
| `binary` | `C3` | `C3` † | 6 | **7** |

† Under `SET NAMES latin1` / `binary` the MySQL `HEX` values legitimately agree with dolt: pymysql
sends `'é'` as `C3 A9` regardless of `SET NAMES`, so MySQL sees **two** characters and padding to
length 1 correctly keeps the first byte. Those two cells are about the *client's* encoding, not about
`RPAD` — do not read them as MySQL endorsing dolt's behaviour. `CHAR_LENGTH` separates the engines in
every row, including those two, which is the column that matters.

Explicit introducers and conversions do not help either: `RPAD(_utf8mb4'é', 1, _utf8mb4'x')` and
`RPAD(CONVERT('é' USING utf8mb4), 1, 'x')` both give `C3` on dolt and `C3A9` on MySQL. Nor does the
column's own charset:

| Column type | dolt `RPAD(v,1,'x')` | dolt `CHAR_LENGTH(RPAD(v,7,'ab'))` | MySQL 9.7 |
|---|---|---|---|
| `VARCHAR CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci` | `C3` | 6 | `C3A9` / 7 |
| `VARCHAR CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin` | `C3` | 6 | `C3A9` / 7 |
| `VARCHAR CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci` | `C3` | 6 | `C3A9` / 7 |
| `VARCHAR CHARACTER SET latin1 COLLATE latin1_bin` | `C3` | 6 | `E9` / 7 |
| `VARBINARY` | `C3` | 6 | `C3` / 7 |

Two readings worth pinning down so nobody misreads the tables:

- **The `VARBINARY` row is the tell.** MySQL's `RPAD(varbinary_col, 7, 'ab')` returns exactly the bytes
  dolt returns for every type (`C3A96162616261`), because a binary string's "characters" are its bytes.
  So dolt is not miscounting — it is treating every string as binary, i.e. ignoring the argument's
  charset. (MySQL still reports `CHAR_LENGTH` 7 there, dolt 6, because dolt's result is typed
  `utf8mb4`; `CHAR_LENGTH` itself is fine in dolt — `CHAR_LENGTH(CAST('é' AS BINARY))` is 2 on both.)
- **The one measurement that could mislead** is the `latin1` / `binary` *session* rows: see the † note
  above. Both are client-encoding artefacts, and `CHAR_LENGTH` still shows the defect there.

## Second defect: a latin1 column comes back as UTF-8 bytes labelled latin1

Separate from the length bug, and a real charset bug. With `v` a `VARCHAR CHARACTER SET latin1`
column holding `'é'` (stored correctly as `E9` on both engines):

| Expression | MySQL 9.7 | Dolt |
|---|---|---|
| `HEX(RPAD(v,7,'ab'))` | `E9616261626162` | **`C3A96162616261`** |
| `HEX(CONCAT(v,'ab'))` | `E96162` | **`C3A96162`** |
| `HEX(LEFT(v,1))` | `E9` | **`C3A9`** |
| `LENGTH(CONCAT(v,''))` | `1` | **`2`** |
| `CHARSET(RPAD(v,7,'ab'))` | `latin1` | `latin1` (but the bytes are UTF-8) |
| `HEX(UPPER(v))` | `C9` | `C9` ✓ |
| `HEX(REVERSE(v))` | `E9` | `E9` ✓ |

So dolt re-encodes the value to UTF-8 inside some string functions while still declaring the result
`latin1`. It is function-dependent — `UPPER` and `REVERSE` are correct — so this is not a blanket
"latin1 is unsupported".

**Bounded impact:** it is a wrong result for byte-length introspection (`LENGTH(CONCAT(v,''))` = 2
instead of 1), but it does **not** break comparisons or row matching: `CONCAT(v,'') = v` is still true
and `WHERE CONCAT(v,'') = v` still returns both rows on both engines. So: report it, don't rate it as a
silent-row-loss bug.

This also **narrows this report's original control claim**. "`LEFT`/`SUBSTRING` are correctly
character-based, so the bug is specific to the pad functions, not dolt's general multibyte handling"
is true for `utf8mb4` — which is all the finding exercised — but `LEFT` *is* wrong on a `latin1`
column. The pad-length bug remains specific to `RPAD`/`LPAD`; the charset re-encoding is not.

## Equivalence construction

Not implicated. This is a pure single-query, single-table bug — it reproduces on a literal
`SELECT RPAD(...)` with no table and no equivalence rewrite. The 5 findings were labelled crashes,
not mismatches, so the base and equivalent results are identical (both malformed); the equivalence
oracle is not what surfaced this (the query generator's use of `RPAD`/`LPAD` over the multibyte
seed values `'é'`, `'©'`, `'𒀀'` did).

## Minimal oracle exposure path

- **Object composition arity:** `0`.
- **GCL builder path:** none — no equivalence object is needed or implicated.
- **Confidence:** high; the report establishes a literal-only repro and identical failure on both
  oracle sides.
- **Realization:** none; the probe is a bare `SELECT`.
- **Workload/data requirements (excluded from arity):** `RPAD`/`LPAD`, a multibyte input or pad string,
  the target length, and the UTF-8-decoding client are workload/data conditions, not object builders.
- **Exposure vs. intrinsic trigger:** there is no object contrast: both sides return the same
  malformed value and fail identically in the client. The intrinsic trigger is byte-counting inside
  `RPAD`/`LPAD`, independent of equivalence construction.

## The ASCII control, and two cases beyond it

The dolt maintainers confirmed the report reproducible and noted that `RPAD('e', 7, 'ab')` correctly
returns `eababab`. That is the **control that bounds the bug, not a counterexample**: for `'e'`,
`LENGTH` and `CHAR_LENGTH` are both 1, so byte-counting and character-counting are indistinguishable.
Pure-ASCII input can never expose this. One query shows the whole thing:

```sql
SELECT CHAR_LENGTH(RPAD('e', 7, 'ab')) AS ascii_len,
       CHAR_LENGTH(RPAD('é', 7, 'ab')) AS multibyte_len;
--   MySQL 9.7: (7, 7)        Dolt: (7, 6)
```

Two further measurements matter for scoping a fix, and neither is covered by the ASCII case:

**(a) The pad string is byte-counted too — the first argument need not be multibyte.** So a fix that
only measures the input string would leave this wrong:

| | MySQL 9.7 | Dolt |
|---|---|---|
| `RPAD('e', 7, 'é')` | `65C3A9C3A9C3A9C3A9C3A9C3A9`, 7 chars | **`65C3A9C3A9C3A9`, 4 chars** (7 bytes) |
| `LPAD('e', 7, 'é')` | `C3A9C3A9C3A9C3A9C3A9C3A965`, 7 chars | **`C3A9C3A9C3A9C3A965`, 4 chars** |

**(b) A third face: multibyte input *and* multibyte pad is an outright error, not a wrong result.**
Dolt's own length check catches the malformed string it just built:

```sql
SELECT RPAD('é', 7, 'é');
--   MySQL: 'ééééééé' (7 characters, 14 bytes)
--   Dolt : ERROR 1105 (HY000): malformed string encountered while checking length
```

So the same defect has three surfaces — silent wrong length (ASCII pad), invalid UTF-8 (byte boundary
inside a character), and a hard error (multibyte pad). Fixing the length computation to operate on
characters in the argument's charset should close all three at once.

## Characterization

- **Trigger**: `RPAD(s, n, pad)` / `LPAD(s, n, pad)` where `s` (or the resulting prefix) contains a
  multibyte UTF-8 character and `n` (in bytes) does not align to a character boundary → truncated,
  invalid UTF-8. Even when it stays valid (ASCII padding), `CHAR_LENGTH`/content are wrong because
  `n` is interpreted as bytes.
- **Does NOT trigger**: `LEFT`, `SUBSTRING` on `utf8mb4` (correctly character-based) — so the *length*
  defect is specific to the pad functions. Note the qualification above: on a `latin1` column `LEFT`
  mis-encodes its result, which is the separate charset defect, not this one.
- **Independent of collation and charset**: seven `SET NAMES … COLLATE …` sessions, five column
  charsets, explicit `_utf8mb4` introducers and `CONVERT(… USING utf8mb4)` all give the same wrong
  answer. See *Is this a collation issue?* above.
- **Equivalent to treating the argument as `VARBINARY`**: MySQL's `VARBINARY` behaviour matches dolt's
  behaviour for all types, which points the fix at where the pad functions read (or fail to read) their
  argument's charset rather than at the padding loop itself.
- **Not a crash**: the server stays up. Verified by polling the dolt `sql-server` process across all
  5 findings — it never exits; the server log shows only a `broken pipe` when the client drops after
  the decode error.
- Assertions off in this build, but irrelevant — no assertion or panic is involved; the engine
  returns a malformed value cleanly.

## How it was found

The eqgen **data-equivalence oracle** runs each generated workload against a base table and a
row-identical equivalent. Here the workload projected `RPAD`/`LPAD` over the multibyte seed data;
dolt returned invalid UTF-8, the pymysql client raised `UnicodeDecodeError`, and the harness's
execution wrapper classified the non-SQL exception as an engine crash. So while the oracle ran the
query, the *bug* is a plain wrong-result/robustness defect, not an equivalence divergence — it does
not need the equivalent relation at all. (A query-rewrite oracle would find it equally well; a
single-query fuzzer with a UTF-8 client would too, though it would likewise misread it as a crash.)

- **Seeds**: 219201927 (round0), 820734299 (round24), 2105471635 (round40), 752587666 (round73),
  519319024 (round96) — all 5 "crash" findings feature `RPAD`/`LPAD` over `'é'`/`'©'`/`'𒀀'`.
- Reduced repro: [`reduced.sql`](./reduced.sql).
- Original findings: hunt log
  and `crash_round{24,40,73,96}.sql`.

## Harness note (report to eqgen owners, not a dolt bug)

The dolt dialect classifies a **client-side `UnicodeDecodeError`** (engine returned invalid UTF-8)
as `ENGINE CRASH (exited status 1)` even though the server process is alive. This is the same class
of mislabel as the ClickHouse mid-stream false-crash. The adapter should check server liveness
before declaring a crash, and either surface "engine returned invalid UTF-8" as a wrong-result/error
finding or read the bytes without mandatory utf8 decoding. All 5 dolt_run2 "crashes" are this
mislabel over the RPAD/LPAD bug above.
