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

"""Unit tests for per-round catalog and IC-aware row sampling."""

from __future__ import annotations

import pytest

from eqgen.fuzz.cli import load_adapter
from eqgen.fuzz.journal import PK_COLUMN, sample_catalog, sample_rows

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def duckdb_adapter():
    return load_adapter("duckdb", duckdb_backend="wheel")


def test_sample_catalog_always_has_c_pk(duckdb_adapter) -> None:
    table = sample_catalog(duckdb_adapter, seed=7)
    names = [c.get_column_name() for c in table.get_column_list()]
    assert names[0] == PK_COLUMN
    assert not table.get_column_list()[0].get_is_nullable()


def test_sample_catalog_deterministic(duckdb_adapter) -> None:
    a = sample_catalog(duckdb_adapter, seed=99)
    b = sample_catalog(duckdb_adapter, seed=99)
    assert [c.get_column_name() for c in a.get_column_list()] == [
        c.get_column_name() for c in b.get_column_list()
    ]
    assert [repr(c.get_data_type()) for c in a.get_column_list()] == [
        repr(c.get_data_type()) for c in b.get_column_list()
    ]


def test_sample_catalog_types_come_from_pool(duckdb_adapter) -> None:
    pool_types = {type(dtype) for _, dtype in duckdb_adapter.catalog_type_pool()}
    for seed in range(20):
        table = sample_catalog(duckdb_adapter, seed=seed)
        for column in table.get_column_list()[1:]:
            assert type(column.get_data_type()) in pool_types


def test_sample_rows_uniquifies_c_pk_with_duplicate_payload(duckdb_adapter) -> None:
    table = duckdb_adapter.rich_catalog("t")
    rows = sample_rows(table, 8, seed=1)
    pk_i = next(i for i, c in enumerate(table.get_column_list()) if c.get_column_name() == PK_COLUMN)
    pks = [row[pk_i] for row in rows]
    assert None not in pks
    assert len(set(pks)) == len(pks)
    # Intentional duplicate on non-key columns: last row matches row 1 except c_pk.
    assert len(rows) >= 3
    payload = lambda row: tuple(v for i, v in enumerate(row) if i != pk_i)
    assert payload(rows[-1]) == payload(rows[1])


def test_sample_rows_keeps_duplicate_payload_with_inf(duckdb_adapter) -> None:
    """Regression: Inf used to be written onto ``rows[1]`` *after* ``rows[-1] = rows[1]``, so on
    every ``supports_float_inf`` dialect with a DOUBLE column the duplicate pair vanished."""
    table = duckdb_adapter.rich_catalog("t")
    pk_i = next(i for i, c in enumerate(table.get_column_list()) if c.get_column_name() == PK_COLUMN)
    payload = lambda row: tuple(v for i, v in enumerate(row) if i != pk_i)
    for seed in range(10):
        rows = sample_rows(table, 8, seed=seed, allow_inf=True)
        assert payload(rows[-1]) == payload(rows[1]), f"duplicate lost at seed {seed}"
        flat = [v for row in rows for v in row]
        assert float("inf") in flat and float("-inf") in flat
