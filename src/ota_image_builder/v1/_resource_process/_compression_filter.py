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
"""Do compression over large resources."""

from __future__ import annotations

import _thread
import contextlib
import logging
import os
import sqlite3
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
from itertools import count
from pathlib import Path

import zstandard
from ota_image_libs._resource_filter import CompressFilter
from ota_image_libs.common import tmp_fname
from ota_image_libs.v1.consts import ZSTD_COMPRESSION_ALG
from ota_image_libs.v1.resource_table.db import ResourceTableDBHelper
from ota_image_libs.v1.resource_table.schema import (
    ResourceTableManifest,
    ResourceTableManifestTypedDict,
)

from ota_image_builder._common import (
    WriteThreadSafeDict,
    func_call_with_se,
    human_readable_size,
)
from ota_image_builder._configs import cfg

from ._db_utils import count_entries_in_table

logger = logging.getLogger(__name__)

_global_shutdown = False


class CompressionFilterProcesser:
    def __init__(
        self,
        *,
        resource_dir: Path,
        rst_dbf: Path,
        size_lower_bound: int = cfg.COMPRESSION_LOWER_THRESHOLD,
        compression_ratio_threshold: float = cfg.COMPRESSION_MIN_RATIO,
        zstd_compression_level: int = cfg.ZSTD_COMPRESSION_LEVEL,
        read_size: int = cfg.READ_SIZE,
        worker_threads: int = cfg.COMPRESSION_RESOURCE_SCAN_WORKER_THREADS,
        zstd_multi_threads: int = cfg.COMPRESSION_ZSTD_MULTI_THTHREADS,
        concurrent_jobs: int = cfg.COMPRESSION_MAX_CONCURRENT,
    ) -> None:
        self._read_size = read_size
        self._resource_dir = resource_dir
        self._db_helper = ResourceTableDBHelper(rst_dbf)
        self._lower_bound = size_lower_bound
        self._compression_ratio_threshold = compression_ratio_threshold

        self._zstd_multi_threads = zstd_multi_threads
        self._zstd_compression_level = zstd_compression_level

        self._worker_thread_local = threading.local()
        self._worker_threads = worker_threads
        self._se = threading.Semaphore(concurrent_jobs)

        # <origin_rs_id>, (<compressed_digest>, <compressed_size>)
        self._compressed: WriteThreadSafeDict[int, tuple[bytes, int]] = (
            WriteThreadSafeDict()
        )

    def _thread_worker_initializer(self) -> None:
        thread_local = self._worker_thread_local
        thread_local.cctx = zstandard.ZstdCompressor(
            level=self._zstd_compression_level, threads=self._zstd_multi_threads
        )

    # ------ worker thread workload ------ #

    def _do_compression_at_thread(self, src: Path, dst: Path) -> tuple[bytes, int]:
        cctx: zstandard.ZstdCompressor = self._worker_thread_local.cctx
        src_size = src.stat().st_size
        hasher, compressed_size = sha256(), 0
        with open(src, "rb") as src_f, open(dst, "wb") as dst_f:
            # NOTE: VERY IMPORTANT! Need to provide the src_f's size, otherwise the
            #       python zstandard might complain the generated compressed file
            #       is broken during decompressing.
            for compressed_chunk in cctx.read_to_iter(
                src_f, size=src_size, read_size=self._read_size
            ):
                compressed_size += len(compressed_chunk)
                hasher.update(compressed_chunk)
                dst_f.write(compressed_chunk)
        return hasher.digest(), compressed_size

    def _process_one_entry_at_thread(self, _row: tuple[int, bytes, int]) -> None:
        resource_id, origin_digest, origin_size = _row
        origin_resource = self._resource_dir / origin_digest.hex()

        _tmp_compressed = self._resource_dir / tmp_fname(origin_digest.hex())
        try:
            compressed_digest, compressed_size = self._do_compression_at_thread(
                origin_resource, _tmp_compressed
            )
            if origin_size / compressed_size >= self._compression_ratio_threshold:
                os.replace(
                    _tmp_compressed, self._resource_dir / compressed_digest.hex()
                )
                origin_resource.unlink(missing_ok=True)
                self._compressed[resource_id] = compressed_digest, compressed_size
        finally:
            _tmp_compressed.unlink(missing_ok=True)

    # ------------------------ #

    def _task_done_cb(self, _fut: Future):
        self._se.release()
        if exc := _fut.exception():
            logger.debug(
                f"failed on compression filter applying: {exc!r}", exc_info=exc
            )
            global _global_shutdown
            if not _global_shutdown:
                _global_shutdown = True
                _thread.interrupt_main()

    def process(self) -> None:
        rs_orm = self._db_helper.get_orm()
        with contextlib.closing(rs_orm.orm_con):
            _table_name = rs_orm.orm_table_name
            # NOTE: resource_id starts from 1
            next_rs_id = count(start=count_entries_in_table(rs_orm) + 1)

            origin_size = 0

            _stmt = ResourceTableManifest.table_select_stmt(
                select_from=_table_name,
                select_cols=("resource_id", "digest", "size"),
                where_stmt=f"WHERE size > {self._lower_bound} AND filter_applied IS NULL",
            )
            with ThreadPoolExecutor(
                initializer=self._thread_worker_initializer,
                max_workers=self._worker_threads,
                thread_name_prefix="ota_image_builder",
            ) as pool:
                submit_with_se = func_call_with_se(pool.submit, self._se)
                for _raw_row in rs_orm.orm_select_entries(
                    _stmt=_stmt, _row_factory=sqlite3.Row
                ):
                    origin_size += _raw_row[-1]
                    submit_with_se(
                        self._process_one_entry_at_thread,
                        _raw_row,  # type: ignore
                    ).add_done_callback(self._task_done_cb)

            # ------ update database ------ #
            # insert the newly added resources
            # NOTE: resource_id is 1to1 mapping to original resource, so we deepcopy the counter.
            _copied_counter = deepcopy(next_rs_id)
            rs_orm.orm_insert_mappings(
                ResourceTableManifestTypedDict(
                    resource_id=_next_id,
                    digest=compressed_digest,
                    size=compressed_size,
                )
                for _next_id, (compressed_digest, compressed_size) in zip(
                    _copied_counter, self._compressed.values(), strict=False
                )
            )

            # update the origianl resources that being compressed
            rs_orm.orm_update_entries_many(
                set_cols=("filter_applied",),
                set_cols_value=(
                    ResourceTableManifestTypedDict(
                        filter_applied=CompressFilter(
                            resource_id=_next_rs_id,
                            compression_alg=ZSTD_COMPRESSION_ALG,
                        )
                    )
                    for _next_rs_id in next_rs_id
                ),
                where_cols=("resource_id",),
                where_cols_value=(
                    ResourceTableManifestTypedDict(resource_id=_resource_id)
                    for _resource_id in self._compressed
                ),
            )

            compressed = len(self._compressed)
            compressed_size = sum(_size for _, _size in self._compressed.values())
            logger.info(
                f"compression_filter: total {compressed} files are compressed"
                f"(original size: {human_readable_size(origin_size)}, "
                f"compressed size: {human_readable_size(compressed_size)})"
            )
