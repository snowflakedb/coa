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

"""SQLite type names and literals."""

from __future__ import annotations

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


def sqlite_type(dtype: SqlType) -> str:
    """Column DDL type for *dtype*."""
    if isinstance(dtype, (IntegerType, BooleanType)):
        return "INTEGER"
    if isinstance(dtype, DoubleType):
        return "REAL"
    if isinstance(dtype, NumericType):
        return "NUMERIC"
    if isinstance(dtype, (TextType, DateType, TimestampType)):
        return "TEXT"
    return "TEXT"


def sqlite_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return repr(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"SQLite cannot represent {value!r}; no faithful literal exists")
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"
