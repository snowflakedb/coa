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

"""What the input table looks like: :class:`Table`, :class:`Column`, and :class:`Named` for one
output column's name and type.

A description, not a connection. Nothing here opens a database or reads a live schema — a table is
written out in Python and handed to the generator::

    Table("t", [Column("c_int", IntegerType(), 1), Column("c_txt", VarcharType(), 2)])

That is what lets the generator run with no engine present, which is how every unit test runs it.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Generic, Optional, Sequence, TypeVar

from eqgen.core.types import SqlType

# Covariant, so Named[<subclass of T>] is usable as Named[T].
T_co = TypeVar("T_co", covariant=True)


@dataclass(frozen=True)
class Named(Generic[T_co]):
    """An output name paired with what it holds — usually ``Named[SqlType]``.

    One relation's signature is a list of these, and that list is the only thing a parent
    builder needs to know about a child it did not build: the *shape* of the rows, never
    the rows.
    """

    alias: str
    target: T_co


class Relation(abc.ABC):
    """Anything with an output signature: a table, a defining query, a built equivalent.

    The single shared abstraction lets a query read from "the base table" or "an
    equivalent of the base table" without caring which, which is what makes chaining
    equivalences on top of each other possible.
    """

    @abc.abstractmethod
    def get_signature(self) -> list[Named[SqlType]]:
        """The output columns (name + type), in order."""


@dataclass(frozen=True)
class Column:
    """One column: name, type, ordinal position, nullability.

    Position is part of the column's identity rather than incidental — column *order* is
    observable through ``SELECT *``, so an equivalence that preserves rows but permutes
    columns is not equivalent.
    """

    name: str
    data_type: SqlType
    position: int = 0
    nullable: bool = True

    def __post_init__(self) -> None:
        assert self.name, "column name must be non-empty"

    # Accessors, spelled as methods because that is how builders read them.
    def get_column_name(self) -> str:
        return self.name

    def get_data_type(self) -> SqlType:
        return self.data_type

    def get_ordinal_position(self) -> int:
        return self.position

    def get_is_nullable(self) -> bool:
        return self.nullable

    def __str__(self) -> str:
        return f"{self.name} {self.data_type}"


class Table(Relation):
    """A base table: a name, an ordered list of columns, and optionally a schema.

    Immutable by construction. The generator mints many object names derived from
    ``table_name`` during a run, so a table that could change identity underneath it
    would make generated SQL non-reproducible for a given seed.
    """

    def __init__(self, name: str, columns: Sequence[Column], schema: Optional[str] = None) -> None:
        assert name, "table name must be non-empty"
        self._name = name
        self._columns: tuple[Column, ...] = tuple(columns)
        self._schema = schema

    @property
    def table_name(self) -> str:
        return self._name

    def get_table_name(self) -> str:
        return self._name

    def get_schema(self) -> Optional[str]:
        return self._schema

    def get_column_list(self) -> list[Column]:
        return list(self._columns)

    def get_column(self, name: str) -> Optional[Column]:
        folded = name.casefold()
        return next((c for c in self._columns if c.name.casefold() == folded), None)

    def get_sql_name(self, use_schema_name: bool = False) -> str:
        """How a statement refers to this table.

        Unqualified unless a schema is set *and* the caller asks for it: the harness runs
        base and equivalent in separate databases and refers to one table name, so
        qualifying by default would break the "same query text on both sides" property
        the differential comparison depends on.
        """
        if self._schema is not None and use_schema_name:
            return f"{self._schema}.{self._name}"
        return self._name

    def get_signature(self) -> list[Named[SqlType]]:
        return [Named(alias=c.name, target=c.data_type) for c in self._columns]

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Table):
            return NotImplemented
        return (self._name, self._columns, self._schema) == (other._name, other._columns, other._schema)

    def __hash__(self) -> int:
        return hash((self._name, self._columns, self._schema))

    def __str__(self) -> str:
        cols = ", ".join(str(c) for c in self._columns)
        return f"Table({self.get_sql_name(use_schema_name=True)}: {cols})"

    def __repr__(self) -> str:
        return str(self)
