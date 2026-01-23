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
"""CLI interface for adding a rootfs image into an OTA image."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from queue import Queue
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any

import yaml
from ota_image_libs.common import tmp_fname
from ota_image_libs.v1.annotation_keys import (
    NVIDIA_JETSON_BSP_VER,
    OTA_RELEASE_KEY,
    PLATFORM_ECU,
)
from ota_image_libs.v1.file_table.schema import ZstdCompressedFileTableDescriptor
from ota_image_libs.v1.image_config.schema import ImageConfig
from ota_image_libs.v1.image_config.sys_config import SysConfig
from ota_image_libs.v1.image_index.utils import ImageIndexHelper
from ota_image_libs.v1.image_manifest.schema import ImageManifest, OTAReleaseKey
from ota_image_libs.v1.resource_table.schema import ResourceTableDescriptor

from ota_image_builder._common import (
    NV_TEGRA_RELEASE_FPATH,
    check_if_valid_ota_image,
    exit_with_err_msg,
    get_bsp_ver_info,
    human_readable_size,
)
from ota_image_builder._configs import cfg
from ota_image_builder.cmds._utils import validate_annotations
from ota_image_builder.v1._image_config import (
    AddImageConfigAnnotations,
    compose_image_config,
)
from ota_image_builder.v1._image_manifest import (
    AddImageManifestAnnotations,
    compose_image_manifest,
)
from ota_image_builder.v1._resource_process._db_utils import (
    DataBaseBuilder,
    ImageStats,
    ImageStatsQuery,
    init_file_table_db,
    init_resource_table_db,
)
from ota_image_builder.v1._resource_process._rootfs_process import SystemImageProcesser

if TYPE_CHECKING:
    from argparse import ArgumentParser, Namespace, _SubParsersAction


logger = logging.getLogger(__name__)


class AddImageCMDAnnotations(AddImageManifestAnnotations, AddImageConfigAnnotations):
    """Annotations needed for add-image cmd."""


def add_image_cmd_args(
    sub_arg_parser: _SubParsersAction[ArgumentParser], *parent_parser: ArgumentParser
) -> None:
    add_image_arg_parser = sub_arg_parser.add_parser(
        name="add-image",
        help=(_help_txt := "Add a system rootfs image into OTA image"),
        description=_help_txt,
        parents=parent_parser,
    )
    add_image_arg_parser.add_argument(
        "--annotations-file",
        help="A yaml file that contains annotations for this rootfs image.",
        required=True,
    )
    add_image_arg_parser.add_argument(
        "--sys-config",
        action="append",
        help="A yaml for post OTA configuration target ECU. "
        "Can be used multiple times for multi-spec OTA image. "
        "Schema: `<ecu_id>:[<path_to_syscfg_file>]`. "
        "<path_to_syscfg_file> can be empty, which means no sys config file "
        "will be used for this ECU.",
        required=True,
    )
    add_image_arg_parser.add_argument(
        "--release-key",
        choices=["dev", "prd"],
        help="The release variant of the input system rootfs image. "
        "Available choices: 'dev' or 'prd'. "
        "If not set, the `vnd.tier4.ota.release-key` annotation will be used instead.",
    )
    add_image_arg_parser.add_argument(
        "--rootfs",
        default="/rootfs",
        help="The location of rootfs to be imported. Default: /rootfs",
        required=True,
    )
    add_image_arg_parser.add_argument(
        "--tmp-dir",
        default=None,
        help="The temporary working dir used when importing the rootfs image. "
        "If not set, OTA image builder will setup a temporary workdir by itself.",
    )
    add_image_arg_parser.add_argument(
        "image_root",
        help="The folder of the OTA image we will add new system rootfs image to.",
    )
    add_image_arg_parser.set_defaults(handler=add_image_cmd)


def _parse_specs(sys_config_pairs: list[str]) -> dict[str, Path | None]:
    """Parse and validate the --sys-config args."""
    sys_config_files: dict[str, Path | None] = {}
    sys_config_pair: str  # schema: `<ecu_id>:<path_to_syscfg_file>`
    for sys_config_pair in sys_config_pairs:
        _ecu_id, _syscfg_fpath = sys_config_pair.split(":", 1)
        if not _syscfg_fpath:
            logger.warning(f"No sys_config is defined for ECU {_ecu_id}.")
            sys_config_files[_ecu_id] = None
            continue

        _syscfg_fpath = Path(_syscfg_fpath)
        try:
            _loaded_sys_cfg = yaml.safe_load(_syscfg_fpath.read_text())
            assert isinstance(_loaded_sys_cfg, dict), "invalid sys_config file"
            SysConfig.model_validate(_loaded_sys_cfg)
            sys_config_files[_ecu_id] = _syscfg_fpath
        except Exception as e:
            logger.debug(
                f"invalid sys_config file {_syscfg_fpath}: {e}",
                exc_info=e,
            )
            exit_with_err_msg(
                f"sys config file {_syscfg_fpath} is not a valid sys config file: {e}"
            )
    return sys_config_files


def _add_one_spec(
    file_table_descriptor: ZstdCompressedFileTableDescriptor,
    *,
    sys_config: Path | None,
    resource_dir: Path,
    annotations: dict[str, Any],
) -> ImageManifest.Descriptor:
    """For multi-spec OTA image, add one spec into the OTA image."""
    sys_config_descriptor = None
    if sys_config is not None:
        sys_config_descriptor = SysConfig.Descriptor.add_file_to_resource_dir(
            sys_config,
            resource_dir=resource_dir,
        )

    image_config = compose_image_config(
        file_table_descriptor=file_table_descriptor,
        sys_config_descriptor=sys_config_descriptor,
        annotations=annotations,
    )
    image_config_descriptor = ImageConfig.Descriptor.export_metafile_to_resource_dir(
        meta_file=image_config,
        resource_dir=resource_dir,
    )

    image_manifest = compose_image_manifest(
        image_config_descriptor=image_config_descriptor,
        file_table_descriptor=file_table_descriptor,
        annotations=annotations,
    )
    return ImageManifest.Descriptor.export_metafile_to_resource_dir(
        meta_file=image_manifest,
        resource_dir=resource_dir,
        annotations=annotations,
    )


def _process_rootfs_image(
    *, rootfs: Path, resource_dir: Path, ft_dbf: Path, rst_dbf: Path
) -> tuple[ImageStats, ZstdCompressedFileTableDescriptor]:
    """Process the input rootfs image, add it into the OTA image."""
    init_file_table_db(ft_dbf)

    start_time = time.time()
    _que = Queue()
    db_builder = DataBaseBuilder(ft_dbf=ft_dbf, rst_dbf=rst_dbf, que=_que)

    logger.info("Start processing of input rootfs ...")
    db_builder_thread = db_builder.start_builder_thread()
    image_processor = SystemImageProcesser(_que, src=rootfs, resource_dir=resource_dir)
    image_processor.process_sysimg_src()

    db_builder_thread.join()
    logger.info(
        f"Finish initial processing of input rootfs: time cost: {int(time.time() - start_time)}s"
    )

    image_stat_query = ImageStatsQuery(ft_dbf=ft_dbf, rst_dbf=rst_dbf)
    image_stat = image_stat_query.get_stats_after_process()
    logger.info(f"System image statistics: {image_stat}\n")

    logger.info("Add file_table into the OTA image ...")
    _ft_db_descriptor = ZstdCompressedFileTableDescriptor.add_file_to_resource_dir(
        ft_dbf,
        resource_dir=resource_dir,
        remove_origin=True,
        zstd_compression_level=cfg.DB_ZSTD_COMPRESSION_LEVEL,
    )

    logger.info(
        f"Add file_table for this image to OTA image: {_ft_db_descriptor},\n"
        f"file_table size: {human_readable_size(_ft_db_descriptor.size)}"
    )
    logger.info(
        f"Image is added into the OTA image: time cost {int(time.time()) - start_time}s"
    )
    return image_stat, _ft_db_descriptor


def _detect_nvidia_jetson_bsp_ver(_rootfs_dir: Path) -> str | None:
    """Try to detect rootfs BSP version.

    If it is a rootfs image for NVIDIA Jetson ECU, return the detected BSP ver string.
    """
    nv_tegra_release_fpath = _rootfs_dir / Path(NV_TEGRA_RELEASE_FPATH).relative_to("/")
    if not nv_tegra_release_fpath.is_file():
        return

    return get_bsp_ver_info(nv_tegra_release_fpath.read_text())


def add_image_cmd(args: Namespace) -> None:
    logger.debug(f"calling {add_image_cmd.__name__} with {args}")
    image_root = Path(args.image_root)
    if not check_if_valid_ota_image(image_root):
        exit_with_err_msg(f"{image_root} is not a valid OTA image root directory.")
    index_helper = ImageIndexHelper(image_root=image_root)

    rootfs_path = Path(args.rootfs)
    if not rootfs_path.is_dir():
        exit_with_err_msg(f"Rootfs path {rootfs_path} is not a directory.")

    logger.info(
        f"Will add image payload from {rootfs_path} into OTA image at {image_root} ..."
    )

    annotations = validate_annotations(
        Path(args.annotations_file), AddImageCMDAnnotations
    )
    if args.release_key:
        ota_release_key = OTAReleaseKey(args.release_key)
        logger.info(f"Release key specified from CLI args: {ota_release_key}")
        annotations[OTA_RELEASE_KEY] = ota_release_key.value
    else:
        ota_release_key = annotations.get(OTA_RELEASE_KEY, None)
        try:
            OTAReleaseKey(ota_release_key)
        except ValueError:
            exit_with_err_msg(
                f"Invalid release key {ota_release_key} in annotations, "
                "should be one of 'dev' or 'prd'."
            )
        logger.info(f"Using release key from annotations: {ota_release_key}.")

    # try to determine the rootfs' BSP version if it is a NVIDIA Jetson ECU's rootfs image
    if rootfs_bsp_ver := _detect_nvidia_jetson_bsp_ver(rootfs_path):
        logger.info(
            f"Rootfs image is an rootfs image of NVIDIA Jetson ECU, rootfs BSP ver: {rootfs_bsp_ver}"
        )
        logger.info(
            f"Add {NVIDIA_JETSON_BSP_VER} annotation to the image_manifest and image_config"
        )
        annotations[NVIDIA_JETSON_BSP_VER] = rootfs_bsp_ver

    sys_config_files = _parse_specs(args.sys_config)

    # NOTE: if work_dir is set, use it as the parent of the temporary workdir.
    with TemporaryDirectory(dir=args.tmp_dir) as tmp_workdir:
        logger.debug(f"Using temporary workdir: {tmp_workdir}")
        tmp_workdir = Path(tmp_workdir)

        resource_dir = index_helper.image_resource_dir
        file_table_db = tmp_workdir / tmp_fname("file_table.sqlite3")
        rst_dbf = tmp_workdir / tmp_fname("resource_table.sqlite3")

        # use the existed OTA image's scope resource_table or initialize a new one
        if _old_rst_descriptor := index_helper.image_index.image_resource_table:
            logger.info(
                "Found existing resource_table db, "
                "will update this db when importing new image payload."
            )
            _old_rst_descriptor.export_blob_from_resource_dir(
                resource_dir=resource_dir,
                save_dst=rst_dbf,
                auto_decompress=True,
            )
        else:
            logger.info("First image payload to add, init resource_table db.")
            init_resource_table_db(rst_dbf)

        # add input rootfs image as image payload into OTA image
        rootfs_image_stat, file_table_descriptor = _process_rootfs_image(
            rootfs=rootfs_path,
            resource_dir=resource_dir,
            ft_dbf=file_table_db,
            rst_dbf=rst_dbf,
        )

        # support for multi-spec OTA image
        # for each multi-spec ecu_id,sys_config pair, we will add image_manifest backed
        #   by the same image payload(image_meta).
        for _ecu_id, _sys_config in sys_config_files.items():
            logger.info(f"Add manifest for spec: {_ecu_id=},{_sys_config=}")
            _annotations = annotations.copy()
            _annotations[PLATFORM_ECU] = _ecu_id
            # NOTE: add the rootfs image_stats into the annotations.
            _annotations.update(rootfs_image_stat)

            image_manifest_descriptor = _add_one_spec(
                file_table_descriptor,
                sys_config=_sys_config,
                resource_dir=resource_dir,
                annotations=_annotations,
            )
            index_helper.image_index.add_image(image_manifest_descriptor)

        # NOTE that we will do the zstd compression when finalizing the OTA image, so
        #   not applying zstd compression yet here.
        _new_rst_descriptor = ResourceTableDescriptor.add_file_to_resource_dir(
            rst_dbf,
            resource_dir=resource_dir,
            remove_origin=True,
        )
        logger.info(
            "Update the resource_table in the OTA image: \n"
            f"New resource_table blob: {_new_rst_descriptor.digest}"
        )
        index_helper.image_index.update_resource_table(_new_rst_descriptor)

        if (
            _old_rst_descriptor
            and _old_rst_descriptor.digest != _new_rst_descriptor.digest
        ):
            logger.info(
                "Remove the old resource_table blob as the new one is added: \n"
                f"Old resource_table blob: {_old_rst_descriptor.digest}"
            )
            _old_rst_descriptor.remove_blob_from_resource_dir(resource_dir)

        logger.info("Sync index.json on finishing up adding image payload.")
        index_helper.sync_index()
