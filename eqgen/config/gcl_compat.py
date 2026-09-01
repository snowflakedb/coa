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

"""Import shim for the GCL config language, which has two possible layouts.

GCL (`Generic Configuration Language <https://github.com/rix0rrr/gcl>`_, MIT) is a
third-party declarative config language. It can be present in two shapes:

* **PyPI** (``pip install gcl==0.6.10``) — the package root *is* the library, so the
  names live at ``gcl``.
* **Vendored** — a checkout of the upstream repository, in which the library is the inner
  ``gcl/gcl`` package, so the names live at ``gcl.gcl``.

This module resolves whichever is present so no other file has to care. The dependency is
declared as the PyPI package; the fallback exists because a monorepo may still carry a
vendored checkout on ``sys.path``, and importing the outer wrapper by accident yields a
module with none of the names on it — a confusing failure some distance from the cause.

Resolution goes through :mod:`importlib` and checks for the names rather than relying on
import order, so neither layout is privileged and a half-present install fails loudly here
with a message that says what was tried.

Only two names are needed: ``load`` (parse a ``.gcl`` file into a model) and ``TupleLike``
(the parsed-tuple protocol ``base_gcl_tuple`` type-annotates against). GCL ships no type
stubs, so both arrive untyped either way.
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any

_CANDIDATES = ("gcl", "gcl.gcl")
_REQUIRED = ("load", "TupleLike")


def _resolve() -> ModuleType:
    tried: list[str] = []
    for name in _CANDIDATES:
        try:
            module = importlib.import_module(name)
        except ImportError:
            tried.append(f"{name} (not importable)")
            continue
        missing = [attr for attr in _REQUIRED if not hasattr(module, attr)]
        if missing:
            tried.append(f"{name} (missing {', '.join(missing)})")
            continue
        return module
    raise ImportError(
        "could not find a usable GCL installation. Tried: " + "; ".join(tried) + ". Install it with `pip install gcl==0.6.10`."
    )


_gcl = _resolve()

#: Parse a ``.gcl`` file into a model whose keys are readable via ``exportable_keys()``.
load: Any = _gcl.load
#: The parsed-tuple protocol. Untyped upstream, so this is ``Any``.
TupleLike: Any = _gcl.TupleLike

__all__ = ["TupleLike", "load"]
