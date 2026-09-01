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

"""The nodes a rewrite is built from.

Three kinds, and the difference is whether the thing has a name::

    BaseTableSource      the existing table. No DDL; referenced as        t__base
    QueryNode            the part after AS. Never named, written inline:  SELECT * FROM t__base
    EquivalentRelation   a real object with a name:      CREATE VIEW t_view_1 AS <QueryNode>

An :class:`EquivalentRelation` is also an :class:`EquivalentSource`, which is the one fact that
makes rewrites stack. "Read the base table" and "read a rewrite" are the same request, so a
query's source can be either::

    CREATE VIEW t_view_1 AS SELECT * FROM t__base      -- source: the base table
    CREATE VIEW t_view_2 AS SELECT * FROM t_view_1     -- source: another relation

Names are fixed in ``build()``, never later, so a builder can read its finished child's name.

One relation can need several statements, which is why ``steps`` is a list::

    CREATE TABLE t_table_1 AS SELECT * FROM t__base
    DELETE FROM t_table_1 WHERE ...
    INSERT INTO t_table_1 SELECT ... FROM t__base WHERE ...

To add a kind: one class, one ``build()``, and — only if its statement shape is new — one
``visit_*`` on the emitter. There is no registry to update.
"""

from __future__ import annotations

import abc
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Sequence, TypeVar

from eqgen.core.catalog import Named, Relation, Table
from eqgen.core.types import SqlType
from eqgen.equivalence.actions import Delete, Insert, Update
from eqgen.equivalence.capabilities import ObjectKind
from eqgen.equivalence.objects import Object, SetupStep, TableObject, ViewObject
from eqgen.ir.expr import ExpressionNode

if TYPE_CHECKING:
    from eqgen.equivalence.context import ObjectNamer
    from eqgen.equivalence.visitor import QueryVisitor

_T = TypeVar("_T")


class EqNode(Relation, abc.ABC):
    """A node in the equivalence AST: its inputs, its signature, and how to reference it."""

    def __init__(self, inputs: Sequence["EqNode"]) -> None:
        super().__init__()
        self._inputs: tuple[EqNode, ...] = tuple(inputs)

    def children(self) -> tuple["EqNode", ...]:
        """This node's inputs, walked before the node itself."""
        return self._inputs

    @abc.abstractmethod
    def get_signature(self) -> list[Named[SqlType]]:
        """The output columns (name + type) of this node's relation."""

    @abc.abstractmethod
    def ref_sql(self) -> str:
        """The SQL fragment another statement uses to reference this node's output."""


class EquivalentSource(EqNode, abc.ABC):
    """Anything that can go after ``FROM`` and holds the base table's rows — the base table
    itself, or a rewrite. A builder asks for one of these rather than picking a class, which is
    what lets rewrites stack."""


class BaseTableSource(EquivalentSource):
    """The existing table. Emits no statement; just gets named after ``FROM``."""

    def __init__(self, table: Table) -> None:
        super().__init__([])
        self._table = table

    @property
    def table(self) -> Table:
        return self._table

    def get_signature(self) -> list[Named[SqlType]]:
        return self._table.get_signature()

    def ref_sql(self) -> str:
        return self._table.get_sql_name(use_schema_name=True)


# ---------------------------------------------------------------------------
# Query nodes — the defining query; not materialized, never named
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectionItem:
    """One entry in a ``SELECT`` list: the output name, the expression, and its type::

        alias="c_int", expr=col("c_int")        ->  c_int
        alias="c_int", expr=mod(col("c_int"))   ->  MOD(c_int, 2) AS c_int

    The ``AS`` appears only when the expression differs from the name.
    """

    alias: str
    expr: ExpressionNode
    data_type: SqlType


class QueryNode(EqNode, abc.ABC):
    """The query that goes after ``CREATE ... AS``. Emits no statement of its own; its sources
    are created first."""

    def ref_sql(self) -> str:
        raise TypeError("query nodes are rendered inline by their relation, not referenced by name")

    @abc.abstractmethod
    def accept(self, visitor: "QueryVisitor[_T]") -> _T:
        """Double-dispatch into a :class:`~eqgen.equivalence.visitor.QueryVisitor`."""


class SelectQuery(QueryNode):
    """``SELECT <projection> FROM <source> [WHERE …] [QUALIFY …] [GROUP BY …]``::

        projection=None   ->  SELECT * FROM t              -- columns follow the source
        projection=(...)  ->  SELECT MOD(c_int, 2) AS c_int, c_txt FROM t
        group_by=(...)    ->  ... GROUP BY eq_key_1         -- with aggregate projection items
        qualify=(...)     ->  ... QUALIFY ROW_NUMBER() … >= 1  (DuckDB; PG emitter rewrites)
        order_by=(...)    ->  inner ORDER BY for ordered-scan (multiset-neutral)

    A plugin predicate reaches SQL either through ``predicate`` here or through a ``CASE``
    condition in a projection item.
    """

    def __init__(
        self,
        source: EqNode,
        projection: Optional[Sequence[ProjectionItem]] = None,
        predicate: Optional[ExpressionNode] = None,
        *,
        distinct: bool = False,
        group_by: Optional[Sequence[ExpressionNode]] = None,
        qualify: Optional[ExpressionNode] = None,
        order_by: Optional[Sequence[ExpressionNode]] = None,
    ) -> None:
        super().__init__([source])
        self._source = source
        self._projection: Optional[tuple[ProjectionItem, ...]] = None if projection is None else tuple(projection)
        self._predicate = predicate
        self._distinct = distinct
        self._group_by: Optional[tuple[ExpressionNode, ...]] = None if group_by is None else tuple(group_by)
        self._qualify = qualify
        self._order_by: Optional[tuple[ExpressionNode, ...]] = None if order_by is None else tuple(order_by)

    @property
    def source(self) -> EqNode:
        return self._source

    @property
    def projection(self) -> Optional[tuple[ProjectionItem, ...]]:
        """The explicit projection, or ``None`` for ``SELECT *``."""
        return self._projection

    @property
    def predicate(self) -> Optional[ExpressionNode]:
        return self._predicate

    @property
    def distinct(self) -> bool:
        """``SELECT DISTINCT``. Only keeps the rows when they are already distinct, which is why the
        one builder that sets it puts a per-row key in the projection first."""
        return self._distinct

    @property
    def group_by(self) -> Optional[tuple[ExpressionNode, ...]]:
        """``GROUP BY`` keys, or ``None`` when there is no grouping."""
        return self._group_by

    @property
    def qualify(self) -> Optional[ExpressionNode]:
        """``QUALIFY`` predicate, or ``None``. DuckDB emits it natively; PostgreSQL rewrites."""
        return self._qualify

    @property
    def order_by(self) -> Optional[tuple[ExpressionNode, ...]]:
        """``ORDER BY`` keys (multiset-neutral when the oracle ignores order), or ``None``."""
        return self._order_by

    def get_signature(self) -> list[Named[SqlType]]:
        if self._projection is None:
            return self._source.get_signature()
        return [Named(alias=item.alias, target=item.data_type) for item in self._projection]

    def accept(self, visitor: "QueryVisitor[_T]") -> _T:
        return visitor.visit_select_query(self)


class UnionAllQuery(QueryNode):
    """``SELECT * FROM b1 UNION ALL SELECT * FROM b2 [UNION ALL ...]``, two branches or more.

    Holds no expressions at all. A builder that splits rows does not put the ``WHERE`` here — it
    asks for each branch with a filter constraint and lets the branch apply it. All this node
    does is concatenate.
    """

    def __init__(self, *branches: "EquivalentRelation") -> None:
        assert len(branches) >= 2, "UNION ALL requires at least two branches"
        super().__init__(list(branches))
        self._branches: tuple[EquivalentRelation, ...] = tuple(branches)

    @property
    def branches(self) -> tuple["EquivalentRelation", ...]:
        return self._branches

    def get_signature(self) -> list[Named[SqlType]]:
        return self._branches[0].get_signature()

    def accept(self, visitor: "QueryVisitor[_T]") -> _T:
        return visitor.visit_union_all_query(self)


class ExceptAllQuery(QueryNode):
    """``SELECT * FROM left EXCEPT ALL SELECT * FROM right``.

    Binary only. Used for the bag identity ``R EXCEPT ALL empty``. Engines without
    ``EXCEPT ALL`` must keep the builder weight at 0.
    """

    def __init__(self, left: "EquivalentRelation", right: "EquivalentRelation") -> None:
        super().__init__([left, right])
        self._left = left
        self._right = right

    @property
    def left(self) -> "EquivalentRelation":
        return self._left

    @property
    def right(self) -> "EquivalentRelation":
        return self._right

    def get_signature(self) -> list[Named[SqlType]]:
        return self._left.get_signature()

    def accept(self, visitor: "QueryVisitor[_T]") -> _T:
        return visitor.visit_except_all_query(self)


class IntersectAllQuery(QueryNode):
    """``SELECT * FROM left INTERSECT ALL SELECT * FROM right``.

    Used for the bag identity ``R INTERSECT ALL R``. Engines without ``INTERSECT ALL``
    (or where multiset intersect is not an identity) keep the builder weight at 0.
    """

    def __init__(self, left: "EquivalentRelation", right: "EquivalentRelation") -> None:
        super().__init__([left, right])
        self._left = left
        self._right = right

    @property
    def left(self) -> "EquivalentRelation":
        return self._left

    @property
    def right(self) -> "EquivalentRelation":
        return self._right

    def get_signature(self) -> list[Named[SqlType]]:
        return self._left.get_signature()

    def accept(self, visitor: "QueryVisitor[_T]") -> _T:
        return visitor.visit_intersect_all_query(self)


class JoinQuery(QueryNode):
    """A join of two named relations::

        SELECT l.c_int AS c_int FROM t_view_1 l LEFT OUTER JOIN t_view_2 r ON 1 = 1

    ``join_type`` is the keyword, passed straight through: ``INNER``, ``LEFT OUTER``,
    ``FULL OUTER``, ``ANTI``, ``SEMI``. Add a keyword, not a new class.

    Every projection item names a side, or the SQL is ambiguous when both sides share a column
    name.

    Two builders produce one, both in ``builders/joins.py``.
    """

    def __init__(
        self,
        left: "EquivalentRelation",
        right: "EquivalentRelation",
        condition: Optional[ExpressionNode],
        join_type: str,
        projection: Sequence[ProjectionItem],
        left_alias: str,
        right_alias: str,
        predicate: Optional[ExpressionNode] = None,
    ) -> None:
        super().__init__([left, right])
        self._left = left
        self._right = right
        self._condition = condition
        self._join_type = join_type
        self._projection: tuple[ProjectionItem, ...] = tuple(projection)
        self._left_alias = left_alias
        self._right_alias = right_alias
        self._predicate = predicate

    @property
    def left(self) -> "EquivalentRelation":
        return self._left

    @property
    def right(self) -> "EquivalentRelation":
        return self._right

    @property
    def condition(self) -> Optional[ExpressionNode]:
        return self._condition

    @property
    def join_type(self) -> str:
        return self._join_type

    @property
    def projection(self) -> tuple[ProjectionItem, ...]:
        return self._projection

    @property
    def left_alias(self) -> str:
        return self._left_alias

    @property
    def right_alias(self) -> str:
        return self._right_alias

    @property
    def predicate(self) -> Optional[ExpressionNode]:
        return self._predicate

    def get_signature(self) -> list[Named[SqlType]]:
        return [Named(alias=item.alias, target=item.data_type) for item in self._projection]

    def accept(self, visitor: "QueryVisitor[_T]") -> _T:
        return visitor.visit_join_query(self)


class CteQuery(QueryNode):
    """The read moved into a common table expression::

        WITH t_cte_1 AS (SELECT * FROM t__base WHERE MOD(c_int, 2) = 0)
        SELECT * FROM t_cte_1

    Same rows as reading the source directly, but the engine has to decide whether to fold the CTE
    into the outer query or compute it once — a different plan over identical data.

    Columns follow the source rather than being passed in, so this node cannot be handed a
    projection its input does not have.
    """

    def __init__(
        self,
        source: EqNode,
        cte_name: str,
        predicate: Optional[ExpressionNode] = None,
        *,
        materialize: Optional[bool] = None,
    ) -> None:
        super().__init__([source])
        self._source = source
        self._cte_name = cte_name
        self._predicate = predicate
        # ``True`` / ``False`` -> ``AS MATERIALIZED`` / ``AS NOT MATERIALIZED`` (Postgres, DuckDB).
        self._materialize = materialize

    @property
    def source(self) -> EqNode:
        return self._source

    @property
    def cte_name(self) -> str:
        return self._cte_name

    @property
    def predicate(self) -> Optional[ExpressionNode]:
        return self._predicate

    @property
    def materialize(self) -> Optional[bool]:
        return self._materialize

    def get_signature(self) -> list[Named[SqlType]]:
        return self._source.get_signature()

    def accept(self, visitor: "QueryVisitor[_T]") -> _T:
        return visitor.visit_cte_query(self)


class LateralReprojectQuery(QueryNode):
    """Every row sent through a one-row lateral subquery and back::

        SELECT l.c_int AS c_int, l.c_txt AS c_txt
        FROM t__base s, LATERAL (SELECT s.c_int AS c_int, s.c_txt AS c_txt) AS l

    The lateral produces exactly one row per source row and selects nothing new, so the rows are
    unchanged — while the engine has to run a correlated subquery per row to find that out.

    Like :class:`CteQuery`, the columns come from the source's own signature.
    """

    def __init__(self, source: EqNode, *, source_alias: str = "s", lateral_alias: str = "l") -> None:
        super().__init__([source])
        self._source = source
        self._source_alias = source_alias
        self._lateral_alias = lateral_alias

    @property
    def source(self) -> EqNode:
        return self._source

    @property
    def source_alias(self) -> str:
        return self._source_alias

    @property
    def lateral_alias(self) -> str:
        return self._lateral_alias

    def get_signature(self) -> list[Named[SqlType]]:
        return self._source.get_signature()

    def accept(self, visitor: "QueryVisitor[_T]") -> _T:
        return visitor.visit_lateral_reproject_query(self)


class DialectNativeQuery(QueryNode, abc.ABC):
    """A query only one engine can write. Subclasses live in that engine's package.

    Reaching the wrong emitter raises rather than producing plausible SQL. Try hard not to need
    one: a new keyword usually belongs in :class:`JoinQuery`'s ``join_type`` or another existing
    field. See ``EXTENDING.md``.
    """


# ---------------------------------------------------------------------------
# Relation nodes — the equivalents; steps assembled at construction
# ---------------------------------------------------------------------------


class EquivalentRelation(EquivalentSource, abc.ABC):
    """An object with a name. ``build()`` fixes its name and its ``steps`` there and then, so a
    parent can read a finished child."""

    def __init__(
        self,
        kind: ObjectKind,
        inputs: Sequence[EqNode],
        *,
        steps: Sequence[SetupStep],
        exposed: Object,
    ) -> None:
        super().__init__(inputs)
        self._kind = kind
        self._steps: tuple[SetupStep, ...] = tuple(steps)
        self._exposed = exposed

    @property
    def kind(self) -> ObjectKind:
        return self._kind

    @property
    def steps(self) -> tuple[SetupStep, ...]:
        """The ordered setup steps that materialize this relation."""
        return self._steps

    @property
    def exposed(self) -> Object:
        """The named object a parent FROM-references."""
        return self._exposed

    @property
    def materialized_name(self) -> str:
        return self._exposed.name

    def ref_sql(self) -> str:
        return self.materialized_name


class CreateFromQuery(EquivalentRelation, abc.ABC):
    """A relation that materializes a query via ``CREATE <keyword> <name> AS <query>``."""

    def __init__(
        self,
        kind: ObjectKind,
        query: QueryNode,
        *,
        steps: Sequence[SetupStep],
        exposed: Object,
    ) -> None:
        super().__init__(kind, [query], steps=steps, exposed=exposed)
        self._query = query

    @property
    def query(self) -> QueryNode:
        return self._query

    def get_signature(self) -> list[Named[SqlType]]:
        return self._query.get_signature()

    @staticmethod
    def _create_as_select(
        namer: "ObjectNamer",
        query: QueryNode,
        keyword: str,
        label: str,
        exposed_name: Optional[str],
    ) -> tuple[Object, tuple[SetupStep, ...]]:
        """Take a name and build the single ``CREATE <keyword> <name> AS <query>`` object.

        Every ``CREATE ... AS`` kind shares this, so a new keyword needs no emitter change::

            _create_as_select(namer, query, "MATERIALIZED VIEW", "view", None)
        """
        name = exposed_name or namer.mint(label)
        object_cls = ViewObject if "VIEW" in keyword else TableObject
        created: Object = object_cls(name=name, keyword=keyword, query=query)
        return created, (created,)


class CreateView(CreateFromQuery):
    """``CREATE VIEW <name> AS <query>``. Holds no rows, so a query reading it folds the view in
    and compiles as one statement. Cheap, and the most useful kind."""

    @classmethod
    def build(cls, namer: "ObjectNamer", query: QueryNode, *, exposed_name: Optional[str] = None) -> "CreateView":
        created, steps = cls._create_as_select(namer, query, "VIEW", "view", exposed_name)
        return cls(ObjectKind.VIEW, query, steps=steps, exposed=created)


class CreateTemporaryView(CreateFromQuery):
    """``CREATE TEMPORARY VIEW <name> AS <query>`` — session-scoped, otherwise a view."""

    @classmethod
    def build(cls, namer: "ObjectNamer", query: QueryNode, *, exposed_name: Optional[str] = None) -> "CreateTemporaryView":
        created, steps = cls._create_as_select(namer, query, "TEMPORARY VIEW", "view", exposed_name)
        return cls(ObjectKind.TEMPORARY_VIEW, query, steps=steps, exposed=created)


class CreateMaterializedView(CreateFromQuery):
    """``CREATE MATERIALIZED VIEW <name> AS <query>``. Built by
    :class:`~eqgen.equivalence.builders.creates.CreateMaterializedViewBuilder`; DuckDB has no
    materialized views, so that engine keeps the builder's weight at zero."""

    @classmethod
    def build(cls, namer: "ObjectNamer", query: QueryNode, *, exposed_name: Optional[str] = None) -> "CreateMaterializedView":
        created, steps = cls._create_as_select(namer, query, "MATERIALIZED VIEW", "view", exposed_name)
        return cls(ObjectKind.MATERIALIZED_VIEW, query, steps=steps, exposed=created)


class CreateTable(CreateFromQuery):
    """``CREATE TABLE <name> AS <query>``. The rows are really written, so a query reading it
    cannot fold anything in and reads a real table with its own statistics."""

    @classmethod
    def build(cls, namer: "ObjectNamer", query: QueryNode, *, exposed_name: Optional[str] = None) -> "CreateTable":
        created, steps = cls._create_as_select(namer, query, "TABLE", "table", exposed_name)
        return cls(ObjectKind.TABLE, query, steps=steps, exposed=created)


class CreateTemporaryTable(CreateFromQuery):
    """``CREATE TEMPORARY TABLE <name> AS <query>`` — session-scoped, otherwise a table."""

    @classmethod
    def build(cls, namer: "ObjectNamer", query: QueryNode, *, exposed_name: Optional[str] = None) -> "CreateTemporaryTable":
        created, steps = cls._create_as_select(namer, query, "TEMPORARY TABLE", "table", exposed_name)
        return cls(ObjectKind.TEMPORARY_TABLE, query, steps=steps, exposed=created)


class CreateUnloggedTable(CreateFromQuery):
    """``CREATE UNLOGGED TABLE <name> AS <query>`` — PostgreSQL WAL-skipping heap **(Mat)**."""

    @classmethod
    def build(cls, namer: "ObjectNamer", query: QueryNode, *, exposed_name: Optional[str] = None) -> "CreateUnloggedTable":
        created, steps = cls._create_as_select(namer, query, "UNLOGGED TABLE", "table", exposed_name)
        return cls(ObjectKind.UNLOGGED_TABLE, query, steps=steps, exposed=created)


class DeleteReinsertTable(CreateFromQuery):
    """A table created, then emptied of some rows and refilled from the base::

        CREATE TABLE t_table_1 AS SELECT * FROM t__base
        DELETE FROM t_table_1 WHERE MOD(c_int, 2) = 0
        INSERT INTO t_table_1 SELECT c_int, c_txt FROM t__base WHERE MOD(c_int, 2) = 0

    Same rows at the end, but the table has been written to, not just created.

    The ``DELETE`` and ``INSERT`` share one predicate object so they cannot come apart, and it
    stays an expression rather than text so the emitter picks the spelling.
    """

    def __init__(
        self,
        query: QueryNode,
        base_table: Table,
        delete_predicate: Optional[ExpressionNode] = None,
        *,
        steps: Sequence[SetupStep],
        exposed: Object,
    ) -> None:
        super().__init__(ObjectKind.TABLE, query, steps=steps, exposed=exposed)
        self._base_table = base_table
        self._delete_predicate = delete_predicate

    @property
    def base_table(self) -> Table:
        return self._base_table

    @property
    def delete_predicate(self) -> Optional[ExpressionNode]:
        return self._delete_predicate

    @classmethod
    def build(
        cls,
        namer: "ObjectNamer",
        query: QueryNode,
        base_table: Table,
        delete_predicate: Optional[ExpressionNode] = None,
        *,
        exposed_name: Optional[str] = None,
    ) -> "DeleteReinsertTable":
        name = exposed_name or namer.mint("table")
        created = TableObject(name=name, keyword="TABLE", query=query)
        columns = tuple(named.alias for named in query.get_signature())
        steps: tuple[SetupStep, ...] = (
            created,
            Delete(target=name, predicate=delete_predicate),
            Insert(
                target=name,
                select_columns=columns,
                source_ref=base_table.get_sql_name(use_schema_name=True),
                predicate=delete_predicate,
            ),
        )
        return cls(query, base_table, delete_predicate, steps=steps, exposed=created)


class NoopUpdateTable(CreateFromQuery):
    """A table created, then written with identity assignments::

        CREATE TABLE t_table_1 AS SELECT * FROM t__base
        UPDATE t_table_1 SET c_int = c_int, c_txt = c_txt WHERE MOD(c_int, 2) = 1

    Same rows at the end, but the table has been written to. Assignments are identity
    ``col = col``; the optional predicate comes from the same helper as delete/reinsert.
    """

    def __init__(
        self,
        query: QueryNode,
        assignments: tuple[tuple[str, ExpressionNode], ...],
        update_predicate: Optional[ExpressionNode] = None,
        *,
        steps: Sequence[SetupStep],
        exposed: Object,
    ) -> None:
        super().__init__(ObjectKind.TABLE, query, steps=steps, exposed=exposed)
        self._assignments = assignments
        self._update_predicate = update_predicate

    @property
    def assignments(self) -> tuple[tuple[str, ExpressionNode], ...]:
        return self._assignments

    @property
    def update_predicate(self) -> Optional[ExpressionNode]:
        return self._update_predicate

    @classmethod
    def build(
        cls,
        namer: "ObjectNamer",
        query: QueryNode,
        assignments: Sequence[tuple[str, ExpressionNode]],
        update_predicate: Optional[ExpressionNode] = None,
        *,
        exposed_name: Optional[str] = None,
    ) -> "NoopUpdateTable":
        name = exposed_name or namer.mint("table")
        created = TableObject(name=name, keyword="TABLE", query=query)
        assign_tuple = tuple(assignments)
        steps: tuple[SetupStep, ...] = (
            created,
            Update(target=name, assignments=assign_tuple, predicate=update_predicate),
        )
        return cls(query, assign_tuple, update_predicate, steps=steps, exposed=created)


class TagPruneDeleteTable(EquivalentRelation):
    """Delete every throwaway-tagged row from an expansion table, then expose the base columns::

        DELETE FROM t_table_2 WHERE eq_tag_1 <> 1
        CREATE VIEW t AS SELECT c_int, c_txt FROM t_table_2

    The table already holds the keep-tagged base rows plus filler; after the delete only the base
    multiset remains. A view rather than renaming the table, so the workload still reads a name
    that never held the filler rows.
    """

    def __init__(
        self,
        child: EquivalentRelation,
        base_items: Sequence[ProjectionItem],
        tag_col: str,
        keep_value: int,
        *,
        steps: Sequence[SetupStep],
        exposed: Object,
    ) -> None:
        super().__init__(ObjectKind.VIEW, [child], steps=steps, exposed=exposed)
        self._child = child
        self._base_items: tuple[ProjectionItem, ...] = tuple(base_items)
        self._tag_col = tag_col
        self._keep_value = keep_value

    @property
    def child(self) -> EquivalentRelation:
        return self._child

    @property
    def base_items(self) -> tuple[ProjectionItem, ...]:
        return self._base_items

    @property
    def tag_col(self) -> str:
        return self._tag_col

    @property
    def keep_value(self) -> int:
        return self._keep_value

    def get_signature(self) -> list[Named[SqlType]]:
        return [Named(alias=item.alias, target=item.data_type) for item in self._base_items]

    @classmethod
    def build(
        cls,
        namer: "ObjectNamer",
        child: EquivalentRelation,
        base_items: Sequence[ProjectionItem],
        tag_col: str,
        keep_value: int,
        *,
        exposed_name: Optional[str] = None,
    ) -> "TagPruneDeleteTable":
        from eqgen.core.types import IntegerType
        from eqgen.ir import expr

        delete = Delete(
            target=child.materialized_name,
            predicate=expr.ne(expr.col(tag_col, IntegerType()), expr.int_lit(keep_value)),
        )
        name = exposed_name or namer.mint("view")
        view = ViewObject(name=name, keyword="VIEW", query=SelectQuery(child, base_items))
        return cls(child, base_items, tag_col, keep_value, steps=(delete, view), exposed=view)


class InsertExtrasExpansion(EquivalentRelation):
    """``INSERT`` more value-identical copies into an expansion table that already exists::

        INSERT INTO t_table_2 SELECT c_int, c_txt, 0 FROM t_table_2 WHERE eq_tag_1 = 1

    Creates no object of its own — the insert targets the child's table, and this node exposes
    that same name. Stacks under a reducer the same way an explode does.
    """

    def __init__(self, child: EquivalentRelation, *, steps: Sequence[SetupStep], exposed: Object) -> None:
        super().__init__(ObjectKind.TABLE, [child], steps=steps, exposed=exposed)
        self._child = child

    @property
    def child(self) -> EquivalentRelation:
        return self._child

    def get_signature(self) -> list[Named[SqlType]]:
        return self._child.get_signature()

    @classmethod
    def build(cls, child: EquivalentRelation, insert: Insert) -> "InsertExtrasExpansion":
        return cls(child, steps=(insert,), exposed=child.exposed)


# ---------------------------------------------------------------------------
# Describing a built tree — for logs and reports, not for emission
# ---------------------------------------------------------------------------


def _label(node: EqNode) -> str:
    """A node's class, plus its object name if it has one::

    CreateView t_view_3        -- searchable in the statement list
    SelectQuery                -- no name to give
    """
    if isinstance(node, EquivalentRelation):
        return f"{type(node).__name__} {node.materialized_name}"
    if isinstance(node, BaseTableSource):
        return f"{type(node).__name__} {node.table.get_sql_name()}"
    return type(node).__name__


def describe_shape(node: EqNode) -> str:
    """One searchable line summarising a whole tree::

        CreateView, depth 5, 11 nodes [CreateTable 3, SelectQuery 3, BaseTableSource 3, ...]

    Counts rather than nesting, because trees reach depth ten and eighty nodes, and written out
    in full that is a few thousand characters nobody reads.
    """
    counts: Counter[str] = Counter()
    depth = 0
    stack = [(node, 1)]
    while stack:
        current, level = stack.pop()
        counts[type(current).__name__] += 1
        depth = max(depth, level)
        stack.extend((child, level + 1) for child in current.children())
    counted = ", ".join(f"{name} {count}" for name, count in counts.most_common())
    return f"{type(node).__name__}, depth {depth}, {sum(counts.values())} nodes [{counted}]"


def render_tree(node: EqNode, *, indent: str = "") -> list[str]:
    """The tree, one node per line, root first::

        CreateTable t
          UnionAllQuery
            CreateView t_view_1
              SelectQuery
                BaseTableSource t__base

    Working this out by hand from forty ``CREATE`` statements is the tedious part of reading a
    round.
    """
    lines = [f"{indent}{_label(node)}"]
    for child in node.children():
        lines.extend(render_tree(child, indent=indent + "  "))
    return lines
