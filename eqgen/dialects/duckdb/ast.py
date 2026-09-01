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

"""Nodes only DuckDB can write, and the visitor methods that render them.

    DuckDBPositionalJoinQuery   query        POSITIONAL JOIN of keyed column halves
    DuckDBRecursiveCteQuery     query        WITH RECURSIVE … UNION ALL empty
    DuckDBStarReplaceQuery      query        SELECT * REPLACE (c AS c)
    DuckDBPivotStructQuery      query        PIVOT through a one-key STRUCT pack
    DuckDBCreateMacro           statement    CREATE MACRO … AS TABLE …
    DuckDBCreateIndex           statement    CREATE INDEX + exposing view
    DuckDBCreateAttach          statement    ATTACH ':memory:' + cross-catalog mirror
    DuckDBCreateCatalog         statement    catalog-object round trips (enum, schema, …)

``accept`` checks the visitor understands DuckDB and raises otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, ClassVar, Optional, Sequence, TypeVar, cast

from eqgen.core.catalog import Named
from eqgen.core.types import SqlType
from eqgen.equivalence.ast import (
    DialectNativeQuery,
    EquivalentRelation,
    ProjectionItem,
    QueryNode,
)
from eqgen.equivalence.capabilities import ObjectKind
from eqgen.equivalence.objects import Object, SetupStep
from eqgen.equivalence.visitor import QueryVisitor, SetupVisitor

if TYPE_CHECKING:
    from eqgen.equivalence.context import ObjectNamer

T = TypeVar("T")
_T = TypeVar("_T")


def _unsupported(visitor: object, node: object) -> None:
    raise TypeError(
        f"{type(node).__name__} is a DuckDB-only node and {type(visitor).__name__} cannot render it. "
        "A dialect node must be rendered by that dialect's emitter."
    )


class DuckDBQueryVisitor(QueryVisitor[T]):
    def visit_duckdb_positional_join_query(self, query: "DuckDBPositionalJoinQuery") -> T:
        raise NotImplementedError

    def visit_duckdb_recursive_cte_query(self, query: "DuckDBRecursiveCteQuery") -> T:
        raise NotImplementedError

    def visit_duckdb_star_replace_query(self, query: "DuckDBStarReplaceQuery") -> T:
        raise NotImplementedError

    def visit_duckdb_pivot_struct_query(self, query: "DuckDBPivotStructQuery") -> T:
        raise NotImplementedError


class DuckDBSetupVisitor(SetupVisitor[T]):
    def visit_duckdb_macro_object(self, node: "DuckDBMacroObject") -> T:
        raise NotImplementedError

    def visit_duckdb_index_object(self, node: "DuckDBIndexObject") -> T:
        raise NotImplementedError

    def visit_duckdb_attach_object(self, node: "DuckDBAttachObject") -> T:
        raise NotImplementedError

    def visit_duckdb_catalog_object(self, node: "DuckDBCatalogObject") -> T:
        raise NotImplementedError


class DuckDBPositionalJoinQuery(DialectNativeQuery):
    """Keyed column split recombined with ``POSITIONAL JOIN`` (algebra **(Positional)**)."""

    def __init__(
        self,
        source: EquivalentRelation,
        key_col: str,
        left_cols: Sequence[str],
        right_cols: Sequence[str],
        base_items: Sequence[ProjectionItem],
    ) -> None:
        super().__init__([source])
        self._source = source
        self._key_col = key_col
        self._left_cols = tuple(left_cols)
        self._right_cols = tuple(right_cols)
        self._base_items: tuple[ProjectionItem, ...] = tuple(base_items)

    @property
    def source(self) -> EquivalentRelation:
        return self._source

    @property
    def key_col(self) -> str:
        return self._key_col

    @property
    def left_cols(self) -> tuple[str, ...]:
        return self._left_cols

    @property
    def right_cols(self) -> tuple[str, ...]:
        return self._right_cols

    @property
    def out_cols(self) -> tuple[str, ...]:
        return tuple(item.alias for item in self._base_items)

    def get_signature(self) -> list[Named[SqlType]]:
        return [Named(alias=item.alias, target=item.data_type) for item in self._base_items]

    def accept(self, visitor: QueryVisitor[_T]) -> _T:
        if isinstance(visitor, DuckDBQueryVisitor):
            return cast(_T, visitor.visit_duckdb_positional_join_query(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


class DuckDBRecursiveCteQuery(DialectNativeQuery):
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
        if isinstance(visitor, DuckDBQueryVisitor):
            return cast(_T, visitor.visit_duckdb_recursive_cte_query(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


class DuckDBStarReplaceQuery(DialectNativeQuery):
    """``SELECT * REPLACE (col AS col) FROM src`` — identity through the REPLACE syntax path."""

    def __init__(self, source: EquivalentRelation, replace_col: str, base_items: Sequence[ProjectionItem]) -> None:
        super().__init__([source])
        self._source = source
        self._replace_col = replace_col
        self._base_items: tuple[ProjectionItem, ...] = tuple(base_items)

    @property
    def source(self) -> EquivalentRelation:
        return self._source

    @property
    def replace_col(self) -> str:
        return self._replace_col

    def get_signature(self) -> list[Named[SqlType]]:
        return [Named(alias=item.alias, target=item.data_type) for item in self._base_items]

    def accept(self, visitor: QueryVisitor[_T]) -> _T:
        if isinstance(visitor, DuckDBQueryVisitor):
            return cast(_T, visitor.visit_duckdb_star_replace_query(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


class DuckDBPivotStructQuery(DialectNativeQuery):
    """Pack non-key columns into a STRUCT, ``PIVOT`` on a constant key, unpack — identity."""

    def __init__(
        self,
        source: EquivalentRelation,
        key_col: str,
        measure_cols: Sequence[str],
        base_items: Sequence[ProjectionItem],
    ) -> None:
        super().__init__([source])
        self._source = source
        self._key_col = key_col
        self._measure_cols = tuple(measure_cols)
        self._base_items: tuple[ProjectionItem, ...] = tuple(base_items)

    @property
    def source(self) -> EquivalentRelation:
        return self._source

    @property
    def key_col(self) -> str:
        return self._key_col

    @property
    def measure_cols(self) -> tuple[str, ...]:
        return self._measure_cols

    def get_signature(self) -> list[Named[SqlType]]:
        return [Named(alias=item.alias, target=item.data_type) for item in self._base_items]

    def accept(self, visitor: QueryVisitor[_T]) -> _T:
        if isinstance(visitor, DuckDBQueryVisitor):
            return cast(_T, visitor.visit_duckdb_pivot_struct_query(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


@dataclass(frozen=True)
class DuckDBMacroObject(Object):
    query: Optional["QueryNode"] = None
    macro_name: str = ""

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, DuckDBSetupVisitor):
            return cast(T, visitor.visit_duckdb_macro_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


class DuckDBCreateMacro(EquivalentRelation):
    def __init__(self, query: QueryNode, exposed: Object) -> None:
        super().__init__(ObjectKind.VIEW, [query], steps=(exposed,), exposed=exposed)
        self._query = query

    @property
    def query(self) -> QueryNode:
        return self._query

    def get_signature(self) -> list[Named[SqlType]]:
        return self._query.get_signature()

    @classmethod
    def build(cls, namer: "ObjectNamer", query: QueryNode, *, exposed_name: Optional[str] = None) -> "DuckDBCreateMacro":
        name = exposed_name or namer.mint("view")
        return cls(query, DuckDBMacroObject(name=name, query=query, macro_name=f"{name}_macro"))


@dataclass(frozen=True)
class DuckDBIndexObject(Object):
    body_ref: str = ""
    index_name: str = ""
    out_cols: tuple[str, ...] = ()
    target: str = ""
    unique: bool = False

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, DuckDBSetupVisitor):
            return cast(T, visitor.visit_duckdb_index_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


class DuckDBCreateIndex(EquivalentRelation):
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
    ) -> "DuckDBCreateIndex":
        name = exposed_name or namer.mint("view")
        return cls(
            body,
            DuckDBIndexObject(
                name=name,
                body_ref=body.materialized_name,
                index_name=f"{name}_idx",
                out_cols=tuple(out_cols),
                target=target,
                unique=unique,
            ),
        )


@dataclass(frozen=True)
class DuckDBAttachObject(Object):
    body_ref: str = ""
    alias: str = ""
    mirror: str = ""

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, DuckDBSetupVisitor):
            return cast(T, visitor.visit_duckdb_attach_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


class DuckDBCreateAttach(EquivalentRelation):
    """``ATTACH ':memory:'`` mirror — algebra **(Mat)** (cross-catalog resolution)."""

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
        cls, namer: "ObjectNamer", body: EquivalentRelation, *, exposed_name: Optional[str] = None
    ) -> "DuckDBCreateAttach":
        name = exposed_name or namer.mint("view")
        return cls(
            body,
            DuckDBAttachObject(
                name=name,
                body_ref=body.materialized_name,
                alias=f"{name}_att_db",
                mirror="mirror",
            ),
        )


class DuckDBCatalogKind(Enum):
    SCHEMA = auto()
    ADD_DROP_COLUMN = auto()
    ENUM_ROUND_TRIP = auto()
    CHECKPOINT = auto()


@dataclass(frozen=True)
class DuckDBCatalogObject(Object):
    body_ref: str = ""
    out_cols: tuple[str, ...] = ()
    kind: DuckDBCatalogKind = DuckDBCatalogKind.SCHEMA
    aux: str = ""
    aux_table: str = ""
    extra_col: str = ""
    text_col: str = ""

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, DuckDBSetupVisitor):
            return cast(T, visitor.visit_duckdb_catalog_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


class DuckDBCreateCatalog(EquivalentRelation):
    """Catalog-object round trip — algebra **(Mat)**."""

    _AUX_LABEL: ClassVar[dict[DuckDBCatalogKind, str]] = {
        DuckDBCatalogKind.SCHEMA: "sch",
        DuckDBCatalogKind.ADD_DROP_COLUMN: "altered",
        DuckDBCatalogKind.ENUM_ROUND_TRIP: "enum",
        DuckDBCatalogKind.CHECKPOINT: "ckpt",
    }

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
        kind: DuckDBCatalogKind,
        extra_col: str = "",
        text_col: str = "",
        exposed_name: Optional[str] = None,
    ) -> "DuckDBCreateCatalog":
        name = exposed_name or namer.mint("view")
        return cls(
            body,
            DuckDBCatalogObject(
                name=name,
                body_ref=body.materialized_name,
                out_cols=tuple(named.alias for named in body.get_signature()),
                kind=kind,
                aux=f"{name}_{cls._AUX_LABEL[kind]}",
                aux_table=f"{name}_tbl",
                extra_col=extra_col,
                text_col=text_col,
            ),
        )


__all__ = [
    "DuckDBAttachObject",
    "DuckDBCatalogKind",
    "DuckDBCatalogObject",
    "DuckDBCreateAttach",
    "DuckDBCreateCatalog",
    "DuckDBCreateIndex",
    "DuckDBCreateMacro",
    "DuckDBIndexObject",
    "DuckDBMacroObject",
    "DuckDBPivotStructQuery",
    "DuckDBPositionalJoinQuery",
    "DuckDBQueryVisitor",
    "DuckDBRecursiveCteQuery",
    "DuckDBSetupVisitor",
    "DuckDBStarReplaceQuery",
    "SetupStep",
]
