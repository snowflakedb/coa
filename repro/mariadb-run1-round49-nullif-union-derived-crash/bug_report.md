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

# MariaDB: assertion crash in `Item_func_nullif::fix_length_and_dec` — NULLIF in WHERE over a UNION ALL derived table

## Summary

A `NULLIF` in a `WHERE` clause over a **materialized / `UNION ALL` derived table** crashes the
server (debug build) with an assertion failure in the optimizer. `NULLIF(a,b)` is internally
rewritten to `CASE WHEN a=b THEN NULL ELSE a END`, and `fix_length_and_dec` asserts that the
rewritten form's `args[0]` and `args[2]` are the same `Item`. When the enclosing derived table /
view is re-optimized (`mysql_derived_optimize` → `JOIN::optimize` → `Item_func::fix_fields`), the
two args are no longer identical and it is not a prepared-statement execute, so the assertion trips.

**This is a duplicate of the open upstream bug [MDEV-19091](https://jira.mariadb.org/browse/MDEV-19091)**
(Confirmed / Unresolved; fix versions 10.11, 11.4, 11.8, 12.3), which has the same assertion and the
same shape (`NULLIF` in a WHERE over an `ALGORITHM=TEMPTABLE` view). Our repro reaches it via a
`UNION ALL` derived table, which forces the same materialization. **Do not file a new upstream bug;
link MDEV-19091.**

## Environment

- **Version:** `13.1.0-MariaDB-debug`, source revision `cded2b25e65853a75c2213cfe0832819832708bd` (main, assertions on)
- **Assertion:** `sql/item_cmpfunc.cc:2758` — `Item_func_nullif::fix_length_and_dec`: `args[0] == args[2] || thd->stmt_arena->is_stmt_execute()`
- **Signal:** 6 (SIGABRT). `sql_mode`/charset/collation are the harness defaults and immaterial (crash is at optimize time).

## Minimal repro

```sql
CREATE TABLE r (id BIGINT);
INSERT INTO r VALUES (0);

SELECT id FROM (SELECT id FROM r UNION ALL SELECT id FROM r WHERE 0) t
WHERE NULLIF('12', id) = id;
```

## Expected vs actual

| | Expected | Actual |
|---|---|---|
| the SELECT above | 0 or 1 rows | **server SIGABRT** (lost connection; assertion in `Item_func_nullif::fix_length_and_dec`) |

## Equivalence construction

The equivalent `t` was built as a **FULL-OUTER-JOIN emulation** (MariaDB has no `FULL JOIN`): a
`ROW_NUMBER()` CTAS (`t__base_table_1`) split into two projected views (`v1(id,k)`, `v2(name,created_at,k)`)
recombined as `v1 LEFT JOIN v2 … UNION ALL v1 RIGHT JOIN v2 … WHERE NOT EXISTS(…)`.

- **Load-bearing construct:** the `UNION ALL` derived table / materialized view (the union round-trip
  inside the FULL-join emulation). It is what forces the derived-table re-optimization that re-fixes
  the `NULLIF`.
- **It is a composition, not a single construct:** `UNION ALL` derived table **×** `NULLIF` in the
  `WHERE`. Either alone is fine — a plain derived table doesn't crash, and `NULLIF` on the base table
  doesn't crash. The interacting pair is the bug.
- **Reduced away (not needed):** the `ROW_NUMBER()` window CTAS, the two split views, the FULL-join
  RIGHT-anti branch, and all but one row — a bare `SELECT id FROM r UNION ALL SELECT id FROM r WHERE 0`
  derived table suffices.

## Minimal oracle exposure path

- **Object composition arity:** **1**
- **GCL builder path:** `UnionEmptyRoundTripBuilder` (**inferred mapping**)
- **Confidence:** Inferred — the reduced SQL is exactly `R UNION ALL empty(R)`, but the report's historical FULL-join-emulation chain does not retain AST metadata proving that class was selected.
- **Realization:** The builder's union query is consumed as a materialized derived relation; no separate final `Create*Builder` is required by the minimal mapping.
- **Workload/data requirements (excluded from arity):**
  - `NULLIF` in a `WHERE` comparison over the union-derived relation.
  - An assertions-enabled affected MariaDB build for the reported SIGABRT.
  - One row of otherwise immaterial data.

**Exposure vs. intrinsic trigger:** The inferred union-empty builder is the smallest GCL-shaped exposure of the optimizer path. The intrinsic trigger is `NULLIF` being re-fixed while a materialized/`UNION ALL` derived relation is optimized; the original FULL-join emulation, row-number CTAS, and split views are not required.

## Characterization (verified against the build)

All three ingredients are individually necessary — removing any one avoids the crash:
- **`NULLIF`** specifically — replacing it with `COALESCE('12', id) = id` does **not** crash.
- **A `UNION ALL` derived table / materialized view** — a plain derived table `(SELECT id FROM r)`
  does **not** crash; neither does the base table directly.
- **`NULLIF` evaluated over that derived relation** — `NULLIF` on the base table does not crash.

`NULLIF` in the `SELECT` list (rather than a WHERE comparison) over the same view does not crash;
it is the WHERE/optimizer path that re-fixes the item. One row is sufficient; the row values are
immaterial.

Full stack trace (from the server error log; `mysqld got signal 6`, resolved by the debug build's
built-in `my_print_stacktrace` + addr2line — MariaDB frames carry file:line):

```
mysqld: sql/item_cmpfunc.cc:2758: virtual bool Item_func_nullif::fix_length_and_dec(THD*):
        Assertion `args[0] == args[2] || thd->stmt_arena->is_stmt_execute()' failed.
mysqld got signal 6 ;

mysys/stacktrace.c:216(my_print_stacktrace)
sql/signal_handler.cc:230(handle_fatal_signal)
libc.so.6(+0x94c6c)                                    <-- __kernel_rt_sigreturn
libc.so.6(gsignal+0x18)
libc.so.6(abort+0x28)
libc.so.6(+0x349fc)                                    <-- __assert_fail
                                                       (Item_func_nullif::fix_length_and_dec, item_cmpfunc.cc:2758 — inlined at the assert)
sql/item_func.cc:412(Item_func::fix_fields(THD*, Item**))
sql/item_func.cc:394(Item_func::fix_fields(THD*, Item**))
sql/sql_select.cc:2430(JOIN::optimize_inner())
sql/sql_select.cc:2016(JOIN::optimize())
sql/sql_union.cc:2320(st_select_lex_unit::optimize())
sql/sql_derived.cc:1092(mysql_derived_optimize(THD*, LEX*, TABLE_LIST*))
sql/sql_derived.cc:235(mysql_handle_single_derived(LEX*, TABLE_LIST*, unsigned int))
sql/sql_select.cc:2583(JOIN::optimize_inner())
sql/sql_select.cc:2016(JOIN::optimize())
sql/sql_select.cc:5425(mysql_select(...))
sql/sql_select.cc:636(handle_select(THD*, LEX*, select_result*, unsigned long long))
sql/sql_parse.cc:6217(execute_sqlcom_select(THD*, TABLE_LIST*))
sql/sql_parse.cc:5530(mysql_execute_command(THD*, bool))
sql/sql_parse.cc:1916(dispatch_command(enum_server_command, THD*, char*, unsigned int, bool))
sql/sql_parse.cc:1437(do_command(THD*, bool))
sql/sql_connect.cc:1510(do_handle_one_connection(CONNECT*, bool))
sql/sql_connect.cc:1424(handle_one_connection)
perfschema/pfs.cc:2201(pfs_spawn_thread)
libc.so.6 (thread start)
```

Crashing query (from the same dump): `SELECT id FROM (SELECT id FROM r UNION ALL SELECT id FROM r
WHERE 0) t WHERE NULLIF('12', id) = id`; `Status: NOT_KILLED`. The `sql/sql_string.h:833
(String::~String())` line that appears between `abort` and `fix_fields` in the raw log is an
addr2line misattribution of the `__assert_fail`/`fix_length_and_dec` frame (inlined debug code), not
a real frame — the assertion site is `Item_func_nullif::fix_length_and_dec` as named in the abort
message.

(Captured on this build by launching the reduced repro against the debug server and reading the
error log; the debug build's built-in handler resolves the backtrace on `SIGABRT`.)

## How it was found

The eqgen differential fuzzer (equivalence oracle) ran a workload query against a base table and
against a row-identical rewrite whose `t` was a FULL-OUTER-JOIN emulation
(`LEFT … UNION ALL … RIGHT … WHERE NOT EXISTS`). The query used `NULLIF('12', t2.id)` in a
`WHERE … IN (…)`; the rewrite's `UNION ALL`-materialized `t` triggered the assertion while the plain
base table did not. Reduced to the 3-line case above by execution-guided delta-debugging.

- Original finding: hunt log
- Reduced repro: `reduced.sql` (this folder)
- Fuzzer seed: `1088414189`
