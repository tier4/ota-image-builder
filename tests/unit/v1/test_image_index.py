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
"""Unit tests for v1/_image_index.py module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ota_image_libs.v1.annotation_keys import BUILD_TOOL_VERSION
from ota_image_libs.v1.consts import (
    IMAGE_INDEX_FNAME,
    OCI_LAYOUT_FNAME,
    RESOURCE_DIR,
)
from pydantic import ValidationError

from ota_image_builder.v1._image_index import _init_index, init_ota_image

# Required annotation for ImageIndex
REQUIRED_ANNOTATIONS = {
    BUILD_TOOL_VERSION: "0.1.0-test",
}


class TestInitIndex:
    """Tests for _init_index function."""

    def test_init_with_required_annotations(self):
        """Test initializing index with required annotations."""
        result = _init_index(REQUIRED_ANNOTATIONS)

        assert result.manifests == []
        assert result.annotations is not None

    def test_init_with_additional_annotations(self):
        """Test initializing index with additional annotations."""
        annotations = {
            **REQUIRED_ANNOTATIONS,
            "vnd.tier4.pilot-auto.platform": "test-platform",
            "vnd.tier4.pilot-auto.project.version": "1.0.0",
        }

        result = _init_index(annotations)

        assert result.manifests == []
        assert result.annotations is not None

    def test_init_missing_required_field(self):
        """Test that missing required field raises validation error."""
        with pytest.raises(ValidationError):
            _init_index({})


class TestInitOtaImage:
    """Tests for init_ota_image function."""

    def test_creates_directory_structure(self, tmp_path: Path):
        """Test that init_ota_image creates the correct directory structure."""
        image_root = tmp_path / "ota-image"

        result = init_ota_image(image_root, REQUIRED_ANNOTATIONS)

        assert result == image_root
        assert image_root.is_dir()
        assert (image_root / RESOURCE_DIR).is_dir()
        assert (image_root / OCI_LAYOUT_FNAME).is_file()
        assert (image_root / IMAGE_INDEX_FNAME).is_file()

    def test_oci_layout_content(self, tmp_path: Path):
        """Test that oci-layout file has correct content."""
        image_root = tmp_path / "ota-image"

        init_ota_image(image_root, REQUIRED_ANNOTATIONS)

        oci_layout = json.loads((image_root / OCI_LAYOUT_FNAME).read_text())
        assert oci_layout == {"imageLayoutVersion": "1.0.0"}

    def test_index_json_content(self, tmp_path: Path):
        """Test that index.json is valid JSON."""
        image_root = tmp_path / "ota-image"

        init_ota_image(image_root, REQUIRED_ANNOTATIONS)

        index_content = json.loads((image_root / IMAGE_INDEX_FNAME).read_text())
        assert "schemaVersion" in index_content
        assert "manifests" in index_content
        assert index_content["manifests"] == []

    def test_creates_nested_directories(self, tmp_path: Path):
        """Test that nested parent directories are created."""
        image_root = tmp_path / "deep" / "nested" / "path" / "ota-image"

        init_ota_image(image_root, REQUIRED_ANNOTATIONS)

        assert image_root.is_dir()
        assert (image_root / RESOURCE_DIR).is_dir()

    def test_with_additional_annotations(self, tmp_path: Path):
        """Test initialization with additional annotations."""
        image_root = tmp_path / "ota-image"
        annotations = {
            **REQUIRED_ANNOTATIONS,
            "vnd.tier4.pilot-auto.platform": "my-platform",
        }

        init_ota_image(image_root, annotations)

        index_content = json.loads((image_root / IMAGE_INDEX_FNAME).read_text())
        assert "annotations" in index_content
