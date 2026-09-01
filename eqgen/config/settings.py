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

"""Typed accessors for the equivalence generator's GCL settings.

GCL (`Generic Configuration Language <https://github.com/rix0rrr/gcl>`_, MIT) is the source
of the generator's tunable knobs. This module is the typed bridge: ``gcl/*.gcl`` holds the
data, :class:`EquivalenceGeneratorV3Settings` reads it with types attached, and
:meth:`~eqgen.equivalence.config.EquivalenceConfig.from_gcl` resolves it into the runtime
dataclass builders consume.

**Why a config language rather than Python defaults.** GCL tuples inherit, so a dialect can
*declare* its own configuration by inheriting the base and overriding what differs:

.. code-block:: none

    equivalence_generator_v3 = eqg3 { builder_weights = [ ... ]; };

Weight ``0`` means "excluded", so that one declaration is how an engine restricts itself to
the transforms it can actually run — see ``dialects/duckdb/duckdb.gcl``. Note GCL lists
*replace* rather than merge, so an override restates the whole list; with a small builder set
that is a feature, since the allow-list ends up explicit rather than a set difference.

Knobs are exposed as ``Choices`` and stay that way through resolution: builders call
``.choose_one()`` at the use site. Pinning a knob to a single option therefore keeps a draw
deterministic, which is how a coverage payload can be added weight-0 without perturbing any
existing seed's output.
"""

from enum import StrEnum

from eqgen.config.base_gcl_tuple import BaseGclTuple, RequiredKeyProperty, this_method_name
from eqgen.config.builder_settings import GclBuilderSettings
from eqgen.config.weighted import Choices


class KeySelection(StrEnum):
    """How a builder picks a key column among the eligible ones.

    ``FIRST`` is deterministic and is the default; ``RANDOM`` trades that for coverage across
    seeds. Both are correct — which eligible column keys a partition never affects
    equivalence, only which rows land in which branch.
    """

    FIRST = "FIRST"
    RANDOM = "RANDOM"


class JoinTypeChoice(StrEnum):
    """Which join keyword the flag-table join writes.

    A one-to-one complete join returns the base rows whatever the type is — nothing is dropped and
    nothing is null-padded — so this is coverage of four operators from one identity.
    """

    INNER = "INNER"
    LEFT_OUTER = "LEFT OUTER"
    RIGHT_OUTER = "RIGHT OUTER"
    FULL_OUTER = "FULL OUTER"


class WindowFunctionChoice(StrEnum):
    """Which window function the window rewrite uses. Over a partition where every row shares the
    value, all four return that value, so the choice is coverage rather than correctness."""

    MIN = "MIN"
    MAX = "MAX"
    FIRST_VALUE = "FIRST_VALUE"
    LAST_VALUE = "LAST_VALUE"


class WindowFrameChoice(StrEnum):
    """Whether to write an explicit frame, and which kind.

    ``NONE`` leaves the default frame. ``ROWS`` and ``RANGE`` both name the whole partition, so the
    answer is the same and the plan need not be.
    """

    NONE = "NONE"
    ROWS = "ROWS"
    RANGE = "RANGE"


class EquivalenceGeneratorV3Settings(BaseGclTuple):
    """The generator's GCL settings, typed."""

    @RequiredKeyProperty
    def builder_settings(self) -> GclBuilderSettings:
        """Framework limits: recursion depth and the node/attempt budgets."""
        return self._value_as_gcl_tuple(this_method_name(), GclBuilderSettings)

    @RequiredKeyProperty
    def builder_weights(self) -> Choices[str]:
        """Selection weights keyed by builder class name.

        Weight ``0`` excludes a builder outright, which is the mechanism a dialect uses to
        restrict itself to what it can run.
        """
        return self._value_as_choices(this_method_name(), str)

    @RequiredKeyProperty
    def root_builder_weights(self) -> Choices[str]:
        """Weights applied only to the root object.

        The root is the drop-in replacement for the base table, so it is the one position
        where the choice of object kind is externally visible. Empty means "use
        ``builder_weights`` at the root too".
        """
        return self._value_as_choices(this_method_name(), str)

    @RequiredKeyProperty
    def key_selection_weights(self) -> Choices[KeySelection]:
        """Strategy for picking a key column: deterministic first, or a seeded pick."""
        return self._value_as_choices_enum(this_method_name(), KeySelection)

    @RequiredKeyProperty
    def join_type_weights(self) -> Choices[JoinTypeChoice]:
        """Which join keyword the flag-table join writes."""
        return self._value_as_choices_enum(this_method_name(), JoinTypeChoice)

    @RequiredKeyProperty
    def window_function_weights(self) -> Choices[WindowFunctionChoice]:
        """Which window function the window rewrite writes."""
        return self._value_as_choices_enum(this_method_name(), WindowFunctionChoice)

    @RequiredKeyProperty
    def window_frame_weights(self) -> Choices[WindowFrameChoice]:
        """Whether the window rewrite writes an explicit frame, and which kind."""
        return self._value_as_choices_enum(this_method_name(), WindowFrameChoice)

    @RequiredKeyProperty
    def big_table_rowcount_weights(self) -> Choices[int]:
        """How many copies the expand/reduce builders write per base row before pruning
        back to one. Row-neutral at any value — the choice trades setup/query cost (paid on
        every workload query run against the round) against exercising the collapse at scale."""
        return self._value_as_choices(this_method_name(), int)
