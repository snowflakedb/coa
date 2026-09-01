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

"""Turning a built object into the SQL statements that create it.

Three classes, each calling into the next::

    emit_equivalence(root)      walks the tree, decides statement order
      SqlEmitter               a step        ->  ["CREATE VIEW v AS SELECT * FROM t"]
        QueryRenderer          a query node  ->  "SELECT * FROM t"
          Spelling             an expression ->  "c_int > 3"       (in eqgen/ir/render.py)

An engine subclasses whichever it needs and passes it into the one above:
``SqlEmitter(query_renderer=DuckDBQueryRenderer())``, ``QueryRenderer(spelling=...)``. So
changing how expressions are written changes every statement, without touching the other two.

Nothing here mints a name or changes a node — generation already did that — so running it
twice gives the same text.

Two things it deliberately does not do:

* No SQL is built during generation. Every character of every statement is produced here, so
  an engine can change all of it. An earlier version wrote a delete predicate as text inside
  ``build()``, where no emitter could reach it.
* Expressions are never inspected. Whether a projection needs ``AS`` is decided by comparing
  rendered text against the alias, so plugin-supplied text behaves like anything else.
"""

from __future__ import annotations

from typing import Optional

from eqgen.core.statement import Statement
from eqgen.equivalence import actions, objects
from eqgen.equivalence.ast import (
    CteQuery,
    EqNode,
    EquivalentRelation,
    ExceptAllQuery,
    IntersectAllQuery,
    JoinQuery,
    LateralReprojectQuery,
    ProjectionItem,
    QueryNode,
    SelectQuery,
    UnionAllQuery,
)
from eqgen.equivalence.visitor import QueryVisitor, SetupVisitor
from eqgen.ir.render import DEFAULT_SPELLING, Spelling

# There is no translation step anywhere in this project, and no "already converted" flag on a
# statement. An earlier version emitted Snowflake SQL and ran it through a translator, so every
# statement had to record whether it had been converted yet — and converting twice shifted an
# array index by one, silently. Each engine emits its own SQL, so there is nothing to track.


def _projection_sql(item: ProjectionItem, spelling: Spelling) -> str:
    """One projection item, with ``AS`` only when it changes the name::

        c_int                              -- item is just the column
        MOD(c_int, 2) AS c_int             -- item computes something

    Decided by comparing rendered text against the alias, so nothing has to look inside the
    expression.
    """
    rendered = spelling.expr(item.expr)
    return rendered if rendered == item.alias else f"{rendered} AS {item.alias}"


class QueryRenderer(QueryVisitor[str]):
    """A query node into the ``SELECT`` that goes after ``CREATE ... AS``::

        SELECT * FROM t_view_1
        SELECT * FROM t_view_1 UNION ALL SELECT * FROM t_view_2

    Sources are already created, so each is named rather than inlined. Subclass this to add an
    engine's own query shapes, or to change the ``spelling`` used for every expression inside
    them.
    """

    def __init__(self, spelling: Optional[Spelling] = None) -> None:
        self._spelling: Spelling = spelling or DEFAULT_SPELLING

    @property
    def spelling(self) -> Spelling:
        return self._spelling

    def visit_select_query(self, query: SelectQuery) -> str:
        where = f" WHERE {self._spelling.expr(query.predicate)}" if query.predicate is not None else ""
        if query.projection is None:
            select_list = "*"
        else:
            select_list = ", ".join(_projection_sql(item, self._spelling) for item in query.projection)
        distinct = "DISTINCT " if query.distinct else ""
        group = (
            " GROUP BY " + ", ".join(self._spelling.expr(key) for key in query.group_by)
            if query.group_by is not None
            else ""
        )
        qualify = f" QUALIFY {self._spelling.expr(query.qualify)}" if query.qualify is not None else ""
        body = f"SELECT {distinct}{select_list} FROM {query.source.ref_sql()}{where}{group}{qualify}"
        if query.order_by is None:
            return body
        # Nested ORDER BY forces a sort; the outer SELECT restores a multiset-neutral result.
        order = ", ".join(self._spelling.expr(key) for key in query.order_by)
        return f"SELECT {select_list if query.projection is not None else '*'} FROM ({body} ORDER BY {order}) AS eq_ord"

    def visit_union_all_query(self, query: UnionAllQuery) -> str:
        return " UNION ALL ".join(f"SELECT * FROM {branch.ref_sql()}" for branch in query.branches)

    def visit_except_all_query(self, query: ExceptAllQuery) -> str:
        return (
            f"SELECT * FROM {query.left.ref_sql()} "
            f"EXCEPT ALL "
            f"SELECT * FROM {query.right.ref_sql()}"
        )

    def visit_intersect_all_query(self, query: IntersectAllQuery) -> str:
        return (
            f"SELECT * FROM {query.left.ref_sql()} "
            f"INTERSECT ALL "
            f"SELECT * FROM {query.right.ref_sql()}"
        )

    def visit_cte_query(self, query: CteQuery) -> str:
        where = f" WHERE {self._spelling.expr(query.predicate)}" if query.predicate is not None else ""
        body = f"SELECT * FROM {query.source.ref_sql()}{where}"
        if query.materialize is True:
            hint = " MATERIALIZED"
        elif query.materialize is False:
            hint = " NOT MATERIALIZED"
        else:
            hint = ""
        return f"WITH {query.cte_name} AS{hint} ({body}) SELECT * FROM {query.cte_name}"

    def visit_lateral_reproject_query(self, query: LateralReprojectQuery) -> str:
        outer, inner = query.lateral_alias, query.source_alias
        names = [named.alias for named in query.source.get_signature()]
        inner_list = ", ".join(f"{inner}.{name} AS {name}" for name in names)
        outer_list = ", ".join(f"{outer}.{name} AS {name}" for name in names)
        return f"SELECT {outer_list} FROM {query.source.ref_sql()} {inner}, LATERAL (SELECT {inner_list}) AS {outer}"

    def visit_join_query(self, query: JoinQuery) -> str:
        select_list = ", ".join(_projection_sql(item, self._spelling) for item in query.projection)
        where = f" WHERE {self._spelling.expr(query.predicate)}" if query.predicate is not None else ""
        # CROSS JOIN has no ON clause (PostgreSQL / DuckDB / MySQL reject ``CROSS JOIN … ON``).
        if query.join_type.upper() == "CROSS":
            on = ""
        else:
            if query.condition is None:
                raise ValueError("a join must have an ON condition")
            on = f" ON {self._spelling.expr(query.condition)}"
        return (
            f"SELECT {select_list} FROM {query.left.ref_sql()} {query.left_alias} "
            f"{query.join_type} JOIN {query.right.ref_sql()} {query.right_alias}{on}{where}"
        )


class SqlEmitter(SetupVisitor[list[Statement]]):
    """One setup step into the statements that carry it out::

        TableObject  ->  ["CREATE TABLE t_table_1 AS SELECT * FROM t"]
        Delete       ->  ["DELETE FROM t_table_1 WHERE MOD(c_int, 2) = 0"]

    ``query_renderer`` handles the part after ``AS``. Pass a subclass for an engine whose query
    shapes differ; the default raises on them rather than guessing.
    """

    def __init__(self, query_renderer: Optional[QueryVisitor[str]] = None) -> None:
        self._query_renderer: QueryVisitor[str] = query_renderer or QueryRenderer()

    @property
    def spelling(self) -> Spelling:
        """The spelling to use for expressions that appear outside a query — a ``DELETE``'s
        ``WHERE``, for instance."""
        renderer = self._query_renderer
        return renderer.spelling if isinstance(renderer, QueryRenderer) else DEFAULT_SPELLING

    def _render_query(self, query: QueryNode) -> str:
        return self._query_renderer.visit(query)

    # -- objects ----------------------------------------------------------

    def _create_as_select(self, node: objects._CreateAsSelectObject) -> Statement:
        body = self._render_query(node.query) if node.query is not None else node.definition_sql
        return Statement(f"CREATE {node.keyword} {node.name} AS {body}")

    def visit_table_object(self, node: objects.TableObject) -> list[Statement]:
        return [self._create_as_select(node)]

    def visit_view_object(self, node: objects.ViewObject) -> list[Statement]:
        return [self._create_as_select(node)]

    # -- actions ----------------------------------------------------------

    def visit_delete(self, node: actions.Delete) -> list[Statement]:
        where = f" WHERE {self.spelling.expr(node.predicate)}" if node.predicate is not None else ""
        return [Statement(f"DELETE FROM {node.target}{where}")]

    def visit_insert(self, node: actions.Insert) -> list[Statement]:
        if node.query is not None:
            return [Statement(f"INSERT INTO {node.target} {self._render_query(node.query)}")]
        columns = ", ".join(node.select_columns)
        target = node.target if node.column_list is None else f"{node.target} ({', '.join(node.column_list)})"
        where = f" WHERE {self.spelling.expr(node.predicate)}" if node.predicate is not None else ""
        return [Statement(f"INSERT INTO {target} SELECT {columns} FROM {node.source_ref}{where}")]

    def visit_update(self, node: actions.Update) -> list[Statement]:
        sets = ", ".join(f"{column} = {self.spelling.expr(value)}" for column, value in node.assignments)
        where = f" WHERE {self.spelling.expr(node.predicate)}" if node.predicate is not None else ""
        return [Statement(f"UPDATE {node.target} SET {sets}{where}")]


def emit_equivalence(root: EquivalentRelation, emitter: Optional[SqlEmitter] = None) -> list[Statement]:
    """Every statement needed to build *root*, in an order that works.

    Children are emitted before the parents that read them, and each node only once — one
    child can feed several parents, as a table read by every branch of a union does::

        CREATE TABLE t_table_1 AS ...            -- shared child, emitted once
        CREATE VIEW  t_view_1  AS SELECT * FROM t_table_1
        CREATE VIEW  t_view_2  AS SELECT * FROM t_table_1

    Emitting it twice would fail on the second ``CREATE``, or shadow the first.
    """
    active = emitter or SqlEmitter()
    statements: list[Statement] = []
    seen: set[int] = set()

    def walk(node: EqNode) -> None:
        if id(node) in seen:
            return
        seen.add(id(node))
        for child in node.children():
            walk(child)
        if isinstance(node, EquivalentRelation):
            for step in node.steps:
                statements.extend(active.visit(step))

    walk(root)
    return statements
