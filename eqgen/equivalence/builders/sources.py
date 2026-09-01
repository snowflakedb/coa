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

"""The base table itself — where a chain of rewrites stops::

    SELECT * FROM t_view_2      -- factory chose another rewrite: one level deeper
    SELECT * FROM t__base       -- factory chose this builder: chain ends here

Nothing else can end a chain, so this is the one builder no configuration should set to weight
zero.
"""

from __future__ import annotations

from typing import Optional, Type

from eqgen.builder.constraint_set import Constraint, ConstraintSet
from eqgen.equivalence.ast import BaseTableSource, EqNode
from eqgen.equivalence.builders.base import EquivalenceBuilder
from eqgen.equivalence.constraints import SingleSourceConstraint
from eqgen.equivalence.context import EquivalenceContext


class BaseTableSourceBuilder(EquivalenceBuilder[BaseTableSource]):
    """The base table as a FROM source — the terminating leaf."""

    @property
    def is_leaf(self) -> bool:
        return True

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        # The base table is the canonical single source, so it honours SingleSource: a body
        # restricted to one table resolves its FROM to this, never to a chain.
        return [SingleSourceConstraint]

    def _build(self, constraint_set: ConstraintSet[EqNode], context: EquivalenceContext) -> Optional[BaseTableSource]:
        del constraint_set
        return BaseTableSource(context.base_table)
