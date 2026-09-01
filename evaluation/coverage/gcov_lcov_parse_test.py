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

"""Paper-comparable lcov branch parsing (exclude never-evaluated BRDA ``-``)."""

from __future__ import annotations

from pathlib import Path

from evaluation.coverage.gcov import _parse_lcov_info, hit_lines_from_lcov_report


def test_parse_lcov_skips_unevaluated_branches(tmp_path: Path) -> None:
    info = tmp_path / "lcov.info"
    info.write_text(
        "\n".join(
            [
                "SF:src/foo.cpp",
                "DA:1,1",
                "DA:2,0",
                "BRDA:10,0,0,1",  # taken → covered
                "BRDA:10,0,1,0",  # evaluated, not taken → missed
                "BRDA:11,0,0,-",  # never evaluated → omit from denom
                "BRDA:11,0,1,-",  # never evaluated → omit
                "FNDA:1,foo",
                "end_of_record",
                "",
            ]
        ),
        encoding="utf-8",
    )
    report = _parse_lcov_info(info)
    assert report["line_covered"] == 1
    assert report["line_total"] == 2
    assert report["branch_covered"] == 1
    assert report["branch_total"] == 2
    assert report["branch_percent"] == 50.0
    assert report["function_covered"] == 1
    assert report["function_total"] == 1
    assert report["files"][0]["lines"] == [
        {"line_number": 1, "count": 1},
        {"line_number": 2, "count": 0},
    ]


def test_hit_lines_from_lcov_report(tmp_path: Path) -> None:
    info = tmp_path / "lcov.info"
    info.write_text(
        "\n".join(
            [
                f"SF:{tmp_path / 'src' / 'a.cpp'}",
                "DA:10,3",
                "DA:11,0",
                "end_of_record",
                "SF:relative/b.cpp",
                "DA:1,1",
                "end_of_record",
                "",
            ]
        ),
        encoding="utf-8",
    )
    report = _parse_lcov_info(info)
    hit, instrumented = hit_lines_from_lcov_report(report, root=tmp_path)
    assert instrumented == 3
    assert hit == {("src/a.cpp", 10), ("relative/b.cpp", 1)}
