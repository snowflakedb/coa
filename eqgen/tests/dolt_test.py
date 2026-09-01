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

"""The Dolt dialect."""

from __future__ import annotations

import pytest

from eqgen.equivalence.config import default_equivalence_config

pytestmark = pytest.mark.unit


def _cluster_ready() -> bool:
    from eqgen.dialects.dolt.cluster import cluster_available

    return cluster_available()


def _adapter():
    pytest.importorskip("pymysql")
    if not _cluster_ready():
        pytest.skip("Dolt cluster not available for live tests")
    from eqgen.dialects.dolt.adapter import DoltAdapter

    return DoltAdapter()


def test_the_dialect_has_portable_builders_only() -> None:
    from eqgen.dialects.dolt.adapter import dolt_equivalence_config

    portable = set(default_equivalence_config().builder_weights)
    dolt = set(dolt_equivalence_config().builder_weights)
    assert dolt == portable


def test_dolt_adapter_has_no_extra_builders() -> None:
    from eqgen.dialects.dolt.adapter import DoltAdapter

    assert DoltAdapter().extra_builders() == ()


def test_two_connections_cannot_see_each_others_databases() -> None:
    adapter = _adapter()
    a = adapter.connect()
    b = adapter.connect()
    try:
        a.execute("CREATE TABLE only_a (id INT)")
        with pytest.raises(adapter.db_error):
            b.execute("SELECT * FROM only_a")
    finally:
        a.close()
        b.close()
