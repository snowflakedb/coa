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

"""PostgreSQL scalar functions allowed in typed predicates.

Signatures are the gate: anything not listed here is never built for ``--dialect postgres``.
Aggregates (``max``/``min``/…) stay out — illegal in ``WHERE``, and ``max(boolean)`` is rejected
by Postgres even as an aggregate.
"""

from __future__ import annotations

from eqgen.core.types import DoubleType, IntegerType, NumericType, VarcharType
from eqgen.generators.typed_predicate.func_spec import FuncSpec

FUNCS: tuple[FuncSpec, ...] = (
    # numeric / float
    FuncSpec("ABS", (NumericType,), "arg0"),
    FuncSpec("ABS", (DoubleType,), "arg0"),
    FuncSpec("CEIL", (NumericType,), "arg0"),
    FuncSpec("CEIL", (DoubleType,), DoubleType),
    FuncSpec("FLOOR", (NumericType,), "arg0"),
    FuncSpec("FLOOR", (DoubleType,), DoubleType),
    FuncSpec("SIGN", (NumericType,), "arg0"),
    FuncSpec("SIGN", (DoubleType,), "arg0"),
    # strings — CHAR_LENGTH is the Postgres spelling for character length
    FuncSpec("CHAR_LENGTH", (VarcharType,), IntegerType),
    FuncSpec("UPPER", (VarcharType,), VarcharType),
    FuncSpec("LOWER", (VarcharType,), VarcharType),
    FuncSpec("INITCAP", (VarcharType,), VarcharType),
    FuncSpec("ASCII", (VarcharType,), IntegerType),
    # binary string ops (same family in, same family out)
    FuncSpec("GREATEST", (NumericType, NumericType), "arg0"),
    FuncSpec("GREATEST", (DoubleType, DoubleType), "arg0"),
    FuncSpec("GREATEST", (VarcharType, VarcharType), VarcharType),
    FuncSpec("LEAST", (NumericType, NumericType), "arg0"),
    FuncSpec("LEAST", (DoubleType, DoubleType), "arg0"),
    FuncSpec("LEAST", (VarcharType, VarcharType), VarcharType),
    # deliberately no GREATEST/LEAST on BooleanType — keeps boolean out of these shapes
)
