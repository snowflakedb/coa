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

"""MySQL-only AST nodes and the visitor methods that render them.

    MySqlIndexObject             CREATE INDEX (+ optional prefix / INVISIBLE) + exposing view
    MySqlTableOptionObject       typed CREATE TABLE … ENGINE=InnoDB + INSERT + exposing view
    MySqlJsonPackRoundTripObject whole-row JSON_OBJECT pack, JSON_EXTRACT/JSON_UNQUOTE unpack

``accept`` checks the visitor understands MySQL and raises otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
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
        f"{type(node).__name__} is a MySQL-only node and {type(visitor).__name__} cannot render it. "
        "A dialect node must be rendered by that dialect's emitter."
    )


class MySqlSetupVisitor(SetupVisitor[T]):
    """Shared setup visitor plus a method per MySQL-only object."""

    def visit_mysql_index_object(self, node: "MySqlIndexObject") -> T:
        raise NotImplementedError

    def visit_mysql_table_option_object(self, node: "MySqlTableOptionObject") -> T:
        raise NotImplementedError

    def visit_mysql_json_pack_round_trip_object(self, node: "MySqlJsonPackRoundTripObject") -> T:
        raise NotImplementedError


class MySqlIndexKind(Enum):
    PLAIN = "plain"
    PREFIX = "prefix"
    UNIQUE = "unique"
    INVISIBLE = "invisible"


@dataclass(frozen=True)
class MySqlIndexObject(Object):
    """An index on a CTAS body, plus a view that exposes the base columns."""

    body_ref: str = ""
    index_name: str = ""
    out_cols: tuple[str, ...] = ()
    kind: MySqlIndexKind = MySqlIndexKind.PLAIN
    target: str = ""
    prefix_length: int = 10

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, MySqlSetupVisitor):
            return cast(T, visitor.visit_mysql_index_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


class MySqlCreateIndex(EquivalentRelation):
    """Indexed table body, exposed under a view so the workload still reads Σ."""

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
        kind: MySqlIndexKind,
        target: str,
        out_cols: Sequence[str],
        exposed_name: Optional[str] = None,
        prefix_length: int = 10,
    ) -> "MySqlCreateIndex":
        name = exposed_name or namer.mint("view")
        return cls(
            body,
            MySqlIndexObject(
                name=name,
                body_ref=body.materialized_name,
                index_name=f"{name}_idx",
                out_cols=tuple(out_cols),
                kind=kind,
                target=target,
                prefix_length=prefix_length,
            ),
        )


@dataclass(frozen=True)
class MySqlTableOptionObject(Object):
    """Typed table with ``ENGINE=…``, loaded from a query body, plus an exposing view."""

    body_ref: str = ""
    child_name: str = ""
    out_cols: tuple[str, ...] = ()
    col_types: tuple[str, ...] = ()
    engine: str = "InnoDB"

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, MySqlSetupVisitor):
            return cast(T, visitor.visit_mysql_table_option_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


class MySqlCreateTableOption(EquivalentRelation):
    """Storage-engine Mat: typed CREATE + INSERT…SELECT + exposing view."""

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
        col_types: Sequence[str],
        engine: str = "InnoDB",
        exposed_name: Optional[str] = None,
    ) -> "MySqlCreateTableOption":
        name = exposed_name or namer.mint("view")
        return cls(
            body,
            MySqlTableOptionObject(
                name=name,
                body_ref=body.materialized_name,
                child_name=f"{name}_tbl",
                out_cols=tuple(out_cols),
                col_types=tuple(col_types),
                engine=engine,
            ),
        )


@dataclass(frozen=True)
class MySqlJsonPackRoundTripObject(Object):
    """Pack every column into one ``JSON_OBJECT`` value, then unpack it back out.

    ``columns`` is ``(name, cast_type, needs_unquote)`` per column: ``needs_unquote`` is true for
    types JSON stores as a quoted string (``DATE``/``DATETIME``/text) and false for types it stores
    as a bare JSON number (``BOOLEAN``/``DOUBLE``/integer/``DECIMAL``). A JSON *null* member (as
    opposed to the whole ``JSON_OBJECT`` value itself being SQL ``NULL``) reads back as ``0``/``''``
    through a bare ``CAST``, not SQL ``NULL`` — each column is guarded against that explicitly.
    """

    body_ref: str = ""
    columns: tuple[tuple[str, str, bool], ...] = ()

    def accept(self, visitor: SetupVisitor[T]) -> T:
        if isinstance(visitor, MySqlSetupVisitor):
            return cast(T, visitor.visit_mysql_json_pack_round_trip_object(self))
        _unsupported(visitor, self)
        raise AssertionError("unreachable")


class MySqlJsonPackRoundTrip(EquivalentRelation):
    """A view whose ``SELECT`` list packs then unpacks every column through ``JSON_OBJECT``."""

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
        columns: Sequence[tuple[str, str, bool]],
        exposed_name: Optional[str] = None,
    ) -> "MySqlJsonPackRoundTrip":
        name = exposed_name or namer.mint("view")
        return cls(
            body,
            MySqlJsonPackRoundTripObject(
                name=name,
                body_ref=body.materialized_name,
                columns=tuple(columns),
            ),
        )
