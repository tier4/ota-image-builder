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

from argparse import ArgumentParser, Namespace
from pathlib import Path

import pytest

from ota_image_builder.cmds import finalize
from ota_image_builder.cmds.finalize import (
    _collect_protected_resources_digest,
    finalize_cmd,
    finalize_cmd_args,
)

MODULE = finalize.__name__


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
        a config and bundles that must never be bundled, compressed or sliced (the
        update agent package's config is the 2-byte `{}`, which a rootfs may well hold
        as a file too); a pipeline with an older step still writes the latter."""
        from ota_image_libs.v1.otaclient_package.schema import OTAClientPackageManifest
        from ota_image_libs.v1.update_agent_package.schema import (
            UpdateAgentPackageManifest,
        )

        def manifest_descriptor(kind, prefix: bytes):
            d = mocker.MagicMock()
            d.__class__ = kind.Descriptor  # what the isinstance dispatch looks at
            d.digest.digest = prefix + b"-manifest"
            bundle = mocker.MagicMock()
            bundle.digest.digest = prefix + b"-bundle"
            loaded = d.load_metafile_from_resource_dir.return_value
            loaded.layers = [bundle]
            loaded.config.digest.digest = prefix + b"-config"
            return d

        mock_helper = mocker.MagicMock()
        mock_helper.image_index.manifests = [
            manifest_descriptor(UpdateAgentPackageManifest, b"ua"),
            manifest_descriptor(OTAClientPackageManifest, b"oc"),
        ]

        result = _collect_protected_resources_digest(mock_helper)

        assert result == {
            f"{kind}-{part}".encode()
            for kind in ("ua", "oc")
            for part in ("manifest", "config", "bundle")
        }


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
            slice_size=32,
        )

        with pytest.raises(SystemExit):
            finalize_cmd(args)

    @pytest.mark.parametrize(
        "argv, expected",
        (
            pytest.param([], 4, id="default"),
            pytest.param(["--slice-size", "1"], 1, id="1MiB"),
            pytest.param(["--slice-size", "4"], 4, id="4MiB"),
            pytest.param(["--slice-size", "32"], 32, id="32MiB"),
        ),
    )
    def test_slice_size_arg(self, argv: list[str], expected: int):
        parser = ArgumentParser()
        finalize_cmd_args(parser.add_subparsers())

        args = parser.parse_args(["finalize", *argv, "image_root"])

        assert args.slice_size == expected

    @pytest.mark.parametrize("slice_size", ("0", "-1", "33", "4.5"))
    def test_slice_size_out_of_range_refused(self, slice_size: str, capsys):
        parser = ArgumentParser()
        finalize_cmd_args(parser.add_subparsers())

        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["finalize", "--slice-size", slice_size, "image_root"])

        assert exc_info.value.code == 2
        assert "--slice-size" in capsys.readouterr().err

    @pytest.mark.parametrize("slice_size", (1, 4))
    def test_slice_size_reaches_slice_filter(
        self, slice_size: int, tmp_path: Path, mocker
    ):
        mocker.patch(f"{MODULE}.check_if_valid_ota_image", return_value=True)
        mocker.patch(f"{MODULE}.ImageIndexHelper")
        mocker.patch(
            f"{MODULE}._collect_protected_resources_digest", return_value=set()
        )
        mocker.patch(f"{MODULE}.BundleFilterProcesser")
        mocker.patch(f"{MODULE}.CompressionFilterProcesser")
        mocker.patch(f"{MODULE}.vacuum_db")
        rst_descriptor = mocker.patch(f"{MODULE}.ZstdCompressedResourceTableDescriptor")
        rst_descriptor.add_file_to_resource_dir.return_value.size = 1
        mocker.patch(f"{MODULE}.count_blobs_in_dir", return_value=(0, 0))
        slice_filter = mocker.patch(f"{MODULE}.SliceFilterProcesser")

        finalize_cmd(
            Namespace(
                image_root=str(tmp_path),
                tmp_dir=str(tmp_path),
                o_skip_bundle=False,
                o_skip_compression=False,
                o_skip_slice=False,
                slice_size=slice_size,
            )
        )

        assert slice_filter.call_args.kwargs["slice_size"] == slice_size * 1024**2
