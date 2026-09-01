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

"""CrateDB-only AST nodes and the visitor methods that render them."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Sequence, TypeVar, cast

from eqgen.core.catalog import Named
from eqgen.core.types import SqlType
from eqgen.equivalence.ast import EquivalentRelation
from eqgen.equivalence.capabilities import ObjectKind
from eqgen.equivalence.objects import Object
from eqgen.equivalence.visitor import SetupVisitor

if TYPE_CHECKING:
    from eqgen.equivalence.context import ObjectNamer

T = TypeVar("T")


def _unsupported(visitor: object, node: object) -> None:
    raise TypeError(
        f"{type(node).__name__} is a CrateDB-only node and {type(visitor).__name__} cannot render it. "
        "A dialect node must be rendered by that dialect's emitter."
    )


class CrateSetupVisitor(SetupVisitor[T]):
    def visit_cratedb_column_index_object(self, node: "CrateColumnIndexObject") -> T:
        raise NotImplementedError

    def visit_cratedb_partitioned_object(self, node: "CratePartitionedObject") -> T:
        raise NotImplementedError

    def visit_cratedb_object_pack_object(self, node: "CrateObjectPackObject") -> T:
        raise NotImplementedError

    def visit_cratedb_shard_layout_object(self, node: "CrateShardLayoutObject") -> T:
        raise NotImplementedError


class CrateIndexMode(enum.Enum):
    INDEX_OFF = "index_off"
    NAMED_FULLTEXT = "named_fulltext"
    COLUMNSTORE_OFF = "columnstore_off"


@dataclass(frozen=True)
class _CratePhysicalObject(Object):
    body_ref: str = ""
    child_name: str = ""
    out_cols: tuple[str, ...] = ()
    col_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class CrateColumnIndexObject(_CratePhysicalObject):
    mode: CrateIndexMode = CrateIndexMode.INDEX_OFF
    index_columns: tuple[str, ...] = ()
    index_name: str = ""

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, CrateSetupVisitor):
            return cast(T, visitor.visit_cratedb_column_index_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


@dataclass(frozen=True)
class CratePartitionedObject(_CratePhysicalObject):
    bucket_column: str = ""
    bucket_source: str = ""
    buckets: int = 4

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, CrateSetupVisitor):
            return cast(T, visitor.visit_cratedb_partitioned_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


@dataclass(frozen=True)
class CrateObjectPackObject(_CratePhysicalObject):
    object_column: str = ""

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, CrateSetupVisitor):
            return cast(T, visitor.visit_cratedb_object_pack_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


@dataclass(frozen=True)
class CrateShardLayoutObject(_CratePhysicalObject):
    """Row-neutral re-cluster: same rows, spread across multiple shards (optionally routed by a
    column). Everything else here is single-shard, so this is the only handle on CrateDB's
    cross-shard merge/sort/aggregate paths."""

    shards: int = 4
    routing_column: str = ""

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, CrateSetupVisitor):
            return cast(T, visitor.visit_cratedb_shard_layout_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


class _CrateNativeCreate(EquivalentRelation):
    def __init__(self, body: EquivalentRelation, step: Object) -> None:
        super().__init__(ObjectKind.VIEW, [body], steps=(step,), exposed=step)
        self._body = body
        self._out_cols: tuple[str, ...] = tuple(getattr(step, "out_cols", ()))

    @property
    def body(self) -> EquivalentRelation:
        return self._body

    def get_signature(self) -> list[Named[SqlType]]:
        body_signature = self._body.get_signature()
        if not self._out_cols:
            return body_signature
        by_alias = {named.alias: named for named in body_signature}
        return [by_alias[col] for col in self._out_cols if col in by_alias]


def _col_types(body: EquivalentRelation) -> tuple[str, ...]:
    from eqgen.dialects.cratedb.types_sql import cratedb_type

    return tuple(cratedb_type(named.target) for named in body.get_signature())


def indexable_columns(body: EquivalentRelation) -> tuple[str, ...]:
    return tuple(
        named.alias
        for named, rendered in zip(body.get_signature(), _col_types(body), strict=True)
        if "OBJECT" not in rendered and "GEO" not in rendered
    )


def indexable_non_numeric_columns(body: EquivalentRelation) -> tuple[str, ...]:
    """INDEX OFF on NUMERIC/DECIMAL is a known silent-empty-range bug
    (``repro/cratedb-20260813-round2-numeric-index-off-range``). Skip those columns so hunts
    look for a different defect instead of flooding the same one."""
    return tuple(
        named.alias
        for named, rendered in zip(body.get_signature(), _col_types(body), strict=True)
        if "OBJECT" not in rendered
        and "GEO" not in rendered
        and not rendered.startswith("NUMERIC")
    )


def fulltext_columns(body: EquivalentRelation) -> tuple[str, ...]:
    return tuple(
        named.alias
        for named, rendered in zip(body.get_signature(), _col_types(body), strict=True)
        if rendered == "TEXT"
    )


def bucketable_columns(body: EquivalentRelation) -> tuple[str, ...]:
    return tuple(
        named.alias
        for named, rendered in zip(body.get_signature(), _col_types(body), strict=True)
        if rendered == "BIGINT"
    )


class CrateCreateColumnIndex(_CrateNativeCreate):
    @classmethod
    def build(
        cls,
        namer: "ObjectNamer",
        body: EquivalentRelation,
        *,
        out_cols: Sequence[str],
        mode: CrateIndexMode,
        index_columns: Sequence[str],
        exposed_name: Optional[str] = None,
    ) -> "CrateCreateColumnIndex":
        name = exposed_name or namer.mint("view")
        return cls(
            body,
            CrateColumnIndexObject(
                name=name,
                body_ref=body.materialized_name,
                child_name=namer.mint("table"),
                out_cols=tuple(out_cols),
                col_types=_col_types(body),
                mode=mode,
                index_columns=tuple(index_columns),
                index_name=namer.mint("idx"),
            ),
        )


class CrateCreatePartitioned(_CrateNativeCreate):
    @classmethod
    def build(
        cls,
        namer: "ObjectNamer",
        body: EquivalentRelation,
        *,
        out_cols: Sequence[str],
        bucket_source: str,
        buckets: int = 4,
        exposed_name: Optional[str] = None,
    ) -> "CrateCreatePartitioned":
        name = exposed_name or namer.mint("view")
        return cls(
            body,
            CratePartitionedObject(
                name=name,
                body_ref=body.materialized_name,
                child_name=namer.mint("table"),
                out_cols=tuple(out_cols),
                col_types=_col_types(body),
                bucket_column=namer.mint("bucket"),
                bucket_source=bucket_source,
                buckets=buckets,
            ),
        )


class CrateCreateObjectPack(_CrateNativeCreate):
    @classmethod
    def build(
        cls,
        namer: "ObjectNamer",
        body: EquivalentRelation,
        *,
        out_cols: Sequence[str],
        exposed_name: Optional[str] = None,
    ) -> "CrateCreateObjectPack":
        name = exposed_name or namer.mint("view")
        return cls(
            body,
            CrateObjectPackObject(
                name=name,
                body_ref=body.materialized_name,
                child_name=namer.mint("table"),
                out_cols=tuple(out_cols),
                col_types=_col_types(body),
                object_column=namer.mint("obj"),
            ),
        )


class CrateCreateShardLayout(_CrateNativeCreate):
    @classmethod
    def build(
        cls,
        namer: "ObjectNamer",
        body: EquivalentRelation,
        *,
        out_cols: Sequence[str],
        shards: int = 4,
        routing_column: str = "",
        exposed_name: Optional[str] = None,
    ) -> "CrateCreateShardLayout":
        name = exposed_name or namer.mint("view")
        return cls(
            body,
            CrateShardLayoutObject(
                name=name,
                body_ref=body.materialized_name,
                child_name=namer.mint("table"),
                out_cols=tuple(out_cols),
                col_types=_col_types(body),
                shards=shards,
                routing_column=routing_column,
            ),
        )
