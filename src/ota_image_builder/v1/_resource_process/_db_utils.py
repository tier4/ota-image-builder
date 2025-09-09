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

import _thread
import logging
import signal
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from queue import Queue
from typing import Any

from ota_image_libs.v1.file_table import FT_REGULAR_TABLE_NAME, FT_RESOURCE_TABLE_NAME
from ota_image_libs.v1.file_table.db import FileTableDBHelper
from ota_image_libs.v1.file_table.schema import (
    FileTableDirectories,
    FileTableInode,
    FileTableNonRegularFiles,
    FileTableRegularFiles,
    FileTableResource,
)
from ota_image_libs.v1.resource_table.db import ResourceTableDBHelper
from pydantic import BaseModel
from simple_sqlite3_orm import ORMBase, gen_sql_stmt

from ota_image_builder._configs import cfg

logger = logging.getLogger(__name__)

_global_interrupted = False


def _global_shutdown_on_failed(exc: BaseException):
    global _global_interrupted
    if not _global_interrupted:
        _global_interrupted = True
        logger.error(f"failed during processing: {exc!r}, abort now!!!", exc_info=exc)
        # interrupt the main thread with a KeyBoardInterrupt
        _thread.interrupt_main(signal.SIGINT)


def _set_db_journal_mode(conn: sqlite3.Connection, mode: str):
    """Set the journal mode for the SQLite connection."""
    try:
        conn.execute(f"PRAGMA journal_mode={mode};")
    except sqlite3.OperationalError as e:
        logger.error(f"Failed to set journal mode to {mode}: {e}")
        raise


def vacuum_db(db_f: Path):
    with closing(sqlite3.connect(db_f)) as conn:
        logger.debug(f"issue VACUUM on {db_f} ...")
        conn.execute("VACUUM;")


def count_entries_in_table(orm: ORMBase, where_stmt: str | None = None) -> int:
    """Issue a SELECT COUNT query on a database table."""
    # fmt: off
    query = gen_sql_stmt(
        "SELECT", "COUNT(*)", "FROM", orm.orm_table_name,
        where_stmt if where_stmt else ""
    )
    # fmt: on
    _cur = orm.orm_execute(query, row_factory=None)
    _entries = _cur[0][0]
    logger.info(f"{_entries=} in {orm.orm_table_name}")
    return _entries


def get_total_entries_size_in_table(orm: ORMBase, where_stmt: str | None = None) -> int:
    """Issue a SELECT SUM query on table that has `size` field."""
    # fmt: off
    query = gen_sql_stmt(
        "SELECT", "SUM(size)", "FROM", orm.orm_table_name,
        where_stmt if where_stmt else ""
    )
    # fmt: on
    _cur = orm.orm_execute(query, row_factory=None)
    return _cur[0][0]


def init_file_table_db(ft_dbf: Path) -> FileTableDBHelper:
    """Initialize the file table database."""
    file_table_db = FileTableDBHelper(ft_dbf)
    file_table_db.bootstrap_db()
    return file_table_db


def init_resource_table_db(rst_dbf: Path) -> ResourceTableDBHelper:
    """Initialize the resource table database."""
    resource_table_db = ResourceTableDBHelper(rst_dbf)
    resource_table_db.bootstrap_db()
    return resource_table_db


class ImageStats(BaseModel):
    # NOTE: also see ImageManifestAnnotations and ImageAnnotations
    image_blobs_count: int = 0
    image_blobs_size: int = 0
    sys_image_size: int = 0
    sys_image_regular_files_count: int = 0
    sys_image_non_regular_files_count: int = 0
    sys_image_dirs_count: int = 0
    sys_image_unique_file_entries: int = 0
    sys_image_unique_file_entries_size: int = 0


class _FileTableDBInsertHelper:
    def __init__(
        self, orm: ORMBase, batch_size: int = cfg.INIT_PROCESS_BATCH_WRITE_SIZE
    ):
        self._orm = orm
        self._batch_size = batch_size
        self._store = []

    # NOTE: for handling hardlinked files, use `ignore` option.

    def insert(self, entry: Any):
        self._store.append(entry)
        if len(self._store) > self._batch_size:
            self._orm.orm_insert_entries(self._store, or_option="ignore")
            self._store.clear()

    def finalize(self):
        if self._store:
            self._orm.orm_insert_entries(self._store, or_option="ignore")
            self._store.clear()


class _ResourceDBInsertMappingsHelper:
    def __init__(
        self, orm: ORMBase, batch_size: int = cfg.INIT_PROCESS_BATCH_WRITE_SIZE
    ):
        self._orm = orm
        self._batch_size = batch_size
        self._store = []

    # NOTE: the caller will ensure there is not duplicated entries to be inserted.

    def insert_mappings(self, entry: Any):
        self._store.append(entry)
        if len(self._store) > self._batch_size:
            self._orm.orm_insert_mappings(self._store)
            self._store.clear()

    def finalize_mappings(self):
        if self._store:
            self._orm.orm_insert_mappings(self._store)
            self._store.clear()


class DataBaseBuilder:
    def __init__(self, ft_dbf: Path, rst_dbf: Path, que: Queue) -> None:
        self._que = que
        self._ft_db_helper = FileTableDBHelper(ft_dbf)
        self._rst_db_helper = ResourceTableDBHelper(rst_dbf)

        self._stats = ImageStats()

    def _worker_thread(self) -> None:
        _ft_helper = self._ft_db_helper
        _rst_helper = self._rst_db_helper
        try:
            with (
                closing(_ft_helper.connect_fstable_db()) as ft_conn,
                closing(_rst_helper.connect_rstable_db()) as rst_conn,
            ):
                # fmt: off
                _ft_regular_insert = _FileTableDBInsertHelper(_ft_helper.get_regular_file_orm(ft_conn))
                _ft_non_regular_insert = _FileTableDBInsertHelper(_ft_helper.get_non_regular_file_orm(ft_conn))
                _ft_dir_insert = _FileTableDBInsertHelper(_ft_helper.get_dir_orm(ft_conn))
                _ft_inode_insert = _FileTableDBInsertHelper(_ft_helper.get_inode_orm(ft_conn))
                _ft_resource_insert = _FileTableDBInsertHelper(_ft_helper.get_resource_orm(ft_conn))
                _rst_insert = _ResourceDBInsertMappingsHelper(_rst_helper.get_orm(rst_conn))
                # fmt: on
                while item := self._que.get():
                    match item:
                        case FileTableDirectories():
                            _ft_dir_insert.insert(item)
                        case FileTableNonRegularFiles():
                            _ft_non_regular_insert.insert(item)
                        case FileTableRegularFiles():
                            _ft_regular_insert.insert(item)
                        case FileTableInode():
                            _ft_inode_insert.insert(item)
                        case FileTableResource():
                            _ft_resource_insert.insert(item)
                        # fallback for ResourceTableManifestTypedDict, as we only use typed dict for
                        #   inserting resource table entries.
                        case dict():
                            _rst_insert.insert_mappings(item)

                _ft_regular_insert.finalize()
                _ft_non_regular_insert.finalize()
                _ft_dir_insert.finalize()
                _ft_inode_insert.finalize()
                _ft_resource_insert.finalize()
                _rst_insert.finalize_mappings()
            logger.info("database builder: finish and exit")
        except Exception as e:
            logger.exception(f"database builder: exits on exception: {e}")
            _global_shutdown_on_failed(e)
        finally:
            self._finished = True

    def start_builder_thread(self) -> threading.Thread:
        t = threading.Thread(
            target=self._worker_thread, name="ota_image_builder", daemon=True
        )
        t.start()
        return t

    def finalize_db_build(self) -> None:
        # set journal mode to DELETE for both databases for distribution
        logger.debug("finalizing database build, setting journal mode to DELETE")
        with closing(self._ft_db_helper.connect_fstable_db()) as _fst_conn:
            _set_db_journal_mode(_fst_conn, "DELETE")
        with closing(self._rst_db_helper.connect_rstable_db()) as _rst_conn:
            _set_db_journal_mode(_rst_conn, "DELETE")


class ImageStatsQuery:
    """Get the statistics of this system image after processing.

    NOTE that this should be called after all filters are applied.
    """

    def __init__(self, ft_dbf: Path, rst_dbf: Path) -> None:
        self._ft_db_helper = FileTableDBHelper(ft_dbf)

    def _cal_sys_image_size(self) -> int:
        with closing(self._ft_db_helper.connect_fstable_db()) as conn:
            _res = conn.execute(
                f"SELECT SUM(size) FROM {FT_REGULAR_TABLE_NAME} JOIN {FT_RESOURCE_TABLE_NAME} USING(resource_id)"
            ).fetchone()
            return _res[0]

    def get_stats_after_process(self):
        with (
            closing(self._ft_db_helper.connect_fstable_db()) as ft_conn,
        ):
            ft_regular_orm = self._ft_db_helper.get_regular_file_orm(ft_conn)
            ft_dir_orm = self._ft_db_helper.get_dir_orm(ft_conn)
            ft_non_regular_orm = self._ft_db_helper.get_non_regular_file_orm(ft_conn)
            ft_resource_orm = self._ft_db_helper.get_resource_orm(ft_conn)

            # fmt: off
            return ImageStats(
                # NOTE: entry that is not inlined will be one blob in the blob storage.
                image_blobs_count=count_entries_in_table(ft_resource_orm, "WHERE contents IS NULL"),
                image_blobs_size=get_total_entries_size_in_table(ft_resource_orm, "WHERE contents IS NULL"),
                sys_image_size =self._cal_sys_image_size(),
                sys_image_regular_files_count=count_entries_in_table(ft_regular_orm),
                sys_image_non_regular_files_count=count_entries_in_table(ft_non_regular_orm),
                sys_image_dirs_count=count_entries_in_table(ft_dir_orm),
                sys_image_unique_file_entries=count_entries_in_table(ft_resource_orm),
                sys_image_unique_file_entries_size=get_total_entries_size_in_table(ft_resource_orm),
            )
