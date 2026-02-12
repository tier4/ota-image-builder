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

from __future__ import annotations

import argparse
import functools
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from ota_image_libs import version as ota_image_libs_version

from ota_image_builder._common import exit_with_err_msg
from ota_image_builder.cmds import (
    add_image_cmd_args,
    add_otaclient_package_cmd_args,
    add_otaclient_package_compat_cmd_args,
    build_annotation_cmd_args,
    build_exclude_cfg_cmd_args,
    finalize_cmd_args,
    init_cmd_args,
    pack_artifact_cmd_args,
    prepare_sysimg_cmd_args,
    sign_cmd_args,
)

from ._version import version

if TYPE_CHECKING:
    from argparse import ArgumentParser, _SubParsersAction


logger = logging.getLogger(__name__)


def main():
    logger.info(f"OTA image builder, version {version}")
    arg_parser = argparse.ArgumentParser(
        description="OTA Image Builder CLI for OTA Image version 1",
    )

    def missing_subcmd(_):
        print("Please specify subcommand.")
        print(arg_parser.format_help())

    arg_parser.set_defaults(handler=missing_subcmd)

    # ------ top-level parser ------ #
    arg_parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        help="Enable debug logging for this script",
    )

    sub_arg_parser: _SubParsersAction[ArgumentParser] = arg_parser.add_subparsers(
        title="available sub-commands",
        parser_class=functools.partial(
            argparse.ArgumentParser,
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        ),  # type: ignore
    )

    # ------ sub commands registering ------ #

    version_cmd = sub_arg_parser.add_parser(
        name="version",
        help="Print the version string of this OTA Image Builder.",
    )
    version_cmd.set_defaults(handler=lambda _: print(f"v{version}"))

    version_info_cmd = sub_arg_parser.add_parser(
        name="version-info",
        help="Print the full version info of this OTA Image Builder.",
    )
    version_info_cmd.set_defaults(
        handler=lambda _: print(
            f"ota-image-builder v{version} (Built with ota-image-lib v{ota_image_libs_version})"
        )
    )

    prepare_sysimg_cmd_args(sub_arg_parser)
    init_cmd_args(sub_arg_parser)
    build_exclude_cfg_cmd_args(sub_arg_parser)
    build_annotation_cmd_args(sub_arg_parser)
    add_image_cmd_args(sub_arg_parser)
    add_otaclient_package_cmd_args(sub_arg_parser)
    add_otaclient_package_compat_cmd_args(sub_arg_parser)
    finalize_cmd_args(sub_arg_parser)
    sign_cmd_args(sub_arg_parser)
    pack_artifact_cmd_args(sub_arg_parser)

    # ------ top-level args parsing ----- #
    args = arg_parser.parse_args()
    if args.debug:
        _root_logger = logging.getLogger("ota_image_builder")
        _root_logger.setLevel(logging.DEBUG)
        _libs_logger = logging.getLogger("ota_image_libs")
        _libs_logger.setLevel(logging.DEBUG)
        _root_logger.debug("set to debug logging")

    # ------ execute command ------ #
    handler: Callable = args.handler
    try:
        handler(args)
    except Exception as e:
        logger.exception(f"failed during processing: {e!r}")
        exit_with_err_msg("Exit on failure occurs.")