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

"""Things that get created and so introduce a name.

A step is one thing to do while building, and there are two kinds::

    Object (here)                CREATE TABLE t_table_1 AS ...   -- introduces a name
    Action (in actions.py)       DELETE FROM t_table_1 ...       -- uses an existing one

Having both is why one relation can take several statements.

A step holds a name and a structure, never SQL text — the emitter writes the text, which is how
one generated tree can be rendered for different engines. The name is settled here because a
parent needs to reference a finished child.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, TypeVar

if TYPE_CHECKING:
    from eqgen.equivalence.ast import QueryNode
    from eqgen.equivalence.visitor import SetupVisitor

T = TypeVar("T")


class SetupStep(abc.ABC):
    """One ordered build step, rendering to one or a few statements."""

    @abc.abstractmethod
    def accept(self, visitor: "SetupVisitor[T]") -> T:
        """Double-dispatch into a :class:`~eqgen.equivalence.visitor.SetupVisitor`."""


@dataclass(frozen=True)
class Object(SetupStep):
    """A setup step that defines a referenceable named object."""

    name: str

    def ref_sql(self) -> str:
        """How another statement FROM-references this object."""
        return self.name


@dataclass(frozen=True)
class _CreateAsSelectObject(Object):
    """``CREATE <keyword> <name> AS <body>`` — shared by the table and view families.

    The body comes from ``query`` (rendered inline by the emitter) or, when ``query`` is
    absent, from the pre-decided ``definition_sql`` fragment. ``keyword`` is data —
    ``TABLE``, ``VIEW``, ``TEMPORARY TABLE``, ``MATERIALIZED VIEW`` — which is what keeps
    one emitter method serving the whole family, and lets a dialect rewrite the keyword
    without touching the node.
    """

    keyword: str = ""
    query: Optional["QueryNode"] = None
    definition_sql: str = ""


@dataclass(frozen=True)
class TableObject(_CreateAsSelectObject):
    """``CREATE [TEMPORARY] TABLE <name> AS <query>``."""

    def accept(self, visitor: "SetupVisitor[T]") -> T:
        return visitor.visit_table_object(self)


@dataclass(frozen=True)
class ViewObject(_CreateAsSelectObject):
    """``CREATE [TEMPORARY|MATERIALIZED] VIEW <name> AS <query>``."""

    def accept(self, visitor: "SetupVisitor[T]") -> T:
        return visitor.visit_view_object(self)
