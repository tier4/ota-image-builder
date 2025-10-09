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
import shutil
from pathlib import Path
from typing import TYPE_CHECKING
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from ota_image_libs.v1.consts import IMAGE_INDEX_FNAME
from ota_image_libs.v1.image_index.utils import ImageIndexHelper

from ota_image_builder._common import check_if_valid_ota_image, exit_with_err_msg

if TYPE_CHECKING:
    from argparse import ArgumentParser, Namespace, _SubParsersAction


logger = logging.getLogger(__name__)

REPORT_BATCH_SIZE = 10_000
DEFAULT_TIMESTAMP = (2009, 1, 1, 0, 0, 0)
RW_CHUNK_SIZE = 8 * 1024 * 1024  # 8MiB


def _add_dir(zipf: ZipFile, filename: Path, arcname: Path | str) -> None:
    """
    Add a directory to the OTA image zipfile. The src must be a directory.
    """
    _zipinfo = ZipInfo.from_file(
        filename=f"{str(filename).rstrip('/')}/",
        arcname=f"{str(arcname).rstrip('/')}/",
    )
    _zipinfo.CRC = 0
    _zipinfo.date_time = DEFAULT_TIMESTAMP
    zipf.mkdir(_zipinfo, mode=0o755)


def _add_file(zipf: ZipFile, filename: Path, arcname: Path | str) -> None:
    """
    Add a regular file to the OTA image zipfile. The src must be a regular file.

    Basically a copy of the ZipFile.writestr method.
    """
    _zipinfo = ZipInfo.from_file(filename=filename, arcname=str(arcname))
    _zipinfo.date_time = DEFAULT_TIMESTAMP
    _zipinfo.compress_type = zipf.compression
    _zipinfo.compress_level = zipf.compresslevel
    _zipinfo.external_attr |= 0o644 << 16  # rw_r_r_

    with open(filename, "rb") as src, zipf.open(_zipinfo, "w") as dst:
        shutil.copyfileobj(src, dst, RW_CHUNK_SIZE)


def _pack_artifact(_image_root: Path, _output: Path):
    _file_count, _top_level = 0, True
    with ZipFile(_output, mode="w", compression=ZIP_STORED) as output_f:
        for curdir, _, files in os.walk(_image_root):
            curdir = Path(curdir)
            relative_curdir = curdir.relative_to(_image_root)

            if _top_level:
                _top_level = False

                # add the index.json file as the first file entry in zipfile,
                #   effectively defining the manifest for this image.
                # see https://pkware.cachefly.net/webdocs/casestudies/APPNOTE.TXT chapter 4.1.11
                #   for more details about ZIP manifest.
                _add_file(
                    zipf=output_f,
                    filename=curdir / IMAGE_INDEX_FNAME,
                    arcname=IMAGE_INDEX_FNAME,
                )
                _file_count += 1

                for _fname in sorted(files):
                    if _fname == IMAGE_INDEX_FNAME:
                        continue
                    _add_file(zipf=output_f, filename=curdir / _fname, arcname=_fname)
                    _file_count += 1

            else:
                _add_dir(zipf=output_f, filename=curdir, arcname=relative_curdir)
                for _file in sorted(files):
                    _src = curdir / _file
                    _relative_src = relative_curdir / _file
                    _add_file(zipf=output_f, filename=_src, arcname=_relative_src)
                    _file_count += 1
                    if _file_count % REPORT_BATCH_SIZE == 0:
                        print(
                            f"Packing in-progress: {_file_count} files are packed ..."
                        )


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
        _pack_artifact(image_root, output)
        print(f"Packed artifact is populated to {output}.")
    except Exception as e:
        _err_msg = (
            f"failed to pack OTA image at {image_root} and output to {output}: {e}"
        )
        logger.error(_err_msg, exc_info=e)
        exit_with_err_msg(_err_msg)
