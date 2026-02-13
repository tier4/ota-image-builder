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
"""Initial processing of the original system image rootfs."""

from __future__ import annotations

import _thread
import itertools
import logging
import os
import shutil
import signal
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from hashlib import sha256
from pathlib import Path
from queue import Queue
from typing import Any, NoReturn, ParamSpec, TypeVar

from ota_image_libs.common import MsgPackedDict
from ota_image_libs.v1.file_table.schema import (
    FileTableDirectories,
    FileTableInode,
    FileTableNonRegularFiles,
    FileTableRegularFiles,
    FileTableResource,
)
from ota_image_libs.v1.resource_table.schema import ResourceTableManifestTypedDict

from ota_image_builder._common import func_call_with_se
from ota_image_builder._configs import cfg
from ota_image_builder._consts import EMPTY_FILE_SHA256_BYTE

logger = logging.getLogger(__name__)

P = ParamSpec("P")
RT = TypeVar("RT")

_global_interrupted = False

EMPTY_FILE_RS_ID = 0


def _global_shutdown_on_failed(exc: BaseException):
    global _global_interrupted
    if not _global_interrupted:
        _global_interrupted = True
        logger.error(f"failed during processing: {exc!r}, abort now!!!", exc_info=exc)
        # interrupt the main thread with a KeyBoardInterrupt
        _thread.interrupt_main(signal.SIGINT)


class ResourceRegister:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._impl: dict[bytes, int] = {}
        # pre-register for the empty file
        self.register_entry(EMPTY_FILE_SHA256_BYTE)

    def register_entry(self, k: bytes) -> tuple[bool, int]:
        if v := self._impl.get(k):
            return False, v

        with self._lock:
            _next = len(self._impl)
            res = self._impl.setdefault(k, _next)
            return res == _next, res


class SystemImageProcesser:
    def __init__(
        self,
        que: Queue[Any],
        *,
        src: Path,
        resource_dir: Path,
        worker_threads: int = cfg.WORKER_THREADS,
        read_chunk_size=cfg.READ_SIZE,
        max_concurrent_tasks=cfg.INIT_PROCESS_MAX_CONCURRENT_TASKS,
        inline_threshold=cfg.INLINE_THRESHOULD,
    ) -> None:
        self._inline_threshold = inline_threshold
        self._worker_threads = worker_threads
        self._read_chunk_size = read_chunk_size
        self._que = que

        self._src = src
        self._resource_dir = resource_dir

        self._inode_count = itertools.count(start=1)
        self._resource_register = ResourceRegister()
        self._se = threading.Semaphore(max_concurrent_tasks)

    @staticmethod
    def _thread_worker_initializer(thread_local, chunksize: int) -> None:
        thread_local.buffer = buffer = bytearray(chunksize)
        thread_local.view = memoryview(buffer)

    def _process_inode(self, fpath: Path) -> int:
        f_stat = fpath.stat(follow_symlinks=False)
        xattrs = MsgPackedDict(
            {
                attrn: os.getxattr(fpath, attrn, follow_symlinks=False)
                for attrn in os.listxattr(fpath, follow_symlinks=False)
            }
        )

        # fast path for non-hardlinked entry
        # NOTE(20250814): directory is naturally hardlinked, which st_nlink is always 3.
        #                 directory is linked to `.`, `..` and itself.
        if f_stat.st_nlink == 1 or (not fpath.is_symlink() and fpath.is_dir()):
            _inode_id = next(self._inode_count)
            self._que.put_nowait(
                FileTableInode(
                    inode_id=_inode_id,
                    uid=f_stat.st_uid,
                    gid=f_stat.st_gid,
                    mode=f_stat.st_mode,
                    xattrs=xattrs or None,
                )
            )
            return _inode_id

        # only do special treatment for hardlinked entries
        # for hardlinked entry, we set the database's virtual inode to minus real_inode.
        # the database insertion mode is set to IGNORE.
        _inode_in_db = -f_stat.st_ino
        self._que.put_nowait(
            FileTableInode(
                inode_id=_inode_in_db,
                uid=f_stat.st_uid,
                gid=f_stat.st_gid,
                mode=f_stat.st_mode,
                xattrs=xattrs or None,
            )
        )
        return _inode_in_db

    def _add_entries_to_ft_resource(
        self,
        rs_idx: int,
        digest: bytes,
        size: int,
        *,
        file_contents: bytes | None = None,
    ):
        """Add this resource to file_table db ft_resource table."""
        self._que.put_nowait(
            FileTableResource(
                resource_id=rs_idx,
                digest=digest,
                size=size,
                contents=file_contents,
            )
        )

    def _add_entries_to_rst(self, digest: bytes, size: int) -> None:
        """Add the original resource entry to the resource_table.

        NOTE that the resource_id in resource_table is NOT the same as
          the resource_id in file_table_resource.
        NOTE: if the blob is already presented(which means there is an entry in resource_table
              for this blob), will not do insertion to the resource_table.
        NOTE: for inlined resources, we will not write blob to the store,
              so don't add inlined entry to resource_table.
        """
        self._que.put_nowait(ResourceTableManifestTypedDict(digest=digest, size=size))

    def _regular_file_worker(self, src_entry: Path, canonical_path: Path, thread_local):
        hash_buffer, hash_bufferview = thread_local.buffer, thread_local.view

        f_stat = src_entry.stat()
        f_size = f_stat.st_size
        if f_size == 0:  # fastpath for empty file
            resource_id = EMPTY_FILE_RS_ID

        # fastpath for small file, inline it into the database
        elif f_size <= self._inline_threshold:
            file_contents = src_entry.read_bytes()
            digest = sha256(file_contents).digest()

            # NOTE: don't need to write blob for inlined entry.
            first_resource, resource_id = self._resource_register.register_entry(digest)
            if first_resource:
                self._add_entries_to_ft_resource(
                    resource_id, digest, f_size, file_contents=file_contents
                )

        # for normal resource that we will write to the blob store
        else:
            hash_f, read_size_count = sha256(), 0
            with open(src_entry, "rb") as src_f:
                while read_size := src_f.readinto(hash_buffer):
                    hash_f.update(hash_bufferview[:read_size])
                    read_size_count += read_size
            digest = hash_f.digest()

            first_resource, resource_id = self._resource_register.register_entry(digest)
            if first_resource:
                self._add_entries_to_ft_resource(resource_id, digest, read_size_count)
                # NOTE(20260212): ALWAYS add a resource entry to the resource_table!
                #   Resources added by non-OTA-image payload like otaclient release package
                #   will not be recorded by resource table, while these files may also present
                #   in the input system image!
                #   It will not be a problem to add duplicated entry as we set IGNORE on
                #   duplicated entry insertion.
                self._add_entries_to_rst(digest, read_size_count)

                # NOTE: in case of multi-image OTA image, skip preparing
                #       resources that have already being prepared.
                dst_f = self._resource_dir / digest.hex()
                if not dst_f.is_file():
                    shutil.copyfile(src_entry, dst_f, follow_symlinks=False)

        self._que.put_nowait(
            FileTableRegularFiles(
                path=str(canonical_path),
                inode_id=self._process_inode(src_entry),
                resource_id=resource_id,
            )
        )

    def _directory_worker(self, entry: Path, canonical_path: Path):
        self._que.put_nowait(
            FileTableDirectories(
                path=str(canonical_path),
                inode_id=self._process_inode(entry),
            )
        )

    def _symlink_worker(self, entry: Path, canonical_path: Path):
        self._que.put_nowait(
            FileTableNonRegularFiles(
                path=str(canonical_path),
                inode_id=self._process_inode(entry),
                meta=os.readlink(entry).encode("utf-8"),
            )
        )

    def _char_worker(self, entry: Path, canonical_path: Path):
        src_stat = entry.stat()
        major, minor = os.major(src_stat.st_rdev), os.minor(src_stat.st_rdev)

        # NOTE: for chardev, we only support overlayfs whiteout file, which has
        #       major and minor both are 0.
        if not (major == 0 and minor == 0):
            return

        self._que.put_nowait(
            FileTableNonRegularFiles(
                path=str(canonical_path),
                inode_id=self._process_inode(entry),
            )
        )

    def _task_done_cb(self, fut: Future) -> None | NoReturn:
        self._se.release()  # release se right after task done
        if exc := fut.exception():
            logger.debug(f"failed during processing: {exc!r}", exc_info=exc)
            _global_shutdown_on_failed(exc)

    def process_sysimg_src(self):
        thread_local = threading.local()
        CANONICAL_ROOT_P = Path("/")

        # NOTE: add empty entry in ft_resource table for fastpath processing empty file
        self._add_entries_to_ft_resource(
            EMPTY_FILE_RS_ID, EMPTY_FILE_SHA256_BYTE, 0, file_contents=b""
        )

        with ThreadPoolExecutor(
            max_workers=self._worker_threads,
            thread_name_prefix="ota_image_sysimg_processer",
            # initialize buffer at thread worker starts up
            initializer=partial(
                self._thread_worker_initializer,
                thread_local,
                self._read_chunk_size,
            ),
        ) as worker_pool:
            submit_with_se = func_call_with_se(worker_pool.submit, self._se)
            for curdir, dirnames, filenames in os.walk(
                self._src, topdown=True, followlinks=False
            ):
                curdir_path = Path(curdir)
                canonical_curdir = CANONICAL_ROOT_P / curdir_path.relative_to(self._src)

                # NOTE: submit_with_se will block if se is exhausted
                submit_with_se(
                    self._directory_worker,
                    curdir_path,
                    canonical_curdir,
                ).add_done_callback(self._task_done_cb)

                # NOTE: symlinks point to dir will also be listed in dirnames!
                # NOTE: eventually we will access all dirs, so no need to process subdirs.
                for _dirn in dirnames:
                    _dirpath = curdir_path / _dirn
                    if _dirpath.is_symlink():
                        _canonical_dirpath = canonical_curdir / _dirn
                        submit_with_se(
                            self._symlink_worker,
                            _dirpath,
                            _canonical_dirpath,
                        ).add_done_callback(self._task_done_cb)

                for _fn in filenames:
                    _fpath = curdir_path / _fn
                    _canonical_fpath = canonical_curdir / _fn
                    # NOTE: always check is_symlink first!
                    if _fpath.is_symlink():
                        submit_with_se(
                            self._symlink_worker, _fpath, _canonical_fpath
                        ).add_done_callback(self._task_done_cb)

                    elif _fpath.is_file():
                        submit_with_se(
                            self._regular_file_worker,
                            _fpath,
                            _canonical_fpath,
                            thread_local,
                        ).add_done_callback(self._task_done_cb)

                    elif _fpath.is_char_device():
                        submit_with_se(
                            self._char_worker, _fpath, _canonical_fpath
                        ).add_done_callback(self._task_done_cb)

            logger.info("all entries are dispatched, waiting for finish")

        logger.info("system image process finished!")
        self._que.put(None)
