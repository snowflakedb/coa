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

# Dolt: `REVERSE(<numeric column>)` declares the numeric type on the wire but sends a string, breaking typed clients

## Summary

`SELECT REVERSE(c_int) FROM t` where `c_int BIGINT` returns the string `'1-'` for the value `-1`, but
Dolt declares the result column as **LONGLONG** in the result-set metadata. Any client that honours the
declared type therefore fails to decode the row — pymysql raises
`ValueError: invalid literal for int() with base 10: '1-'`, and the connection is left desynced
(`Packet sequence number wrong` on the next query). MariaDB 11.4 declares **VAR_STRING** for the same
expression and returns `'1-'` cleanly. The bug is in the return-type derivation for `REVERSE`: an
explicit `CAST(... AS CHAR)` around the argument produces a string type and works, and `CONCAT` over the
same column already declares a string type, so this is specific to `REVERSE` rather than a general rule
about string functions over numerics.

## Environment

| | |
|---|---|
| Engine | `dolt version 2.2.3` (server reports `VERSION()` = `8.0.31`, its MySQL compatibility string) |
| Reference | MariaDB `11.4.12-MariaDB-ubu2404` (docker `mariadb:11.4`) — declares `VAR_STRING`, decodes fine |
| Access path | `dolt sql-server` wire protocol. Text-only clients (`dolt sql`, mariadb CLI) print `1-` happily because they never apply the declared type — so the bug is invisible there |
| Session | all defaults; `sql_mode`/collation not load-bearing |
| Regression window | not determined (only one Dolt build available) |

## Minimal repro

```sql
CREATE TABLE t (c_int BIGINT);
INSERT INTO t VALUES (-1), (0), (NULL);
SELECT REVERSE(c_int) FROM t;
```

Dolt: declared wire type `LONGLONG`, values `'0'`, `NULL`, `'1-'` → typed client raises on `'1-'`.
MariaDB: declared wire type `VAR_STRING`, values `'1-'`, `'0'`, `NULL` → fine.

## Expected vs actual

**Dolt is the wrong side** — established against MariaDB as a reference engine, and internally: Dolt's
own `REVERSE(CAST(c_int AS CHAR))` and `CONCAT(c_int)` both declare a string type for the same data.

| Query | MariaDB (reference) | Dolt |
|---|---|---|
| `SELECT REVERSE(c_int) FROM t` | `VAR_STRING`, `('1-'),('0'),(NULL)` | **`LONGLONG`, client cannot decode `'1-'`** |
| `SELECT REVERSE(CAST(c_int AS CHAR)) FROM t` | `VAR_STRING`, ok | `BLOB`, ok |
| `SELECT CONCAT(c_int) FROM t` | `VAR_STRING`, ok | `BLOB`, ok |
| `SELECT LOWER(c_int) FROM t` | `VAR_STRING`, `('-1'),('0'),(NULL)` | `LONGLONG`, `(-1),(0),(NULL)` — self-consistent but returns a *number* where MySQL returns a string |

`LOWER` is a second, milder type-derivation difference: it does not break the client because the value
matches the declared type, but the result type differs from MySQL/MariaDB. Worth fixing in the same
pass; it is not what the fuzzer tripped over.

## Why the fuzzer reported this as a CRASH (it is not one)

The two findings behind this report are recorded as `crash_round0_0.sql` and `crash_round6_0.sql` with
`-- crash: exited with status 1`. **The engine never died.** The chain is:

1. Dolt sends a `LONGLONG` column containing `'1-'`.
2. pymysql's int converter raises `ValueError` **inside `cursor.execute`**.
3. eqgen's `Database.query` catches `adapter.db_error` (pymysql.Error), `TypeError` and
   `UnicodeDecodeError` — but **not `ValueError`** — so it escapes and kills the round worker.
4. The parent's `_crash_note()` reports the *worker's* exit status, and the finding is written as an
   engine crash.

Evidence the server was alive throughout: the run's own dolt log
(`/tmp/eqgen-dolt-3o0mjkbq/server.log`, port 49431 — the port in all the crash banners) contains exactly
**one** `Starting server` line, and the log is opened in append mode, so a restart would have added
another. `SELECT VERSION()` still answers after all three "crashes". Both findings' queries, replayed
against a private server with the exact base and equivalent blocks, return rows and leave the server up.

So the crash label is a harness misclassification. See *Harness notes*.

## Characterization

`reduced.sql` has five blocks; each was run against Dolt **and** MariaDB, checking the declared wire
type plus whether the client can decode. All Dolt blocks behave as documented.

| Ingredient | Control |
|---|---|
| the argument is numeric | `REVERSE(CAST(c_int AS CHAR))` → string type, ok |
| the function is `REVERSE` | `CONCAT(c_int)` → string type, ok |
| the reversal is not a valid integer | `REVERSE(0)` = `'0'` parses fine, so a table without a negative value hides the bug entirely — data with a `-` sign is the reliable trigger |
| the client honours the declared type | mariadb CLI / `dolt sql` print `1-` and never notice |

Not established: the source location. The place to look is `REVERSE`'s return-type derivation in
go-mysql-server (it should be a text type regardless of argument type, as `CONCAT`'s already is).

## Minimal oracle exposure path

- **Object composition arity:** `0`.
- **GCL builder path:** none — no equivalence object contributes to the failure.
- **Confidence:** high; the report explicitly confirms identical behavior on both oracle sides.
- **Realization:** none; a plain table query is sufficient.
- **Workload/data requirements (excluded from arity):** `REVERSE` over a numeric column, a negative value
  whose reversed text is not parseable as an integer, and a typed wire-protocol client are
  workload/data/client conditions.
- **Exposure vs. intrinsic trigger:** there is no object contrast: both sides fail identically in the
  client. The intrinsic trigger is inconsistent result metadata and payload for numeric `REVERSE`,
  independent of equivalence construction.

## How it was found

The eqgen data-equivalence oracle ran `SELECT ((t1.c_int)%(t1.c_int)), REVERSE(t1.c_int), t1.c_dec ...`
against two row-identical relations. The finding is not a divergence between the two sides at all —
**both sides fail identically** — which is worth noting about the oracle: here it acted as a plain
random-query fuzzer, and the value was simply that it generated `REVERSE` over a `BIGINT` column with a
negative value in it. The equivalence machinery contributed nothing to this one; the harness's
exception handling is what turned it into a misleading crash report.

* Rounds 0 and 6, seeds 1414262907 and 301909642
* Reduced repro: [`reduced.sql`](reduced.sql)
* Original findings: `dolt_20260809-052933/crash_round0_0.sql`, `crash_round6_0.sql`

## Harness notes (eqgen, not Dolt)

1. **`ValueError` is not caught, and it is reported as an engine crash.** `Database.query`
   (`eqgen/fuzz/database.py`) catches `db_error`, `TypeError`, `UnicodeDecodeError`. A driver
   *type-conversion* failure raises `ValueError`, which escapes and is misreported as the engine dying.
   Fix: catch `ValueError` alongside `UnicodeDecodeError` — the existing comment there already argues
   the right principle ("a genuine engine defect but *not* a crash") and even calls
   `_reset_after_decode_failure()` to recover the desynced socket, which this path needs too.
2. **A client-visible lost connection is treated as proof the engine died.**
   `_maybe_abort` (`eqgen/dialects/mysql/adapter.py:212`) raises `SIGABRT` in the worker whenever the
   client errno is in `_LOST_CONNECTION_ERRNOS = {2006, 2013, 2055, 1053, 1077}`. But **1053 is
   `ER_SERVER_SHUTDOWN`, which is also what the harness's own `KILL QUERY` watchdog produces**, and
   **2013 also occurs when the server raises an error mid-result-set** — which Dolt does: the same run's
   log shows `Error in the middle of a stream to client 80: DOUBLE out of range for COT`, and
   `crash_round17_0.sql` (the `COT(t1.c_int)` query) raises errno 2013 on *both* sides while the server
   stays up. Suggested fix: before declaring a crash, probe liveness (reconnect and `SELECT 1`) and only
   report a crash if the server is actually gone; record "error mid-stream" and "killed by watchdog" as
   their own outcome kinds.
3. The version banner records `dolt 8.0.31` (MySQL compat string) instead of `dolt version 2.2.3`, and
   the base block omits fork DDL — both already reported from earlier runs.
