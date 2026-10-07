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
"""`add-otaclient-package`: an otaclient release directory, as the OTAClient release
package and as update agent bundles."""

from __future__ import annotations

import logging
from argparse import Namespace
from pathlib import Path
from typing import TYPE_CHECKING

from ota_image_libs.v1.otaclient_package.utils import add_otaclient_package
from ota_image_libs.v1.update_agent_package.utils import (
    bundles_from_otaclient_release,
)

from ota_image_builder._common import exit_with_err_msg
from ota_image_builder.cmds.add_update_agent_package import (
    add_bundles,
    open_image_for_agent_package,
)

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
    release_dir = Path(args.release_dir)
    if not release_dir.is_dir():
        exit_with_err_msg(f"{release_dir} doesn't exist.")

    index_helper = open_image_for_agent_package(Path(args.image_root))
    image_index = index_helper.image_index
    if image_index.find_otaclient_package():
        exit_with_err_msg(
            "OTAClient release package has already been added into the OTA image, abort!"
        )
    bundles = bundles_from_otaclient_release(release_dir)
    if not bundles:
        exit_with_err_msg(f"{release_dir} holds no squashfs release to add.")

    # otaclient up to v3.14 finds its release through the OTAClient release package
    #   entry, later versions through the update agent one. Both are written, sharing
    #   the blobs, until no fleet runs the former.
    logger.info(f"Add otaclient release package from {release_dir} ...")
    image_index.add_otaclient_package(
        add_otaclient_package(release_dir, resource_dir=index_helper.image_resource_dir)
    )
    add_bundles(index_helper, bundles)
    index_helper.sync_index()
