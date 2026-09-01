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

"""Typed AST predicate source — build an :class:`~eqgen.ir.expr.ExpressionNode`, then print
it to SQL before handing it to the equivalence harness.

Dialect scalar functions live in per-engine catalogs (:mod:`pg_func`, :mod:`duckdb_func`,
:mod:`sqlite_func`, :mod:`mysql_func`). Printing is local (:mod:`print`); object-emission
spelling under ``dialects/`` is untouched.
"""

from __future__ import annotations

from typing import Optional

from eqgen.core.catalog import Table
from eqgen.generators.typed_predicate.build import build_predicate
from eqgen.generators.typed_predicate.print import print_predicate


class TypedPredicateSource:
    """:class:`~eqgen.plugins.PredicateSource` that builds a typed AST, then stringifies it.

    *dialect* selects the local printer and function catalog (``postgres``, ``duckdb``,
    ``sqlite``, ``clickhouse``, MySQL-family, ``cratedb``). Equivalence still sees opaque text via
    :class:`~eqgen.ir.expr.GeneratedPredicate`.
    """

    name = "typed"

    def __init__(self, dialect: str = "postgres") -> None:
        self.dialect = dialect

    def boolean_predicate(self, table: Table, *, seed: int) -> Optional[str]:
        node = build_predicate(table, seed=seed, dialect=self.dialect)
        if node is None:
            return None
        return print_predicate(node, dialect=self.dialect)


__all__ = ["TypedPredicateSource", "build_predicate", "print_predicate"]
