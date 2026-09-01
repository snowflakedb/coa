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

"""The MariaDB dialect."""

from __future__ import annotations

import pytest

from eqgen.dialects.mysql.builders import MySqlInvisibleIndexBuilder
from eqgen.equivalence.config import default_equivalence_config

pytestmark = pytest.mark.unit


def _docker_ready() -> bool:
    from eqgen.dialects.mysql.cluster import docker_available

    return docker_available()


def _adapter():
    pytest.importorskip("pymysql")
    if not _docker_ready():
        pytest.skip("Docker not available for MariaDB live tests")
    from eqgen.dialects.mysql.adapter import MariaDbAdapter

    return MariaDbAdapter()


def test_mariadb_invisible_index_builder_is_disabled_in_gcl() -> None:
    from eqgen.dialects.mysql.adapter import mariadb_equivalence_config, mysql_equivalence_config

    assert mariadb_equivalence_config().builder_weights[MySqlInvisibleIndexBuilder.__name__] == 0
    assert mysql_equivalence_config().builder_weights[MySqlInvisibleIndexBuilder.__name__] > 0


def test_mariadb_gcl_matches_mysql_except_known_deltas() -> None:
    from eqgen.dialects.mysql.adapter import mariadb_equivalence_config, mysql_equivalence_config

    mariadb = mariadb_equivalence_config().builder_weights
    mysql = mysql_equivalence_config().builder_weights
    differing = {name for name in mysql if mysql[name] != mariadb.get(name, -1)}
    # Invisible indexes spell differently; WindowRewrite is throttled on MariaDB (errno 4016).
    # LATERAL, CREATE MATERIALIZED VIEW, and the MySQL JSON_OBJECT pack builder stay off:
    # MariaDB 11.4 has no LATERAL (1064), no MATERIALIZED VIEW, and JsonPack is still unswept.
    still_mysql_only = {
        "CreateMaterializedViewBuilder",
        "LateralReprojectQueryBuilder",
        "MySqlJsonPackRoundTripBuilder",
    }
    assert differing == {
        MySqlInvisibleIndexBuilder.__name__,
        "WindowRewriteQueryBuilder",
    } | still_mysql_only


def test_mariadb_known_issue_labels_match_mysql() -> None:
    from eqgen.dialects.mysql.adapter import MariaDbAdapter, MySqlAdapter

    mysql = MySqlAdapter.__new__(MySqlAdapter)
    mariadb = MariaDbAdapter.__new__(MariaDbAdapter)
    for cursor in ("near 'brien')' at line 1", "near 'rG')' at line 1"):
        exc = Exception(1064, f"You have an error in your SQL syntax; check the manual {cursor}")
        assert mariadb.known_issue_label(exc) == mysql.known_issue_label(exc) == "mysql-obrien-apostrophe-syntax"


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
