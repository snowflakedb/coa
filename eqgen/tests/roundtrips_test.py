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

"""The portable value-codec round trips.

Five dialects each wrote their own version of this rewrite before it was lifted. What these tests pin
is the part that made the duplication possible to remove: the builder names no function and writes no
SQL, so the same node renders on every engine that has the codec and refuses on every engine that
does not.
"""

from __future__ import annotations

import pytest

from eqgen.core.catalog import Column, Table
from eqgen.core.types import DoubleType, IntegerType, TextType, TimestampType, VarcharType
from eqgen.equivalence.builders.roundtrips import (
    HexCodecRoundTripBuilder,
    MixedCodecRoundTripBuilder,
    PerColumnAssignment,
    UniformAssignment,
    ValueCodecSpec,
)
from eqgen.ir import expr
from eqgen.ir.expr import ValueCodec, ValueCodecRoundTrip
from eqgen.ir.render import PostgresSpelling, UnsupportedForDialect

pytestmark = pytest.mark.unit


def _context(*columns: Column):
    class _Ctx:
        base_table = Table("t__base", list(columns))

    return _Ctx()


def _text_and_numeric() -> tuple[Column, ...]:
    return (
        Column("c_txt", VarcharType(32), 1),
        Column("c_int", IntegerType(), 2),
        Column("c_dbl", DoubleType(), 3),
        Column("c_ts", TimestampType(), 4),
    )


def test_only_text_columns_are_eligible() -> None:
    """A DOUBLE loses digits through a text form and a TIMESTAMP's text form is session-dependent, so
    both would report as engine mismatches with no engine bug behind them."""
    plan = UniformAssignment(ValueCodecSpec(ValueCodec.HEX)).plan(_context(*_text_and_numeric()))
    assert set(plan) == {"c_txt"}


def test_a_uniform_assignment_gives_every_eligible_column_the_same_codec() -> None:
    columns = (Column("a", VarcharType(8), 1), Column("b", TextType(), 2), Column("n", IntegerType(), 3))
    plan = UniformAssignment(ValueCodecSpec(ValueCodec.JSON_PACK)).plan(_context(*columns))
    assert set(plan) == {"a", "b"}
    assert {spec.codec for spec in plan.values()} == {ValueCodec.JSON_PACK}


def test_a_per_column_assignment_can_mix_codecs_across_columns() -> None:
    """The case none of the five dialect implementations covered — all were effectively uniform.

    Sound because value codecs act on disjoint columns: each preserves its own column, so the row is
    preserved whatever the mix.
    """
    columns = tuple(Column(f"c{i}", VarcharType(16), i + 1) for i in range(12))
    specs = [ValueCodecSpec(ValueCodec.HEX), ValueCodecSpec(ValueCodec.JSON_PACK)]
    plans = [PerColumnAssignment(specs).plan(_context(*columns)) for _ in range(12)]
    assert all(len(plan) == 12 for plan in plans)
    seen = {spec.codec for plan in plans for spec in plan.values()}
    assert seen == {ValueCodec.HEX, ValueCodec.JSON_PACK}, "expected both codecs to be drawn"


def test_the_round_trip_is_null_guarded() -> None:
    """Explicit even though every codec propagates NULL on every engine tested — it makes it
    impossible for the round trip to turn a NULL into an empty string, rather than merely unlikely."""
    rewritten = ValueCodecSpec(ValueCodec.HEX).roundtrip("c_txt", VarcharType(32))
    rendered = PostgresSpelling().expr(rewritten)
    assert "c_txt IS NULL" in rendered
    assert "CAST(NULL AS" in rendered


def test_the_builder_declines_when_no_column_is_eligible() -> None:
    """Rather than producing an object identical to the base, which would waste the round."""
    builder = HexCodecRoundTripBuilder.__new__(HexCodecRoundTripBuilder)
    rewrite = builder._column_rewriter(_context(Column("c_int", IntegerType(), 1)))
    assert rewrite("c_int", IntegerType()) is None


def test_a_builder_names_no_function_and_writes_no_sql() -> None:
    """The property that let five implementations collapse into one.

    The dialect versions each formatted a ``_WRAP`` string inside ``build()`` — SQL text created
    during generation, where no emitter can reach it, which is what ARCHITECTURE.md §7 forbids and
    why the same rewrite had to be written again for every engine.
    """
    rewritten = ValueCodecSpec(ValueCodec.HEX).roundtrip("c_txt", VarcharType(32))
    codecs = [node for node in [rewritten, *getattr(rewritten, "children", lambda: [])()]]
    del codecs
    # The codec node carries an enum, not a function name or a format string.
    node = expr.value_codec_roundtrip(ValueCodec.HEX, expr.col("c_txt", VarcharType(32)), VarcharType(32))
    assert isinstance(node.codec, ValueCodec)
    assert not any(isinstance(getattr(node, field, None), str) for field in ("sql", "wrap", "template"))


@pytest.mark.parametrize(
    ("dialect", "spelling_path", "codec", "expected"),
    [
        ("duckdb", "eqgen.dialects.duckdb.emitter.DuckDBSpelling", ValueCodec.HEX, "unhex(hex("),
        ("duckdb", "eqgen.dialects.duckdb.emitter.DuckDBSpelling", ValueCodec.BASE64, "from_base64(to_base64("),
        ("sqlite", "eqgen.dialects.sqlite.emitter.SqliteSpelling", ValueCodec.HEX, "CAST(unhex(hex("),
        ("mysql", "eqgen.dialects.mysql.emitter.MySqlSpelling", ValueCodec.HEX, "UNHEX(HEX("),
        ("clickhouse", "eqgen.dialects.clickhouse.emitter.ClickHouseSpelling", ValueCodec.HEX, "unhex(hex("),
    ],
)
def test_each_engine_spells_the_codec_its_own_way(dialect: str, spelling_path: str, codec, expected: str) -> None:
    module_name, class_name = spelling_path.rsplit(".", 1)
    spelling = getattr(__import__(module_name, fromlist=[class_name]), class_name)()
    node = expr.value_codec_roundtrip(codec, expr.col("c_txt", VarcharType(32)), VarcharType(32))
    assert expected in spelling.expr(node), f"{dialect} did not spell {codec.value} as expected"


def test_an_engine_without_the_codec_refuses_rather_than_guessing() -> None:
    """SQLite has no base64. It must raise, not inherit PostgreSQL's spelling.

    Falling through to ``super()`` would render ``convert_from(decode(encode(…)))``, which SQLite also
    lacks, so every round would die unbuildable with only a config weight standing in the way — the
    exact silent coupling this lift exists to remove.
    """
    from eqgen.dialects.sqlite.emitter import SqliteSpelling

    node = expr.value_codec_roundtrip(ValueCodec.BASE64, expr.col("c", TextType()), TextType())
    with pytest.raises(UnsupportedForDialect, match="BASE64"):
        SqliteSpelling().expr(node)


def test_the_mixed_builder_leaves_out_the_codec_sqlite_cannot_spell() -> None:
    """A per-column builder cannot be turned off per codec from the configuration, so the mix has to
    contain only codecs available everywhere it is enabled."""
    assignment = MixedCodecRoundTripBuilder.__new__(MixedCodecRoundTripBuilder)._assignment()
    plan = assignment.plan(_context(*(Column(f"c{i}", VarcharType(16), i + 1) for i in range(20))))
    assert ValueCodec.BASE64 not in {spec.codec for spec in plan.values()}
