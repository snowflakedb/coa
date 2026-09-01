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

"""The TiDB dialect."""

from __future__ import annotations

import pytest

from eqgen.core.catalog import Column, Table
from eqgen.core.types import IntegerType, TextType
from eqgen.dialects.tidb.builders import TiDbCachedTableBuilder
from eqgen.dialects.tidb.emitter import TiDbEmitter
from eqgen.equivalence.ast import BaseTableSource, ProjectionItem, SelectQuery
from eqgen.equivalence.config import default_equivalence_config
from eqgen.equivalence.context import EquivalenceContext, NameGenerator
from eqgen.equivalence.emitter import emit_equivalence
from eqgen.equivalence.factory import EquivalenceBuilderFactory
from eqgen.ir.expr import col

pytestmark = pytest.mark.unit


def _cluster_ready() -> bool:
    from eqgen.dialects.tidb.cluster import cluster_available

    return cluster_available()


def _adapter():
    pytest.importorskip("pymysql")
    if not _cluster_ready():
        pytest.skip("TiDB cluster not available for live tests")
    from eqgen.dialects.tidb.adapter import TiDbAdapter

    return TiDbAdapter()


def test_tidb_cached_table_builder_emits_cache() -> None:
    table = Table(
        "t",
        [
            Column("c_pk", IntegerType(), 1, nullable=False),
            Column("c_txt", TextType(), 2),
        ],
    )
    factory = EquivalenceBuilderFactory()
    context = EquivalenceContext(
        default_equivalence_config(),
        base_table=table,
        predicate_source=None,
        name_generator=NameGenerator(),
    )
    query = SelectQuery(
        BaseTableSource(table),
        [
            ProjectionItem("c_pk", col("c_pk", IntegerType()), IntegerType()),
            ProjectionItem("c_txt", col("c_txt", TextType()), TextType()),
        ],
    )
    cached = TiDbCachedTableBuilder(factory)._wrap(query, context, exposed_name="t")
    assert cached is not None
    statements = [s.statement_text for s in emit_equivalence(cached, TiDbEmitter())]
    assert any("ALTER TABLE" in s and "CACHE" in s for s in statements), statements
    assert statements[-1].startswith("CREATE VIEW t AS SELECT")


def test_the_dialect_declares_ticachedtable_in_gcl() -> None:
    from eqgen.dialects.tidb.adapter import tidb_equivalence_config

    portable = set(default_equivalence_config().builder_weights)
    tidb = set(tidb_equivalence_config().builder_weights)
    assert portable <= tidb, portable - tidb
    assert tidb - portable == {"TiDbCachedTableBuilder"}


def test_tidb_known_issue_labels_cant_find_column() -> None:
    """Dup of dbfuzz tidb-run19-anyvalue-view-expr-in-subquery — demote, do not re-file."""
    from eqgen.dialects.tidb.adapter import TiDbAdapter

    adapter = TiDbAdapter.__new__(TiDbAdapter)  # no cluster — label logic is pure
    for msg in (
        "Can't find column Column#5 in schema Column: [Column#15] PKOrUK: [] NullableUK: []",
        "Can't find column eqgen_1.t__base_table_17.c_dbl in schema Column: [eqgen_1.t__base_table_14.c_pk,Column#42]",
        "Can't find column Column#12 in schema Column: [] Unique key: []",
    ):
        exc = Exception(1105, msg)
        assert adapter.known_issue_label(exc) == "tidb-anyvalue-view-cant-find-column", msg
    assert adapter.known_issue_label(Exception(1105, "not implemented: foo")) == "tidb-unsupported-feature"
    assert (
        adapter.known_issue_label(
            Exception(1105, "runtime error: invalid memory address or nil pointer dereference")
        )
        == "tidb-predicate-pushdown-nil-plan"
    )
    assert (
        adapter.known_issue_label(Exception(1105, "assignment to entry in nil map"))
        == "tidb-unionall-fd-nil-map"
    )


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
