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

import abc
from typing import ClassVar, Generic, Optional, Self, Sequence, Type, TypeVar

NodeTypeT = TypeVar("NodeTypeT")


class Constraint(Generic[NodeTypeT], abc.ABC):
    """
    A constraint is a set of constraints that must be met by a node.
    """

    # Carried from a parent set into a same-level child by ConstraintSet.derive().
    propagates_to_children: ClassVar[bool] = False

    @abc.abstractmethod
    def merge_constraint(self, other: "Self") -> "Self":
        """
        This should return the type of the constraint.
        """
        pass

    @abc.abstractmethod
    def meets_constraint(self, node: NodeTypeT) -> bool:
        """
        This should return True if the node meets the constraint, False otherwise.
        """
        pass


ConstraintNodeTypeT_co = TypeVar("ConstraintNodeTypeT_co", covariant=True)
ConstraintReturnTypeT = TypeVar("ConstraintReturnTypeT")


class ConstraintSet(Generic[ConstraintNodeTypeT_co]):
    __constraint_map: dict[Type[Constraint[ConstraintNodeTypeT_co]], Constraint[ConstraintNodeTypeT_co]]

    def __init__(self, constraints: Sequence[Optional[Constraint[ConstraintNodeTypeT_co]]]) -> None:
        self.__constraint_map: dict[Type[Constraint[ConstraintNodeTypeT_co]], Constraint[ConstraintNodeTypeT_co]] = {}
        for constraint in constraints:
            if constraint is None:
                continue
            if type(constraint) in self.__constraint_map:
                self.__constraint_map[type(constraint)] = self.__constraint_map[type(constraint)].merge_constraint(constraint)
            else:
                self.__constraint_map[type(constraint)] = constraint

    @classmethod
    def of(cls, *constraints: Optional[Constraint[ConstraintNodeTypeT_co]]) -> "ConstraintSet[ConstraintNodeTypeT_co]":
        """Build a ConstraintSet from individual constraints; ``None`` entries are skipped."""
        return cls(constraints)

    def derive(self, *additional: Optional[Constraint[ConstraintNodeTypeT_co]]) -> "ConstraintSet[ConstraintNodeTypeT_co]":
        """Build a same-level child set: carry forward every constraint marked
        ``propagates_to_children``, then append ``additional`` (``None`` entries
        skipped, merged on type collision via ``__init__``)."""
        propagated = [c for c in self.all_constraints() if c.propagates_to_children]
        return type(self)([*propagated, *additional])

    def all_constraints(self) -> Sequence[Constraint[ConstraintNodeTypeT_co]]:
        return list(self.__constraint_map.values())

    def get_constraint_types(self) -> Sequence[Type[Constraint[ConstraintNodeTypeT_co]]]:
        return list(self.__constraint_map.keys())

    def get_constraint(self, constraint_type: Type[ConstraintReturnTypeT]) -> ConstraintReturnTypeT:
        # TODO: We should validate that constraint types are unique.
        result = self.get_optional_constraint(constraint_type)
        if result is None:
            raise ValueError(f"Constraint of type {constraint_type} not found in constraint set")
        return result

    def get_optional_constraint(self, constraint_type: Type[ConstraintReturnTypeT]) -> Optional[ConstraintReturnTypeT]:
        return next((constraint for constraint in self.__constraint_map.values() if isinstance(constraint, constraint_type)), None)
