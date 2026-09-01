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

# Build a coverage-instrumented PostgreSQL for eqgen (SQLancer++ paper path by default).
#
#            bug-finding                         coverage (default = paper)     EQGEN_COVERAGE_FAIR=1
#   version  master / NNdevel                    REL_18_4                       REL_18_4 (or override)
#   asserts  --enable-cassert                    off                            off
#   configure --enable-cassert …                 --enable-coverage only         --enable-coverage --enable-debug -O0 --without-*
#
# Paper matches the SQLancer++ Dockerfile's *measurement*: ``./configure --enable-coverage`` (no
# ``--enable-debug``), then campaign keeps initdb and reports with full-tree lcov.
#
# The version is REL_18_4 in both modes: eqgen's PostgreSQL builders require it (MERGE is PG15+,
# ANY_VALUE is PG16+), and on an older server they fail setup on every pick, so the run would
# measure a crippled generator. Paper vs fair is therefore a difference of configure flags and
# reporting only -- which is where the comparability actually lay.
#
# Fair (``EQGEN_COVERAGE_FAIR=1``): older eqgen curves — -O0, --without-icu/readline/zlib, usually
# paired with ``campaign --fair`` (gcovr backend+common, initdb zeroed).
#
# Needs lcov on PATH (configure hard-errors without it)::
#
#   export PATH="$(nix --extra-experimental-features 'nix-command flakes' \
#                     build --no-link --print-out-paths nixpkgs#lcov)/bin:$PATH"

set -euo pipefail

FAIR="${EQGEN_COVERAGE_FAIR:-0}"
# Legacy: EQGEN_COVERAGE_ARTIFACT=0 meant non-paper.
if [[ "${EQGEN_COVERAGE_ARTIFACT:-1}" == "0" ]]; then
    FAIR=1
fi

if [[ "$FAIR" == "1" ]]; then
    REF="${EQGEN_PG_COVERAGE_REF:-REL_18_4}"
    SRC="${EQGEN_PG_COVERAGE_SRC:-/tmp/pgsrc-cov}"
    PREFIX="${EQGEN_PG_COVERAGE_PREFIX:-/tmp/pgcov}"
else
    REF="${EQGEN_PG_COVERAGE_REF:-REL_18_4}"
    SRC="${EQGEN_PG_COVERAGE_SRC:-$HOME/pgsrc-cov-18.4}"
    PREFIX="${EQGEN_PG_COVERAGE_PREFIX:-$HOME/pgcov-18.4}"
fi
JOBS="${JOBS:-$(nproc)}"

command -v gcov >/dev/null || { echo "gcov not found -- install a GCC toolchain" >&2; exit 1; }
command -v lcov >/dev/null || { echo "lcov not found -- see the header of this script" >&2; exit 1; }

echo "==> ref     $REF"
echo "==> source  $SRC"
echo "==> prefix  $PREFIX"
echo "==> jobs    $JOBS"
echo "==> mode    $([[ "$FAIR" == "1" ]] && echo fair || echo paper)"
echo "==> gcov    $(gcov --version | head -1)"

if [[ ! -d "$SRC/.git" ]]; then
    echo "==> cloning $REF"
    git clone --depth 1 --branch "$REF" https://github.com/postgres/postgres.git "$SRC"
fi
echo "==> at $(git -C "$SRC" log -1 --format='%h %s')"

cd "$SRC"
if [[ ! -f config.status ]]; then
    echo "==> configure"
    if [[ "$FAIR" == "1" ]]; then
        ./configure \
            --prefix="$PREFIX" \
            --enable-debug \
            --enable-coverage \
            --without-icu \
            --without-readline \
            --without-zlib \
            CFLAGS="-O0 -g"
    else
        # SQLancer++ Dockerfile: --enable-coverage only (no --enable-debug).
        if ! ./configure \
            --prefix="$PREFIX" \
            --enable-coverage; then
            echo "==> configure retry with --without-readline/zlib/icu (host missing deps)" >&2
            ./configure \
                --prefix="$PREFIX" \
                --enable-coverage \
                --without-icu \
                --without-readline \
                --without-zlib
        fi
    fi
fi

echo "==> build"
make -j"$JOBS" -s
echo "==> install"
make -s install

echo "==> zeroing counters left by the build"
before=$(find "$SRC" -name '*.gcda' | wc -l)
find "$SRC" -name '*.gcda' -delete
echo "    deleted $before .gcda file(s)"

echo
"$PREFIX/bin/postgres" --version
echo "configure: $("$PREFIX/bin/pg_config" --configure)"
echo "instrumented translation units: $(find "$SRC" -name '*.gcno' | wc -l)"
echo
echo "Done. Paper-default campaign:"
echo "  EQGEN_PG_BINDIR=$PREFIX/bin EQGEN_PG_COVERAGE=1 \\"
echo "    python -m eqgen.evaluation.coverage.campaign --dialect postgres --rich --rounds 200"
echo "Fair opt-out: EQGEN_COVERAGE_FAIR=1 $0  &&  campaign --fair"
