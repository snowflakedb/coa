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

"""The CrateDB dialect.

Offline tests always run. Live tests skip unless Docker is available.
"""

from __future__ import annotations

import pytest

from eqgen.core.catalog import Column, Table
from eqgen.core.types import DateType, DoubleType, IntegerType, NumericType, TextType
from eqgen.dialects.cratedb.refresh import Effect, FenceState, Verb, classify, normalize
from eqgen.dialects.cratedb.types_sql import cratedb_literal, cratedb_type
from eqgen.equivalence.config import default_equivalence_config

pytestmark = pytest.mark.unit


def _docker_ready() -> bool:
    from eqgen.dialects.cratedb.cluster import docker_available

    return docker_available()


def _adapter():
    pytest.importorskip("psycopg")
    if not _docker_ready():
        pytest.skip("Docker not available for CrateDB live tests")
    from eqgen.dialects.cratedb.adapter import CrateDbAdapter

    return CrateDbAdapter()


@pytest.mark.parametrize(
    ("sql", "verb", "target"),
    [
        ("INSERT INTO t VALUES (1)", Verb.INSERT_VALUES, "t"),
        ("INSERT INTO t SELECT * FROM u", Verb.WRITE, "t"),
        ("UPDATE t SET a = 1", Verb.WRITE, "t"),
        ("CREATE TABLE t (a int)", Verb.CREATE_TABLE, "t"),
        ("CREATE VIEW v AS SELECT 1", Verb.CREATE_VIEW, "v"),
        ("DROP TABLE t", Verb.DROP, "t"),
    ],
)
def test_classify_verb_and_target(sql: str, verb: Verb, target: str) -> None:
    effect = classify(sql)
    assert effect.verb is verb
    assert effect.targets == frozenset({target})


def test_classify_read_only() -> None:
    assert classify("SELECT 1").verb is Verb.READ
    assert classify("REFRESH TABLE t").verb is Verb.READ


def test_normalize_lowercases_unquoted() -> None:
    assert normalize("T") == "t"
    assert normalize('"T"') == '"T"'


def test_insert_values_skips_preflush() -> None:
    fence = FenceState()
    fence.tables.add("t")
    fence.dirty.add("t")
    assert fence.before("INSERT INTO t VALUES (1)") == []


def test_read_flushes_all_dirty_tables() -> None:
    fence = FenceState()
    fence.tables.update({"a", "b"})
    fence.dirty.update({"b", "a"})
    assert fence.before("SELECT 1") == ["REFRESH TABLE a", "REFRESH TABLE b"]


def test_after_write_dirties_target() -> None:
    fence = FenceState()
    fence.after("INSERT INTO t SELECT 1 FROM u")
    assert "t" in fence.dirty
    assert "t" in fence.tables


def test_cratedb_type_mappings() -> None:
    assert cratedb_type(IntegerType()) == "BIGINT"
    assert cratedb_type(NumericType(10, 2)) == "NUMERIC(10, 2)"
    assert cratedb_type(DateType()) == "TIMESTAMP WITHOUT TIME ZONE"
    assert cratedb_type(DoubleType()) == "DOUBLE PRECISION"
    assert cratedb_type(TextType()) == "TEXT"


def test_cratedb_literal_bool_before_int() -> None:
    assert cratedb_literal(True) == "TRUE"
    assert cratedb_literal(None) == "NULL"


def test_the_dialect_declares_its_builder_set_in_gcl() -> None:
    from eqgen.dialects.cratedb.adapter import cratedb_equivalence_config
    from eqgen.dialects.cratedb.builders import (
        CrateDbClusteredByBuilder,
        CrateDbColumnstoreOffBuilder,
        CrateDbIndexOffBuilder,
        CrateDbNamedFulltextIndexBuilder,
        CrateDbObjectRoundTripBuilder,
        CrateDbPartitionedBuilder,
        CrateDbShardCountBuilder,
    )

    portable = set(default_equivalence_config().builder_weights)
    cratedb = set(cratedb_equivalence_config().builder_weights)
    extras = {
        CrateDbIndexOffBuilder.__name__,
        CrateDbColumnstoreOffBuilder.__name__,
        CrateDbNamedFulltextIndexBuilder.__name__,
        CrateDbPartitionedBuilder.__name__,
        CrateDbObjectRoundTripBuilder.__name__,
        CrateDbShardCountBuilder.__name__,
        CrateDbClusteredByBuilder.__name__,
    }
    assert portable <= cratedb, portable - cratedb
    assert cratedb - portable == extras, (cratedb - portable) ^ extras


def test_native_builder_emits_index_off() -> None:
    from eqgen.dialects.cratedb.builders import CrateDbIndexOffBuilder
    from eqgen.dialects.cratedb.emitter import CrateEmitter
    from eqgen.equivalence.ast import BaseTableSource, ProjectionItem, SelectQuery
    from eqgen.equivalence.context import EquivalenceContext, NameGenerator
    from eqgen.equivalence.emitter import emit_equivalence
    from eqgen.equivalence.factory import EquivalenceBuilderFactory
    from eqgen.ir.expr import col

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
    node = CrateDbIndexOffBuilder(factory)._wrap(query, context, exposed_name="t")
    assert node is not None
    statements = [s.statement_text for s in emit_equivalence(node, CrateEmitter())]
    assert any("INDEX OFF" in s for s in statements), statements
    assert statements[-1].startswith("CREATE VIEW t AS SELECT")


def test_native_builder_emits_columnstore_off() -> None:
    from eqgen.dialects.cratedb.builders import CrateDbColumnstoreOffBuilder
    from eqgen.dialects.cratedb.emitter import CrateEmitter
    from eqgen.equivalence.ast import BaseTableSource, ProjectionItem, SelectQuery
    from eqgen.equivalence.context import EquivalenceContext, NameGenerator
    from eqgen.equivalence.emitter import emit_equivalence
    from eqgen.equivalence.factory import EquivalenceBuilderFactory
    from eqgen.ir.expr import col

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
    node = CrateDbColumnstoreOffBuilder(factory)._wrap(query, context, exposed_name="t")
    assert node is not None
    statements = [s.statement_text for s in emit_equivalence(node, CrateEmitter())]
    assert any("columnstore = false" in s for s in statements), statements
    assert statements[-1].startswith("CREATE VIEW t AS SELECT")
    adapter = _adapter()
    a = adapter.connect()
    b = adapter.connect()
    try:
        a.execute("CREATE TABLE only_a (id BIGINT)")
        with pytest.raises(adapter.db_error):
            b.execute("SELECT * FROM only_a")
    finally:
        a.close()
        b.close()


def test_a_round_runs_end_to_end_on_cratedb() -> None:
    from eqgen.equivalence.generator import EquivalenceGenerator
    from eqgen.fuzz.journal import sample_rows
    from eqgen.fuzz.round import run_round
    from eqgen.generators.example_generator import RandomPredicateSource, RandomSelectSource

    adapter = _adapter()
    table = adapter.simple_catalog("t")
    rows = sample_rows(table, 8, seed=3)
    generator = EquivalenceGenerator(
        adapter.equivalence_config(),
        predicate_source=RandomPredicateSource(),
        emitter=adapter.emitter(),
        extra_builders=adapter.extra_builders(),
    )
    queries = list(RandomSelectSource().iter_queries(table, seed=5, limit=8))
    outcome = run_round(adapter, generator, table, rows, queries, seed=11)
    assert outcome.setup_error is None
    assert outcome.object_comparison is not None and outcome.object_comparison.equal
    assert not outcome.findings, [f.query for f in outcome.findings]
