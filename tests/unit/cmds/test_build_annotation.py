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
"""Unit tests for cmds/build_annotation.py module."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest
import yaml

from ota_image_builder.cmds.build_annotation import (
    _load_annotation_keys,
    _load_base,
    _parse_kv,
    _parse_user_annotations,
    allowed_user_annotations,
    build_annotation_cmd,
)


class TestLoadAnnotationKeys:
    """Tests for _load_annotation_keys function."""

    def test_returns_set_of_strings(self):
        """Test that _load_annotation_keys returns a set of strings."""
        result = _load_annotation_keys()

        assert isinstance(result, set)
        assert all(isinstance(k, str) for k in result)

    def test_excludes_private_attributes(self):
        """Test that private attributes (starting with _) are excluded."""
        result = _load_annotation_keys()

        for key in result:
            assert not key.startswith("_")

    def test_contains_known_keys(self):
        """Test that result contains some known annotation keys."""
        result = _load_annotation_keys()

        # These should be in the annotation_keys module
        assert len(result) > 0


class TestParseKv:
    """Tests for _parse_kv function."""

    def test_parse_valid_kv_pairs(self):
        """Test parsing valid key=value pairs."""
        available_keys = frozenset({"key1", "key2", "key3"})
        input_list = ["key1=value1", "key2=value2"]

        result = _parse_kv(input_list, available_keys=available_keys)

        assert result == {"key1": "value1", "key2": "value2"}

    def test_filter_unavailable_keys(self):
        """Test that keys not in available_keys are filtered."""
        available_keys = frozenset({"key1"})
        input_list = ["key1=value1", "key2=value2", "key3=value3"]

        result = _parse_kv(input_list, available_keys=available_keys)

        assert result == {"key1": "value1"}
        assert "key2" not in result
        assert "key3" not in result

    def test_ignore_invalid_format(self):
        """Test that invalid format (no =) is ignored."""
        available_keys = frozenset({"key1", "key2", "invalid"})
        input_list = ["key1=value1", "invalid", "key2=value2"]

        result = _parse_kv(input_list, available_keys=available_keys)

        assert result == {"key1": "value1", "key2": "value2"}
        assert "invalid" not in result

    def test_value_with_equals_sign(self):
        """Test that values containing = are handled correctly."""
        available_keys = frozenset({"key1"})
        input_list = ["key1=value=with=equals"]

        result = _parse_kv(input_list, available_keys=available_keys)

        assert result == {"key1": "value=with=equals"}

    def test_empty_value(self):
        """Test that empty values are allowed."""
        available_keys = frozenset({"key1"})
        input_list = ["key1="]

        result = _parse_kv(input_list, available_keys=available_keys)

        assert result == {"key1": ""}

    def test_empty_input_list(self):
        """Test with empty input list."""
        available_keys = frozenset({"key1"})

        result = _parse_kv([], available_keys=available_keys)

        assert result == {}


class TestParseUserAnnotations:
    """Tests for _parse_user_annotations function."""

    def test_parse_allowed_user_annotations(self):
        """Test parsing allowed user annotations."""
        # Get one of the allowed keys for testing
        allowed_key = next(iter(allowed_user_annotations))
        input_list = [f"{allowed_key}=test_value"]

        result = _parse_user_annotations(input_list)

        assert result == {allowed_key: "test_value"}

    def test_filter_disallowed_annotations(self):
        """Test that disallowed annotations are filtered."""
        input_list = ["not.allowed.key=value"]

        result = _parse_user_annotations(input_list)

        assert result == {}


class TestLoadBase:
    """Tests for _load_base function."""

    def test_load_valid_yaml(self, tmp_path: Path):
        """Test loading a valid YAML file."""
        base_file = tmp_path / "base.yaml"
        data = {"key1": "value1", "key2": "value2"}
        base_file.write_text(yaml.dump(data))

        result = _load_base(base_file)

        assert result == data

    def test_load_empty_yaml(self, tmp_path: Path):
        """Test loading an empty YAML file returns empty dict or None."""
        base_file = tmp_path / "empty.yaml"
        base_file.write_text("")

        # Empty YAML file returns None which is not a dict
        with pytest.raises(SystemExit):
            _load_base(base_file)

    def test_load_yaml_with_nested_structure(self, tmp_path: Path):
        """Test loading YAML with nested structure (still returns dict)."""
        base_file = tmp_path / "nested.yaml"
        data = {"key1": "value1", "nested": {"inner": "value"}}
        base_file.write_text(yaml.dump(data))

        result = _load_base(base_file)

        assert result == data

    def test_load_invalid_yaml_list(self, tmp_path: Path):
        """Test that loading a YAML list raises error."""
        base_file = tmp_path / "list.yaml"
        base_file.write_text("- item1\n- item2\n")

        with pytest.raises(SystemExit):
            _load_base(base_file)


class TestBuildAnnotationCmd:
    """Tests for build_annotation_cmd function."""

    def test_output_to_file(self, tmp_path: Path):
        """Test writing annotations to output file."""
        output_file = tmp_path / "output.yaml"
        available_keys = list(_load_annotation_keys())

        args = Namespace(
            i=None,
            o=str(output_file),
            add_user_annotation=None,
            add_or=[f"{available_keys[0]}=value1"] if available_keys else [],
            add_replace=None,
        )

        build_annotation_cmd(args)

        assert output_file.exists()
        loaded = yaml.safe_load(output_file.read_text())
        if available_keys:
            assert available_keys[0] in loaded

    def test_output_to_stdout(self, tmp_path: Path, capsys):
        """Test printing annotations to stdout."""
        available_keys = list(_load_annotation_keys())

        args = Namespace(
            i=None,
            o=None,
            add_user_annotation=None,
            add_or=[f"{available_keys[0]}=value1"] if available_keys else [],
            add_replace=None,
        )

        build_annotation_cmd(args)

        captured = capsys.readouterr()
        if available_keys:
            assert available_keys[0] in captured.out or "value1" in captured.out

    def test_with_base_file(self, tmp_path: Path):
        """Test loading base file and adding annotations."""
        base_file = tmp_path / "base.yaml"
        output_file = tmp_path / "output.yaml"
        available_keys = list(_load_annotation_keys())

        base_data = {available_keys[0]: "base_value"} if available_keys else {}
        base_file.write_text(yaml.dump(base_data))

        args = Namespace(
            i=str(base_file),
            o=str(output_file),
            add_user_annotation=None,
            add_or=None,
            add_replace=None,
        )

        build_annotation_cmd(args)

        loaded = yaml.safe_load(output_file.read_text())
        if available_keys:
            assert loaded[available_keys[0]] == "base_value"

    def test_add_or_does_not_override(self, tmp_path: Path):
        """Test that add_or does not override existing values."""
        base_file = tmp_path / "base.yaml"
        output_file = tmp_path / "output.yaml"
        available_keys = list(_load_annotation_keys())

        if not available_keys:
            pytest.skip("No available annotation keys")

        key = available_keys[0]
        base_file.write_text(yaml.dump({key: "original"}))

        args = Namespace(
            i=str(base_file),
            o=str(output_file),
            add_user_annotation=None,
            add_or=[f"{key}=new_value"],
            add_replace=None,
        )

        build_annotation_cmd(args)

        loaded = yaml.safe_load(output_file.read_text())
        assert loaded[key] == "original"  # Should not be overridden

    def test_add_replace_overrides(self, tmp_path: Path):
        """Test that add_replace overrides existing values."""
        base_file = tmp_path / "base.yaml"
        output_file = tmp_path / "output.yaml"
        available_keys = list(_load_annotation_keys())

        if not available_keys:
            pytest.skip("No available annotation keys")

        key = available_keys[0]
        base_file.write_text(yaml.dump({key: "original"}))

        args = Namespace(
            i=str(base_file),
            o=str(output_file),
            add_user_annotation=None,
            add_or=None,
            add_replace=[f"{key}=replaced"],
        )

        build_annotation_cmd(args)

        loaded = yaml.safe_load(output_file.read_text())
        assert loaded[key] == "replaced"

    def test_nonexistent_base_file_exits(self, tmp_path: Path):
        """Test that nonexistent base file causes SystemExit."""
        args = Namespace(
            i=str(tmp_path / "nonexistent.yaml"),
            o=None,
            add_user_annotation=None,
            add_or=None,
            add_replace=None,
        )

        with pytest.raises(SystemExit):
            build_annotation_cmd(args)
