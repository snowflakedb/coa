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

"""A private PostgreSQL server in a temp directory, for the duration of one run.

Started once per process and thrown away at exit. Socket-only, so two runs on one machine cannot
collide on a TCP port::

    /tmp/eqgen-pg-XXXX/data     the cluster
    /tmp/eqgen-pg-XXXX/sock     the socket the adapter connects to
    /tmp/eqgen-pg-XXXX/server.log

Point it at a build with ``EQGEN_PG_BINDIR``, or leave it to find ``/tmp/pgmain/bin``. Use a server
built with ``--enable-cassert``: assertions turn a class of silent corruption into an immediate
abort, and an abort is something this harness attributes to the exact query that caused it.

**The owner-pid guard is not optional.** ``run_round`` forks a child per round, the child inherits
this object, and its ``atexit`` handlers run when it exits — so without the guard the first round
would shut down the server the parent is still using.
"""

from __future__ import annotations

import atexit
import os
import shutil
import signal
import subprocess
import tempfile
from typing import Any, Callable, Optional

#: Where to find ``initdb``/``pg_ctl``. Overridden by :data:`BINDIR_ENV`.
DEFAULT_BINDIR = "/tmp/pgmain/bin"

#: Environment variable naming the server to use instead of :data:`DEFAULT_BINDIR`.
BINDIR_ENV = "EQGEN_PG_BINDIR"

#: The superuser the cluster is created with, and the role the adapter connects as.
SUPERUSER = "eqgen"

#: Appended to ``postgresql.conf``. Durability is off because this data is thrown away and ``fsync``
#: would dominate a workload that is almost all DDL and tiny inserts. The rest matter to the results:
#:
#: * ``restart_after_crash`` — a crashed backend is a finding, not the end of the run.
#: * ``standard_conforming_strings`` — backslashes stay literal inside ``'…'``, so
#:   :meth:`PostgresAdapter.literal` only has to double single quotes. It is the default; pinned so
#:   the escaping does not depend on a default changing.
#: * ``statement_timeout`` — a generated query can be pathological. Generous, because a timeout on
#:   one side only looks exactly like a one-sided error, i.e. a finding that is not one.
#: * ``dynamic_shared_memory_type = mmap`` — a parallel plan allocates a shared-memory segment, and
#:   the default POSIX implementation uses ``/dev/shm``, often only 64 MB, so a plan wanting more
#:   fails on the equivalent side alone. ``mmap`` puts it under the data directory instead. Chosen
#:   over switching parallel plans off, because a parallel plan is exactly the kind of difference
#:   this harness exists to compare.
_SETTINGS: tuple[tuple[str, str], ...] = (
    ("fsync", "off"),
    ("full_page_writes", "off"),
    ("synchronous_commit", "off"),
    ("restart_after_crash", "on"),
    ("standard_conforming_strings", "on"),
    ("statement_timeout", "'60s'"),
    # Generous, because a backend exits *asynchronously* and a coverage-instrumented one has ~940
    # .gcda files to write on the way out. A connection-per-round workload can otherwise outrun the
    # server's reaping and hit "too many clients already"; PostgresAdapter.connect also retries.
    ("max_connections", "'200'"),
    ("log_min_messages", "'warning'"),
    ("dynamic_shared_memory_type", "mmap"),
)


#: Set this when the server is a coverage build. It changes only the shutdown mode — see
#: :meth:`PgCluster.stop`.
COVERAGE_ENV = "EQGEN_PG_COVERAGE"


def pg_bindir() -> str:
    """The directory holding the server binaries, or raise saying how to supply one."""
    override = os.environ.get(BINDIR_ENV)
    for candidate in (override, DEFAULT_BINDIR):
        if candidate and os.path.isfile(os.path.join(candidate, "initdb")):
            return candidate
    raise RuntimeError(
        f"no PostgreSQL server found at {DEFAULT_BINDIR}. Build one with --enable-cassert, or set "
        f"{BINDIR_ENV} to a directory containing initdb."
    )


class PgCluster:
    """One private cluster. Construction runs ``initdb`` and starts the server, so it takes a
    second or two — use :func:`shared_cluster` rather than building one per round."""

    def __init__(self) -> None:
        self._bindir = pg_bindir()
        # A short path: a Unix socket name has a ~107-byte limit, so this cannot live under the repo.
        self.root = tempfile.mkdtemp(prefix="eqgen-pg-")
        self.datadir = os.path.join(self.root, "data")
        self.sockdir = os.path.join(self.root, "sock")
        self.logfile = os.path.join(self.root, "server.log")
        self.owner_pid = os.getpid()
        self._stopped = False
        os.mkdir(self.sockdir)
        self._initdb()
        self._start()

    def _run(self, *args: str) -> None:
        """Run a server binary, raising with the server log attached if it fails."""
        proc = subprocess.run([os.path.join(self._bindir, args[0]), *args[1:]], capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"{args[0]} failed ({proc.returncode}): {proc.stderr.strip()}\n{self.log_tail()}")

    def _initdb(self) -> None:
        """Create the cluster.

        ``--locale=C`` keeps text sorting and comparison byte-for-byte predictable. Both sides share
        this cluster so a collation would cancel out, but C also removes any dependence on the host's
        ICU version — one less thing to explain when a run on another machine disagrees. ``-A trust``
        is fine on a socket-only cluster in a private directory.
        """
        self._run(
            "initdb", "-D", self.datadir, "-U", SUPERUSER, "-A", "trust",
            "--no-sync", "--encoding=UTF8", "--locale=C",
        )  # fmt: skip
        with open(os.path.join(self.datadir, "postgresql.conf"), "a", encoding="utf-8") as conf:
            conf.write("\n# --- eqgen ---\n")
            conf.write("listen_addresses = ''\n")  # socket only, so there is no port to collide on
            conf.write(f"unix_socket_directories = '{self.sockdir}'\n")
            for key, value in _SETTINGS:
                conf.write(f"{key} = {value}\n")

    def _start(self) -> None:
        """Start the server and wait until it accepts connections (that is what ``-w`` does)."""
        self._run("pg_ctl", "-D", self.datadir, "-l", self.logfile, "-w", "start")

    def restart(self) -> None:
        """Stop and start the server, keeping the data directory.

        Exists for coverage measurement, and the reason is not obvious. A backend flushes its gcov
        counters when its connection closes, so per-round work appears on a coverage curve as it
        happens. The **long-lived** processes — postmaster, checkpointer, walwriter, background writer,
        autovacuum — do not: they accumulate counters in memory for the whole campaign and only write
        them at exit. Their contribution therefore arrives as a single jump in the final snapshot,
        measured at 1.6 to 2.9 points of line coverage, which makes a curve that trends and then leaps
        rather than one that trends.

        Restarting between rounds makes them exit and write. Nothing is lost: ``.gcda`` files *merge*,
        so the new processes' counters add to what the old ones left. Safe here because the harness
        opens a connection per round and holds none between them — every schema is created and dropped
        inside a round.
        """
        self._run("pg_ctl", "-D", self.datadir, "-l", self.logfile, "-m", "fast", "-w", "restart")

    def stop(self) -> None:
        """Stop the server and delete the directory. Idempotent, and a no-op in a forked child.

        ``immediate`` is the default because it always returns: it is SIGQUIT, so nothing can sit
        waiting for a pathological query to finish, and the data directory is about to be deleted
        anyway.

        Under a coverage build that is the wrong trade. gcov writes a process's counters from an
        ``atexit`` handler, and a process killed by SIGQUIT never runs one — so the counters of every
        still-live backend and of the checkpointer, walwriter and bgwriter are simply lost.
        :data:`COVERAGE_ENV` switches to ``fast`` (SIGINT to the postmaster, SIGTERM to its children),
        which lets them exit through ``proc_exit`` and write what they measured.

        Measured on PostgreSQL 18.4 over a 10-round run, the two modes differ by::

            -m fast        14.07% of lines, 9.34% of branches
            -m immediate   13.34% of lines, 8.86% of branches

        — about 2,800 lines, or 5% of everything the run had covered.

        One thing that is *not* the difference, and looks like it should be: the .gcda **files**
        themselves appear either way, because the postmaster exits through ``proc_exit`` under both
        modes and its image contains every backend translation unit. Only the counts inside differ, so
        comparing file counts between the two modes shows nothing.
        """
        if self._stopped or os.getpid() != self.owner_pid:
            return
        self._stopped = True
        mode = "fast" if os.environ.get(COVERAGE_ENV) else "immediate"
        subprocess.run(
            [os.path.join(self._bindir, "pg_ctl"), "-D", self.datadir, "-m", mode, "-w", "stop"],
            capture_output=True,
            check=False,
        )
        shutil.rmtree(self.root, ignore_errors=True)

    @property
    def dsn(self) -> str:
        """A connection string. ``host`` being a path is what selects the Unix socket."""
        return f"host={self.sockdir} user={SUPERUSER} dbname=postgres"

    def server_version(self) -> str:
        proc = subprocess.run([os.path.join(self._bindir, "postgres"), "--version"], capture_output=True, text=True, check=False)
        return proc.stdout.strip() or "unknown"

    def log_tail(self, lines: int = 20) -> str:
        """The end of the server log, for an error message worth reading."""
        try:
            with open(self.logfile, encoding="utf-8", errors="replace") as handle:
                return "".join(handle.readlines()[-lines:])
        except OSError:
            return ""


_shared: Optional[PgCluster] = None


def _install_teardown(stop: Callable[[], None]) -> None:
    """Stop on a normal exit and on Ctrl-C, so a killed run does not leave a server behind."""
    atexit.register(stop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous = signal.getsignal(sig)

        def handler(signum: int, frame: Any, _previous: Any = previous) -> None:
            stop()
            if callable(_previous):
                _previous(signum, frame)
            else:
                raise KeyboardInterrupt

        signal.signal(sig, handler)


def shared_cluster() -> PgCluster:
    """The one cluster for this process, started on first use."""
    global _shared
    if _shared is None:
        _shared = PgCluster()
        _install_teardown(_shared.stop)
    return _shared
