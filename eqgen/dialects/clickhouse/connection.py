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

"""HTTP connection the ClickHouse adapter hands the harness.

One private ``Atomic`` database per ``connect()``. Stdlib only — no ClickHouse driver.
"""

from __future__ import annotations

import contextlib
import http.client
import itertools
import json
import os
import signal
import urllib.parse
from typing import Any, Optional, Sequence

from eqgen.dialects.clickhouse import cluster

_OWNER_PID = os.getpid()
_COUNTER = itertools.count(1)
_FORMAT = "JSONCompactEachRowWithNamesAndTypes"
_DescriptionRow = tuple[str, str, None, None, None, None, None]


class ClickHouseError(Exception):
    """SQL / transport error from the ClickHouse server."""

    def __init__(self, message: str, *, code: Optional[int] = None, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _parse_error(body: str, status: int) -> ClickHouseError:
    code: Optional[int] = None
    text = body.strip()
    if text.startswith("Code:"):
        head = text[len("Code:") :].lstrip()
        digits = head.split(".", 1)[0].strip()
        if digits.isdigit():
            code = int(digits)
    return ClickHouseError(text or f"HTTP {status} with empty body", code=code, status=status)


class _ChCursor:
    def __init__(self, rows: list[list[Any]], names: Sequence[str], types: Sequence[str]) -> None:
        self._rows = rows
        self._description: list[_DescriptionRow] = [
            (str(name), str(type_name), None, None, None, None, None)
            for name, type_name in zip(names, types)
        ]

    def fetchall(self) -> list[list[Any]]:
        return self._rows

    def fetchone(self) -> Optional[list[Any]]:
        return self._rows[0] if self._rows else None

    @property
    def description(self) -> Sequence[_DescriptionRow]:
        return self._description


class ChConnection:
    def __init__(self, endpoint: str, *, abort_on_crash: bool) -> None:
        parsed = urllib.parse.urlparse(endpoint)
        self._host = parsed.hostname or "127.0.0.1"
        self._port = parsed.port or 8123
        self._abort_on_crash = abort_on_crash
        self._conn = http.client.HTTPConnection(self._host, self._port, timeout=300)
        self._database: Optional[str] = None
        database = f"eqgen_{os.getpid()}_{next(_COUNTER)}"
        self._request(f"CREATE DATABASE {database} ENGINE = Atomic")
        self._database = database

    def _request(self, sql: str) -> tuple[str, str]:
        params: dict[str, str] = {"default_format": _FORMAT}
        if self._database is not None:
            params["database"] = self._database
        path = "/?" + urllib.parse.urlencode(params)
        try:
            self._conn.request("POST", path, body=sql.encode("utf-8"))
            response = self._conn.getresponse()
            body = response.read().decode("utf-8", errors="replace")
            status = response.status
        except (http.client.HTTPException, OSError) as exc:
            self._crash(f"connection to clickhouse server lost: {exc}")
            raise
        if status != 200:
            raise _parse_error(body, status)
        return body, response.getheader("Content-Type", "") or ""

    def _crash(self, message: str) -> None:
        if self._abort_on_crash:
            signal.raise_signal(signal.SIGABRT)
        raise ClickHouseError(message)

    def execute(self, sql: str, /) -> _ChCursor:
        body, _ = self._request(sql)
        lines = [line for line in body.split("\n") if line.strip()]
        if not lines:
            return _ChCursor([], (), ())
        names = json.loads(lines[0])
        types = json.loads(lines[1]) if len(lines) > 1 else []
        rows = [json.loads(line) for line in lines[2:]]
        return _ChCursor(rows, names, types)

    def close(self) -> None:
        database, self._database = self._database, None
        if database is not None:
            with contextlib.suppress(Exception):
                self._request(f"DROP DATABASE IF EXISTS {database} SYNC")
        with contextlib.suppress(Exception):
            self._conn.close()


def connect() -> ChConnection:
    return ChConnection(cluster.shared_cluster().url, abort_on_crash=os.getpid() != _OWNER_PID)
