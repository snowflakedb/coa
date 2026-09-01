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

"""What stays the same for one whole generation: the base table, the name minter, the settings,
the predicate source.

The row filter is deliberately **not** here. It travels as a constraint instead, because it
applies to one branch and not its siblings::

    even branch:  WHERE MOD(c_int, 2) = 0
    odd branch:   WHERE MOD(c_int, 2) <> 0 OR c_int IS NULL

Put it on the context and the two branches would share one value, and the object would lose or
duplicate rows.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Optional

from eqgen.builder.context import BuilderContext
from eqgen.core.catalog import Table
from eqgen.equivalence.config import EquivalenceConfig
from eqgen.plugins import PredicateSource


class NameGenerator:
    """Mints unique object names within a run.

    Instance-scoped, with no class-level state. The original kept a class-level dict, which
    made generated names depend on how many runs had happened earlier in the process — a
    frequent source of tests that passed alone and failed in a suite.

    Counters are kept *per prefix*, so kind-scoped names (``t_view_1``, ``t_table_1``) each
    start at one instead of sharing an opaque global sequence.
    """

    def __init__(self) -> None:
        self._object_counters: dict[str, int] = {}

    def generate_object_name(self, prefix: str) -> str:
        count = self._object_counters.get(prefix, 0) + 1
        self._object_counters[prefix] = count
        return f"{prefix}_{count}"

    def generate_column_name(self, prefix: str) -> str:
        """A name for a column a rewrite synthesizes: ``eq_tmp_col_1``, ``eq_key_1``.

        Shares the counters with the object names, so a synthesized column can never collide with
        a generated object name or with another synthesized column.
        """
        return self.generate_object_name(prefix)


class ObjectNamer:
    """Mints readable, kind-scoped names for one run: ``<base>_<kind>_<n>``.

    So emitted SQL reads ``CREATE VIEW t_view_1 …`` rather than ``eq_obj_7``. That matters
    more than it sounds: these names end up in saved repro scripts that a human reads while
    deciding whether a finding is real.
    """

    def __init__(self, base: str, names: NameGenerator) -> None:
        self._base = base
        self._names = names

    def mint(self, kind: str) -> str:
        return self._names.generate_object_name(f"{self._base}_{kind}")


class EquivalenceContext(BuilderContext):
    """What one generation run needs: the ``base_table`` every object reproduces, the weights in
    ``config``, a name minter, and optionally a ``predicate_source``.

    The predicate source being optional matters. Without one, the three-way row split declines and
    the always-true ``CASE`` builder falls back to a predicate it writes itself, so a run still
    happens with nothing installed beyond DuckDB.
    """

    def __init__(
        self,
        config: EquivalenceConfig,
        base_table: Table,
        *,
        predicate_source: Optional[PredicateSource] = None,
        deadline: Optional[datetime] = None,
        name_generator: Optional[NameGenerator] = None,
    ) -> None:
        super().__init__(deadline=deadline)
        self._config = config
        self._base_table = base_table
        self._predicate_source = predicate_source
        #: Where each predicate embedded in this object came from, by source name — plus ``declined``
        #: when the plugin returned nothing and ``fallback`` when a builder used its own.
        #:
        #: Worth recording because a predicate is opaque *text* by the time it reaches the DDL, so the
        #: emitted SQL cannot tell you who wrote it. A literal like ``o'brien`` is a hard-coded member of
        #: ``example_generator``'s pool, but working that out by eye means recognising a generator's
        #: style, which is not a reasonable thing to ask of a log.
        self.predicate_origin: Counter[str] = Counter()
        # A caller generating several equivalences into one schema passes a shared generator so
        # intermediate names stay unique across them; the default is a fresh per-run counter,
        # which is what keeps single-table generation deterministic.
        self._names = name_generator if name_generator is not None else NameGenerator()

    @property
    def config(self) -> EquivalenceConfig:
        return self._config

    @property
    def base_table(self) -> Table:
        return self._base_table

    @property
    def predicate_source(self) -> Optional[PredicateSource]:
        """The plugin supplying generated predicates, or ``None`` (builders then decline)."""
        return self._predicate_source

    @property
    def names(self) -> NameGenerator:
        return self._names

    @property
    def namer(self) -> ObjectNamer:
        """A kind-scoped namer bound to this run's base table."""
        return ObjectNamer(self._base_table.table_name, self._names)
