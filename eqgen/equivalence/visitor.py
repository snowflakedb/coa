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

"""One ``visit_*`` method per node kind, in two groups::

    SetupVisitor    steps        -> list[Statement]      "CREATE TABLE t AS ..."
    QueryVisitor    query nodes  -> str                  "SELECT * FROM t"

Methods here are abstract, so an emitter that forgets one fails when the class is defined
rather than producing plausible SQL later.

Nodes belonging to a single engine are **not** added here. They go on that engine's own
subclass of these (see ``dialects/duckdb/ast.py``), so a node one engine understands cannot
quietly reach another engine's emitter — there is no method for it, and ``accept`` raises.
"""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING, Generic, TypeVar

if TYPE_CHECKING:
    from eqgen.equivalence.actions import Delete, Insert, Update
    from eqgen.equivalence.ast import (
        CteQuery,
        ExceptAllQuery,
        IntersectAllQuery,
        JoinQuery,
        LateralReprojectQuery,
        QueryNode,
        SelectQuery,
        UnionAllQuery,
    )
    from eqgen.equivalence.objects import (
        SetupStep,
        TableObject,
        ViewObject,
    )

T = TypeVar("T")


class SetupVisitor(Generic[T], abc.ABC):
    """Double-dispatch visitor over setup steps: the objects created and the actions
    performed on them."""

    def visit(self, step: "SetupStep") -> T:
        return step.accept(self)

    # -- objects (things built) -------------------------------------------
    @abc.abstractmethod
    def visit_table_object(self, node: "TableObject") -> T: ...

    @abc.abstractmethod
    def visit_view_object(self, node: "ViewObject") -> T: ...

    # -- actions (operations on objects) ---------------------------------
    @abc.abstractmethod
    def visit_insert(self, node: "Insert") -> T: ...

    @abc.abstractmethod
    def visit_delete(self, node: "Delete") -> T: ...

    @abc.abstractmethod
    def visit_update(self, node: "Update") -> T: ...


class QueryVisitor(Generic[T], abc.ABC):
    """Double-dispatch visitor over defining-query nodes."""

    def visit(self, query: "QueryNode") -> T:
        return query.accept(self)

    @abc.abstractmethod
    def visit_select_query(self, query: "SelectQuery") -> T: ...

    @abc.abstractmethod
    def visit_union_all_query(self, query: "UnionAllQuery") -> T: ...

    @abc.abstractmethod
    def visit_except_all_query(self, query: "ExceptAllQuery") -> T: ...

    @abc.abstractmethod
    def visit_intersect_all_query(self, query: "IntersectAllQuery") -> T: ...

    @abc.abstractmethod
    def visit_join_query(self, query: "JoinQuery") -> T: ...

    @abc.abstractmethod
    def visit_cte_query(self, query: "CteQuery") -> T: ...

    @abc.abstractmethod
    def visit_lateral_reproject_query(self, query: "LateralReprojectQuery") -> T: ...
