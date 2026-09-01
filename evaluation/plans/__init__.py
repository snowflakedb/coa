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

"""Distinct query-plan count for evaluation campaigns (Postgres, QPG-style).

Enabled with ``--track-plans`` on the coverage campaign. Fingerprints ride the forked
worker via a callback so :mod:`eqgen.fuzz` never imports this package.
"""

from evaluation.plans.tracker import PlanTracker

__all__ = ["PlanTracker"]
