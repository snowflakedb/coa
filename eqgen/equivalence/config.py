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

"""The settings builders read, loaded from a ``.gcl`` file.

``config/gcl/equivalence_generator_v3.gcl`` holds the values; :class:`EquivalenceConfig` is what
code reads. The file format is kept because an engine can inherit it and override one list::

    equivalence_generator_v3 = eqg3 { builder_weights = [ ... ]; };

There are no defaults in this file. Every value comes from the ``.gcl``, so a setting has exactly
one place it is defined. Tests that need a variant start from :func:`default_equivalence_config`
and use :func:`dataclasses.replace`.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from eqgen.config.gcl_compat import load
from eqgen.config.settings import (
    EquivalenceGeneratorV3Settings,
    JoinTypeChoice,
    KeySelection,
    WindowFrameChoice,
    WindowFunctionChoice,
)
from eqgen.config.weighted import Choices

#: The GCL data file backing :func:`default_equivalence_config`, resolved relative to this
#: package so it works regardless of the process's working directory.
_GCL_PATH = Path(__file__).resolve().parent.parent / "config" / "gcl" / "equivalence_generator_v3.gcl"


@dataclass(frozen=True)
class EquivalenceConfig:
    """Tunable knobs for one generation run.

    Attributes:
        max_depth: Builder-stack recursion limit. A single transform spans create -> query
            (-> nested relation) levels, so this counts more levels than the number of
            stacked rewrites.
        max_nodes: Surviving-tree node budget; triggers a leaf-only wind-down when
            exhausted. ``0`` means unlimited.
        max_attempts: Total nodes-built budget, *including* subtrees discarded when a parent
            failed — a work ceiling rather than a size cap. ``0`` means unlimited.
        builder_weights: ``{builder class name: weight}``. Weight ``0`` excludes a builder,
            which is how a dialect restricts itself to what it can run.
        root_builder_weights: Weights applied only to the root object; empty means "use
            ``builder_weights`` at the root too".
        key_selection_weights: How to pick a key column among eligible ones.
        join_type_weights: Which join keyword the flag-table join writes.
        window_function_weights: Which window function the window rewrite writes.
        window_frame_weights: Whether that rewrite writes an explicit frame, and which kind.
        big_table_rowcount_weights: How many copies the expand/reduce builders write per base
            row before pruning back to one.
    """

    max_depth: int
    max_nodes: int
    max_attempts: int
    builder_weights: dict[str, float]
    root_builder_weights: dict[str, float]
    key_selection_weights: Choices[KeySelection]
    join_type_weights: Choices[JoinTypeChoice]
    window_function_weights: Choices[WindowFunctionChoice]
    window_frame_weights: Choices[WindowFrameChoice]
    big_table_rowcount_weights: Choices[int]

    @classmethod
    def from_gcl(cls, settings: EquivalenceGeneratorV3Settings) -> "EquivalenceConfig":
        """Resolve GCL settings into a runtime config.

        Weighted knobs pass through as ``Choices`` rather than being flattened: builders call
        ``.choose_one()`` at the point of use, so there is no reimplementation of weighted
        selection here. Only the builder weights become a plain dict, because the factory's
        weighted *shuffle* consumes them rather than drawing one.
        """
        weights = {str(option.value): float(option.weight) for option in settings.builder_weights.options}
        root_weights = {str(option.value): float(option.weight) for option in settings.root_builder_weights.options}
        limits = settings.builder_settings
        return cls(
            max_depth=limits.max_depth,
            max_nodes=limits.max_nodes,
            max_attempts=limits.max_attempts,
            builder_weights=weights,
            root_builder_weights=root_weights,
            key_selection_weights=settings.key_selection_weights,
            join_type_weights=settings.join_type_weights,
            window_function_weights=settings.window_function_weights,
            window_frame_weights=settings.window_frame_weights,
            big_table_rowcount_weights=settings.big_table_rowcount_weights,
        )


def load_config(path: str | Path, *, key: Optional[str] = None) -> EquivalenceConfig:
    """Resolve a ``.gcl`` file into an :class:`EquivalenceConfig`.

    This is the seam a dialect uses: it points at its own file, which inherits the portable one and
    overrides the builder weights.

    *key* names a sub-tuple to descend into. GCL expresses inheritance by *naming* the derived
    tuple — ``equivalence_generator_v3 = eqg3 { … }`` — so a file that overrides the defaults has
    its settings one level down, while the base file has them at the top. Passing the key is how a
    caller says which shape it wrote.
    """
    model = load(str(path))
    root = model if key is None else model[key]
    gcl_dict = {name: root[name] for name in root.exportable_keys()}
    return EquivalenceConfig.from_gcl(EquivalenceGeneratorV3Settings(gcl_dict))


@functools.lru_cache(maxsize=1)
def default_equivalence_config() -> EquivalenceConfig:
    """The GCL-resolved default config, cached for the process.

    Cached because parsing GCL is not free and the result is immutable configuration.
    """
    return load_config(_GCL_PATH)
