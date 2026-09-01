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

"""Things done to an object that already exists, so they introduce no name::

    CREATE TABLE t_table_1 AS SELECT * FROM t__base      -- an Object (objects.py)
    DELETE FROM t_table_1 WHERE MOD(c_int, 2) = 0        -- Delete, here
    INSERT INTO t_table_1 SELECT ... WHERE MOD(...)      -- Insert, here

The predicate is kept as an expression object, not as text. An earlier version stored
``where_sql: str`` built during generation, which meant the SQL was already fixed to one engine
before any emitter saw it. It worked only because the single predicate ever used, ``MOD(col, 2)
= 1``, happens to be written the same everywhere.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional, TypeVar

from eqgen.ir.expr import ExpressionNode

if TYPE_CHECKING:
    from eqgen.equivalence.ast import QueryNode
    from eqgen.equivalence.visitor import SetupVisitor

from eqgen.equivalence.objects import SetupStep

T = TypeVar("T")


class Action(SetupStep, abc.ABC):
    """A setup step that operates on existing objects and introduces no name."""


@dataclass(frozen=True)
class Delete(Action):
    """``DELETE FROM <target> [WHERE <predicate>]``.

    An absent predicate means delete every row — valid, and used by the degenerate
    delete-all-then-reinsert-all case.
    """

    target: str
    predicate: Optional[ExpressionNode] = None

    def accept(self, visitor: "SetupVisitor[T]") -> T:
        return visitor.visit_delete(self)


@dataclass(frozen=True)
class Insert(Action):
    """``INSERT INTO <target> [(<column_list>)] <body>``.

    Two body shapes:

    * ``select_columns`` + ``source_ref`` (+ optional ``predicate``): re-insert rows read back
      from another relation. The delete/reinsert rewrite needs the predicate to be *the same*
      object the preceding :class:`Delete` used — that identity is why both carry a node rather
      than two independently rendered strings that could drift.
    * ``query``: an arbitrary ``SELECT`` (used when the insert must rewrite a column, e.g. tag
      filler rows as throwaway). When set, the other body fields are ignored.
    """

    target: str
    select_columns: tuple[str, ...] = ()
    source_ref: str = ""
    predicate: Optional[ExpressionNode] = None
    column_list: Optional[tuple[str, ...]] = field(default=None)
    query: Optional["QueryNode"] = field(default=None)

    def accept(self, visitor: "SetupVisitor[T]") -> T:
        return visitor.visit_insert(self)


@dataclass(frozen=True)
class Update(Action):
    """``UPDATE <target> SET <col> = <expr>, ... [WHERE <predicate>]``.

    Assignments stay as expression nodes so the emitter spells them. An absent predicate updates
    every row — valid for the identity no-op rewrite when no filter was available.
    """

    target: str
    assignments: tuple[tuple[str, ExpressionNode], ...]
    predicate: Optional[ExpressionNode] = None

    def accept(self, visitor: "SetupVisitor[T]") -> T:
        return visitor.visit_update(self)
