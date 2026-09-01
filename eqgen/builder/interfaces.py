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
from typing import Generic, Optional, Type, TypeVar, cast

from eqgen.builder.builder_settings import BuilderSettings
from eqgen.builder.constraint_set import ConstraintSet
from eqgen.builder.type_variables import ContextTypeT, NodeTypeT


class BuilderFactoryT(Generic[ContextTypeT, NodeTypeT]):
    ResultNodeTypeT_co = TypeVar("ResultNodeTypeT_co", covariant=True)

    def build_subtree(
        self, type: Type[ResultNodeTypeT_co], constraint_set: ConstraintSet[NodeTypeT], context: ContextTypeT
    ) -> Optional[ResultNodeTypeT_co]:
        context.check_deadline()
        result = self._dispatch(cast(Type[NodeTypeT], type), constraint_set, context)

        if result is None:
            return None
        assert isinstance(result, type), f"Result {result} is not a {type}"
        return result

    @abc.abstractmethod
    def _dispatch(
        self, type: Type[NodeTypeT], constraint_set: ConstraintSet[NodeTypeT], context: ContextTypeT
    ) -> Optional[NodeTypeT]:
        """Filter eligible builders, order them, and try each one until one succeeds.

        Subclasses override this to control builder ordering (e.g. weighted
        shuffle, profiling instrumentation).
        """
        pass

    @property
    @abc.abstractmethod
    def settings(self) -> BuilderSettings:
        pass
