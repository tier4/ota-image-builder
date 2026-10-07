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
"""Composing a partition-based payload: the spec JSON the platform tooling writes
beside its blobs, how the blobs are stored, and the payload's config and manifest.

The spec's shape is described in the README.
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, NamedTuple

from ota_image_libs.common.io import file_sha256
from ota_image_libs.v1.annotation_keys import (
    OTA_IMAGE_BLOBS_COUNT,
    OTA_IMAGE_BLOBS_SIZE,
    PARTITION_IMAGE_DELTA_ALGORITHM,
    PARTITION_IMAGE_DELTA_SOURCE_DIGEST,
    PARTITION_IMAGE_DELTA_SOURCE_SIZE,
    PARTITION_IMAGE_FILESYSTEM,
    PARTITION_IMAGE_FIRMWARE_FORMAT,
    PARTITION_IMAGE_UNCOMPRESSED_DIGEST,
    PARTITION_IMAGE_UNCOMPRESSED_SIZE,
    PARTITION_IMAGE_VENDOR_PACKAGE_FORMAT,
    PARTITION_IMAGE_VERITY_HASH_OFFSET,
    PARTITION_IMAGE_VERITY_ROOT_HASH,
)
from ota_image_libs.v1.image_config.sys_config import SysConfig
from ota_image_libs.v1.partition_image.schema import (
    DELTA_ALGORITHM_BLOCK_DIFF,
    ActionPerformer,
    BootFilesDescriptor,
    DataImageBlobDescriptor,
    DataImageBlobZstdDescriptor,
    DataImageEntry,
    DeliveryMode,
    FirmwareEntry,
    FirmwarePackageDescriptor,
    FirmwarePackageZstdDescriptor,
    PartitionAction,
    PartitionDeltaDescriptor,
    PartitionEntry,
    PartitionImageBlobDescriptor,
    PartitionImageBlobZstdDescriptor,
    PartitionImageConfig,
    PartitionImageManifest,
    PayloadBlobDescriptor,
    VendorPackageDescriptor,
    VendorPackageZstdDescriptor,
    VersionRange,
    payload_descriptors,
)
from ota_image_tools.libs import block_diff
from pydantic import BaseModel, Field, model_validator

from ota_image_builder._common import exit_with_err_msg, human_readable_size
from ota_image_builder.v1._image_config import AddImageConfigAnnotations
from ota_image_builder.v1._image_manifest import AddImageManifestAnnotations

logger = logging.getLogger(__name__)

BOOT_FILES_ROLE = "boot"
"""The written role whose blob is the slot's boot files tar, not a partition image."""

DEFAULT_ZSTD_LEVEL = 19
"""On an 8.15 GiB rootfs image: 2.40 GB at 19 against 2.87 GB at 3, in six minutes
against eleven seconds. Delta literals barely move with the level."""


class AddPartitionImageAnnotations(
    AddImageManifestAnnotations, AddImageConfigAnnotations
):
    """Annotations needed for add-partition-image cmd."""


# ------ the spec file ------ #


class VeritySpec(BaseModel):
    root_hash: str = Field(pattern=r"^[0-9a-fA-F]{32,128}$")
    hash_offset: int = Field(ge=0)


class DeltaSpec(BaseModel):
    # The previous build's image, relative to the spec file or absolute.
    from_: str = Field(alias="from", min_length=1)


class PartitionSpec(BaseModel):
    """A partition role and what the update does with it. `image` is a file name next
    to the spec; it, `filesystem`, `verity` and `delta` go only with `write`."""

    name: str
    action: PartitionAction
    image: str | None = None
    filesystem: str | None = None
    verity: VeritySpec | None = None
    delta: DeltaSpec | None = None

    # Checked before any blob is stored: the libs schema refuses the same things, but
    # only once the blobs have been copied.
    @model_validator(mode="after")
    def _consistent(self):
        if self.action != PartitionAction.write and (
            self.image or self.filesystem or self.verity or self.delta
        ):
            raise ValueError(f"partition {self.name!r}: {self.action} takes no image")
        if self.delta is not None and self.blob_kind != "partition":
            raise ValueError(f"partition {self.name!r}: boot files take no delta")
        if self.delta is not None and not self.image:
            raise ValueError(
                f"partition {self.name!r}: a delta needs the image it reconstructs, so "
                "that its digest, size and verity can be recorded"
            )
        if self.image is not None and ("/" in self.image or not self.image):
            raise ValueError(
                f"partition {self.name!r}: image must be a file name next to the spec"
            )
        if self.blob_kind == "boot-files" and (self.filesystem or self.verity):
            raise ValueError(
                f"partition {self.name!r}: boot files carry no filesystem or verity"
            )
        return self

    @property
    def blob_kind(self) -> Literal["partition", "boot-files"]:
        return "boot-files" if self.name == BOOT_FILES_ROLE else "partition"


class VendorPackageSpec(BaseModel):
    file: str
    format: str | None = None


class VersionRangeSpec(BaseModel):
    min: str | None = None
    max: str | None = None

    def to_range(self) -> VersionRange:
        return VersionRange(min=self.min, max=self.max)


class FirmwareSpec(BaseModel):
    """The package the platform's own firmware updater applies, opaque here but for
    `format`. `file` is a file name next to the spec."""

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    format: str = Field(min_length=1)
    file: str
    requires: dict[str, VersionRangeSpec] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self):
        if "/" in self.file or not self.file:
            raise ValueError(
                f"firmware {self.name!r}: file must be a file name next to the spec"
            )
        return self


class DataImageSpec(BaseModel):
    """A data image: a filesystem image the device keeps as a file outside the slots
    and mounts at `mount`. `image` is a file name next to the spec."""

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    mount: str
    image: str
    filesystem: str | None = None
    verity: VeritySpec | None = None
    requires: dict[str, VersionRangeSpec] = Field(default_factory=dict)
    delta: DeltaSpec | None = None

    @model_validator(mode="after")
    def _consistent(self):
        if "/" in self.image or not self.image:
            raise ValueError(
                f"data image {self.name!r}: image must be a file name next to the spec"
            )
        return self


class PartitionPayloadSpec(BaseModel):
    """The JSON the platform tooling writes beside the blobs (see the README)."""

    delivery: DeliveryMode
    version: str = Field(min_length=1)
    partitions: list[PartitionSpec] = Field(min_length=1)
    package: VendorPackageSpec | None = None
    firmware: FirmwareSpec | None = None
    data_images: list[DataImageSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _delivery_consistent(self):
        names = [p.name for p in self.partitions]
        if len(set(names)) != len(names):
            raise ValueError(f"partition names must be unique: {names}")
        data_names = [d.name for d in self.data_images]
        if len(set(data_names)) != len(data_names):
            raise ValueError(f"data image names must be unique: {data_names}")
        if self.firmware is not None and self.firmware.name in data_names:
            raise ValueError(
                f"firmware {self.firmware.name!r} is named like a data image"
            )
        for p in self.partitions:
            if p.action != PartitionAction.write:
                continue
            if self.delivery == DeliveryMode.direct and not p.image:
                raise ValueError(f"partition {p.name!r} is written but names no image")
            if self.delivery == DeliveryMode.vendor_package and p.image:
                raise ValueError(
                    f"partition {p.name!r}: with a vendor package the package "
                    "writes it; it names no image of its own"
                )
        if self.delivery == DeliveryMode.direct and self.package:
            raise ValueError("delivery 'direct' takes no package")
        if self.delivery == DeliveryMode.vendor_package and not self.package:
            raise ValueError("delivery 'vendor-package' needs the package")
        return self


def data_only(spec: PartitionPayloadSpec) -> PartitionPayloadSpec:
    """The same spec with every partition kept: what updates the data images alone."""
    if not spec.data_images:
        exit_with_err_msg("--data-only: the spec carries no data images")
    if spec.delivery != DeliveryMode.direct:
        exit_with_err_msg(
            "--data-only: only a direct delivery can keep every partition"
        )
    if spec.firmware is not None:
        # Firmware never travels without the slot roles (it is slotted with the boot
        # chain where it is slotted at all), so a data-only payload drops it.
        logger.info(
            f"--data-only: the firmware entry {spec.firmware.name!r} is left out"
        )
    return spec.model_copy(
        update={
            "partitions": [
                PartitionSpec(name=p.name, action=PartitionAction.keep)
                for p in spec.partitions
            ],
            "package": None,
            "firmware": None,
        }
    )


def delta_sources(
    spec: PartitionPayloadSpec, spec_path: Path, args: list[str]
) -> dict[str, Path]:
    """Where each partition's or data image's delta is built from: the spec's `delta`,
    overridden by `--delta-from NAME=IMAGE`. Data images are keyed `data:NAME`, since
    one may share a partition role's name."""
    sources: dict[str, Path] = {}
    for p in spec.partitions:
        if p.delta is not None:
            sources[p.name] = (spec_path.parent / p.delta.from_).resolve()
    for d in spec.data_images:
        if d.delta is not None:
            sources[f"data:{d.name}"] = (spec_path.parent / d.delta.from_).resolve()
    for arg in args:
        name, sep, image = arg.partition("=")
        if not sep or not name or not image:
            exit_with_err_msg(
                f"--delta-from takes NAME=IMAGE or data:NAME=IMAGE, not {arg!r}"
            )
        if name.startswith("data:"):
            name = name[len("data:") :]
            if not any(d.name == name for d in spec.data_images):
                exit_with_err_msg(
                    f"--delta-from data:{name}: the spec names no such data image"
                )
            sources[f"data:{name}"] = Path(image).resolve()
            continue
        p = next((p for p in spec.partitions if p.name == name), None)
        d = next((d for d in spec.data_images if d.name == name), None)
        if p is not None and d is not None:
            exit_with_err_msg(
                f"--delta-from {name}: both a partition and a data image are called that; "
                f"say data:{name}=IMAGE for the data image"
            )
        if p is not None:
            if p.action != PartitionAction.write or not p.image:
                exit_with_err_msg(
                    f"--delta-from {name}: the spec does not write an image to that partition"
                )
            if p.blob_kind != "partition":
                exit_with_err_msg(f"--delta-from {name}: boot files take no delta")
            sources[name] = Path(image).resolve()
        elif d is not None:
            sources[f"data:{name}"] = Path(image).resolve()
        else:
            exit_with_err_msg(
                f"--delta-from {name}: the spec names no such partition or data image"
            )
    return sources


# ------ storing the blobs ------ #


class PayloadBlobs(NamedTuple):
    """What `add_payload_blobs` stored, as the entries of the config."""

    partitions: list[PartitionEntry]
    package: VendorPackageDescriptor | VendorPackageZstdDescriptor | None
    data_images: list[DataImageEntry]
    firmware: FirmwareEntry | None

    @property
    def descriptors(self) -> list[PayloadBlobDescriptor]:
        return payload_descriptors(
            self.partitions, self.package, self.data_images, self.firmware
        )


def _blob_path(spec_path: Path, name: str) -> Path:
    _f = spec_path.parent / name
    if not _f.is_file():
        exit_with_err_msg(f"blob {name} named by the spec is not next to it: {_f}")
    return _f


def _check_inputs(
    spec: PartitionPayloadSpec, spec_path: Path, delta_sources: dict[str, Path]
) -> None:
    """Every file the spec and the command line name, before the first blob is stored."""
    if spec.delivery == DeliveryMode.direct:
        for p in spec.partitions:
            if p.action == PartitionAction.write and p.image:
                _blob_path(spec_path, p.image)
    if spec.package is not None:
        _blob_path(spec_path, spec.package.file)
    for d in spec.data_images:
        _blob_path(spec_path, d.image)
    if spec.firmware is not None:
        _blob_path(spec_path, spec.firmware.file)
    for name, source in delta_sources.items():
        if not source.is_file():
            exit_with_err_msg(
                f"--delta-from {name}: the delta's source {source} is not a file"
            )


def _digest_of(path: Path) -> str:
    return f"sha256:{file_sha256(path).hexdigest()}"


def _add_delta(
    name: str, source: Path, target: Path, resource_dir: Path, *, zstd_level: int
) -> tuple[PartitionDeltaDescriptor, block_diff.Stats]:
    """Build the block diff that turns `source` into `target` and store it."""
    logger.info(
        f"Build the block diff for {name!r} from {source} "
        f"({human_readable_size(source.stat().st_size)}) to {target} ..."
    )
    fd, tmp_name = tempfile.mkstemp(dir=resource_dir)
    delta_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out:
            stats = block_diff.encode(source, target, out, level=zstd_level)
    except (OSError, ValueError) as e:
        delta_path.unlink(missing_ok=True)
        exit_with_err_msg(f"partition {name!r}: building the delta failed: {e}")
    logger.info(
        f"  {human_readable_size(stats.copied)} copied from the device's own slot, "
        f"{human_readable_size(stats.zero)} zero, "
        f"{human_readable_size(stats.literal)} literal -> "
        f"{human_readable_size(stats.literal_compressed)} compressed, {stats.runs} runs"
    )
    delta = PartitionDeltaDescriptor.add_file_to_resource_dir(
        delta_path,
        resource_dir,
        remove_origin=True,
        annotations={
            PARTITION_IMAGE_DELTA_ALGORITHM: DELTA_ALGORITHM_BLOCK_DIFF,
            PARTITION_IMAGE_DELTA_SOURCE_DIGEST: f"sha256:{stats.source_digest}",
            PARTITION_IMAGE_DELTA_SOURCE_SIZE: stats.source_size,
        },
    )
    return delta, stats


def _store_image(
    name: str,
    blob: Path,
    resource_dir: Path,
    *,
    raw_type,
    zstd_type,
    annotations: dict[str, Any],
    compressor,
    zstd_level: int,
    delta_source: Path | None,
):
    """An image blob the agent writes (a partition image or a data image): compressed
    unless told otherwise, or a block diff instead of the image. Returns the image
    descriptor (described, not stored, when a delta ships) and the delta's, if any."""
    size = human_readable_size(blob.stat().st_size)
    if delta_source is not None:
        delta, stats = _add_delta(
            name, delta_source, blob, resource_dir, zstd_level=zstd_level
        )
        image = raw_type(
            digest=f"sha256:{stats.target_digest}",
            size=stats.target_size,
            annotations=annotations or None,
        )
        return image, delta
    if compressor is not None:
        logger.info(
            f"Add the image of {name!r} from {blob} ({size}), zstd level {zstd_level} ..."
        )
        annotations = {
            **annotations,
            PARTITION_IMAGE_UNCOMPRESSED_DIGEST: _digest_of(blob),
            PARTITION_IMAGE_UNCOMPRESSED_SIZE: blob.stat().st_size,
        }
        image = zstd_type.add_file_to_resource_dir(
            blob,
            resource_dir,
            annotations=annotations,
            zstd_compression_level=compressor,
        )
        logger.info(f"  stored as {human_readable_size(image.size)}")
        return image, None
    logger.info(f"Add the image of {name!r} from {blob} ({size}) as it is ...")
    image = raw_type.add_file_to_resource_dir(
        blob, resource_dir, annotations=annotations or None
    )
    return image, None


def _image_annotations(filesystem: str | None, verity: VeritySpec | None) -> dict:
    annotations: dict[str, Any] = {}
    if filesystem:
        annotations[PARTITION_IMAGE_FILESYSTEM] = filesystem
    if verity:
        annotations[PARTITION_IMAGE_VERITY_ROOT_HASH] = verity.root_hash
        annotations[PARTITION_IMAGE_VERITY_HASH_OFFSET] = verity.hash_offset
    return annotations


def _add_data_image(
    d: DataImageSpec,
    blob: Path,
    resource_dir: Path,
    *,
    compressor,
    zstd_level: int,
    delta_source: Path | None,
) -> DataImageEntry:
    image, delta = _store_image(
        d.name,
        blob,
        resource_dir,
        raw_type=DataImageBlobDescriptor,
        zstd_type=DataImageBlobZstdDescriptor,
        annotations=_image_annotations(d.filesystem, d.verity),
        compressor=compressor,
        zstd_level=zstd_level,
        delta_source=delta_source,
    )
    return DataImageEntry(
        name=d.name,
        version=d.version,
        mount=d.mount,
        requires={k: v.to_range() for k, v in d.requires.items()},
        image=image,
        delta=delta,
    )


def _add_written_image(
    p: PartitionSpec,
    blob: Path,
    resource_dir: Path,
    *,
    compressor,
    zstd_level: int,
    delta_source: Path | None,
) -> PartitionEntry:
    """The entry of a partition the agent writes: its boot files as they are, its
    image compressed unless told otherwise, or a block diff instead of the image."""
    size = human_readable_size(blob.stat().st_size)
    if p.blob_kind == "boot-files":
        logger.info(f"Add boot files for {p.name!r} from {blob} ({size}) ...")
        image = BootFilesDescriptor.add_file_to_resource_dir(blob, resource_dir)
        return PartitionEntry(name=p.name, action=p.action, image=image)

    image, delta = _store_image(
        p.name,
        blob,
        resource_dir,
        raw_type=PartitionImageBlobDescriptor,
        zstd_type=PartitionImageBlobZstdDescriptor,
        annotations=_image_annotations(p.filesystem, p.verity),
        compressor=compressor,
        zstd_level=zstd_level,
        delta_source=delta_source,
    )
    return PartitionEntry(name=p.name, action=p.action, image=image, delta=delta)


def _add_package(
    package: VendorPackageSpec,
    blob: Path,
    resource_dir: Path,
    *,
    compressor,
    zstd_level: int,
) -> VendorPackageDescriptor | VendorPackageZstdDescriptor:
    size = human_readable_size(blob.stat().st_size)
    annotations: dict[str, Any] = {}
    if package.format:
        annotations[PARTITION_IMAGE_VENDOR_PACKAGE_FORMAT] = package.format
    if compressor is not None:
        logger.info(
            f"Add vendor package from {blob} ({size}), zstd level {zstd_level} ..."
        )
        annotations[PARTITION_IMAGE_UNCOMPRESSED_DIGEST] = _digest_of(blob)
        annotations[PARTITION_IMAGE_UNCOMPRESSED_SIZE] = blob.stat().st_size
        desc = VendorPackageZstdDescriptor.add_file_to_resource_dir(
            blob,
            resource_dir,
            annotations=annotations,
            zstd_compression_level=compressor,
        )
        logger.info(f"  stored as {human_readable_size(desc.size)}")
        return desc
    logger.info(f"Add vendor package from {blob} ({size}) as it is ...")
    return VendorPackageDescriptor.add_file_to_resource_dir(
        blob, resource_dir, annotations=annotations or None
    )


def _add_firmware(
    fw: FirmwareSpec,
    blob: Path,
    resource_dir: Path,
    *,
    compressor,
    zstd_level: int,
) -> FirmwareEntry:
    """The firmware package, stored like a vendor package, its format in the
    annotations where the agent reads it before staging."""
    size = human_readable_size(blob.stat().st_size)
    annotations: dict[str, Any] = {PARTITION_IMAGE_FIRMWARE_FORMAT: fw.format}
    if compressor is not None:
        logger.info(
            f"Add firmware package {fw.name!r} {fw.version} from {blob} ({size}), "
            f"zstd level {zstd_level} ..."
        )
        annotations[PARTITION_IMAGE_UNCOMPRESSED_DIGEST] = _digest_of(blob)
        annotations[PARTITION_IMAGE_UNCOMPRESSED_SIZE] = blob.stat().st_size
        desc = FirmwarePackageZstdDescriptor.add_file_to_resource_dir(
            blob,
            resource_dir,
            annotations=annotations,
            zstd_compression_level=compressor,
        )
        logger.info(f"  stored as {human_readable_size(desc.size)}")
    else:
        logger.info(
            f"Add firmware package {fw.name!r} {fw.version} from {blob} ({size}) as it is ..."
        )
        desc = FirmwarePackageDescriptor.add_file_to_resource_dir(
            blob, resource_dir, annotations=annotations
        )
    return FirmwareEntry(
        name=fw.name,
        version=fw.version,
        format=fw.format,
        requires={k: v.to_range() for k, v in fw.requires.items()},
        package=desc,
    )


def add_payload_blobs(
    spec: PartitionPayloadSpec,
    spec_path: Path,
    resource_dir: Path,
    *,
    compress: bool,
    zstd_level: int,
    delta_sources: dict[str, Path],
) -> PayloadBlobs:
    """Store every blob the spec names and return the config entries describing them."""
    _check_inputs(spec, spec_path, delta_sources)
    compressor = block_diff.compressor(zstd_level) if compress else None
    entries: list[PartitionEntry] = []
    for p in spec.partitions:
        if p.action != PartitionAction.write:
            entries.append(PartitionEntry(name=p.name, action=p.action))
        elif spec.delivery == DeliveryMode.vendor_package:
            entries.append(
                PartitionEntry(
                    name=p.name, action=p.action, performed_by=ActionPerformer.package
                )
            )
        else:
            assert p.image is not None
            entries.append(
                _add_written_image(
                    p,
                    _blob_path(spec_path, p.image),
                    resource_dir,
                    compressor=compressor,
                    zstd_level=zstd_level,
                    delta_source=delta_sources.get(p.name),
                )
            )
    package = None
    if spec.package is not None:
        package = _add_package(
            spec.package,
            _blob_path(spec_path, spec.package.file),
            resource_dir,
            compressor=compressor,
            zstd_level=zstd_level,
        )
    data_images = [
        _add_data_image(
            d,
            _blob_path(spec_path, d.image),
            resource_dir,
            compressor=compressor,
            zstd_level=zstd_level,
            delta_source=delta_sources.get(f"data:{d.name}"),
        )
        for d in spec.data_images
    ]
    firmware = None
    if spec.firmware is not None:
        firmware = _add_firmware(
            spec.firmware,
            _blob_path(spec_path, spec.firmware.file),
            resource_dir,
            compressor=compressor,
            zstd_level=zstd_level,
        )
    return PayloadBlobs(entries, package, data_images, firmware)


# ------ the config and the manifest ------ #


def compose_partition_image_config(
    *,
    spec: PartitionPayloadSpec,
    blobs: PayloadBlobs,
    sys_config_descriptor: SysConfig.Descriptor | None = None,
    annotations: dict[str, Any],
) -> PartitionImageConfig:
    try:
        validated_annotations = AddImageConfigAnnotations.model_validate(annotations)
    except Exception as e:
        exit_with_err_msg(f"Invalid annotations: {e}")

    descriptors = blobs.descriptors
    labels = {
        **annotations,
        OTA_IMAGE_BLOBS_COUNT: len(descriptors),
        OTA_IMAGE_BLOBS_SIZE: sum(d.size for d in descriptors),
    }
    return PartitionImageConfig(
        description=validated_annotations.description,
        created=validated_annotations.created
        or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        architecture=validated_annotations.architecture,
        os=validated_annotations.os,
        os_version=validated_annotations.os_version,  # type: ignore
        image_version=spec.version,
        delivery=spec.delivery,
        partitions=blobs.partitions,
        data_images=blobs.data_images,
        firmware=blobs.firmware,
        package=blobs.package,
        sys_config=sys_config_descriptor,
        labels=PartitionImageConfig.Annotations.model_validate(labels),
    )


def compose_partition_image_manifest(
    *,
    config_descriptor: PartitionImageConfig.Descriptor,
    layers: list[PayloadBlobDescriptor],
    annotations: dict[str, Any],
) -> PartitionImageManifest:
    return PartitionImageManifest(
        config=config_descriptor,
        layers=layers,
        annotations=PartitionImageManifest.Annotations.model_validate(annotations),
    )
