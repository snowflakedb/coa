#!/usr/bin/env bash
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

# Build a gcov-instrumented DuckDB CLI for eqgen (SQLancer++ paper path by default).
#
#            bug-finding                         coverage (default = paper)     EQGEN_COVERAGE_FAIR=1
#   version  artifacts.duckdb.org ``main``       v1.0.0                         v1.5.5 (or override)
#   asserts  whatever the nightly ships          off (Release)                  off
#   opt      Release                             CXXFLAGS=--coverage, Release   -O0 -g --coverage
#   unity    —                                   ON (paper Dockerfile)          DISABLE_UNITY=1
#   instr.   none                                GCC --coverage                 GCC --coverage
#
# Paper matches the SQLancer++ Dockerfile. Fair (``EQGEN_COVERAGE_FAIR=1``) keeps the older
# DISABLE_UNITY / -O0 tree for debugging attribution.
#
# Counters land under ``$SRC/build/coverage``. Point ``EQGEN_DUCKDB_CLI`` /
# ``EQGEN_DUCKDB_COVERAGE_SRC`` at the build; campaign reports with lcov + lcov_exclude +
# omit-unevaluated BRDA (see COVERAGE_NOTES.md).

set -euo pipefail

FAIR="${EQGEN_COVERAGE_FAIR:-0}"
if [[ "${EQGEN_COVERAGE_ARTIFACT:-1}" == "0" ]]; then
    FAIR=1
fi

if [[ "$FAIR" == "1" ]]; then
    REF="${EQGEN_DUCKDB_COVERAGE_REF:-v1.5.5}"
    SRC="${EQGEN_DUCKDB_COVERAGE_SRC:-$HOME/ducksrc-cov}"
    BUILD="${EQGEN_DUCKDB_COVERAGE_BUILD:-$SRC/build/coverage}"
else
    REF="${EQGEN_DUCKDB_COVERAGE_REF:-v1.0.0}"
    SRC="${EQGEN_DUCKDB_COVERAGE_SRC:-$HOME/ducksrc-spp-paper}"
    BUILD="${EQGEN_DUCKDB_COVERAGE_BUILD:-$SRC/build/coverage}"
fi
JOBS="${JOBS:-$(nproc)}"

command -v gcov >/dev/null || { echo "gcov not found -- install a GCC toolchain" >&2; exit 1; }
if ! command -v cmake >/dev/null; then
    for candidate in \
        /scratch/bazel/*/external/rules_foreign_cc~~tools~cmake-*-linux-*/bin/cmake \
        /usr/local/bin/cmake; do
        if [[ -x $candidate ]]; then
            PATH="$(dirname "$candidate"):$PATH"
            export PATH
            break
        fi
    done
fi
command -v cmake >/dev/null || { echo "cmake not found" >&2; exit 1; }

echo "==> ref     $REF"
echo "==> source  $SRC"
echo "==> build   $BUILD"
echo "==> jobs    $JOBS"
echo "==> mode    $([[ "$FAIR" == "1" ]] && echo fair || echo paper)"
echo "==> gcov    $(gcov --version | head -1)"
echo "==> cmake   $(cmake --version | head -1)"

if [[ ! -d "$SRC/.git" ]]; then
    echo "==> cloning $REF"
    git clone --depth 1 --branch "$REF" https://github.com/duckdb/duckdb.git "$SRC"
fi
echo "==> at $(git -C "$SRC" log -1 --format='%h %s')"

mkdir -p "$BUILD"
if [[ ! -f "$BUILD/CMakeCache.txt" ]]; then
    echo "==> configure"
    if [[ "$FAIR" == "1" ]]; then
        # jemalloc off: with DISABLE_UNITY its allocator TU fails to compile on v1.5.5.
        (cd "$BUILD" && cmake -E env \
            CFLAGS="-O0 -g --coverage" \
            CXXFLAGS="-O0 -g --coverage" \
            LDFLAGS="--coverage" \
            cmake \
                -DENABLE_SANITIZER=0 \
                -DENABLE_UBSAN=0 \
                -DENABLE_JEMALLOC=0 \
                -DDISABLE_UNITY=1 \
                -DBUILD_UNITTESTS=0 \
                -DBUILD_BENCHMARKS=0 \
                -DCMAKE_BUILD_TYPE=Release \
                "$SRC")
    else
        # SQLancer++ Dockerfile: CXXFLAGS=--coverage, unity ON, Release.
        (cd "$BUILD" && cmake -E env \
            CXXFLAGS="--coverage" \
            LDFLAGS="--coverage" \
            cmake \
                -DENABLE_SANITIZER=0 \
                -DENABLE_UBSAN=0 \
                -DBUILD_UNITTESTS=0 \
                -DBUILD_BENCHMARKS=0 \
                -DCMAKE_BUILD_TYPE=Release \
                "$SRC")
    fi
fi

echo "==> build"
cmake --build "$BUILD" -j"$JOBS"

CLI="$BUILD/duckdb"
if [[ ! -x "$CLI" ]]; then
    echo "expected CLI at $CLI" >&2
    exit 1
fi

echo "==> zeroing counters left by the build"
before=$(find "$BUILD" -name '*.gcda' | wc -l)
find "$BUILD" -name '*.gcda' -delete
echo "    deleted $before .gcda file(s)"

echo
"$CLI" -c "SELECT library_version, source_id FROM pragma_version()"
echo "instrumented translation units: $(find "$BUILD" -name '*.gcno' | wc -l)"
echo
echo "Done. Paper-default campaign:"
echo "  EQGEN_DUCKDB_CLI=$CLI \\"
echo "  EQGEN_DUCKDB_COVERAGE_SRC=$SRC \\"
echo "  EQGEN_DUCKDB_COVERAGE_BUILD=$BUILD \\"
echo "    python -m eqgen.evaluation.coverage.campaign --dialect duckdb --rich --rounds 50"
echo "Fair opt-out: EQGEN_COVERAGE_FAIR=1 $0  &&  campaign --fair"
echo "Branches: omit BRDA taken=- (COVERAGE_NOTES.md §7b–7c)."
