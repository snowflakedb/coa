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

# Measuring engine coverage: what actually bites

Notes from building `eqgen/evaluation/` on PostgreSQL 18.4. Every number here was measured on this
codebase, not reasoned about. If you are adding an engine or an arm, read the traps first — most of them
produce a *plausible wrong number* rather than an error, which is the worst failure mode a measurement
can have.

## The pipeline in one box

```
compile   gcc --coverage      one .gcno per translation unit: every line, every branch
run       counters in RAM     arcs increment as taken
exit      one .gcda per TU    written out, MERGED with whatever is already on disk
```

Everything below follows from the third line. **Counters reach disk at process exit**, and .gcda writes
*add*, so cumulative coverage accrues on its own and nothing needs resetting between samples.

A snapshot is a **copy of the .gcda files** (~870 files, ~4 MB), not a report. Reports cost ~22 s each and
are built afterwards by `report.py`, so a campaign is never paused by its own measurement.

## The traps, in the order they cost the most

### 1. `initdb` is worth ~18% of lines on its own

Building the system catalogue covers about **18% of `src/backend`** — six times what a 1,200-query
workload adds. A tool that connects to an already-running server never pays for it and never gets credit
for it, so counting it flatters whichever harness starts its own cluster. `run.py` therefore zeroes
counters **twice**: once before anything, once after the cluster is up.

### 2. The build leaves counters behind

A freshly built tree already carries counts for ~860 files, because `make` compiles and *runs*
instrumented helper programs. Zero after building or your first report includes the build.

### 3. Long-lived processes do not flush until they exit

Backends flush when their connection closes, so per-round work appears on the curve as it happens. The
postmaster, checkpointer, walwriter, background writer and autovacuum do **not** — they accumulate for
the whole campaign and write at shutdown. Measured, that lands as a **1.6–2.9 point jump in the final
snapshot**:

```
run              gap since last snapshot   jump at final
1 min                        31 s              +2.91
10 min                      122 s              +1.70
6 h                         607 s              +1.64
```

Note the jump does *not* scale with the gap — a 31 s gap jumped more than a 600 s one — which is how you
know it is the flush and not work-since-last-snapshot.

Fix: `PgCluster.restart()` before every snapshot (`Sampler(flush=...)`). Restarting makes those processes
exit and write; .gcda merge, so nothing is lost. Safe because the harness holds no connection between
rounds.

Verified by re-running one arm identically with and without it:

```
             post-fix   pre-fix
baseline       5.73%     1.99%    +3.74   aux startup now visible at the first snapshot
 2 min        18.51%    16.69%    +1.82
 8 min        19.02%    17.13%    +1.89
final         19.07%    18.83%    +0.24
             +0.05      +1.70     <- the end jump, collapsed
```

So the jump was **real coverage arriving late**, not an artifact: every intermediate point rises by the
amount that used to appear only at the end, and the final is essentially unchanged. Two consequences:

* the flush costs ~12% of throughput at a 2-minute cadence (164,400 queries against 187,830), so keep
  the interval at 2 minutes or longer, and apply it to **every** arm being compared or the cost falls on
  one side only;
* the baseline rises from ~2% to ~5.7%, so "workload contribution" figures are not comparable across the
  fix even though finals are.

It cannot be applied to a tool that holds a connection open for its whole run — restarting would sever
it. The SQLancer++ standalone arms therefore have no usable curve, only a final.

`__gcov_dump()` from inside the server is **not** an option: only `__gcov_reset` is in the postgres
binary's dynamic symbol table, and an extension runs in a backend anyway, so it could never make the
checkpointer dump.

### 4. `pg_ctl stop -m immediate` throws counters away

SIGQUIT skips gcov's `atexit` handler. Measured over a 10-round run:

```
-m fast        14.07% lines,  9.34% branches
-m immediate   13.34% lines,  8.86% branches      <- ~2,800 lines lost
```

The **files** appear under both modes, because the postmaster exits via `proc_exit` either way and its
image contains every backend TU. Only the counts inside differ — so comparing .gcda *file counts* between
modes tells you nothing. `EQGEN_PG_COVERAGE=1` selects `fast`.

### 5. The denominator moves unless you pin it

gcovr can only report on TUs that have written a .gcda, so a module loaded on demand *joins* the
denominator partway through and the percentage can **fall while coverage only grew**:

```
round 10    26,961 / 378,016 = 7.13%
round 20    27,400 / 391,000 = 7.01%     more covered, lower percentage
```

`coverage.fixed_denominator()` takes the max per file across every snapshot and divides everything by
that. A file absent from an early snapshot is then zero-covered, which is what it was.

### 6. Three gcovr flags are mandatory on PostgreSQL

* `--merge-mode-functions=merge-use-line-min` — `src/common` is compiled three times (`_srv`, `_shlib`,
  frontend), so an inlined header function lands at different lines in different objects and gcovr
  aborts the whole report with `GcovrMergeAssertionError`.
* `--gcov-ignore-parse-errors=suspicious_hits.warn_once_per_file` — gcovr aborts on any hit count above
  2^32, assuming GCC bug 68080. On a long run it is real: after 6.7 M queries `MemSetAligned` in
  `mcxt.c` had genuinely run **7,116,195,248** times and the report died on it. Only *whether* a line
  was hit matters here, never how often.
* Run from the source root. A relative `--filter` is resolved against the **cwd**, so from anywhere else
  it silently matches nothing and reports `0 out of 0` — not an error.

### 7. `--enable-coverage` needs lcov

PostgreSQL's `configure` hard-errors `lcov not found`. Install it (nix works). DuckDB whole-engine
reports also need it: eqgen uses DuckDB's `lcov --no-external` + `.github/workflows/lcov_exclude`
pipeline (same as SQLancer++), not bare gcovr over `src/`.

### 7b. DuckDB: gcovr `src/` is not comparable to the papers

On the same counters, gcovr with `--filter src/` reported **15.4% of 366k lines** while
`lcov --no-external` + `lcov_exclude` reported **30.0% of 109k**. Most of the gap is gcovr attributing
~196k lines under `src/include/` that lcov barely counts. `report.py` therefore dispatches DuckDB
`scope=all` through :func:`~eqgen.evaluation.coverage.gcov.run_duckdb_lcov`. Narrow scopes
(parser/planner/optimizer) still use gcovr.

**Branch totals (paper rule, all lcov reporters):** ``_parse_lcov_info`` omits ``BRDA`` records with
``taken=-`` (never evaluated) from both numerator and denominator — matching older lcov / the
SQLancer++ Docker branch scale (~226k on DuckDB v1.0.0), not lcov 2.x ``--summary`` which counts
``-`` as missed (~370k). Capture also passes DuckDB's ``.github/workflows/lcovrc`` when present.
Postgres ``run_postgres_lcov`` uses the same branch rule. Manifests/CSV reports stamp
``branch_counting: omit_unevaluated_brda``.

**Stale CSVs:** any DuckDB v1.0.0 ``coverage.csv`` with ``branches_total≈370110``, or Postgres
``coverage_lcov.csv`` with ``branches_total≈233446`` (an older, now-retired PG build), predates
this rule — re-run
``python -m eqgen.evaluation.coverage.report <run_dir>`` (use ``--reporter lcov`` if the manifest
still says gcovr).

### 7c. Paper is the default; fair is opt-in

Defaults match the SQLancer++ artifact (paper). Use ``EQGEN_COVERAGE_FAIR=1`` on the build scripts
and ``campaign --fair`` only for the older eqgen-only curves.

| | **paper (default)** | **fair** (`EQGEN_COVERAGE_FAIR=1` / ``--fair``) |
|---|---|---|
| PG version | **REL_18_4** | REL_18_4 (override via env) |
| PG configure | `--enable-coverage` only | `--enable-debug -O0 --without-*` |
| PG initdb | Counted | Zeroed after setup |
| PG report | full-tree lcov | gcovr `src/backend/` + `src/common/` |
| DuckDB version | **v1.0.0** | v1.5.5 |
| DuckDB build | unity ON, `CXXFLAGS=--coverage` | `DISABLE_UNITY=1`, `-O0` |
| DuckDB report | lcov + `lcov_exclude` (+ `lcovrc`) | same |
| Branches (lcov) | omit `taken=-` | omit `taken=-` (if using lcov) |

Manifest fields: `artifact_aligned` (true unless `--fair`), `coverage_artifact_build`,
`branch_counting`, optional `duckdb_coverage_ref` / `pg_coverage_ref`. `include_setup` is Postgres-only.

## Two things that are not coverage bugs but will ruin a run

**Connection churn exhausts `max_connections` on a coverage build.** An exiting backend writes ~870
`.gcda` files, so teardown is far slower than normal and backends pile up. At 47 rounds/s a 50-slot
cluster died in seven minutes with `sorry, too many clients already`. Fixed by raising to 200 *and* a
bounded retry in `PostgresAdapter.connect()`.

**Throughput is I/O-sensitive to a shocking degree.** The same run did 40 rounds in 48 s with `/tmp` at
94% full, and 424 rounds in 36 s once freed — **14x**. Check free space before trusting any timing
comparison.

## What cannot be measured this way

**In-process engines.** `multiprocessing` children exit via `os._exit()`, which skips `atexit` — and
libgcov's dump *is* an atexit handler. eqgen forks a child per round, so an engine living inside that
child flushes nothing. DuckDB therefore executes through the CLI binary. For coverage, build an
instrumented CLI with `build_duckdb_coverage.sh` (GCC `--coverage`, not DuckDB's clang `COVERAGE=1`)
and point `EQGEN_DUCKDB_CLI` / `EQGEN_DUCKDB_COVERAGE_SRC` at it; each connection is a process that
writes `.gcda` on close, so there is no postmaster-style flush to arrange.

**Intermediate points for a tool that holds connections open.** SQLancer++ standalone keeps one
connection for its whole run, so nothing flushes until the jar exits:

```
t30s     11,637 / 378,016    3.08%
t60s     16,305 / 378,016    4.31%
final    60,642 / 378,016   16.04%      <- 4x jump in 0.4 s
```

Only the **final** snapshot is meaningful for such a tool. Do not plot the rest. Segmenting the jar to
force intermediate flushes was considered and rejected: restarting resets its internal learner and
schema state, which would understate the baseline we are comparing against.

## Comparing arms

**Equal wall clock, not equal round or query count.** That is the convention in the literature (Argus's
figures are 24-hour runs) and it is correct rather than a concession: throughput is part of what a tool
is, so a rewrite that costs time and buys coverage should be judged on the trade. Query counts are
recorded per snapshot as a *diagnostic* for why a curve moved, never as the axis of comparison.

**One instrumented tree per concurrent arm.** .gcda are written into the build tree, so two arms sharing
one tree merge each other's counters. `build_postgres_coverage.sh` is parameterised by
`EQGEN_PG_COVERAGE_SRC` / `EQGEN_PG_COVERAGE_PREFIX`; a second tree is a ~2 minute build. Pair the bindir
with its own source tree or you measure nothing.

**Short runs are noise.** At 1 minute, combining a better query generator with better predicates measured
*worse* than the query generator alone; at 10 minutes it measured clearly better. Anything inside ~2
points at 1 minute should not be believed. Every number in this file is a single seed and a single run.

## Recorded results (fair / historical), PostgreSQL 18.4, `src/backend` + `src/common`

These tables predate paper-as-default. They used gcovr filters and (usually) zeroed initdb — **not**
comparable to SQLancer++ Table 3. Paper PG is REL_18_4 + full-tree lcov + initdb counted.

(378,016 lines / 240,396 branches under the fair gcovr denominator.)

| arm | duration | lines | branches |
|---|---|---|---|
| plain table, toy queries (no objects) | 6 h | 13.10% | 8.53% |
| eqgen + toy | 6 h | 17.53% | 11.98% |
| eqgen + toy | 10 min | 14.72% | 10.32% |
| eqgen + SQLancer++ queries + predicates | 10 min | **18.83%** | **13.00%** |
| eqgen + SQLancer++ queries, toy predicates | 1 min | 17.69% | 12.05% |
| eqgen + toy queries, SQLancer++ predicates | 1 min | 16.38% | 11.13% |
| SQLancer++ standalone, TLP (`QUERY_PARTITIONING`) | 1 min | 17.53% | 12.12% |
| SQLancer++ standalone, NoREC | 1 min | 16.04% | 10.89% |

The 10-minute set, which is the one to quote — all on the same denominator, equal wall clock:

| arm | lines | branches | queries | restart? |
|---|---|---|---|---|
| **eqgen + SQLancer++ queries + predicates** | **19.07%** | **13.09%** | 164,400 | yes |
| SQLancer++ standalone, TLP | 18.11% | 12.66% | 420,253 | cannot |
| SQLancer++ standalone, NoREC | 16.27% | 11.15% | 793,963 | cannot |
| base side only, SQLancer++ queries (no objects) | 15.85% | 10.45% | 810,210 | yes |
| eqgen + toy generator | 14.72% | 10.32% | ~190,000 | no (pre-fix) |

Two readings, and one asymmetry to be honest about:

* **the object layer is worth ~+3.2 points** — 19.07% against 15.85% for the identical query stream on a
  plain table, while running 4.9x fewer queries;
* **eqgen beats SQLancer++'s own oracles at equal time**, narrowly against TLP and clearly against NoREC,
  despite those creating their own schema and running their own DDL/DML — code eqgen never touches;
* the restarted arms lose ~12% of their queries and the standalone arms do not, so eqgen's figures here
  are **conservative** rather than flattered.

Every curve plateaus fast: **85% of a six-hour total arrives in the first ten minutes.** Long campaigns
are for finding bugs and rare paths, not for moving the coverage number.

## Known defect in these numbers

25 `src/bin` files (3,035 lines, 0% covered — `pg_waldump`, `pg_rewind`) leak past the filter via shared
`src/common` objects, depressing every percentage by ~0.1 points. Consistent across arms, so comparisons
hold, but fix it before quoting an absolute figure.
