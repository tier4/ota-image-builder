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
"""Slice large files into smaller chunks."""

from __future__ import annotations

import _thread
import logging
import signal
import sqlite3
import threading
import typing
from concurrent.futures import Future, ThreadPoolExecutor
from hashlib import sha256
from itertools import batched, repeat
from pathlib import Path
from threading import Semaphore
from typing import Generator, Iterable, NoReturn

from ota_image_libs._resource_filter import SliceFilter
from ota_image_libs.v1.resource_table.db import ResourceTableDBHelper, ResourceTableORM
from ota_image_libs.v1.resource_table.schema import (
    ResourceTableManifestTypedDict,
)
from simple_sqlite3_orm import gen_sql_stmt

from ota_image_builder._common import (
    WriteThreadSafeList,
    func_call_with_se,
    human_readable_size,
)
from ota_image_builder._configs import cfg

from ._common import ResourceID, Sha256DigestBytes, Size

logger = logging.getLogger(__name__)

_global_interrupted = False

Sliced = tuple[ResourceID, dict[Sha256DigestBytes, Size]]
SliceResult = WriteThreadSafeList[Sliced]
"""<original_rs_id>, dict[<slice_digest>, <slice_size>]."""


def _global_shutdown_on_failed(exc: BaseException):
    global _global_interrupted
    if not _global_interrupted:
        _global_interrupted = True
        logger.error(f"failed during processing: {exc!r}, abort now!!!", exc_info=exc)
        # interrupt the main thread with a KeyBoardInterrupt
        _thread.interrupt_main(signal.SIGINT)


def _iter_slices(
    _batch: Iterable[Sliced],
) -> Generator[tuple[Sha256DigestBytes, Size]]:  # pragma: no cover
    for _sliced in _batch:
        yield from _sliced[1].items()


def _update_one_batch(rs_orm: ResourceTableORM, batch: Iterable[Sliced]) -> None:
    # NOTE: there is possibility that, one slice from file A might be
    #       the same of another slice from file B. We must handle this case!
    # NOTE: DO NOT overwrite already exists resources!
    rs_orm.orm_insert_mappings(
        (
            ResourceTableManifestTypedDict(digest=_digest, size=_size)
            for _digest, _size in _iter_slices(batch)
        ),
        or_option="ignore",
    )

    # get the mapping of slices and resource_id
    # NOTE: the len will be cfg.SLICE_UPDATE_BATCH_SIZE, which is small.
    _slices = list(_iter_slices(batch))
    # fmt: off
    slices_entries: dict[Sha256DigestBytes, ResourceID] = dict(
        rs_orm.orm_execute(
            gen_sql_stmt(
                "SELECT", "digest, resource_id",
                "FROM", rs_orm.orm_table_name,
                "WHERE", "digest", "IN", f"({','.join(repeat('?', len(_slices)))})"
            ),
            params=tuple(_slice[0] for _slice in _slices),
            row_factory=typing.cast(
                typing.Callable[..., tuple[Sha256DigestBytes, ResourceID]], sqlite3.Row
            ),
        )
    )
    # fmt: on

    # then update the sliced origin's filter_applied field
    rs_orm.orm_update_entries_many(
        set_cols=("filter_applied",),
        set_cols_value=(
            ResourceTableManifestTypedDict(
                filter_applied=SliceFilter(
                    slices=[slices_entries[_digest] for _digest in _slices],
                )
            )
            for _, _slices in batch
        ),
        where_cols=("resource_id",),
        where_cols_value=(
            ResourceTableManifestTypedDict(resource_id=_resource_id)
            for _resource_id, _ in batch
        ),
    )


class SliceFilterProcesser:
    def __init__(
        self,
        *,
        resource_dir: Path,
        rst_dbf: Path,
        slice_size: int = cfg.SLICE_SIZE,
        worker_threads: int = cfg.WORKER_THREADS,
        concurrent_tasks: int = cfg.SLICE_CONCURRENT_TASKS,
        db_update_batch_size: int = cfg.SLICE_UPDATE_BATCH_SIZE,
        protected_resources: set[Sha256DigestBytes],
    ) -> None:
        self._protected_resources = protected_resources
        self._update_batch = db_update_batch_size
        self._worker_threads = worker_threads
        self._se = Semaphore(concurrent_tasks)

        self._resource_dir = resource_dir
        self._db_helper = ResourceTableDBHelper(rst_dbf)

        self._slice_size = slice_size
        self._lower_bound = slice_size * 2
        self._last_slice_maximum_size = max_slice_size = int(slice_size * 1.5)

        self._max_slice_size = max_slice_size
        self._thread_local = threading.local()

    def _thread_worker_initializer(self) -> None:
        _thread_local = self._thread_local
        _thread_local.buffer = buffer = bytearray(self._max_slice_size)
        _thread_local.bufferview = memoryview(buffer)

    def _task_done_cb(self, fut: Future) -> None | NoReturn:
        self._se.release()  # release se right after task done
        if exc := fut.exception():
            logger.debug(f"failed during processing: {exc!r}", exc_info=exc)
            _global_shutdown_on_failed(exc)

    def _process_one_origin_at_thread(
        self,
        resource_id: ResourceID,
        entry_digest: Sha256DigestBytes,
        entry_size: Size,
        sliced: SliceResult,
    ) -> None:
        _thread_local = self._thread_local
        _buffer, _buffer_view = _thread_local.buffer, _thread_local.bufferview
        slices: dict[Sha256DigestBytes, Size] = {}

        entry_fpath = self._resource_dir / entry_digest.hex()
        with open(entry_fpath, "rb") as _entry_f:

            def _process_chunk() -> int:
                _read_len = _entry_f.readinto(_buffer)
                _slice_digest = sha256(_buffer_view[:_read_len]).digest()
                _slice_resource_fpath = self._resource_dir / _slice_digest.hex()
                _slice_resource_fpath.write_bytes(_buffer_view[:_read_len])

                slices[_slice_digest] = _read_len
                return _read_len

            while entry_size > self._last_slice_maximum_size:
                entry_size -= _process_chunk()
            _process_chunk()  # read final chunk of data
        entry_fpath.unlink(missing_ok=True)  # finally, remove the original resource
        sliced.append((resource_id, slices))

    # ------------------------ #

    def _update_db(self, sliced: SliceResult) -> None:
        with self._db_helper.get_orm() as rs_orm:
            for batch in batched(sliced, self._update_batch, strict=False):
                _update_one_batch(rs_orm, batch)

    def _process_slicing(self) -> tuple[int, Size, SliceResult]:
        sliced_count, sliced_size = 0, 0
        slice_result = SliceResult()

        with (
            self._db_helper.get_orm() as rs_orm,
            ThreadPoolExecutor(
                max_workers=self._worker_threads,
                thread_name_prefix="slice_filter",
                initializer=self._thread_worker_initializer,
            ) as pool,
        ):
            submit_with_se = func_call_with_se(pool.submit, self._se)
            # fmt: off
            for _row in rs_orm.orm_select_entries(
                _stmt=rs_orm.orm_table_spec.table_select_stmt(
                    select_from=rs_orm.orm_table_name,
                    select_cols=("resource_id", "digest", "size"),
                    where_stmt=gen_sql_stmt(
                        "WHERE","size", ">", f"{self._lower_bound}",
                        "AND", "filter_applied IS NULL",
                        end_with=None,
                    ),
                ),
                _row_factory=sqlite3.Row,
            ):
                resource_id, entry_digest, entry_size = _row
                if entry_digest in self._protected_resources:
                    continue

                sliced_count += 1
                sliced_size += entry_size

                submit_with_se(self._process_one_origin_at_thread,
                    resource_id, entry_digest, entry_size, slice_result
                ).add_done_callback(self._task_done_cb)
            # fmt: on
        return sliced_count, sliced_size, slice_result

    def process(self):
        sliced_count, sliced_size, slice_result = self._process_slicing()
        self._update_db(slice_result)

        logger.info(
            f"slice_filter: total {sliced_count} files({human_readable_size(sliced_size)}) are sliced."
        )
