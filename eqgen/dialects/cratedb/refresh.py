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

"""Write fence policy for CrateDB eventual consistency.

Pure — no connection, no I/O — so classify and :class:`FenceState` can be tested offline.
One ``REFRESH TABLE`` per dirty table; reads flush everything dirty; ``INSERT ... VALUES`` skips
pre-flush.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import Optional


class Verb(enum.Enum):
    INSERT_VALUES = "insert_values"
    WRITE = "write"
    READ = "read"
    RENAME = "rename"
    CREATE_TABLE = "create_table"
    CREATE_VIEW = "create_view"
    DROP = "drop"
    OPAQUE = "opaque"


@dataclass(frozen=True)
class Effect:
    verb: Verb
    targets: frozenset[str] = frozenset()
    renamed_to: Optional[str] = None

    @property
    def may_read(self) -> bool:
        return self.verb is not Verb.INSERT_VALUES


_LEADING_NOISE = re.compile(r"\A(?:\s|--[^\n]*\n|/\*.*?\*/)+", re.DOTALL)

_IDENT = r'(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*)'
_QUALIFIED = rf"(?:{_IDENT}\s*\.\s*)?{_IDENT}"

_INSERT = re.compile(rf"\AINSERT\s+INTO\s+(?P<t>{_QUALIFIED})", re.IGNORECASE)
_INSERT_VALUES = re.compile(
    rf"\AINSERT\s+INTO\s+{_QUALIFIED}\s*(?:\([^()]*\)\s*)?VALUES\b",
    re.IGNORECASE,
)
_UPDATE = re.compile(rf"\AUPDATE\s+(?P<t>{_QUALIFIED})", re.IGNORECASE)
_DELETE = re.compile(rf"\ADELETE\s+FROM\s+(?P<t>{_QUALIFIED})", re.IGNORECASE)
_COPY_FROM = re.compile(rf"\ACOPY\s+(?P<t>{_QUALIFIED})\s+FROM\b", re.IGNORECASE)
_RENAME = re.compile(
    rf"\AALTER\s+TABLE\s+(?P<t>{_QUALIFIED})\s+RENAME\s+TO\s+(?P<to>{_QUALIFIED})",
    re.IGNORECASE,
)
_CREATE_TABLE = re.compile(rf"\ACREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<t>{_QUALIFIED})", re.IGNORECASE)
_CREATE_VIEW = re.compile(rf"\ACREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+(?P<t>{_QUALIFIED})", re.IGNORECASE)
_DROP = re.compile(rf"\ADROP\s+(?:TABLE|VIEW)\s+(?:IF\s+EXISTS\s+)?(?P<t>{_QUALIFIED})", re.IGNORECASE)
_READ_ONLY = re.compile(
    r"\A(?:SELECT|WITH|SHOW|EXPLAIN|ANALYZE|REFRESH|SET|RESET|BEGIN|COMMIT|ROLLBACK|DEALLOCATE)\b",
    re.IGNORECASE,
)
_CTAS = re.compile(rf"\ACREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?{_QUALIFIED}\s+AS\b", re.IGNORECASE)


def normalize(name: str) -> str:
    name = re.sub(r"\s*\.\s*", ".", name.strip())
    return ".".join(p if p.startswith('"') else p.lower() for p in name.split("."))


def classify(sql: str) -> Effect:
    stripped = _LEADING_NOISE.sub("", sql).strip().rstrip(";").strip()
    if not stripped:
        return Effect(Verb.READ)

    if m := _RENAME.match(stripped):
        return Effect(
            Verb.RENAME,
            frozenset({normalize(m.group("t"))}),
            renamed_to=normalize(m.group("to")),
        )
    if m := _CREATE_VIEW.match(stripped):
        return Effect(Verb.CREATE_VIEW, frozenset({normalize(m.group("t"))}))
    if _CTAS.match(stripped):
        m = _CREATE_TABLE.match(stripped)
        assert m is not None
        return Effect(Verb.WRITE, frozenset({normalize(m.group("t"))}))
    if m := _CREATE_TABLE.match(stripped):
        return Effect(Verb.CREATE_TABLE, frozenset({normalize(m.group("t"))}))
    if m := _DROP.match(stripped):
        return Effect(Verb.DROP, frozenset({normalize(m.group("t"))}))
    if m := _INSERT.match(stripped):
        target = frozenset({normalize(m.group("t"))})
        return Effect(Verb.INSERT_VALUES if _INSERT_VALUES.match(stripped) else Verb.WRITE, target)
    for pattern in (_UPDATE, _DELETE, _COPY_FROM):
        if m := pattern.match(stripped):
            return Effect(Verb.WRITE, frozenset({normalize(m.group("t"))}))
    if _READ_ONLY.match(stripped):
        return Effect(Verb.READ)
    return Effect(Verb.OPAQUE)


@dataclass
class FenceState:
    tables: set[str] = field(default_factory=set)
    views: set[str] = field(default_factory=set)
    dirty: set[str] = field(default_factory=set)

    def before(self, sql: str) -> list[str]:
        effect = classify(sql)
        if not effect.may_read or not self.dirty:
            return []
        return [f"REFRESH TABLE {name}" for name in sorted(self.dirty)]

    def after(self, sql: str) -> None:
        effect = classify(sql)
        if effect.verb is Verb.INSERT_VALUES or effect.verb is Verb.WRITE:
            self.tables.update(effect.targets - self.views)
            self.dirty.update(effect.targets - self.views)
        elif effect.verb is Verb.RENAME:
            self.dirty -= effect.targets
            self.tables -= effect.targets
            if effect.renamed_to is not None:
                self.tables.add(effect.renamed_to)
        elif effect.verb is Verb.CREATE_TABLE:
            self.tables.update(effect.targets)
        elif effect.verb is Verb.CREATE_VIEW:
            self.views.update(effect.targets)
            self.tables -= effect.targets
        elif effect.verb is Verb.DROP:
            self.tables -= effect.targets
            self.views -= effect.targets
            self.dirty -= effect.targets
        elif effect.verb is Verb.OPAQUE:
            self.dirty.update(self.tables)

    def flushed(self) -> None:
        self.dirty.clear()

    def paranoid_before(self) -> list[str]:
        return [f"REFRESH TABLE {name}" for name in sorted(self.tables)]
