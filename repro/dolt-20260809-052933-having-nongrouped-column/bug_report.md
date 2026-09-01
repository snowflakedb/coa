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

# Dolt: accepts `HAVING` referencing a non-grouped, non-aggregated column that MySQL/MariaDB reject (ERROR 1054)

## Summary

MySQL and MariaDB resolve `HAVING` against the *grouped* output: it may reference `GROUP BY` columns,
aggregates, and select-list aliases, and a bare non-grouped column is an error —
`ERROR 1054 Unknown column 'b' in 'HAVING'`. Dolt rejects that too in the simplest shape, but accepts
it when an aggregate appears beside the bare column in the `HAVING` predicate (`HAVING MAX(b) > b`), and
in several other shapes the fuzzer produced. Dolt then invents a value for the bare column, so the same
query over two row-identical relations can return different rows. This is a **compatibility divergence**,
not a wrong-result bug in the usual sense: the query has no MySQL-defined meaning, so there is no
correct answer to compare against.

## Environment

| | |
|---|---|
| Engine | `dolt version 2.2.3` (server reports `VERSION()` = `8.0.31`) |
| Reference | MariaDB `11.4.12-MariaDB-ubu2404` (docker `mariadb:11.4`) — rejects with 1054 |
| Session | all defaults; **also accepted with `sql_mode='ONLY_FULL_GROUP_BY'`**, so strict mode does not catch it |

## Minimal repro

```sql
CREATE TABLE t (g BIGINT, b BIGINT);
INSERT INTO t VALUES (1,10),(1,20),(2,30);

-- `b` is neither grouped nor aggregated. MySQL/MariaDB: ERROR 1054. Dolt: accepted, returns a result.
SELECT g, COUNT(*) FROM t GROUP BY g HAVING MAX(b) > b;
```

| Query | MariaDB 11.4 | Dolt 2.2.3 |
|---|---|---|
| `... GROUP BY g HAVING MAX(b) > b` | **ERROR 1054** | **accepted** (returns `()` here) |
| `... GROUP BY g+0 HAVING MAX(b) > b` | ERROR 1054 | **accepted** |
| `... GROUP BY g HAVING b > 15` | ERROR 1054 | ERROR 1105 — both reject |
| `... GROUP BY g+0 HAVING b > 15` | ERROR 1054 | ERROR 1105 — both reject |
| `... GROUP BY g HAVING g > 1` (legal) | `(2,1)` | `(2,1)` — agree |
| `... GROUP BY g HAVING COUNT(*) > 1` (legal) | `(1,2)` | `(1,2)` — agree |

The distinguishing ingredient in the minimal case is **an aggregate beside the bare column inside
`HAVING`**: with one present Dolt keeps the underlying relation's columns in scope and resolves `b`;
without one it correctly reports the column out of scope.

## Why this matters beyond strictness

Because Dolt supplies *some* value for the bare column, the query becomes sensitive to which row of the
group that value comes from. Running such a query against two relations holding identical rows through
different plan shapes therefore yields different results — which is how the fuzzer surfaced it, 11 times
in one run (see below). MySQL never has to define this because it rejects the query up front.

Related and legitimate, for contrast: a bare non-grouped column in the **SELECT** list *is* allowed with
`ONLY_FULL_GROUP_BY` off, and there the two engines legitimately disagree —
`SELECT g, b FROM t GROUP BY g` gives MariaDB `(1,10),(2,30)` and Dolt `(1,20),(2,30)`. That is
documented-arbitrary in both engines and is **not** part of this report.

## Minimal oracle exposure path

- **Object composition arity:** `2`.
- **GCL builder path:** `SelectStarQueryBuilder → CreateViewBuilder` *(historical/inferred)*.
- **Confidence:** inferred; both class names are registered in the current factory, but the report
  does not retain exact historical builder-selection metadata for the 11 findings.
- **Realization:** inferred workload-facing view created by `CreateViewBuilder`.
- **Workload/data requirements (excluded from arity):** the aggregate query, bare non-grouped column in
  `HAVING`, group contents, and whichever pre-group row Dolt resolves are workload/data conditions.
- **Exposure vs. intrinsic trigger:** the inferred object path varied plan shape and made Dolt's
  invented bare-column value diverge. The intrinsic compatibility defect is accepting and resolving
  the invalid `HAVING` reference; no particular equivalence object is required.

## How it was found — and what it explains

Triaging `dolt_20260809-052933`, 11 of the 19 mismatch findings shared one property: the
workload query's `HAVING` referenced a bare non-grouped, non-aggregated column, and **MariaDB rejects
all 11 with ERROR 1054**:

| finding | `HAVING` clause | diff |
|---|---|---|
| round1_0 | `VAR_SAMP(t2.c_chr) <= t2.c_big` | base-only 1 |
| round11_0 | `MIN(t1.c_int) IN (t1.c_int)` | equiv-only 1 |
| round16_1 | `CASE t0.c_txt WHEN MAX(t0.c_txt) THEN false ELSE true END` | equiv-only 1 |
| round16_2 | `CASE t0.c_txt WHEN t0.c_txt ... END` | equiv-only 1 |
| round19_0 | `VAR_SAMP(t0.c_big) > t0.c_pk` | base-only 3 |
| round23_0 | `t0.c_txt IS NULL` | equiv-only 9 |
| round23_1 | `t2.c_chr >= '\e'` | base-only 4 |
| round25_0 | `t0.c_int >= MAX(t0.c_int)` | equiv-only 2 |
| round26_0 | `t2.c_txt < 'N''n∟Z亅F뭍'` | base-only 5 |
| round31_0 | `t0.c_chr <= 1262771866` | equiv-only 2 |
| round32_0 | `t0.c_txt = t0.c_chr` | base-only 1 |

All 11 pass the oracle's admissibility and type-equivalence gates and are individually deterministic,
so they look like clean engine bugs — but they are not, because the query is not valid MySQL. They
should be counted as **one** compatibility defect plus a harness comparability gap, not 11 findings.

Two hypotheses I tested and **rejected**, so nobody repeats them:

* *"The bare column takes an arbitrary row's value, so the query is underdetermined."* Rebuilding the
  base relations with the same row multiset in reverse insertion order did **not** move the answer for
  any of the 11.
* *"`ONLY_FULL_GROUP_BY` would filter them."* Dolt accepts them with that mode on, and MySQL's
  functional-dependency checker is not what rejects them — plain name resolution is (1054).

* Reduced repro: [`reduced.sql`](reduced.sql)
  (runs each block against Dolt **and** MariaDB and checks the accept/reject verdicts)
* Original findings: `dolt_20260809-052933/mismatch_round{1_0,11_0,16_1,16_2,19_0,23_0,23_1,25_0,26_0,31_0,32_0}.sql`

## Open items

* **The exact acceptance rule is not pinned.** "An aggregate beside the bare column in `HAVING`" is one
  shape Dolt accepts, confirmed minimally. But rounds 16_2, 23_0, 23_1, 26_0, 31_0 and 32_0 have **no**
  aggregate in their `HAVING` and are still accepted, so at least one more accepting shape exists
  (those queries do have aggregates in the SELECT list and group by expressions). Someone filing this
  should either narrow it further or report it as "HAVING resolves columns from the pre-grouping scope
  in aggregate queries" and let the maintainer bound it.
* Whether Dolt's chosen value for the bare column is the first row of the group, the last, or plan
  dependent — not investigated.
* Regression window not determined (single build available).

## Harness note (eqgen)

These queries reach the findings file **only because Dolt accepts them**. On mysql/mariadb the harness
already discards this shape automatically through its existing "uncomparable (base rejected it)" path —
the base side raises 1054 and the round is skipped. Dolt's permissiveness defeats that filter, so the
generator should stop emitting bare non-grouped columns in `HAVING` (they are invalid MySQL and carry no
ground truth) rather than relying on the engine to reject them. Until then, 11 of 19 mismatches in a
Dolt run are noise of this one kind.
