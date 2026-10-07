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

import pytest

from ota_image_builder.cmds.finalize import (
    _collect_protected_resources_digest,
    finalize_cmd,
)


class TestCollectProtectedResourcesDigest:
    """Tests for _collect_protected_resources_digest function."""

    def test_returns_set_of_bytes(self, mocker):
        """Test that function returns a set of bytes when no manifests."""
        mock_helper = mocker.MagicMock()
        mock_helper.image_index.manifests = []

        result = _collect_protected_resources_digest(mock_helper)

        assert isinstance(result, set)
        assert len(result) == 0

    def test_the_bundles_of_either_agent_manifest_kind_are_protected(self, mocker):
        """An update agent release package and an OTAClient release package both hold
        bundles that must never be bundled, compressed or sliced; a pipeline with an
        older step still writes the latter."""
        from ota_image_libs.v1.otaclient_package.schema import OTAClientPackageManifest
        from ota_image_libs.v1.update_agent_package.schema import (
            UpdateAgentPackageManifest,
        )

        def manifest_descriptor(kind, bundle_digest: bytes, own_digest: bytes):
            d = mocker.MagicMock()
            d.__class__ = kind.Descriptor  # what the isinstance dispatch looks at
            d.digest.digest = own_digest
            bundle = mocker.MagicMock()
            bundle.digest.digest = bundle_digest
            d.load_metafile_from_resource_dir.return_value.layers = [bundle]
            return d

        mock_helper = mocker.MagicMock()
        mock_helper.image_index.manifests = [
            manifest_descriptor(
                UpdateAgentPackageManifest, b"ua-bundle", b"ua-manifest"
            ),
            manifest_descriptor(OTAClientPackageManifest, b"oc-bundle", b"oc-manifest"),
        ]

        result = _collect_protected_resources_digest(mock_helper)

        assert result == {b"ua-bundle", b"ua-manifest", b"oc-bundle", b"oc-manifest"}


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
