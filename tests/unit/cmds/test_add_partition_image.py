# Copyright 2026 TIER IV, INC. All rights reserved.
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
"""Unit tests for cmds/add_partition_image.py: a partition-based payload, end to end
against a real OTA image directory."""

from __future__ import annotations

import io
import json
import tarfile
from argparse import Namespace
from hashlib import sha256
from pathlib import Path

import pytest
from ota_image_libs.v1.annotation_keys import (
    BUILD_TOOL_VERSION,
    OTA_RELEASE_KEY,
    PLATFORM_ECU_ARCH,
    SYS_IMAGE_BASE_IMAGE,
)
from ota_image_libs.v1.consts import RESOURCE_DIR
from ota_image_libs.v1.image_index.utils import ImageIndexHelper
from ota_image_libs.v1.image_manifest.schema import ImageIdentifier, OTAReleaseKey
from ota_image_libs.v1.partition_image.schema import (
    ActionPerformer,
    BootFilesDescriptor,
    PartitionImageBlobDescriptor,
    PartitionImageManifest,
)
from pydantic import ValidationError

from ota_image_builder.cmds.add_partition_image import (
    PartitionPayloadSpec,
    add_partition_image_cmd,
)
from ota_image_builder.cmds.finalize import finalize_cmd
from ota_image_builder.v1._image_index import init_ota_image

ROOT_HASH = "194fde591ff4d762eb379215fa650c61f359d9ea66dc4c3be9b27e9a02726abc"
ROOTFS = b"\x01" * 8192 + b"\x02" * 4096
ANNOTATIONS = {
    SYS_IMAGE_BASE_IMAGE: "ubuntu:24.04",
    PLATFORM_ECU_ARCH: "x86_64",
    OTA_RELEASE_KEY: "dev",
    "vnd.tier4.image.os": "linux",
    "vnd.tier4.image.os.version": "24.04",
}
DIRECT_SPEC = {
    "delivery": "direct",
    "version": "1.2.0",
    "partitions": [
        {
            "name": "rootfs",
            "action": "write",
            "image": "rootfs.img",
            "filesystem": "ext4",
            "verity": {"root_hash": ROOT_HASH, "hash_offset": 8192},
        },
        {"name": "boot", "action": "write", "image": "boot.tar"},
        {"name": "scratch", "action": "mkfs"},
        {"name": "identity", "action": "keep"},
        {"name": "optdata", "action": "keep"},
    ],
}


NEW_ROOTFS = b"\x01" * 8192 + b"\x03" * 4096
"""The next build of the same partition: what a delta reconstructs."""
DELTA = b"a delta that turns ROOTFS into NEW_ROOTFS"
SOURCE_DIGEST = "sha256:" + sha256(ROOTFS).hexdigest()


def delta_spec(*, store_image: bool = False) -> dict:
    """The spec a delta campaign writes: the image is still named, its bytes may not
    ship."""
    spec = json.loads(json.dumps(DIRECT_SPEC))
    spec["version"] = "1.3.0"
    spec["partitions"][0] = {
        "name": "rootfs",
        "action": "write",
        "image": "rootfs-new.img",
        "filesystem": "ext4",
        "verity": {"root_hash": ROOT_HASH, "hash_offset": 8192},
        "store_image": store_image,
        "delta": {
            "file": "rootfs.delta",
            "algorithm": "zstd-patch-from",
            "source": {"digest": SOURCE_DIGEST, "size": len(ROOTFS)},
        },
    }
    return spec


def boot_tar() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in (("vmlinuz", b"k"), ("initrd.img", b"i"), ("grub.cfg", b"c")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def image_root(tmp_path: Path) -> Path:
    root = tmp_path / "ota_image"
    init_ota_image(root, {BUILD_TOOL_VERSION: "test"})
    return root


@pytest.fixture
def blobs(tmp_path: Path) -> Path:
    d = tmp_path / "blobs"
    d.mkdir()
    (d / "rootfs.img").write_bytes(ROOTFS)
    (d / "boot.tar").write_bytes(boot_tar())
    (d / "spec.json").write_text(json.dumps(DIRECT_SPEC))
    (d / "rootfs-new.img").write_bytes(NEW_ROOTFS)
    (d / "rootfs.delta").write_bytes(DELTA)
    return d


@pytest.fixture
def annotations_file(tmp_path: Path) -> Path:
    f = tmp_path / "annotations.yaml"
    f.write_text("\n".join(f'"{k}": "{v}"' for k, v in ANNOTATIONS.items()) + "\n")
    return f


@pytest.fixture
def sys_config_file(tmp_path: Path) -> Path:
    f = tmp_path / "sys_config.yaml"
    f.write_text("hostname: main\npersist_files:\n  - /etc/hosts\n")
    return f


def make_args(image_root: Path, spec: Path, annotations_file: Path, *sys_configs: str):
    return Namespace(
        image_root=str(image_root),
        spec=str(spec),
        annotations_file=str(annotations_file),
        sys_config=list(sys_configs),
        release_key=None,
    )


class TestSpec:
    def test_direct_spec_parses(self):
        spec = PartitionPayloadSpec.model_validate(DIRECT_SPEC)
        assert spec.partitions[0].blob_kind == "partition"
        assert spec.partitions[1].blob_kind == "boot-files"

    @pytest.mark.parametrize(
        ("change", "message"),
        [
            (
                {
                    "partitions": DIRECT_SPEC["partitions"][:1]
                    + [{"name": "boot", "action": "write"}]
                },
                "names no image",
            ),
            (
                {"partitions": [dict(DIRECT_SPEC["partitions"][2], image="x")]},
                "takes no image",
            ),
            (
                {
                    "partitions": DIRECT_SPEC["partitions"]
                    + [{"name": "optdata", "action": "keep"}]
                },
                "must be unique",
            ),
            ({"package": {"file": "pkg"}}, "takes no package"),
            ({"version": ""}, "at least 1 character"),
            (
                {"partitions": [dict(DIRECT_SPEC["partitions"][0], image="../x")]},
                "file name next to the spec",
            ),
            (
                {"partitions": [dict(DIRECT_SPEC["partitions"][1], filesystem="ext4")]},
                "boot files carry no",
            ),
        ],
    )
    def test_invalid_specs(self, change, message):
        with pytest.raises(ValueError, match=message):
            PartitionPayloadSpec.model_validate({**DIRECT_SPEC, **change})

    def test_vendor_package_spec(self):
        spec = PartitionPayloadSpec.model_validate(
            {
                "delivery": "vendor-package",
                "version": "1.0.0",
                "package": {"file": "update.pkg", "format": "example"},
                "partitions": [
                    {"name": "rootfs", "action": "write"},
                    {"name": "identity", "action": "keep"},
                ],
            }
        )
        assert spec.package is not None and spec.package.format == "example"
        with pytest.raises(ValueError, match="needs the package"):
            PartitionPayloadSpec.model_validate(
                {
                    "delivery": "vendor-package",
                    "version": "1",
                    "partitions": [{"name": "r", "action": "write"}],
                }
            )
        with pytest.raises(ValueError, match="names no image of its own"):
            PartitionPayloadSpec.model_validate(
                {
                    "delivery": "vendor-package",
                    "version": "1",
                    "package": {"file": "p"},
                    "partitions": [{"name": "r", "action": "write", "image": "r.img"}],
                }
            )


class TestDeltaSpec:
    """A delta ships the change; the image it reconstructs is still described."""

    def test_a_delta_only_payload_stores_the_delta_and_not_the_image(
        self, image_root, blobs, annotations_file, sys_config_file, tmp_path
    ):
        spec = tmp_path / "blobs" / "delta.json"
        spec.write_text(json.dumps(delta_spec()))
        add_partition_image_cmd(
            make_args(image_root, spec, annotations_file, f"autoware:{sys_config_file}")
        )

        helper = ImageIndexHelper(image_root)
        descriptor = helper.image_index.find_partition_image(
            ImageIdentifier("autoware", OTAReleaseKey.dev)
        )
        manifest = descriptor.load_metafile_from_resource_dir(helper.image_resource_dir)
        config = manifest.config.load_metafile_from_resource_dir(
            helper.image_resource_dir
        )
        rootfs = config.partition("rootfs")
        assert rootfs.delta is not None
        assert rootfs.delta.annotations.algorithm == "zstd-patch-from"
        assert rootfs.delta.annotations.source_digest == SOURCE_DIGEST
        assert rootfs.delta.annotations.source_size == len(ROOTFS)
        # the image is described from the file, byte for byte, without being stored
        assert rootfs.image is not None
        assert str(rootfs.image.digest) == "sha256:" + sha256(NEW_ROOTFS).hexdigest()
        assert rootfs.image.size == len(NEW_ROOTFS)
        assert rootfs.image.annotations.verity_root_hash == ROOT_HASH
        assert rootfs.image_in_storage is False

        stored = {f.name for f in helper.image_resource_dir.iterdir()}
        assert str(rootfs.delta.digest).removeprefix("sha256:") in stored
        assert str(rootfs.image.digest).removeprefix("sha256:") not in stored
        layers = {str(d.digest) for d in manifest.layers}
        assert str(rootfs.delta.digest) in layers
        assert str(rootfs.image.digest) not in layers

    def test_a_payload_may_ship_both_the_image_and_the_delta(
        self, image_root, blobs, annotations_file, sys_config_file, tmp_path
    ):
        """One image then serves devices at any version."""
        spec = tmp_path / "blobs" / "delta.json"
        spec.write_text(json.dumps(delta_spec(store_image=True)))
        add_partition_image_cmd(
            make_args(image_root, spec, annotations_file, f"autoware:{sys_config_file}")
        )
        helper = ImageIndexHelper(image_root)
        descriptor = helper.image_index.find_partition_image(
            ImageIdentifier("autoware", OTAReleaseKey.dev)
        )
        manifest = descriptor.load_metafile_from_resource_dir(helper.image_resource_dir)
        config = manifest.config.load_metafile_from_resource_dir(
            helper.image_resource_dir
        )
        rootfs = config.partition("rootfs")
        stored = {f.name for f in helper.image_resource_dir.iterdir()}
        assert str(rootfs.image.digest).removeprefix("sha256:") in stored
        assert str(rootfs.delta.digest).removeprefix("sha256:") in stored
        assert rootfs.image_in_storage is True

    def test_a_delta_needs_the_image_it_reconstructs(self):
        spec = delta_spec()
        del spec["partitions"][0]["image"]
        with pytest.raises(ValidationError, match="needs the image"):
            PartitionPayloadSpec.model_validate(spec)

    def test_not_storing_the_image_needs_a_delta(self):
        spec = delta_spec()
        del spec["partitions"][0]["delta"]
        with pytest.raises(ValidationError, match="needs a delta"):
            PartitionPayloadSpec.model_validate(spec)

    def test_the_delta_is_a_file_beside_the_spec(self):
        spec = delta_spec()
        spec["partitions"][0]["delta"]["file"] = "../elsewhere.delta"
        with pytest.raises(ValidationError, match="file name next to the spec"):
            PartitionPayloadSpec.model_validate(spec)

    def test_a_kept_partition_takes_no_delta(self):
        spec = delta_spec()
        spec["partitions"][3]["delta"] = spec["partitions"][0]["delta"]
        with pytest.raises(ValidationError, match="takes no image"):
            PartitionPayloadSpec.model_validate(spec)


class TestAddPartitionImageCmd:
    def test_adds_the_payload_and_finalizes_without_a_resource_table(
        self, image_root, blobs, annotations_file, sys_config_file
    ):
        add_partition_image_cmd(
            make_args(
                image_root,
                blobs / "spec.json",
                annotations_file,
                f"autoware:{sys_config_file}",
            )
        )

        helper = ImageIndexHelper(image_root)
        index = helper.image_index
        assert [i.ecu_id for i in index.image_identifiers] == ["autoware"]
        descriptor = index.find_partition_image(
            ImageIdentifier("autoware", OTAReleaseKey.dev)
        )
        assert descriptor is not None
        manifest = descriptor.load_metafile_from_resource_dir(helper.image_resource_dir)
        assert isinstance(manifest, PartitionImageManifest)
        assert manifest.annotations.pilot_auto_platform_ecu_arch == "x86_64"

        config = manifest.config.load_metafile_from_resource_dir(
            helper.image_resource_dir
        )
        assert config.image_version == "1.2.0"
        assert config.delivery == "direct"
        assert [p.name for p in config.partitions] == [
            "rootfs",
            "boot",
            "scratch",
            "identity",
            "optdata",
        ]
        rootfs = config.partition("rootfs")
        assert rootfs is not None and isinstance(
            rootfs.image, PartitionImageBlobDescriptor
        )
        assert rootfs.image.annotations is not None
        assert rootfs.image.annotations.verity_root_hash == ROOT_HASH
        assert rootfs.image.annotations.verity_hash_offset == 8192
        assert rootfs.image.annotations.filesystem == "ext4"
        boot = config.partition("boot")
        assert boot is not None and isinstance(boot.image, BootFilesDescriptor)
        assert config.sys_config is not None
        assert config.labels.image_blobs_count == 2
        assert config.labels.image_blobs_size == len(ROOTFS) + len(boot_tar())
        assert config.labels.os_version == "24.04"
        assert manifest.layers == [rootfs.image, boot.image]

        # the blobs are in the blob storage, byte for byte
        blob = helper.image_resource_dir / rootfs.image.digest.digest_hex
        assert blob.read_bytes() == ROOTFS
        assert sha256(ROOTFS).hexdigest() == rootfs.image.digest.digest_hex
        raw = json.loads(
            (helper.image_resource_dir / descriptor.digest.digest_hex).read_text()
        )
        assert raw["config"]["mediaType"].endswith(
            "partition-based-ota-image.config.v1+json"
        )

        # a partition-only image has no resource_table; finalize leaves the blobs alone
        finalize_cmd(
            Namespace(
                image_root=str(image_root),
                tmp_dir=None,
                o_skip_bundle=False,
                o_skip_compression=False,
                o_skip_slice=False,
            )
        )
        index = ImageIndexHelper(image_root).image_index
        assert index.image_finalized
        assert index.image_resource_table is None
        assert index.annotations.total_blobs_count == len(
            list((image_root / RESOURCE_DIR).iterdir())
        )
        assert blob.read_bytes() == ROOTFS

        with pytest.raises(SystemExit):
            add_partition_image_cmd(
                make_args(
                    image_root,
                    blobs / "spec.json",
                    annotations_file,
                    f"sub:{sys_config_file}",
                )
            )

    def test_multi_spec_shares_the_blobs(
        self, image_root, blobs, annotations_file, sys_config_file
    ):
        add_partition_image_cmd(
            make_args(
                image_root,
                blobs / "spec.json",
                annotations_file,
                f"main:{sys_config_file}",
                "sub:",
            )
        )
        index = ImageIndexHelper(image_root).image_index
        assert sorted(i.ecu_id for i in index.image_identifiers) == ["main", "sub"]
        main = index.find_partition_image(ImageIdentifier("main", OTAReleaseKey.dev))
        sub = index.find_partition_image(ImageIdentifier("sub", OTAReleaseKey.dev))
        assert main is not None and sub is not None and main.digest != sub.digest
        # one copy of each blob
        blobs_in_store = list((image_root / RESOURCE_DIR).iterdir())
        assert sha256(ROOTFS).hexdigest() in {b.name for b in blobs_in_store}
        assert len([b for b in blobs_in_store if b.stat().st_size == len(ROOTFS)]) == 1

    def test_vendor_package_delivery(self, image_root, tmp_path, annotations_file):
        d = tmp_path / "vendor"
        d.mkdir()
        (d / "update.pkg").write_bytes(b"opaque" * 100)
        (d / "spec.json").write_text(
            json.dumps(
                {
                    "delivery": "vendor-package",
                    "version": "2.0.0",
                    "package": {"file": "update.pkg", "format": "example-format"},
                    "partitions": [
                        {"name": "rootfs", "action": "write"},
                        {"name": "boot", "action": "write"},
                        {"name": "identity", "action": "keep"},
                    ],
                }
            )
        )
        add_partition_image_cmd(
            make_args(image_root, d / "spec.json", annotations_file, "ecu:")
        )
        helper = ImageIndexHelper(image_root)
        descriptor = helper.image_index.find_partition_image(
            ImageIdentifier("ecu", OTAReleaseKey.dev)
        )
        assert descriptor is not None
        manifest = descriptor.load_metafile_from_resource_dir(helper.image_resource_dir)
        config = manifest.config.load_metafile_from_resource_dir(
            helper.image_resource_dir
        )
        assert config.delivery == "vendor-package"
        assert config.package is not None and config.package.annotations is not None
        assert config.package.annotations.format == "example-format"
        assert all(
            p.performed_by == ActionPerformer.package for p in config.written_partitions
        )
        assert config.sys_config is None
        assert manifest.layers == [config.package]

    @pytest.mark.parametrize(
        ("break_it", "message"),
        [
            (lambda d: (d / "boot.tar").unlink(), "not next to it"),
            (lambda d: (d / "spec.json").write_text("{not json"), "invalid spec"),
            (lambda d: (d / "spec.json").unlink(), "does not exist"),
        ],
    )
    def test_refusals(
        self, image_root, blobs, annotations_file, capsys, break_it, message
    ):
        break_it(blobs)
        with pytest.raises(SystemExit):
            add_partition_image_cmd(
                make_args(
                    image_root, blobs / "spec.json", annotations_file, "autoware:"
                )
            )
        assert message in capsys.readouterr().out
        assert ImageIndexHelper(image_root).image_index.manifests == []

    def test_release_key_from_cli_wins(self, image_root, blobs, annotations_file):
        args = make_args(image_root, blobs / "spec.json", annotations_file, "autoware:")
        args.release_key = "prd"
        add_partition_image_cmd(args)
        index = ImageIndexHelper(image_root).image_index
        assert (
            index.find_partition_image(ImageIdentifier("autoware", OTAReleaseKey.prd))
            is not None
        )
        assert (
            index.find_partition_image(ImageIdentifier("autoware", OTAReleaseKey.dev))
            is None
        )

    def test_not_an_ota_image(self, tmp_path, blobs, annotations_file):
        with pytest.raises(SystemExit):
            add_partition_image_cmd(
                make_args(tmp_path, blobs / "spec.json", annotations_file, "autoware:")
            )
