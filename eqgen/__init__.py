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

"""Finding SQL engine bugs by building a second object that holds the same rows.

Take a table. Build something else that holds exactly those rows — a view, a chain of views, a
copy, two halves unioned back together, a macro. Then run the same query against both::

    SELECT c_int FROM t      -- against the base table
    SELECT c_int FROM t      -- against the replacement, which is called t in its own database

Different answers mean an engine bug, because the rows are the same by construction.

Each rewrite keeps the rows, so any stack of them does too — which is why an object can be ten
layers deep and still be trusted as a comparison. Every layer is checked by running it, not by
argument.
"""
