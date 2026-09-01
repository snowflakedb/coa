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

"""A DuckDB execution backend that drives the prebuilt ``duckdb`` CLI binary.

The Python ``duckdb`` wheel statically compiles the engine into its extension and is pinned (currently
``1.5.0`` in requirements). The genuine ``main`` engine ships as the prebuilt CLI at
``artifacts.duckdb.org``. To fuzz that engine — and so that coverage instrumentation can flush at
process exit — we talk to the CLI instead of the wheel.

This module supplies:

* :class:`DuckDbCliConnection` — a persistent-process :class:`~eqgen.fuzz.adapter.Connection` (one
  ``duckdb :memory:`` process per connection, state preserved across ``execute`` calls).
* :func:`ensure_latest_duckdb_cli` / :func:`resolve_duckdb_cli` — download the newest ``main``
  binary and locate it.

Only the dialect's *execution* path uses this. The wheel remains available as
``DuckDBAdapter(execution_backend="wheel")`` for offline unit tests that must not download a binary.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

import duckdb

# The engine crash is the point of the differential test, so a crashed CLI process must be
# attributed to the in-flight query — but only inside a forked round worker. Recorded at import
# time (which happens in the launching process, before the harness forks): a worker's pid differs,
# the owner's (CLI startup, sweep, tests) does not. Mirrors postgres's ``cluster.owner_pid`` check.
_OWNER_PID = os.getpid()

#: Environment override for the CLI binary path (set by ``--duckdb-cli``); wins over the download.
_CLI_ENV_VAR = "EQGEN_DUCKDB_CLI"

#: Where :func:`ensure_latest_duckdb_cli` caches the downloaded binary.
_CACHE_DIR = Path(os.path.expanduser("~/.cache/eqgen/duckdb-cli"))
_CACHED_BINARY = _CACHE_DIR / "duckdb"

#: Cap on one statement's wall time. DuckDB has no ``statement_timeout`` like Postgres; without this
#: a pathological query blocks ``stdout.readline`` forever and the campaign's ``--hours`` deadline
#: never fires (it is only checked between rounds). Matches Postgres's 60s default.
_STATEMENT_TIMEOUT = float(os.environ.get("EQGEN_DUCKDB_STATEMENT_TIMEOUT", "60"))

# A DBAPI ``cursor.description`` row is a 7-tuple; the harness only reads index 1 (the type name).
_DescriptionRow = tuple[str, str, None, None, None, None, None]


def _arch_tag() -> str:
    """DuckDB's release-artifact arch suffix for this machine (``arm64`` / ``amd64``)."""
    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        return "arm64"
    if machine in ("x86_64", "amd64"):
        return "amd64"
    raise RuntimeError(f"no DuckDB CLI artifact known for machine {machine!r} (expected aarch64/x86_64)")


def ensure_latest_duckdb_cli() -> Path:
    """Download the newest ``main`` ``duckdb`` CLI binary into the cache and return its path.

    ``artifacts.duckdb.org/latest`` is rebuilt from ``main`` daily; the payload is an outer zip
    containing a nested ``duckdb_cli-linux-<arch>.zip`` that holds the ``duckdb`` executable. We
    download unconditionally (the caller gates this on ``--dialect duckdb`` and ``--no-download-``
    ``duckdb``) so a fuzz run always tracks ``main`` HEAD.

    **The install is an atomic rename inside the cache directory, not a copy into place.** The
    extracted binary is staged next to its destination and swapped in with :func:`os.replace`, which
    only rewrites the directory entry — an already-running process keeps the old inode and is
    unaffected. Staging on a different filesystem from the cache made ``shutil.move`` fall back to
    copying **into** the target file and concurrent runs failed with ``Text file busy``.
    """
    arch = _arch_tag()
    url = f"https://artifacts.duckdb.org/latest/duckdb-binaries-linux-{arch}.zip"
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # Stage inside the cache dir so the final install is a same-filesystem rename. The pid keeps two
    # concurrent downloads from clobbering each other's staging file.
    staged = _CACHE_DIR / f".duckdb.incoming.{os.getpid()}"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        outer = tmp_path / "outer.zip"
        # A User-Agent is required: the artifact CDN 403s the default ``Python-urllib`` agent.
        request = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
        with urllib.request.urlopen(request) as resp:
            outer.write_bytes(resp.read())
        with zipfile.ZipFile(outer) as zf:
            zf.extract(f"duckdb_cli-linux-{arch}.zip", tmp_path)
        with zipfile.ZipFile(tmp_path / f"duckdb_cli-linux-{arch}.zip") as zf:
            zf.extract("duckdb", tmp_path)
        try:
            shutil.copyfile(str(tmp_path / "duckdb"), str(staged))
            staged.chmod(0o755)
            os.replace(str(staged), str(_CACHED_BINARY))
        finally:
            staged.unlink(missing_ok=True)
    return _CACHED_BINARY


def resolve_duckdb_cli() -> Path:
    """Locate the CLI binary: ``$EQGEN_DUCKDB_CLI`` override, else the cached download."""
    override = os.environ.get(_CLI_ENV_VAR)
    if override:
        path = Path(override)
        if not path.is_file():
            raise RuntimeError(f"{_CLI_ENV_VAR}={override!r} is not a file")
        return path
    if _CACHED_BINARY.is_file():
        return _CACHED_BINARY
    raise RuntimeError(
        f"no DuckDB CLI binary found (looked at ${_CLI_ENV_VAR} and {_CACHED_BINARY}); "
        "run without --no-download-duckdb to fetch it, or pass --duckdb-cli PATH"
    )


def engine_version(cli_path: Path) -> tuple[str, str]:
    """``(library_version, source_id)`` from ``pragma_version()`` via a one-shot CLI invocation."""
    proc = subprocess.run(
        [str(cli_path), "-json", "-c", "SELECT library_version, source_id FROM pragma_version()"],
        capture_output=True,
        text=True,
        check=True,
    )
    row = json.loads(proc.stdout)[0]
    return row["library_version"], row["source_id"]


@dataclass
class _CliCursor:
    """The object :meth:`DuckDbCliConnection.execute` returns: rows now, types on demand.

    ``fetchall``/``fetchone`` serve the already-parsed rows. ``description`` is computed lazily
    (only column-type probes need it) by running ``DESCRIBE`` of the same SQL through the
    connection — JSON output carries no column types.
    """

    _rows: list[list[object]]
    _conn: "DuckDbCliConnection"
    _sql: str
    _description: Optional[Sequence[_DescriptionRow]] = field(default=None, init=False)

    def fetchall(self) -> list[list[object]]:
        return self._rows

    def fetchone(self) -> Optional[list[object]]:
        return self._rows[0] if self._rows else None

    @property
    def description(self) -> Sequence[_DescriptionRow]:
        if self._description is None:
            described = self._conn._run(f"DESCRIBE {self._sql.rstrip().rstrip(';')}")
            # DESCRIBE rows are [column_name, column_type, null, key, default, extra].
            self._description = [(str(r[0]), str(r[1]), None, None, None, None, None) for r in described]
        return self._description


class DuckDbCliConnection:
    """A :class:`~eqgen.fuzz.adapter.Connection` backed by one persistent ``duckdb`` CLI process.

    The harness opens two of these per round (base + equivalent) and reuses each across many
    statements, so the process must keep session state (temp tables, macros) and survive a
    per-query SQL error. Framing:

    * stdin is fed one statement at a time, each followed by ``.print <token>``; stdout is read up
      to the ``<token>`` line, so that block is exactly this statement's output.
    * ``.mode jsonlines`` makes each result row one compact JSON object per line; ``.bail off``
      keeps an errored statement from killing the session.
    * stderr is merged into stdout, so an error's text arrives in-band, ahead of the token. Any
      non-JSON, non-token line in the block is error text → raise :class:`duckdb.Error` (the same
      class the wheel backend raises, so ``adapter.db_error`` is unchanged).
    * EOF before the token means the process died → a crash (see :meth:`_run`).

    *database* defaults to ``:memory:`` for fuzzing. Metamorphic coverage passes a file path so a
    later CLI process can reopen the same catalog after this one exits (and flushes ``.gcda``).
    """

    def __init__(self, cli_path: Path, *, abort_on_crash: bool, database: str = ":memory:") -> None:
        self._abort_on_crash = abort_on_crash
        self._token = "__EQGEN_" + uuid.uuid4().hex + "__"
        self._proc = subprocess.Popen(
            [str(cli_path), "-batch", database],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._send(".bail off")
        self._send(".mode jsonlines")
        # Optional session SETs for steered hunts (e.g. debug_verify_column_bindings=true).
        # EQGEN_DUCKDB_SESSION_SQL is a semicolon-separated list of statements run once at connect.
        # Each statement is executed through `_run` (token-framed) so it cannot glue onto the next SQL.
        for stmt in (s.strip() for s in os.environ.get("EQGEN_DUCKDB_SESSION_SQL", "").split(";") if s.strip()):
            if not stmt.endswith(";"):
                stmt += ";"
            self._run(stmt)

    def _send(self, line: str) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(line + "\n")
        self._proc.stdin.flush()

    def _crash(self) -> None:
        """Handle a dead CLI process: abort the forked worker, else raise.

        In a round worker, ``SIGABRT`` makes the parent see EOF on the pipe and blame the in-flight
        query (exactly the postgres path). In the owner/tests, aborting would kill the run, so we
        raise instead and the failure is recorded as an ordinary one-sided error.
        """
        if self._abort_on_crash:
            signal.raise_signal(signal.SIGABRT)
        raise duckdb.Error("duckdb CLI process exited unexpectedly (possible engine crash)")

    def _kill(self) -> None:
        """Force-kill the CLI process (used on statement timeout)."""
        with contextlib.suppress(Exception):
            if self._proc.poll() is None:
                self._proc.kill()
        with contextlib.suppress(Exception):
            self._proc.wait(timeout=5)

    def _readline_timed(self, deadline: float) -> str:
        """One line from the CLI, or raise :class:`duckdb.Error` on timeout / EOF.

        Implemented with a helper thread rather than :func:`select.select`: the CLI's stdout is a
        text-mode pipe, and ``select`` on its fileno misses data already sitting in Python's buffer —
        which made every short query look like a hang until the statement timeout fired.
        """
        assert self._proc.stdout is not None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            self._kill()
            raise duckdb.Error(
                f"duckdb statement timed out after {_STATEMENT_TIMEOUT:g}s "
                f"(EQGEN_DUCKDB_STATEMENT_TIMEOUT)"
            )
        holder: list[object] = []

        def _read() -> None:
            try:
                holder.append(self._proc.stdout.readline())
            except Exception as exc:  # noqa: BLE001 — delivered to the waiter below
                holder.append(exc)

        thread = threading.Thread(target=_read, daemon=True)
        thread.start()
        thread.join(timeout=remaining)
        if thread.is_alive():
            self._kill()
            raise duckdb.Error(
                f"duckdb statement timed out after {_STATEMENT_TIMEOUT:g}s "
                f"(EQGEN_DUCKDB_STATEMENT_TIMEOUT)"
            )
        if not holder:
            self._crash()
        outcome = holder[0]
        if isinstance(outcome, Exception):
            raise duckdb.Error(f"duckdb CLI read failed: {outcome}") from outcome
        line = outcome
        if line == "":
            self._crash()  # never returns
        return str(line)

    def _run(self, sql: str) -> list[list[object]]:
        """Execute one statement, returning parsed rows; raise :class:`duckdb.Error` on failure."""
        assert self._proc.stdout is not None
        if self._proc.poll() is not None:
            raise duckdb.Error("duckdb CLI process is dead (previous statement may have timed out)")
        self._send(sql.rstrip().rstrip(";") + ";")
        self._send(f".print {self._token}")
        rows: list[list[object]] = []
        errors: list[str] = []
        saw_token = False
        deadline = time.monotonic() + _STATEMENT_TIMEOUT
        while not saw_token:
            raw = self._readline_timed(deadline)
            line = raw.rstrip("\n")
            if line == self._token:
                saw_token = True
                break
            if line.startswith("{"):
                try:
                    rows.append(json.loads(line, object_pairs_hook=lambda pairs: [v for _, v in pairs]))
                except json.JSONDecodeError:
                    # Truncated / non-JSON stdout that happens to start with `{` — treat as error text
                    # so Database.query can classify it, rather than taking down the campaign.
                    errors.append(line)
            elif line.strip():
                errors.append(line)
        if errors:
            raise duckdb.Error("\n".join(errors))
        return rows

    def execute(self, sql: str, /) -> _CliCursor:
        return _CliCursor(self._run(sql), self, sql)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            if self._proc.poll() is None:
                self._send(".quit")
        with contextlib.suppress(Exception):
            self._proc.wait(timeout=5)
        if self._proc.poll() is None:
            with contextlib.suppress(Exception):
                self._proc.kill()


def connect_cli(database: str | Path = ":memory:") -> DuckDbCliConnection:
    """Open a CLI-backed connection.

    Fuzzing uses ``:memory:``. Metamorphic coverage passes a file path so objects survive the
    process exit that flushes gcov counters — a second ``connect_cli(path)`` sees the same catalog.
    """
    return DuckDbCliConnection(
        resolve_duckdb_cli(),
        abort_on_crash=os.getpid() != _OWNER_PID,
        database=str(database),
    )
