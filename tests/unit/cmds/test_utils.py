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
"""Unit tests for cmds/_utils.py module."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest
import yaml
from pydantic import BaseModel

from ota_image_builder.cmds._utils import validate_annotations


class SampleAnnotationModel(BaseModel):
    """Sample pydantic model for testing."""

    name: str
    version: str
    description: Optional[str] = None


class TestValidateAnnotations:
    """Tests for validate_annotations function."""

    def test_valid_annotations(self, tmp_path: Path):
        """Test validating a valid annotations file."""
        annotations_file = tmp_path / "annotations.yaml"
        data = {"name": "test", "version": "1.0.0", "description": "A test"}
        annotations_file.write_text(yaml.dump(data))

        result = validate_annotations(annotations_file, SampleAnnotationModel)

        assert result["name"] == "test"
        assert result["version"] == "1.0.0"
        assert result["description"] == "A test"

    def test_valid_annotations_with_optional_missing(self, tmp_path: Path):
        """Test validating annotations with optional field missing."""
        annotations_file = tmp_path / "annotations.yaml"
        data = {"name": "test", "version": "1.0.0"}
        annotations_file.write_text(yaml.dump(data))

        result = validate_annotations(annotations_file, SampleAnnotationModel)

        assert result["name"] == "test"
        assert result["version"] == "1.0.0"
        assert result["description"] is None

    def test_nonexistent_file(self, tmp_path: Path):
        """Test with non-existent file raises SystemExit."""
        nonexistent = tmp_path / "nonexistent.yaml"

        with pytest.raises(SystemExit):
            validate_annotations(nonexistent, SampleAnnotationModel)

    def test_invalid_yaml_not_dict(self, tmp_path: Path):
        """Test with YAML that is not a dict raises SystemExit."""
        annotations_file = tmp_path / "annotations.yaml"
        annotations_file.write_text("- item1\n- item2\n")

        with pytest.raises(SystemExit):
            validate_annotations(annotations_file, SampleAnnotationModel)

    def test_missing_required_field(self, tmp_path: Path):
        """Test with missing required field raises SystemExit."""
        annotations_file = tmp_path / "annotations.yaml"
        data = {"name": "test"}  # missing 'version'
        annotations_file.write_text(yaml.dump(data))

        with pytest.raises(SystemExit):
            validate_annotations(annotations_file, SampleAnnotationModel)

    def test_extra_fields_ignored(self, tmp_path: Path):
        """Test that extra fields not in model are ignored."""
        annotations_file = tmp_path / "annotations.yaml"
        data = {
            "name": "test",
            "version": "1.0.0",
            "extra_field": "should be ignored",
        }
        annotations_file.write_text(yaml.dump(data))

        result = validate_annotations(annotations_file, SampleAnnotationModel)

        assert "extra_field" not in result
        assert result["name"] == "test"
        assert result["version"] == "1.0.0"
