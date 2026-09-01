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

from typing import TypeVar

from eqgen.builder.context import BuilderContext

"""
Type variables so that we can define a generic builder framework.

NodeTypeT: The broad node family (e.g. ``Node``).  Used for constraint
    sets and factory collections.
ContextTypeT: The type of context that the builder requires.
ResultT_co: The *specific* AST node type a builder produces.  Covariant
    so ``NodeBuilder[C, N, Literal]`` is a subtype of
    ``NodeBuilder[C, N, Node]``, allowing heterogeneous builder lists.
    Appears only in return positions (``result_type``, ``_build``).
"""
NodeTypeT = TypeVar("NodeTypeT")
ContextTypeT = TypeVar("ContextTypeT", bound=BuilderContext)
ResultT_co = TypeVar("ResultT_co", covariant=True)
