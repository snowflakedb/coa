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

"""Constraints: what a builder asks a child for.

A constraint says what the child must *do*, never which class it must be. Asking for a
relation holding only the rows where ``MOD(c_int, 2) = 0``::

    build_subtree(EquivalentRelation, ConstraintSet([RowFilterConstraint(even)]), context)

The factory then offers only builders that listed ``RowFilterConstraint`` in
``supported_constraint_types()``, and any of them may serve it::

    CREATE VIEW  t_view_1  AS SELECT * FROM t WHERE MOD(c_int, 2) = 0
    CREATE TABLE t_table_1 AS SELECT * FROM t WHERE MOD(c_int, 2) = 0

Two rules, both applied by the factory rather than by anything here:

* a constraint in the set rules out every builder that does not list it;
* a constraint absent from the set rules out nobody.

There is no "here is the relation to reproduce" constraint. What every object reproduces is
the base table on the context, narrowed by whatever ``RowFilterConstraint`` accumulated on the
way down, and whether it really did is settled by running both sides. That is why most
``meets_constraint`` methods below just return ``True``.

Two of the seven below have no builder that creates them yet. They are kept as worked examples,
and as the list of what can be added next:

===========================  =======================================  ======================
Constraint                   What is unusual about it                 Created by
===========================  =======================================  ======================
``RowFilterConstraint``      carries a payload; two of them ``AND``    the row-splitting
                             together
``ExposedNameConstraint``    set only at the root, and really checked  the generator
``SingleSourceConstraint``   carries nothing, only routes              nothing yet
``AcceptsDmlConstraint``     carries nothing, only routes              tag-delete / DML asks
``ColumnRewriteConstraint``  checks structure; merging two that        _materialize_row_key
                             touch one column raises
``TagChannelConstraint``     *required* by one builder, *supported*    the tag expand/reduce
``KeyChannelConstraint``     by another - see below                    the key expand/reduce
===========================  =======================================  ======================

The last pair shows the one trick worth knowing. A builder can *require* a constraint instead of
merely supporting it, so it can only ever be built underneath the builder that creates it. Add a
third builder that does not support it, and those two can never nest inside each other.
``builders/expansion.py`` is the worked example: the reducer creates the channel, the expander
requires it, and the reducer does not support it — so an expansion can never be left in a tree with
nothing above it to collapse the copies again.
"""

from __future__ import annotations

from typing import Optional, Self

from eqgen.builder.constraint_set import Constraint
from eqgen.equivalence.ast import EqNode, EquivalentRelation, ProjectionItem
from eqgen.equivalence.keys import KeySpec
from eqgen.ir.expr import ExpressionNode, conjoin


class RowFilterConstraint(Constraint[EqNode]):
    """The child must return only the rows matching ``predicate``::

        SELECT * FROM t WHERE MOD(c_int, 2) = 0

    Two of these in one set combine with ``AND``::

        MOD(c_int, 2) = 0  +  c_big > 0   ->   MOD(c_int, 2) = 0 AND c_big > 0

    So a builder that splits rows in two passes each branch its own filter and asks for a
    relation, instead of working out the rows itself. Any builder that can put a ``WHERE`` in
    can serve either branch.
    """

    def __init__(self, predicate: ExpressionNode) -> None:
        self._predicate = predicate

    @property
    def predicate(self) -> ExpressionNode:
        return self._predicate

    def merge_constraint(self, other: Self) -> Self:
        return type(self)(conjoin(self._predicate, other._predicate))

    def meets_constraint(self, node: EqNode) -> bool:
        # Which rows a node returns is only known by running it, so that is where it is checked.
        return True


class AcceptsDmlConstraint(Constraint[EqNode]):
    """The child must be something you can write to — a table, not a view.

    Carries nothing. Being in the set is the whole message: tables list it, views do not, so
    views are not offered.
    """

    def merge_constraint(self, other: Self) -> Self:
        return self

    def meets_constraint(self, node: EqNode) -> bool:
        return True  # routing already guaranteed only DML-capable builders ran


class SingleSourceConstraint(Constraint[EqNode]):
    """The child's query must read one table and no more::

        ok      SELECT * FROM t
        not ok  SELECT * FROM t1 UNION ALL SELECT * FROM t2

    For object kinds whose body is restricted that way — a materialized view, in most engines.
    Carries nothing; the ``UNION ALL`` builders do not list it, so they are not offered.
    """

    def merge_constraint(self, other: Self) -> Self:
        return self

    def meets_constraint(self, node: EqNode) -> bool:
        return True  # routing already guaranteed only single-source builders ran


class ColumnRewriteConstraint(Constraint[EqNode]):
    """The child must return exactly these columns, in this order.

    The only one here that really checks the node it gets back, and the only one whose merge
    can fail: two of these both rewriting ``c_int`` would need one to win silently, so it
    raises instead.
    """

    def __init__(self, projection: tuple[ProjectionItem, ...]) -> None:
        self._projection = projection

    @property
    def projection(self) -> tuple[ProjectionItem, ...]:
        return self._projection

    def merge_constraint(self, other: Self) -> Self:
        names = {item.alias for item in self._projection}
        if names & {item.alias for item in other._projection}:
            raise ValueError("Cannot merge ColumnRewriteConstraint with overlapping output names")
        return type(self)(self._projection + other._projection)

    def meets_constraint(self, node: EqNode) -> bool:
        produced = tuple(named.alias for named in node.get_signature())
        return produced == tuple(item.alias for item in self._projection)


class TagChannelConstraint(Constraint[EqNode]):
    """Pairs a builder that adds throwaway rows with one that removes them again.

    The idea: one builder copies each base row several times, marking one copy to keep, and its
    parent deletes the rest. Net effect is the base rows, but the engine had to build and
    filter a bigger table on the way.

    ``tag_col`` is the column holding the mark and ``keep_value`` the value that survives.

    The builder that adds rows both *requires* and supports this, so it can only be built
    underneath the one that removes them; the remover does not support it, so a second remover
    cannot appear in between. See ``builders/expansion.py``.
    """

    def __init__(self, tag_col: str, keep_value: int) -> None:
        self._tag_col = tag_col
        self._keep_value = keep_value

    @property
    def tag_col(self) -> str:
        return self._tag_col

    @property
    def keep_value(self) -> int:
        return self._keep_value

    def merge_constraint(self, other: Self) -> Self:
        return self

    def meets_constraint(self, node: EqNode) -> bool:
        return True


class KeyChannelConstraint(Constraint[EqNode]):
    """Like :class:`TagChannelConstraint`, but the extra copies share a key instead of a mark.

    Carries a :class:`KeySpec` for a key that is unique per base row and repeated across that
    row's copies, so the parent can collapse each key back to one row.

    Safer than the tag version with duplicate rows: two identical base rows still get different
    keys, so collapsing cannot merge two rows that were meant to stay two.
    """

    def __init__(self, key: KeySpec) -> None:
        self._key = key

    @property
    def key(self) -> KeySpec:
        return self._key

    def merge_constraint(self, other: Self) -> Self:
        return self

    def meets_constraint(self, node: EqNode) -> bool:
        return True


class ExposedNameConstraint(Constraint[EqNode]):
    """The object must be called ``name``.

    Set on the outermost object only, and set to the base table's own name, so the same query
    text runs on both sides::

        database 1:  CREATE TABLE t (...)                      -- the base
        database 2:  CREATE TABLE t__base (...)                -- base renamed aside
                     CREATE VIEW  t AS SELECT * FROM t__base   -- the equivalent, same name

    Not passed to children, which get names like ``t_view_1``. This one is really checked: the
    name has to have taken.
    """

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def merge_constraint(self, other: Self) -> Self:
        return self

    def meets_constraint(self, node: EqNode) -> bool:
        return not isinstance(node, EquivalentRelation) or node.materialized_name == self._name


def row_filter_of(constraint_set: object) -> Optional[ExpressionNode]:
    """The accumulated row-filter predicate, or ``None``. Convenience for builders."""
    from eqgen.builder.constraint_set import ConstraintSet

    assert isinstance(constraint_set, ConstraintSet)
    found = constraint_set.get_optional_constraint(RowFilterConstraint)
    return found.predicate if found is not None else None
