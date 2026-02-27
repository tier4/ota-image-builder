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

from ota_image_libs.v1.artifact.packer import pack_artifact
from ota_image_libs.v1.image_index.utils import ImageIndexHelper

from ota_image_builder._common import check_if_valid_ota_image, exit_with_err_msg

if TYPE_CHECKING:
    from argparse import ArgumentParser, Namespace, _SubParsersAction


logger = logging.getLogger(__name__)

RW_CHUNK_SIZE = 8 * 1024**2  # 8MiB


def pack_artifact_cmd_args(
    sub_arg_parser: _SubParsersAction[ArgumentParser], *parent_parser: ArgumentParser
) -> None:
    pack_artifact_arg_parser = sub_arg_parser.add_parser(
        name="pack-artifact",
        help=(
            _help_txt
            := "Pack OTA image into one ZIP archive. This cmd implements reproducible build, "
            "for the same OTA image input, the output artifact is always the same."
        ),
        description=_help_txt,
        parents=parent_parser,
    )
    pack_artifact_arg_parser.add_argument(
        "-o",
        "--output",
        help="The location to output the ZIP archive to.",
        required=True,
    )
    pack_artifact_arg_parser.add_argument(
        "image_root",
        help="The location of the OTA image.",
    )
    pack_artifact_arg_parser.set_defaults(handler=pack_artifact_cmd)


def pack_artifact_cmd(args: Namespace) -> None:
    logger.debug(f"calling {pack_artifact_cmd.__name__} with {args}")
    image_root = Path(args.image_root)
    output = Path(args.output)
    if output.exists():
        exit_with_err_msg(f"{output} already exists!")
    print(f"Will output the ZIP archive to {output}.")

    if not image_root.is_dir():
        exit_with_err_msg(f"{image_root} is not a directory!")
    _index_helper = ImageIndexHelper(image_root)
    if not _index_helper.image_index.image_signed:
        exit_with_err_msg(
            "image is not yet signed, please sign it before pack_artifact, abort!"
        )

    if not check_if_valid_ota_image(image_root):
        exit_with_err_msg(f"{image_root} doesn't hold a valid OTA image.")

    try:
        pack_artifact(image_root, output, rw_chunk_size=RW_CHUNK_SIZE)
        print(f"Packed artifact is populated to {output}.")
    except Exception as e:
        _err_msg = (
            f"failed to pack OTA image at {image_root} and output to {output}: {e}"
        )
        logger.error(_err_msg, exc_info=e)
        exit_with_err_msg(_err_msg)
