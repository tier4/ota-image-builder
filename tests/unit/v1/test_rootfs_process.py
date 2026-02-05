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
"""Unit tests for v1/_resource_process/_rootfs_process.py module."""

from __future__ import annotations

import threading
from pathlib import Path
from queue import Queue
from unittest.mock import patch

from ota_image_builder._consts import EMPTY_FILE_SHA256_BYTE
from ota_image_builder.v1._resource_process._rootfs_process import (
    EMPTY_FILE_RS_ID,
    ResourceRegister,
    SystemImageProcesser,
    _global_shutdown_on_failed,
)


class TestResourceRegister:
    """Tests for ResourceRegister class."""

    def test_init_preregisters_empty_file(self):
        """Test that empty file is pre-registered on init."""
        register = ResourceRegister()

        # Empty file should already be registered with ID 0
        is_new, rs_id = register.register_entry(EMPTY_FILE_SHA256_BYTE)

        assert is_new is False
        assert rs_id == EMPTY_FILE_RS_ID

    def test_register_new_entry(self):
        """Test registering a new entry."""
        register = ResourceRegister()

        test_digest = b"test_digest_that_is_unique_123"
        is_new, rs_id = register.register_entry(test_digest)

        assert is_new is True
        assert rs_id == 1  # First entry after empty file (0)

    def test_register_existing_entry_returns_false(self):
        """Test that registering existing entry returns False."""
        register = ResourceRegister()

        test_digest = b"test_digest_123"

        # First registration
        is_new1, rs_id1 = register.register_entry(test_digest)
        assert is_new1 is True

        # Second registration of same digest
        is_new2, rs_id2 = register.register_entry(test_digest)
        assert is_new2 is False
        assert rs_id2 == rs_id1

    def test_multiple_unique_entries(self):
        """Test registering multiple unique entries."""
        register = ResourceRegister()

        digests = [b"digest1", b"digest2", b"digest3"]
        for i, digest in enumerate(digests, start=1):
            is_new, rs_id = register.register_entry(digest)
            assert is_new is True
            assert rs_id == i

    def test_thread_safety(self):
        """Test that registration is thread-safe."""
        import threading

        register = ResourceRegister()
        results = []

        def register_entry(digest):
            is_new, rs_id = register.register_entry(digest)
            results.append((digest, is_new, rs_id))

        threads = []
        for i in range(10):
            t = threading.Thread(target=register_entry, args=(f"digest_{i}".encode(),))
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All entries should have unique IDs
        rs_ids = [r[2] for r in results]
        assert len(set(rs_ids)) == 10


class TestGlobalShutdownOnFailed:
    """Tests for _global_shutdown_on_failed function."""

    def test_sets_global_interrupted_and_interrupts_main(self):
        """Test that the function sets global flag and interrupts main thread."""
        import ota_image_builder.v1._resource_process._rootfs_process as rp_module

        # Reset global state
        rp_module._global_interrupted = False

        with patch.object(rp_module, "_thread") as mock_thread:
            _global_shutdown_on_failed(Exception("test error"))

            assert rp_module._global_interrupted is True
            mock_thread.interrupt_main.assert_called_once()

        # Reset for other tests
        rp_module._global_interrupted = False

    def test_only_interrupts_once(self):
        """Test that interrupt_main is only called once."""
        import ota_image_builder.v1._resource_process._rootfs_process as rp_module

        # Set as already interrupted
        rp_module._global_interrupted = True

        with patch.object(rp_module, "_thread") as mock_thread:
            _global_shutdown_on_failed(Exception("test error"))

            # Should not call interrupt_main again
            mock_thread.interrupt_main.assert_not_called()

        # Reset for other tests
        rp_module._global_interrupted = False


class TestSystemImageProcesser:
    """Tests for SystemImageProcesser class."""

    def test_init(self, tmp_path: Path):
        """Test SystemImageProcesser initialization."""
        que = Queue()
        src = tmp_path / "src"
        src.mkdir()
        resource_dir = tmp_path / "resources"
        resource_dir.mkdir()

        processor = SystemImageProcesser(
            que,
            src=src,
            resource_dir=resource_dir,
            worker_threads=2,
            read_chunk_size=4096,
            max_concurrent_tasks=4,
            inline_threshold=1024,
        )

        assert processor._src == src
        assert processor._resource_dir == resource_dir
        assert processor._worker_threads == 2
        assert processor._read_chunk_size == 4096
        assert processor._inline_threshold == 1024

    def test_thread_worker_initializer(self):
        """Test that thread worker initializer sets up buffer."""
        thread_local = threading.local()
        chunksize = 8192

        SystemImageProcesser._thread_worker_initializer(thread_local, chunksize)

        assert hasattr(thread_local, "buffer")
        assert hasattr(thread_local, "view")
        assert isinstance(thread_local.buffer, bytearray)
        assert len(thread_local.buffer) == chunksize

    def test_resource_register_created(self, tmp_path: Path):
        """Test that resource register is created with empty file pre-registered."""
        que = Queue()
        src = tmp_path / "src"
        src.mkdir()
        resource_dir = tmp_path / "resources"
        resource_dir.mkdir()

        processor = SystemImageProcesser(
            que,
            src=src,
            resource_dir=resource_dir,
        )

        # Empty file should be pre-registered
        is_new, rs_id = processor._resource_register.register_entry(
            EMPTY_FILE_SHA256_BYTE
        )
        assert is_new is False
        assert rs_id == EMPTY_FILE_RS_ID

    def test_default_values(self, tmp_path: Path):
        """Test default values are used when not specified."""
        from ota_image_builder._configs import cfg

        que = Queue()
        src = tmp_path / "src"
        src.mkdir()
        resource_dir = tmp_path / "resources"
        resource_dir.mkdir()

        processor = SystemImageProcesser(
            que,
            src=src,
            resource_dir=resource_dir,
        )

        assert processor._worker_threads == cfg.WORKER_THREADS
        assert processor._read_chunk_size == cfg.READ_SIZE
        assert processor._inline_threshold == cfg.INLINE_THRESHOULD

    def test_inode_count_starts_at_one(self, tmp_path: Path):
        """Test that inode count starts at 1."""
        que = Queue()
        src = tmp_path / "src"
        src.mkdir()
        resource_dir = tmp_path / "resources"
        resource_dir.mkdir()

        processor = SystemImageProcesser(
            que,
            src=src,
            resource_dir=resource_dir,
        )

        # The inode count should start at 1
        first_inode = next(processor._inode_count)
        assert first_inode == 1

    def test_semaphore_created(self, tmp_path: Path):
        """Test that semaphore is created with max concurrent tasks."""
        que = Queue()
        src = tmp_path / "src"
        src.mkdir()
        resource_dir = tmp_path / "resources"
        resource_dir.mkdir()

        max_tasks = 5
        processor = SystemImageProcesser(
            que,
            src=src,
            resource_dir=resource_dir,
            max_concurrent_tasks=max_tasks,
        )

        # Should be able to acquire max_tasks times
        for _ in range(max_tasks):
            assert processor._se.acquire(blocking=False) is True

        # Should not be able to acquire anymore
        assert processor._se.acquire(blocking=False) is False
