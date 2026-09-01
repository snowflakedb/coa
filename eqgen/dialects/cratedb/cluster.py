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

"""A throwaway CrateDB server for one fuzz process — Docker by default.

Listens on an ephemeral host port bound to ``127.0.0.1``. Started once per process via
:func:`shared_cluster` and removed at exit.

Set ``EQGEN_CRATEDB_IMAGE`` to override the image tag (default ``crate:6.4.1``).
"""

from __future__ import annotations

import atexit
import os
import shutil
import socket
import subprocess
import time
import uuid
from typing import Optional

DEFAULT_IMAGE = "crate:6.4.1"
IMAGE_ENV = "EQGEN_CRATEDB_IMAGE"
SUPERUSER = "crate"
DATABASE = "doc"

_START_TIMEOUT_SECONDS = 120.0


def cratedb_image() -> str:
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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class CrateCluster:
    """One Dockerized CrateDB node. Construction pulls/runs the container and waits until psycopg connects."""

    def __init__(self) -> None:
        if not docker_available():
            raise RuntimeError(
                "Docker is required for --dialect cratedb (daemon not reachable). "
                f"Install Docker and ensure `docker info` works, or set {IMAGE_ENV} after fixing Docker."
            )
        self.image = cratedb_image()
        self.owner_pid = os.getpid()
        self.port = _free_port()
        self.container = f"eqgen-crate-{uuid.uuid4().hex[:12]}"
        self.host = "127.0.0.1"
        self._stopped = False
        self._pull()
        self._run()
        self._wait_ready()

    def _pull(self) -> None:
        proc = subprocess.run(
            ["docker", "pull", self.image],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"docker pull {self.image} failed: {proc.stderr.strip() or proc.stdout.strip()}")

    def _run(self) -> None:
        proc = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                self.container,
                "-p",
                f"127.0.0.1:{self.port}:5432",
                self.image,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"docker run failed: {proc.stderr.strip() or proc.stdout.strip()}")

    def _wait_ready(self) -> None:
        import psycopg

        deadline = time.monotonic() + _START_TIMEOUT_SECONDS
        last_err: Optional[BaseException] = None
        while time.monotonic() < deadline:
            try:
                conn = psycopg.connect(self.dsn, connect_timeout=2)
                conn.execute("SELECT 1")
                conn.close()
                return
            except Exception as exc:
                last_err = exc
                status = subprocess.run(
                    ["docker", "inspect", "-f", "{{.State.Running}}", self.container],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if status.returncode == 0 and status.stdout.strip() != "true":
                    logs = subprocess.run(
                        ["docker", "logs", "--tail", "40", self.container],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    raise RuntimeError(
                        f"CrateDB container {self.container} is not running.\n{logs.stdout}\n{logs.stderr}"
                    ) from last_err
                time.sleep(1.0)
        raise RuntimeError(
            f"CrateDB in {self.container} did not accept connections within {_START_TIMEOUT_SECONDS:.0f}s: {last_err}"
        )

    def ensure_running(self) -> None:
        status = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", self.container],
            capture_output=True,
            text=True,
            check=False,
        )
        if status.returncode == 0 and status.stdout.strip() == "true":
            return
        subprocess.run(["docker", "start", self.container], capture_output=True, text=True, check=False)
        self._wait_ready()

    def server_version(self) -> str:
        import psycopg

        conn = psycopg.connect(self.dsn, connect_timeout=5)
        try:
            row = conn.execute("SELECT version()").fetchone()
            return f"cratedb {row[0]}" if row else f"cratedb ({self.image})"
        finally:
            conn.close()

    def stop(self) -> None:
        if self._stopped or os.getpid() != self.owner_pid:
            return
        self._stopped = True
        subprocess.run(["docker", "rm", "-f", self.container], capture_output=True, text=True, check=False)

    @property
    def dsn(self) -> str:
        return f"host={self.host} port={self.port} user={SUPERUSER} dbname={DATABASE}"


_shared: Optional[CrateCluster] = None


def shared_cluster() -> CrateCluster:
    global _shared
    if _shared is None:
        _shared = CrateCluster()
        atexit.register(_shared.stop)
    return _shared
