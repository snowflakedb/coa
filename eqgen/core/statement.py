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

"""One SQL statement: some text, and nothing else.

The generator returns a list of these. The version this replaces was a 446-line model carrying
bindings, versioning and result metadata, of which this project used two things: build one from a
string, read the string back.

A class rather than a plain ``str`` so that ``list[Statement]`` in a signature clearly is not a list
of SQL fragments.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Statement:
    """One SQL statement, ready to execute."""

    statement_text: str

    def __post_init__(self) -> None:
        assert self.statement_text, "statement text must be non-empty"

    def startswith(self, prefix: str) -> bool:
        """Convenience for marker checks, which read the leading bytes of the text."""
        return self.statement_text.startswith(prefix)

    def __str__(self) -> str:
        return self.statement_text
