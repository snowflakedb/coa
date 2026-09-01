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

"""SQLite scalar functions allowed in typed predicates.

Stock SQLite only — expand slightly for hunt surface (``instr`` / ``substr`` / ``replace`` /
``trim`` / ``iif`` / ``coalesce`` / ``nullif``). No ``INITCAP`` / ``ASCII``.
"""

from __future__ import annotations

from eqgen.core.types import DoubleType, IntegerType, NumericType, VarcharType
from eqgen.generators.typed_predicate.func_spec import FuncSpec

FUNCS: tuple[FuncSpec, ...] = (
    FuncSpec("ABS", (NumericType,), "arg0"),
    FuncSpec("ABS", (DoubleType,), "arg0"),
    FuncSpec("LENGTH", (VarcharType,), IntegerType),
    FuncSpec("UPPER", (VarcharType,), VarcharType),
    FuncSpec("LOWER", (VarcharType,), VarcharType),
    FuncSpec("TRIM", (VarcharType,), VarcharType),
    FuncSpec("LTRIM", (VarcharType,), VarcharType),
    FuncSpec("RTRIM", (VarcharType,), VarcharType),
    FuncSpec("REPLACE", (VarcharType, VarcharType, VarcharType), VarcharType),
    FuncSpec("SUBSTR", (VarcharType, IntegerType), VarcharType),
    FuncSpec("SUBSTR", (VarcharType, IntegerType, IntegerType), VarcharType),
    FuncSpec("INSTR", (VarcharType, VarcharType), IntegerType),
    FuncSpec("HEX", (VarcharType,), VarcharType),
    FuncSpec("QUOTE", (VarcharType,), VarcharType),
    FuncSpec("IFNULL", (IntegerType, IntegerType), "arg0"),
    FuncSpec("IFNULL", (VarcharType, VarcharType), VarcharType),
    FuncSpec("NULLIF", (IntegerType, IntegerType), "arg0"),
    FuncSpec("NULLIF", (VarcharType, VarcharType), VarcharType),
    FuncSpec("COALESCE", (IntegerType, IntegerType), "arg0"),
    FuncSpec("COALESCE", (VarcharType, VarcharType), VarcharType),
    FuncSpec("max", (NumericType, NumericType), "arg0"),
    FuncSpec("max", (DoubleType, DoubleType), "arg0"),
    FuncSpec("max", (VarcharType, VarcharType), VarcharType),
    FuncSpec("min", (NumericType, NumericType), "arg0"),
    FuncSpec("min", (DoubleType, DoubleType), "arg0"),
    FuncSpec("min", (VarcharType, VarcharType), VarcharType),
)
