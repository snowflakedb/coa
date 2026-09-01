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

"""Structural boundaries, enforced.

Three rules, each protecting something that took real work to establish and that a single
convenient import would undo:

1. **Nothing under ``eqgen/`` imports the monorepo it was extracted from** — no ``yeti``, no
   ``common``, no ``snowflake``. The extraction's whole point.
2. **``generators/example_generator`` depends on almost nothing**, and nothing in the core depends
   on it. It is a replaceable example; the moment the core imports it, it stops being replaceable.
3. **No ``sqlglot``.** SQL translation was removed deliberately — each dialect emits its own SQL —
   and a translator reintroduced quietly would bring back a class of silent mistranslation.

These are checked by reading the source rather than by importing it, so a violation is caught even
in a module nothing imports yet.

There used to be a fourth rule — "nothing depends on ``evaluation``" — which no test ever enforced.
It is now structural instead: the measurement code lives outside this package entirely, so an import
of it cannot typecheck, let alone run. The harness keeps the two hooks it fed
(``run_fuzz(round_hook=…, plan_fingerprint=…)``) because both are useful to anyone observing a run.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent

#: Packages the extraction exists to be free of.
_FORBIDDEN_ROOTS = ("yeti", "common", "snowflake", "gcl_config", "sqlglot")

#: What the replaceable example generator is allowed to know about.
_EXAMPLE_ALLOWED = ("eqgen.core", "eqgen.plugins", "eqgen.generators.example_generator")

#: Nothing in these packages may import a query generator.
_MUST_NOT_USE_EXAMPLE = ("equivalence", "ir", "core", "builder", "dialects")


def _python_files() -> list[Path]:
    # Skip run artifacts under log/: past coverage campaigns wrote generated runners there that
    # import yeti on purpose, and those files are evidence rather than source.
    return sorted(
        path
        for path in _ROOT.rglob("*.py")
        if "__pycache__" not in path.parts and "log" not in path.relative_to(_ROOT).parts
    )


def _imported_modules(path: Path) -> set[str]:
    """Every module name a file imports, resolved to dotted form."""
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def test_nothing_imports_the_monorepo_it_came_from() -> None:
    """The gate that stops the extraction rotting. One convenient ``from yeti…`` undoes it."""
    violations: list[str] = []
    for path in _python_files():
        for module in _imported_modules(path):
            root = module.split(".")[0]
            if root in _FORBIDDEN_ROOTS:
                violations.append(f"{path.relative_to(_ROOT)}: imports {module}")
    assert not violations, "forbidden imports:\n  " + "\n  ".join(violations)


def test_no_sql_translator_is_reintroduced() -> None:
    """Each dialect emits its own SQL. A translator in the path would mean text could be converted
    twice — which silently corrupted an array index in the version this replaces."""
    offenders = [
        path.relative_to(_ROOT)
        for path in _python_files()
        if any(module.split(".")[0] == "sqlglot" for module in _imported_modules(path))
    ]
    assert not offenders, f"sqlglot reintroduced in {offenders}"


def test_the_example_generator_depends_on_almost_nothing() -> None:
    """It may know about the vocabulary and the contracts, and nothing else — that is what makes it
    swappable for SQLancer or anything else."""
    violations: list[str] = []
    for path in (_ROOT / "generators" / "example_generator").rglob("*.py"):
        for module in _imported_modules(path):
            if not module.startswith("eqgen"):
                continue
            if not any(module.startswith(allowed) for allowed in _EXAMPLE_ALLOWED):
                violations.append(f"{path.relative_to(_ROOT)}: imports {module}")
    assert not violations, "the example generator must not reach into the project:\n  " + "\n  ".join(violations)


def test_nothing_depends_on_the_example_generator() -> None:
    """The harness selects it at runtime from the CLI, so no module needs it. If the core imported
    it, replacing it would stop being a configuration change."""
    violations: list[str] = []
    for package in _MUST_NOT_USE_EXAMPLE:
        for path in (_ROOT / package).rglob("*.py"):
            for module in _imported_modules(path):
                if module.startswith("eqgen.generators.example_generator"):
                    violations.append(f"{path.relative_to(_ROOT)}: imports {module}")
    assert not violations, "the example generator must stay replaceable:\n  " + "\n  ".join(violations)


def test_the_core_does_not_depend_on_a_query_generator() -> None:
    """``generators/`` holds replaceable query sources, including the example.

    ``fuzz`` is allowed to import one, because selecting a generator at the command line is its job.
    Everything that builds objects must not: the generator supplies query *text* through
    ``plugins.QuerySource`` and the equivalence machinery has no business knowing which tool produced it.
    """
    violations: list[str] = []
    for package in _MUST_NOT_USE_EXAMPLE:
        for path in (_ROOT / package).rglob("*.py"):
            for module in _imported_modules(path):
                if module.startswith("eqgen.generators"):
                    violations.append(f"{path.relative_to(_ROOT)}: imports {module}")
    assert not violations, "query generators must stay replaceable:\n  " + "\n  ".join(violations)


def test_the_plugin_contracts_import_only_the_vocabulary() -> None:
    """So a third-party source can be written against ``plugins.py`` alone, without pulling in the
    generator or the harness."""
    for module in _imported_modules(_ROOT / "plugins.py"):
        if module.startswith("eqgen"):
            assert module.startswith("eqgen.core"), f"plugins.py should not import {module}"


def test_the_equivalence_core_does_not_import_the_harness() -> None:
    """The generator is usable without any engine at all — every unit test here does exactly that."""
    violations: list[str] = []
    for path in (_ROOT / "equivalence").rglob("*.py"):
        for module in _imported_modules(path):
            if module.startswith("eqgen.fuzz") or module.startswith("eqgen.dialects"):
                violations.append(f"{path.relative_to(_ROOT)}: imports {module}")
    assert not violations, "the core must not depend on the harness or a dialect:\n  " + "\n  ".join(violations)


def test_every_dialect_builder_is_prefixed_with_its_dialect() -> None:
    """A builder's name alone must say whether it is portable.

    Two reasons this is enforced rather than trusted. Builder weights in a ``.gcl`` file are matched
    by **class name**, so a portable-looking name that only works on one engine is a silent trap for
    whoever edits the configuration. And a contributor scanning the builder list should be able to
    tell at a glance which rewrites they can reuse.

    The prefix is the dialect's package directory, capitalised the way the dialect's own classes
    already are — ``duckdb`` → ``DuckDB``, matching ``DuckDBAdapter`` and ``DuckDBEmitter``.
    """
    dialects_root = _ROOT / "dialects"
    violations: list[str] = []
    for package in sorted(p for p in dialects_root.iterdir() if p.is_dir() and p.name != "__pycache__"):
        # Derive the expected prefix from how the dialect names its own adapter, so the convention
        # cannot drift between a package name and its classes.
        adapter_source = (package / "adapter.py").read_text()
        prefix = next(
            (
                node.name.removesuffix("Adapter")
                for node in ast.walk(ast.parse(adapter_source))
                if isinstance(node, ast.ClassDef) and node.name.endswith("Adapter")
            ),
            None,
        )
        assert prefix, f"{package.name} has no *Adapter class to take a prefix from"
        for path in package.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
                if not isinstance(node, ast.ClassDef) or not node.name.endswith("Builder"):
                    continue
                if not node.name.startswith(prefix):
                    violations.append(f"{path.relative_to(_ROOT)}: {node.name} should start with {prefix!r}")
    assert not violations, "dialect builders must carry their dialect's prefix:\n  " + "\n  ".join(violations)


def test_portable_builders_do_not_claim_a_dialect() -> None:
    """The other direction: a builder in the portable core must not look engine-specific, or it reads
    as unusable when it is in fact available everywhere."""
    dialect_prefixes = [p.name for p in (_ROOT / "dialects").iterdir() if p.is_dir() and p.name != "__pycache__"]
    violations: list[str] = []
    for path in (_ROOT / "equivalence" / "builders").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if isinstance(node, ast.ClassDef) and any(node.name.lower().startswith(d) for d in dialect_prefixes):
                violations.append(f"{path.relative_to(_ROOT)}: {node.name}")
    assert not violations, "portable builders must not be named after an engine:\n  " + "\n  ".join(violations)


def test_the_type_layer_does_not_render_sql() -> None:
    """Type names are a dialect's business. A ``sql_string`` on the type is how the original ended up
    with Snowflake spellings baked into the data model."""
    source = (_ROOT / "core" / "types.py").read_text()
    for forbidden in ("def sql_string", "NUMBER(", "TIMESTAMP_NTZ", "VARIANT"):
        assert forbidden not in source, f"{forbidden!r} should not appear in the type vocabulary"


def _emitted_strings(path: Path) -> list[str]:
    """Every string literal the code could *emit*, excluding docstrings.

    Scanning raw file text would flag prose — a docstring explaining that ``TRANSIENT`` was removed
    reads identically to code emitting it. Only literals that are not documentation can reach SQL,
    so those are what this scan looks at.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))

    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            found.append(node.value)
        elif isinstance(node, ast.JoinedStr):  # an f-string: check its literal parts
            found.extend(part.value for part in node.values if isinstance(part, ast.Constant) and isinstance(part.value, str))
    return found


def test_no_snowflake_specific_sql_survives_in_the_portable_core() -> None:
    """Each of these is a construct only Snowflake has, so finding one in a
    string the portable core can emit would mean it had quietly acquired an engine.

    Attribute-style docstrings are still skipped by ``_emitted_strings``' docstring detection only
    for module/class/function docstrings, so a stray ``#:`` comment cannot trip this either — comments
    are not in the AST at all.
    """
    forbidden = (
        "CLUSTER BY",
        "TRANSIENT TABLE",
        "SECURE VIEW",
        "AUTOINCREMENT",
        "GENERATOR(",
        "CONNECT BY",
        "LAST_QUERY_ID",
        "SYSTEM$",
        "RESULT_SCAN",
        "UNDROP",
        "EXTERNAL_VOLUME",
        "PARSE_JSON",
        "TIMESTAMP_NTZ",
        "NUMBER(",
    )
    portable = [path for path in _python_files() if "dialects" not in path.parts and "tests" not in path.parts]
    violations: list[str] = []
    for path in portable:
        for literal in _emitted_strings(path):
            for construct in forbidden:
                if construct in literal:
                    violations.append(f"{path.relative_to(_ROOT)}: {construct!r} in {literal[:60]!r}")
    assert not violations, "Snowflake-specific SQL in the portable core:\n  " + "\n  ".join(violations)


# ---------------------------------------------------------------------------
# Configuration consistency
# ---------------------------------------------------------------------------

#: Every dialect's config, reached through its own accessor rather than its adapter — building an
#: adapter can want a server or a downloaded binary, and this only needs the ``.gcl``.
_DIALECT_CONFIGS = (
    ("postgres", "eqgen.dialects.postgres.adapter", "postgres_equivalence_config"),
    ("duckdb", "eqgen.dialects.duckdb.adapter", "duckdb_equivalence_config"),
    ("mysql", "eqgen.dialects.mysql.adapter", "mysql_equivalence_config"),
    ("mariadb", "eqgen.dialects.mysql.adapter", "mariadb_equivalence_config"),
    ("tidb", "eqgen.dialects.tidb.adapter", "tidb_equivalence_config"),
    ("dolt", "eqgen.dialects.dolt.adapter", "dolt_equivalence_config"),
    ("cratedb", "eqgen.dialects.cratedb.adapter", "cratedb_equivalence_config"),
    ("sqlite", "eqgen.dialects.sqlite.adapter", "sqlite_equivalence_config"),
    ("clickhouse", "eqgen.dialects.clickhouse.adapter", "clickhouse_equivalence_config"),
)


def _channel_producers_and_reducers() -> dict[object, tuple[set[str], set[str]]]:
    """``{channel constraint: (producer names, reducer names)}``, read off the builder classes.

    Both sides declare their channel in a ``_channel_type`` class variable, so this pairing comes
    from the code rather than a list here that would rot the moment a third channel is added.

    Portable builders only: the channel mechanism lives in ``builders/expansion.py`` and no dialect
    builder joins in. If one ever does, it will need adding here.
    """
    from eqgen.equivalence.builders.expansion import (
        _ExpansionBuilderBase,
        _KeyReduceBuilderBase,
        _TagReduceBuilderBase,
    )
    from eqgen.equivalence.factory import PORTABLE_BUILDERS

    channels: dict[object, tuple[set[str], set[str]]] = {}
    for builder in PORTABLE_BUILDERS:
        channel = getattr(builder, "_channel_type", None)
        if channel is None:
            continue
        producers, reducers = channels.setdefault(channel, (set(), set()))
        if issubclass(builder, _ExpansionBuilderBase):
            producers.add(builder.__name__)
        elif issubclass(builder, (_TagReduceBuilderBase, _KeyReduceBuilderBase)):
            reducers.add(builder.__name__)
    return channels


def test_no_channel_reducer_is_enabled_without_a_producer() -> None:
    """A reducer that mints a channel nobody can fill declines every draw, so its weight is dead.

    The expand/reduce pair is the one place a builder's weight is not enough to make it reachable:
    a reducer mints its channel and dispatches a subtree constrained to it, and only builders that
    *require* that channel are offered. Disable every producer and ``build_subtree`` returns ``None``,
    the reducer declines, and the configuration silently tests nothing — measured on SQLite, a
    reducer forced to weight 40 with expanders off was drawn 0 times in 30 seeds.

    This is always a configuration mistake rather than a deliberate choice: if an engine cannot run
    the expansion, the reducers for that channel have nothing to reduce and belong at weight 0 too.
    """
    channels = _channel_producers_and_reducers()
    assert channels, "expected to find channel-coupled builders"

    violations: list[str] = []
    for label, module_name, function_name in _DIALECT_CONFIGS:
        module = __import__(module_name, fromlist=[function_name])
        weights = getattr(module, function_name)().builder_weights
        for channel, (producers, reducers) in sorted(channels.items(), key=lambda kv: kv[0].__name__):
            live_producers = sorted(name for name in producers if weights.get(name, 0.0) > 0)
            live_reducers = sorted(name for name in reducers if weights.get(name, 0.0) > 0)
            if live_reducers and not live_producers:
                violations.append(
                    f"{label}: {channel.__name__} has no enabled producer "
                    f"(of {', '.join(sorted(producers))}) but enables {', '.join(live_reducers)}"
                )
    assert not violations, "channel reducers with no producer:\n  " + "\n  ".join(violations)
