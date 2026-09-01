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

"""Names for an added key column, for a rewrite that copies rows and then removes the copies.

The two scopes, over a base table whose first two rows are identical::

    base rows        IDENTITY key      GROUPED key
    (1, 'a')         1                 1
    (1, 'a')         2                 2        -- a duplicate row, still its own key
    (2, 'b')         3                 3

An IDENTITY key is different for every base row, duplicates included, so collapsing by it keeps
two identical rows as two. Collapse by the row's values instead and they become one. A GROUPED
key is shared by the copies that should collapse together.

Only :class:`KeyChannelConstraint` uses this so far.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING


class KeyScope(enum.Enum):
    """Uniqueness scope of a synthesized key."""

    #: Distinct per base row — so duplicate base rows stay distinct through a reduction.
    IDENTITY = "IDENTITY"
    #: Shared across the copies of a base row that collapse to one.
    GROUPED = "GROUPED"


@dataclass(frozen=True)
class KeySpec:
    """A synthesized key column: name, uniqueness scope, and whether it is physical.

    ``physical`` distinguishes a key declared in DDL (a ``PRIMARY KEY``, which exists in the
    stored table and must therefore be projected away again so the equivalent's signature
    still matches the base) from a logical helper column that only ever appears mid-query.
    Getting that wrong does not produce wrong rows — it produces an extra *column*, which the
    comparison catches as a column-type difference rather than a row difference.
    """

    column: str
    scope: KeyScope
    physical: bool = False


if TYPE_CHECKING:
    from eqgen.equivalence.ast import EquivalentRelation, ProjectionItem


@dataclass(frozen=True)
class KeyedRelation:
    """A relation with one extra column: a key that tells its rows apart.

    Returned by :meth:`~eqgen.equivalence.builders.base.EquivalenceBuilder._materialize_row_key`::

        relation    CREATE TABLE t_table_1 AS
                      SELECT c_int, c_txt, ROW_NUMBER() OVER (ORDER BY c_int) AS eq_key_1
                      FROM t__base
        key         eq_key_1, distinct per row
        base_items  c_int, c_txt          <- the key is NOT in here

    ``base_items`` exists so a caller can project exactly the base columns back out without
    recomputing which ones they were. This relation deliberately does **not** have the base table's
    columns — it has one more — which is why a builder constructs it rather than asking for it.
    """

    relation: "EquivalentRelation"
    key: KeySpec
    base_items: tuple["ProjectionItem", ...]
