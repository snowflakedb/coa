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

"""Shared description of one allowed scalar function call shape.

Catalogs live per dialect (:mod:`pg_func`, :mod:`duckdb_func`). Build picks only specs
from the active dialect whose argument families match available columns — so a shape like
``max(boolean)`` simply does not appear in the Postgres list, rather than being rejected at
print time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from eqgen.core.types import (
    BooleanType,
    DoubleType,
    IntegerType,
    NumericType,
    SqlType,
    VarcharType,
)


@dataclass(frozen=True)
class FuncSpec:
    """One concrete call shape: SQL name, argument type families, result.

    *result* is either the string ``\"arg0\"`` (result type follows the first argument) or a
    :class:`SqlType` *class* used to build a fresh instance (``IntegerType``, ``BooleanType``,
    ``VarcharType``, …).
    """

    sql_name: str
    arg_families: tuple[type, ...]
    result: str | type

    def arity(self) -> int:
        return len(self.arg_families)

    def matches(self, arg_types: Sequence[SqlType]) -> bool:
        if len(arg_types) != len(self.arg_families):
            return False
        return all(isinstance(t, family) for t, family in zip(arg_types, self.arg_families))

    def result_type_for(self, arg_types: Sequence[SqlType]) -> SqlType:
        if self.result == "arg0":
            return arg_types[0]
        if self.result is IntegerType:
            return IntegerType()
        if self.result is BooleanType:
            return BooleanType()
        if self.result is VarcharType:
            return VarcharType()
        if self.result is DoubleType:
            return DoubleType()
        if self.result is NumericType:
            return arg_types[0] if arg_types else NumericType()
        raise TypeError(f"unsupported FuncSpec.result {self.result!r}")


def catalog_for(dialect: str) -> tuple[FuncSpec, ...]:
    """Function allowlist for *dialect*."""
    if dialect == "postgres" or dialect == "cratedb":
        from eqgen.generators.typed_predicate.pg_func import FUNCS

        return FUNCS
    if dialect == "duckdb":
        from eqgen.generators.typed_predicate.duckdb_func import FUNCS

        return FUNCS
    if dialect == "sqlite":
        from eqgen.generators.typed_predicate.sqlite_func import FUNCS

        return FUNCS
    if dialect == "clickhouse":
        from eqgen.generators.typed_predicate.clickhouse_func import FUNCS

        return FUNCS
    if dialect in ("mysql", "mariadb", "tidb", "dolt"):
        from eqgen.generators.typed_predicate.mysql_func import FUNCS

        return FUNCS
    raise ValueError(f"no function catalog for dialect {dialect!r}")
