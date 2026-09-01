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

"""The base class every rewrite inherits, and the helpers they share.

The one rule: **never name the class of a child**. Ask the factory for a *kind* of thing and
take whatever it gives back::

    source = self._dispatch_source(context)     -- may be the base table:
                                                --   SELECT * FROM t
                                                -- or three rewrites deep:
                                                --   SELECT * FROM t_view_3

Both are the same line of code, so a new rewrite immediately works inside every existing one
without anybody listing the combinations.
"""

from __future__ import annotations

import abc
import random
from typing import Callable, Optional, Type, TypeVar

from eqgen.builder.builder import NodeBuilder
from eqgen.builder.constraint_set import Constraint, ConstraintSet
from eqgen.builder.type_variables import ResultT_co
from eqgen.config.settings import KeySelection
from eqgen.core.types import IntegerType, SqlType
from eqgen.equivalence.ast import EqNode, EquivalentRelation, EquivalentSource, ProjectionItem, SelectQuery
from eqgen.equivalence.constraints import (
    AcceptsDmlConstraint,
    ColumnRewriteConstraint,
    ExposedNameConstraint,
    RowFilterConstraint,
    SingleSourceConstraint,
)
from eqgen.equivalence.context import EquivalenceContext
from eqgen.equivalence.keys import KeyedRelation, KeyScope, KeySpec
from eqgen.ir import expr
from eqgen.ir.expr import ExpressionNode

_T = TypeVar("_T")


class EquivalenceBuilder(NodeBuilder[EquivalenceContext, EqNode, ResultT_co]):
    """Base for every rewrite. With an empty constraint set the job is "return all the base
    table's rows"; anything narrower arrives as a constraint from the builder above."""

    def _dispatch_source(
        self, context: EquivalenceContext, constraint_set: Optional[ConstraintSet[EqNode]] = None
    ) -> Optional[EquivalentSource]:
        """Something to put after ``FROM`` — the base table, or another rewrite.

        Whatever comes back holds **all** the base rows; no filter is pushed into it. Your query
        puts its own ``WHERE`` on top::

            SELECT * FROM <whatever came back> WHERE MOD(c_int, 2) = 0

        So your rewrite is correct regardless of what it ends up reading from.

        Only :class:`SingleSourceConstraint` is passed down, which leaves just the base table.
        """
        forwarded: list[Optional[Constraint[EqNode]]] = []
        if constraint_set is not None:
            forwarded.append(constraint_set.get_optional_constraint(SingleSourceConstraint))
        return self.builder_factory.build_subtree(
            EquivalentSource,  # type: ignore[type-abstract]
            ConstraintSet(forwarded),
            context,
        )

    @staticmethod
    def _current_filter(constraint_set: ConstraintSet[EqNode]) -> Optional[ExpressionNode]:
        """The filter this build was asked for, or ``None``. Put it in your ``WHERE``."""
        found = constraint_set.get_optional_constraint(RowFilterConstraint)
        return found.predicate if found is not None else None

    @staticmethod
    def _exposed_name(constraint_set: ConstraintSet[EqNode]) -> Optional[str]:
        """The name the outermost object must take (the base table's), or ``None`` — then take a
        generated name like ``t_view_1``."""
        found = constraint_set.get_optional_constraint(ExposedNameConstraint)
        return found.name if found is not None else None

    @staticmethod
    def _passthrough_items(context: EquivalenceContext) -> tuple[ProjectionItem, ...]:
        """One item per base column, unchanged, in declaration order::

            c_int, c_big, c_txt          -- these, as a projection

        Keep the order. ``SELECT *`` shows column order, so shuffling them is a difference even
        when every row survives.
        """
        return tuple(
            ProjectionItem(
                column.get_column_name(), expr.col(column.get_column_name(), column.get_data_type()), column.get_data_type()
            )
            for column in context.base_table.get_column_list()
        )

    @staticmethod
    def _select_key(candidates: list[_T], context: EquivalenceContext) -> _T:
        """Pick one of *candidates*, per config: the first one, or a random one.

        Either is correct. Which column a split uses changes which rows go to which branch, not
        whether the branches together cover all of them. Callers must pass a non-empty list.
        """
        strategy = context.config.key_selection_weights.choose_one()
        if strategy == KeySelection.RANDOM and len(candidates) > 1:
            return random.choice(candidates)
        return candidates[0]

    def _materialize_row_key(self, context: EquivalenceContext, key_name: str) -> Optional[KeyedRelation]:
        """Ask for the base rows with a key column added, and return all three parts::

            CREATE TABLE t_table_1 AS
              SELECT c_int, c_txt, ROW_NUMBER() OVER (ORDER BY c_int) AS eq_key_1 FROM t__base

        Asked for rather than built, so the relation is drawn from the pool like any other child: it
        comes back as a ``CREATE TABLE`` or a ``CREATE TEMPORARY TABLE`` today, and picks up any
        writable kind added later.

        Both halves of the request matter. ``ColumnRewriteConstraint`` is what makes "exactly these
        columns" sayable at all — without it there is no way to ask for a relation the base table's
        shape does not describe. ``AcceptsDmlConstraint`` means "something you can write to", i.e. a
        table: over a view, two readers could evaluate ``ROW_NUMBER()`` separately and number the
        rows differently, and a join on that key would then match the wrong rows.

        The ordering does not need to be a total one. The key exists to tell rows apart, so ties
        change which row gets which number and nothing else.

        Returns ``None`` when the base table has no columns to order by.
        """
        base_items = self._passthrough_items(context)
        if not base_items:
            return None
        order_by = expr.col(base_items[0].alias, base_items[0].data_type)
        key_item = ProjectionItem(key_name, expr.row_number((order_by,)), IntegerType())
        keyed = self.builder_factory.build_subtree(
            EquivalentRelation,  # type: ignore[type-abstract]
            ConstraintSet(
                [
                    # Exactly these columns: the base ones, plus the key.
                    ColumnRewriteConstraint((*base_items, key_item)),
                    # And written down, not a view. ROW_NUMBER() has to be evaluated once or two
                    # readers could number the rows differently; asking for something writable is
                    # how that is said in the constraint vocabulary.
                    AcceptsDmlConstraint(),
                ]
            ),
            context,
        )
        if not isinstance(keyed, EquivalentRelation):
            return None
        return KeyedRelation(relation=keyed, key=KeySpec(key_name, KeyScope.IDENTITY), base_items=base_items)

    @staticmethod
    def _generated_predicate(context: EquivalenceContext) -> Optional[ExpressionNode]:
        """A predicate from the plugin, or ``None`` if none is configured — then decline, or fall
        back to something you build yourself.

        Each call gets a different predicate, and the whole round still replays from its one
        seed.
        """
        source = context.predicate_source
        if source is None:
            context.predicate_origin["none-configured"] += 1
            return None
        text = source.boolean_predicate(context.base_table, seed=random.randrange(2**31))
        if not text:
            # A starved streaming source lands here, and it matters: the builder either declines or uses
            # its own fallback, so the object silently stops reflecting the source that was asked for.
            context.predicate_origin[f"{source.name}-declined"] += 1
            return None
        context.predicate_origin[source.name] += 1
        return expr.generated_predicate(text)


class ColumnRewriteQueryBuilder(EquivalenceBuilder[SelectQuery], abc.ABC):
    """Base for rewrites that replace each column with an expression giving the same value::

        SELECT <expression over c_int> AS c_int, c_txt FROM t
                                                --^^ this one returned None, so it passes through

    Implement :meth:`_column_rewriter`. It is called once per build, so set up there and close
    over the result — that way one predicate is shared by every column instead of each getting
    its own.

    Declines if no column was rewritten: an object identical to the base is not worth a round.
    """

    @abc.abstractmethod
    def _column_rewriter(self, context: EquivalenceContext) -> Callable[[str, SqlType], Optional[ExpressionNode]]:
        """Return a per-column rewrite ``(name, type) -> expr | None`` (``None`` = passthrough)."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return []

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[SelectQuery]:
        rewrite = self._column_rewriter(context)
        items: list[ProjectionItem] = []
        any_rewritten = False
        for column in context.base_table.get_column_list():
            name = column.get_column_name()
            data_type = column.get_data_type()
            rewritten = rewrite(name, data_type)
            if rewritten is None:
                items.append(ProjectionItem(name, expr.col(name, data_type), data_type))
            else:
                items.append(ProjectionItem(name, rewritten, data_type))
                any_rewritten = True
        if not any_rewritten:
            return None
        source = self._dispatch_source(context)
        if source is None:
            return None
        return SelectQuery(source, items)
