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
"""Unit tests for cmds/add_otaclient_package_compat.py module."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from ota_image_builder.cmds.add_otaclient_package_compat import (
    OTACLIENT_RELEASE_DIR_LEGACY,
    add_otaclient_package_compat_cmd,
)


class TestAddOtaclientPackageCompatCmd:
    """Tests for add_otaclient_package_compat_cmd function."""

    def test_invalid_ota_image_exits(self, tmp_path: Path):
        """Test that invalid OTA image directory causes SystemExit."""
        image_root = tmp_path / "invalid_image"
        image_root.mkdir()

        args = Namespace(
            image_root=str(image_root),
            release_dir=str(tmp_path),
        )

        with pytest.raises(SystemExit):
            add_otaclient_package_compat_cmd(args)

    def test_nonexistent_release_dir_exits(self, tmp_path: Path, mocker: MockerFixture):
        """Test that non-existent release directory causes SystemExit."""
        image_root = tmp_path / "ota_image"
        image_root.mkdir()
        release_dir = tmp_path / "nonexistent"

        args = Namespace(
            image_root=str(image_root),
            release_dir=str(release_dir),
        )

        mocker.patch(
            "ota_image_builder.cmds.add_otaclient_package_compat.check_if_valid_ota_image",
            return_value=True,
        )
        with pytest.raises(SystemExit):
            add_otaclient_package_compat_cmd(args)

    def test_success(self, tmp_path: Path, mocker: MockerFixture):
        """Test successful adding of otaclient package with legacy compat."""
        image_root = tmp_path / "ota_image"
        image_root.mkdir()
        release_dir = tmp_path / "release"
        release_dir.mkdir()

        # Create a test file in release_dir
        test_file = release_dir / "test.txt"
        test_file.write_text("test content")

        args = Namespace(
            image_root=str(image_root),
            release_dir=str(release_dir),
        )

        mocker.patch(
            "ota_image_builder.cmds.add_otaclient_package_compat.check_if_valid_ota_image",
            return_value=True,
        )
        mock_helper_class = mocker.patch(
            "ota_image_builder.cmds.add_otaclient_package_compat.ImageIndexHelper",
        )
        mock_helper = mocker.MagicMock()
        mock_helper.image_index.image_finalized = False
        mock_helper.image_index.image_signed = False
        mock_helper_class.return_value = mock_helper

        add_otaclient_package_compat_cmd(args)

        # Check that the release was copied to the legacy location
        expected_dest = image_root / OTACLIENT_RELEASE_DIR_LEGACY / "test.txt"
        assert expected_dest.exists()
        assert expected_dest.read_text() == "test content"

    def test_already_added_exits(self, tmp_path: Path, mocker: MockerFixture):
        """Test that already existing legacy compat package causes SystemExit."""
        image_root = tmp_path / "ota_image"
        image_root.mkdir()
        release_dir = tmp_path / "release"
        release_dir.mkdir()

        # Create destination directory to simulate already added package
        dest_dir = image_root / OTACLIENT_RELEASE_DIR_LEGACY
        dest_dir.mkdir(parents=True)

        args = Namespace(
            image_root=str(image_root),
            release_dir=str(release_dir),
        )

        mocker.patch(
            "ota_image_builder.cmds.add_otaclient_package_compat.check_if_valid_ota_image",
            return_value=True,
        )

        with pytest.raises(SystemExit):
            add_otaclient_package_compat_cmd(args)
