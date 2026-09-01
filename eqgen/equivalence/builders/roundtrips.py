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

"""Codec round trips — ``decode(encode(c)) == c``, one builder for every codec and engine.

Five dialects each wrote their own version of this before it was lifted: DuckDB, ClickHouse, SQLite,
MySQL and CrateDB all had a JSON-pack round trip, three had hex, two had base64. The rewrite is the
same on all of them — put a value through a lossless encoding and take it back out — so only the
*spelling* was ever dialect-specific, and spelling belongs in an emitter. PostgreSQL, the most
complete configuration in the project, had none of them at all.

The shape follows the generator this was extracted from (``builders/roundtrips.py`` there):

* :class:`ValueCodecSpec` — a codec plus which column types it round-trips losslessly;
* :class:`ColumnCodecAssignment` — which codec each column gets, if any;
* :class:`CodecRoundTripQueryBuilder` — one build, driven by an assignment.

:class:`UniformAssignment` gives every eligible column the same codec (the classic per-codec round
trip); :class:`PerColumnAssignment` draws independently per column, which is sound because value
codecs act on disjoint columns, so any mix of individually-valid codecs is still row-preserving.

The engine is asked for the SQL through :class:`~eqgen.ir.expr.ValueCodecRoundTrip`, so a builder here
names no function and writes no text — the failure mode the dialect versions had, each of them
formatting a ``_WRAP`` string inside ``build()`` where no emitter could reach it.
"""

from __future__ import annotations

import abc
import random
from typing import Callable, Optional, Sequence

from eqgen.core.types import CharType, SqlType, TextType, VarcharType
from eqgen.equivalence.builders.base import ColumnRewriteQueryBuilder
from eqgen.equivalence.context import EquivalenceContext
from eqgen.ir import expr
from eqgen.ir.expr import ExpressionNode, ValueCodec

#: Types a codec round trip is offered. Text only, deliberately: every codec here goes through a
#: character or binary representation, and the round trip is only lossless if the value's text form
#: is exact. Numeric and temporal columns *look* eligible and are not — a DOUBLE loses digits and a
#: TIMESTAMP's text form is session-format-dependent, so both would report as engine mismatches with
#: no engine bug behind them. Every one of the five dialect implementations reached the same
#: conclusion independently.
_TEXT_TYPES = (VarcharType, TextType, CharType)


class ValueCodecSpec:
    """A codec, plus the eligibility gate saying which columns survive it.

    Not an ABC with a method per codec: the round-trip *expression* is the engine's business (see
    :class:`~eqgen.ir.expr.ValueCodecRoundTrip`), so all that is left here is which codec to ask for
    and which columns to ask it about.
    """

    def __init__(self, codec: ValueCodec, eligible_types: tuple[type, ...] = _TEXT_TYPES) -> None:
        self.codec = codec
        self._eligible_types = eligible_types

    def eligible(self, data_type: SqlType) -> bool:
        """Whether a column of this type round-trips losslessly through the codec."""
        return isinstance(data_type, self._eligible_types)

    def roundtrip(self, name: str, data_type: SqlType) -> ExpressionNode:
        """``decode(encode(c))`` for one column, guarded against NULL.

        The guard is explicit even though every codec propagates NULL on every engine tested. It is
        cheap and it makes it *impossible* for a round trip to turn a NULL into an empty string —
        which is the difference between a rewrite that is row-preserving by construction and one that
        is row-preserving as long as several engines keep agreeing about NULL handling in string
        functions. The DuckDB version had this guard and a test pinning it; keeping it here is why
        that test still passes.
        """
        column = expr.col(name, data_type)
        return expr.case_when(
            expr.is_null(column),
            expr.typed_null(data_type),
            expr.value_codec_roundtrip(self.codec, column, data_type),
            data_type,
        )


class ColumnCodecAssignment(abc.ABC):
    """Chooses which codec, if any, each column round-trips through.

    Called once per build. A column the plan omits passes through unchanged.
    """

    @abc.abstractmethod
    def plan(self, context: EquivalenceContext) -> dict[str, ValueCodecSpec]:
        """Map ``column name -> codec`` for the columns to rewrite this build."""


class UniformAssignment(ColumnCodecAssignment):
    """One codec, applied to every column eligible for it — the classic per-codec round trip."""

    def __init__(self, spec: ValueCodecSpec) -> None:
        self._spec = spec

    def plan(self, context: EquivalenceContext) -> dict[str, ValueCodecSpec]:
        return {
            column.get_column_name(): self._spec
            for column in context.base_table.get_column_list()
            if self._spec.eligible(column.get_data_type())
        }


class PerColumnAssignment(ColumnCodecAssignment):
    """Draw a codec per column, independently, from that column's eligible set.

    Sound because value codecs act on **disjoint** columns: each one preserves its own column's
    value, so any mix of individually-valid codecs preserves the row. This is the case no dialect
    implementation covered — all five were effectively uniform — so "several different codecs in one
    query" was untested everywhere until now.
    """

    def __init__(self, specs: Sequence[ValueCodecSpec]) -> None:
        self._specs = tuple(specs)

    def plan(self, context: EquivalenceContext) -> dict[str, ValueCodecSpec]:
        assignment: dict[str, ValueCodecSpec] = {}
        for column in context.base_table.get_column_list():
            candidates = [spec for spec in self._specs if spec.eligible(column.get_data_type())]
            if candidates:
                assignment[column.get_column_name()] = random.choice(candidates)
        return assignment


class CodecRoundTripQueryBuilder(ColumnRewriteQueryBuilder, abc.ABC):
    """Rewrite each column through the codec its :meth:`_assignment` gives it.

    Declines when no column was rewritten, inherited from
    :class:`~eqgen.equivalence.builders.base.ColumnRewriteQueryBuilder` — which is what happens on a
    catalog with no text column, and is the honest outcome rather than an object identical to the
    base.
    """

    @abc.abstractmethod
    def _assignment(self) -> ColumnCodecAssignment:
        """The column-to-codec strategy for this builder."""

    def _column_rewriter(self, context: EquivalenceContext) -> Callable[[str, SqlType], Optional[ExpressionNode]]:
        plan = self._assignment().plan(context)

        def rewrite(name: str, data_type: SqlType) -> Optional[ExpressionNode]:
            spec = plan.get(name)
            return None if spec is None else spec.roundtrip(name, data_type)

        return rewrite


class HexCodecRoundTripBuilder(CodecRoundTripQueryBuilder):
    """Text through a hex encode/decode pair — **(Rewrite)**.

    The one codec confirmed present on every engine checked (DuckDB, SQLite, MySQL, ClickHouse).
    """

    def _assignment(self) -> ColumnCodecAssignment:
        return UniformAssignment(ValueCodecSpec(ValueCodec.HEX))


class Base64CodecRoundTripBuilder(CodecRoundTripQueryBuilder):
    """Text through a base64 encode/decode pair — **(Rewrite)**.

    SQLite has no base64, so it leaves this at weight 0 — a genuine absence, not a spelling.
    """

    def _assignment(self) -> ColumnCodecAssignment:
        return UniformAssignment(ValueCodecSpec(ValueCodec.BASE64))


class JsonPackCodecRoundTripBuilder(CodecRoundTripQueryBuilder):
    """Text through a one-key JSON object and back out — **(Rewrite)**."""

    def _assignment(self) -> ColumnCodecAssignment:
        return UniformAssignment(ValueCodecSpec(ValueCodec.JSON_PACK))


class MixedCodecRoundTripBuilder(CodecRoundTripQueryBuilder):
    """A codec drawn per column, so one query mixes several — **(Rewrite)**.

    Base64 is left out of the mix: SQLite cannot spell it, and a builder that decides per column
    cannot be turned off per codec from the configuration. Hex and JSON pack are available
    everywhere tested, so this one builder is portable as it stands.
    """

    def _assignment(self) -> ColumnCodecAssignment:
        return PerColumnAssignment(
            [ValueCodecSpec(ValueCodec.HEX), ValueCodecSpec(ValueCodec.JSON_PACK)]
        )
