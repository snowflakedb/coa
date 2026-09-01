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

"""Nodes only SQLite can write, and the visitor methods that render them.

    SqliteCreateIndex              CREATE [UNIQUE] INDEX + exposing view
    SqliteCreatePartialIndex       CREATE INDEX … WHERE … + exposing view
    SqliteCreateAttach             ATTACH ':memory:' + cross-schema mirror
    SqliteCreateWithoutRowid       WITHOUT ROWID table + INSERT + view
    SqliteCreateGenerated          VIRTUAL/STORED generated column round-trip
    SqliteCreateStrict             STRICT table + INSERT + view
    SqliteCreateExprIndex          expression INDEX + exposing view
    SqliteCreateWithoutRowidIndex  WITHOUT ROWID + secondary index
    SqliteRecursiveCteQuery        WITH RECURSIVE … UNION ALL empty

``accept`` checks the visitor understands SQLite and raises otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Sequence, TypeVar, cast

from eqgen.core.catalog import Named
from eqgen.core.types import SqlType
from eqgen.equivalence.ast import DialectNativeQuery, EquivalentRelation, ProjectionItem, QueryNode
from eqgen.equivalence.capabilities import ObjectKind
from eqgen.equivalence.objects import Object
from eqgen.equivalence.visitor import QueryVisitor, SetupVisitor

if TYPE_CHECKING:
    from eqgen.equivalence.context import ObjectNamer

T = TypeVar("T")
_T = TypeVar("_T")


def _unsupported(visitor: object, node: object) -> None:
    raise TypeError(
        f"{type(node).__name__} is a SQLite-only node and {type(visitor).__name__} cannot render it. "
        "A dialect node must be rendered by that dialect's emitter."
    )


class SqliteQueryVisitor(QueryVisitor[T]):
    def visit_sqlite_recursive_cte_query(self, query: "SqliteRecursiveCteQuery") -> T:
        raise NotImplementedError

    def visit_sqlite_nested_materialized_cte_query(self, query: "SqliteNestedMaterializedCteQuery") -> T:
        raise NotImplementedError


class SqliteSetupVisitor(SetupVisitor[T]):
    def visit_sqlite_index_object(self, node: "SqliteIndexObject") -> T:
        raise NotImplementedError

    def visit_sqlite_attach_object(self, node: "SqliteAttachObject") -> T:
        raise NotImplementedError

    def visit_sqlite_without_rowid_object(self, node: "SqliteWithoutRowidObject") -> T:
        raise NotImplementedError

    def visit_sqlite_generated_object(self, node: "SqliteGeneratedObject") -> T:
        raise NotImplementedError

    def visit_sqlite_strict_object(self, node: "SqliteStrictObject") -> T:
        raise NotImplementedError

    def visit_sqlite_expr_index_object(self, node: "SqliteExprIndexObject") -> T:
        raise NotImplementedError

    def visit_sqlite_without_rowid_index_object(self, node: "SqliteWithoutRowidIndexObject") -> T:
        raise NotImplementedError

    def visit_sqlite_analyze_index_object(self, node: "SqliteAnalyzeIndexObject") -> T:
        raise NotImplementedError


@dataclass(frozen=True)
class SqliteIndexObject(Object):
    body_ref: str = ""
    index_name: str = ""
    out_cols: tuple[str, ...] = ()
    target: str = ""
    unique: bool = False
    where_sql: str = ""  # empty → full index; else partial ``WHERE …``

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, SqliteSetupVisitor):
            return cast(T, visitor.visit_sqlite_index_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


class SqliteCreateIndex(EquivalentRelation):
    """``CREATE [UNIQUE] [PARTIAL] INDEX`` + exposing view — algebra **(Mat)**."""

    def __init__(self, body: EquivalentRelation, exposed: Object) -> None:
        super().__init__(ObjectKind.VIEW, [body], steps=(exposed,), exposed=exposed)
        self._body = body

    @property
    def body(self) -> EquivalentRelation:
        return self._body

    def get_signature(self) -> list[Named[SqlType]]:
        return self._body.get_signature()

    @classmethod
    def build(
        cls,
        namer: "ObjectNamer",
        body: EquivalentRelation,
        *,
        target: str,
        out_cols: Sequence[str],
        exposed_name: Optional[str] = None,
        unique: bool = False,
        where_sql: str = "",
    ) -> "SqliteCreateIndex":
        name = exposed_name or namer.mint("view")
        return cls(
            body,
            SqliteIndexObject(
                name=name,
                body_ref=body.materialized_name,
                index_name=f"{name}_idx",
                out_cols=tuple(out_cols),
                target=target,
                unique=unique,
                where_sql=where_sql,
            ),
        )


@dataclass(frozen=True)
class SqliteAttachObject(Object):
    body_ref: str = ""
    alias: str = ""
    mirror: str = ""
    out_cols: tuple[str, ...] = ()

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, SqliteSetupVisitor):
            return cast(T, visitor.visit_sqlite_attach_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


class SqliteCreateAttach(EquivalentRelation):
    """``ATTACH ':memory:'`` mirror copied back into ``main`` — algebra **(Mat)**.

    SQLite forbids views that reference other schemas (``view … cannot reference objects in
    database …``), so the exposing object is a ``CREATE TABLE`` in ``main`` filled from the
    attached mirror — not a view over it.
    """

    def __init__(self, body: EquivalentRelation, exposed: Object) -> None:
        super().__init__(ObjectKind.TABLE, [body], steps=(exposed,), exposed=exposed)
        self._body = body

    @property
    def body(self) -> EquivalentRelation:
        return self._body

    def get_signature(self) -> list[Named[SqlType]]:
        return self._body.get_signature()

    @classmethod
    def build(
        cls, namer: "ObjectNamer", body: EquivalentRelation, *, exposed_name: Optional[str] = None
    ) -> "SqliteCreateAttach":
        name = exposed_name or namer.mint("view")
        return cls(
            body,
            SqliteAttachObject(
                name=name,
                body_ref=body.materialized_name,
                alias=f"{name}_att",
                mirror="mirror",
                out_cols=tuple(named.alias for named in body.get_signature()),
            ),
        )


@dataclass(frozen=True)
class SqliteWithoutRowidObject(Object):
    body_ref: str = ""
    table_name: str = ""
    out_cols: tuple[str, ...] = ()
    col_defs: tuple[str, ...] = ()  # ``name TYPE [NOT NULL]`` pieces
    pk_col: str = "c_pk"

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, SqliteSetupVisitor):
            return cast(T, visitor.visit_sqlite_without_rowid_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


class SqliteCreateWithoutRowid(EquivalentRelation):
    """``WITHOUT ROWID`` table filled from body — algebra **(Mat)**."""

    def __init__(self, body: EquivalentRelation, exposed: Object) -> None:
        super().__init__(ObjectKind.VIEW, [body], steps=(exposed,), exposed=exposed)
        self._body = body

    @property
    def body(self) -> EquivalentRelation:
        return self._body

    def get_signature(self) -> list[Named[SqlType]]:
        return self._body.get_signature()

    @classmethod
    def build(
        cls,
        namer: "ObjectNamer",
        body: EquivalentRelation,
        *,
        col_defs: Sequence[str],
        out_cols: Sequence[str],
        pk_col: str = "c_pk",
        exposed_name: Optional[str] = None,
    ) -> "SqliteCreateWithoutRowid":
        name = exposed_name or namer.mint("view")
        return cls(
            body,
            SqliteWithoutRowidObject(
                name=name,
                body_ref=body.materialized_name,
                table_name=f"{name}_wor",
                out_cols=tuple(out_cols),
                col_defs=tuple(col_defs),
                pk_col=pk_col,
            ),
        )


@dataclass(frozen=True)
class SqliteGeneratedObject(Object):
    body_ref: str = ""
    table_name: str = ""
    out_cols: tuple[str, ...] = ()
    col_defs: tuple[str, ...] = ()
    gen_col: str = "eq_gen"
    gen_of: str = "c_pk"
    stored: bool = False

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, SqliteSetupVisitor):
            return cast(T, visitor.visit_sqlite_generated_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


class SqliteCreateGenerated(EquivalentRelation):
    """VIRTUAL or STORED generated-column round-trip — algebra **(Mat)**."""

    def __init__(self, body: EquivalentRelation, exposed: Object) -> None:
        super().__init__(ObjectKind.VIEW, [body], steps=(exposed,), exposed=exposed)
        self._body = body

    @property
    def body(self) -> EquivalentRelation:
        return self._body

    def get_signature(self) -> list[Named[SqlType]]:
        return self._body.get_signature()

    @classmethod
    def build(
        cls,
        namer: "ObjectNamer",
        body: EquivalentRelation,
        *,
        col_defs: Sequence[str],
        out_cols: Sequence[str],
        gen_of: str = "c_pk",
        stored: bool = False,
        exposed_name: Optional[str] = None,
    ) -> "SqliteCreateGenerated":
        name = exposed_name or namer.mint("view")
        return cls(
            body,
            SqliteGeneratedObject(
                name=name,
                body_ref=body.materialized_name,
                table_name=f"{name}_gen",
                out_cols=tuple(out_cols),
                col_defs=tuple(col_defs),
                gen_col="eq_gen",
                gen_of=gen_of,
                stored=stored,
            ),
        )


@dataclass(frozen=True)
class SqliteStrictObject(Object):
    body_ref: str = ""
    table_name: str = ""
    out_cols: tuple[str, ...] = ()
    col_defs: tuple[str, ...] = ()

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, SqliteSetupVisitor):
            return cast(T, visitor.visit_sqlite_strict_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


class SqliteCreateStrict(EquivalentRelation):
    """``CREATE TABLE … STRICT`` filled from body — algebra **(Mat)**."""

    def __init__(self, body: EquivalentRelation, exposed: Object) -> None:
        super().__init__(ObjectKind.VIEW, [body], steps=(exposed,), exposed=exposed)
        self._body = body

    @property
    def body(self) -> EquivalentRelation:
        return self._body

    def get_signature(self) -> list[Named[SqlType]]:
        return self._body.get_signature()

    @classmethod
    def build(
        cls,
        namer: "ObjectNamer",
        body: EquivalentRelation,
        *,
        col_defs: Sequence[str],
        out_cols: Sequence[str],
        exposed_name: Optional[str] = None,
    ) -> "SqliteCreateStrict":
        name = exposed_name or namer.mint("view")
        return cls(
            body,
            SqliteStrictObject(
                name=name,
                body_ref=body.materialized_name,
                table_name=f"{name}_strict",
                out_cols=tuple(out_cols),
                col_defs=tuple(col_defs),
            ),
        )


@dataclass(frozen=True)
class SqliteExprIndexObject(Object):
    """CTAS body + ``CREATE INDEX ON t((expr))`` + exposing view."""

    body_ref: str = ""
    table_name: str = ""
    index_name: str = ""
    out_cols: tuple[str, ...] = ()
    expr_sql: str = ""

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, SqliteSetupVisitor):
            return cast(T, visitor.visit_sqlite_expr_index_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


class SqliteCreateExprIndex(EquivalentRelation):
    """Expression index Mat — algebra **(Mat)**."""

    def __init__(self, body: EquivalentRelation, exposed: Object) -> None:
        super().__init__(ObjectKind.VIEW, [body], steps=(exposed,), exposed=exposed)
        self._body = body

    @property
    def body(self) -> EquivalentRelation:
        return self._body

    def get_signature(self) -> list[Named[SqlType]]:
        return self._body.get_signature()

    @classmethod
    def build(
        cls,
        namer: "ObjectNamer",
        body: EquivalentRelation,
        *,
        out_cols: Sequence[str],
        expr_sql: str,
        exposed_name: Optional[str] = None,
    ) -> "SqliteCreateExprIndex":
        name = exposed_name or namer.mint("view")
        return cls(
            body,
            SqliteExprIndexObject(
                name=name,
                body_ref=body.materialized_name,
                table_name=f"{name}_eix",
                index_name=f"{name}_eix_idx",
                out_cols=tuple(out_cols),
                expr_sql=expr_sql,
            ),
        )


@dataclass(frozen=True)
class SqliteWithoutRowidIndexObject(Object):
    """WITHOUT ROWID table + secondary btree index + exposing view."""

    body_ref: str = ""
    table_name: str = ""
    index_name: str = ""
    out_cols: tuple[str, ...] = ()
    col_defs: tuple[str, ...] = ()
    pk_col: str = "c_pk"
    index_col: str = "c_int"

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, SqliteSetupVisitor):
            return cast(T, visitor.visit_sqlite_without_rowid_index_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


class SqliteCreateWithoutRowidIndex(EquivalentRelation):
    """WITHOUT ROWID + secondary index — algebra **(Mat)**."""

    def __init__(self, body: EquivalentRelation, exposed: Object) -> None:
        super().__init__(ObjectKind.VIEW, [body], steps=(exposed,), exposed=exposed)
        self._body = body

    @property
    def body(self) -> EquivalentRelation:
        return self._body

    def get_signature(self) -> list[Named[SqlType]]:
        return self._body.get_signature()

    @classmethod
    def build(
        cls,
        namer: "ObjectNamer",
        body: EquivalentRelation,
        *,
        col_defs: Sequence[str],
        out_cols: Sequence[str],
        pk_col: str,
        index_col: str,
        exposed_name: Optional[str] = None,
    ) -> "SqliteCreateWithoutRowidIndex":
        name = exposed_name or namer.mint("view")
        return cls(
            body,
            SqliteWithoutRowidIndexObject(
                name=name,
                body_ref=body.materialized_name,
                table_name=f"{name}_wri",
                index_name=f"{name}_wri_idx",
                out_cols=tuple(out_cols),
                col_defs=tuple(col_defs),
                pk_col=pk_col,
                index_col=index_col,
            ),
        )


class SqliteRecursiveCteQuery(DialectNativeQuery):
    """``WITH RECURSIVE r AS (SELECT * FROM src UNION ALL SELECT * FROM r WHERE FALSE)``."""

    def __init__(self, source: EquivalentRelation, cte_name: str, base_items: Sequence[ProjectionItem]) -> None:
        super().__init__([source])
        self._source = source
        self._cte_name = cte_name
        self._base_items: tuple[ProjectionItem, ...] = tuple(base_items)

    @property
    def source(self) -> EquivalentRelation:
        return self._source

    @property
    def cte_name(self) -> str:
        return self._cte_name

    @property
    def out_cols(self) -> tuple[str, ...]:
        return tuple(item.alias for item in self._base_items)

    def get_signature(self) -> list[Named[SqlType]]:
        return [Named(alias=item.alias, target=item.data_type) for item in self._base_items]

    def accept(self, visitor: QueryVisitor[_T]) -> _T:
        if isinstance(visitor, SqliteQueryVisitor):
            return cast(_T, visitor.visit_sqlite_recursive_cte_query(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


class SqliteNestedMaterializedCteQuery(DialectNativeQuery):
    """Two stacked ``AS MATERIALIZED`` CTEs — forces snapshot-of-snapshot plans."""

    def __init__(
        self,
        source: EquivalentRelation,
        inner_name: str,
        outer_name: str,
        base_items: Sequence[ProjectionItem],
    ) -> None:
        super().__init__([source])
        self._source = source
        self._inner_name = inner_name
        self._outer_name = outer_name
        self._base_items: tuple[ProjectionItem, ...] = tuple(base_items)

    @property
    def source(self) -> EquivalentRelation:
        return self._source

    @property
    def inner_name(self) -> str:
        return self._inner_name

    @property
    def outer_name(self) -> str:
        return self._outer_name

    @property
    def out_cols(self) -> tuple[str, ...]:
        return tuple(item.alias for item in self._base_items)

    def get_signature(self) -> list[Named[SqlType]]:
        return [Named(alias=item.alias, target=item.data_type) for item in self._base_items]

    def accept(self, visitor: QueryVisitor[_T]) -> _T:
        if isinstance(visitor, SqliteQueryVisitor):
            return cast(_T, visitor.visit_sqlite_nested_materialized_cte_query(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


@dataclass(frozen=True)
class SqliteAnalyzeIndexObject(Object):
    """CTAS + index + ``ANALYZE`` + exposing view — planner-stats Mat."""

    body_ref: str = ""
    table_name: str = ""
    index_name: str = ""
    out_cols: tuple[str, ...] = ()
    target: str = ""
    where_sql: str = ""

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, SqliteSetupVisitor):
            return cast(T, visitor.visit_sqlite_analyze_index_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


class SqliteCreateAnalyzeIndex(EquivalentRelation):
    """Indexed table with ``ANALYZE`` so the planner sees fresh stats. Algebra **(Mat)**."""

    def __init__(self, body: EquivalentRelation, exposed: Object) -> None:
        super().__init__(ObjectKind.VIEW, [body], steps=(exposed,), exposed=exposed)
        self._body = body

    @property
    def body(self) -> EquivalentRelation:
        return self._body

    def get_signature(self) -> list[Named[SqlType]]:
        return self._body.get_signature()

    @classmethod
    def build(
        cls,
        namer: "ObjectNamer",
        body: EquivalentRelation,
        *,
        target: str,
        out_cols: Sequence[str],
        where_sql: str = "",
        exposed_name: Optional[str] = None,
    ) -> "SqliteCreateAnalyzeIndex":
        name = exposed_name or namer.mint("view")
        return cls(
            body,
            SqliteAnalyzeIndexObject(
                name=name,
                body_ref=body.materialized_name,
                table_name=body.materialized_name,
                index_name=f"{name}_an_idx",
                out_cols=tuple(out_cols),
                target=target,
                where_sql=where_sql,
            ),
        )


# Silence unused QueryNode import warning for type docs.
_ = QueryNode
