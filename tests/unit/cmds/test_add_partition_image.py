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
import zstandard
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
    PartitionAction,
    PartitionImageBlobDescriptor,
    PartitionImageBlobZstdDescriptor,
    PartitionImageManifest,
    VendorPackageZstdDescriptor,
)
from ota_image_tools.libs import block_diff
from ota_image_tools.libs.deploy_partition_image import apply_delta
from pydantic import ValidationError

from ota_image_builder.cmds.add_partition_image import add_partition_image_cmd
from ota_image_builder.cmds.finalize import finalize_cmd
from ota_image_builder.v1._image_index import init_ota_image
from ota_image_builder.v1._partition_image import PartitionPayloadSpec

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
SOURCE_DIGEST = "sha256:" + sha256(ROOTFS).hexdigest()


def delta_spec() -> dict:
    """The spec a delta campaign writes: the image is still named, its bytes do not
    ship, and the delta is built from the previous build's image."""
    spec = json.loads(json.dumps(DIRECT_SPEC))
    spec["version"] = "1.3.0"
    spec["partitions"][0] = {
        "name": "rootfs",
        "action": "write",
        "image": "rootfs-new.img",
        "filesystem": "ext4",
        "verity": {"root_hash": ROOT_HASH, "hash_offset": 8192},
        "delta": {"from": "rootfs.img"},
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


def make_args(
    image_root: Path,
    spec: Path,
    annotations_file: Path,
    *sys_configs: str,
    compress: bool = False,
    zstd_level: int = 3,
    delta_from: list[str] | None = None,
    data_only: bool = False,
):
    """The command's namespace. Most tests look at stored bytes, so they store them as
    they are; the compression tests turn it on."""
    return Namespace(
        image_root=str(image_root),
        spec=str(spec),
        annotations_file=str(annotations_file),
        sys_config=list(sys_configs),
        release_key=None,
        no_compress=not compress,
        zstd_level=zstd_level,
        delta_from=delta_from or [],
        data_only=data_only,
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


def _rootfs_of(image_root: Path, ecu_id: str = "autoware"):
    helper = ImageIndexHelper(image_root)
    descriptor = helper.image_index.find_partition_image(
        ImageIdentifier(ecu_id, OTAReleaseKey.dev)
    )
    manifest = descriptor.load_metafile_from_resource_dir(helper.image_resource_dir)
    config = manifest.config.load_metafile_from_resource_dir(helper.image_resource_dir)
    return helper, manifest, config, config.partition("rootfs")


def _reconstruct(helper, rootfs, source: bytes, tmp_path: Path) -> bytes:
    """What the device does with the delta: apply it to the bytes it holds."""
    slot = tmp_path / "slot"
    slot.write_bytes(source)
    dst = tmp_path / "dst"
    dst.write_bytes(b"\0" * rootfs.image.image_size)
    with open(helper.image_resource_dir / rootfs.delta.digest.digest_hex, "rb") as f:
        apply_delta(
            f,
            dst,
            source_dev=slot,
            source_digest_hex=rootfs.delta.annotations.source_digest[7:],
            source_size=rootfs.delta.annotations.source_size,
            target_size=rootfs.image.image_size,
            target_digest=rootfs.image.image_digest[7:],
        )
    return dst.read_bytes()


class TestDeltaSpec:
    """A delta ships the change; the image it reconstructs is still described. The
    delta is built here, from the previous build's image."""

    def test_a_partition_with_a_delta_stores_the_delta_and_not_the_image(
        self, image_root, blobs, annotations_file, sys_config_file, tmp_path
    ):
        spec = tmp_path / "blobs" / "delta.json"
        spec.write_text(json.dumps(delta_spec()))
        add_partition_image_cmd(
            make_args(image_root, spec, annotations_file, f"autoware:{sys_config_file}")
        )

        helper, manifest, config, rootfs = _rootfs_of(image_root)
        assert rootfs.delta is not None
        assert rootfs.delta.annotations.algorithm == "block-diff"
        assert rootfs.delta.annotations.source_digest == SOURCE_DIGEST
        assert rootfs.delta.annotations.source_size == len(ROOTFS)
        # the image is described from the file, byte for byte, without being stored
        assert rootfs.image is not None
        assert str(rootfs.image.digest) == "sha256:" + sha256(NEW_ROOTFS).hexdigest()
        assert rootfs.image.size == len(NEW_ROOTFS)
        assert rootfs.image.annotations.verity_root_hash == ROOT_HASH

        stored = {f.name for f in helper.image_resource_dir.iterdir()}
        assert rootfs.delta.digest.digest_hex in stored
        assert rootfs.image.digest.digest_hex not in stored
        layers = {str(d.digest) for d in manifest.layers}
        assert str(rootfs.delta.digest) in layers
        assert str(rootfs.image.digest) not in layers
        assert config.labels.image_blobs_count == 2  # the delta and the boot files
        assert (
            config.labels.image_blobs_size
            == rootfs.delta.size + config.partition("boot").image.size
        )

        # and the delta is one the device turns back into the image
        with tarfile.open(
            helper.image_resource_dir / rootfs.delta.digest.digest_hex
        ) as tar:
            assert tar.getnames() == [block_diff.OPS_MEMBER, block_diff.LITERALS_MEMBER]
        assert _reconstruct(helper, rootfs, ROOTFS, tmp_path) == NEW_ROOTFS

    def test_the_source_may_be_named_on_the_command_line(
        self, image_root, blobs, annotations_file, tmp_path
    ):
        """A build writes its spec without knowing what the fleet runs; the campaign
        tooling names the base later."""
        base = tmp_path / "released" / "rootfs.img"
        base.parent.mkdir()
        base.write_bytes(ROOTFS)
        (blobs / "rootfs.img").write_bytes(NEW_ROOTFS)
        add_partition_image_cmd(
            make_args(
                image_root,
                blobs / "spec.json",
                annotations_file,
                "autoware:",
                delta_from=[f"rootfs={base}"],
            )
        )
        helper, _, _, rootfs = _rootfs_of(image_root)
        assert rootfs.delta is not None
        assert rootfs.delta.annotations.source_digest == SOURCE_DIGEST
        assert _reconstruct(helper, rootfs, ROOTFS, tmp_path) == NEW_ROOTFS

    @pytest.mark.parametrize(
        ("delta_from", "message"),
        [
            (["rootfs"], "takes NAME=IMAGE"),
            (["scratch=x"], "does not write an image"),
            (["boot=x"], "boot files take no delta"),
            (["rootfs=/nowhere/rootfs.img"], "is not a file"),
        ],
    )
    def test_a_source_that_cannot_be_used_is_refused(
        self, image_root, blobs, annotations_file, capsys, delta_from, message
    ):
        with pytest.raises(SystemExit):
            add_partition_image_cmd(
                make_args(
                    image_root,
                    blobs / "spec.json",
                    annotations_file,
                    "autoware:",
                    delta_from=delta_from,
                )
            )
        assert message in capsys.readouterr().out

    def test_a_delta_needs_the_image_it_reconstructs(self):
        spec = delta_spec()
        del spec["partitions"][0]["image"]
        with pytest.raises(ValidationError, match="needs the image"):
            PartitionPayloadSpec.model_validate(spec)

    def test_a_kept_partition_takes_no_delta(self):
        spec = delta_spec()
        spec["partitions"][3]["delta"] = spec["partitions"][0]["delta"]
        with pytest.raises(ValidationError, match="takes no image"):
            PartitionPayloadSpec.model_validate(spec)


ML_IMAGE = b"\x05" * 8192 + b"\x06" * 4096
NEW_ML_IMAGE = b"\x05" * 8192 + b"\x07" * 4096


def data_image_spec(delta: bool = False) -> dict:
    """A payload carrying a data image beside the partitions: a model set the device
    keeps as a file on optdata and mounts."""
    spec = json.loads(json.dumps(DIRECT_SPEC))
    entry = {
        "name": "models",
        "version": "2026.9.1",
        "mount": "/opt/models",
        "image": "ml-new.img" if delta else "ml.img",
        "filesystem": "squashfs",
        "verity": {"root_hash": ROOT_HASH, "hash_offset": 8192},
        "requires": {"rootfs": {"min": "1.0.0", "max": "2.0.0"}},
    }
    if delta:
        entry["delta"] = {"from": "ml.img"}
    spec["data_images"] = [entry]
    return spec


def data_only_spec() -> dict:
    """Every partition kept, one data image: how models is updated on its own."""
    spec = data_image_spec()
    spec["version"] = "2026.9.1"
    spec["partitions"] = [
        {"name": n, "action": "keep"}
        for n in ("rootfs", "boot", "scratch", "identity", "optdata")
    ]
    return spec


def _data_image_of(image_root: Path, name: str = "models"):
    helper, manifest, config, _ = _rootfs_of(image_root)
    return helper, manifest, config, config.data_image(name)


FIRMWARE = b"\xca\x05" * 4096


def firmware_spec() -> dict:
    """A payload carrying the bootloader firmware beside the partitions, for the
    platform's own updater."""
    spec = json.loads(json.dumps(DIRECT_SPEC))
    spec["firmware"] = {
        "name": "bsp",
        "version": "39.2.0",
        "format": "example-updater.capsule.v1",
        "file": "firmware.pkg",
        "requires": {"rootfs": {"min": "1.0.0"}},
    }
    return spec


class TestFirmware:
    """The firmware package is stored like a vendor package, its format where the
    agent reads it, and listed in the manifest with the rest of the payload."""

    @pytest.fixture(autouse=True)
    def fw_blobs(self, blobs):
        (blobs / "firmware.pkg").write_bytes(FIRMWARE)
        (blobs / "fw.json").write_text(json.dumps(firmware_spec()))
        spec = firmware_spec()
        spec["data_images"] = data_image_spec()["data_images"]
        (blobs / "ml.img").write_bytes(ML_IMAGE)
        (blobs / "fw-data.json").write_text(json.dumps(spec))
        return blobs

    def test_the_spec_parses(self):
        spec = PartitionPayloadSpec.model_validate(firmware_spec())
        assert spec.firmware is not None
        assert spec.firmware.format == "example-updater.capsule.v1"
        bad = firmware_spec()
        bad["firmware"]["file"] = "sub/firmware.pkg"
        with pytest.raises(ValidationError, match="file name next to the spec"):
            PartitionPayloadSpec.model_validate(bad)
        bad = firmware_spec()
        bad["data_images"] = data_image_spec()["data_images"]
        bad["data_images"][0]["name"] = "bsp"
        with pytest.raises(ValidationError, match="named like a data image"):
            PartitionPayloadSpec.model_validate(bad)

    def test_the_package_is_stored_compressed_with_its_format(
        self, image_root, fw_blobs, annotations_file, sys_config_file
    ):
        add_partition_image_cmd(
            make_args(
                image_root,
                fw_blobs / "fw.json",
                annotations_file,
                f"autoware:{sys_config_file}",
                compress=True,
            )
        )
        helper, manifest, config, _ = _rootfs_of(image_root)
        fw = config.firmware
        assert fw is not None
        assert fw.name == "bsp" and fw.version == "39.2.0"
        assert fw.format == "example-updater.capsule.v1" == fw.package.format
        assert fw.requires["rootfs"].allows("1.5.0")
        assert fw.package.mediaType.endswith("firmware-package.v1+zstd")
        assert fw.package.image_size == len(FIRMWARE)
        assert fw.package.image_digest == "sha256:" + sha256(FIRMWARE).hexdigest()
        stored = helper.image_resource_dir / fw.package.digest.digest_hex
        assert zstandard.decompress(stored.read_bytes(), len(FIRMWARE)) == FIRMWARE
        assert manifest.layers[-1].digest == fw.package.digest
        assert config.labels.image_blobs_count == 3

    def test_it_is_listed_after_the_data_images(
        self, image_root, fw_blobs, annotations_file, sys_config_file
    ):
        add_partition_image_cmd(
            make_args(
                image_root,
                fw_blobs / "fw-data.json",
                annotations_file,
                f"autoware:{sys_config_file}",
            )
        )
        _, manifest, config, _ = _rootfs_of(image_root)
        assert config.firmware is not None and config.data_image("models")
        assert [layer.digest for layer in manifest.layers[-2:]] == [
            config.data_image("models").image.digest,
            config.firmware.package.digest,
        ]
        assert config.labels.image_blobs_count == 4

    def test_data_only_drops_the_firmware(
        self, image_root, fw_blobs, annotations_file, sys_config_file
    ):
        """Firmware is slotted with the boot chain: it never travels without the slot
        roles, so the data-image-only payload derived from the same spec has none."""
        add_partition_image_cmd(
            make_args(
                image_root,
                fw_blobs / "fw-data.json",
                annotations_file,
                f"autoware:{sys_config_file}",
                data_only=True,
            )
        )
        _, manifest, config, _ = _rootfs_of(image_root)
        assert config.firmware is None
        assert len(manifest.layers) == 1


class TestDataImages:
    """A data image rides in the config beside the partitions, stored like a
    partition image, and may be the only thing a payload carries."""

    @pytest.fixture(autouse=True)
    def ml_blobs(self, blobs):
        (blobs / "ml.img").write_bytes(ML_IMAGE)
        (blobs / "ml-new.img").write_bytes(NEW_ML_IMAGE)
        (blobs / "data.json").write_text(json.dumps(data_image_spec()))
        (blobs / "data-delta.json").write_text(json.dumps(data_image_spec(delta=True)))
        (blobs / "data-only.json").write_text(json.dumps(data_only_spec()))
        return blobs

    def test_the_spec_parses(self):
        spec = PartitionPayloadSpec.model_validate(data_image_spec())
        assert spec.data_images[0].mount == "/opt/models"
        assert spec.data_images[0].requires["rootfs"].max == "2.0.0"
        bad = data_image_spec()
        bad["data_images"].append(bad["data_images"][0])
        with pytest.raises(ValidationError, match="data image names must be unique"):
            PartitionPayloadSpec.model_validate(bad)
        bad = data_image_spec()
        bad["data_images"][0]["image"] = "sub/ml.img"
        with pytest.raises(ValidationError, match="file name next to the spec"):
            PartitionPayloadSpec.model_validate(bad)

    def test_a_data_image_is_stored_compressed_and_described(
        self, image_root, ml_blobs, annotations_file, sys_config_file
    ):
        add_partition_image_cmd(
            make_args(
                image_root,
                ml_blobs / "data.json",
                annotations_file,
                f"autoware:{sys_config_file}",
                compress=True,
            )
        )
        helper, manifest, config, ml = _data_image_of(image_root)
        assert ml is not None and ml.delta is None
        assert ml.version == "2026.9.1" and ml.mount == "/opt/models"
        assert ml.requires["rootfs"].allows("1.5.0")
        assert ml.image.mediaType.endswith("data-image.v1+zstd")
        assert ml.image.image_size == len(ML_IMAGE)
        assert ml.image.image_digest == "sha256:" + sha256(ML_IMAGE).hexdigest()
        assert ml.image.annotations.filesystem == "squashfs"
        assert ml.image.annotations.verity_hash_offset == 8192
        stored = helper.image_resource_dir / ml.image.digest.digest_hex
        assert zstandard.decompress(stored.read_bytes(), len(ML_IMAGE)) == ML_IMAGE
        # the manifest lists it: partitions first, then the data image
        assert manifest.layers[-1].digest == ml.image.digest
        assert config.labels.image_blobs_count == 3

    def test_a_data_image_ships_as_a_delta_when_its_previous_image_is_named(
        self, image_root, ml_blobs, annotations_file, sys_config_file, tmp_path
    ):
        add_partition_image_cmd(
            make_args(
                image_root,
                ml_blobs / "data-delta.json",
                annotations_file,
                f"autoware:{sys_config_file}",
            )
        )
        helper, manifest, config, ml = _data_image_of(image_root)
        assert ml.delta is not None
        assert (
            ml.delta.annotations.source_digest
            == "sha256:" + sha256(ML_IMAGE).hexdigest()
        )
        assert ml.image.image_digest == "sha256:" + sha256(NEW_ML_IMAGE).hexdigest()
        stored = {f.name for f in helper.image_resource_dir.iterdir()}
        assert ml.delta.digest.digest_hex in stored
        assert ml.image.digest.digest_hex not in stored
        assert _reconstruct(helper, ml, ML_IMAGE, tmp_path) == NEW_ML_IMAGE

    def test_the_source_may_be_named_on_the_command_line(
        self, image_root, ml_blobs, annotations_file, sys_config_file
    ):
        spec = data_image_spec()
        spec["data_images"][0]["image"] = "ml-new.img"
        (ml_blobs / "cli.json").write_text(json.dumps(spec))
        add_partition_image_cmd(
            make_args(
                image_root,
                ml_blobs / "cli.json",
                annotations_file,
                f"autoware:{sys_config_file}",
                delta_from=[f"models={ml_blobs / 'ml.img'}"],
            )
        )
        _, _, _, ml = _data_image_of(image_root)
        assert ml.delta is not None

    def test_an_unknown_name_is_refused(
        self, image_root, ml_blobs, annotations_file, capsys
    ):
        with pytest.raises(SystemExit):
            add_partition_image_cmd(
                make_args(
                    image_root,
                    ml_blobs / "data.json",
                    annotations_file,
                    "autoware:",
                    delta_from=["maps=/nowhere.img"],
                )
            )
        assert "names no such partition or data image" in capsys.readouterr().out

    def test_data_only_derives_the_payload_from_the_release_spec(
        self, image_root, ml_blobs, annotations_file, sys_config_file
    ):
        """The spec that built the rootfs release builds the data-image-only payload
        too: no second spec, and the same blob by digest."""
        add_partition_image_cmd(
            make_args(
                image_root,
                ml_blobs / "data.json",
                annotations_file,
                f"autoware:{sys_config_file}",
                data_only=True,
            )
        )
        helper, manifest, config, ml = _data_image_of(image_root)
        assert config.written_partitions == []
        assert [p.action for p in config.partitions] == [PartitionAction.keep] * 5
        assert [layer.digest for layer in manifest.layers] == [ml.image.digest]
        stored = {f.name for f in helper.image_resource_dir.iterdir()}
        assert sha256(ROOTFS).hexdigest() not in stored

    def test_data_only_needs_a_data_image(
        self, image_root, blobs, annotations_file, capsys
    ):
        with pytest.raises(SystemExit):
            add_partition_image_cmd(
                make_args(
                    image_root,
                    blobs / "spec.json",
                    annotations_file,
                    "autoware:",
                    data_only=True,
                )
            )
        assert "carries no data images" in capsys.readouterr().out

    def test_a_payload_may_carry_only_a_data_image(
        self, image_root, ml_blobs, annotations_file, sys_config_file
    ):
        add_partition_image_cmd(
            make_args(
                image_root,
                ml_blobs / "data-only.json",
                annotations_file,
                f"autoware:{sys_config_file}",
            )
        )
        helper, manifest, config, ml = _data_image_of(image_root)
        assert config.written_partitions == []
        assert [layer.digest for layer in manifest.layers] == [ml.image.digest]
        assert config.image_version == "2026.9.1"


class TestCompression:
    """Blobs are stored zstd-compressed unless told otherwise; what the partition ends
    up holding is then in the annotations."""

    def test_images_and_the_package_are_compressed_by_default(
        self, image_root, blobs, annotations_file
    ):
        add_partition_image_cmd(
            make_args(
                image_root,
                blobs / "spec.json",
                annotations_file,
                "autoware:",
                compress=True,
            )
        )
        helper, manifest, config, rootfs = _rootfs_of(image_root)
        assert isinstance(rootfs.image, PartitionImageBlobZstdDescriptor)
        assert (
            rootfs.image.annotations.uncompressed_digest
            == "sha256:" + sha256(ROOTFS).hexdigest()
        )
        assert rootfs.image.annotations.uncompressed_size == len(ROOTFS)
        assert rootfs.image.annotations.verity_root_hash == ROOT_HASH
        stored = (
            helper.image_resource_dir / rootfs.image.digest.digest_hex
        ).read_bytes()
        assert rootfs.image.size == len(stored) < len(ROOTFS)
        assert (
            zstandard.ZstdDecompressor().decompress(stored, max_output_size=len(ROOTFS))
            == ROOTFS
        )
        # the boot files stay as they are
        boot = config.partition("boot")
        assert isinstance(boot.image, BootFilesDescriptor)
        assert config.labels.image_blobs_size == rootfs.image.size + boot.image.size
        assert manifest.layers == [rootfs.image, boot.image]

    def test_the_vendor_package_is_compressed_too(
        self, image_root, tmp_path, annotations_file
    ):
        d = tmp_path / "vendor"
        d.mkdir()
        (d / "update.pkg").write_bytes(b"opaque" * 100)
        (d / "spec.json").write_text(
            json.dumps(
                {
                    "delivery": "vendor-package",
                    "version": "2.0.0",
                    "package": {"file": "update.pkg", "format": "example-format"},
                    "partitions": [{"name": "rootfs", "action": "write"}],
                }
            )
        )
        add_partition_image_cmd(
            make_args(
                image_root, d / "spec.json", annotations_file, "ecu:", compress=True
            )
        )
        _, manifest, config, _ = _rootfs_of(image_root, "ecu")
        assert isinstance(config.package, VendorPackageZstdDescriptor)
        assert config.package.format == "example-format"
        assert config.package.annotations.uncompressed_size == 600
        assert (
            config.package.annotations.uncompressed_digest
            == "sha256:" + sha256(b"opaque" * 100).hexdigest()
        )
        assert manifest.layers == [config.package]

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


class TestInputsAndNames:
    """What is refused before a byte is stored, and how a name that is both a partition
    and a data image is told apart on the command line."""

    @pytest.fixture(autouse=True)
    def ml_blobs(self, blobs):
        (blobs / "ml.img").write_bytes(ML_IMAGE)
        (blobs / "ml-new.img").write_bytes(NEW_ML_IMAGE)
        (blobs / "data.json").write_text(json.dumps(data_image_spec()))
        spec = data_image_spec(delta=False)
        spec["data_images"][0]["name"] = "rootfs"
        (blobs / "data-rootfs.json").write_text(json.dumps(spec))
        return blobs

    def test_a_data_image_delta_source_may_be_named_with_the_data_prefix(
        self, image_root, blobs, annotations_file, tmp_path
    ):
        (blobs / "ml.img").write_bytes(NEW_ML_IMAGE)
        add_partition_image_cmd(
            make_args(
                image_root,
                blobs / "data.json",
                annotations_file,
                "autoware:",
                delta_from=[f"data:models={blobs / 'ml-new.img'}"],
            )
        )
        _, _, config, _ = _rootfs_of(image_root)
        (entry,) = config.data_images
        assert entry.delta is not None

    def test_a_name_that_is_both_a_partition_and_a_data_image_is_ambiguous(
        self, image_root, blobs, annotations_file, capsys
    ):
        with pytest.raises(SystemExit):
            add_partition_image_cmd(
                make_args(
                    image_root,
                    blobs / "data-rootfs.json",
                    annotations_file,
                    "autoware:",
                    delta_from=[f"rootfs={blobs / 'rootfs-new.img'}"],
                )
            )
        assert (
            "both a partition and a data image are called that"
            in capsys.readouterr().out
        )

    def test_a_missing_input_is_found_before_anything_is_stored(
        self, image_root, blobs, annotations_file, capsys
    ):
        (blobs / "ml.img").unlink()
        before = sorted((image_root / RESOURCE_DIR).rglob("*"))
        with pytest.raises(SystemExit):
            add_partition_image_cmd(
                make_args(
                    image_root, blobs / "data.json", annotations_file, "autoware:"
                )
            )
        assert (
            "blob ml.img named by the spec is not next to it" in capsys.readouterr().out
        )
        assert sorted((image_root / RESOURCE_DIR).rglob("*")) == before, (
            "no blob stored"
        )

    def test_a_payload_the_schema_refuses_is_a_message_not_a_traceback(
        self, image_root, blobs, annotations_file, capsys
    ):
        """A data image named after a partition role passes the spec and is refused by
        the schema at config time: said as a refusal, after the blobs it would have
        stored are already in the resource dir (the schema is the last word)."""
        with pytest.raises(SystemExit):
            add_partition_image_cmd(
                make_args(
                    image_root,
                    blobs / "data-rootfs.json",
                    annotations_file,
                    "autoware:",
                )
            )
        assert "the spec names a payload the schema refuses" in capsys.readouterr().out


class TestRawStorage:
    """--no-compress stores data images and firmware as they are, under the raw
    descriptor types; and a vendor-package delivery has no data-only form."""

    @pytest.fixture(autouse=True)
    def more_blobs(self, blobs):
        (blobs / "ml.img").write_bytes(ML_IMAGE)
        (blobs / "data.json").write_text(json.dumps(data_image_spec()))
        (blobs / "firmware.pkg").write_bytes(b"\xed\xd5\xcb\x6d" + b"\xca\x05" * 500)
        (blobs / "firmware.json").write_text(json.dumps(firmware_spec()))
        return blobs

    def test_a_data_image_and_a_firmware_package_are_stored_raw_when_asked(
        self, image_root, blobs, annotations_file
    ):
        from ota_image_libs.v1.partition_image.schema import (
            DataImageBlobDescriptor,
            FirmwarePackageDescriptor,
        )

        add_partition_image_cmd(
            make_args(image_root, blobs / "data.json", annotations_file, "autoware:")
        )
        _, _, config, _ = _rootfs_of(image_root)
        (entry,) = config.data_images
        assert isinstance(entry.image, DataImageBlobDescriptor)
        assert entry.image.image_size == len(ML_IMAGE)

        root2 = image_root.parent / "ota_image_fw"
        init_ota_image(root2, {BUILD_TOOL_VERSION: "test"})
        add_partition_image_cmd(
            make_args(root2, blobs / "firmware.json", annotations_file, "autoware:")
        )
        _, _, config, _ = _rootfs_of(root2)
        assert config.firmware is not None
        assert isinstance(config.firmware.package, FirmwarePackageDescriptor)
        assert config.firmware.package.format == config.firmware.format

    def test_data_only_has_no_meaning_for_a_vendor_package(
        self, image_root, blobs, annotations_file, capsys
    ):
        (blobs / "pkg.bin").write_bytes(b"P" * 600)
        spec = {
            "delivery": "vendor-package",
            "version": "1.0.0",
            "package": {"file": "pkg.bin", "format": "example-format"},
            "partitions": [
                {"name": n, "action": "write" if n in ("rootfs", "boot") else "keep"}
                for n in ("rootfs", "boot", "scratch", "identity", "optdata")
            ],
            "data_images": data_image_spec()["data_images"],
        }
        (blobs / "vendor.json").write_text(json.dumps(spec))
        with pytest.raises(SystemExit):
            add_partition_image_cmd(
                make_args(
                    image_root,
                    blobs / "vendor.json",
                    annotations_file,
                    "autoware:",
                    data_only=True,
                )
            )
        assert (
            "only a direct delivery can keep every partition" in capsys.readouterr().out
        )
