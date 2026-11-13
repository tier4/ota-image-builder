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
"""`add-parameter-set`: an ECU's parameter set, made for one base payload, into a
parameter image (see `ota_image_builder.v1._parameter_set`)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ota_image_libs.v1.image_index.utils import ImageIndexHelper
from ota_image_libs.v1.image_manifest.schema import ImageIdentifier, OTAReleaseKey

from ota_image_builder._common import check_if_valid_ota_image, exit_with_err_msg
from ota_image_builder.v1._parameter_set import (
    ParameterSetError,
    add_parameter_set,
    load_base_payload,
    select_slots,
)

if TYPE_CHECKING:
    from argparse import ArgumentParser, Namespace, _SubParsersAction


logger = logging.getLogger(__name__)


def add_parameter_set_cmd_args(
    sub_arg_parser: _SubParsersAction[ArgumentParser], *parent_parser: ArgumentParser
) -> None:
    parser = sub_arg_parser.add_parser(
        name="add-parameter-set",
        help=(
            _help_txt := "Add an ECU's parameter set, the files of the parameter slots "
            "of one base payload, into a parameter image"
        ),
        description=_help_txt,
        parents=parent_parser,
    )
    parser.add_argument(
        "--base-image",
        help="The base OTA image (a folder or an artifact ZIP) the set is made for: "
        "the set is bound to the ECU's payload in it and fills the slots it declares.",
        required=True,
    )
    parser.add_argument(
        "--ecu-id",
        help="The ECU the set is for.",
        required=True,
    )
    parser.add_argument(
        "--release-key",
        choices=["dev", "prd"],
        help="The release variant of the base payload the set is made for.",
        required=True,
    )
    parser.add_argument(
        "--rootfs",
        help="The tree that holds the slots' directories at their paths, e.g. "
        "<rootfs>/opt/autoware/vehicle/<vehicle>/individual_params. "
        "Not used with --empty.",
    )
    parser.add_argument(
        "--slot",
        action="append",
        default=[],
        help="A slot the set fills, by name. Can be used multiple times. "
        "Default: every slot the base declares.",
    )
    parser.add_argument(
        "--empty",
        action="store_true",
        help="An empty set: every slot keeps the base's defaults. For an ECU a "
        "release names but whose parameters it does not set.",
    )
    parser.add_argument(
        "--name",
        help="The set's name.",
        required=True,
    )
    parser.add_argument(
        "--version",
        help="The set's version, reported once it is applied.",
        required=True,
    )
    parser.add_argument(
        "image_root",
        help="The folder of the parameter image to add the set to.",
    )
    parser.set_defaults(handler=add_parameter_set_cmd)


def add_parameter_set_cmd(args: Namespace) -> None:
    logger.debug(f"calling {add_parameter_set_cmd.__name__} with {args}")
    image_root = Path(args.image_root)
    if not check_if_valid_ota_image(image_root):
        exit_with_err_msg(f"{image_root} is not a valid OTA image root directory.")

    index_helper = ImageIndexHelper(image_root=image_root)
    image_index = index_helper.image_index
    if image_index.image_finalized or image_index.image_signed:
        exit_with_err_msg("Modifying an already finalized image is NOT allowed, abort!")
    if image_index.image_resource_table is not None:
        exit_with_err_msg(
            "The image carries file-based payloads: a parameter set goes into a "
            "parameter image of its own."
        )

    if args.empty and args.rootfs:
        exit_with_err_msg("--empty takes no --rootfs: an empty set carries no files.")
    if not args.empty and not args.rootfs:
        exit_with_err_msg("--rootfs is required for a set that fills a slot.")
    rootfs = Path(args.rootfs).resolve() if args.rootfs else None
    if rootfs is not None and not rootfs.is_dir():
        exit_with_err_msg(f"{args.rootfs} is not a directory.")

    image_id = ImageIdentifier(args.ecu_id, OTAReleaseKey(args.release_key))
    base_image = Path(args.base_image)
    if not base_image.exists():
        exit_with_err_msg(f"base image {base_image} not found.")
    try:
        base = load_base_payload(base_image, image_id)
        slots = select_slots(base, args.slot, empty=args.empty)
        logger.info(
            f"Will add parameter set {args.name} {args.version} for {image_id}, "
            f"filling {[_s.name for _s in slots] or 'no slot'}, made for base payload "
            f"{base.subject.digest} of {base_image} ..."
        )
        add_parameter_set(
            index=image_index,
            resource_dir=index_helper.image_resource_dir,
            image_id=image_id,
            base=base,
            slots=slots,
            rootfs=rootfs,
            name=args.name,
            version=args.version,
        )
    except ParameterSetError as e:
        exit_with_err_msg(str(e))

    index_helper.sync_index()
    logger.info(f"Added parameter set {args.name} {args.version} into {image_root}.")
