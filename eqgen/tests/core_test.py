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

"""Step-1 gate: the vocabulary constructs, and the GCL config layer resolves.

Small on purpose. These are the few facts about the type vocabulary that something else relies on — the ones a
builder silently depends on — plus proof that the config language is wired up end to end.
Anything richer belongs with the code that has opinions about types, not here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eqgen.config.gcl_compat import load
from eqgen.config.settings import EquivalenceGeneratorV3Settings
from eqgen.config.weighted import Choices, Weighted, weighted_shuffle
from eqgen.core.catalog import Column, Named, Table
from eqgen.core.statement import Statement
from eqgen.core.types import (
    ALL_TYPES,
    BooleanType,
    CharType,
    IntegerType,
    NumericType,
    SqlType,
    TextType,
    TimestampType,
    TypeKind,
    TypeProperty,
    VarcharType,
)

pytestmark = pytest.mark.unit

_GCL_DIR = Path(__file__).resolve().parent.parent / "config" / "gcl"


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("type_cls", ALL_TYPES, ids=lambda t: t.__name__)
def test_every_type_constructs_and_reports_its_kind(type_cls: type[SqlType]) -> None:
    instance = type_cls()
    assert isinstance(instance.get_type_kind(), TypeKind)
    assert str(instance) == str(instance.get_type_kind())


def test_no_type_kind_is_snowflake_specific() -> None:
    """The vocabulary is PostgreSQL-named. A kind that only Snowflake has would mean the
    type layer had started making claims a PostgreSQL run cannot honour."""
    assert {k.value for k in TypeKind} == {
        "INTEGER",
        "NUMERIC",
        "DOUBLE",
        "VARCHAR",
        "CHAR",
        "TEXT",
        "BOOLEAN",
        "DATE",
        "TIMESTAMP",
        "JSONB",
        "UUID",
        "INT4RANGE",
    }


def test_integer_is_a_zero_scale_numeric() -> None:
    """The whole-number check the row-splitting builders rely on:
    ``isinstance(t, NumericType) and t.get_scale() in (None, 0)``."""
    assert isinstance(IntegerType(), NumericType)
    assert IntegerType().get_scale() == 0
    assert NumericType().get_scale() is None  # bare NUMERIC is integer-valued
    assert NumericType(38, 0).get_scale() == 0


def test_scaled_decimal_is_not_an_integer_key() -> None:
    """``MOD(x, 2)`` on a scaled decimal yields a fractional remainder matching neither
    parity branch, so such a row would fall out of *both* and break equivalence."""
    assert NumericType(10, 2).get_scale() == 2


def test_char_and_text_are_varchars() -> None:
    """So a string-family check catches all three spellings."""
    assert isinstance(CharType(), VarcharType)
    assert isinstance(TextType(), VarcharType)


def test_types_are_value_compared() -> None:
    assert NumericType(10, 2) == NumericType(10, 2)
    assert NumericType(10, 2) != NumericType(10, 3)
    assert IntegerType() != NumericType()  # distinct classes are distinct types
    assert len({NumericType(10, 2), NumericType(10, 2)}) == 1


def test_types_are_frozen() -> None:
    with pytest.raises(Exception):
        NumericType(10, 2).precision = 5  # type: ignore[misc]


#: The only types allowed to lack a property, and which one. Both are PostgreSQL-native and both
#: group fine — they simply have no ``MAX``/``MIN``, and this vocabulary declines to order them
#: (see the class docstrings). An entry here is a deliberate opt-out; anything else is a bug.
_PROPERTY_OPT_OUTS: dict[str, TypeProperty] = {
    "JsonbType": TypeProperty.GROUPABLE,
    "Int4RangeType": TypeProperty.GROUPABLE,
}


@pytest.mark.parametrize("type_cls", ALL_TYPES, ids=lambda t: t.__name__)
def test_every_type_declares_the_properties_builders_expect(type_cls: type[SqlType]) -> None:
    """Builders decline on a missing property, so a type that quietly drops one silently removes
    itself from every builder that consults it — the failure mode is lost coverage, not an error.

    ORDERABLE backs the always-true ``CASE`` fallback (``c >= c``) and any ``ORDER BY``; GROUPABLE
    backs ``GROUP BY``/``DISTINCT``/``PARTITION BY``; AGGREGATABLE backs ``MAX``/``MIN``. Asserting
    equality against an explicit opt-out table is what makes this test able to fail.
    """
    expected = _PROPERTY_OPT_OUTS.get(
        type_cls.__name__,
        TypeProperty.ORDERABLE | TypeProperty.GROUPABLE | TypeProperty.AGGREGATABLE,
    )
    assert type_cls().get_properties() == expected


def test_the_property_opt_out_table_only_names_real_types() -> None:
    """Guards the table above against a rename: a stale key would silently stop applying."""
    assert set(_PROPERTY_OPT_OUTS) <= {t.__name__ for t in ALL_TYPES}


def test_parameterised_types_render_their_parameters_in_str() -> None:
    assert str(NumericType(10, 2)) == "NUMERIC(10, 2)"
    assert str(VarcharType(20)) == "VARCHAR(20)"


def test_types_carry_no_sql_rendering() -> None:
    """Rendering a type name belongs to a dialect, because the same logical type is
    ``NUMERIC(10, 2)`` in PostgreSQL and ``DECIMAL(10, 2)`` in DuckDB."""
    for name in ("sql_string", "iceberg_sql_string", "type_sql"):
        assert not hasattr(NumericType(10, 2), name), f"{name} must live on the dialect, not the type"


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def _table() -> Table:
    return Table(
        "t",
        [
            Column("c_int", IntegerType(), 1),
            Column("c_dec", NumericType(10, 2), 2),
            Column("c_txt", VarcharType(), 3),
            Column("c_flag", BooleanType(), 4),
            Column("c_ts", TimestampType(), 5),
        ],
    )


def test_table_signature_is_ordered_name_type_pairs() -> None:
    signature = _table().get_signature()
    assert signature[0] == Named(alias="c_int", target=IntegerType())
    assert [n.alias for n in signature] == ["c_int", "c_dec", "c_txt", "c_flag", "c_ts"]


def test_table_sql_name_is_unqualified_by_default() -> None:
    """Base and equivalent live in separate databases and are queried by one name, so
    qualifying by default would break the same-text-on-both-sides property."""
    schemaful = Table("t", _table().get_column_list(), schema="s")
    assert schemaful.get_sql_name() == "t"
    assert schemaful.get_sql_name(use_schema_name=True) == "s.t"
    assert _table().get_sql_name(use_schema_name=True) == "t"  # no schema to qualify with


def test_table_column_lookup_is_case_insensitive() -> None:
    assert _table().get_column("C_INT") is not None
    assert _table().get_column("nope") is None


def test_table_column_list_cannot_be_mutated_through_the_accessor() -> None:
    table = _table()
    table.get_column_list().clear()
    assert len(table.get_column_list()) == 5


def test_column_requires_a_name() -> None:
    with pytest.raises(AssertionError):
        Column("", IntegerType())


# ---------------------------------------------------------------------------
# Statement
# ---------------------------------------------------------------------------


def test_statement_carries_text_and_nothing_else() -> None:
    assert Statement("SELECT 1").statement_text == "SELECT 1"
    assert str(Statement("SELECT 1")) == "SELECT 1"
    assert Statement("-- @marker\nSELECT 1").startswith("-- @marker\n")


def test_statement_rejects_empty_text() -> None:
    with pytest.raises(AssertionError):
        Statement("")


# ---------------------------------------------------------------------------
# Config: GCL resolves, and weighted choice works
# ---------------------------------------------------------------------------


def _settings() -> EquivalenceGeneratorV3Settings:
    model = load(str(_GCL_DIR / "equivalence_generator_v3.gcl"))
    return EquivalenceGeneratorV3Settings({key: model[key] for key in model.exportable_keys()})


def test_gcl_resolves_into_the_typed_settings_tuple() -> None:
    settings = _settings()
    assert settings.builder_settings.max_depth > 0
    assert settings.builder_weights.options, "builder weights must not be empty"


def test_gcl_includes_resolve_relative_to_the_package() -> None:
    """``equivalence_generator_v3.gcl`` includes ``weighted.gcl`` and
    ``builder_settings.gcl``; all three must travel together."""
    for name in ("equivalence_generator_v3.gcl", "weighted.gcl", "builder_settings.gcl"):
        assert (_GCL_DIR / name).is_file(), name


def test_weighted_knobs_arrive_as_choices_the_builders_can_draw_from() -> None:
    """Builders call ``.choose_one()`` at the use site, so the knobs must stay ``Choices``
    rather than being flattened during resolution."""
    settings = _settings()
    for knob in (settings.builder_weights, settings.root_builder_weights, settings.key_selection_weights):
        assert isinstance(knob, Choices)
        assert knob.choose_one() in knob.values


def test_weighted_shuffle_only_yields_nonzero_weights() -> None:
    """Weight 0 means "excluded" — that is the mechanism a dialect uses to restrict the
    builder set, so it has to hold at the shuffle level."""
    items = ["a", "b", "c"]
    weights = {"a": 1.0, "b": 0.0, "c": 2.0}
    for _ in range(20):
        assert "b" not in weighted_shuffle(items, lambda i: weights[i])


def test_choices_restrict_to_narrows_the_domain() -> None:
    options = [Weighted("x", 1, str), Weighted("y", 1, str)]
    choices = Choices(options, str)
    assert set(choices.restrict_to("x").values) == {"x"}
