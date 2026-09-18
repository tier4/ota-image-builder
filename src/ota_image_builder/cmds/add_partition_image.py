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
"""CLI interface for adding a partition-based image payload into an OTA image.

A partition-based payload carries whole partition images (a read-only root with its
integrity metadata, the boot files that go with it) or one vendor package, instead of
the files of a rootfs. The platform's own tooling produces those blobs; this command
takes them, with a small JSON spec that says which blob goes to which partition role,
and turns them into an OTA image payload: blobs in the blob storage as they are, a
partition image config, and one image manifest per ECU spec.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ota_image_libs.common import AliasEnabledModel
from ota_image_libs.v1.annotation_keys import (
    OS,
    OS_VERSION,
    OTA_IMAGE_BLOBS_COUNT,
    OTA_IMAGE_BLOBS_SIZE,
    OTA_RELEASE_KEY,
    PARTITION_IMAGE_DELTA_ALGORITHM,
    PARTITION_IMAGE_DELTA_SOURCE_DIGEST,
    PARTITION_IMAGE_DELTA_SOURCE_SIZE,
    PARTITION_IMAGE_FILESYSTEM,
    PARTITION_IMAGE_VENDOR_PACKAGE_FORMAT,
    PARTITION_IMAGE_VERITY_HASH_OFFSET,
    PARTITION_IMAGE_VERITY_ROOT_HASH,
    PILOT_AUTO_PLATFORM,
    PLATFORM_ECU,
    PLATFORM_ECU_ARCH,
    PLATFORM_ECU_HARDWARE_MODEL,
    PLATFORM_ECU_HARDWARE_SERIES,
    SYS_IMAGE_BASE_IMAGE,
)
from ota_image_libs.v1.image_config.sys_config import SysConfig
from ota_image_libs.v1.image_index.utils import ImageIndexHelper
from ota_image_libs.v1.image_manifest.schema import OTAReleaseKey
from ota_image_libs.v1.partition_image.schema import (
    ActionPerformer,
    BootFilesDescriptor,
    DeliveryMode,
    PartitionAction,
    PartitionDeltaDescriptor,
    PartitionEntry,
    PartitionImageBlobDescriptor,
    PartitionImageConfig,
    PartitionImageManifest,
    VendorPackageDescriptor,
)
from pydantic import BaseModel, Field, ValidationError, model_validator

from ota_image_builder._common import (
    check_if_valid_ota_image,
    exit_with_err_msg,
    human_readable_size,
)
from ota_image_builder.cmds._utils import validate_annotations
from ota_image_builder.cmds.add_image import _parse_specs

if TYPE_CHECKING:
    from argparse import ArgumentParser, Namespace, _SubParsersAction


logger = logging.getLogger(__name__)


# ------ the spec file: what the platform tooling hands over ------ #


class VeritySpec(BaseModel):
    root_hash: str = Field(pattern=r"^[0-9a-fA-F]{32,128}$")
    hash_offset: int = Field(ge=0)


class PartitionSpec(BaseModel):
    """One line of the spec: a partition role and what the update does with it."""

    name: str
    action: PartitionAction
    image: str | None = None
    """File name of the blob, relative to the spec file; only for `write`."""
    kind: Literal["partition", "boot-files"] | None = None
    """What the blob is; defaults to boot-files for the role named `boot`."""
    filesystem: str | None = None
    verity: VeritySpec | None = None
    delta: DeltaSpec | None = None
    """A binary delta that reconstructs `image` from an earlier build. The image is
    still described — the agent verifies the reconstruction against it."""
    store_image: bool = True
    """Whether the image's own bytes go into the payload. False with a delta ships the
    delta alone, which is the point of a delta campaign; True ships both, so that one
    image serves devices at any version."""

    @model_validator(mode="after")
    def _consistent(self):
        if self.action != PartitionAction.write and (
            self.image or self.kind or self.filesystem or self.verity or self.delta
        ):
            raise ValueError(f"partition {self.name!r}: {self.action} takes no image")
        if self.delta is not None and not self.image:
            raise ValueError(
                f"partition {self.name!r}: a delta needs the image it reconstructs, so "
                "that its digest, size and verity can be recorded"
            )
        if not self.store_image and self.delta is None:
            raise ValueError(
                f"partition {self.name!r}: store_image false needs a delta; nothing "
                "else could produce the image on the device"
            )
        if self.delta is not None and ("/" in self.delta.file or not self.delta.file):
            raise ValueError(
                f"partition {self.name!r}: the delta must be a file name next to the spec"
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
        if self.kind:
            return self.kind
        return "boot-files" if self.name == "boot" else "partition"


class DeltaSourceSpec(BaseModel):
    """The bytes the delta applies to, named by digest: on the device they are the
    committed slot's own partition, and a digest identifies them exactly."""

    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    size: int = Field(ge=1)


class DeltaSpec(BaseModel):
    file: str
    """File name of the delta blob, relative to the spec file."""
    algorithm: str = Field(min_length=1)
    """How to apply it, e.g. `zstd-patch-from`. The agent refuses what it cannot run."""
    source: DeltaSourceSpec


class VendorPackageSpec(BaseModel):
    file: str
    format: str | None = None


class PartitionPayloadSpec(BaseModel):
    """The JSON the platform tooling writes beside the blobs.

    ```json
    {
      "delivery": "direct",
      "version": "1.2.0",
      "partitions": [
        {"name": "rootfs", "action": "write", "image": "rootfs.img",
         "filesystem": "ext4", "verity": {"root_hash": "…", "hash_offset": 1468006400}},
        {"name": "boot", "action": "write", "image": "boot.tar"},
        {"name": "scratch", "action": "mkfs"},
        {"name": "identity", "action": "keep"},
        {"name": "optdata", "action": "keep"}
      ]
    }
    ```

    With `"delivery": "vendor-package"` the spec names `"package": {"file": "…",
    "format": "…"}` instead of images, and the written roles carry no image.

    A partition may ship a binary delta instead of (or beside) its image, which is what
    makes a campaign transfer the change rather than the whole partition:

    ```json
    {"name": "rootfs", "action": "write", "image": "rootfs.img",
     "filesystem": "ext4", "verity": {"root_hash": "…", "hash_offset": 1468006400},
     "store_image": false,
     "delta": {"file": "rootfs.delta.zst", "algorithm": "zstd-patch-from",
               "source": {"digest": "sha256:…", "size": 1479573504}}}
    ```

    The image file is still named: its digest, size and verity are recorded so that the
    agent can verify what the delta reconstructs. With `store_image: false` the image's
    bytes stay out of the payload and only the delta is shipped; with the default they
    both ship, so one image serves devices at any version.
    """

    delivery: DeliveryMode
    version: str = Field(min_length=1)
    partitions: list[PartitionSpec] = Field(min_length=1)
    package: VendorPackageSpec | None = None

    @model_validator(mode="after")
    def _delivery_consistent(self):
        names = [p.name for p in self.partitions]
        if len(set(names)) != len(names):
            raise ValueError(f"partition names must be unique: {names}")
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


class AddPartitionImageAnnotations(AliasEnabledModel):
    """Annotations the caller provides for a partition-based payload.

    The manifest's (release key, platform, hardware, architecture) and the config's
    (base image, description, OS); aliases are the annotation keys.
    """

    # fmt: off
    ota_release_key: OTAReleaseKey | None = Field(alias=OTA_RELEASE_KEY, default=None)
    pilot_auto_platform: str | None = Field(alias=PILOT_AUTO_PLATFORM, default=None)
    pilot_auto_platform_ecu_hardware: str | None = Field(alias=PLATFORM_ECU_HARDWARE_MODEL, default=None)
    pilot_auto_platform_ecu_hardware_series: str | None = Field(alias=PLATFORM_ECU_HARDWARE_SERIES, default=None)
    architecture: str = Field(alias=PLATFORM_ECU_ARCH)

    base_image: str = Field(alias=SYS_IMAGE_BASE_IMAGE)
    description: str | None = None
    created: str | None = None
    os: str | None = Field(alias=OS, default=None)
    os_version: str | None = Field(alias=OS_VERSION, default=None)
    # fmt: on

    def manifest_annotations(self, ecu_id: str) -> dict[str, Any]:
        """The annotations of the manifest for `ecu_id`, keyed by annotation key."""
        _dump = self.model_dump(by_alias=True, exclude_none=True)
        for _own in ("description", "created"):
            _dump.pop(_own, None)
        _dump[PLATFORM_ECU] = ecu_id
        return _dump


# ------ the command ------ #


def add_partition_image_cmd_args(
    sub_arg_parser: _SubParsersAction[ArgumentParser], *parent_parser: ArgumentParser
) -> None:
    parser = sub_arg_parser.add_parser(
        name="add-partition-image",
        help=(
            _help_txt := "Add a partition-based image payload (whole partition images "
            "or a vendor package, from a spec JSON) into the OTA image"
        ),
        description=_help_txt,
        parents=parent_parser,
    )
    parser.add_argument(
        "--annotations-file",
        help="A yaml file that contains annotations for this image payload.",
        required=True,
    )
    parser.add_argument(
        "--sys-config",
        action="append",
        help="The sys config of the target ECU, informational for a partition-based "
        "payload (its items are applied at image build). Can be used multiple times "
        "for a multi-spec payload. Schema: `<ecu_id>:[<path_to_syscfg_file>]`.",
        required=True,
    )
    parser.add_argument(
        "--release-key",
        choices=["dev", "prd"],
        help="The release variant of the payload. If not set, the "
        "`vnd.tier4.ota.release-key` annotation will be used instead.",
    )
    parser.add_argument(
        "--spec",
        help="The JSON spec written beside the blobs: delivery, version, and per "
        "partition role the action and the blob file (see the command's module doc).",
        required=True,
    )
    parser.add_argument(
        "image_root",
        help="The folder of the OTA image we will add the payload to.",
    )
    parser.set_defaults(handler=add_partition_image_cmd)


def _load_spec(spec_path: Path) -> PartitionPayloadSpec:
    if not spec_path.is_file():
        exit_with_err_msg(f"spec file {spec_path} does not exist.")
    try:
        return PartitionPayloadSpec.model_validate_json(spec_path.read_text())
    except ValidationError as e:
        exit_with_err_msg(f"invalid spec file {spec_path}: {e}")
    except Exception as e:
        exit_with_err_msg(f"failed to read spec file {spec_path}: {e!r}")


def _blob_path(spec_path: Path, name: str) -> Path:
    _f = spec_path.parent / name
    if not _f.is_file():
        exit_with_err_msg(f"blob {name} named by the spec is not next to it: {_f}")
    return _f


def _describe_without_storing(
    descriptor_cls: type, blob: Path, annotations: dict[str, Any] | None
):
    """The descriptor of a blob that stays out of the payload: its digest and size are
    read from the file, so that a delta-only payload still says exactly what the
    partition must end up holding."""
    digest = sha256()
    with open(blob, "rb") as f:
        while chunk := f.read(1024 * 1024):
            digest.update(chunk)
    return descriptor_cls.model_validate(
        {
            "digest": f"sha256:{digest.hexdigest()}",
            "size": blob.stat().st_size,
            "annotations": annotations,
        }
    )


def _add_blobs(
    spec: PartitionPayloadSpec, spec_path: Path, resource_dir: Path
) -> tuple[list[PartitionEntry], VendorPackageDescriptor | None]:
    """Copy every blob into the blob storage, as it is, and build the entries."""
    entries: list[PartitionEntry] = []
    for p in spec.partitions:
        if p.action != PartitionAction.write:
            entries.append(PartitionEntry(name=p.name, action=p.action))
            continue
        if spec.delivery == DeliveryMode.vendor_package:
            entries.append(
                PartitionEntry(
                    name=p.name,
                    action=p.action,
                    performed_by=ActionPerformer.package,
                )
            )
            continue

        assert p.image is not None
        blob = _blob_path(spec_path, p.image)
        annotations: dict[str, Any] = {}
        if p.blob_kind == "partition":
            if p.filesystem:
                annotations[PARTITION_IMAGE_FILESYSTEM] = p.filesystem
            if p.verity:
                annotations[PARTITION_IMAGE_VERITY_ROOT_HASH] = p.verity.root_hash
                annotations[PARTITION_IMAGE_VERITY_HASH_OFFSET] = p.verity.hash_offset
        descriptor_cls = (
            BootFilesDescriptor
            if p.blob_kind == "boot-files"
            else PartitionImageBlobDescriptor
        )
        if p.store_image:
            logger.info(
                f"Add {p.blob_kind} blob for {p.name!r} from {blob} "
                f"({human_readable_size(blob.stat().st_size)}) ..."
            )
            image = descriptor_cls.add_file_to_resource_dir(
                blob, resource_dir, annotations=annotations or None
            )
        else:
            # The payload ships only the delta, so the image is described without being
            # stored: the bytes it names are the ones the reconstruction must produce.
            logger.info(
                f"Describe {p.blob_kind} blob for {p.name!r} from {blob} "
                f"({human_readable_size(blob.stat().st_size)}) without storing it ..."
            )
            image = _describe_without_storing(descriptor_cls, blob, annotations or None)

        delta = None
        if p.delta is not None:
            delta_blob = _blob_path(spec_path, p.delta.file)
            logger.info(
                f"Add delta for {p.name!r} from {delta_blob} "
                f"({human_readable_size(delta_blob.stat().st_size)}), "
                f"{p.delta.algorithm} from {p.delta.source.digest[:19]}… ..."
            )
            delta = PartitionDeltaDescriptor.add_file_to_resource_dir(
                delta_blob,
                resource_dir,
                annotations={
                    PARTITION_IMAGE_DELTA_ALGORITHM: p.delta.algorithm,
                    PARTITION_IMAGE_DELTA_SOURCE_DIGEST: p.delta.source.digest,
                    PARTITION_IMAGE_DELTA_SOURCE_SIZE: p.delta.source.size,
                },
            )
        entries.append(
            PartitionEntry(
                name=p.name,
                action=p.action,
                image=image,
                delta=delta,
                image_in_storage=p.store_image,
            )
        )

    package = None
    if spec.package is not None:
        blob = _blob_path(spec_path, spec.package.file)
        logger.info(
            f"Add vendor package from {blob} ({human_readable_size(blob.stat().st_size)}) ..."
        )
        package = VendorPackageDescriptor.add_file_to_resource_dir(
            blob,
            resource_dir,
            annotations={PARTITION_IMAGE_VENDOR_PACKAGE_FORMAT: spec.package.format}
            if spec.package.format
            else None,
        )
    return entries, package


def _compose_config(
    *,
    spec: PartitionPayloadSpec,
    entries: list[PartitionEntry],
    package: VendorPackageDescriptor | None,
    sys_config_descriptor: SysConfig.Descriptor | None,
    annotations: AddPartitionImageAnnotations,
) -> PartitionImageConfig:
    payload_blobs = [e.image for e in entries if e.image is not None]
    if package is not None:
        payload_blobs.append(package)
    labels = {
        SYS_IMAGE_BASE_IMAGE: annotations.base_image,
        OTA_IMAGE_BLOBS_COUNT: len(payload_blobs),
        OTA_IMAGE_BLOBS_SIZE: sum(b.size for b in payload_blobs),
    }
    if annotations.os:
        labels[OS] = annotations.os
    if annotations.os_version:
        labels[OS_VERSION] = annotations.os_version
    return PartitionImageConfig(
        description=annotations.description,
        created=annotations.created
        or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        architecture=annotations.architecture,
        os=annotations.os,
        os_version=annotations.os_version,  # type: ignore[call-arg]
        image_version=spec.version,
        delivery=spec.delivery,
        partitions=entries,
        package=package,
        sys_config=sys_config_descriptor,
        labels=PartitionImageConfig.Annotations.model_validate(labels),
    )


def add_partition_image_cmd(args: Namespace) -> None:
    logger.debug(f"calling {add_partition_image_cmd.__name__} with {args}")
    image_root = Path(args.image_root)
    if not check_if_valid_ota_image(image_root):
        exit_with_err_msg(f"{image_root} is not a valid OTA image root directory.")

    index_helper = ImageIndexHelper(image_root=image_root)
    image_index = index_helper.image_index
    if image_index.image_finalized or image_index.image_signed:
        exit_with_err_msg("Modifying an already finalized image is NOT allowed, abort!")

    spec_path = Path(args.spec)
    spec = _load_spec(spec_path)
    logger.info(
        f"Will add a partition-based payload ({spec.delivery}, version {spec.version}) "
        f"from {spec_path} into OTA image at {image_root} ..."
    )

    annotations = AddPartitionImageAnnotations.model_validate(
        validate_annotations(Path(args.annotations_file), AddPartitionImageAnnotations)
    )
    if args.release_key:
        annotations.ota_release_key = OTAReleaseKey(args.release_key)
        logger.info(
            f"Release key specified from CLI args: {annotations.ota_release_key}"
        )
    elif annotations.ota_release_key is None:
        exit_with_err_msg(
            f"No release key: pass --release-key or set {OTA_RELEASE_KEY} "
            "in the annotations file ('dev' or 'prd')."
        )
    sys_config_files = _parse_specs(args.sys_config)

    resource_dir = index_helper.image_resource_dir
    entries, package = _add_blobs(spec, spec_path, resource_dir)

    for ecu_id, sys_config in sys_config_files.items():
        logger.info(f"Add manifest for spec: {ecu_id=}, {sys_config=}")
        sys_config_descriptor = None
        if sys_config is not None:
            sys_config_descriptor = SysConfig.Descriptor.add_file_to_resource_dir(
                sys_config, resource_dir=resource_dir
            )
        config = _compose_config(
            spec=spec,
            entries=entries,
            package=package,
            sys_config_descriptor=sys_config_descriptor,
            annotations=annotations,
        )
        config_descriptor = (
            PartitionImageConfig.Descriptor.export_metafile_to_resource_dir(
                config, resource_dir
            )
        )
        manifest_annotations = annotations.manifest_annotations(ecu_id)
        manifest = PartitionImageManifest(
            config=config_descriptor,
            layers=config.payload_descriptors,
            annotations=PartitionImageManifest.Annotations.model_validate(
                manifest_annotations
            ),
        )
        manifest_descriptor = (
            PartitionImageManifest.Descriptor.export_metafile_to_resource_dir(
                manifest, resource_dir, annotations=manifest_annotations
            )
        )
        image_index.add_image(manifest_descriptor)

    logger.info("Sync index.json on finishing up adding the payload.")
    index_helper.sync_index()
    print(
        f"Added partition-based payload version {spec.version} "
        f"({spec.delivery}) for {', '.join(sys_config_files)} into {image_root}."
    )
