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

"""What kind of object an equivalent is: view, table, and so on.

Every member is something PostgreSQL has. The Snowflake-only kinds the earlier version carried
(``SECURE_VIEW``, ``DYNAMIC_TABLE``, ``MANAGED_ICEBERG`` and the rest) left along with their
nodes.

Nothing branches on this. Builders choose by constraint and the emitter renders whatever the
node's steps say; this is here so a log line or a report can name the kind.
"""

from __future__ import annotations

from enum import Enum, auto


class ObjectKind(Enum):
    """The kinds of object an equivalence can materialize."""

    TABLE = auto()
    TEMPORARY_TABLE = auto()
    UNLOGGED_TABLE = auto()
    VIEW = auto()
    TEMPORARY_VIEW = auto()
    MATERIALIZED_VIEW = auto()
