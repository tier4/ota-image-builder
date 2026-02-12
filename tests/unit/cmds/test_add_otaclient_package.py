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
"""Unit tests for cmds/add_otaclient_package.py module."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from ota_image_builder.cmds.add_otaclient_package import add_otaclient_package_cmd


class TestAddOtaclientPackageCmd:
    """Tests for add_otaclient_package_cmd function."""

    def test_invalid_ota_image_exits(self, tmp_path: Path):
        """Test that invalid OTA image directory causes SystemExit."""
        image_root = tmp_path / "invalid_image"
        image_root.mkdir()

        args = Namespace(
            image_root=str(image_root),
            release_dir=str(tmp_path),
        )

        with pytest.raises(SystemExit):
            add_otaclient_package_cmd(args)

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
            "ota_image_builder.cmds.add_otaclient_package.check_if_valid_ota_image",
            return_value=True,
        )
        with pytest.raises(SystemExit):
            add_otaclient_package_cmd(args)

    def test_success(self, tmp_path: Path, mocker: MockerFixture):
        """Test successful adding of otaclient package."""
        image_root = tmp_path / "ota_image"
        image_root.mkdir()
        release_dir = tmp_path / "release"
        release_dir.mkdir()

        args = Namespace(
            image_root=str(image_root),
            release_dir=str(release_dir),
        )

        mocker.patch(
            "ota_image_builder.cmds.add_otaclient_package.check_if_valid_ota_image",
            return_value=True,
        )
        mock_helper_class = mocker.patch(
            "ota_image_builder.cmds.add_otaclient_package.ImageIndexHelper"
        )
        mock_helper = mocker.MagicMock()
        mock_helper.image_index.image_finalized = False
        mock_helper.image_index.image_signed = False
        mock_helper.image_index.find_otaclient_package.return_value = None
        mock_helper_class.return_value = mock_helper

        mock_add = mocker.patch(
            "ota_image_builder.cmds.add_otaclient_package.add_otaclient_package"
        )
        mock_add.return_value = mocker.MagicMock()

        add_otaclient_package_cmd(args)

        mock_helper.sync_index.assert_called_once()
        mock_add.assert_called_once()
