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

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ota_image_libs.common import AliasEnabledModel
from ota_image_libs.v1.annotation_keys import (
    BUILD_TOOL_VERSION,
    PILOT_AUTO_PLATFORM,
    PILOT_AUTO_PROJECT_BRANCH,
    PILOT_AUTO_PROJECT_COMMIT,
    PILOT_AUTO_PROJECT_SOURCE,
    PILOT_AUTO_PROJECT_VERSION,
    WEB_AUTO_CATALOG,
    WEB_AUTO_CATALOG_ID,
    WEB_AUTO_ENV,
    WEB_AUTO_PROJECT,
    WEB_AUTO_PROJECT_ID,
)
from pydantic import Field

from ota_image_builder._common import exit_with_err_msg
from ota_image_builder._version import version
from ota_image_builder.cmds._utils import validate_annotations
from ota_image_builder.v1._image_index import init_ota_image

if TYPE_CHECKING:
    from argparse import ArgumentParser, Namespace, _SubParsersAction


logger = logging.getLogger(__name__)


class InitCMDAnnotations(AliasEnabledModel):
    """Required annotations for initializing an empty OTA image."""

    # fmt: off
    pilot_auto_platform: str | None = Field(alias=PILOT_AUTO_PLATFORM, default=None)
    pilot_auto_source_repo: str | None = Field(alias=PILOT_AUTO_PROJECT_SOURCE, default=None)
    pilot_auto_version: str | None = Field(alias=PILOT_AUTO_PROJECT_VERSION, default=None)
    pilot_auto_release_commit: str | None = Field(alias=PILOT_AUTO_PROJECT_COMMIT, default=None)
    pilot_auto_release_branch: str | None = Field(alias=PILOT_AUTO_PROJECT_BRANCH, default=None)

    web_auto_project: str | None = Field(alias=WEB_AUTO_PROJECT, default=None)
    web_auto_project_id: str | None = Field(alias=WEB_AUTO_PROJECT_ID, default=None)
    web_auto_catalog: str | None = Field(alias=WEB_AUTO_CATALOG, default=None)
    web_auto_catalog_id: str | None = Field(alias=WEB_AUTO_CATALOG_ID, default=None)
    web_auto_env: str | None = Field(alias=WEB_AUTO_ENV, default=None)
    # fmt: on


def init_cmd_args(
    sub_arg_parser: _SubParsersAction[ArgumentParser], *parent_parser: ArgumentParser
) -> None:
    init_cmd_arg_parser = sub_arg_parser.add_parser(
        name="init",
        help=(_help_txt := "Init an empty OTA image"),
        description=_help_txt,
        parents=parent_parser,
    )
    init_cmd_arg_parser.add_argument(
        "--annotations-file",
        help="An yaml file that contains annotations for initialized index.json.",
        required=True,
    )
    init_cmd_arg_parser.add_argument(
        "image_root",
        help="The folder to hold a new empty OTA image. It should be an empty folder.",
    )
    init_cmd_arg_parser.set_defaults(handler=init_cmd)


def init_cmd(args: Namespace) -> None:
    logger.debug(f"calling {init_cmd.__name__} with {args}")
    image_root = Path(args.image_root)
    annotations_file = Path(args.annotations_file)

    if image_root.is_dir():
        try:
            image_root.rmdir()
        except OSError as e:
            logger.debug(f"failed to cleanup image_root: {e}", exc_info=e)
            if e.errno == 39:
                exit_with_err_msg(f"{image_root} is not empty, abort!")
            else:
                exit_with_err_msg(f"failed to prepare {image_root}: {e}")

    annotations = validate_annotations(annotations_file, InitCMDAnnotations)
    logger.info(f"ota-image-builder version: {version}")
    annotations[BUILD_TOOL_VERSION] = version

    logger.info(f"Initialize empty OTA image at {image_root}")
    try:
        init_ota_image(image_root, annotations)
    except Exception as e:
        logger.debug(f"failed to init OTA image: {e}", exc_info=e)
        exit_with_err_msg(f"failed during image initializing: {e}")
    print(f"An empty OTA image is initialized at {image_root}.")
