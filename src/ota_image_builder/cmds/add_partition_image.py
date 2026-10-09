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
"""`add-partition-image`: a partition-based payload from the blobs the platform
tooling built and its spec JSON (see `ota_image_builder.v1._partition_image`)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ota_image_libs.v1.annotation_keys import OTA_RELEASE_KEY, PLATFORM_ECU
from ota_image_libs.v1.image_config.sys_config import SysConfig
from ota_image_libs.v1.image_index.utils import ImageIndexHelper
from ota_image_libs.v1.image_manifest.schema import OTAReleaseKey
from ota_image_libs.v1.partition_image.schema import (
    PartitionImageConfig,
    PartitionImageManifest,
)
from pydantic import ValidationError

from ota_image_builder._common import check_if_valid_ota_image, exit_with_err_msg
from ota_image_builder.cmds._utils import parse_sys_config_specs, validate_annotations
from ota_image_builder.v1._partition_image import (
    DEFAULT_ZSTD_LEVEL,
    AddPartitionImageAnnotations,
    PartitionPayloadSpec,
    add_payload_blobs,
    compose_partition_image_config,
    compose_partition_image_manifest,
    data_only,
    delta_sources,
)

if TYPE_CHECKING:
    from argparse import ArgumentParser, Namespace, _SubParsersAction


logger = logging.getLogger(__name__)


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
        "partition role the action and the blob file (see the README).",
        required=True,
    )
    parser.add_argument(
        "--delta-from",
        action="append",
        default=[],
        metavar="NAME=IMAGE",
        help="Build a block diff for partition role or data image NAME against IMAGE, "
        "the previous build's image, and ship it instead of the image: the payload then "
        "applies only to devices holding the bytes the delta was built from. "
        "`data:NAME=IMAGE` names a data image that shares a partition role's name. "
        "Can be used multiple times; overrides the spec's own `delta`.",
    )
    parser.add_argument(
        "--data-only",
        action="store_true",
        help="Ship only the spec's data images: every partition becomes `keep` and no "
        "partition blob is stored, so the same spec that builds a rootfs release also "
        "builds the payload that updates its data images alone. The payload's version "
        "is still the spec's `version`.",
    )
    parser.add_argument(
        "--no-compress",
        action="store_true",
        help="Store partition images, data images, firmware and the vendor package as "
        "they are instead of zstd-compressed.",
    )
    parser.add_argument(
        "--zstd-level",
        type=int,
        choices=range(1, 23),
        metavar="1..22",
        default=DEFAULT_ZSTD_LEVEL,
        help=f"The zstd level blobs and delta literals are compressed with "
        f"(default: {DEFAULT_ZSTD_LEVEL}).",
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
    if args.data_only:
        spec = data_only(spec)
    logger.info(
        f"Will add a partition-based payload ({spec.delivery}, version {spec.version}"
        f"{', data images only' if args.data_only else ''}) "
        f"from {spec_path} into OTA image at {image_root} ..."
    )

    annotations = validate_annotations(
        Path(args.annotations_file), AddPartitionImageAnnotations
    )
    if args.release_key:
        annotations["ota_release_key"] = OTAReleaseKey(args.release_key)
        logger.info(f"Release key specified from CLI args: {args.release_key}")
    elif annotations.get("ota_release_key") is None:
        exit_with_err_msg(
            f"No release key: pass --release-key or set {OTA_RELEASE_KEY} "
            "in the annotations file ('dev' or 'prd')."
        )
    sys_config_files = parse_sys_config_specs(args.sys_config)
    sources = delta_sources(spec, spec_path, args.delta_from)

    resource_dir = index_helper.image_resource_dir
    try:
        blobs = add_payload_blobs(
            spec,
            spec_path,
            resource_dir,
            compress=not args.no_compress,
            zstd_level=args.zstd_level,
            delta_sources=sources,
        )
    except ValidationError as e:
        exit_with_err_msg(f"the spec names a payload the schema refuses: {e}")

    # support for multi-spec OTA image: one manifest per ECU, backed by the same blobs
    for ecu_id, sys_config in sys_config_files.items():
        logger.info(f"Add manifest for spec: {ecu_id=}, {sys_config=}")
        _annotations = {**annotations, PLATFORM_ECU: ecu_id}
        sys_config_descriptor = None
        if sys_config is not None:
            sys_config_descriptor = SysConfig.Descriptor.add_file_to_resource_dir(
                sys_config, resource_dir=resource_dir
            )
        config = compose_partition_image_config(
            spec=spec,
            blobs=blobs,
            sys_config_descriptor=sys_config_descriptor,
            annotations=_annotations,
        )
        config_descriptor = (
            PartitionImageConfig.Descriptor.export_metafile_to_resource_dir(
                config, resource_dir
            )
        )
        manifest = compose_partition_image_manifest(
            config_descriptor=config_descriptor,
            layers=config.payload_descriptors,
            annotations=_annotations,
        )
        image_index.add_image(
            PartitionImageManifest.Descriptor.export_metafile_to_resource_dir(
                manifest, resource_dir, annotations=_annotations
            )
        )

    index_helper.sync_index()
    logger.info(
        f"Added partition-based payload version {spec.version} ({spec.delivery}) "
        f"for {', '.join(sys_config_files)} into {image_root}."
    )
