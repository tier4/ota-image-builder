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
"""`add-parameter-set`: an ECU's parameter set, the files of the slots it fills with
how each takes effect, into a parameter image (see `ota_image_builder.v1._parameter_set`).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ota_image_libs.v1.image_index.utils import ImageIndexHelper
from ota_image_libs.v1.image_manifest.schema import ImageIdentifier, OTAReleaseKey
from ota_image_libs.v1.parameter_set.slots import ParameterSlot

from ota_image_builder._common import check_if_valid_ota_image, exit_with_err_msg
from ota_image_builder.v1._parameter_set import (
    ParameterSetError,
    add_parameter_set,
    load_parameter_slots,
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
            _help_txt := "Add a parameter set for one ECU into a parameter image: the "
            "files of the slots it fills, each with how a change to it takes effect. "
            "The set names no base: which base it suits is decided by evaluation."
        ),
        description=_help_txt,
        parents=parent_parser,
    )
    parser.add_argument(
        "--ecu-id",
        help="The ECU the set is for.",
        required=True,
    )
    parser.add_argument(
        "--release-key",
        choices=["dev", "prd"],
        help="The release variant the set is for.",
        required=True,
    )
    parser.add_argument(
        "--slots",
        metavar="FILE",
        help="A YAML or JSON list of the slots the set fills, each `{name, path, "
        "apply}`: the directory it replaces, and the executable that makes a change "
        "take effect (the set's own inside its slots, the base's otherwise; or "
        "`reboot`). Required unless --empty.",
    )
    parser.add_argument(
        "--rootfs",
        help="The tree that holds the slots' directories at their paths "
        "(<rootfs>/<a slot's path>). Required unless --empty.",
    )
    parser.add_argument(
        "--empty",
        action="store_true",
        help="An empty set: takes away the set applied before, so that the ECU's "
        "slots go back to the base's defaults. Takes no --slots and no --rootfs.",
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

    if args.empty:
        if args.slots or args.rootfs:
            exit_with_err_msg(
                "--empty takes no --slots and no --rootfs: an empty set fills no slot."
            )
    else:
        if not args.slots:
            exit_with_err_msg("--slots is required for a set that fills a slot.")
        if not args.rootfs:
            exit_with_err_msg("--rootfs is required for a set that fills a slot.")
    rootfs = Path(args.rootfs).resolve() if args.rootfs else None
    if rootfs is not None and not rootfs.is_dir():
        exit_with_err_msg(f"{args.rootfs} is not a directory.")

    image_id = ImageIdentifier(args.ecu_id, OTAReleaseKey(args.release_key))
    try:
        slots: list[ParameterSlot] = (
            load_parameter_slots(Path(args.slots)) if args.slots else []
        )
        logger.info(
            f"Will add parameter set {args.name} {args.version} for "
            f"{args.ecu_id} ({args.release_key}), "
            f"filling {[_s.name for _s in slots] or 'no slot'} ..."
        )
        add_parameter_set(
            index=image_index,
            resource_dir=index_helper.image_resource_dir,
            image_id=image_id,
            slots=slots,
            rootfs=rootfs,
            name=args.name,
            version=args.version,
        )
    except ParameterSetError as e:
        logger.debug(f"failed to add the parameter set: {e!r}", exc_info=e)
        exit_with_err_msg(str(e))

    index_helper.sync_index()
    logger.info(f"Added parameter set {args.name} {args.version} into {image_root}.")
