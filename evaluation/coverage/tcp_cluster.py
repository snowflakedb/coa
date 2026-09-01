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

"""An instrumented PostgreSQL reachable over TCP, for measuring an external tool.

This exists for one reason: **JDBC cannot use a Unix socket.** eqgen's own cluster
(``dialects/postgres/cluster.py``) is socket-only on purpose — "no TCP port to collide on" — and that is
right for the measured databases. But a Java tool like SQLancer++ can only reach a server over TCP, so
measuring *it* needs a cluster that listens on one.

Deliberately a separate file rather than a mode on ``PgCluster``. The core cluster is used by every
normal run, and adding a TCP switch to it would put a port-collision foot-gun in the measured path to
serve one evaluation arm.

Note which build this points at, because the two evaluation roles are opposites:

* here, SQLancer++ **is the tool under test**, so it must drive the *instrumented* build — its traffic is
  the measurement;
* when SQLancer++ is used as eqgen's query *generator*, it must drive a *plain* server instead, or its
  own generation traffic lands in eqgen's coverage number.
"""

from __future__ import annotations

import atexit
import os
import shutil
import signal
import socket
import subprocess
import tempfile
from typing import Any, Callable, Optional

#: The superuser. Named ``postgres`` because SQLancer++ expects that role (``jdbc.properties`` has
#: ``POSTGRESQL.user=postgres``) and because it creates and drops its own databases, so it needs a
#: superuser rather than the ``eqgen`` role the harness uses.
SUPERUSER = "postgres"

#: Appended to ``postgresql.conf``. Durability is off because the data is thrown away.
#: ``max_connections`` is generous for the same reason it is in the harness cluster: a
#: coverage-instrumented backend has ~870 ``.gcda`` files to write as it exits, so connection churn can
#: outrun the server's reaping.
_SETTINGS: tuple[tuple[str, str], ...] = (
    ("fsync", "off"),
    ("full_page_writes", "off"),
    ("synchronous_commit", "off"),
    ("restart_after_crash", "on"),
    ("max_connections", "'200'"),
    ("log_min_messages", "'warning'"),
    ("dynamic_shared_memory_type", "mmap"),
)


def free_port() -> int:
    """A port nothing is listening on, chosen by asking the kernel for one.

    Not the 10010 that SQLancer++'s ``jdbc.properties`` defaults to: on this machine something else
    already holds it, and hard-coding a port is how two runs collide. The caller passes the chosen port
    to the tool with ``--port``, which SQLancer++ documents as taking priority over the properties file.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class TcpCluster:
    """A private, TCP-listening cluster from an instrumented build. Started on construction."""

    def __init__(self, bindir: str, *, port: Optional[int] = None) -> None:
        self._bindir = bindir
        self.port = port or free_port()
        self.root = tempfile.mkdtemp(prefix="eqgen-pgtcp-")
        self.datadir = os.path.join(self.root, "data")
        self.logfile = os.path.join(self.root, "server.log")
        self.owner_pid = os.getpid()
        self._stopped = False
        self._initdb()
        self._start()

    def _run(self, *args: str) -> None:
        proc = subprocess.run([os.path.join(self._bindir, args[0]), *args[1:]], capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"{args[0]} failed ({proc.returncode}): {proc.stderr.strip()}\n{self.log_tail()}")

    def _initdb(self) -> None:
        """``--locale=C`` for the same reason as the harness cluster: byte-predictable text comparison.

        ``-A trust`` accepts whatever password the tool sends, which is what lets SQLancer++ connect with
        its configured ``postgres``/``postgres`` without us provisioning a password.
        """
        self._run(
            "initdb", "-D", self.datadir, "-U", SUPERUSER, "-A", "trust",
            "--no-sync", "--encoding=UTF8", "--locale=C",
        )  # fmt: skip
        with open(os.path.join(self.datadir, "postgresql.conf"), "a", encoding="utf-8") as conf:
            conf.write("\n# --- eqgen evaluation (TCP) ---\n")
            conf.write("listen_addresses = 'localhost'\n")
            conf.write(f"port = {self.port}\n")
            for key, value in _SETTINGS:
                conf.write(f"{key} = {value}\n")

    def _start(self) -> None:
        self._run("pg_ctl", "-D", self.datadir, "-l", self.logfile, "-w", "start")

    def stop(self) -> None:
        """Stop with ``-m fast`` and delete the directory. Idempotent; a no-op in a forked child.

        Always ``fast``, never ``immediate``: this cluster only ever exists to be measured, and SIGQUIT
        skips gcov's ``atexit`` handler, losing the counters of every process that had not already
        exited. On the harness cluster that is a trade-off; here it is simply wrong.
        """
        if self._stopped or os.getpid() != self.owner_pid:
            return
        self._stopped = True
        subprocess.run(
            [os.path.join(self._bindir, "pg_ctl"), "-D", self.datadir, "-m", "fast", "-w", "stop"],
            capture_output=True,
            check=False,
        )
        shutil.rmtree(self.root, ignore_errors=True)

    def server_version(self) -> str:
        proc = subprocess.run([os.path.join(self._bindir, "postgres"), "--version"], capture_output=True, text=True, check=False)
        return proc.stdout.strip() or "unknown"

    def log_tail(self, lines: int = 20) -> str:
        try:
            with open(self.logfile, encoding="utf-8", errors="replace") as handle:
                return "".join(handle.readlines()[-lines:])
        except OSError:
            return ""

    def wait_until_ready(self, timeout: float = 30.0) -> None:
        """Block until the server answers on its port, or raise with the server log attached."""
        import time

        deadline = time.monotonic() + timeout
        ready = os.path.join(self._bindir, "pg_isready")
        while time.monotonic() < deadline:
            proc = subprocess.run(
                [ready, "-h", "127.0.0.1", "-p", str(self.port), "-U", SUPERUSER], capture_output=True, check=False
            )
            if proc.returncode == 0:
                return
            time.sleep(0.25)
        raise RuntimeError(f"cluster on port {self.port} never became ready\n{self.log_tail()}")


def install_teardown(stop: Callable[[], None]) -> None:
    """Stop on normal exit and on Ctrl-C, so an interrupted run leaves no server behind."""
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
