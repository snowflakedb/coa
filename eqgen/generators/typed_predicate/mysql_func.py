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

"""MySQL-family scalar functions for typed predicates (mysql / mariadb / tidb / dolt).

``CHAR_LENGTH`` / ``GREATEST`` / ``LEAST`` / ``ASCII`` match MySQL 8+ spelling.
No Postgres-only ``INITCAP``.
"""

from __future__ import annotations

from eqgen.core.types import DoubleType, IntegerType, NumericType, VarcharType
from eqgen.generators.typed_predicate.func_spec import FuncSpec

FUNCS: tuple[FuncSpec, ...] = (
    FuncSpec("ABS", (NumericType,), "arg0"),
    FuncSpec("ABS", (DoubleType,), "arg0"),
    FuncSpec("CEIL", (NumericType,), "arg0"),
    FuncSpec("CEIL", (DoubleType,), DoubleType),
    FuncSpec("FLOOR", (NumericType,), "arg0"),
    FuncSpec("FLOOR", (DoubleType,), DoubleType),
    FuncSpec("SIGN", (NumericType,), IntegerType),
    FuncSpec("SIGN", (DoubleType,), IntegerType),
    FuncSpec("CHAR_LENGTH", (VarcharType,), IntegerType),
    FuncSpec("UPPER", (VarcharType,), VarcharType),
    FuncSpec("LOWER", (VarcharType,), VarcharType),
    FuncSpec("ASCII", (VarcharType,), IntegerType),
    FuncSpec("GREATEST", (NumericType, NumericType), "arg0"),
    FuncSpec("GREATEST", (DoubleType, DoubleType), "arg0"),
    FuncSpec("GREATEST", (VarcharType, VarcharType), VarcharType),
    FuncSpec("LEAST", (NumericType, NumericType), "arg0"),
    FuncSpec("LEAST", (DoubleType, DoubleType), "arg0"),
    FuncSpec("LEAST", (VarcharType, VarcharType), VarcharType),
)
