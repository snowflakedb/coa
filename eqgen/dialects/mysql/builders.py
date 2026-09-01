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

"""MySQL-only builders.

Names start with ``MySql`` — ``tests/boundaries_test.py`` enforces the prefix.

Index Mat:
    MySqlPlainIndexBuilder / Unique / Invisible / Prefix

Other Mat:
    MySqlInnodbTableBuilder
    MySqlJsonPackRoundTripBuilder
"""

from __future__ import annotations

from typing import ClassVar, Optional, Type

from eqgen.builder.constraint_set import Constraint
from eqgen.core.types import (
    BooleanType,
    DoubleType,
    IntegerType,
    NumericType,
    VarcharType,
)
from eqgen.dialects.mysql.ast import (
    MySqlCreateIndex,
    MySqlCreateTableOption,
    MySqlIndexKind,
    MySqlJsonPackRoundTrip,
)
from eqgen.dialects.mysql.types_sql import mysql_cast_type, mysql_type
from eqgen.equivalence.ast import CreateTable, EqNode, QueryNode
from eqgen.equivalence.builders.creates import CreateFromQueryBuilder
from eqgen.equivalence.context import EquivalenceContext

_PK_COLUMN = "c_pk"


def _out_cols(query: QueryNode) -> list[str]:
    return [named.alias for named in query.get_signature()]


def _first_text_col(query: QueryNode) -> Optional[str]:
    for named in query.get_signature():
        if isinstance(named.target, VarcharType):
            return named.alias
    return None


class MySqlIndexBuilderBase(CreateFromQueryBuilder[MySqlCreateIndex]):
    """Shared plumbing: CTAS the query, attach this builder's index shape."""

    KIND: ClassVar[MySqlIndexKind] = MySqlIndexKind.PLAIN

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _spec(self, query: QueryNode, context: EquivalenceContext) -> Optional[tuple[str, int]]:
        """Return ``(target, prefix_length)`` or ``None`` to decline."""
        del context
        out_cols = _out_cols(query)
        if not out_cols:
            return None
        return out_cols[0], 10

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> Optional[MySqlCreateIndex]:
        spec = self._spec(query, context)
        if spec is None:
            return None
        target, prefix_length = spec
        out_cols = _out_cols(query)
        body = CreateTable.build(context.namer, query)
        return MySqlCreateIndex.build(
            context.namer,
            body,
            kind=self.KIND,
            target=target,
            out_cols=out_cols,
            exposed_name=exposed_name,
            prefix_length=prefix_length,
        )


class MySqlPlainIndexBuilder(MySqlIndexBuilderBase):
    """Plain secondary index — plan-only κ."""

    KIND = MySqlIndexKind.PLAIN


class MySqlPrefixIndexBuilder(MySqlIndexBuilderBase):
    """Prefix index on a text column ``col(10)``."""

    KIND = MySqlIndexKind.PREFIX

    def _spec(self, query: QueryNode, context: EquivalenceContext) -> Optional[tuple[str, int]]:
        del context
        target = _first_text_col(query)
        if target is None:
            return None
        return target, 10


class MySqlInvisibleIndexBuilder(MySqlIndexBuilderBase):
    """Index created then marked ``INVISIBLE`` (optimizer ignores it)."""

    KIND = MySqlIndexKind.INVISIBLE


class MySqlUniqueIndexBuilder(CreateFromQueryBuilder[MySqlCreateIndex]):
    """CTAS → ``CREATE UNIQUE INDEX`` on ``c_pk`` → exposing view. Algebra **(Mat)** / Mat⁺."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> Optional[MySqlCreateIndex]:
        out_cols = _out_cols(query)
        if _PK_COLUMN not in out_cols:
            return None
        body = CreateTable.build(context.namer, query)
        return MySqlCreateIndex.build(
            context.namer,
            body,
            kind=MySqlIndexKind.UNIQUE,
            target=_PK_COLUMN,
            out_cols=out_cols,
            exposed_name=exposed_name,
        )


class MySqlInnodbTableBuilder(CreateFromQueryBuilder[MySqlCreateTableOption]):
    """Typed ``CREATE TABLE … ENGINE=InnoDB`` + ``INSERT…SELECT`` + exposing view."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> Optional[MySqlCreateTableOption]:
        signature = query.get_signature()
        if not signature:
            return None
        out_cols = [named.alias for named in signature]
        col_types = [mysql_type(named.target) for named in signature]
        body = CreateTable.build(context.namer, query)
        return MySqlCreateTableOption.build(
            context.namer,
            body,
            out_cols=out_cols,
            col_types=col_types,
            engine="InnoDB",
            exposed_name=exposed_name,
        )


class MySqlJsonPackRoundTripBuilder(CreateFromQueryBuilder[MySqlJsonPackRoundTrip]):
    """CTAS → pack every column into one ``JSON_OBJECT`` value → unpack it back out.

    Numeric types round-trip as bare JSON numbers; ``DATE``/``DATETIME``/text types round-trip as
    quoted JSON strings and need ``JSON_UNQUOTE``. A JSON *null* member is guarded against reading
    back as ``0``/``''`` instead of SQL ``NULL`` (see ``MySqlJsonPackRoundTripObject``).

    Declines on a duplicate column alias: ``JSON_OBJECT`` keeps only the last value for a repeated
    key (``JSON_OBJECT('a',1,'a',2)`` is ``{"a": 2}``), so both unpacked columns would read back
    the second one's value — a builder-introduced wrong answer that would look like an engine bug.
    """

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> Optional[MySqlJsonPackRoundTrip]:
        signature = query.get_signature()
        if not signature:
            return None
        aliases = [named.alias for named in signature]
        if len(set(aliases)) != len(aliases):
            return None
        columns = []
        for named in signature:
            target = named.target
            needs_unquote = not isinstance(target, (BooleanType, DoubleType, IntegerType, NumericType))
            columns.append((named.alias, mysql_cast_type(target), needs_unquote))
        body = CreateTable.build(context.namer, query)
        return MySqlJsonPackRoundTrip.build(
            context.namer,
            body,
            columns=columns,
            exposed_name=exposed_name,
        )
