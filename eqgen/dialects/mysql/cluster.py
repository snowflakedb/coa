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

"""A throwaway MySQL/MariaDB server for one fuzz process — Docker by default.

Pinned official images (no local compile). Listens on an ephemeral host port bound to
``127.0.0.1`` so concurrent runs do not collide. Started once per process via
:func:`shared_cluster` and removed at exit.

Set ``EQGEN_MYSQL_IMAGE`` / ``EQGEN_MARIADB_IMAGE`` to override image tags.
``EQGEN_MYSQL_BINDIR`` / ``EQGEN_MARIADB_BINDIR`` are reserved for a future local/coverage build;
v1 always uses Docker.
"""

from __future__ import annotations

import atexit
import enum
import os
import shutil
import socket
import subprocess
import time
import uuid
from typing import Optional

#: Default official images. Override with :data:`IMAGE_ENV`.
DEFAULT_IMAGE = "mysql:9.7.2"
DEFAULT_MARIADB_IMAGE = "mariadb:11.4"

IMAGE_ENV = "EQGEN_MYSQL_IMAGE"
MARIADB_IMAGE_ENV = "EQGEN_MARIADB_IMAGE"
BINDIR_ENV = "EQGEN_MYSQL_BINDIR"
MARIADB_BINDIR_ENV = "EQGEN_MARIADB_BINDIR"

SUPERUSER = "root"
MYSQL_COLLATION = "utf8mb4_0900_bin"
MARIADB_COLLATION = "utf8mb4_nopad_bin"

#: sql_mode pins that keep the row oracle honest.
_SQL_MODE = "STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES"

_START_TIMEOUT_SECONDS = 120.0


class Flavor(enum.Enum):
    """Which engine of the family."""

    MYSQL = "mysql"
    MARIADB = "mariadb"


def mysql_image() -> str:
    return os.environ.get(IMAGE_ENV) or DEFAULT_IMAGE


def mariadb_image() -> str:
    return os.environ.get(MARIADB_IMAGE_ENV) or DEFAULT_MARIADB_IMAGE


def collation_for(flavor: Flavor) -> str:
    """NO PAD binary collation per engine.

    MySQL and MariaDB share no NO PAD binary name: ``utf8mb4_0900_bin`` is MySQL-only and
    ``utf8mb4_nopad_bin`` is MariaDB-only. Plain ``utf8mb4_bin`` on MariaDB is PAD SPACE, so
    strings that differ only in trailing spaces compare equal and ``DISTINCT`` / ``GROUP BY``
    may keep a different representative on each side of the oracle — a false mismatch.
    """
    return MYSQL_COLLATION if flavor is Flavor.MYSQL else MARIADB_COLLATION


def docker_available() -> bool:
    """True when ``docker`` is on PATH and the daemon answers."""
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


class MyCluster:
    """One Dockerized mysqld/mariadbd. Construction pulls/runs the container and waits until it accepts connections."""

    def __init__(self, flavor: Flavor = Flavor.MYSQL) -> None:
        bindir_env = BINDIR_ENV if flavor is Flavor.MYSQL else MARIADB_BINDIR_ENV
        if os.environ.get(bindir_env):
            raise RuntimeError(
                f"{bindir_env} is set, but local mysqld is not implemented in v1. "
                f"Unset it to use Docker, or implement the bindir escape hatch."
            )
        if not docker_available():
            raise RuntimeError(
                f"Docker is required for --dialect {flavor.value} (daemon not reachable). "
                f"Install Docker and ensure `docker info` works."
            )
        self.flavor = flavor
        self.image = mysql_image() if flavor is Flavor.MYSQL else mariadb_image()
        self.owner_pid = os.getpid()
        self.port = _free_port()
        self.container = f"eqgen-{flavor.value}-{uuid.uuid4().hex[:12]}"
        self.host = "127.0.0.1"
        self.collation = collation_for(flavor)
        self._stopped = False
        self._version: Optional[str] = None
        self._pull()
        self._run()
        self._wait_ready()
        self._version = self._fetch_version()

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
                "-e",
                "MYSQL_ALLOW_EMPTY_PASSWORD=yes",
                "-e",
                "MYSQL_ROOT_HOST=%",
                "-p",
                f"127.0.0.1:{self.port}:3306",
                self.image,
                "--character-set-server=utf8mb4",
                f"--collation-server={self.collation}",
                f"--sql-mode={_SQL_MODE}",
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
                        f"{self.flavor.value} container {self.container} is not running.\n{logs.stdout}\n{logs.stderr}"
                    ) from last_err
                time.sleep(1.0)
        raise RuntimeError(
            f"{self.flavor.value} in {self.container} did not accept connections within "
            f"{_START_TIMEOUT_SECONDS:.0f}s: {last_err}"
        )

    def ensure_running(self) -> None:
        """Restart the container if mysqld died (one process = whole server)."""
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

    def _fetch_version(self) -> str:
        import pymysql

        conn = pymysql.connect(host=self.host, port=self.port, user=SUPERUSER, password="", autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT VERSION()")
                row = cur.fetchone()
                return f"{self.flavor.value} {row[0]}" if row else f"{self.flavor.value} ({self.image})"
        finally:
            conn.close()

    def server_version(self) -> str:
        """Cached at start so writing a finding cannot die on a dead mysqld."""
        if self._version is None:
            self._version = self._fetch_version()
        return self._version

    def stop(self) -> None:
        if self._stopped or os.getpid() != self.owner_pid:
            return
        self._stopped = True
        subprocess.run(["docker", "rm", "-f", self.container], capture_output=True, text=True, check=False)

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


_shared: dict[Flavor, MyCluster] = {}


def shared_cluster(flavor: Flavor = Flavor.MYSQL) -> MyCluster:
    """Process-wide cluster for *flavor*; constructed on first use."""
    cluster = _shared.get(flavor)
    if cluster is None:
        cluster = MyCluster(flavor=flavor)
        _shared[flavor] = cluster
        atexit.register(cluster.stop)
    return cluster
