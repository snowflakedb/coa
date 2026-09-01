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

"""ClickHouse type names and literals.

Every column type is wrapped in ``Nullable(...)``: ClickHouse coerces NULL into type
defaults (``0``, ``''``) on non-nullable columns, which would silently corrupt both sides
of the oracle. ``DATE`` maps to ``Date32`` (plain ``Date`` silently clamps pre-1970).
"""

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

_DATETIME_PRECISION = 6


def _unwrapped_type(dtype: SqlType) -> str:
    if isinstance(dtype, DoubleType):
        return "Float64"
    if isinstance(dtype, IntegerType):
        return "Int64"
    if isinstance(dtype, NumericType):
        scale = dtype.get_scale()
        if scale in (None, 0):
            return "Int64"
        precision = dtype.get_precision() or 18
        # Space after the comma matches ClickHouse's DESCRIBE spelling.
        return f"Decimal({precision}, {scale})"
    if isinstance(dtype, BooleanType):
        return "Bool"
    if isinstance(dtype, DateType):
        return "Date32"
    if isinstance(dtype, TimestampType):
        return f"DateTime64({_DATETIME_PRECISION})"
    if isinstance(dtype, TextType):
        return "String"
    return "String"


def clickhouse_type(dtype: SqlType) -> str:
    """Column DDL type for *dtype*, always ``Nullable(...)``."""
    return f"Nullable({_unwrapped_type(dtype)})"


def clickhouse_literal(value: object) -> str:
    """Render *value* as a ClickHouse SQL literal (backslash-aware)."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value).replace("\\", "\\\\").replace("'", "''")
    return f"'{text}'"
