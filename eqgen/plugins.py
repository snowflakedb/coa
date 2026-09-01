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

"""The two things you can plug in: a predicate source and a query source.

Both hand over SQL as **strings**, so an external generator needs to know nothing about this
project's classes.

    PredicateSource   ->  "c_int > 3"                 goes inside the generated DDL
    QuerySource       ->  "SELECT c_int FROM t"       run against both databases

Implementations live outside this file. :mod:`eqgen.generators.example_generator` has a small one;
:class:`CorpusSource` below replays queries from a file.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Protocol, Sequence, runtime_checkable

from eqgen.core.catalog import Table


@runtime_checkable
class PredicateSource(Protocol):
    """Supplies a boolean predicate over the base table's columns.

    The generator embeds your predicate three times in one object — as ``p``, ``NOT p`` and
    ``p IS NULL`` — and needs those three to cover every row exactly once::

        ok      c_int > 3
        ok      c_txt IS NULL OR c_big < 0
        ok      MOD(c_int, 2) = 0

        not ok  random() < 0.5            -- two evaluations, two answers, so a row lands in
                                          -- two branches or in none
        not ok  now() > c_ts              -- same problem
        not ok  COUNT(*) > 2              -- aggregates are illegal in WHERE
        not ok  SUM(c_int) OVER () > 0    -- window functions are illegal in WHERE
        not ok  c_int IN (SELECT ...)     -- does not survive being copied into three
                                          -- separate branch bodies
        not ok  t.c_int > 3               -- use a bare column name: this gets substituted
                                          -- into queries whose source is an alias or a
                                          -- derived table

    It also has to be valid SQL for the engine you are testing — nothing downstream
    translates it.

    Returning ``None`` is always fine: builders that wanted a predicate decline, and the run
    continues with fewer rewrites available.
    """

    @property
    def name(self) -> str:
        """Short identifier, shown in logs."""
        ...

    def boolean_predicate(self, table: Table, *, seed: int) -> Optional[str]:
        """A predicate over *table*'s columns, or ``None`` to decline.

        The same *seed* and *table* must give the same text, or a finding cannot be replayed.
        """
        ...


@runtime_checkable
class QuerySource(Protocol):
    """Supplies the queries that run against the base table and against its equivalent.

    The same text runs on both sides and the two sets of rows are compared, so a query has to
    return the same rows either way::

        ok      SELECT c_int, c_txt FROM t
        ok      SELECT c_int FROM t ORDER BY c_int DESC   -- ORDER BY is welcome: it changes
                                                          -- the plan, and rows are compared
                                                          -- ignoring order anyway
        ok      SELECT COUNT(*), c_flag FROM t GROUP BY c_flag
        ok      SELECT a.c_pk FROM t0 a JOIN t1 b ON a.c_pk = b.c_pk
                                                          -- when the harness exposes several
                                                          -- fork names (``exposed_names``)

        not ok  SELECT * FROM t LIMIT 3        -- with no total order each side may pick a
                                               -- different three rows
        not ok  SELECT SUM(c_dbl) FROM t       -- adding DOUBLEs in a different order can
                                               -- differ in the last bit
        not ok  SELECT * FROM other_table      -- only names in *exposed_names* exist
        not ok  DELETE FROM t WHERE c_int > 3  -- writing makes the two databases differ
        not ok  SELECT random() FROM t         -- different answer per run

    Yield lazily. Each query is written to the log before it runs, so one that crashes the
    engine is already on disk when the process dies.
    """

    @property
    def name(self) -> str:
        """Short identifier, shown in logs."""
        ...

    def iter_queries(
        self,
        table: Table,
        *,
        seed: int,
        limit: Optional[int] = None,
        exposed_names: Sequence[str] = (),
    ) -> Iterator[str]:
        """Yield at most *limit* queries over the relations in *exposed_names*.

        *table* is the column catalog (shared signature). *exposed_names* are the relation
        names installed on both databases this round; empty means ``(table.get_sql_name(),)``.
        The same *seed* must give the same queries.
        """
        ...


#: Drops whole-line ``--`` comments from a corpus file. Not a SQL parser: a ``--`` inside a
#: string literal on its own line is misread, which costs one dropped query.
_COMMENT_LINE = re.compile(r"^\s*--.*$", re.MULTILINE)


@dataclass(frozen=True)
class CorpusSource:
    """A fixed list of queries, replayed in order.

    Three uses: rerunning a saved finding, holding the queries still while the generated
    objects vary, and driving this harness from another tool (SQLancer, sqlsmith, a captured
    trace).

    These queries did not come from here, so keeping to :class:`QuerySource`'s rules is the
    supplier's job — a corpus containing ``LIMIT`` produces mismatches that are not bugs.
    With ``--forks>1`` the corpus text must already use the exposed names (``t0``, ``t1``, …).
    """

    queries: tuple[str, ...]
    name: str = "corpus"
    #: When True, each ``iter_queries`` call yields a permutation of ``queries``
    #: shuffled with *seed*. Ordered replay (the default) is what finding files
    #: and journal round-trips need; shuffle is for covering a large file under a
    #: short per-round time budget without always executing the same prefix.
    shuffle: bool = False

    @classmethod
    def from_text(cls, text: str, *, name: str = "corpus") -> "CorpusSource":
        """Split on ``;``, dropping comment lines and blanks."""
        stripped = _COMMENT_LINE.sub("", text)
        found = tuple(q for q in (part.strip() for part in stripped.split(";")) if q)
        return cls(found, name=name)

    @classmethod
    def from_path(cls, path: str | Path) -> "CorpusSource":
        resolved = Path(path).expanduser()
        return cls.from_text(resolved.read_text(), name=f"corpus:{resolved.name}")

    @classmethod
    def from_queries(cls, queries: Sequence[str], *, name: str = "corpus") -> "CorpusSource":
        return cls(tuple(queries), name=name)

    def iter_queries(
        self,
        table: Table,
        *,
        seed: int,
        limit: Optional[int] = None,
        exposed_names: Sequence[str] = (),
    ) -> Iterator[str]:
        """Replay the corpus.

        *table* and *exposed_names* are ignored. *seed* is ignored unless
        :attr:`shuffle` is set — ordered replay must not vary, or a saved finding
        would not reproduce.
        """
        del table, exposed_names
        n = len(self.queries)
        if self.shuffle:
            order = list(range(n))
            random.Random(seed).shuffle(order)
            indices: Sequence[int] = order
        else:
            del seed
            indices = range(n)
        for i, idx in enumerate(indices):
            if limit is not None and i >= limit:
                return
            yield self.queries[idx]