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
from hashlib import sha256
from pathlib import Path
from typing import TypeAlias

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

logger = logging.getLogger(__name__)

_global_shutdown = False

ResourceID: TypeAlias = int
Sha256DigestBytes: TypeAlias = bytes
Size: TypeAlias = int
CompressionResult = WriteThreadSafeDict[ResourceID, tuple[Sha256DigestBytes, Size]]


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
        concurrent_jobs: int = cfg.COMPRESSION_MAX_CONCURRENT,
        protected_resources: set[bytes],
    ) -> None:
        self._protected_resources = protected_resources
        self._read_size = read_size
        self._resource_dir = resource_dir
        self._db_helper = ResourceTableDBHelper(rst_dbf)
        self._lower_bound = size_lower_bound
        self._compression_ratio_threshold = compression_ratio_threshold

        self._zstd_compression_level = zstd_compression_level

        self._worker_thread_local = threading.local()
        self._worker_threads = worker_threads
        self._se = threading.Semaphore(concurrent_jobs)

    def _thread_worker_initializer(self) -> None:
        thread_local = self._worker_thread_local
        thread_local.cctx = zstandard.ZstdCompressor(
            level=self._zstd_compression_level,
            write_checksum=True,
            write_content_size=True,
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

    def _process_one_entry_at_thread(
        self,
        row: tuple[int, bytes, int],
        compressed: WriteThreadSafeDict[int, tuple[bytes, int]],
    ) -> None:
        resource_id, origin_digest, origin_size = row
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
                compressed[resource_id] = compressed_digest, compressed_size
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

    def _process_compression(self) -> tuple[int, CompressionResult]:
        origin_size, compressed = 0, CompressionResult()

        rs_orm = self._db_helper.get_orm()
        with (
            contextlib.closing(rs_orm.orm_con),
            ThreadPoolExecutor(
                initializer=self._thread_worker_initializer,
                max_workers=self._worker_threads,
                thread_name_prefix="ota_image_builder",
            ) as pool,
        ):
            _stmt = ResourceTableManifest.table_select_stmt(
                select_from=rs_orm.orm_table_name,
                select_cols=("resource_id", "digest", "size"),
                where_stmt=f"WHERE size > {self._lower_bound} AND filter_applied IS NULL",
            )

            submit_with_se = func_call_with_se(pool.submit, self._se)
            for _raw_row in rs_orm.orm_select_entries(
                _stmt=_stmt, _row_factory=sqlite3.Row
            ):
                if _raw_row[1] in self._protected_resources:
                    continue

                origin_size += _raw_row[-1]
                submit_with_se(
                    self._process_one_entry_at_thread,
                    _raw_row,  # type: ignore
                    compressed,
                ).add_done_callback(self._task_done_cb)

        return origin_size, compressed

    def _update_db(self, compressed: CompressionResult):
        rs_orm = self._db_helper.get_orm()
        with contextlib.closing(rs_orm.orm_con):
            # NOTE: DO NOT overwrite the already there resources if any!
            rs_orm.orm_insert_mappings(
                (
                    ResourceTableManifestTypedDict(
                        digest=compressed_digest,
                        size=compressed_size,
                    )
                    for compressed_digest, compressed_size in compressed.values()
                ),
                or_option="ignore",
            )

            for origin_rs_id, (compressed_digest, _) in compressed.items():
                # look up the compressed entry
                _compressed_entry = rs_orm.orm_select_entry(
                    ResourceTableManifestTypedDict(digest=compressed_digest)
                )

                # update the origin entry
                rs_orm.orm_update_entries(
                    set_values=ResourceTableManifestTypedDict(
                        filter_applied=CompressFilter(
                            resource_id=_compressed_entry.resource_id,
                            compression_alg=ZSTD_COMPRESSION_ALG,
                        )
                    ),
                    where_cols_value=ResourceTableManifestTypedDict(
                        resource_id=origin_rs_id
                    ),
                )

    def process(self) -> None:
        origin_size, _compressed = self._process_compression()
        self._update_db(_compressed)

        compressed = len(_compressed)
        compressed_size = sum(_size for _, _size in _compressed.values())
        logger.info(
            f"compression_filter: total {compressed} files are compressed"
            f"(original size: {human_readable_size(origin_size)}, "
            f"compressed size: {human_readable_size(compressed_size)})"
        )
