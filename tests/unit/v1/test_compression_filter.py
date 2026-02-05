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
"""Unit tests for v1/_resource_process/_compression_filter.py module."""

from __future__ import annotations

from concurrent.futures import Future
from hashlib import sha256
from pathlib import Path
from unittest.mock import MagicMock, patch

import zstandard

from ota_image_builder.v1._resource_process._compression_filter import (
    CompressionFilterProcesser,
)


class TestCompressionFilterProcesser:
    """Tests for CompressionFilterProcesser class."""

    def test_init(self, tmp_path: Path):
        """Test CompressionFilterProcesser initialization."""
        resource_dir = tmp_path / "resources"
        resource_dir.mkdir()
        rst_dbf = tmp_path / "resource_table.db"

        # Create a minimal database file
        import sqlite3

        conn = sqlite3.connect(rst_dbf)
        conn.close()

        processor = CompressionFilterProcesser(
            resource_dir=resource_dir,
            rst_dbf=rst_dbf,
            size_lower_bound=1024,
            compression_ratio_threshold=1.1,
            zstd_compression_level=3,
            read_size=4096,
            worker_threads=2,
            concurrent_jobs=4,
            protected_resources=set(),
        )

        assert processor._resource_dir == resource_dir
        assert processor._lower_bound == 1024
        assert processor._compression_ratio_threshold == 1.1
        assert processor._zstd_compression_level == 3
        assert processor._worker_threads == 2

    def test_thread_worker_initializer(self, tmp_path: Path):
        """Test that thread worker initializer sets up zstd compressor."""
        resource_dir = tmp_path / "resources"
        resource_dir.mkdir()
        rst_dbf = tmp_path / "resource_table.db"

        import sqlite3

        conn = sqlite3.connect(rst_dbf)
        conn.close()

        processor = CompressionFilterProcesser(
            resource_dir=resource_dir,
            rst_dbf=rst_dbf,
            protected_resources=set(),
        )

        processor._thread_worker_initializer()

        assert hasattr(processor._worker_thread_local, "cctx")
        assert isinstance(processor._worker_thread_local.cctx, zstandard.ZstdCompressor)

    def test_do_compression_at_thread(self, tmp_path: Path):
        """Test compression of a file."""
        resource_dir = tmp_path / "resources"
        resource_dir.mkdir()
        rst_dbf = tmp_path / "resource_table.db"

        import sqlite3

        conn = sqlite3.connect(rst_dbf)
        conn.close()

        processor = CompressionFilterProcesser(
            resource_dir=resource_dir,
            rst_dbf=rst_dbf,
            protected_resources=set(),
        )

        # Initialize the thread local compressor
        processor._thread_worker_initializer()

        # Create a test file with compressible content
        src_file = tmp_path / "src.txt"
        src_file.write_text("A" * 10000)  # Highly compressible
        dst_file = tmp_path / "dst.zst"

        digest, size = processor._do_compression_at_thread(src_file, dst_file)

        assert dst_file.exists()
        assert size > 0
        assert size < 10000  # Should be smaller after compression
        assert len(digest) == 32  # SHA256 digest

    def test_task_done_cb_releases_semaphore(self, tmp_path: Path):
        """Test that task_done_cb releases semaphore."""
        resource_dir = tmp_path / "resources"
        resource_dir.mkdir()
        rst_dbf = tmp_path / "resource_table.db"

        import sqlite3

        conn = sqlite3.connect(rst_dbf)
        conn.close()

        processor = CompressionFilterProcesser(
            resource_dir=resource_dir,
            rst_dbf=rst_dbf,
            concurrent_jobs=1,
            protected_resources=set(),
        )

        # Acquire the semaphore
        processor._se.acquire()

        # Create a mock future with no exception
        mock_future = MagicMock(spec=Future)
        mock_future.exception.return_value = None

        processor._task_done_cb(mock_future)

        # Semaphore should be released (can acquire again)
        assert processor._se.acquire(blocking=False) is True

    def test_task_done_cb_handles_exception(self, tmp_path: Path):
        """Test that task_done_cb handles exceptions and triggers shutdown."""
        import ota_image_builder.v1._resource_process._compression_filter as cf_module

        resource_dir = tmp_path / "resources"
        resource_dir.mkdir()
        rst_dbf = tmp_path / "resource_table.db"

        import sqlite3

        conn = sqlite3.connect(rst_dbf)
        conn.close()

        # Reset global state
        cf_module._global_shutdown = False

        processor = CompressionFilterProcesser(
            resource_dir=resource_dir,
            rst_dbf=rst_dbf,
            concurrent_jobs=1,
            protected_resources=set(),
        )

        # Acquire the semaphore
        processor._se.acquire()

        # Create a mock future with an exception
        mock_future = MagicMock(spec=Future)
        mock_future.exception.return_value = Exception("test error")

        with patch.object(cf_module, "_thread") as mock_thread:
            processor._task_done_cb(mock_future)

            # Should set global shutdown and interrupt main
            assert cf_module._global_shutdown is True
            mock_thread.interrupt_main.assert_called_once()

        # Reset global state
        cf_module._global_shutdown = False

    def test_process_one_entry_compression_below_threshold(self, tmp_path: Path):
        """Test that files not meeting compression ratio are not replaced."""
        resource_dir = tmp_path / "resources"
        resource_dir.mkdir()
        rst_dbf = tmp_path / "resource_table.db"

        import sqlite3

        conn = sqlite3.connect(rst_dbf)
        conn.close()

        processor = CompressionFilterProcesser(
            resource_dir=resource_dir,
            rst_dbf=rst_dbf,
            compression_ratio_threshold=100.0,  # Very high threshold
            protected_resources=set(),
        )

        # Initialize thread local
        processor._thread_worker_initializer()

        # Create a test resource file with actual content and digest
        content = b"A" * 1000
        test_digest = sha256(content).digest()
        test_file = resource_dir / test_digest.hex()
        test_file.write_bytes(content)

        # Process the entry
        processor._process_one_entry_at_thread((1, test_digest, 1000))

        # Original file should still exist (compression ratio not met)
        assert test_file.exists()
        # Should not be in compressed dict
        assert 1 not in processor._compressed

    def test_process_one_entry_compression_meets_threshold(self, tmp_path: Path):
        """Test that files meeting compression ratio are replaced."""
        resource_dir = tmp_path / "resources"
        resource_dir.mkdir()
        rst_dbf = tmp_path / "resource_table.db"

        import sqlite3

        conn = sqlite3.connect(rst_dbf)
        conn.close()

        processor = CompressionFilterProcesser(
            resource_dir=resource_dir,
            rst_dbf=rst_dbf,
            compression_ratio_threshold=1.1,  # Low threshold
            protected_resources=set(),
        )

        # Initialize thread local
        processor._thread_worker_initializer()

        # Create a highly compressible test resource file with actual digest
        content = b"A" * 100000  # Very compressible
        test_digest = sha256(content).digest()
        test_file = resource_dir / test_digest.hex()
        test_file.write_bytes(content)

        # Process the entry
        processor._process_one_entry_at_thread((1, test_digest, 100000))

        # Original file should be removed
        assert not test_file.exists()
        # Should be in compressed dict
        assert 1 in processor._compressed
