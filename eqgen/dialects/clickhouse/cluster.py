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

"""Throwaway local ClickHouse server for the differential-fuzzing harness.

``clickhouse local`` is not viable: without ``--ignore-error`` the first SQL error kills
the session; with it, errors are suppressed. So we run ``clickhouse server`` and talk HTTP
(see :mod:`connection`). Ported from dbfuzz's ClickHouse cluster.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

BINARY_ENV = "EQGEN_CLICKHOUSE_BIN"
#: Prefer eqgen's own cache; fall back to dbfuzz's if already present.
CACHE_DIR = Path(os.path.expanduser("~/.cache/eqgen/clickhouse"))
CACHED_BINARY = CACHE_DIR / "clickhouse"
MARKER_PATH = CACHE_DIR / "eqgen_build.json"
_DBFUZZ_BINARY = Path(os.path.expanduser("~/.cache/dbfuzz/clickhouse/clickhouse"))

_START_TIMEOUT_SECONDS = 60.0

_PROFILE_SETTINGS: tuple[tuple[str, str], ...] = (
    ("join_use_nulls", "1"),
    ("max_threads", "1"),
    ("database_atomic_wait_for_drop_and_detach_synchronously", "1"),
    ("default_table_engine", "MergeTree"),
    ("create_table_empty_primary_key_by_default", "1"),
    ("max_execution_time", "60"),
    ("mutations_sync", "2"),
    # Surface #111901-class wrong results when a builder emits a reverse key.
    ("optimize_aggregation_in_order", "1"),
)

USER = "default"

_CONFIG_XML = """<clickhouse>
    <logger>
        <level>warning</level>
        <log>{root}/logs/server.log</log>
        <errorlog>{root}/logs/server.err.log</errorlog>
        <size>50M</size>
        <count>1</count>
    </logger>
    <http_port>{http_port}</http_port>
    <listen_host>127.0.0.1</listen_host>
    <path>{root}/data/</path>
    <tmp_path>{root}/tmp/</tmp_path>
    <user_files_path>{root}/user_files/</user_files_path>
    <format_schema_path>{root}/format_schemas/</format_schema_path>
    <user_directories>
        <users_xml><path>{root}/users.xml</path></users_xml>
        <local_directory><path>{root}/access/</path></local_directory>
    </user_directories>
    <mark_cache_size>268435456</mark_cache_size>
    <mlock_executable>false</mlock_executable>
</clickhouse>
"""

_USERS_XML = """<clickhouse>
    <profiles>
        <default>
{settings}
        </default>
    </profiles>
    <users>
        <default>
            <password></password>
            <networks><ip>127.0.0.1</ip></networks>
            <profile>default</profile>
            <quota>default</quota>
            <access_management>1</access_management>
        </default>
    </users>
    <quotas><default/></quotas>
</clickhouse>
"""


def arch_tag() -> str:
    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        return "aarch64"
    if machine in ("x86_64", "amd64"):
        return "amd64"
    raise RuntimeError(f"no ClickHouse build known for machine {machine!r}")


def download_url() -> str:
    return f"https://builds.clickhouse.com/master/{arch_tag()}/clickhouse"


def ensure_latest_clickhouse() -> Path:
    """Download the newest ``master`` ClickHouse binary into the eqgen cache."""
    url = download_url()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    staged = CACHE_DIR / f".clickhouse.download.{os.getpid()}"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
        with urllib.request.urlopen(request, timeout=600) as response:
            staged.write_bytes(response.read())
        staged.chmod(0o755)
        os.replace(staged, CACHED_BINARY)
    finally:
        staged.unlink(missing_ok=True)
    _write_marker(CACHED_BINARY, url)
    return CACHED_BINARY


def _write_marker(binary: Path, url: str) -> None:
    MARKER_PATH.write_text(
        json.dumps(
            {
                "engine": "clickhouse",
                "branch": "master",
                "remote": url,
                "source_version": engine_version(binary),
                "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "assertions": False,
            },
            indent=2,
        )
        + "\n"
    )


def read_marker() -> Optional[dict[str, object]]:
    try:
        loaded = json.loads(MARKER_PATH.read_text())
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def clickhouse_binary() -> Path:
    """Locate the server binary: env override, eqgen cache, else dbfuzz cache."""
    override = os.environ.get(BINARY_ENV) or os.environ.get("DBFUZZ_CLICKHOUSE_BIN")
    if override:
        path = Path(override)
        if not path.is_file():
            raise RuntimeError(f"CLICKHOUSE_BIN={override!r} is not a file")
        return path
    if CACHED_BINARY.is_file():
        return CACHED_BINARY
    if _DBFUZZ_BINARY.is_file():
        return _DBFUZZ_BINARY
    raise RuntimeError(
        f"no ClickHouse binary found (looked at ${BINARY_ENV}, {CACHED_BINARY}, {_DBFUZZ_BINARY}).\n"
        f"  fetch: run without --no-download-clickhouse\n"
        f"  or:    --clickhouse-bin /path/to/clickhouse"
    )


def engine_version(binary: Path) -> str:
    proc = subprocess.run(
        [str(binary), "local", "--version"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    text = (proc.stdout or "") + (proc.stderr or "")
    for token in text.split():
        if token and token[0].isdigit() and "." in token:
            return token
    return "unknown"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ChCluster:
    """Private ClickHouse server in a temp directory, HTTP on localhost."""

    def __init__(self) -> None:
        self.binary = clickhouse_binary()
        self.version = engine_version(self.binary)
        self.root = tempfile.mkdtemp(prefix="eqgen-ch-")
        self.http_port = _free_port()
        self.owner_pid = os.getpid()
        self._stopped = False
        self._proc: Optional[subprocess.Popen[bytes]] = None
        self._write_config()
        self._start()

    def _write_config(self) -> None:
        for sub in ("data", "logs", "tmp", "user_files", "format_schemas", "access"):
            os.makedirs(os.path.join(self.root, sub), exist_ok=True)
        settings = "\n".join(f"            <{key}>{value}</{key}>" for key, value in _PROFILE_SETTINGS)
        Path(self.root, "users.xml").write_text(_USERS_XML.format(settings=settings))
        Path(self.root, "config.xml").write_text(
            _CONFIG_XML.format(root=self.root, http_port=self.http_port)
        )

    def _start(self) -> None:
        self._proc = subprocess.Popen(
            [str(self.binary), "server", f"--config-file={os.path.join(self.root, 'config.xml')}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=self.root,
        )
        deadline = time.monotonic() + _START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"clickhouse server exited immediately (rc={self._proc.returncode}) "
                    f"[{self.binary}, version {self.version}]\n{self.log_tail()}"
                )
            if self._ping():
                return
            time.sleep(0.1)
        self.stop()
        raise RuntimeError(
            f"clickhouse server did not become ready within {_START_TIMEOUT_SECONDS:.0f}s "
            f"[{self.binary}, version {self.version}]\n{self.log_tail()}"
        )

    def _ping(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.url}ping", timeout=2) as response:
                return bool(response.read().strip() == b"Ok.")
        except (urllib.error.URLError, OSError):
            return False

    def stop(self) -> None:
        if self._stopped or os.getpid() != self.owner_pid:
            return
        self._stopped = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=5)
        shutil.rmtree(self.root, ignore_errors=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.http_port}/"

    def log_tail(self, lines: int = 40) -> str:
        out = []
        for name in ("server.err.log", "server.log"):
            path = os.path.join(self.root, "logs", name)
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    tail = handle.readlines()[-lines:]
            except OSError:
                continue
            if tail:
                out.append(f"--- {name} ---\n" + "".join(tail))
        return "\n".join(out)


_CLUSTER: Optional[ChCluster] = None


def _install_teardown(stop: Callable[[], None]) -> None:
    atexit.register(stop)

    def _handler(signum: int, frame: object) -> None:
        del frame
        stop()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except ValueError:
            pass


def shared_cluster() -> ChCluster:
    global _CLUSTER
    if _CLUSTER is None:
        _CLUSTER = ChCluster()
        _install_teardown(_CLUSTER.stop)
    return _CLUSTER
