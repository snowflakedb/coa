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

"""Dolt cluster lifecycle — local ``dolt sql-server`` or Docker."""

from __future__ import annotations

import atexit
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from typing import Optional

DEFAULT_IMAGE = "dolthub/dolt-sql-server:latest"

IMAGE_ENV = "EQGEN_DOLT_IMAGE"
BINDIR_ENV = "EQGEN_DOLT_BINDIR"

SUPERUSER = "root"
COLLATION = "utf8mb4_0900_bin"

_START_TIMEOUT_SECONDS = 90.0


def dolt_image() -> str:
    return os.environ.get(IMAGE_ENV) or DEFAULT_IMAGE


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    proc = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    return proc.returncode == 0


def cluster_available() -> bool:
    bindir = os.environ.get(BINDIR_ENV)
    if bindir and os.path.isfile(os.path.join(bindir, "dolt")):
        return True
    return docker_available()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _settings(data_dir: str, host: str, port: int) -> str:
    return "\n".join(
        [
            "log_level: error",
            f"data_dir: {data_dir}",
            "listener:",
            f'  host: "{host}"',
            f"  port: {port}",
            "",
        ]
    )


class DoltCluster:
    """One Dolt SQL server on loopback TCP, torn down at exit."""

    def __init__(self) -> None:
        self.owner_pid = os.getpid()
        self.host = "127.0.0.1"
        self.port = _free_port()
        self.collation = COLLATION
        self._stopped = False
        self._process: Optional[subprocess.Popen[bytes]] = None
        self._container: Optional[str] = None
        self._root: Optional[str] = None
        self._bindir: Optional[str] = None
        self._version: Optional[str] = None
        self.image = dolt_image()
        bindir = os.environ.get(BINDIR_ENV)
        if not bindir:
            # Prefer a local dolt binary when present — Docker images have flipped root-auth
            # defaults (Access denied for user 'root' with empty password).
            candidate = os.path.expanduser("~/dbfuzz-engines/dolt-main/bin")
            if os.path.isfile(os.path.join(candidate, "dolt")):
                bindir = candidate
        if bindir and os.path.isfile(os.path.join(bindir, "dolt")):
            self._bindir = bindir
            self._start_local()
        elif docker_available():
            self._start_docker()
        else:
            raise RuntimeError(
                f"Dolt requires {BINDIR_ENV} pointing at dolt, or Docker with {IMAGE_ENV} "
                f"(default {DEFAULT_IMAGE})."
            )
        self._wait_ready()
        self._version = self._fetch_version()

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self._root is not None:
            env["HOME"] = self._root
            env.setdefault("DOLT_ROOT_PATH", self._root)
        return env

    def _start_local(self) -> None:
        assert self._bindir is not None
        self._root = tempfile.mkdtemp(prefix="eqgen-dolt-")
        datadir = os.path.join(self._root, "data")
        logfile = os.path.join(self._root, "server.log")
        conf = os.path.join(self._root, "dolt.yaml")
        os.mkdir(datadir)
        with open(conf, "w") as handle:
            handle.write(_settings(datadir, self.host, self.port))
        with open(logfile, "ab") as log:
            self._process = subprocess.Popen(
                [os.path.join(self._bindir, "dolt"), "sql-server", f"--config={conf}"],
                stdout=log,
                stderr=subprocess.STDOUT,
                env=self._env(),
            )

    def _start_docker(self) -> None:
        proc = subprocess.run(
            ["docker", "pull", self.image],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"docker pull {self.image} failed: {proc.stderr.strip() or proc.stdout.strip()}")
        self._container = f"eqgen-dolt-{uuid.uuid4().hex[:12]}"
        proc = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                self._container,
                "-p",
                f"127.0.0.1:{self.port}:3306",
                self.image,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"docker run failed: {proc.stderr.strip() or proc.stdout.strip()}")

    def _wait_ready(self) -> None:
        import pymysql

        deadline = time.monotonic() + _START_TIMEOUT_SECONDS
        last_err: Optional[BaseException] = None
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                raise RuntimeError(f"dolt sql-server exited during start ({self._process.returncode})")
            if self._container is not None:
                status = subprocess.run(
                    ["docker", "inspect", "-f", "{{.State.Running}}", self._container],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if status.returncode == 0 and status.stdout.strip() != "true":
                    logs = subprocess.run(
                        ["docker", "logs", "--tail", "40", self._container],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    raise RuntimeError(
                        f"Dolt container {self._container} is not running.\n{logs.stdout}\n{logs.stderr}"
                    )
            try:
                conn = pymysql.connect(
                    host=self.host,
                    port=self.port,
                    user=SUPERUSER,
                    password="",
                    autocommit=True,
                    connect_timeout=2,
                )
                conn.close()
                return
            except Exception as exc:
                last_err = exc
                time.sleep(1.0)
        raise RuntimeError(
            f"Dolt did not accept connections within {_START_TIMEOUT_SECONDS:.0f}s: {last_err}"
        )

    def ensure_running(self) -> None:
        if self._stopped:
            return
        import pymysql

        try:
            conn = pymysql.connect(
                host=self.host,
                port=self.port,
                user=SUPERUSER,
                password="",
                autocommit=True,
                connect_timeout=2,
            )
            conn.close()
            return
        except Exception:
            pass
        if self._container is not None:
            subprocess.run(["docker", "start", self._container], capture_output=True, text=True, check=False)
            self._wait_ready()
        elif self._process is not None:
            if self._process.poll() is None:
                return
            self._start_local()
            self._wait_ready()

    def _fetch_version(self) -> str:
        import pymysql

        conn = pymysql.connect(host=self.host, port=self.port, user=SUPERUSER, password="", autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT VERSION()")
                row = cur.fetchone()
                if row:
                    return f"dolt {row[0]}"
        finally:
            conn.close()
        if self._bindir:
            return f"dolt ({self._bindir})"
        return f"dolt (docker {self.image})"

    def server_version(self) -> str:
        """Cached at start so writing a finding cannot die on a dead sql-server."""
        if self._version is None:
            self._version = self._fetch_version()
        return self._version

    def stop(self) -> None:
        if self._stopped or os.getpid() != self.owner_pid:
            return
        self._stopped = True
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._process.kill()
        if self._container is not None:
            subprocess.run(["docker", "rm", "-f", self._container], capture_output=True, text=True, check=False)
        if self._root is not None:
            shutil.rmtree(self._root, ignore_errors=True)

    def connect_kwargs(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "user": SUPERUSER,
            "password": "",
            "autocommit": True,
            "charset": "utf8mb4",
            "collation": self.collation,
        }


_shared: Optional[DoltCluster] = None


def shared_cluster() -> DoltCluster:
    global _shared
    if _shared is None:
        _shared = DoltCluster()
        atexit.register(_shared.stop)
    return _shared
