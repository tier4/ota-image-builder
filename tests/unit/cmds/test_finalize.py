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
"""Unit tests for cmds/finalize.py module."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ota_image_builder.cmds.finalize import (
    _collect_protected_resources_digest,
    finalize_cmd,
)


class TestCollectProtectedResourcesDigest:
    """Tests for _collect_protected_resources_digest function."""

    def test_returns_set_of_bytes(self):
        """Test that function returns a set of bytes when no manifests."""
        mock_helper = MagicMock()
        mock_helper.image_index.manifests = []

        result = _collect_protected_resources_digest(mock_helper)

        assert isinstance(result, set)
        assert len(result) == 0


class TestFinalizeCmd:
    """Tests for finalize_cmd function."""

    def test_invalid_ota_image_exits(self, tmp_path: Path):
        """Test that invalid OTA image directory causes SystemExit."""
        image_root = tmp_path / "invalid_image"
        image_root.mkdir()

        args = Namespace(
            image_root=str(image_root),
            tmp_dir=None,
            o_skip_bundle=False,
            o_skip_compression=False,
            o_skip_slice=False,
        )

        with pytest.raises(SystemExit):
            finalize_cmd(args)
