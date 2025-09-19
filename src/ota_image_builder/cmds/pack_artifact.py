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
import os
from pathlib import Path
from typing import TYPE_CHECKING
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from ota_image_builder._common import check_if_valid_ota_image, exit_with_err_msg

if TYPE_CHECKING:
    from argparse import ArgumentParser, Namespace, _SubParsersAction


logger = logging.getLogger(__name__)

REPORT_BATCH_SIZE = 10_000
DEFAULT_TIMESTAMP = (2009, 1, 1, 0, 0, 0)


def _pack_artifact(_image_root: Path, _output: Path):
    _file_count = 0
    with ZipFile(_output, mode="w", compression=ZIP_STORED) as output_f:
        for curdir, _, files in os.walk(_image_root):
            curdir = Path(curdir)
            relative_curdir = curdir.relative_to(_image_root)

            _curdir_zipinfo = ZipInfo.from_file(
                filename=f"{str(curdir).rstrip('/')}/",
                arcname=f"{str(relative_curdir).rstrip('/')}/",
            )
            _curdir_zipinfo.CRC = 0
            _curdir_zipinfo.date_time = DEFAULT_TIMESTAMP
            output_f.mkdir(_curdir_zipinfo, mode=0o755)

            for _file in files:
                _src = curdir / _file
                _relative_src = relative_curdir / _file

                _src_zipinfo = ZipInfo.from_file(
                    filename=_src, arcname=str(_relative_src)
                )
                _src_zipinfo.date_time = DEFAULT_TIMESTAMP
                _src_zipinfo.compress_type = output_f.compression
                _src_zipinfo.compress_level = output_f.compresslevel
                # NOTE: for OTA image, we have regulated the file size(less than 32MiB pre-blob),
                #       so just directly read the whole chunk is not a problem.
                output_f.writestr(_src_zipinfo, _src.read_bytes())
                _file_count += 1

                if _file_count % REPORT_BATCH_SIZE == 0:
                    print(f"Packing in-progress: {_file_count} files are packed ...")


def pack_artifact_cmd_args(
    sub_arg_parser: _SubParsersAction[ArgumentParser], *parent_parser: ArgumentParser
) -> None:
    pack_artifact_arg_parser = sub_arg_parser.add_parser(
        name="pack-artifact",
        help=(_help_txt := "Pack OTA image into one ZIP archive."),
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

    if not check_if_valid_ota_image(image_root):
        exit_with_err_msg(f"{image_root} doesn't hold a valid OTA image.")

    try:
        _pack_artifact(image_root, output)
        print(f"Packed artifact is populated to {output}.")
    except Exception as e:
        _err_msg = (
            f"failed to pack OTA image at {image_root} and output to {output}: {e}"
        )
        logger.error(_err_msg, exc_info=e)
        exit_with_err_msg(_err_msg)
