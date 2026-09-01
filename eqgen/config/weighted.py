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

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from typing import Generic, TypeVar

T = TypeVar("T")


class Weighted(Generic[T]):
    """
    A Weighted option for use in sampling a weighted population of type T.

    Corresponds to the Gcl file weighted.gcl.
    """

    data_type: type[T]

    def __init__(self, value: T, weight: int, t: type[T]):
        """
        A Weighted option of type t.

        :param value: The value of the option
        :param weight: The relative weight in a population of options.
        :param t: The type of value
        """
        self.data_type = t
        self._value: T = value
        self._weight: int = weight

    def __repr__(self) -> str:
        return f"{{value: {self._value} weight: {self._weight}}}"

    @property
    def value(self) -> T:
        return self._value

    @property
    def weight(self) -> int:
        return self._weight


class Choices(Generic[T]):
    """
    Class for choosing from collection of Weighted options.
    """

    data_type: type[T]

    def __init__(self, options: Sequence[Weighted[T]], t: type[T]):
        """
        Create Choices class for the provided Weighted options of type t.
        :param options: Weighted options to choose between
        :param t: Type of the Weighted values
        """
        self.data_type = t
        self._options: Sequence[Weighted[T]] = options
        self._values: Sequence[T] = [o.value for o in options]
        self._weights: Sequence[int] = [o.weight for o in options]

    def __repr__(self) -> str:
        return f"[{', '.join(map(str, self.options))}]"

    @property
    def options(self) -> Sequence[Weighted[T]]:
        """
        :return: The Weighted options.
        """
        return self._options

    @property
    def values(self) -> Sequence[T]:
        """
        :return: The values in the Weighted options.
        """
        return self._values

    def choose(self, k: int = 1) -> list[T]:
        """Return *k* distinct values sampled without replacement, biased by weight.

        Higher-weight options are more likely to appear earlier in the result.
        No value will appear more than once.  Items with weight <= 0 are
        excluded (consistent with ``choose_one`` and ``weighted_shuffle``).

        :param k: Number of distinct values to return.  Must be ``<= len(eligible options)``.
        :return: The chosen values, ordered from highest effective priority to lowest.
        """
        eligible_count = sum(1 for w in self._weights if w > 0)
        assert k <= eligible_count, f"k={k} must be <= the number of positive-weight options ({eligible_count}).\n{self}"
        shuffled = weighted_shuffle(
            list(zip(self._values, self._weights)),
            lambda pair: pair[1],
        )
        return [v for v, _ in shuffled[:k]]

    def restrict_to(self, *allowed: T) -> Choices[T]:
        """Return a new ``Choices`` keeping only *allowed* values.

        Preserves the original GCL weights for the kept options.
        Asserts that every value in *allowed* exists in the original options.
        """
        allowed_set = set(allowed)
        available_set = set(self._values)
        unknown = allowed_set - available_set
        assert not unknown, f"Values {unknown} not found in available options {available_set}"
        kept = [o for o in self._options if o.value in allowed_set]
        assert kept, f"No options in {allowed} found in {self}"
        return Choices(kept, self.data_type)

    def choose_one(self) -> T:
        """Choose a single value from the weighted population."""
        return random.choices(population=self._values, weights=self._weights, k=1)[0]


def weighted_shuffle(items: list[T], weight_fn: Callable[[T], float]) -> list[T]:
    """Order *items* by weighted-random draw without replacement.

    Higher-weight items are more likely to appear earlier.  Items whose
    weight is zero or negative are excluded from the result.

    Uses the Gumbel/exponential-sort trick: for each eligible item draw
    ``u ~ Uniform(0,1)`` and compute ``key = -ln(u) / weight``.  Sorting
    by key ascending produces a weighted permutation in O(n log n) time
    with no floating-point accumulation issues.
    """
    eligible = [(item, w) for item in items if (w := weight_fn(item)) > 0]
    if not eligible:
        return []
    keyed = [(-math.log(1.0 - random.random()) / w, item) for item, w in eligible]
    keyed.sort(key=lambda pair: pair[0])
    return [item for _, item in keyed]


def random_sample(pool: Sequence[T], max_items: int, min_items: int = 1) -> list[T]:
    """Return a random-sized distinct subset of *pool*.

    Picks ``n`` uniformly from ``[min_items, min(max_items, len(pool))]``,
    then returns ``n`` items sampled without replacement.

    Returns an empty list when *pool* is empty.
    """
    if not pool:
        return []
    upper = min(max_items, len(pool))
    if min_items > upper:
        raise ValueError(
            f"min_items ({min_items}) exceeds upper bound ({upper}) = min(max_items={max_items}, pool_size={len(pool)})"
        )
    n = random.randint(min_items, upper)
    return random.sample(pool, n)
