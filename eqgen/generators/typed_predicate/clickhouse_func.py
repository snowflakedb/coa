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

"""ClickHouse scalar functions allowed in typed predicates.

Spellings follow ClickHouse (``endsWith`` / ``startsWith``, ``LENGTH``). Keep the set
conservative — only shapes verified against 26.8.

Prefer ``IntegerType`` over bare ``NumericType`` for multi-arg numeric funcs: ClickHouse
rejects Int64↔Float64 / Decimal↔Float64 common types (Code 386) when a Decimal peer or
floaty literal sneaks into ``least`` / ``greatest`` / ``IN``.
"""

from __future__ import annotations

from eqgen.core.types import DoubleType, IntegerType, VarcharType
from eqgen.generators.typed_predicate.func_spec import FuncSpec

FUNCS: tuple[FuncSpec, ...] = (
    FuncSpec("ABS", (IntegerType,), "arg0"),
    FuncSpec("ABS", (DoubleType,), "arg0"),
    FuncSpec("CEIL", (DoubleType,), DoubleType),
    FuncSpec("FLOOR", (DoubleType,), DoubleType),
    FuncSpec("SIGN", (IntegerType,), "arg0"),
    FuncSpec("SIGN", (DoubleType,), "arg0"),
    FuncSpec("LENGTH", (VarcharType,), IntegerType),
    FuncSpec("UPPER", (VarcharType,), VarcharType),
    FuncSpec("LOWER", (VarcharType,), VarcharType),
    FuncSpec("endsWith", (VarcharType, VarcharType), IntegerType),
    FuncSpec("startsWith", (VarcharType, VarcharType), IntegerType),
    FuncSpec("position", (VarcharType, VarcharType), IntegerType),
    FuncSpec("substring", (VarcharType, IntegerType), VarcharType),
    FuncSpec("GREATEST", (IntegerType, IntegerType), "arg0"),
    FuncSpec("GREATEST", (DoubleType, DoubleType), "arg0"),
    FuncSpec("GREATEST", (VarcharType, VarcharType), VarcharType),
    FuncSpec("LEAST", (IntegerType, IntegerType), "arg0"),
    FuncSpec("LEAST", (DoubleType, DoubleType), "arg0"),
    FuncSpec("LEAST", (VarcharType, VarcharType), VarcharType),
    FuncSpec("ifNull", (IntegerType, IntegerType), "arg0"),
    FuncSpec("ifNull", (DoubleType, DoubleType), "arg0"),
    FuncSpec("ifNull", (VarcharType, VarcharType), VarcharType),
    FuncSpec("nullIf", (IntegerType, IntegerType), "arg0"),
    FuncSpec("nullIf", (VarcharType, VarcharType), VarcharType),
    FuncSpec("coalesce", (IntegerType, IntegerType), "arg0"),
    FuncSpec("coalesce", (VarcharType, VarcharType), VarcharType),
    FuncSpec("trim", (VarcharType,), VarcharType),
    FuncSpec("reverse", (VarcharType,), VarcharType),
    FuncSpec("toString", (IntegerType,), VarcharType),
)
