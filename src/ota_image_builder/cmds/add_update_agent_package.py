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
import tempfile
from argparse import Namespace
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from ota_image_libs.v1.image_index.utils import ImageIndexHelper
from ota_image_libs.v1.media_types import UPDATE_AGENT_TYPE_PARTITION_AGENT
from ota_image_libs.v1.update_agent_package.utils import add_update_agent_package

from ota_image_builder._common import check_if_valid_ota_image, exit_with_err_msg
from ota_image_builder.v1 import _partition_blobs as blobs

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
        default=[],
        metavar="FILE:TYPE:VERSION[:ARCH]",
        help=(
            "One agent bundle. TYPE is what the image calls it and is opaque: a "
            "consumer takes only a type it implements. Repeat for several."
        ),
    )
    parser.add_argument(
        "--rootfs-dir",
        action="append",
        default=[],
        metavar="DIR",
        help=(
            "A partition-based payload's rootfs tree: the partition agent the platform "
            f"installer put in it (/{blobs.PARTITION_AGENT_BUNDLE}) is shipped as "
            f"{UPDATE_AGENT_TYPE_PARTITION_AGENT!r}, its version from "
            f"{blobs.PARTITION_AGENT_VERSION_FILE} and its architecture from its entry "
            "point. A tree without one adds nothing; trees with nothing between them "
            "leave the image as it is. Repeat for several."
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


def _bundles_from_trees(
    trees: Sequence[str], out_dir: Path
) -> list[tuple[Path, str, str, str | None]]:
    """The partition agents the trees carry, one per architecture: two trees of one
    architecture shipping different versions cannot both be the image's agent."""
    by_arch: dict[str, tuple[Path, str, str, str | None]] = {}
    for n, tree in enumerate(trees):
        try:
            found = blobs.agent_bundle_from_tree(Path(tree), out_dir / str(n))
        except blobs.BlobBuildError as e:
            exit_with_err_msg(str(e))
        if found is None:
            logger.info(f"{tree} carries no partition agent bundle")
            continue
        _file, _version, _arch = found
        seen = by_arch.get(_arch)
        if seen is not None and seen[2] != _version:
            exit_with_err_msg(
                f"the trees carry two {_arch} agents, {seen[2]} and {_version}: an image "
                "ships one per architecture"
            )
        by_arch.setdefault(
            _arch, (_file, UPDATE_AGENT_TYPE_PARTITION_AGENT, _version, _arch)
        )
    return list(by_arch.values())


def add_update_agent_package_cmd(args: Namespace) -> None:
    logger.debug(f"calling {add_update_agent_package_cmd.__name__} with {args}")
    if not args.bundle and not args.rootfs_dir:
        exit_with_err_msg(
            "pass --bundle, or --rootfs-dir to take the agent from a tree"
        )
    index_helper = open_image_for_agent_package(Path(args.image_root))
    bundles = [_parse_bundle(_b) for _b in args.bundle]
    with tempfile.TemporaryDirectory(prefix="agent-bundles-") as tmp:
        bundles += _bundles_from_trees(args.rootfs_dir, Path(tmp))
        if not bundles:
            logger.info(
                "no agent bundle to ship: the image carries no update agent package"
            )
            return
        add_bundles(index_helper, bundles)
    index_helper.sync_index()
