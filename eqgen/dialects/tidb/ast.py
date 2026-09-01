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

"""TiDB-only AST nodes and the visitor methods that render them."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Sequence, TypeVar, cast

from eqgen.core.catalog import Named
from eqgen.core.types import SqlType
from eqgen.dialects.mysql.types_sql import mysql_type
from eqgen.equivalence.ast import EquivalentRelation
from eqgen.equivalence.capabilities import ObjectKind
from eqgen.equivalence.objects import Object
from eqgen.equivalence.visitor import SetupVisitor

if TYPE_CHECKING:
    from eqgen.equivalence.context import ObjectNamer

T = TypeVar("T")


def _unsupported(visitor: object, node: object) -> None:
    raise TypeError(
        f"{type(node).__name__} is a TiDB-only node and {type(visitor).__name__} cannot render it. "
        "A dialect node must be rendered by that dialect's emitter."
    )


class TiDbSetupVisitor(SetupVisitor[T]):
    """Shared setup visitor plus a method per TiDB-only object."""

    def visit_tidb_cached_table_object(self, node: "TiDbCachedTableObject") -> T:
        raise NotImplementedError


@dataclass(frozen=True)
class TiDbCachedTableObject(Object):
    """Typed child table, ``ALTER TABLE ... CACHE``, plus an exposing view."""

    body_ref: str = ""
    child_name: str = ""
    out_cols: tuple[str, ...] = ()
    col_types: tuple[str, ...] = ()

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, TiDbSetupVisitor):
            return cast(T, visitor.visit_tidb_cached_table_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


class TiCreateCachedTable(EquivalentRelation):
    """Table materialized from a query body, cached, exposed under a view."""

    def __init__(self, body: EquivalentRelation, exposed: Object) -> None:
        super().__init__(ObjectKind.VIEW, [body], steps=(exposed,), exposed=exposed)
        self._body = body
        self._out_cols: tuple[str, ...] = tuple(getattr(exposed, "out_cols", ()))

    @property
    def body(self) -> EquivalentRelation:
        return self._body

    def get_signature(self) -> list[Named[SqlType]]:
        body_signature = self._body.get_signature()
        if not self._out_cols:
            return body_signature
        by_alias = {named.alias: named for named in body_signature}
        return [by_alias[col] for col in self._out_cols if col in by_alias]

    @classmethod
    def build(
        cls,
        namer: "ObjectNamer",
        body: EquivalentRelation,
        *,
        out_cols: Sequence[str],
        exposed_name: Optional[str] = None,
    ) -> "TiCreateCachedTable":
        name = exposed_name or namer.mint("view")
        col_types = tuple(mysql_type(named.target) for named in body.get_signature())
        return cls(
            body,
            TiDbCachedTableObject(
                name=name,
                body_ref=body.materialized_name,
                child_name=f"{name}_tbl",
                out_cols=tuple(out_cols),
                col_types=col_types,
            ),
        )
