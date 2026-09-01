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

"""Ensure a pinned SQLite amalgamation is on disk and preloaded into the process.

System Python on this host links ``_sqlite3`` against ``libsqlite3.so.0`` (often 3.34.x).
eqgen hunts want the latest release (currently **3.53.4**). This module downloads the
official amalgamation into ``~/.cache/eqgen/sqlite-3.53.4/``, builds ``libsqlite3.so.0``,
and ``ctypes.CDLL(..., RTLD_GLOBAL)`` loads it before ``import sqlite3`` so the extension
binds the cached library.

Overrides:
  EQGEN_SQLITE_LIB     absolute path to a ``libsqlite3.so*`` to preload
  EQGEN_SQLITE_LIBDIR  directory containing ``libsqlite3.so.0``
  EQGEN_SQLITE_ALLOW_OLD=1  skip the minimum-version check (tests / emergencies only)
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

PINNED_VERSION = "3.53.4"
AMALGAMATION_YEAR = "2026"
AMALGAMATION_NAME = "sqlite-amalgamation-3530400"
AMALGAMATION_ZIP = f"{AMALGAMATION_NAME}.zip"
DOWNLOAD_URL = f"https://www.sqlite.org/{AMALGAMATION_YEAR}/{AMALGAMATION_ZIP}"
MIN_VERSION = (3, 53, 0)

_CACHE_ROOT = Path(os.environ.get("EQGEN_SQLITE_CACHE", Path.home() / ".cache" / "eqgen"))
_DEFAULT_DIR = _CACHE_ROOT / f"sqlite-{PINNED_VERSION}"

_bootstrapped = False
_lib_path: Path | None = None


def cache_dir() -> Path:
    override = os.environ.get("EQGEN_SQLITE_LIBDIR")
    return Path(override) if override else _DEFAULT_DIR


def lib_path() -> Path:
    override = os.environ.get("EQGEN_SQLITE_LIB")
    if override:
        return Path(override)
    return cache_dir() / "libsqlite3.so.0"


def _parse_version(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in text.split(".")[:3])


def ensure_amalgamation(directory: Path | None = None) -> Path:
    """Download + compile if needed; return path to ``libsqlite3.so.0``."""
    directory = directory or cache_dir()
    directory.mkdir(parents=True, exist_ok=True)
    so = directory / "libsqlite3.so.0"
    if so.is_file() and so.stat().st_size > 0:
        return so

    src = directory / "sqlite3.c"
    if not src.is_file():
        zip_path = directory / AMALGAMATION_ZIP
        if not zip_path.is_file():
            urllib.request.urlretrieve(DOWNLOAD_URL, zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(directory)
        nested = directory / AMALGAMATION_NAME
        if nested.is_dir():
            for item in nested.iterdir():
                dest = directory / item.name
                if not dest.exists():
                    item.rename(dest)

    if not src.is_file():
        raise RuntimeError(f"sqlite amalgamation missing at {src} after download")

    cmd = [
        "gcc",
        "-shared",
        "-fPIC",
        "-O2",
        "-o",
        str(so),
        str(src),
        "-ldl",
        "-lpthread",
        "-lm",
    ]
    subprocess.run(cmd, check=True, cwd=directory)
    if not so.is_file():
        raise RuntimeError(f"failed to build {so}")
    return so


def bootstrap(*, require_min: bool = True) -> str:
    """Preload the pinned libsqlite and return ``sqlite3.sqlite_version``.

    Safe to call multiple times. Must run before any other import of ``sqlite3`` in this
    process for the preload to take effect.

    ``EQGEN_SQLITE_LIB`` / ``EQGEN_SQLITE_LIBDIR`` select an alternate build (e.g. trunk
    ``~/.cache/eqgen/sqlite-trunk/libsqlite3.so.0``) without re-downloading the pin.
    """
    global _bootstrapped, _lib_path
    if _bootstrapped:
        import sqlite3

        return sqlite3.sqlite_version

    override = os.environ.get("EQGEN_SQLITE_LIB")
    if override:
        path = Path(override)
        if not path.is_file():
            raise FileNotFoundError(f"EQGEN_SQLITE_LIB not found: {path}")
    else:
        path = ensure_amalgamation()
    _lib_path = path
    libdir = str(path.parent)
    existing = os.environ.get("LD_LIBRARY_PATH", "")
    if libdir not in existing.split(":"):
        os.environ["LD_LIBRARY_PATH"] = f"{libdir}:{existing}" if existing else libdir

    mode = getattr(os, "RTLD_GLOBAL", None)
    if mode is None:
        mode = ctypes.RTLD_GLOBAL
    ctypes.CDLL(str(path), mode=mode)

    import sqlite3

    version = sqlite3.sqlite_version
    if require_min and os.environ.get("EQGEN_SQLITE_ALLOW_OLD") != "1":
        if _parse_version(version) < MIN_VERSION:
            raise RuntimeError(
                f"eqgen requires SQLite >= {'.'.join(map(str, MIN_VERSION))} for --dialect sqlite, "
                f"got {version}. Preload failed (still using system lib?). "
                f"Built library was {path}. Set EQGEN_SQLITE_LIB / EQGEN_SQLITE_LIBDIR, or "
                f"EQGEN_SQLITE_ALLOW_OLD=1 to bypass."
            )
    _bootstrapped = True
    return version


def library_label() -> str:
    """Short path fragment for banners."""
    path = _lib_path or lib_path()
    return str(path)


if __name__ == "__main__":
    ver = bootstrap()
    print(f"sqlite {ver} from {library_label()}", file=sys.stderr)
    print(ver)
