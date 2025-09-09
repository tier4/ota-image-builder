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
import contextlib
import logging
import signal
import sqlite3
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
from itertools import count
from pathlib import Path
from queue import Queue
from threading import Semaphore
from typing import NoReturn

from ota_image_libs._resource_filter import SliceFilter
from ota_image_libs.v1.resource_table.db import ResourceTableDBHelper, ResourceTableORM
from ota_image_libs.v1.resource_table.schema import (
    ResourceTableManifest,
    ResourceTableManifestTypedDict,
)
from simple_sqlite3_orm import gen_sql_stmt

from ota_image_builder._common import func_call_with_se, human_readable_size
from ota_image_builder._configs import cfg

from ._db_utils import count_entries_in_table

logger = logging.getLogger(__name__)

_global_interrupted = False


def _global_shutdown_on_failed(exc: BaseException):
    global _global_interrupted
    if not _global_interrupted:
        _global_interrupted = True
        logger.error(f"failed during processing: {exc!r}, abort now!!!", exc_info=exc)
        # interrupt the main thread with a KeyBoardInterrupt
        _thread.interrupt_main(signal.SIGINT)


def _count(counter: count, times: int) -> list[int]:
    """Count a itertools.count instance <times> times,
    return the list of counted numbers."""
    return [_c for _, _c in zip(range(times), counter, strict=False)]


def _update_one_batch(
    rs_orm: ResourceTableORM,
    batch: list[tuple[int, dict[bytes, int]]],
    counter: count,
) -> count:
    _slice_counter = deepcopy(counter)
    _update_counter = deepcopy(counter)

    # first insert new slice entries
    # NOTE: there is possibility that, one slice from file A might be
    #       the same of another slice from file B. With this consideration,
    #       `digest` is not expected to be unique in resource_table.
    #       But in blob storage, deduplication will still work.
    def slices_batch():
        for _entry in batch:
            yield from _entry[1].items()

    rs_orm.orm_insert_entries(
        (
            ResourceTableManifest(resource_id=_rs_id, digest=_digest, size=_size)
            for _rs_id, (_digest, _size) in zip(
                _slice_counter, slices_batch(), strict=False
            )
        )
    )

    # then update the sliced origin's filter_applied field
    rs_orm.orm_update_entries_many(
        set_cols=("filter_applied",),
        set_cols_value=(
            ResourceTableManifestTypedDict(
                filter_applied=SliceFilter(
                    slices=_count(_update_counter, len(_slices)),
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
    return _update_counter


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
    ) -> None:
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

        # <resource_id>: <slice_digest>,<slice_size>
        self._sliced: Queue[tuple[int, dict[bytes, int]] | None] = Queue()

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
        self, resource_id: int, entry_digest: bytes, entry_size: int
    ):
        _thread_local = self._thread_local
        _buffer, _buffer_view = _thread_local.buffer, _thread_local.bufferview
        slices: dict[bytes, int] = {}

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
        self._sliced.put_nowait((resource_id, slices))

    def _update_db(self, rs_orm: ResourceTableORM, rs_id_start: int):
        rs_id_counter = count(start=rs_id_start)

        batch: list[tuple[int, dict[bytes, int]]] = []
        while _entry := self._sliced.get_nowait():
            batch.append(_entry)
            if len(batch) > self._update_batch:
                rs_id_counter = _update_one_batch(rs_orm, batch, rs_id_counter)
                batch.clear()
        if batch:
            _update_one_batch(rs_orm, batch, rs_id_counter)

    def process(self):
        rs_orm = self._db_helper.get_orm()
        sliced_count, sliced_size = 0, 0
        with contextlib.closing(rs_orm.orm_con):
            _table_name = rs_orm.orm_table_name
            # NOTE: resource_id starts from 1
            rs_id_start = count_entries_in_table(rs_orm) + 1

            # ------ step1: slicing large files ------ #
            # fmt: off
            with ThreadPoolExecutor(
                max_workers=self._worker_threads,
                thread_name_prefix="slice_filter",
                initializer=self._thread_worker_initializer,
            ) as pool:
                submit_with_se = func_call_with_se(pool.submit, self._se)
                for _row in rs_orm.orm_select_entries(
                    _stmt=rs_orm.orm_table_spec.table_select_stmt(
                        select_from=_table_name,
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
                    sliced_count += 1
                    sliced_size += entry_size

                    submit_with_se(self._process_one_origin_at_thread,
                        resource_id, entry_digest, entry_size
                    ).add_done_callback(self._task_done_cb)
            # fmt: on
            self._sliced.put_nowait(None)

            # ------ step2: update resource_table database ------ #
            self._update_db(rs_orm, rs_id_start)

        logger.info(
            f"slice_filter: total {sliced_count} files({human_readable_size(sliced_size)}) are sliced."
        )
