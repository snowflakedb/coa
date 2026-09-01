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

"""DuckDB catalogs and the adapter that ties the dialect to the harness.

The catalogs span the types DuckDB represents natively, chosen so the rewrites have something to
work with: two integer columns give the parity rewrites a key, a scaled decimal proves the *wrong*
kind of key is rejected, a double is present precisely because it must never be aggregated, and a
text column lets the codec rewrite fire.

**Execution defaults to the prebuilt CLI**, refreshed from ``artifacts.duckdb.org`` each fuzz run so
the harness tracks DuckDB ``main``. The Python wheel remains available as
``execution_backend="wheel"`` for offline unit tests that must not download a binary — and because
the wheel's statically linked engine cannot track ``main`` HEAD.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from eqgen.core.catalog import Column, Table
from eqgen.core.types import (
    DateType,
    DoubleType,
    IntegerType,
    NumericType,
    SqlType,
    TextType,
    TimestampType,
)
from eqgen.dialects.duckdb import cli
from eqgen.dialects.duckdb.builders import (
    DuckDBAddDropColumnTableBuilder,
    DuckDBAntiJoinEmptyRoundTripBuilder,
    DuckDBAsofLeftEmptyRoundTripBuilder,
    DuckDBExceptAllEmptyTableRoundTripBuilder,
    DuckDBAttachedDatabaseBuilder,
    DuckDBCheckpointTableBuilder,
    DuckDBCreateIndexBuilder,
    DuckDBCreateMacroBuilder,
    DuckDBUniqueIndexMatBuilder,
    DuckDBEnumTypeRoundTripBuilder,
    DuckDBListPackRoundTripBuilder,
    DuckDBListTransformRoundTripBuilder,
    DuckDBListFilterRoundTripBuilder,
    DuckDBMapPackRoundTripBuilder,
    DuckDBPivotStructRoundTripBuilder,
    DuckDBPositionalJoinRoundTripBuilder,
    DuckDBRecursiveCteIdentityBuilder,
    DuckDBRowNumberBoundQualifyBuilder,
    DuckDBSchemaQualifiedTableBuilder,
    DuckDBStarReplaceIdentityBuilder,
    DuckDBStructPackRoundTripBuilder,
)
from eqgen.dialects.duckdb.emitter import DuckDBEmitter, duckdb_type
from eqgen.equivalence.config import EquivalenceConfig, load_config
from eqgen.equivalence.emitter import SqlEmitter
from eqgen.fuzz.adapter import Connection, DialectAdapter

#: The dialect's own configuration, inheriting the portable one. See ``duckdb.gcl``.
_GCL_PATH = Path(__file__).resolve().parent / "duckdb.gcl"

#: ``c_pk`` is a unique seed key (see ``sample_rows``): PRIMARY KEY / UNIQUE Mat and fork joins
#: target it. Other columns may still intentionally duplicate across rows.
_SIMPLE_SPEC: list[tuple[str, SqlType]] = [
    ("c_pk", IntegerType()),
    ("id", IntegerType()),
    ("name", TextType()),
    ("created_at", TextType()),
]

#: Note what is **not** here: ``VarcharType`` and ``BooleanType``. On PostgreSQL any function
#: applied to a ``varchar`` returns ``text``, so a rewrite that touches the column changes its
#: declared type with no engine bug involved; DuckDB cannot tell the two apart either, so the
#: stable member of the pair is ``TextType``. ``BooleanType`` is omitted because PostgreSQL has
#: no ``MAX(boolean)`` / ``MIN(boolean)``, and the window / key-window rewrites would emit those.
#: ``NumericType`` and ``DoubleType`` are a different case and both stay.
_RICH_SPEC: list[tuple[str, SqlType]] = [
    ("c_pk", IntegerType()),
    ("c_int", IntegerType()),
    ("c_big", NumericType(38, 0)),  # a wide integer-valued decimal: still a valid parity key
    ("c_dec", NumericType(10, 2)),  # scaled: must be *rejected* as a parity key
    ("c_dbl", DoubleType()),  # present so the never-aggregate-a-double rule is exercised
    ("c_txt", TextType()),
    ("c_chr", TextType()),
    ("c_date", DateType()),
    ("c_ts", TimestampType()),
]


def _table(name: str, spec: list[tuple[str, SqlType]]) -> Table:
    columns = []
    for i, (column, data_type) in enumerate(spec, start=1):
        # ``c_pk`` is NOT NULL so ADD PRIMARY KEY does not need a separate nullability dance.
        nullable = column != "c_pk"
        columns.append(Column(column, data_type, i, nullable=nullable))
    return Table(name, columns)


def simple_catalog(name: str = "t") -> Table:
    """A minimal base table."""
    return _table(name, _SIMPLE_SPEC)


def rich_catalog(name: str = "t") -> Table:
    """A base table spanning DuckDB's representable primitive types."""
    return _table(name, _RICH_SPEC)


def duckdb_equivalence_config() -> EquivalenceConfig:
    """DuckDB's configuration, loaded from its own GCL file."""
    return load_config(_GCL_PATH, key="equivalence_generator_v3")


class DuckDBAdapter(DialectAdapter):
    """DuckDB, via the prebuilt CLI by default (``main``), or the Python wheel for tests.

    An in-memory database per connection, so a round is hermetic: there is no file path to collide
    on and nothing to clean up. (The version this replaces used a fixed ``/tmp`` path with no PID in
    it, which meant two runs could fight over one file — worth not inheriting.)
    """

    name = "duckdb"
    supports_float_inf = True

    def __init__(self, *, execution_backend: str = "cli") -> None:
        """*execution_backend* selects how :meth:`connect` runs SQL.

        ``"cli"`` (default, the fuzz path) drives the prebuilt ``duckdb`` CLI binary, whose engine
        tracks DuckDB ``main``. ``"wheel"`` keeps the in-process Python ``duckdb`` module and is
        used by offline unit tests (no binary download needed).
        """
        if execution_backend not in ("cli", "wheel"):
            raise ValueError(f"execution_backend must be 'cli' or 'wheel', got {execution_backend!r}")
        self._execution_backend = execution_backend
        import duckdb

        self._duckdb = duckdb
        self.db_error = duckdb.Error

    # --- generation ------------------------------------------------------------------

    def equivalence_config(self) -> EquivalenceConfig:
        return duckdb_equivalence_config()

    def emitter(self) -> SqlEmitter:
        return DuckDBEmitter()

    def extra_builders(self) -> tuple[type, ...]:
        return (
            DuckDBAntiJoinEmptyRoundTripBuilder,
            DuckDBAsofLeftEmptyRoundTripBuilder,
            DuckDBExceptAllEmptyTableRoundTripBuilder,
            DuckDBCreateMacroBuilder,
            DuckDBCreateIndexBuilder,
            DuckDBUniqueIndexMatBuilder,
            DuckDBAttachedDatabaseBuilder,
            DuckDBEnumTypeRoundTripBuilder,
            DuckDBSchemaQualifiedTableBuilder,
            DuckDBAddDropColumnTableBuilder,
            DuckDBCheckpointTableBuilder,
            DuckDBPositionalJoinRoundTripBuilder,
            DuckDBRecursiveCteIdentityBuilder,
            DuckDBRowNumberBoundQualifyBuilder,
            DuckDBStarReplaceIdentityBuilder,
            DuckDBPivotStructRoundTripBuilder,
            DuckDBListPackRoundTripBuilder,
            DuckDBListTransformRoundTripBuilder,
            DuckDBListFilterRoundTripBuilder,
            DuckDBMapPackRoundTripBuilder,
            DuckDBStructPackRoundTripBuilder,
        )

    # --- the physical database -------------------------------------------------------

    def connect(self) -> Connection:
        if self._execution_backend == "cli":
            return cli.connect_cli()
        connection: Connection = self._duckdb.connect()
        return connection

    def base_table_ddl(self, table: Table) -> str:
        parts = []
        for c in table.get_column_list():
            piece = f"{c.get_column_name()} {duckdb_type(c.get_data_type())}"
            if not c.get_is_nullable():
                piece += " NOT NULL"
            parts.append(piece)
        return f"CREATE TABLE {table.get_sql_name()} ({', '.join(parts)})"

    def literal(self, value: object) -> str:
        """A SQL literal for the seed ``INSERT``.

        Booleans are checked before numbers on purpose: ``bool`` is a subclass of ``int`` in Python,
        so testing numbers first would render ``True`` as ``1`` and quietly change a column's values.
        """
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, float):
            import math

            if math.isnan(value):
                return "'NaN'::DOUBLE"
            if math.isinf(value):
                return ("'Infinity'::DOUBLE" if value > 0 else "'-Infinity'::DOUBLE")
            return repr(value)
        if isinstance(value, int):
            return repr(value)
        escaped = str(value).replace("'", "''")
        return f"'{escaped}'"

    # --- diagnosis -------------------------------------------------------------------

    def engine_banner(self) -> str:
        """``duckdb <library_version> (<source_id>)`` — the build that produced a finding.

        Read from the engine itself (CLI binary, or the wheel under ``"wheel"``) so the banner
        reflects the loaded library and its source commit. Recorded in the startup banner and every
        repro header.
        """
        if self._execution_backend == "cli":
            library_version, source_id = cli.engine_version(cli.resolve_duckdb_cli())
            return f"duckdb {library_version} ({source_id})"
        row = self._duckdb.connect(":memory:").execute(
            "SELECT library_version, source_id FROM pragma_version()"
        ).fetchone()
        if row is None:
            return f"duckdb {self._duckdb.__version__} (python wheel, in-memory)"
        return f"duckdb {row[0]} ({row[1]}, python wheel)"

    def known_issue_label(self, exc: Exception) -> Optional[str]:
        """Demote errors the generator provokes and the engine is right to raise.

        A **Conversion Error** is row-level and plan-order-dependent — casting the string ``'on'``
        to ``BOOL``, say. Whether it surfaces at all depends on which rows a given plan evaluates,
        so it is not a dependable signal, and not an engine bug.

        The **invalidation** message is a cascade symptom: an earlier query already hit a fatal
        error and poisoned the connection, and every later statement reports this until the database
        is rebuilt. Demoting it clears the flood of one-sided errors that follow a single poisoning.
        Note it matches the invalidation text specifically and **not** a general fatal error — the
        *original* fatal error keeps its finding, which is the whole signal.
        """
        message = str(exc)
        if "Conversion Error" in message:
            return "duckdb-conversion-error"
        if "database has been invalidated" in message:
            return "duckdb-invalidated-after-fatal"
        # CLI-side cap (EQGEN_DUCKDB_STATEMENT_TIMEOUT). Same rationale as Postgres statement_timeout:
        # time-dependent, can hit one side only, not a result difference.
        if "duckdb statement timed out" in message:
            return "duckdb-statement-timeout"
        # Known join-order INTERNAL Error (filed: Operator occurrence N reconstructed more than once).
        # Demote so hunts keep looking for wrong-result / other crashes instead of re-filing.
        if "reconstructed more than once" in message:
            return "duckdb-join-order-reconstruct"
        if "Could not orient operator occurrence" in message:
            return "duckdb-join-order-reconstruct"
        # Filed: ASOF + EXCEPT ALL / INTERSECT ALL type mismatch
        # (repro/duckdb-20260810-except-all-asof-type-mismatch). Do not blanket-demote
        # Vector::Reference — other bugs share that message.
        if "ExpressionExecutor::Execute called with a result vector of type" in message and "does not match expression type" in message:
            return "duckdb-asof-except-all-type-mismatch"
        return None

    # --- catalogs --------------------------------------------------------------------

    def simple_catalog(self, name: str = "t") -> Table:
        return simple_catalog(name)

    def rich_catalog(self, name: str = "t") -> Table:
        return rich_catalog(name)

    def catalog_type_pool(self) -> Sequence[tuple[str, SqlType]]:
        return tuple((name, dtype) for name, dtype in _RICH_SPEC if name != "c_pk")
