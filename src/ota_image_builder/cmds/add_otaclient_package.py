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
"""Add otaclient release package into OTA image."""

from __future__ import annotations

import logging
from argparse import Namespace
from pathlib import Path
from typing import TYPE_CHECKING

from ota_image_libs.v1.consts import RESOURCE_DIR
from ota_image_libs.v1.image_index.utils import ImageIndexHelper
from ota_image_libs.v1.otaclient_package.utils import add_otaclient_package

from ota_image_builder._common import check_if_valid_ota_image, exit_with_err_msg

if TYPE_CHECKING:
    from argparse import ArgumentParser, _SubParsersAction

logger = logging.getLogger(__name__)


def add_otaclient_package_cmd_args(
    sub_arg_parser: _SubParsersAction[ArgumentParser], *parent_parser: ArgumentParser
) -> None:
    add_otaclient_package_arg_parser = sub_arg_parser.add_parser(
        name="add-otaclient-package",
        help=(
            _help_txt := "Add an otaclient release package as artifact into OTA image"
        ),
        description=_help_txt,
        parents=parent_parser,
    )
    add_otaclient_package_arg_parser.add_argument(
        "--release-dir",
        help="The location of the otaclient release package to be imported.",
        required=True,
    )
    add_otaclient_package_arg_parser.add_argument(
        "image_root",
        help="The folder of the OTA image we will add new system rootfs image to.",
    )
    add_otaclient_package_arg_parser.set_defaults(handler=add_otaclient_package_cmd)


def add_otaclient_package_cmd(args: Namespace) -> None:
    logger.debug(f"calling {add_otaclient_package_cmd.__name__} with {args}")
    image_root = Path(args.image_root)
    if not check_if_valid_ota_image(image_root):
        exit_with_err_msg(f"{image_root} is not a valid OTA image root directory.")

    release_dir = Path(args.release_dir)
    if not release_dir.is_dir():
        exit_with_err_msg(f"{release_dir} doesn't exist.")

    logger.info(
        f"Will try to add otaclient release package from {release_dir} to OTA image at {image_root} ..."
    )

    index_helper = ImageIndexHelper(image_root=image_root)
    image_index = index_helper.image_index
    if image_index.image_finalized or image_index.image_signed:
        exit_with_err_msg("modifying an already finalized image is NOT allowed, abort!")

    image_index.add_otaclient_package(
        add_otaclient_package(
            release_dir,
            resource_dir=image_root / RESOURCE_DIR,
        )
    )
    index_helper.sync_index()
