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

"""The way in: ``generate(table)`` returns an object holding the same rows as *table*, plus the
statements that create it.

The outermost object takes the base table's own name, so the same query text runs against both::

    database 1:  CREATE TABLE t (...)                       -- the base table
    database 2:  CREATE TABLE t__base (...)                 -- base renamed out of the way
                 CREATE VIEW  t AS SELECT * FROM t__base    -- the equivalent, called t

    both:        SELECT c_int FROM t                        -- identical text, compared

Give them different names and every query would need rewriting per side, and the comparison would
be testing the rewriter.

Same-base forks use :meth:`generate_forks`: several independent derivations that share one
``NameGenerator`` and land under distinct exposed names in one schema.
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence

from eqgen.builder.constraint_set import ConstraintSet
from eqgen.core.catalog import Table
from eqgen.core.statement import Statement
from eqgen.equivalence.ast import EquivalentRelation
from eqgen.equivalence.config import EquivalenceConfig
from eqgen.equivalence.constraints import ExposedNameConstraint
from eqgen.equivalence.context import EquivalenceContext, NameGenerator
from eqgen.equivalence.emitter import SqlEmitter, emit_equivalence
from eqgen.equivalence.factory import EquivalenceBuilderFactory
from eqgen.plugins import PredicateSource


@dataclass(frozen=True)
class GeneratedEquivalence:
    """One generated object.

    Attributes:
        root: The tree, with names assigned. Kept because a log or a report often wants the
            structure and not just the SQL.
        setup_statements: The statements that build it, in order.
    """

    root: EquivalentRelation
    setup_statements: list[Statement]

    @property
    def exposed_name(self) -> str:
        """The name to query this object under."""
        return self.root.materialized_name


@dataclass(frozen=True)
class GeneratedForks:
    """Several independent equivalents of one base, for same-base fork rounds."""

    forks: tuple[GeneratedEquivalence, ...]
    builders_used: Counter[str]
    predicate_origin: Counter[str]

    @property
    def setup_statements(self) -> list[Statement]:
        """DDL for every fork, in order."""
        statements: list[Statement] = []
        for fork in self.forks:
            statements.extend(fork.setup_statements)
        return statements

    @property
    def exposed_names(self) -> tuple[str, ...]:
        return tuple(fork.exposed_name for fork in self.forks)


class EquivalenceGenerator:
    """Makes an object holding the same rows as a base table, reached a different way."""

    def __init__(
        self,
        config: Optional[EquivalenceConfig] = None,
        *,
        predicate_source: Optional[PredicateSource] = None,
        emitter: Optional[SqlEmitter] = None,
        extra_builders: Sequence[type] = (),
    ) -> None:
        self._factory = EquivalenceBuilderFactory(config, extra_builders=extra_builders)
        self._last_predicate_origin: Counter[str] = Counter()
        self._last_builders_used: Counter[str] = Counter()
        self._config = self._factory.config
        self._predicate_source = predicate_source
        self._emitter = emitter

    @property
    def factory(self) -> EquivalenceBuilderFactory:
        return self._factory

    @property
    def builders_used(self) -> "Counter[str]":
        """Which builders produced a node in the most recent :meth:`generate` / forks, and how many each."""
        return self._last_builders_used if self._last_builders_used else self._factory.chosen

    @property
    def predicate_origin(self) -> "Counter[str]":
        """Where the most recent generation's predicates came from. See ``EquivalenceContext``."""
        return self._last_predicate_origin

    def generate(
        self,
        table: Table,
        *,
        seed: Optional[int] = None,
        exposed_name: Optional[str] = None,
        deadline: Optional[datetime] = None,
        name_generator: Optional[NameGenerator] = None,
    ) -> GeneratedEquivalence:
        """Build an object equivalent to *table*.

        One *seed* fixes the whole result: every weighted choice, column pick and predicate
        request comes from it. Pass ``None`` to carry on from wherever the RNG is.

        *exposed_name* sets the outermost object's name. It defaults to the base table's name;
        pass something else when both have to live in one database.

        Raises ``ValueError`` when no builder could produce anything. Since the base table leaf and
        ``SELECT *`` are always available, that means the configuration is wrong rather than the
        draw being unlucky.
        """
        if seed is not None:
            random.seed(seed)
        self._factory.reset_chosen()
        context = EquivalenceContext(
            self._config,
            base_table=table,
            predicate_source=self._predicate_source,
            deadline=deadline,
            name_generator=name_generator,
        )
        root_name = exposed_name if exposed_name is not None else table.table_name
        # Shares the context's counter object, so it fills in as the tree is built.
        self._last_predicate_origin = context.predicate_origin
        root = self._factory.build_subtree(
            EquivalentRelation,  # type: ignore[type-abstract]
            ConstraintSet([ExposedNameConstraint(root_name)]),
            context,
        )
        if root is None:
            raise ValueError(f"failed to generate an equivalence for table {table.get_sql_name()}")
        self._last_builders_used = Counter(self._factory.chosen)
        return GeneratedEquivalence(root=root, setup_statements=emit_equivalence(root, self._emitter))

    def generate_forks(
        self,
        table: Table,
        *,
        seed: int,
        exposed_names: Sequence[str],
        deadline: Optional[datetime] = None,
    ) -> GeneratedForks:
        """Build one independent equivalent per *exposed_names* entry (same-base forks).

        Shares a :class:`NameGenerator` so intermediate object names do not collide when all
        forks are installed in one schema. Each fork uses ``seed + i`` so the trees diverge.
        """
        if not exposed_names:
            raise ValueError("generate_forks requires at least one exposed name")
        shared_names = NameGenerator()
        forks: list[GeneratedEquivalence] = []
        builders: Counter[str] = Counter()
        origins: Counter[str] = Counter()
        for i, exposed in enumerate(exposed_names):
            eq = self.generate(
                table,
                seed=seed + i,
                exposed_name=exposed,
                deadline=deadline,
                name_generator=shared_names,
            )
            forks.append(eq)
            builders.update(self._factory.chosen)
            origins.update(self._last_predicate_origin)
        self._last_builders_used = builders
        self._last_predicate_origin = origins
        return GeneratedForks(forks=tuple(forks), builders_used=builders, predicate_origin=origins)
