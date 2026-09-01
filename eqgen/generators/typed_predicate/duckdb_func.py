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

"""DuckDB scalar functions allowed in typed predicates.

Same idea as :mod:`pg_func`: only listed shapes are built. DuckDB accepts some things Postgres
rejects (e.g. aggregates over boolean); those still do not belong in ``WHERE``, so they are not
here. Dialect-only scalars (``ends_with``) and spellings (``LENGTH`` vs ``CHAR_LENGTH``) do.
"""

from __future__ import annotations

from eqgen.core.types import BooleanType, DoubleType, IntegerType, NumericType, VarcharType
from eqgen.generators.typed_predicate.func_spec import FuncSpec

FUNCS: tuple[FuncSpec, ...] = (
    FuncSpec("ABS", (NumericType,), "arg0"),
    FuncSpec("ABS", (DoubleType,), "arg0"),
    FuncSpec("CEIL", (NumericType,), "arg0"),
    FuncSpec("CEIL", (DoubleType,), DoubleType),
    FuncSpec("FLOOR", (NumericType,), "arg0"),
    FuncSpec("FLOOR", (DoubleType,), DoubleType),
    FuncSpec("SIGN", (NumericType,), "arg0"),
    FuncSpec("SIGN", (DoubleType,), "arg0"),
    # LENGTH is DuckDB's usual character-length spelling (Postgres catalog uses CHAR_LENGTH)
    FuncSpec("LENGTH", (VarcharType,), IntegerType),
    FuncSpec("UPPER", (VarcharType,), VarcharType),
    FuncSpec("LOWER", (VarcharType,), VarcharType),
    # DuckDB-only boolean atom (not on the Postgres allowlist)
    FuncSpec("ends_with", (VarcharType, VarcharType), BooleanType),
    FuncSpec("starts_with", (VarcharType, VarcharType), BooleanType),
    FuncSpec("GREATEST", (NumericType, NumericType), "arg0"),
    FuncSpec("GREATEST", (DoubleType, DoubleType), "arg0"),
    FuncSpec("GREATEST", (VarcharType, VarcharType), VarcharType),
    # DuckDB allows GREATEST/LEAST on boolean; Postgres does not — listed only here
    FuncSpec("GREATEST", (BooleanType, BooleanType), BooleanType),
    FuncSpec("LEAST", (NumericType, NumericType), "arg0"),
    FuncSpec("LEAST", (DoubleType, DoubleType), "arg0"),
    FuncSpec("LEAST", (VarcharType, VarcharType), VarcharType),
    FuncSpec("LEAST", (BooleanType, BooleanType), BooleanType),
)
