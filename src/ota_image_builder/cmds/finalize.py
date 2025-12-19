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
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from ota_image_libs.common import tmp_fname
from ota_image_libs.v1.image_index.utils import ImageIndexHelper
from ota_image_libs.v1.image_manifest.schema import ImageManifest
from ota_image_libs.v1.otaclient_package.schema import OTAClientPackageManifest
from ota_image_libs.v1.resource_table.schema import (
    ZstdCompressedResourceTableDescriptor,
)

from ota_image_builder._common import (
    check_if_valid_ota_image,
    count_blobs_in_dir,
    exit_with_err_msg,
)
from ota_image_builder._configs import cfg
from ota_image_builder.v1._resource_process._bundle_filter import BundleFilterProcesser
from ota_image_builder.v1._resource_process._compression_filter import (
    CompressionFilterProcesser,
)
from ota_image_builder.v1._resource_process._db_utils import vacuum_db
from ota_image_builder.v1._resource_process._slice_filter import SliceFilterProcesser

if TYPE_CHECKING:
    from argparse import ArgumentParser, Namespace, _SubParsersAction


logger = logging.getLogger(__name__)


def finalize_cmd_args(
    sub_arg_parser: _SubParsersAction[ArgumentParser], *parent_parser: ArgumentParser
) -> None:
    finalize_cmd_args = sub_arg_parser.add_parser(
        name="finalize",
        help=(
            _help_txt
            := "Finalize and optimize the OTA image, make it ready for signing"
        ),
        description=_help_txt,
        parents=parent_parser,
    )
    finalize_cmd_args.add_argument(
        "--tmp-dir",
        default=None,
        help="The temporary working dir when finalizing the OTA image. "
        "If not set, OTA image builder will setup a temporary workdir by itself.",
    )
    finalize_cmd_args.add_argument(
        "--o-skip-bundle",
        action="store_true",
        help="(Advanced) Skip applying bundle filter to the OTA image blob store.",
        default=False,
    )
    finalize_cmd_args.add_argument(
        "--o-skip-compression",
        action="store_true",
        help="(Advanced) Skip applying compression filter to the OTA image blob store.",
        default=False,
    )
    finalize_cmd_args.add_argument(
        "--o-skip-slice",
        action="store_true",
        help="(Advanced) Skip applying slice filter to the OTA image blob store.",
        default=False,
    )
    finalize_cmd_args.add_argument(
        "image_root",
        help="The folder which holds an OTA image.",
    )
    finalize_cmd_args.set_defaults(handler=finalize_cmd)


def _collect_protected_resources_digest(_index_helper: ImageIndexHelper) -> set[bytes]:
    """Scan through OTA image, collect blob digests that don't belong to any system image.

    When optimizing the blob storage, we MUST skip processing these digests.
    The blobs that don't belong any  image payload:
    1. image_payload: sys_config and file_table files.
    2. otaclient_release: manifest.json and release packages.
    3. resource_table itself.

    NOTE(20251219): an example case is when the system image is dev build, and contains
                    the pilot-auto source code within the built system image.
                    This will result in blob of sys_config file is also part of the system image,
                    thus being processed during blob storage optimization, and the original blob
                    being removed.
    """
    _res: set[bytes] = set()
    _resource_dir = _index_helper.image_resource_dir
    for manifest_descriptor in _index_helper.image_index.manifests:
        _res.add(manifest_descriptor.digest.digest)
        if isinstance(manifest_descriptor, ImageManifest.Descriptor):
            _manifest = manifest_descriptor.load_metafile_from_resource_dir(
                _resource_dir
            )

            for _file_table_descriptor in _manifest.layers:
                _res.add(_file_table_descriptor.digest.digest)

            _image_config_descriptor = _manifest.config
            _res.add(_image_config_descriptor.digest.digest)

            _image_config = _image_config_descriptor.load_metafile_from_resource_dir(
                _resource_dir
            )
            if _sys_config_descriptor := _image_config.sys_config:
                _res.add(_sys_config_descriptor.digest.digest)
            _res.add(_image_config.file_table.digest.digest)
        elif isinstance(manifest_descriptor, OTAClientPackageManifest.Descriptor):
            _manifest = manifest_descriptor.load_metafile_from_resource_dir(
                _resource_dir
            )
            _res.add(_manifest.config.digest.digest)
            for _payload in _manifest.layers:
                _res.add(_payload.digest.digest)
    return _res


def finalize_cmd(args: Namespace) -> None:
    logger.debug(f"calling {finalize_cmd.__name__} with {args}")
    image_root = Path(args.image_root)
    if not check_if_valid_ota_image(image_root):
        exit_with_err_msg(f"{image_root} doesn't hold a valid OTA image.")

    index_helper = ImageIndexHelper(image_root)
    logger.info(f"Finalize and optimize OTA image at {image_root} ...")
    logger.info("Optimizing the blob storage of the OTA image ...")
    protected_resources = _collect_protected_resources_digest(index_helper)
    logger.debug(
        f"Skip the protected resources: {[d.hex() for d in protected_resources]}"
    )

    resource_dir = index_helper.image_resource_dir
    _old_rstable_descriptor = index_helper.image_index.image_resource_table
    if _old_rstable_descriptor is None:
        exit_with_err_msg(
            "The OTA image doesn't have a resource_table, "
            "please add at least one image payload into the OTA image."
        )

    with TemporaryDirectory(dir=args.tmp_dir) as tmp_workdir:
        logger.debug(f"Using temporary workdir: {tmp_workdir}")
        tmp_workdir = Path(tmp_workdir)

        _working_rstable = tmp_workdir / tmp_fname("resource_table.sqlite3")
        logger.debug(f"Exporting resource_table to {_working_rstable}")
        _old_rstable_descriptor.export_blob_from_resource_dir(
            resource_dir=resource_dir,
            save_dst=_working_rstable,
            auto_decompress=True,
        )

        start_time = time.time()
        if not args.o_skip_bundle:
            logger.info("Apply bundle filter to the blob storage ...")
            BundleFilterProcesser(
                resource_dir=resource_dir,
                rst_dbf=_working_rstable,
                protected_resources=protected_resources,
            ).process()
            logger.info(
                f"Finish applying bundle filter: time cost: {int(time.time() - start_time)}s"
            )
        else:
            logger.warning("Skip optimizing OTA image blob storage with bundle filter.")

        if not args.o_skip_compression:
            logger.info("Apply compression filter to the blob storage ...")
            _start_time = time.time()
            CompressionFilterProcesser(
                resource_dir=resource_dir,
                rst_dbf=_working_rstable,
                protected_resources=protected_resources,
            ).process()
            logger.info(
                f"Finish applying compression filter: time cost: {int(time.time() - _start_time)}s"
            )
        else:
            logger.warning(
                "Skip optimizing OTA image blob storage with compression filter."
            )

        if not args.o_skip_slice:
            logger.info("Apply slice filter to the blob storage ...")
            _start_time = time.time()
            SliceFilterProcesser(
                resource_dir=resource_dir,
                rst_dbf=_working_rstable,
                protected_resources=protected_resources,
            ).process()
            logger.info(
                f"Finish applying slice filter: time cost: {int(time.time() - _start_time)}s"
            )
        else:
            logger.warning("Skip optimizing OTA image blob storage with slice filter.")

        logger.info(
            "Finish up finalizing the blob storage of the OTA image, "
            f"total time cost: {int(time.time() - start_time)}s"
        )
        logger.info("Optimize resource_table ...")
        vacuum_db(_working_rstable)

        logger.info("Add the updated resource_table back to the OTA image ...")
        _new_rstable_descriptor = (
            ZstdCompressedResourceTableDescriptor.add_file_to_resource_dir(
                _working_rstable,
                resource_dir=resource_dir,
                remove_origin=True,
                zstd_compression_level=cfg.DB_ZSTD_COMPRESSION_LEVEL,
            )
        )
        logger.debug(f"Updated resource_table: {_new_rstable_descriptor}")
        logger.debug(f"Remove the old resource_table's blob: {_old_rstable_descriptor}")
        _old_rstable_descriptor.remove_blob_from_resource_dir(resource_dir)
        index_helper.image_index.update_resource_table(_new_rstable_descriptor)

        total_blobs_count, total_blobs_size = count_blobs_in_dir(resource_dir)
        logger.info(
            f"OTA image blob storage: {total_blobs_count=}, {total_blobs_size=}"
        )
        index_helper.image_index.finalize_image(
            total_blobs_count=total_blobs_count,
            total_blobs_size=total_blobs_size,
        )
        logger.info(
            "Finalize the OTA image, write the updated index.json to OTA image."
        )
        index_helper.sync_index()
