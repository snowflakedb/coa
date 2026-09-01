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

class BuilderSettings:
    """
    Root class for all builder settings.

    ``max_nodes`` caps the size of the *surviving* tree (nodes rolled back when
    a parent build fails do not count).  ``max_attempts`` caps the total nodes
    *built* over the whole run, including subtrees later discarded by a failed
    parent -- a work/thrash ceiling, not a tree-size cap.  When either limit is
    reached the factory switches to leaf-only mode, gracefully winding down the
    tree.  Default of 0 means unlimited (for both).
    """

    max_depth: int
    max_nodes: int = 0
    max_attempts: int = 0
