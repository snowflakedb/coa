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

# MySQL: `HAVING (<view expression column> AND COUNT(...))` drops a group through a merged VIEW

## Summary

A `VIEW` whose column is a genuine expression (`CAST(c_pk AS SIGNED)`, or arithmetic like
`c_pk + 0`) — as opposed to a bare column reference — silently loses a `GROUP BY` group when
queried with `HAVING (<that column> AND COUNT(<that column>))`. Each half of the `AND`, evaluated
alone in `HAVING`, correctly returns true for the missing group; only the specific combination of
"a view-computed column" *and* "that column paired with `COUNT()` via `AND` in `HAVING`" triggers
the defect. Materializing the identical `SELECT` as a `TABLE` instead of a `VIEW` is sufficient to
fix it, isolating the mechanism to MySQL's view-merge/query-rewrite path.

## Environment

- **Engine**: MySQL 9.7.2 (docker image `mysql:9.7.2`).
- **Session**: `sql_mode = STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES`;
  `utf8mb4` / `utf8mb4_0900_bin`. Not load-bearing.

## Minimal repro

See [`reduced.sql`](./reduced.sql):

```sql
CREATE TABLE t__base (c_pk BIGINT NOT NULL, c_int BIGINT);
INSERT INTO t__base VALUES (1, NULL), (2, -7);

CREATE VIEW t AS SELECT CAST(c_pk AS SIGNED) AS c_pk, c_int FROM t__base;

SELECT t.c_pk, COUNT(t.c_pk) FROM t GROUP BY t.c_pk HAVING (t.c_pk AND COUNT(t.c_pk)) ORDER BY t.c_pk;
```

```
+------+---------------+
| c_pk | COUNT(t.c_pk) |
+------+---------------+
|    2 |             1 |
+------+---------------+
```

Expected two rows, `(1, 1)` and `(2, 1)` — every row's own `c_pk` is a singleton group whose
`COUNT` is trivially `1`, and `t.c_pk AND COUNT(t.c_pk)` is `1 AND 1` for group 1 and `2 AND 1` for
group 2, both truthy. The `c_pk = 1` group is dropped outright.

## Isolating the trigger

| View body / query variant | Result |
|---|---|
| `SELECT CAST(c_pk AS SIGNED) AS c_pk, c_int FROM t__base` (the repro) | **`(2, 1)` — group 1 missing** |
| `SELECT (c_pk + 0) AS c_pk, c_int FROM t__base` (arithmetic instead of `CAST`) | **`(2, 1)` — same wrong result** |
| `SELECT c_pk AS c_pk, c_int FROM t__base` (bare column, aliased) | `(1, 1), (2, 1)` — correct |
| `SELECT (+c_pk) AS c_pk, c_int FROM t__base` (unary plus) | `(1, 1), (2, 1)` — correct |
| `CREATE TABLE t AS SELECT CAST(c_pk AS SIGNED) …` (materialized, same `HAVING`) | `(1, 1), (2, 1)` — correct |

Holding the `CAST`-expression view fixed and varying only `HAVING`:

| `HAVING` clause (same view) | Result |
|---|---|
| `(t.c_pk AND COUNT(t.c_pk))` | **wrong — drops group 1** |
| `(t.c_pk AND 1)` | correct |
| `(COUNT(t.c_pk) AND COUNT(t.c_pk))` | correct |
| `(t.c_pk AND t.c_pk)` | correct |
| `TRUE` | correct |
| `t.c_pk` (no `AND` at all) | correct |

So the trigger needs **all** of: (1) the view's `GROUP BY` column is a computed expression, not a
bare reference, (2) `HAVING` combines that column with `COUNT()` via `AND`, and (3) the source is a
`VIEW`, not a materialized `TABLE`. Deterministic — 4/4 identical wrong results across repeated
fresh-connection runs.

## Minimal oracle exposure path

- **Object composition arity:** **2**
- **GCL builder path:** `MySqlJsonPackRoundTripBuilder` → `VIEW` realization
- **Confidence:** Verified — the report names the builder that surfaced the finding, and its GCL implementation hardcodes the final unpacking view.
- **Realization:** The builder internally CTASes and JSON-packs the input, then exposes expression-valued columns through a mergeable `VIEW`.
- **Workload/data requirements (excluded from arity):**
  - A computed, non-bare grouping column in the merged view.
  - `GROUP BY` that column.
  - `HAVING (column AND COUNT(column))` with a truthy singleton group.

**Exposure vs. intrinsic trigger:** The JSON round trip is the oracle exposure path, but its JSON operations reduce away. The intrinsic trigger is view merge of any genuine computed column combined with the grouped `HAVING` expression across aggregate and non-aggregate scope.

## Mechanism (`EXPLAIN FORMAT=TREE`)

```
-> Sort: c_pk
    -> Filter: ((0 <> cast(t__base.c_pk as signed)) and (0 <> count(cast(t__base.c_pk as signed))))
        -> Table scan on <temporary>
            -> Aggregate using temporary table
                -> Table scan on t__base
```

The view is merged/inlined: both the `Filter` (i.e. `HAVING`, rewritten from `x AND y` to
`(x<>0) AND (y<>0)`) and the aggregate reference `t.c_pk` re-expand to the identical text
`cast(t__base.c_pk as signed)` — once as a plain (non-aggregated) operand, once inside `count()`.
Evaluated independently, both give `1` for the `c_pk=1` row (a non-null, non-zero value; a
one-row group's `COUNT` of a non-null expression is `1`), so the `Filter` should pass. That it does
not suggests MySQL's optimizer, having noticed the two occurrences are textually identical after
view-merge, treats them as a single common subexpression — but incorrectly shares it *across* the
aggregate/non-aggregate boundary (pre- vs post-`GROUP BY` scope), so the non-aggregated operand ends
up evaluated in the wrong scope for at least one group. This does not happen when the column is a
bare reference (nothing to merge/rewrite) or the source is a materialized table (no view-merge
rewrite occurs at all).

## Not a comparability gap

`GROUP BY c_pk` groups on a `NOT NULL` primary-key-like column with one row per group; there are no
ties, no non-deterministic tie-breaking, and no underspecified ordering for the skill's
comparability exemption to apply to. Both the correct and incorrect answers are fully deterministic
and reproducible; the incorrect one is simply wrong.

## Blast radius

- Read-only `SELECT`; not tested against `DELETE`/`UPDATE`.
- Any view that exposes a computed column (which is extremely common — type-normalizing casts,
  simple arithmetic, `CASE` expressions) and is then queried with a `HAVING` clause combining that
  column with an aggregate via `AND`/`OR` is at risk of silently losing rows with no error and no
  warning.

## How it was found

eqgen's data-equivalence oracle built a row-identical equivalent `t` whose final step was a new
builder added this session — `MySqlJsonPackRoundTripBuilder`, which exposes `t` as a `VIEW` packing
every column into one `JSON_OBJECT` and unpacking it back via `JSON_EXTRACT`/`CAST`. The random
workload query's `HAVING ((t.c_pk) AND (COUNT(t.c_pk)))` diverged from the base table's answer (an
8-row base vs. a 7-row equivalent, missing exactly the all-`NULL` row). Delta-debugging against the
live engine — first confirming admissibility (the two `t` relations are row-identical), then
ablating the query and view body one ingredient at a time — reduced the original ~9-column,
multi-statement finding down through an intermediate JSON-specific-looking repro to the truly
minimal, **JSON-free** two-column, two-row case above: the `JSON_EXTRACT`/`JSON_OBJECT` machinery
in the builder that surfaced this was incidental — any computed view column plus this exact
`HAVING` shape reproduces it.
