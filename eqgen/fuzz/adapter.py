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

"""Everything the harness needs to know about one engine.

The harness itself knows no SQL: it builds both databases, runs the queries, compares, repeats.
Anything engine-specific goes behind this class — how to connect, how to write a ``CREATE TABLE``,
how to write a literal, which errors are not bugs.

To add an engine, subclass this and fill in the abstract methods. ``dialects/duckdb/adapter.py``
is the one to copy from.

An earlier version had twenty-seven members here; thirteen of them were for driving an external
generator tool and for translating SQL between engines. Both are gone: queries now arrive as text
from a plugin, and each engine writes its own SQL.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional, Protocol, Sequence

from eqgen.core.catalog import Table
from eqgen.core.types import SqlType
from eqgen.equivalence.config import EquivalenceConfig
from eqgen.equivalence.emitter import SqlEmitter


class Connection(Protocol):
    """The minimal DB-API surface the harness needs.

    ``execute`` returning something with ``fetchall``, and ``close``. Structural rather than a
    base class, so a dialect can hand back a driver connection, a wrapper around a subprocess, or
    an HTTP client without any of them inheriting from us.

    ``reset`` is optional and declared rather than discovered: a mid-result decode failure can
    leave unread packets on a socket, and every later query in the round then fails spuriously. A
    dialect that can cheaply re-establish a session implements it; one that cannot omits it. The
    original looked for this with ``getattr``, which meant the capability existed but was invisible
    to anyone reading the protocol.
    """

    def execute(self, sql: str, /) -> Any: ...

    def close(self) -> None: ...


class DialectAdapter(ABC):
    """What the harness needs to know about one engine."""

    #: Short identifier, e.g. ``"duckdb"``. Used in log lines and CLI selection.
    name: str

    #: Whether seed ``DOUBLE`` columns may include IEEE ±Inf. Postgres and DuckDB spell Inf;
    #: MySQL-family adapters reject Inf literals, so they keep this ``False``.
    supports_float_inf: bool = False

    #: The driver exception class the harness catches around statement execution, so a SQL failure
    #: is *recorded* rather than raised. Getting this wrong turns a finding into a crashed run.
    db_error: type[Exception] = Exception

    #: Function names whose presence makes a query non-comparable across two executions. Used as a
    #: cheap text guard on plugin-supplied predicates; a source is expected not to emit them, and
    #: this is the backstop.
    nondeterministic_funcs: frozenset[str] = frozenset()

    # --- generation ------------------------------------------------------------------

    @abstractmethod
    def equivalence_config(self) -> EquivalenceConfig:
        """The configuration restricting generation to what this engine can run.

        Typically loaded from the dialect's own ``.gcl`` file, which inherits the portable one and
        overrides the builder weights.
        """

    def emitter(self) -> SqlEmitter:
        """The emitter that renders an equivalence into this dialect's SQL.

        The default is the portable (PostgreSQL-spelled) emitter, which is genuinely usable: the
        shipped portable builders emit SQL that runs unmodified on more than one engine. A dialect
        overrides this to add native constructs and to swap the expression spelling.
        """
        return SqlEmitter()

    def extra_builders(self) -> tuple[type, ...]:
        """Builders this dialect contributes on top of the portable set.

        This is how an engine adds a rewrite with no portable counterpart. Default: none.
        """
        return ()

    # --- the physical database -------------------------------------------------------

    @abstractmethod
    def connect(self) -> Connection:
        """Open a fresh, empty database."""

    @abstractmethod
    def base_table_ddl(self, table: Table) -> str:
        """``CREATE TABLE`` for the base table, in this dialect's own type names."""

    @abstractmethod
    def literal(self, value: object) -> str:
        """Render *value* as a SQL literal, for the seed ``INSERT`` and for repro text."""

    def rename_aside_sql(self, name: str, hidden: str) -> str:
        """Move the base table out of the way so the equivalent can take its name.

        This is what lets the workload run byte-identical text against both sides. Standard SQL by
        default; override if the dialect spells it differently.
        """
        return f"ALTER TABLE {name} RENAME TO {hidden}"

    def fork_copy_sql(self, seed_name: str, exposed_name: str) -> list[str]:
        """Copy the seeded base table to another exposed name (same-base forks).

        Default is ``CREATE TABLE … AS SELECT *`` (Postgres/MySQL/DuckDB). Dialects without CTAS
        (TiDB) override with typed ``CREATE`` + ``INSERT … SELECT``.
        """
        return [f"CREATE TABLE {exposed_name} AS SELECT * FROM {seed_name}"]

    def teardown_statements(self, statements: Sequence[str]) -> None:
        """Dialect-specific cleanup after a round (temp files, etc.). Default: no-op."""
        del statements

    # --- diagnosis -------------------------------------------------------------------

    def engine_banner(self) -> str:
        """One line describing the engine actually under test, for logs and repro headers.

        Override it to name the **build**, not just the product. "MISMATCH on PostgreSQL" is nearly
        useless to whoever has to act on it; "MISMATCH on PostgreSQL 20devel, assertions on" is a
        bug report.
        """
        return self.name

    def session_context(self) -> list[tuple[str, str]]:
        """``(label, value)`` pairs describing the session a finding was produced under.

        Stamped into every repro header, because some settings silently decide results — a session
        collation or SQL mode can change whether two values compare equal. Best-effort: a failure
        to read this must never abort a run.
        """
        return []

    def known_issue_label(self, exc: Exception) -> Optional[str]:
        """A short label if *exc* is a **known non-bug**, else ``None``.

        For errors the generator provokes and the engine is right to raise — a genuinely
        out-of-range argument, a documented version limitation. Such a failure is demoted from a
        finding to "skipped" and tallied per label.

        Keep each signature narrow. This is the one classification that can make a real bug vanish
        silently, and a broad match here is indistinguishable from a fix.
        """
        del exc
        return None

    # --- catalogs --------------------------------------------------------------------

    @abstractmethod
    def simple_catalog(self, name: str = "t") -> Table:
        """A minimal base table — the harness default."""

    @abstractmethod
    def rich_catalog(self, name: str = "t") -> Table:
        """A base table spanning the types this dialect can represent."""

    def catalog_type_pool(self) -> Sequence[tuple[str, SqlType]]:
        """``(preferred_name, type)`` pairs used when sampling a per-round catalog.

        Excludes the integrity key ``c_pk`` (always added by :func:`~eqgen.fuzz.journal.sample_catalog`).
        Default: empty — dialects with a rich spec override.
        """
        return ()
