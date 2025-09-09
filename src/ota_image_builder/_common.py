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

import json
import logging
import os
import sys
import threading
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Literal, NoReturn, ParamSpec, TypeVar

from ota_image_libs.v1.consts import (
    IMAGE_INDEX_FNAME,
    OCI_LAYOUT_CONTENT,
    OCI_LAYOUT_FNAME,
    RESOURCE_DIR,
)

P = ParamSpec("P")
RT = TypeVar("RT")
KT = TypeVar("KT")
VT = TypeVar("VT")

_MultiUnits = Literal["GiB", "MiB", "KiB", "Bytes", "KB", "MB", "GB"]
# fmt: off
_multiplier: dict[_MultiUnits, int] = {
    "GiB": 1024 ** 3, "MiB": 1024 ** 2, "KiB": 1024 ** 1,
    "GB": 1000 ** 3, "MB": 1000 ** 2, "KB": 1000 ** 1,
    "Bytes": 1,
}
# fmt: on

logger = logging.getLogger(__name__)


class WriteThreadSafeDict(dict[KT, VT]):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = threading.Lock()

    def __setitem__(self, key, value) -> None:
        with self._lock:
            return super().__setitem__(key, value)


def human_readable_size(_in: int) -> str:
    for _mu_name, _mu in _multiplier.items():
        if _mu == 1:
            break
        if (_res := (_in / _mu)) > 1:
            return f"{_res:.2f} {_mu_name}"
    return f"{_in} Bytes"


def func_call_with_se(
    _func: Callable[P, RT], se: threading.Semaphore
) -> Callable[P, RT]:
    @wraps(_func)
    def _wrapped(*args, **kwargs) -> RT:
        se.acquire()
        return _func(*args, **kwargs)

    return _wrapped


def configure_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s]-%(funcName)s:%(lineno)d,%(message)s",
    )
    _root_logger = logging.getLogger()
    # mute loggings from third-party packages
    _root_logger.setLevel(logging.CRITICAL)

    _builder_logger = logging.getLogger("ota_image_builder")
    _builder_logger.setLevel(logging.INFO)
    _libs_logger = logging.getLogger("ota_image_libs")
    _libs_logger.setLevel(logging.INFO)


def exit_with_err_msg(err_msg: str, exit_code: int = 1) -> NoReturn:
    print(f"ERR: {err_msg}")
    sys.exit(exit_code)


def count_blobs_in_dir(resource_dir: Path) -> tuple[int, int]:
    _count, _size = 0, 0
    for _count, entry in enumerate(os.scandir(resource_dir), start=1):
        _size += entry.stat().st_size
    return _count, _size


def check_if_valid_ota_image(image_root: Path) -> bool:
    """Check if the given path holds a valid OTA image.

    Args:
        image_root (Path): The path to the OTA image directory.

    Returns:
        bool: True if valid, False otherwise.
    """
    oci_layout_f = image_root / OCI_LAYOUT_FNAME
    if not oci_layout_f.is_file():
        logger.debug(f"OCI layout file not found: {oci_layout_f}")
        return False

    oci_layout_f_content = json.loads(oci_layout_f.read_text())
    if oci_layout_f_content != OCI_LAYOUT_CONTENT:
        logger.debug(f"Invalid OCI layout content: {oci_layout_f_content}")
        return False

    index_f = image_root / IMAGE_INDEX_FNAME
    if not index_f.is_file():
        logger.debug(f"Image index file not found: {index_f}")
        return False
    # NOTE: let image_index related functions to check if the index file is valid

    resource_dir = image_root / RESOURCE_DIR
    if not resource_dir.is_dir():
        logger.debug(f"Resource directory not found: {resource_dir}")
        return False
    return True
