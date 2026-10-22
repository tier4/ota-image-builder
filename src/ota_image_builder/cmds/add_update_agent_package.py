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
"""`add-update-agent-package`: the agent that applies the image, shipped in it as
one entry of bundles (see ota_image_libs.v1.update_agent_package)."""

from __future__ import annotations

import logging
from argparse import Namespace
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from ota_image_libs.v1.image_index.utils import ImageIndexHelper
from ota_image_libs.v1.update_agent_package.utils import add_update_agent_package

from ota_image_builder._common import check_if_valid_ota_image, exit_with_err_msg

if TYPE_CHECKING:
    from argparse import ArgumentParser, _SubParsersAction

logger = logging.getLogger(__name__)


def add_update_agent_package_cmd_args(
    sub_arg_parser: _SubParsersAction[ArgumentParser], *parent_parser: ArgumentParser
) -> None:
    parser = sub_arg_parser.add_parser(
        name="add-update-agent-package",
        help=(
            _help := "Add an update agent release package as artifact into OTA image"
        ),
        description=_help,
        parents=parent_parser,
    )
    parser.add_argument(
        "--bundle",
        action="append",
        required=True,
        metavar="FILE:TYPE:VERSION[:ARCH]",
        help=(
            "One agent bundle. TYPE is what the image calls it and is opaque: a "
            "consumer takes only a type it implements. Repeat for several."
        ),
    )
    parser.add_argument("image_root", help="The folder of the OTA image.")
    parser.set_defaults(handler=add_update_agent_package_cmd)


def _parse_bundle(spec: str) -> tuple[Path, str, str, str | None]:
    """FILE:TYPE:VERSION[:ARCH], split from the right; a FILE with a colon in it still
    parses when it names an existing file."""
    for n_fields in (3, 2):
        parts = spec.rsplit(":", n_fields)
        if len(parts) != n_fields + 1:
            continue
        _file = Path(parts[0])
        if _file.is_file() or n_fields == 2:
            break
    else:
        exit_with_err_msg(f"--bundle wants FILE:TYPE:VERSION[:ARCH], got {spec!r}")
    if not _file.is_file():
        exit_with_err_msg(f"no such bundle file: {_file}")
    if not parts[1] or not parts[2]:
        exit_with_err_msg(f"--bundle needs a type and a version, got {spec!r}")
    return _file, parts[1], parts[2], parts[3] if len(parts) == 4 else None


def open_image_for_agent_package(image_root: Path) -> ImageIndexHelper:
    """The image an update agent release package is added to: valid, not finalized,
    and holding none yet."""
    if not check_if_valid_ota_image(image_root):
        exit_with_err_msg(f"{image_root} is not a valid OTA image root directory.")
    index_helper = ImageIndexHelper(image_root=image_root)
    image_index = index_helper.image_index
    if image_index.image_finalized or image_index.image_signed:
        exit_with_err_msg("Modifying an already finalized image is NOT allowed, abort!")
    if image_index.find_update_agent_package():
        exit_with_err_msg(
            "An update agent release package is already in this OTA image, abort! "
            "One entry carries every agent the image ships; add them together."
        )
    return index_helper


def add_bundles(
    index_helper: ImageIndexHelper,
    bundles: Sequence[tuple[Path, str, str, str | None]],
) -> None:
    """Add (file, type, version, architecture) bundles as the image's update agent
    release package. The caller syncs the index."""
    for _file, _type, _version, _arch in bundles:
        logger.info(
            f"Add update agent {_type} {_version} ({_arch or 'any'}) from {_file} ..."
        )
    index_helper.image_index.add_update_agent_package(
        add_update_agent_package(bundles, resource_dir=index_helper.image_resource_dir)
    )


def add_update_agent_package_cmd(args: Namespace) -> None:
    logger.debug(f"calling {add_update_agent_package_cmd.__name__} with {args}")
    index_helper = open_image_for_agent_package(Path(args.image_root))
    add_bundles(index_helper, [_parse_bundle(_b) for _b in args.bundle])
    index_helper.sync_index()
