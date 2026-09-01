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

"""Shared typed GCL tuple for the generation builder framework.

``GclBuilderSettings`` is the GCL-side mirror of the runtime
``eqgen.builder.builder_settings.BuilderSettings`` base class: the
framework-level knobs (recursion depth, node/attempt budgets) that every
generator built on the builder framework shares.  Both the query generator and
the v3 equivalence generator expose it as a ``builder_settings`` sub-tuple and
override the values in their own GCL files.
"""

from eqgen.config.base_gcl_tuple import BaseGclTuple, RequiredKeyProperty, this_method_name


class GclBuilderSettings(BaseGclTuple):
    @RequiredKeyProperty
    def max_depth(self) -> int:
        """Builder-stack recursion limit."""
        return self._value_as(this_method_name(), int)

    @RequiredKeyProperty
    def max_nodes(self) -> int:
        """Surviving-tree node budget; triggers leaf-only mode when exhausted.  0 = unlimited."""
        return self._value_as(this_method_name(), int)

    @RequiredKeyProperty
    def max_attempts(self) -> int:
        """Total nodes-built budget (incl. discarded subtrees); a work/thrash ceiling.  0 = unlimited."""
        return self._value_as(this_method_name(), int)
