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

"""PostgreSQL-only AST nodes and the visitor methods that render them.

    PostgresDistinctOnQuery          query        DISTINCT ON (c_pk) over a unique key
    PostgresIndexObject              statement    CREATE INDEX (+ optional expr/partial/INCLUDE)
    PostgresPrimaryKeyObject         statement    ALTER TABLE … ADD PRIMARY KEY
    PostgresMergeUpsertObject        statement    no-op self-MERGE upsert (PG15+)
    PostgresGeneratedColumnObject    statement    ALTER TABLE … ADD COLUMN … GENERATED STORED
    PostgresLegacyInheritanceObject  statement    ALTER TABLE … INHERIT (legacy, pre-declarative)
    PostgresDomainColumnObject       statement    CREATE DOMAIN + ALTER COLUMN … TYPE
    PostgresSecurityBarrierViewObject statement   CREATE VIEW … WITH (security_barrier)
    PostgresExtendedStatisticsObject statement    CREATE STATISTICS + ANALYZE + exposing view
    PostgresPartitionedTableObject   statement    PARTITION BY RANGE + INSERT + view
    PostgresParallelToggleObject     statement    parallel GUCs + exposing view

``accept`` checks the visitor understands PostgreSQL and raises otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Optional, Sequence, TypeVar, cast

from eqgen.core.catalog import Named
from eqgen.core.types import SqlType
from eqgen.equivalence.ast import (
    DialectNativeQuery,
    EquivalentRelation,
    EqNode,
    ProjectionItem,
    QueryNode,
)
from eqgen.equivalence.capabilities import ObjectKind
from eqgen.equivalence.objects import Object
from eqgen.equivalence.visitor import QueryVisitor, SetupVisitor

if TYPE_CHECKING:
    from eqgen.equivalence.context import ObjectNamer

T = TypeVar("T")
_T = TypeVar("_T")


def _unsupported(visitor: object, node: object) -> None:
    raise TypeError(
        f"{type(node).__name__} is a PostgreSQL-only node and {type(visitor).__name__} cannot render it. "
        "A dialect node must be rendered by that dialect's emitter."
    )


class PostgresQueryVisitor(QueryVisitor[T]):
    def visit_postgres_distinct_on_query(self, query: "PostgresDistinctOnQuery") -> T:
        raise NotImplementedError


class PostgresSetupVisitor(SetupVisitor[T]):
    """Shared setup visitor plus a method per PostgreSQL-only object."""

    def visit_postgres_index_object(self, node: "PostgresIndexObject") -> T:
        raise NotImplementedError

    def visit_postgres_primary_key_object(self, node: "PostgresPrimaryKeyObject") -> T:
        raise NotImplementedError

    def visit_postgres_merge_upsert_object(self, node: "PostgresMergeUpsertObject") -> T:
        raise NotImplementedError

    def visit_postgres_generated_column_object(self, node: "PostgresGeneratedColumnObject") -> T:
        raise NotImplementedError

    def visit_postgres_legacy_inheritance_object(self, node: "PostgresLegacyInheritanceObject") -> T:
        raise NotImplementedError

    def visit_postgres_domain_column_object(self, node: "PostgresDomainColumnObject") -> T:
        raise NotImplementedError

    def visit_postgres_security_barrier_view_object(self, node: "PostgresSecurityBarrierViewObject") -> T:
        raise NotImplementedError

    def visit_postgres_extended_statistics_object(self, node: "PostgresExtendedStatisticsObject") -> T:
        raise NotImplementedError

    def visit_postgres_partitioned_table_object(self, node: "PostgresPartitionedTableObject") -> T:
        raise NotImplementedError

    def visit_postgres_parallel_toggle_object(self, node: "PostgresParallelToggleObject") -> T:
        raise NotImplementedError


class PostgresIndexMethod(Enum):
    """Index access methods this dialect builds. The value is the SQL spelling."""

    BTREE = "btree"
    HASH = "hash"
    BRIN = "brin"
    GIN = "gin"
    GIST = "gist"


class PostgresDistinctOnQuery(DialectNativeQuery):
    """``SELECT DISTINCT ON (<key>) <cols> FROM <source> ORDER BY <key>``.

    Row-preserving only when *key* is unique (eqgen uses seed ``c_pk``).
    """

    def __init__(self, source: EqNode, key_col: str, base_items: Sequence[ProjectionItem]) -> None:
        super().__init__([source])
        self._source = source
        self._key_col = key_col
        self._base_items: tuple[ProjectionItem, ...] = tuple(base_items)

    @property
    def source(self) -> EqNode:
        return self._source

    @property
    def key_col(self) -> str:
        return self._key_col

    @property
    def out_cols(self) -> tuple[str, ...]:
        return tuple(item.alias for item in self._base_items)

    def get_signature(self) -> list[Named[SqlType]]:
        return [Named(alias=item.alias, target=item.data_type) for item in self._base_items]

    def accept(self, visitor: QueryVisitor[_T]) -> _T:
        if isinstance(visitor, PostgresQueryVisitor):
            return cast(_T, visitor.visit_postgres_distinct_on_query(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


@dataclass(frozen=True)
class PostgresIndexObject(Object):
    """An index on a CTAS table, plus a view that exposes the base columns.

    Optional *expression* (e.g. ``lower``), *predicate* (partial), and *include* (covering)
    specialize the ``CREATE INDEX`` without changing the exposed row signature.
    """

    body_ref: str = ""
    index_name: str = ""
    out_cols: tuple[str, ...] = ()
    method: PostgresIndexMethod = PostgresIndexMethod.BTREE
    target: str = ""
    expression: Optional[str] = None
    predicate: Optional[str] = None
    include: tuple[str, ...] = ()

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, PostgresSetupVisitor):
            return cast(T, visitor.visit_postgres_index_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


class PostgresCreateIndex(EquivalentRelation):
    """Indexed table body, exposed under a view so the workload still reads ``Σ``."""

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
        method: PostgresIndexMethod,
        target: str,
        out_cols: Sequence[str],
        exposed_name: Optional[str] = None,
        expression: Optional[str] = None,
        predicate: Optional[str] = None,
        include: Sequence[str] = (),
    ) -> "PostgresCreateIndex":
        name = exposed_name or namer.mint("view")
        return cls(
            body,
            PostgresIndexObject(
                name=name,
                body_ref=body.materialized_name,
                index_name=f"{name}_idx",
                out_cols=tuple(out_cols),
                method=method,
                target=target,
                expression=expression,
                predicate=predicate,
                include=tuple(include),
            ),
        )


@dataclass(frozen=True)
class PostgresPrimaryKeyObject(Object):
    """PRIMARY KEY on a CTAS body, plus an exposing view."""

    body_ref: str = ""
    out_cols: tuple[str, ...] = ()
    target: str = ""

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, PostgresSetupVisitor):
            return cast(T, visitor.visit_postgres_primary_key_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


class PostgresCreatePrimaryKey(EquivalentRelation):
    """Table body with PRIMARY KEY on ``c_pk``, exposed under a view. Algebra **(Mat)** / Mat⁺."""

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
    ) -> "PostgresCreatePrimaryKey":
        name = exposed_name or namer.mint("view")
        return cls(
            body,
            PostgresPrimaryKeyObject(
                name=name,
                body_ref=body.materialized_name,
                out_cols=tuple(out_cols),
                target=target,
            ),
        )


@dataclass(frozen=True)
class PostgresMergeUpsertObject(Object):
    """A CTAS table upserted from an identical copy via ``MERGE`` — every row takes the
    ``WHEN MATCHED`` branch (the copy shares every ``pk_col`` value), so ``WHEN NOT MATCHED``
    never fires. Same rows, same values, written through PG15's newest DML statement.
    """

    body_ref: str = ""
    src_ref: str = ""
    out_cols: tuple[str, ...] = ()
    pk_col: str = ""

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, PostgresSetupVisitor):
            return cast(T, visitor.visit_postgres_merge_upsert_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


class PostgresMergeUpsert(EquivalentRelation):
    """Table body re-upserted via a no-op self-``MERGE``, exposed under a view. Algebra **(Mat)**."""

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
        pk_col: str,
        out_cols: Sequence[str],
        exposed_name: Optional[str] = None,
    ) -> "PostgresMergeUpsert":
        name = exposed_name or namer.mint("view")
        return cls(
            body,
            PostgresMergeUpsertObject(
                name=name,
                body_ref=body.materialized_name,
                src_ref=namer.mint("merge_src"),
                out_cols=tuple(out_cols),
                pk_col=pk_col,
            ),
        )


@dataclass(frozen=True)
class PostgresGeneratedColumnObject(Object):
    """A CTAS table with a ``GENERATED ALWAYS AS (<target>) STORED`` twin of one column, added by
    ``ALTER TABLE ... ADD COLUMN``, exposed under the *target*'s own name in place of the raw
    column — same values, computed through PostgreSQL's generated-column machinery instead of
    stored directly.
    """

    body_ref: str = ""
    out_cols: tuple[str, ...] = ()
    target: str = ""
    gen_col: str = ""
    gen_type_sql: str = ""

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, PostgresSetupVisitor):
            return cast(T, visitor.visit_postgres_generated_column_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


class PostgresGeneratedColumn(EquivalentRelation):
    """Table body with a generated-column twin of one column, exposed under a view. Algebra **(Mat)**."""

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
        gen_type_sql: str,
        out_cols: Sequence[str],
        exposed_name: Optional[str] = None,
    ) -> "PostgresGeneratedColumn":
        name = exposed_name or namer.mint("view")
        return cls(
            body,
            PostgresGeneratedColumnObject(
                name=name,
                body_ref=body.materialized_name,
                out_cols=tuple(out_cols),
                target=target,
                gen_col=f"{target}_gen",
                gen_type_sql=gen_type_sql,
            ),
        )


@dataclass(frozen=True)
class PostgresLegacyInheritanceObject(Object):
    """A CTAS table made a child of an empty parent via legacy (pre-declarative) table
    inheritance — ``CREATE TABLE parent (LIKE body)`` then ``ALTER TABLE body INHERIT parent``.
    The exposing view queries the *parent*: an inheritance scan returns the parent's own (empty)
    rows unioned with every child's, so it reads back exactly ``body``'s rows through the old
    constraint-exclusion / inherited-column planning path — architecturally distinct from
    declarative partitioning, which has its own dedicated pruning logic.
    """

    body_ref: str = ""
    parent_ref: str = ""
    out_cols: tuple[str, ...] = ()

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, PostgresSetupVisitor):
            return cast(T, visitor.visit_postgres_legacy_inheritance_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


class PostgresLegacyInheritance(EquivalentRelation):
    """Table body made a child of an empty parent via legacy inheritance, exposed under a view
    querying the parent. Algebra **(Mat)**.
    """

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
        exposed_name: Optional[str] = None,
    ) -> "PostgresLegacyInheritance":
        name = exposed_name or namer.mint("view")
        return cls(
            body,
            PostgresLegacyInheritanceObject(
                name=name,
                body_ref=body.materialized_name,
                parent_ref=namer.mint("parent"),
                out_cols=tuple(out_cols),
            ),
        )


@dataclass(frozen=True)
class PostgresDomainColumnObject(Object):
    """A CTAS table with one column re-typed to a fresh, constraint-free ``DOMAIN`` over its own
    base type — ``CREATE DOMAIN d AS <base type>`` then ``ALTER TABLE body ALTER COLUMN target
    TYPE d``. Same values, same underlying representation, read back through PostgreSQL's domain
    type-checking/comparison path instead of the bare base type.
    """

    body_ref: str = ""
    out_cols: tuple[str, ...] = ()
    target: str = ""
    domain_name: str = ""
    base_type_sql: str = ""

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, PostgresSetupVisitor):
            return cast(T, visitor.visit_postgres_domain_column_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


class PostgresDomainColumn(EquivalentRelation):
    """Table body with one column re-typed to a domain over its own base type, exposed under a
    view. Algebra **(Mat)**.
    """

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
        base_type_sql: str,
        out_cols: Sequence[str],
        exposed_name: Optional[str] = None,
    ) -> "PostgresDomainColumn":
        name = exposed_name or namer.mint("view")
        return cls(
            body,
            PostgresDomainColumnObject(
                name=name,
                body_ref=body.materialized_name,
                out_cols=tuple(out_cols),
                target=target,
                domain_name=namer.mint("domain"),
                base_type_sql=base_type_sql,
            ),
        )


@dataclass(frozen=True)
class PostgresSecurityBarrierViewObject(Object):
    """``CREATE VIEW … WITH (security_barrier = true) AS <query>``."""

    query: Optional[QueryNode] = None

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, PostgresSetupVisitor):
            return cast(T, visitor.visit_postgres_security_barrier_view_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


class PostgresCreateSecurityBarrierView(EquivalentRelation):
    """Security-barrier view — plan-only (blocks pushdown). Algebra **(Mat)**."""

    def __init__(self, query: QueryNode, exposed: Object) -> None:
        super().__init__(ObjectKind.VIEW, [query], steps=(exposed,), exposed=exposed)
        self._query = query

    @property
    def query(self) -> QueryNode:
        return self._query

    def get_signature(self) -> list[Named[SqlType]]:
        return self._query.get_signature()

    @classmethod
    def build(
        cls, namer: "ObjectNamer", query: QueryNode, *, exposed_name: Optional[str] = None
    ) -> "PostgresCreateSecurityBarrierView":
        name = exposed_name or namer.mint("view")
        return cls(query, PostgresSecurityBarrierViewObject(name=name, query=query))


@dataclass(frozen=True)
class PostgresExtendedStatisticsObject(Object):
    """CTAS body already exists; attach multivariate stats + ANALYZE + exposing view."""

    body_ref: str = ""
    stats_name: str = ""
    out_cols: tuple[str, ...] = ()
    stat_cols: tuple[str, ...] = ()

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, PostgresSetupVisitor):
            return cast(T, visitor.visit_postgres_extended_statistics_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


class PostgresCreateExtendedStatistics(EquivalentRelation):
    """``CREATE STATISTICS`` + ``ANALYZE`` on a CTAS body. Estimate-only; rows unchanged."""

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
        stat_cols: Sequence[str],
        exposed_name: Optional[str] = None,
    ) -> "PostgresCreateExtendedStatistics":
        name = exposed_name or namer.mint("view")
        return cls(
            body,
            PostgresExtendedStatisticsObject(
                name=name,
                body_ref=body.materialized_name,
                stats_name=f"{name}_st",
                out_cols=tuple(out_cols),
                stat_cols=tuple(stat_cols),
            ),
        )


@dataclass(frozen=True)
class PostgresPartitionedTableObject(Object):
    """Partitioned parent filled from a CTAS body, exposed under a view.

    ``PARTITION BY RANGE (c_pk)`` with two partitions split at *split_at* — planner partition
    pruning without changing exposed rows. Column types come from ``LIKE`` the body so they
    match CTAS-inferred types (avoids INSERT failures when a rewrite widens a column).
    """

    body_ref: str = ""
    parent_name: str = ""
    out_cols: tuple[str, ...] = ()
    partition_key: str = "c_pk"
    split_at: int = 5

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, PostgresSetupVisitor):
            return cast(T, visitor.visit_postgres_partitioned_table_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


class PostgresCreatePartitionedTable(EquivalentRelation):
    """Range-partitioned copy of a CTAS body. Algebra **(Mat)**."""

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
        partition_key: str = "c_pk",
        split_at: int = 5,
        exposed_name: Optional[str] = None,
    ) -> "PostgresCreatePartitionedTable":
        name = exposed_name or namer.mint("view")
        parent = f"{name}_part"
        return cls(
            body,
            PostgresPartitionedTableObject(
                name=name,
                body_ref=body.materialized_name,
                parent_name=parent,
                out_cols=tuple(out_cols),
                partition_key=partition_key,
                split_at=split_at,
            ),
        )


@dataclass(frozen=True)
class PostgresParallelToggleObject(Object):
    """Session parallel GUCs then exposing view — same rows, parallel plans encouraged."""

    body_ref: str = ""
    out_cols: tuple[str, ...] = ()

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, PostgresSetupVisitor):
            return cast(T, visitor.visit_postgres_parallel_toggle_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


class PostgresCreateParallelToggle(EquivalentRelation):
    """Force cheap parallel plans on the equivalent connection. Algebra **(Mat)**."""

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
        exposed_name: Optional[str] = None,
    ) -> "PostgresCreateParallelToggle":
        name = exposed_name or namer.mint("view")
        return cls(
            body,
            PostgresParallelToggleObject(
                name=name,
                body_ref=body.materialized_name,
                out_cols=tuple(out_cols),
            ),
        )


__all__ = [
    "PostgresCreateExtendedStatistics",
    "PostgresCreateIndex",
    "PostgresCreateParallelToggle",
    "PostgresCreatePartitionedTable",
    "PostgresCreatePrimaryKey",
    "PostgresCreateSecurityBarrierView",
    "PostgresDistinctOnQuery",
    "PostgresExtendedStatisticsObject",
    "PostgresIndexMethod",
    "PostgresIndexObject",
    "PostgresParallelToggleObject",
    "PostgresPartitionedTableObject",
    "PostgresPrimaryKeyObject",
    "PostgresQueryVisitor",
    "PostgresSecurityBarrierViewObject",
    "PostgresSetupVisitor",
    "QueryNode",
]
