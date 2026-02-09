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
"""Unit tests for v1/_image_config.py module."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from ota_image_libs.v1.annotation_keys import (
    OS,
    OS_VERSION,
    PLATFORM_ECU_ARCH,
    SYS_IMAGE_BASE_IMAGE,
)
from pydantic import ValidationError

from ota_image_builder.v1._image_config import (
    AddImageConfigAnnotations,
    compose_image_config,
)


class TestAddImageConfigAnnotations:
    """Tests for AddImageConfigAnnotations pydantic model."""

    def test_required_fields(self):
        """Test that required fields must be provided."""
        with pytest.raises(ValidationError):
            AddImageConfigAnnotations()  # type: ignore

    def test_with_required_fields_only(self):
        """Test with only required fields."""
        model = AddImageConfigAnnotations(
            base_image="ubuntu:22.04",  # type: ignore
            architecture="aarch64",  # type: ignore
        )

        assert model.base_image == "ubuntu:22.04"
        assert model.architecture == "aarch64"
        assert model.description is None
        assert model.created is None
        assert model.os is None
        assert model.os_version is None

    def test_with_all_fields(self):
        """Test with all fields populated."""
        model = AddImageConfigAnnotations(
            base_image="ubuntu:22.04",  # type: ignore
            architecture="aarch64",  # type: ignore
        )

        model.description = "Test image"
        model.created = "2025-01-01T00:00:00"
        model.os = "linux"
        model.os_version = "22.04"

        assert model.base_image == "ubuntu:22.04"
        assert model.architecture == "aarch64"
        assert model.description == "Test image"
        assert model.created == "2025-01-01T00:00:00"
        assert model.os == "linux"
        assert model.os_version == "22.04"

    def test_alias_field_names(self):
        """Test that alias field names work correctly."""
        data = {
            SYS_IMAGE_BASE_IMAGE: "nvidia/jetson:r35.4.1",
            PLATFORM_ECU_ARCH: "arm64",
            OS: "linux",
            OS_VERSION: "ubuntu22.04",
        }

        model = AddImageConfigAnnotations.model_validate(data)

        assert model.base_image == "nvidia/jetson:r35.4.1"
        assert model.architecture == "arm64"
        assert model.os == "linux"
        assert model.os_version == "ubuntu22.04"

    def test_model_dump_by_alias(self):
        """Test that model_dump with by_alias outputs alias names."""
        model = AddImageConfigAnnotations(
            base_image="test-image",  # type: ignore
            architecture="x86_64",  # type: ignore
        )

        dumped = model.model_dump(by_alias=True, exclude_none=True)

        assert SYS_IMAGE_BASE_IMAGE in dumped
        assert PLATFORM_ECU_ARCH in dumped
        assert dumped[SYS_IMAGE_BASE_IMAGE] == "test-image"
        assert dumped[PLATFORM_ECU_ARCH] == "x86_64"


class TestComposeImageConfig:
    """Tests for compose_image_config function."""

    def test_invalid_annotations_exits(self):
        """Test that invalid annotations cause SystemExit."""
        mock_file_table = MagicMock()
        # Missing required fields
        annotations = {}

        with pytest.raises(SystemExit):
            compose_image_config(
                file_table_descriptor=mock_file_table,
                annotations=annotations,
            )
