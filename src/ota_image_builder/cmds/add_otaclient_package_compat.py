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
"""For legacy OTA image backward compatibility.

With this compatibility enabled, old OTAClient(>=3.10) that supports dynamic otaclient update
    while not supporting new OTA image spec can still do otaclient update, further supporting
    new OTA image spec with the dynamically updated otaclient.
"""

from __future__ import annotations

import logging
import shutil
from argparse import Namespace
from pathlib import Path
from typing import TYPE_CHECKING

from ota_image_libs.v1.image_index.utils import ImageIndexHelper

from ota_image_builder._common import check_if_valid_ota_image, exit_with_err_msg

if TYPE_CHECKING:
    from argparse import ArgumentParser, _SubParsersAction

logger = logging.getLogger(__name__)


def add_otaclient_package_compat_cmd_args(
    sub_arg_parser: _SubParsersAction[ArgumentParser], *parent_parser: ArgumentParser
) -> None:
    add_otaclient_package_arg_parser = sub_arg_parser.add_parser(
        name="add-otaclient-package-legacy-compat",
        help="Add an otaclient release package into OTA image, but following legacy OTA image spec.",
        description="With this compatibility, otaclient(>=3.10) but doesn't support new OTA image spec can still "
        "use new OTA image to update itself to newer version that support new OTA image spec.",
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
    add_otaclient_package_arg_parser.set_defaults(
        handler=add_otaclient_package_compat_cmd
    )


OTACLIENT_RELEASE_DIR_LEGACY = "data/opt/ota/otaclient_release"


def add_otaclient_package_compat_cmd(args: Namespace) -> None:
    logger.debug(f"calling {add_otaclient_package_compat_cmd.__name__} with {args}")
    image_root = Path(args.image_root)
    if not check_if_valid_ota_image(image_root):
        exit_with_err_msg(f"{image_root} is not a valid OTA image root directory.")

    release_dir = Path(args.release_dir)
    if not release_dir.is_dir():
        exit_with_err_msg(f"{release_dir} doesn't exist.")

    legacy_target = image_root / OTACLIENT_RELEASE_DIR_LEGACY
    if legacy_target.is_dir():
        exit_with_err_msg(
            "OTAClient release package for legacy compatibility has already being added, abort!"
        )

    index_helper = ImageIndexHelper(image_root=image_root)
    image_index = index_helper.image_index
    if image_index.image_finalized or image_index.image_signed:
        exit_with_err_msg("Modifying an already finalized image is NOT allowed, abort!")

    logger.info(
        "Will try to add otaclient release package "
        f"from {release_dir} to OTA image at {image_root} following legacy OTA image spec ..."
    )
    shutil.copytree(release_dir, legacy_target)
