# Copyright 2025 TIER IV, INC. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Unit tests for cmds/build_exclude_cfg.py module."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from ota_image_builder.cmds.build_exclude_cfg import (
    _load_input,
    build_exclude_cfg_cmd,
    invalid_patterns_pa,
)


class TestInvalidPatternsRegex:
    """Tests for invalid_patterns_pa regex patterns."""

    @pytest.mark.parametrize(
        "pattern",
        [
            ".",
            "./",
            ".//",
            "..",
            "../",
            "..//",
            "/",
            "//",
            "///",
            "/boot/ota",
            "/boot/ota/",
            "/boot/ota/foo",
            "/home/autoware/build",
            "/home/autoware_v1/build",
        ],
    )
    def test_invalid_patterns_matched(self, pattern: str):
        """Test that invalid patterns are correctly matched."""
        matched = any(pa.match(pattern) for pa in invalid_patterns_pa)
        assert matched, f"Pattern '{pattern}' should be matched as invalid"

    @pytest.mark.parametrize(
        "pattern",
        [
            "/etc/hosts",
            "/usr/bin",
            "/var/log",
            "/home/user",
            "/boot/grub",
            "/home/autoware/src",
            "relative/path",
            "*.txt",
            "/home/autoware/build/extra",  # has extra path after build
        ],
    )
    def test_valid_patterns_not_matched(self, pattern: str):
        """Test that valid patterns are not matched by invalid pattern regex."""
        matched = any(pa.match(pattern) for pa in invalid_patterns_pa)
        assert not matched, f"Pattern '{pattern}' should NOT be matched as invalid"


class TestLoadInput:
    """Tests for _load_input function."""

    def test_load_valid_patterns(self, tmp_path: Path):
        """Test loading file with valid patterns."""
        input_file = tmp_path / "patterns.txt"
        input_file.write_text("/etc/hosts\n/usr/bin\n/var/log\n")

        result = _load_input(input_file)

        assert result == {"/etc/hosts", "/usr/bin", "/var/log"}

    def test_filter_invalid_patterns(self, tmp_path: Path):
        """Test that invalid patterns are filtered out."""
        input_file = tmp_path / "patterns.txt"
        input_file.write_text("/etc/hosts\n.\n/boot/ota\n/usr/bin\n")

        result = _load_input(input_file)

        assert "/etc/hosts" in result
        assert "/usr/bin" in result
        assert "." not in result
        assert "/boot/ota" not in result

    def test_empty_file(self, tmp_path: Path):
        """Test loading an empty file."""
        input_file = tmp_path / "empty.txt"
        input_file.write_text("")

        result = _load_input(input_file)

        assert result == set()

    def test_whitespace_handling(self, tmp_path: Path):
        """Test that whitespace is stripped from patterns."""
        input_file = tmp_path / "patterns.txt"
        input_file.write_text("  /etc/hosts  \n\t/usr/bin\t\n")

        result = _load_input(input_file)

        assert "/etc/hosts" in result
        assert "/usr/bin" in result

    def test_duplicate_patterns(self, tmp_path: Path):
        """Test that duplicate patterns are deduplicated."""
        input_file = tmp_path / "patterns.txt"
        input_file.write_text("/etc/hosts\n/etc/hosts\n/etc/hosts\n")

        result = _load_input(input_file)

        assert result == {"/etc/hosts"}
        assert len(result) == 1

    def test_mixed_valid_and_invalid(self, tmp_path: Path):
        """Test file with mixed valid and invalid patterns."""
        input_file = tmp_path / "patterns.txt"
        content = "\n".join(
            [
                "/valid/path1",
                ".",  # invalid
                "/valid/path2",
                "..",  # invalid
                "/boot/ota/file",  # invalid
                "/valid/path3",
                "/",  # invalid
            ]
        )
        input_file.write_text(content)

        result = _load_input(input_file)

        assert result == {"/valid/path1", "/valid/path2", "/valid/path3"}

    def test_nonexistent_file_exits(self, tmp_path: Path):
        """Test that nonexistent file causes SystemExit."""
        nonexistent = tmp_path / "nonexistent.txt"

        with pytest.raises(SystemExit):
            _load_input(nonexistent)


class TestBuildExcludeCfgCmd:
    """Tests for build_exclude_cfg_cmd function."""

    def test_output_to_file(self, tmp_path: Path):
        """Test that command writes patterns to output file."""
        input_file = tmp_path / "input.txt"
        output_file = tmp_path / "output.txt"
        input_file.write_text("/etc/hosts\n/usr/bin\n")

        args = Namespace(i=[str(input_file)], o=str(output_file))
        build_exclude_cfg_cmd(args)

        assert output_file.exists()
        content = output_file.read_text()
        assert "/etc/hosts" in content or "/usr/bin" in content

    def test_output_to_stdout(self, tmp_path: Path, capsys):
        """Test that command prints to stdout when no output specified."""
        input_file = tmp_path / "input.txt"
        input_file.write_text("/etc/hosts\n")

        args = Namespace(i=[str(input_file)], o=None)
        build_exclude_cfg_cmd(args)

        captured = capsys.readouterr()
        assert "/etc/hosts" in captured.out

    def test_nonexistent_input_exits(self, tmp_path: Path):
        """Test that nonexistent input file causes SystemExit."""
        args = Namespace(i=[str(tmp_path / "nonexistent.txt")], o=None)

        with pytest.raises(SystemExit):
            build_exclude_cfg_cmd(args)

    def test_multiple_input_files(self, tmp_path: Path):
        """Test merging multiple input files."""
        input1 = tmp_path / "input1.txt"
        input2 = tmp_path / "input2.txt"
        output_file = tmp_path / "output.txt"

        input1.write_text("/path1\n/path2\n")
        input2.write_text("/path3\n/path4\n")

        args = Namespace(i=[str(input1), str(input2)], o=str(output_file))
        build_exclude_cfg_cmd(args)

        content = output_file.read_text()
        # All paths should be present
        lines = set(content.strip().split("\n"))
        assert len(lines) == 4
