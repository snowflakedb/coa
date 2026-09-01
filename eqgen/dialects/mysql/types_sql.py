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

"""MySQL type names and literals.

Two vocabularies on purpose: column DDL uses :func:`mysql_type`, while ``CAST`` targets use
:func:`mysql_cast_type`. Mixing them produces illegal SQL (``CAST(... AS BIGINT)`` /
``CAST(... AS TINYINT(1))``).
"""

from __future__ import annotations

from eqgen.core.types import (
    BooleanType,
    CharType,
    DateType,
    DoubleType,
    IntegerType,
    NumericType,
    SqlType,
    TextType,
    TimestampType,
    VarcharType,
)

#: Bound every text column so a full-column index is legal under utf8mb4 (InnoDB key ≤ 3072 bytes).
TEXT_LENGTH = 255


def mysql_type(dtype: SqlType) -> str:
    """Column DDL type name for *dtype*."""
    if isinstance(dtype, BooleanType):
        return "TINYINT(1)"
    if isinstance(dtype, DoubleType):
        return "DOUBLE"
    if isinstance(dtype, IntegerType):
        return "BIGINT"
    if isinstance(dtype, NumericType):
        scale = dtype.get_scale()
        if scale in (None, 0):
            return "BIGINT"
        return f"DECIMAL({dtype.get_precision() or 38}, {scale})"
    if isinstance(dtype, DateType):
        return "DATE"
    if isinstance(dtype, TimestampType):
        return "DATETIME(6)"
    if isinstance(dtype, (VarcharType, TextType, CharType)):
        return f"VARCHAR({TEXT_LENGTH})"
    return f"VARCHAR({TEXT_LENGTH})"


def mysql_cast_type(dtype: SqlType) -> str:
    """``CAST`` target for *dtype* — not the same as :func:`mysql_type`."""
    if isinstance(dtype, BooleanType):
        return "SIGNED"
    if isinstance(dtype, DoubleType):
        return "DOUBLE"
    if isinstance(dtype, IntegerType):
        return "SIGNED"
    if isinstance(dtype, NumericType):
        scale = dtype.get_scale()
        if scale in (None, 0):
            return "SIGNED"
        return f"DECIMAL({dtype.get_precision() or 38}, {scale})"
    if isinstance(dtype, DateType):
        return "DATE"
    if isinstance(dtype, TimestampType):
        return "DATETIME(6)"
    return f"CHAR({TEXT_LENGTH})"


def mysql_literal(value: object) -> str:
    """A literal for the seed ``INSERT``.

    Assumes ``NO_BACKSLASH_ESCAPES``: only ``'`` is doubled. Booleans are ``0``/``1`` (checked
    before ``int`` because ``bool`` subclasses ``int``). NaN/Inf have no faithful DOUBLE literal.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return repr(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"MySQL DOUBLE cannot represent {value!r}; no faithful literal exists")
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"
