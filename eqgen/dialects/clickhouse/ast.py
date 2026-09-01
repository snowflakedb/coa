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

"""ClickHouse-only AST nodes and visitor methods for physical MergeTree layouts."""

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
        f"{type(node).__name__} is a ClickHouse-only node and {type(visitor).__name__} cannot render it. "
        "A dialect node must be rendered by that dialect's emitter."
    )


class ClickHouseSetupVisitor(SetupVisitor[T]):
    def visit_clickhouse_projection_object(self, node: "ClickHouseProjectionObject") -> T:
        raise NotImplementedError

    def visit_clickhouse_skip_index_object(self, node: "ClickHouseSkipIndexObject") -> T:
        raise NotImplementedError

    def visit_clickhouse_part_layout_object(self, node: "ClickHousePartLayoutObject") -> T:
        raise NotImplementedError

    def visit_clickhouse_codec_object(self, node: "ClickHouseCodecObject") -> T:
        raise NotImplementedError


class ClickHouseSkipIndexType(enum.Enum):
    MINMAX = "minmax"
    SET = "set(100)"
    BLOOM_FILTER = "bloom_filter"
    TOKENBF = "tokenbf_v1(256, 2, 0)"
    NGRAMBF = "ngrambf_v1(3, 256, 2, 0)"


class ClickHousePartLayoutKind(enum.Enum):
    SORTED = "sorted"
    PARTITIONED = "partitioned"
    FINE_GRANULES = "fine_granules"


class ClickHouseCodec(enum.Enum):
    ZSTD = "ZSTD"
    DELTA_ZSTD = "Delta, ZSTD"


@dataclass(frozen=True)
class _ClickHousePhysicalObject(Object):
    body_ref: str = ""
    child_name: str = ""
    out_cols: tuple[str, ...] = ()
    col_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClickHouseProjectionObject(_ClickHousePhysicalObject):
    order_by: str = ""
    projection_name: str = ""

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, ClickHouseSetupVisitor):
            return cast(T, visitor.visit_clickhouse_projection_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


@dataclass(frozen=True)
class ClickHouseSkipIndexObject(_ClickHousePhysicalObject):
    index_type: ClickHouseSkipIndexType = ClickHouseSkipIndexType.MINMAX
    column: str = ""
    index_name: str = ""

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, ClickHouseSetupVisitor):
            return cast(T, visitor.visit_clickhouse_skip_index_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


@dataclass(frozen=True)
class ClickHousePartLayoutObject(_ClickHousePhysicalObject):
    kind: ClickHousePartLayoutKind = ClickHousePartLayoutKind.SORTED
    key_column: str = ""

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, ClickHouseSetupVisitor):
            return cast(T, visitor.visit_clickhouse_part_layout_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


@dataclass(frozen=True)
class ClickHouseCodecObject(_ClickHousePhysicalObject):
    codec: ClickHouseCodec = ClickHouseCodec.ZSTD
    codec_columns: tuple[str, ...] = ()

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, ClickHouseSetupVisitor):
            return cast(T, visitor.visit_clickhouse_codec_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


class _ClickHouseNativeCreate(EquivalentRelation):
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
    from eqgen.dialects.clickhouse.types_sql import clickhouse_type

    return tuple(clickhouse_type(named.target) for named in body.get_signature())


def numeric_columns(body: EquivalentRelation) -> tuple[str, ...]:
    """Fixed-width columns that accept ``CODEC(Delta)`` / ``ifNull(..., 0)`` keys."""
    fixed = ("Int", "Float", "Decimal", "Date", "UUID", "Bool")
    return tuple(
        named.alias
        for named, rendered in zip(body.get_signature(), _col_types(body), strict=True)
        if any(word in rendered for word in fixed)
    )


def bloom_filter_columns(body: EquivalentRelation, out_cols: Sequence[str]) -> tuple[str, ...]:
    """Columns ClickHouse accepts for ``bloom_filter`` indexes (no Decimal / Date32)."""
    allowed = []
    by_alias = {named.alias: rendered for named, rendered in zip(body.get_signature(), _col_types(body), strict=True)}
    for col in out_cols:
        rendered = by_alias.get(col, "")
        if "Decimal" in rendered or "Date32" in rendered:
            continue
        allowed.append(col)
    return tuple(allowed)


def string_columns(body: EquivalentRelation, out_cols: Sequence[str]) -> tuple[str, ...]:
    """String / FixedString columns for ``tokenbf_v1`` / ``ngrambf_v1`` indexes."""
    by_alias = {named.alias: rendered for named, rendered in zip(body.get_signature(), _col_types(body), strict=True)}
    return tuple(
        col
        for col in out_cols
        if "String" in by_alias.get(col, "") and "LowCardinality" not in by_alias.get(col, "")
    )


class ClickHouseCreateProjection(_ClickHouseNativeCreate):
    @classmethod
    def build(
        cls,
        namer: "ObjectNamer",
        body: EquivalentRelation,
        *,
        out_cols: Sequence[str],
        order_by: str,
        exposed_name: Optional[str] = None,
    ) -> "ClickHouseCreateProjection":
        name = exposed_name or namer.mint("view")
        return cls(
            body,
            ClickHouseProjectionObject(
                name=name,
                body_ref=body.materialized_name,
                child_name=namer.mint("table"),
                out_cols=tuple(out_cols),
                col_types=_col_types(body),
                order_by=order_by,
                projection_name=namer.mint("proj"),
            ),
        )


class ClickHouseCreateSkipIndex(_ClickHouseNativeCreate):
    @classmethod
    def build(
        cls,
        namer: "ObjectNamer",
        body: EquivalentRelation,
        *,
        out_cols: Sequence[str],
        index_type: ClickHouseSkipIndexType,
        column: str,
        exposed_name: Optional[str] = None,
    ) -> "ClickHouseCreateSkipIndex":
        name = exposed_name or namer.mint("view")
        return cls(
            body,
            ClickHouseSkipIndexObject(
                name=name,
                body_ref=body.materialized_name,
                child_name=namer.mint("table"),
                out_cols=tuple(out_cols),
                col_types=_col_types(body),
                index_type=index_type,
                column=column,
                index_name=namer.mint("idx"),
            ),
        )


class ClickHouseCreatePartLayout(_ClickHouseNativeCreate):
    @classmethod
    def build(
        cls,
        namer: "ObjectNamer",
        body: EquivalentRelation,
        *,
        out_cols: Sequence[str],
        kind: ClickHousePartLayoutKind,
        key_column: str,
        exposed_name: Optional[str] = None,
    ) -> "ClickHouseCreatePartLayout":
        name = exposed_name or namer.mint("view")
        return cls(
            body,
            ClickHousePartLayoutObject(
                name=name,
                body_ref=body.materialized_name,
                child_name=namer.mint("table"),
                out_cols=tuple(out_cols),
                col_types=_col_types(body),
                kind=kind,
                key_column=key_column,
            ),
        )


class ClickHouseCreateCodec(_ClickHouseNativeCreate):
    @classmethod
    def build(
        cls,
        namer: "ObjectNamer",
        body: EquivalentRelation,
        *,
        out_cols: Sequence[str],
        codec: ClickHouseCodec,
        codec_columns: Sequence[str],
        exposed_name: Optional[str] = None,
    ) -> "ClickHouseCreateCodec":
        name = exposed_name or namer.mint("view")
        return cls(
            body,
            ClickHouseCodecObject(
                name=name,
                body_ref=body.materialized_name,
                child_name=namer.mint("table"),
                out_cols=tuple(out_cols),
                col_types=_col_types(body),
                codec=codec,
                codec_columns=tuple(codec_columns),
            ),
        )
