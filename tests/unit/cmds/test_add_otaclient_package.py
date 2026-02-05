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
from unittest.mock import MagicMock, patch

import pytest

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

    def test_nonexistent_release_dir_exits(self, tmp_path: Path):
        """Test that non-existent release directory causes SystemExit."""
        image_root = tmp_path / "ota_image"
        image_root.mkdir()
        release_dir = tmp_path / "nonexistent"

        args = Namespace(
            image_root=str(image_root),
            release_dir=str(release_dir),
        )

        with patch(
            "ota_image_builder.cmds.add_otaclient_package.check_if_valid_ota_image",
            return_value=True,
        ):
            with pytest.raises(SystemExit):
                add_otaclient_package_cmd(args)

    def test_success(self, tmp_path: Path):
        """Test successful adding of otaclient package."""
        image_root = tmp_path / "ota_image"
        image_root.mkdir()
        release_dir = tmp_path / "release"
        release_dir.mkdir()

        args = Namespace(
            image_root=str(image_root),
            release_dir=str(release_dir),
        )

        with patch(
            "ota_image_builder.cmds.add_otaclient_package.check_if_valid_ota_image",
            return_value=True,
        ):
            with patch(
                "ota_image_builder.cmds.add_otaclient_package.ImageIndexHelper"
            ) as mock_helper_class:
                mock_helper = MagicMock()
                mock_helper_class.return_value = mock_helper

                with patch(
                    "ota_image_builder.cmds.add_otaclient_package.add_otaclient_package"
                ) as mock_add:
                    mock_add.return_value = MagicMock()

                    add_otaclient_package_cmd(args)

                    mock_helper.sync_index.assert_called_once()
                    mock_add.assert_called_once()
