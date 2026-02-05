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
"""Unit tests for cmds/init.py module."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from ota_image_libs.v1.annotation_keys import (
    PILOT_AUTO_PLATFORM,
    PILOT_AUTO_PROJECT_BRANCH,
    PILOT_AUTO_PROJECT_COMMIT,
    PILOT_AUTO_PROJECT_SOURCE,
    PILOT_AUTO_PROJECT_VERSION,
    WEB_AUTO_CATALOG,
    WEB_AUTO_CATALOG_ID,
    WEB_AUTO_ENV,
    WEB_AUTO_PROJECT,
    WEB_AUTO_PROJECT_ID,
)

from ota_image_builder.cmds.init import InitCMDAnnotations, init_cmd


class TestInitCMDAnnotations:
    """Tests for InitCMDAnnotations pydantic model."""

    def test_all_fields_optional(self):
        """Test that all fields are optional and can be omitted."""
        model = InitCMDAnnotations()

        assert model.pilot_auto_platform is None
        assert model.pilot_auto_source_repo is None
        assert model.pilot_auto_version is None
        assert model.pilot_auto_release_commit is None
        assert model.pilot_auto_release_branch is None
        assert model.web_auto_project is None
        assert model.web_auto_project_id is None
        assert model.web_auto_catalog is None
        assert model.web_auto_catalog_id is None
        assert model.web_auto_env is None

    def test_populate_all_fields(self):
        """Test populating all fields."""
        model = InitCMDAnnotations(
            pilot_auto_platform="test-platform",
            pilot_auto_source_repo="https://github.com/test/repo",
            pilot_auto_version="1.0.0",
            pilot_auto_release_commit="abc123",
            pilot_auto_release_branch="main",
            web_auto_project="test-project",
            web_auto_project_id="proj-123",
            web_auto_catalog="test-catalog",
            web_auto_catalog_id="cat-456",
            web_auto_env="production",
        )

        assert model.pilot_auto_platform == "test-platform"
        assert model.pilot_auto_source_repo == "https://github.com/test/repo"
        assert model.pilot_auto_version == "1.0.0"
        assert model.pilot_auto_release_commit == "abc123"
        assert model.pilot_auto_release_branch == "main"
        assert model.web_auto_project == "test-project"
        assert model.web_auto_project_id == "proj-123"
        assert model.web_auto_catalog == "test-catalog"
        assert model.web_auto_catalog_id == "cat-456"
        assert model.web_auto_env == "production"

    def test_alias_field_names(self):
        """Test that alias field names work correctly."""
        data = {
            PILOT_AUTO_PLATFORM: "platform-via-alias",
            PILOT_AUTO_PROJECT_SOURCE: "source-via-alias",
            PILOT_AUTO_PROJECT_VERSION: "version-via-alias",
            PILOT_AUTO_PROJECT_COMMIT: "commit-via-alias",
            PILOT_AUTO_PROJECT_BRANCH: "branch-via-alias",
            WEB_AUTO_PROJECT: "project-via-alias",
            WEB_AUTO_PROJECT_ID: "proj-id-via-alias",
            WEB_AUTO_CATALOG: "catalog-via-alias",
            WEB_AUTO_CATALOG_ID: "cat-id-via-alias",
            WEB_AUTO_ENV: "env-via-alias",
        }
        model = InitCMDAnnotations.model_validate(data)

        assert model.pilot_auto_platform == "platform-via-alias"
        assert model.pilot_auto_source_repo == "source-via-alias"
        assert model.pilot_auto_version == "version-via-alias"
        assert model.pilot_auto_release_commit == "commit-via-alias"
        assert model.pilot_auto_release_branch == "branch-via-alias"
        assert model.web_auto_project == "project-via-alias"
        assert model.web_auto_project_id == "proj-id-via-alias"
        assert model.web_auto_catalog == "catalog-via-alias"
        assert model.web_auto_catalog_id == "cat-id-via-alias"
        assert model.web_auto_env == "env-via-alias"

    def test_partial_fields(self):
        """Test populating only some fields."""
        model = InitCMDAnnotations(
            pilot_auto_platform="my-platform",
            pilot_auto_version="2.0.0",
        )

        assert model.pilot_auto_platform == "my-platform"
        assert model.pilot_auto_version == "2.0.0"
        assert model.pilot_auto_source_repo is None
        assert model.web_auto_project is None

    def test_model_dump_uses_aliases(self):
        """Test that model_dump can output with alias names."""
        model = InitCMDAnnotations(
            pilot_auto_platform="test-platform",
        )

        # by_alias=True should use the annotation key names
        dumped = model.model_dump(by_alias=True, exclude_none=True)

        assert PILOT_AUTO_PLATFORM in dumped
        assert dumped[PILOT_AUTO_PLATFORM] == "test-platform"

    def test_extra_fields_ignored(self):
        """Test that extra fields are handled according to model config."""
        data = {
            PILOT_AUTO_PLATFORM: "platform",
            "unknown_field": "should be ignored or raise error",
        }

        # Depending on model config, this either ignores extra or raises
        # AliasEnabledModel typically allows extra fields
        model = InitCMDAnnotations.model_validate(data)
        assert model.pilot_auto_platform == "platform"


class TestInitCmd:
    """Tests for init_cmd function."""

    def test_init_cmd_success(self, tmp_path: Path):
        """Test successful initialization of OTA image."""
        image_root = tmp_path / "ota_image"
        annotations_file = tmp_path / "annotations.yaml"

        # Create valid annotations file
        annotations = {PILOT_AUTO_PLATFORM: "test-platform"}
        annotations_file.write_text(yaml.dump(annotations))

        args = Namespace(
            image_root=str(image_root),
            annotations_file=str(annotations_file),
        )

        with patch("ota_image_builder.cmds.init.init_ota_image") as mock_init:
            init_cmd(args)
            mock_init.assert_called_once()

    def test_init_cmd_nonempty_dir_exits(self, tmp_path: Path):
        """Test that non-empty directory causes SystemExit."""
        image_root = tmp_path / "ota_image"
        image_root.mkdir()
        (image_root / "existing_file.txt").write_text("content")

        annotations_file = tmp_path / "annotations.yaml"
        annotations_file.write_text(yaml.dump({}))

        args = Namespace(
            image_root=str(image_root),
            annotations_file=str(annotations_file),
        )

        with pytest.raises(SystemExit):
            init_cmd(args)

    def test_init_cmd_invalid_annotations_exits(self, tmp_path: Path):
        """Test that invalid annotations file causes SystemExit."""
        image_root = tmp_path / "ota_image"
        annotations_file = tmp_path / "nonexistent.yaml"

        args = Namespace(
            image_root=str(image_root),
            annotations_file=str(annotations_file),
        )

        with pytest.raises(SystemExit):
            init_cmd(args)

    def test_init_cmd_init_failure_exits(self, tmp_path: Path):
        """Test that init_ota_image failure causes SystemExit."""
        image_root = tmp_path / "ota_image"
        annotations_file = tmp_path / "annotations.yaml"

        annotations_file.write_text(yaml.dump({}))

        args = Namespace(
            image_root=str(image_root),
            annotations_file=str(annotations_file),
        )

        with patch(
            "ota_image_builder.cmds.init.init_ota_image",
            side_effect=Exception("init failed"),
        ):
            with pytest.raises(SystemExit):
                init_cmd(args)
