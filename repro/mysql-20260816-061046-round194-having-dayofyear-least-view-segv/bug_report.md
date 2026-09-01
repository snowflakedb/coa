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

# MySQL: merged CAST DATE view + aliased `HAVING MIN(dec) <= DAYOFYEAR(LEAST(date, date))` SIGSEGVs in `Date_val::day_number`

## Summary

A mergeable view (or derived table) whose `DATE` column is an expression — `CAST(c_date AS DATE)`, a tautology `CASE`, or a `JSON_EXTRACT` unpack — SIGSEGVs when queried as

```sql
SELECT t1.c_date FROM t t1
GROUP BY t1.c_date
HAVING MIN(t1.c_dec) <= DAYOFYEAR(LEAST(t1.c_date, t1.c_date));
```

The same statement over the base table, an identity view, `ALGORITHM=TEMPTABLE`, or with `derived_merge=off` returns the one expected row. `EXPLAIN` succeeds and shows the view fully inlined (`least(cast(b.c_date as date), cast(b.c_date as date))`); on **9.7.2** the crash is in **execution**, while `copy_funcs` materializes the HAVING expression into the aggregate temp table. On **8.4.10** the same SQL does not crash: it returns 0 rows (still wrong).

The faulting frame is `Date_val::day_number()` (`mysys/my_temporal.cc`), called from `Item_func_dayofyear::val_int()` (`sql/item_timefunc.cc:1433`). `day_number` indexes `sum_days[month() - 1]` with no release-build bounds check; a `Date_val` whose `month()` is 0 or >12 is an unmapped-address SIGSEGV. `YEAR(LEAST(…))` and `MONTH(LEAST(…))` on the same CAST view do **not** crash — only `DAYOFYEAR` takes that path.

This is the crash cousin of the already-filed view-merge HAVING neighbourhood: omitting `t1.c_date` from the SELECT list is 1054 (`having-max-view-expr-1054`); `HAVING MIN <= DAYOFYEAR(t1.c_date)` without `LEAST`/`GREATEST` drops the group (0 rows vs 1); `IFNULL`/`COALESCE` of the date column drop the group the same way as `case-view-having-ifnull`. `LEAST`/`GREATEST` of two `DATE`s is what turns that rewrite into a SIGSEGV.

## Environment

- **Version:** `VERSION()` = `9.7.2` (Docker image `mysql:9.7.2`, Community Server GPL). `BuildID[sha1]=9a4505a7aae969ddb2e30d8250a3eb6ad5839433`. Assertions off (official image). **The SIGSEGV does not fire on `mysql:8.4.10`** (same distilled SQL): 8.4.10 returns **0 rows** (wrong; heap is 1 row) instead of crashing. The 1054 sibling and the no-`LEAST` dropped-group sibling are present on both 8.4.10 and 9.7.2. Crash is a 9.x execution failure of a rewrite 8.4 already gets wrong.
- **`sql_mode`:** `STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES` (harness default; not load-bearing — crash reproduces without touching `sql_mode`).
- **charset / collation:** `utf8mb4` / `utf8mb4_0900_bin`.
- **Access path:** pymysql against Docker `mysqld`. Client sees `OperationalError 2013 Lost connection`; the server error log is `mysqld got signal 11`. The eqgen adapter then SIGABRTs the **worker** (`_maybe_abort` on 2013), which is why hunt files are labeled `crash: killed by SIGABRT`.

## Minimal repro

See [`reduced.sql`](./reduced.sql) PART 2. In a fresh database:

```sql
CREATE TABLE b (c_date DATE, c_dec DECIMAL(10,2));
INSERT INTO b VALUES ('1999-12-31', -5.5);
CREATE VIEW t AS
  SELECT CAST(c_date AS DATE) AS c_date,
         CAST(c_dec AS DECIMAL(10,2)) AS c_dec
  FROM b;

SELECT t1.c_date FROM t t1
GROUP BY t1.c_date
HAVING MIN(t1.c_dec) <= DAYOFYEAR(LEAST(t1.c_date, t1.c_date));
```

## Expected vs actual

`-5.5 <= DAYOFYEAR('1999-12-31')` is `-5.5 <= 365`, TRUE. One group, one row.

| Query | Expected | Actual 9.7.2 | Actual 8.4.10 |
|---|---|---|---|
| distilled CAST view (PART 2) | 1 row `1999-12-31` | **SIGSEGV** (signal 11, address not mapped) | **0 rows** (no crash) |
| JSON-unpack view (PART 1) | 1 row | **SIGSEGV** | not retested |
| `CASE WHEN TRUE THEN c_date ELSE CAST(NULL AS DATE) END` view | 1 row | **SIGSEGV** | not retested |
| inline `FROM (SELECT CAST(c_date AS DATE) AS c_date, … FROM b) t1` (no VIEW) | 1 row | **SIGSEGV** | not retested |
| `ALGORITHM=MERGE` CAST view | 1 row | **SIGSEGV** | not retested |
| `DAYOFYEAR(GREATEST(t1.c_date, t1.c_date))` | 1 row | **SIGSEGV** | not retested |
| `DAYOFYEAR(LEAST(t1.c_date, DATE '1999-12-31'))` | 1 row | **SIGSEGV** | not retested |
| heap / identity view / `ALGORITHM=TEMPTABLE` / `derived_merge=off` / unprefixed `FROM t` (C1–C5) | 1 row | 1 row | 1 row (heap checked) |
| `YEAR(LEAST(…))` / `MONTH(LEAST(…))` (C6) | 1 row | 1 row | not retested |
| `HAVING MIN(c_dec) <= DAYOFYEAR(t1.c_date)` (no LEAST, C7) | 1 row | **0 rows** (wrong result, no crash) | **0 rows** |
| `SELECT 1 … HAVING … DAYOFYEAR(LEAST(…))` (C8) | 1 row | **1054** Unknown column `t1.c_date` in HAVING | **1054** |

The **equivalent** (merged expression-DATE relation) is the crashing side on 9.7.2 and the 0-row side on 8.4.10. The **base table** is correct on both.

## Equivalence construction

`crash_round194_0.sql` (seed 598238259) built equivalent `t` as a JSON-unpack view over a builder chain. The original workload was a ~3k-char `SELECT` with a framed window `SUM(if(…)) OVER (PARTITION BY c_date … ROWS UNBOUNDED)`, `GROUP BY c_date, c_ts`, a `WHERE`, and `HAVING MIN(c_dec) <= greatest(DAYOFYEAR(LEAST(c_date,c_date)), IFNULL('15'*CEIL('-12345678'), CAST(MIN(c_int) AS SIGNED)), 1, '0')`.

- **Load-bearing construct:** a mergeable expression-valued `DATE` column (`CAST` / tautology `CASE` / `JSON_UNQUOTE(JSON_EXTRACT(…))` / derived-table CAST).
- **It is a composition:** that merged DATE **×** table alias in `GROUP BY`/`HAVING` **×** `HAVING MIN(<numeric>) <= DAYOFYEAR(LEAST|GREATEST(<date>, <date-or-literal>))` **×** projecting `t1.c_date` in the SELECT list. Drop any one and the SIGSEGV goes away (replaced, for some drops, by the 1054 or dropped-group siblings).
- **Reduced away:** the window `SUM`, `WHERE`, extra SELECT items, `GREATEST`/`IFNULL`/`CEIL` around `DAYOFYEAR`, `GROUP BY c_ts`, JSON packing (CAST is enough), all but one row.

`EXPLAIN FORMAT=TREE` of the distilled crashing query (planning succeeds):

```
-> Filter: (min(cast(b.c_dec as decimal(10,2))) <= dayofyear(least(cast(b.c_date as date),cast(b.c_date as date))))
    -> Table scan on <temporary>
        -> Aggregate using temporary table
            -> Table scan on b
```

View merge has already substituted `cast(b.c_date as date)` into both `LEAST` arguments. `SET optimizer_switch='derived_merge=off'` keeps the view as a materialization and the query returns 1 row.

## Minimal oracle exposure path

- **Object composition arity:** **2**
- **GCL builder path:** `MySqlJsonPackRoundTripBuilder` → `VIEW` realization
- **Confidence:** Verified — the report identifies the JSON-unpack builder output and the GCL implementation's final view realization.
- **Realization:** The builder internally CTASes the source and exposes the unpacked `DATE` expression through a mergeable `VIEW`.
- **Workload/data requirements (excluded from arity):**
  - An expression-valued `DATE` column and a table alias in `GROUP BY`/`HAVING`.
  - `HAVING MIN(numeric) <= DAYOFYEAR(LEAST|GREATEST(date, ...))`.
  - Projection of the aliased date column; MySQL 9.7.2 is required for the SIGSEGV symptom.

**Exposure vs. intrinsic trigger:** The two-factor path supplied the merged expression-valued date that exposed the failure. JSON is incidental: the intrinsic trigger is the merged `DATE` expression plus the aliased aggregate `HAVING`/`DAYOFYEAR` evaluation shape, which also reproduces with `CAST`, `CASE`, or an inline derived table.

## Characterization

Verified against `mysql:9.7.2` and `mysql:8.4.10`.

**Required for the SIGSEGV**

1. Merge of an expression `DATE` (CAST / CASE / JSON unpack / derived CAST). Identity view and heap do not crash.
2. Table alias (`t t1` or `t AS t1`) on `GROUP BY`/`HAVING`. Unprefixed `FROM t` does not crash.
3. `HAVING MIN(c_dec) <= DAYOFYEAR(LEAST(date, date))` or `GREATEST(date, date)` or `LEAST(date, DATE '…')`. `LEAST(date, NULL)` does not crash.
4. `DAYOFYEAR` specifically. `YEAR`/`MONTH` of the same `LEAST` are correct. `LEAST(DAYOFYEAR(date), DAYOFYEAR(date))` does not crash.
5. Projecting `t1.c_date` (or `SELECT *`). `SELECT 1` / `SELECT MIN(c_dec)` is 1054, not a crash.
6. `MIN` in the HAVING comparison. `HAVING DAYOFYEAR(LEAST(…)) >= 0` and `HAVING LEAST(…) IS NOT NULL` do not crash.
7. `DATE` typed column. `CAST(c_date AS CHAR(255))` + `DAYOFYEAR(LEAST(…))` does not crash.

**Sibling symptoms on the same CAST view + aliased HAVING** (not this crash, same neighbourhood):

- `HAVING MIN(c_dec) <= DAYOFYEAR(t1.c_date)` → 0 rows (heap: 1).
- `HAVING MIN(c_dec) <= DAYOFYEAR(IFNULL(t1.c_date, t1.c_date))` → 0 rows (heap: 1).

**Not a planner crash.** `EXPLAIN` of the crashing statement returns the TREE above. `copy_funcs` then evaluates `DAYOFYEAR` while filling the aggregate temp table.

**Source (mysql-9.7, `sql/item_timefunc.cc:1433` and `mysys/my_temporal.cc:456`):**

```cpp
longlong Item_func_dayofyear::val_int() {
  Date_val date;
  if (val_arg0_date(&date, TIME_ONLY_VALID_DATES)) {
    return 0;   // error / NULL → no day_number()
  }
  return date.day_number() - Date_val{date.year(), 1, 1}.day_number() + 1;
}

uint32_t Date_val::day_number() const {
  assert(month() != 0 && day() != 0);          // debug-only
  // ...
  return ... + sum_days[month() - 1] + day() - ...;
}
```

`sum_days` is a 13-entry table (`my_temporal.cc:49`). Release `mysqld` has no `assert`. `val_arg0_date` on the merged `LEAST(cast(date), cast(date))` reports success with a `Date_val` whose `month()` is not in 1..12; `sum_days[month()-1]` is then an OOB read (`Address not mapped to object` at `0x402748e2c` on this run).

DML not tested. `UPDATE`/`DELETE` with this HAVING shape is not a typical write path; the crash is SELECT execution.

### Full stack trace

Official `mysql:9.7.2` image, stripped of DWARF (`.gnu_debuglink` present, no `.debug_info`). Function names from `.dynsym` via `addr2line -f -C` on `/usr/sbin/mysqld` copied out of the image (`BuildID[sha1]=9a4505a7aae969ddb2e30d8250a3eb6ad5839433`). No file:line. Frame 6 (`Query_dumpvar::send_eof`) is a nearest-symbol artefact on a stripped binary — ignore it. Frames 14 similarly. A local RelWithDebInfo `mysqld` exists on this box but cannot be executed (its Nix dynamic linker is gone); it was **not** used to resolve these PCs (different BuildID).

Server error log (untrimmed body of the distilled crash):

```
2026-08-16T17:33:52Z UTC - mysqld got signal 11 ;
Signal SIGSEGV (Address not mapped to object) at address 0x402748e2c
Most likely, you have hit a bug, but this error can also be caused by malfunctioning hardware.
BuildID[sha1]=9a4505a7aae969ddb2e30d8250a3eb6ad5839433
Thread pointer: 0xffff04035800
Attempting backtrace. You can use the following information to find out
where mysqld died. If you see no messages after this, something went
terribly wrong...
stack_bottom = ffff6439e550 thread_stack 0x100000
 #0 0x17b52b7 <unknown>
 #1 0xffffacf49837 <unknown>
 #2 0x1403b3c <unknown>
 #3 0x8b547b <unknown>
 #4 0xbdb41f <unknown>
 #5 0xd3345f <unknown>
 #6 0xd9c08f <unknown>
 #7 0xcb2017 <unknown>
 #8 0xcd3e8f <unknown>
 #9 0xbf5a8b <unknown>
 #10 0xaa9ee3 <unknown>
 #11 0xca892b <unknown>
 #12 0xca2c1f <unknown>
 #13 0xc9ed17 <unknown>
 #14 0xc85d3f <unknown>
 #15 0xf582d7 <unknown>
 #16 0xffffaaba20e7 <unknown>
 #17 0xffffaac0c8db <unknown>
 #18 0xffffffffffffffff <unknown>

Trying to get some variables.
Some pointers may be invalid and cause the dump to abort.
Query (ffff04f22cf0): SELECT t1.c_date FROM t t1 GROUP BY t1.c_date HAVING MIN(t1.c_dec) <= DAYOFYEAR(LEAST(t1.c_date, t1.c_date))
Connection ID (thread ID): 14
Status: NOT_KILLED
```

Resolved (function names only):

```
 #0  handle_fatal_signal(int, siginfo_t*, void*)
 #1  <libc sigreturn>
 #2  Date_val::day_number() const                          ← fault
 #3  Item_func_dayofyear::val_int()
 #4  Item::save_in_field_inner(Field*, bool)
 #5  copy_funcs(Temp_table_param*, THD const*, Copy_func_type)
 #6  Query_dumpvar::send_eof(THD*)                         ← nearest-symbol junk
 #7  Query_expression::ExecuteIteratorQuery(THD*)
 #8  Sql_cmd_dml::execute_inner(THD*)
 #9  Sql_cmd_dml::execute(THD*)
 #10 mysql_execute_command(THD*, bool)
 #11 dispatch_sql_command(THD*, Parser_state*, bool)
 #12 dispatch_command(THD*, COM_DATA const*, enum_server_command)
 #13 do_command(THD*)
 #14 System_variable_tracker::System_variable_tracker(...) ← nearest-symbol junk
 #15 pfs_spawn_thread_vc(...)
```

## How it was found

Eqgen data-equivalence oracle, `mysql_rich_shuffle2` / `mysql_20260816-061046` round 194 seed 598238259, `crash_round194_0.sql`. The oracle held the query fixed and swapped in a row-identical JSON-unpack view; the base table runs the query, the view kills `mysqld`. A query-rewrite oracle over the heap never builds that view.

Same crashing query (byte-identical) also as `crash_round{274,310,348,506,654}_0.sql`. `crash_round713_0.sql` is SIGTERM from stopping the hunt; `crash_round714_0.sql` is teardown noise. 44 `mismatch_*.sql` files in the same run are headered `crash: killed by SIGABRT` — worker abort after this SIGSEGV, not a row mismatch.

## Open items

- No debug (`WITH_DEBUG=1`) stack with file:line. Official image is stripped; the local debug `mysqld` on this host cannot run (missing Nix glibc). `Date_val::day_number` / `Item_func_dayofyear::val_int` are identified from `.dynsym` + the 9.7 source tree, not from a live debug `bt`.
- Why `val_arg0_date` reports success for merged `LEAST(cast(date), cast(date))` with a `Date_val` whose `month()` is out of range is not pinned to a single rewrite in `sql_resolver.cc`. `EXPLAIN` shows the substitution; the corrupt temporal is at eval time in `copy_funcs`.
- First Innovation / 9.x release that turns the 8.4 0-row rewrite into a SIGSEGV is not pinned (8.4.10 no crash, 9.7.2 crash). `YEAR`/`MONTH` of the same `LEAST` succeeding while `DAYOFYEAR` dies is consistent with `day_number`'s `sum_days[month()-1]` but was not confirmed by inspecting the `LEAST` item's `MYSQL_TIME` in a debugger.
