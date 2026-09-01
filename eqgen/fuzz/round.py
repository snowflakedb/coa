# Copyright 2026 Snowflake Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""One round: build both databases, run the queries, say what happened.

:func:`run_round` prints nothing and writes no files. It returns a :class:`RoundOutcome` and the
caller decides what to do with it, which is what makes it testable — an earlier version was a
250-line loop that also owned the log directory, the file names, the repro files and stdout.

The engine runs in a **forked child process**. A memory-safety bug in it aborts the process, which
no ``except`` can catch::

    parent   keeps the queries and the log      -- survives, knows which query was in flight
    child    holds both databases               -- dies

So a crash becomes "this query killed the engine", which is the most valuable result available,
rather than the end of the run.
"""

from __future__ import annotations

import contextlib
import multiprocessing
import os
import signal
import time
from dataclasses import dataclass, field
from multiprocessing.connection import Connection as PipeConnection
from multiprocessing.process import BaseProcess
from typing import Callable, Iterable, Optional, Sequence

from eqgen.core.catalog import Table
from eqgen.equivalence.ast import describe_shape, render_tree
from eqgen.equivalence.generator import EquivalenceGenerator
from eqgen.fuzz.adapter import DialectAdapter
from eqgen.fuzz.compare import ObjectComparison, QueryComparison, PlanFingerprinter, compare_env, compare_one
from eqgen.fuzz.database import Database, Row, column_names, exposed_fork_names, hidden_base_name
from eqgen.fuzz.journal import QueryJournal
from eqgen.fuzz.schedule import EquivalenceComplexity, RoundTimeScheduler, measure_complexity

#: Parent-side caps on waiting for the forked worker. The DuckDB CLI also has its own per-statement
#: timeout; these exist so a stuck child (or a dialect without one) cannot freeze the whole campaign.
#: Query wait is sized for both sides of a differential check serially (2 × 60s + slack).
_QUERY_RECV_TIMEOUT = float(os.environ.get("EQGEN_ROUND_QUERY_TIMEOUT", "130"))
_SETUP_RECV_TIMEOUT = float(os.environ.get("EQGEN_ROUND_SETUP_TIMEOUT", "600"))


@dataclass(frozen=True)
class RoundOutcome:
    """Everything one round produced. No side effects, just facts."""

    seed: int
    #: Per-query comparisons that completed.
    results: list[QueryComparison] = field(default_factory=list)
    #: The statements that built the equivalent, for the repro.
    equivalent_statements: list[str] = field(default_factory=list)
    #: Whether the equivalence itself was equivalent (the generator's own gate).
    object_comparison: Optional[ObjectComparison] = None
    #: The query that killed the engine, if one did.
    crashed_query: Optional[str] = None
    crash_note: Optional[str] = None
    #: Set when the child died while *building* the equivalence, before any query ran.
    #:
    #: Distinct from :attr:`setup_error`, and the distinction matters: that one is the engine refusing
    #: the DDL and saying so, which is ordinary and common. This is the process *dying* — no exception
    #: raised, nothing sent back — so no single statement can be blamed and
    #: :attr:`equivalent_statements` is the whole of the evidence.
    setup_crash: Optional[str] = None
    #: Set when the equivalence could not be generated or built at all.
    setup_error: Optional[str] = None
    setup_known_issue: bool = False
    #: Visible relation names the workload reads (``t`` or ``t0``/``t1``/…). Needed so the repro
    #: rebuilds the same base-side forks :meth:`Database.build_base` installed at fuzz time.
    exposed_names: tuple[str, ...] = ()
    #: Query-phase wall-clock budget allocated by the scheduler (seconds), if one was used.
    round_budget_seconds: Optional[float] = None
    #: Complexity that produced :attr:`round_budget_seconds`, when scheduled.
    complexity: Optional[EquivalenceComplexity] = None
    #: True when the query loop stopped because the time budget was exhausted.
    stopped_for_budget: bool = False

    @property
    def findings(self) -> list[QueryComparison]:
        return [result for result in self.results if result.is_reportable]

    @property
    def object_diverged(self) -> bool:
        """The equivalence was not equivalent — a generator defect, not an engine one."""
        return self.object_comparison is not None and not self.object_comparison.equal

    @property
    def queries_attempted(self) -> int:
        return len(self.results) + (1 if self.crashed_query is not None else 0)


def _worker(
    pipe: PipeConnection,
    adapter: DialectAdapter,
    table: Table,
    rows: Sequence[Row],
    statements: Sequence[str],
    exposed_names: tuple[str, ...],
    plan_fingerprint: Optional[PlanFingerprinter] = None,
) -> None:
    """Child side: own both databases, answer one query at a time.

    Only this process touches an engine, so a crash kills only this process. It runs after ``fork``,
    so the adapter and the generated statements are inherited rather than pickled.
    """
    try:
        base = Database.build_base(adapter, table, rows, exposed_names=exposed_names)
        equivalent = Database.build_equivalent(
            adapter, table, rows, statements=statements, exposed_names=exposed_names
        )
    except Exception as exc:  # includes the driver error: the equivalent would not build
        known = isinstance(exc, adapter.db_error) and adapter.known_issue_label(exc) is not None
        pipe.send(("setup_error", str(exc), known))
        return

    columns = column_names(table)
    try:
        comparison = compare_env(base, equivalent, table, columns, exposed_names)
    except Exception as exc:  # - reading the object can fail even though creating it did not
        # A deferred failure: the DDL was accepted, so `build_equivalent` succeeded, and the error only
        # appears once rows flow through. `CAST(c_chr AS BOOLEAN)` in a view is the case that found this
        # — type-legal at CREATE time, `invalid input syntax for type boolean: "Zed"` at read time.
        #
        # This is the engine correctly refusing bad input, so it is a `setup_error` like any other
        # unbuildable round, not a crash. Before this was caught it escaped `_worker` entirely, killing
        # the child and — through the pipe closing — the whole campaign.
        known = isinstance(exc, adapter.db_error) and adapter.known_issue_label(exc) is not None
        with contextlib.suppress(Exception):
            base.close()
            equivalent.close()
        pipe.send(("setup_error", f"reading the object failed: {exc}", known))
        return

    pipe.send(("ready", comparison, False))
    try:
        while True:
            query = pipe.recv()
            if query is None:
                return
            pipe.send(
                (
                    "result",
                    compare_one(base, equivalent, query, plan_fingerprint=plan_fingerprint),
                    False,
                )
            )
    finally:
        with contextlib.suppress(Exception):
            base.close()
            equivalent.close()


def _crash_note(process: BaseProcess) -> str:
    """Describe how the child died, naming the signal when it was killed by one."""
    with contextlib.suppress(Exception):
        process.join(timeout=5)
        code = process.exitcode
        if code is None:
            return "child still running but pipe closed"
        if code < 0:
            return f"killed by {signal.Signals(-code).name}"
        return f"exited with status {code}"
    return "unknown"


def _recv(pipe: PipeConnection, process: BaseProcess, timeout: float) -> tuple[object, object, object]:
    """``pipe.recv()`` with a wall-clock cap. Kills *process* on timeout so the campaign can continue."""
    if not pipe.poll(timeout):
        with contextlib.suppress(Exception):
            process.kill()
        raise TimeoutError(f"worker did not respond within {timeout:g}s")
    return pipe.recv()  # type: ignore[return-value]


def run_round(
    adapter: DialectAdapter,
    generator: EquivalenceGenerator,
    table: Table,
    rows: Sequence[Row],
    queries: Iterable[str],
    *,
    seed: int,
    journal: Optional[QueryJournal] = None,
    scheduler: Optional[RoundTimeScheduler] = None,
    clock: Callable[[], float] = time.monotonic,
    forks: int = 1,
    plan_fingerprint: Optional[PlanFingerprinter] = None,
) -> RoundOutcome:
    """Build an equivalence for *table*, run *queries* against both sides, classify the results.

    *table* carries the **seed** catalog name (usually ``t``). Three names are in play and
    keeping them straight is the fiddly part of the setup:

    * the base database holds the seed under that name, then copies it to each exposed fork name;
    * the equivalent's database renames the seed aside to a hidden name and exposes each fork
      equivalence under the exposed names;
    * so the generator is handed a table bearing the **hidden** name, because that is what its source
      references must resolve to.

    *forks* > 1 runs same-base forks (``t0``…``t{k-1}``). Callers must pass a query iterator that
    already uses those :func:`~eqgen.fuzz.database.exposed_fork_names`.

    *queries* is consumed lazily, so a streaming source interleaves generation with execution and the
    journal stays ahead of a crash.

    When *scheduler* is set, the query phase runs until its allocated wall-clock budget is exhausted
    (checked before each query), the source ends, or a crash. The budget is journaled at the start of
    that phase. *clock* is the monotonic source for that deadline (injectable in tests).
    """
    exposed_names = exposed_fork_names(forks, seed_name=table.get_sql_name())
    hidden = Table(hidden_base_name(table), table.get_column_list())
    try:
        generated = generator.generate_forks(hidden, seed=seed, exposed_names=exposed_names)
        statements = [statement.statement_text for statement in generated.setup_statements]
    except Exception as exc:  # - any generation failure discards the round
        error = f"{type(exc).__name__}: {exc}"
        if journal is not None:
            journal.note(f"NOT GENERATED: {error}")
        return RoundOutcome(seed=seed, setup_error=error, exposed_names=exposed_names)

    complexity: Optional[EquivalenceComplexity] = None
    if journal is not None or scheduler is not None:
        # Score the heaviest fork tree; statement count covers every fork's DDL.
        complexity = measure_complexity(
            max(generated.forks, key=lambda fork: len(fork.setup_statements)).root,
            statements=len(statements),
            builders_used=generated.builders_used,
        )

    if journal is not None:
        # Before the fork, deliberately: the DDL that builds the equivalent is the other half of any
        # repro, and a round that goes on to kill the engine must not take it down unwritten.
        if len(generated.forks) == 1:
            journal.record(
                f"equivalence: {describe_shape(generated.forks[0].root)}",
                render_tree(generated.forks[0].root),
            )
        else:
            journal.note(f"forks: {len(generated.forks)} exposed as {', '.join(exposed_names)}")
            for i, fork in enumerate(generated.forks):
                journal.record(
                    f"equivalence fork {i} ({fork.exposed_name}): {describe_shape(fork.root)}",
                    render_tree(fork.root),
                )
        used = generated.builders_used
        journal.note("builders: " + ", ".join(f"{name} {count}" for name, count in sorted(used.items())))
        origin = generated.predicate_origin
        if origin:
            journal.note("predicates: " + ", ".join(f"{name} {count}" for name, count in sorted(origin.items())))
        journal.record("equivalence DDL", [f"{sql};" for sql in statements])

    context = multiprocessing.get_context("fork")
    parent_pipe, child_pipe = context.Pipe()
    process = context.Process(
        target=_worker,
        args=(
            child_pipe,
            adapter,
            table,
            rows,
            statements,
            exposed_names,
            plan_fingerprint,
        ),
        daemon=True,
    )
    process.start()
    child_pipe.close()  # the parent must not hold the child's end, or EOF never arrives

    results: list[QueryComparison] = []
    crashed_query: Optional[str] = None
    crash_note: Optional[str] = None
    object_comparison: Optional[ObjectComparison] = None
    round_budget_seconds: Optional[float] = None
    stopped_for_budget = False
    try:
        try:
            kind, payload, known = _recv(parent_pipe, process, _SETUP_RECV_TIMEOUT)
        except TimeoutError as exc:
            note = f"{exc}; {_crash_note(process)}"
            if journal is not None:
                journal.note(f"CRASH: engine hung while building the equivalence ({note})")
            return RoundOutcome(
                seed=seed,
                equivalent_statements=statements,
                setup_crash=note,
                crash_note=f"while building the equivalence: {note}",
                complexity=complexity,
                exposed_names=exposed_names,
            )
        except (EOFError, BrokenPipeError, ConnectionResetError):
            # The child died while building the object, without raising — ``_worker`` catches every
            # ``Exception`` around setup and reports it, so reaching here means process death, not a
            # rejected statement. Report it as a crash rather than letting the exception escape: an
            # unhandled ``EOFError`` here ends the whole campaign, which is exactly what the fork was
            # supposed to prevent. The DDL is already journaled, so the round is reproducible.
            note = _crash_note(process)
            if journal is not None:
                journal.note(f"CRASH: engine died while building the equivalence ({note})")
            return RoundOutcome(
                seed=seed,
                equivalent_statements=statements,
                setup_crash=note,
                crash_note=f"while building the equivalence: {note}",
                complexity=complexity,
                exposed_names=exposed_names,
            )
        if kind == "setup_error":
            if journal is not None:
                journal.note(f"WOULD NOT BUILD: {payload}")
            return RoundOutcome(
                seed=seed,
                equivalent_statements=statements,
                setup_error=str(payload),
                setup_known_issue=bool(known),
                complexity=complexity,
                exposed_names=exposed_names,
            )
        if journal is not None:
            journal.note(f"equivalence check: {payload.verdict}")
        object_comparison = payload

        deadline: Optional[float] = None
        if scheduler is not None:
            assert complexity is not None
            round_budget_seconds = scheduler.seconds(complexity)
            deadline = clock() + round_budget_seconds
            if journal is not None:
                journal.note(
                    complexity.schedule_note(
                        budget=round_budget_seconds,
                        min_seconds=scheduler.min_seconds,
                        max_seconds=scheduler.max_seconds,
                    )
                )

        # Check the budget *before* pulling the next query. Pulling can block on a cold
        # sqlancerpp jar/container; counting that against the budget then discarding the
        # query left Docker-backed dialects at 0 queries per round.
        query_iter = iter(queries)
        while True:
            if deadline is not None and clock() >= deadline:
                stopped_for_budget = True
                break
            try:
                query = next(query_iter)
            except StopIteration:
                break
            if journal is not None:
                journal.begin(query)
            try:
                parent_pipe.send(query)
                kind, payload, _ = _recv(parent_pipe, process, _QUERY_RECV_TIMEOUT)
            except TimeoutError as exc:
                crashed_query = query
                crash_note = f"{exc}; {_crash_note(process)}"
                if journal is not None:
                    journal.end(f"CRASH: engine hung ({crash_note})")
                break
            except (EOFError, BrokenPipeError, ConnectionResetError):
                # The child died mid-query: this is the query that killed the engine.
                crashed_query = query
                crash_note = _crash_note(process)
                if journal is not None:
                    journal.end(f"CRASH: engine died ({crash_note})")
                break
            results.append(payload)
            if journal is not None:
                journal.end(results[-1].verdict)
    finally:
        with contextlib.suppress(Exception):
            if process.is_alive():
                parent_pipe.send(None)
            process.join(timeout=10)
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
        parent_pipe.close()

    return RoundOutcome(
        seed=seed,
        results=results,
        equivalent_statements=statements,
        object_comparison=object_comparison,
        crashed_query=crashed_query,
        crash_note=crash_note,
        round_budget_seconds=round_budget_seconds,
        complexity=complexity,
        stopped_for_budget=stopped_for_budget,
        exposed_names=exposed_names,
    )
