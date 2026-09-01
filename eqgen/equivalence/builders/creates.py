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

"""Which kind of object holds the rewrite: a view, or a table::

    CREATE VIEW  t AS SELECT * FROM t__base     -- holds no rows; a query reading t gets the
                                                -- view folded in and compiles as one statement
    CREATE TABLE t AS SELECT * FROM t__base     -- holds the rows; a query reads a real table
                                                -- with its own statistics, nothing folds

Same rows either way, different work for the engine. That difference is the point — if the two
give different answers, the engine is wrong, because the data is identical by construction.
"""

from __future__ import annotations

import abc
from typing import ClassVar, Optional, Type

from eqgen.builder.constraint_set import Constraint, ConstraintSet
from eqgen.builder.type_variables import ResultT_co
from eqgen.equivalence.ast import (
    CreateMaterializedView,
    CreateTable,
    CreateTemporaryTable,
    CreateTemporaryView,
    CreateView,
    EqNode,
    QueryNode,
    SelectQuery,
)
from eqgen.equivalence.builders.base import EquivalenceBuilder
from eqgen.equivalence.constraints import (
    AcceptsDmlConstraint,
    ColumnRewriteConstraint,
    ExposedNameConstraint,
    RowFilterConstraint,
)
from eqgen.equivalence.context import EquivalenceContext


class CreateFromQueryBuilder(EquivalenceBuilder[ResultT_co], abc.ABC):
    """Base for the two below. Asks for a query, hands it to :meth:`_wrap`.

    Which constraints get passed down to the query is the part worth reading::

        RowFilterConstraint      -> passed on   (about which rows: a query can do that)
        ColumnRewriteConstraint  -> passed on   (about which columns: same)
        AcceptsDmlConstraint     -> kept here   (about the object; a query cannot be written to)
    """

    #: Read-only kinds advertise the query-layer constraints (so a filter does not exclude them)
    #: but not DML capability.
    _READ_ONLY_SUPPORTED: ClassVar[list[Type[Constraint[EqNode]]]] = [
        RowFilterConstraint,
        ColumnRewriteConstraint,
        ExposedNameConstraint,
    ]
    #: DML-capable kinds additionally advertise ``AcceptsDml``.
    _DML_SUPPORTED: ClassVar[list[Type[Constraint[EqNode]]]] = [
        RowFilterConstraint,
        ColumnRewriteConstraint,
        AcceptsDmlConstraint,
        ExposedNameConstraint,
    ]

    def _query_constraints(self, constraint_set: ConstraintSet[EqNode]) -> ConstraintSet[EqNode]:
        return ConstraintSet(
            [
                constraint_set.get_optional_constraint(RowFilterConstraint),
                constraint_set.get_optional_constraint(ColumnRewriteConstraint),
            ]
        )

    @abc.abstractmethod
    def _wrap(self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]) -> Optional[ResultT_co]:
        """Wrap the dispatched query via this kind's ``build`` constructor.

        ``exposed_name`` is set only at the root (from an :class:`ExposedNameConstraint`), where
        the equivalent takes the base table's own name so the workload can query one name on both
        sides. ``None`` mints a generated name.
        """

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[ResultT_co]:
        query = self.builder_factory.build_subtree(
            QueryNode,  # type: ignore[type-abstract]
            self._query_constraints(constraint_set),
            context,
        )
        if query is None:
            return None
        return self._wrap(query, context, self._exposed_name(constraint_set))


class CreateViewBuilder(CreateFromQueryBuilder[CreateView]):
    """``CREATE VIEW <name> AS <query>``. Cannot be written to; holds no rows."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]) -> CreateView:
        return CreateView.build(context.namer, query, exposed_name=exposed_name)


class CreateTableBuilder(CreateFromQueryBuilder[CreateTable]):
    """``CREATE TABLE <name> AS <query>``. Can be written to, because the rows are really
    there."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._DML_SUPPORTED

    def _wrap(self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]) -> CreateTable:
        return CreateTable.build(context.namer, query, exposed_name=exposed_name)


class CreateTemporaryViewBuilder(CreateFromQueryBuilder[CreateTemporaryView]):
    """``CREATE TEMPORARY VIEW <name> AS <query>``. A view, but dropped when the session ends.

    Worth a separate builder because an engine may resolve and plan a temporary object
    differently — it lives in a different schema, and name resolution has to prefer it.
    """

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]) -> CreateTemporaryView:
        return CreateTemporaryView.build(context.namer, query, exposed_name=exposed_name)


class CreateTemporaryTableBuilder(CreateFromQueryBuilder[CreateTemporaryTable]):
    """``CREATE TEMPORARY TABLE <name> AS <query>``. The rows are really written, then dropped
    with the session. Can be written to, so it takes the DML constraint like a plain table."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._DML_SUPPORTED

    def _wrap(self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]) -> CreateTemporaryTable:
        return CreateTemporaryTable.build(context.namer, query, exposed_name=exposed_name)


class CreateMaterializedViewBuilder(CreateFromQueryBuilder[CreateMaterializedView]):
    """``CREATE MATERIALIZED VIEW <name> AS <query>``. A snapshot heap filled once at create time.

    Same rows as a view over the same query, but the engine stores them and re-plans against a
    physical relation. Not every engine has the construct — DuckDB does not — so dialects that
    cannot run it keep its weight at ``0``.

    The defining query is first written into a **permanent** table, and the matview reads that
    table. PostgreSQL refuses a matview whose query touches a temporary object
    (``materialized views must not use temporary objects``); a temporary view can still appear
    underneath the CTAS — that just copies rows into the permanent table — and the matview then
    only depends on the table.
    """

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> CreateMaterializedView:
        body = CreateTable.build(context.namer, query)
        return CreateMaterializedView.build(context.namer, SelectQuery(body, None), exposed_name=exposed_name)
