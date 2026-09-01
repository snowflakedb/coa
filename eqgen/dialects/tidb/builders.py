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

"""TiDB-only builders."""

from __future__ import annotations

from typing import Optional, Type

from eqgen.builder.constraint_set import Constraint
from eqgen.dialects.tidb.ast import TiCreateCachedTable
from eqgen.equivalence.ast import CreateTable, EqNode, QueryNode
from eqgen.equivalence.builders.creates import CreateFromQueryBuilder
from eqgen.equivalence.context import EquivalenceContext


def _out_cols(query: QueryNode) -> list[str]:
    return [named.alias for named in query.get_signature()]


class TiDbCachedTableBuilder(CreateFromQueryBuilder[TiCreateCachedTable]):
    """``CREATE TABLE`` + ``INSERT`` + ``ALTER TABLE ... CACHE``, exposed through a view."""

    def supported_constraint_types(self) -> list[Type[Constraint[EqNode]]]:
        return self._READ_ONLY_SUPPORTED

    def _wrap(
        self, query: QueryNode, context: EquivalenceContext, exposed_name: Optional[str]
    ) -> Optional[TiCreateCachedTable]:
        out_cols = _out_cols(query)
        if not out_cols:
            return None
        body = CreateTable.build(context.namer, query)
        return TiCreateCachedTable.build(
            context.namer,
            body,
            out_cols=out_cols,
            exposed_name=exposed_name,
        )
