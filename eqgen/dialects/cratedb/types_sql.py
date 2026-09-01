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

"""CrateDB column types and literals."""

from __future__ import annotations

import math

from eqgen.core.types import (
    BooleanType,
    DateType,
    DoubleType,
    IntegerType,
    NumericType,
    SqlType,
    TextType,
    TimestampType,
)


def cratedb_type(dtype: SqlType) -> str:
    """Column DDL type for *dtype*."""
    if isinstance(dtype, DoubleType):
        return "DOUBLE PRECISION"
    if isinstance(dtype, NumericType):
        scale = dtype.get_scale()
        if scale in (None, 0):
            return "BIGINT"
        precision = dtype.get_precision() or 18
        return f"NUMERIC({precision}, {scale})"
    if isinstance(dtype, BooleanType):
        return "BOOLEAN"
    if isinstance(dtype, DateType):
        return "TIMESTAMP WITHOUT TIME ZONE"
    if isinstance(dtype, TimestampType):
        return "TIMESTAMP WITHOUT TIME ZONE"
    if isinstance(dtype, IntegerType):
        return "BIGINT"
    return "TEXT"


def cratedb_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return "'NaN'::double precision"
        return f"'{'Infinity' if value > 0 else '-Infinity'}'::double precision"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"
