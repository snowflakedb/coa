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

"""The types a column can have. Data only — nothing here writes SQL.

Twelve kinds, named the way PostgreSQL names them. A type says what it *is* — kind, precision, scale,
whether it can be ordered — and never how to write it down::

    NumericType(10, 2)  ->  "NUMERIC(10, 2)"   asked of the PostgreSQL spelling
                        ->  "DECIMAL(10, 2)"   asked of the DuckDB spelling

Writing the name is a dialect's job (``type_sql`` in ``ir/render.py``). Keeping the two apart is what
lets one generator serve several engines.

Two places rely on the class hierarchy, so it follows SQL's own families:

* :class:`IntegerType` is a :class:`NumericType` with scale 0, so a builder looking for
  an integer-valued key (``isinstance(t, NumericType) and t.get_scale() in (None, 0)``)
  finds both plain integers and zero-scale decimals, and does *not* find a scaled one —
  ``MOD(12.34, 2)`` is ``0.34``, which is in neither the even nor the odd branch.
* :class:`CharType` and :class:`TextType` are :class:`VarcharType`, so a string-family
  check catches all three.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from enum import Flag, StrEnum, auto
from typing import ClassVar, Optional


class TypeKind(StrEnum):
    """The kinds a :class:`SqlType` can be.

    PostgreSQL names for what a base table needs. Array/map, geospatial, vector,
    interval and time-zone-aware timestamps stay out: no shipped builder produces them.
    ``JSONB`` / ``UUID`` / ``INT4RANGE`` exist for the Postgres rich catalog + index Mats;
    other dialects omit them from ``catalog_type_pool``.
    """

    INTEGER = "INTEGER"
    NUMERIC = "NUMERIC"
    DOUBLE = "DOUBLE"
    VARCHAR = "VARCHAR"
    CHAR = "CHAR"
    TEXT = "TEXT"
    BOOLEAN = "BOOLEAN"
    DATE = "DATE"
    TIMESTAMP = "TIMESTAMP"
    JSONB = "JSONB"
    UUID = "UUID"
    INT4RANGE = "INT4RANGE"


class TypeProperty(Flag):
    """Facts about a type that a builder needs in order to decline.

    Flags rather than booleans because the question a builder asks is open-ended ("can I order
    by this?", "can I group by this?", "can I aggregate it?"), and the answers do not move
    together: PostgreSQL's ``jsonb`` groups and orders fine but has no ``MAX``. A builder that
    consults a property degrades gracefully; one that assumes emits invalid SQL.
    """

    NONE = 0
    #: ``a >= b`` is legal for this type. Read by the always-true ``CASE`` builder's fallback
    #: (``c >= c``, NULL when ``c`` is NULL), which needs an ordering to exist, and by the window
    #: rewrite when it writes an ``ORDER BY``.
    ORDERABLE = auto()
    #: ``GROUP BY a``, ``SELECT DISTINCT a`` and ``PARTITION BY a`` are legal for this type. Read by
    #: the window rewrite (which partitions by the column) and by the collapse-duplicates rewrite
    #: (which puts every column in a ``DISTINCT``).
    GROUPABLE = auto()
    #: ``MAX(a)`` / ``MIN(a)`` exist for this type. Deliberately separate from ``GROUPABLE``:
    #: grouping needs only an equality operator, while the aggregates need a function that a
    #: dialect may simply not define — PostgreSQL groups ``jsonb`` happily and has no ``MAX(jsonb)``.
    #: Conflating the two cost over half of all Postgres rounds' worth of eligible builders.
    AGGREGATABLE = auto()


@dataclass(frozen=True)
class SqlType(abc.ABC):
    """A column type: kind plus optional precision/scale. Frozen and value-compared.

    ``precision``/``scale`` are ``None`` for types that do not carry them, and may be
    ``None`` on :class:`NumericType` to mean "unparameterised" (bare ``NUMERIC``).
    """

    #: The kind this class represents. Set by each concrete subclass.
    kind: ClassVar[TypeKind]

    precision: Optional[int] = None
    scale: Optional[int] = None

    def get_type_kind(self) -> TypeKind:
        return self.kind

    def get_precision(self) -> Optional[int]:
        return self.precision

    def get_scale(self) -> Optional[int]:
        return self.scale

    def get_properties(self) -> TypeProperty:
        """Every type in this vocabulary can be ordered, grouped and aggregated. A kind added
        later that cannot be — JSON, geometry — overrides this and the builders that consult it
        then decline."""
        return TypeProperty.ORDERABLE | TypeProperty.GROUPABLE | TypeProperty.AGGREGATABLE

    def __str__(self) -> str:
        if self.precision is None:
            return str(self.kind)
        if self.scale is None:
            return f"{self.kind}({self.precision})"
        return f"{self.kind}({self.precision}, {self.scale})"

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.precision!r}, {self.scale!r})"


# ---------------------------------------------------------------------------
# Numeric family
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NumericType(SqlType):
    """``NUMERIC`` / ``NUMERIC(p, s)`` — exact decimal.

    Scale 0 (or unset) means whole-numbered, which is what the row-splitting
    partitioning builders require of a key.
    """

    kind: ClassVar[TypeKind] = TypeKind.NUMERIC


@dataclass(frozen=True)
class IntegerType(NumericType):
    """``INTEGER``. A :class:`NumericType` of scale 0, so integer-key checks find it."""

    kind: ClassVar[TypeKind] = TypeKind.INTEGER

    def get_scale(self) -> Optional[int]:
        return 0


@dataclass(frozen=True)
class DoubleType(SqlType):
    """``DOUBLE`` — inexact binary floating point.

    Inexactness is not a detail here: addition is not associative, so ``SUM``/``AVG``
    over this type can differ in the last bit between two plan shapes that are otherwise
    equivalent. The example generator therefore never aggregates over it, and a workload
    source that does will produce mismatches that are float arithmetic rather than engine
    bugs.
    """

    kind: ClassVar[TypeKind] = TypeKind.DOUBLE


# ---------------------------------------------------------------------------
# String family
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VarcharType(SqlType):
    """``VARCHAR`` / ``VARCHAR(n)``."""

    kind: ClassVar[TypeKind] = TypeKind.VARCHAR


@dataclass(frozen=True)
class CharType(VarcharType):
    """``CHAR(n)`` — blank-padded.

    The padding matters when rows are compared ignoring order: under a ``PAD SPACE`` collation two
    values differing only in trailing spaces compare equal, so ``DISTINCT`` may keep a
    different representative on each side of a comparison without either being wrong.
    """

    kind: ClassVar[TypeKind] = TypeKind.CHAR


@dataclass(frozen=True)
class TextType(VarcharType):
    """``TEXT`` — unbounded string.

    Kept distinct from :class:`VarcharType` even where an engine treats them as the same
    type, because "same type, two spellings" is itself worth exercising: it is exactly
    where a dialect's type mapping can disagree with itself.
    """

    kind: ClassVar[TypeKind] = TypeKind.TEXT


# ---------------------------------------------------------------------------
# Boolean and temporal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BooleanType(SqlType):
    """``BOOLEAN`` — and therefore three-valued: TRUE, FALSE, NULL.

    The third value is why splitting rows on a predicate needs *three* branches rather than a
    predicate and its negation.
    """

    kind: ClassVar[TypeKind] = TypeKind.BOOLEAN


@dataclass(frozen=True)
class DateType(SqlType):
    """``DATE``."""

    kind: ClassVar[TypeKind] = TypeKind.DATE


@dataclass(frozen=True)
class TimestampType(SqlType):
    """``TIMESTAMP`` — without time zone.

    There is deliberately no time-zone-aware counterpart: a session time zone is state
    the two sides of a differential comparison would have to agree on, and no shipped
    builder needs one.
    """

    kind: ClassVar[TypeKind] = TypeKind.TIMESTAMP


# ---------------------------------------------------------------------------
# PostgreSQL-native (other dialects omit from catalog_type_pool)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JsonbType(SqlType):
    """``JSONB`` — binary JSON. GIN-indexable; containment / path operators.

    GROUPABLE but not AGGREGATABLE: ``jsonb`` has btree and hash opclasses, so ``GROUP BY`` /
    ``DISTINCT`` / ``PARTITION BY`` are all legal, but PostgreSQL defines no ``MAX(jsonb)`` /
    ``MIN(jsonb)`` and several Mats aggregate every column they group.

    Not ORDERABLE, though ``jsonb`` does have a total order: the predicate generators read that
    flag to decide whether to emit ``<`` / ``>`` against a generated literal, and JSON comparison
    ordering is a thin edge this vocabulary has no need to cover.
    """

    kind: ClassVar[TypeKind] = TypeKind.JSONB

    def get_properties(self) -> TypeProperty:
        return TypeProperty.GROUPABLE


@dataclass(frozen=True)
class UuidType(SqlType):
    """``UUID``."""

    kind: ClassVar[TypeKind] = TypeKind.UUID


@dataclass(frozen=True)
class Int4RangeType(SqlType):
    """``INT4RANGE`` — integer range. GiST-indexable; overlap / containment operators.

    GROUPABLE but not AGGREGATABLE, for the same reason as :class:`JsonbType`: range types carry
    btree and hash opclasses, so grouping works, but ``MAX``/``MIN`` over a range is not defined.
    Not ORDERABLE either — range comparison ordering is a thin edge we do not need for GiST
    coverage, and the predicate generators would start emitting it.
    """

    kind: ClassVar[TypeKind] = TypeKind.INT4RANGE

    def get_properties(self) -> TypeProperty:
        return TypeProperty.GROUPABLE


#: Every concrete type, for tests and for a catalog that wants to span the vocabulary.
ALL_TYPES: tuple[type[SqlType], ...] = (
    IntegerType,
    NumericType,
    DoubleType,
    VarcharType,
    CharType,
    TextType,
    BooleanType,
    DateType,
    TimestampType,
    JsonbType,
    UuidType,
    Int4RangeType,
)
