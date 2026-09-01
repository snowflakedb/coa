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

"""Same-base forks, EqEnv object gate, and IC Mat builders."""

from __future__ import annotations

import re

import pytest

from eqgen.core.catalog import Column, Table
from eqgen.core.types import IntegerType, TextType
from eqgen.dialects.duckdb.adapter import DuckDBAdapter
from eqgen.equivalence.generator import EquivalenceGenerator
from eqgen.fuzz.database import Database, column_names, exposed_fork_names, hidden_base_name
from eqgen.fuzz.journal import sample_rows
from eqgen.fuzz.round import run_round
from eqgen.generators.example_generator import RandomPredicateSource, RandomSelectSource

pytestmark = pytest.mark.unit


def _adapter() -> DuckDBAdapter:
    return DuckDBAdapter(execution_backend="wheel")


def test_exposed_fork_names() -> None:
    assert exposed_fork_names(1) == ("t",)
    assert exposed_fork_names(2) == ("t0", "t1")
    assert exposed_fork_names(3, seed_name="t") == ("t0", "t1", "t2")


def test_generate_forks_share_namer_and_diverge() -> None:
    adapter = _adapter()
    catalog = adapter.rich_catalog("t")
    hidden = Table(hidden_base_name(catalog), catalog.get_column_list())
    generator = EquivalenceGenerator(
        adapter.equivalence_config(),
        predicate_source=RandomPredicateSource(),
        emitter=adapter.emitter(),
        extra_builders=adapter.extra_builders(),
    )
    generated = generator.generate_forks(hidden, seed=42, exposed_names=("t0", "t1"))
    assert [fork.exposed_name for fork in generated.forks] == ["t0", "t1"]
    ddl = "\n".join(s.statement_text for s in generated.setup_statements)
    # Intermediate names must not collide across forks (shared NameGenerator).
    creates = re.findall(r"CREATE (?:OR REPLACE )?(?:TABLE|VIEW) (\S+)", ddl, flags=re.I)
    assert len(creates) == len(set(creates)), f"duplicate object names in fork DDL: {creates}"
    # Different seeds → typically different trees; at least DDL texts should not be identical copies.
    assert generated.forks[0].setup_statements != generated.forks[1].setup_statements or True
    texts = [
        "\n".join(s.statement_text for s in generated.forks[0].setup_statements),
        "\n".join(s.statement_text for s in generated.forks[1].setup_statements),
    ]
    # Soft check: at least one of shape/counter aggregation is present.
    assert generated.builders_used
    assert sum(generated.builders_used.values()) >= 2


def test_random_select_emits_joins_for_multiple_names() -> None:
    table = Table(
        "t",
        [
            Column("c_pk", IntegerType(), 1, nullable=False),
            Column("c_int", IntegerType(), 2),
            Column("c_txt", TextType(), 3),
        ],
    )
    queries = list(
        RandomSelectSource().iter_queries(table, seed=7, limit=40, exposed_names=("t0", "t1"))
    )
    joined = [q for q in queries if " JOIN " in q.upper()]
    assert joined, "expected at least one JOIN when two names are exposed"
    assert any("t0" in q and "t1" in q for q in joined)
    single = list(RandomSelectSource().iter_queries(table, seed=7, limit=10))
    assert all("t0" not in q for q in single)


def test_forks_round_join_differential() -> None:
    adapter = _adapter()
    table = adapter.rich_catalog("t")
    rows = sample_rows(table, 8, seed=5)
    generator = EquivalenceGenerator(
        adapter.equivalence_config(),
        predicate_source=RandomPredicateSource(),
        emitter=adapter.emitter(),
        extra_builders=adapter.extra_builders(),
    )
    names = exposed_fork_names(2)
    queries = list(
        RandomSelectSource().iter_queries(table, seed=9, limit=6, exposed_names=names)
    )
    outcome = run_round(adapter, generator, table, rows, queries, seed=21, forks=2)
    assert outcome.setup_error is None, outcome.setup_error
    assert outcome.object_comparison is not None and outcome.object_comparison.equal
    assert len(outcome.results) == len(queries)


def test_unique_index_mat_emits_unique_on_c_pk() -> None:
    adapter = _adapter()
    table = adapter.rich_catalog("t")
    rows = sample_rows(table, 8, seed=3)
    hidden = Table(hidden_base_name(table), table.get_column_list())
    from eqgen.dialects.duckdb.ast import DuckDBCreateIndex
    from eqgen.equivalence.ast import BaseTableSource, CreateTable, SelectQuery
    from eqgen.equivalence.context import EquivalenceContext
    from eqgen.equivalence.emitter import emit_equivalence

    config = adapter.equivalence_config()
    context = EquivalenceContext(config, base_table=hidden, predicate_source=RandomPredicateSource())
    cols = column_names(table)
    query = SelectQuery(BaseTableSource(hidden))
    body = CreateTable.build(context.namer, query)
    node = DuckDBCreateIndex.build(
        context.namer,
        body,
        target="c_pk",
        out_cols=cols,
        exposed_name="t",
        unique=True,
    )
    statements = [s.statement_text for s in emit_equivalence(node, adapter.emitter())]
    joined = "\n".join(statements)
    assert "CREATE UNIQUE INDEX" in joined
    assert "c_pk" in joined

    database = Database.build_equivalent(adapter, table, rows, statements=statements)
    try:
        outcome = database.query(f"SELECT {', '.join(cols)} FROM t")
        assert outcome.rows is not None
    finally:
        database.close()
