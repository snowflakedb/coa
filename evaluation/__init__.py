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

"""Measuring how much of an engine eqgen reaches. Not part of the tool — a measurement of it.

One import rule, checked by ``eqgen/tests/boundaries_test.py``: **this package may import the core;
nothing in the core may import this package.** So there is no ``--coverage`` flag on
``eqgen.fuzz.cli``; there is a separate entry point here that drives the same round loop::

    python -m evaluation.coverage.campaign --dialect postgres --rounds 200

Layout:

* :mod:`evaluation.coverage` — gcov campaigns, reporting, metamorphic coverage
* :mod:`evaluation.plans` — distinct Postgres query-plan count (``--track-plans``)

What is measured is line and branch coverage of the engine's C source (and optionally distinct
plans), which is what the DBMS-testing literature reports. It says how varied the generated
workload is, not how good it is at finding bugs — those two come apart, and the paper this was
modelled on says so itself.
"""
