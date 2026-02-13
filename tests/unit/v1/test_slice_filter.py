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
"""Unit tests for v1/_resource_process/_slice_filter.py module."""

from __future__ import annotations

import sqlite3
from concurrent.futures import Future
from hashlib import sha256
from pathlib import Path

import ota_image_builder.v1._resource_process._slice_filter as sf_module
from ota_image_builder.v1._resource_process._slice_filter import (
    SliceFilterProcesser,
    _global_shutdown_on_failed,
    _iter_slices,
    _update_one_batch,
)


class TestIterSlices:
    """Tests for _iter_slices helper function."""

    def test_single_entry(self):
        """Test iterating slices from a single batch entry."""
        digest1 = sha256(b"slice1").digest()
        digest2 = sha256(b"slice2").digest()
        batch = [(1, {digest1: 100, digest2: 200})]

        result = list(_iter_slices(batch))

        assert len(result) == 2
        assert (digest1, 100) in result
        assert (digest2, 200) in result

    def test_multiple_entries(self):
        """Test iterating slices from multiple batch entries."""
        digest1 = sha256(b"slice1").digest()
        digest2 = sha256(b"slice2").digest()
        digest3 = sha256(b"slice3").digest()
        batch = [
            (1, {digest1: 100, digest2: 200}),
            (2, {digest3: 300}),
        ]

        result = list(_iter_slices(batch))

        assert len(result) == 3
        assert (digest1, 100) in result
        assert (digest2, 200) in result
        assert (digest3, 300) in result

    def test_empty_batch(self):
        """Test iterating slices from an empty batch."""
        result = list(_iter_slices([]))
        assert result == []


class TestGlobalShutdownOnFailed:
    """Tests for _global_shutdown_on_failed function."""

    def test_sets_global_interrupted_and_interrupts_main(self, mocker):
        """Test that the function sets global flag and interrupts main thread."""

        # Reset global state
        sf_module._global_interrupted = False

        mock_thread = mocker.patch.object(sf_module, "_thread")
        _global_shutdown_on_failed(Exception("test error"))

        assert sf_module._global_interrupted is True
        mock_thread.interrupt_main.assert_called_once()

        # Reset for other tests
        sf_module._global_interrupted = False

    def test_only_interrupts_once(self, mocker):
        """Test that interrupt_main is only called once."""
        # Set as already interrupted
        sf_module._global_interrupted = True

        mock_thread = mocker.patch.object(sf_module, "_thread")
        _global_shutdown_on_failed(Exception("test error"))

        # Should not call interrupt_main again
        mock_thread.interrupt_main.assert_not_called()

        # Reset for other tests
        sf_module._global_interrupted = False


class TestSliceFilterProcesser:
    """Tests for SliceFilterProcesser class."""

    def test_init(self, tmp_path: Path):
        """Test SliceFilterProcesser initialization."""
        resource_dir = tmp_path / "resources"
        resource_dir.mkdir()
        rst_dbf = tmp_path / "resource_table.db"

        conn = sqlite3.connect(rst_dbf)
        conn.close()

        processor = SliceFilterProcesser(
            resource_dir=resource_dir,
            rst_dbf=rst_dbf,
            slice_size=1024,
            worker_threads=2,
            concurrent_tasks=4,
            db_update_batch_size=100,
            protected_resources=set(),
        )

        assert processor._resource_dir == resource_dir
        assert processor._slice_size == 1024
        assert processor._worker_threads == 2
        assert processor._update_batch == 100

    def test_thread_worker_initializer(self, tmp_path: Path):
        """Test that thread worker initializer sets up buffer."""
        resource_dir = tmp_path / "resources"
        resource_dir.mkdir()
        rst_dbf = tmp_path / "resource_table.db"

        conn = sqlite3.connect(rst_dbf)
        conn.close()

        processor = SliceFilterProcesser(
            resource_dir=resource_dir,
            rst_dbf=rst_dbf,
            slice_size=1024,
            protected_resources=set(),
        )

        processor._thread_worker_initializer()

        assert hasattr(processor._thread_local, "buffer")
        assert hasattr(processor._thread_local, "bufferview")
        assert isinstance(processor._thread_local.buffer, bytearray)

    def test_task_done_cb_releases_semaphore(self, tmp_path: Path, mocker):
        """Test that task_done_cb releases semaphore."""
        resource_dir = tmp_path / "resources"
        resource_dir.mkdir()
        rst_dbf = tmp_path / "resource_table.db"

        conn = sqlite3.connect(rst_dbf)
        conn.close()

        processor = SliceFilterProcesser(
            resource_dir=resource_dir,
            rst_dbf=rst_dbf,
            concurrent_tasks=1,
            protected_resources=set(),
        )

        # Acquire the semaphore
        processor._se.acquire()

        # Create a mock future with no exception
        mock_future = mocker.MagicMock(spec=Future)
        mock_future.exception.return_value = None

        processor._task_done_cb(mock_future)

        # Semaphore should be released
        assert processor._se.acquire(blocking=False) is True

    def test_task_done_cb_handles_exception(self, tmp_path: Path, mocker):
        """Test that task_done_cb handles exceptions."""
        resource_dir = tmp_path / "resources"
        resource_dir.mkdir()
        rst_dbf = tmp_path / "resource_table.db"

        conn = sqlite3.connect(rst_dbf)
        conn.close()

        # Reset global state
        sf_module._global_interrupted = False

        processor = SliceFilterProcesser(
            resource_dir=resource_dir,
            rst_dbf=rst_dbf,
            concurrent_tasks=1,
            protected_resources=set(),
        )

        # Acquire the semaphore
        processor._se.acquire()

        # Create a mock future with an exception
        mock_future = mocker.MagicMock(spec=Future)
        mock_future.exception.return_value = Exception("test error")

        mocker.patch.object(sf_module, "_thread")
        processor._task_done_cb(mock_future)

        assert sf_module._global_interrupted is True

        # Reset global state
        sf_module._global_interrupted = False

    def test_process_one_origin_at_thread(self, tmp_path: Path):
        """Test processing a single origin file into slices."""
        resource_dir = tmp_path / "resources"
        resource_dir.mkdir()
        rst_dbf = tmp_path / "resource_table.db"

        conn = sqlite3.connect(rst_dbf)
        conn.close()

        # Create a processor with small slice size for testing
        slice_size = 100
        processor = SliceFilterProcesser(
            resource_dir=resource_dir,
            rst_dbf=rst_dbf,
            slice_size=slice_size,
            protected_resources=set(),
        )

        # Initialize thread-local buffer
        processor._thread_worker_initializer()

        # Create a test file larger than 2 * slice_size
        content = b"A" * 250  # This will create 2-3 slices
        test_digest = sha256(content).digest()
        test_file = resource_dir / test_digest.hex()
        test_file.write_bytes(content)

        # Process the file
        resource_id = 1
        processor._process_one_origin_at_thread(resource_id, test_digest, len(content))

        # Original file should be deleted
        assert not test_file.exists()

        # Sliced files should exist
        result = processor._slice_res_queue.get_nowait()
        assert result is not None
        assert result[0] == resource_id
        assert len(result[1]) >= 2  # Should have multiple slices

        # Verify slice files exist
        for slice_digest in result[1].keys():
            slice_file = resource_dir / slice_digest.hex()
            assert slice_file.exists()

    def test_lower_bound_calculation(self, tmp_path: Path):
        """Test that lower bound is correctly set to 2 * slice_size."""
        resource_dir = tmp_path / "resources"
        resource_dir.mkdir()
        rst_dbf = tmp_path / "resource_table.db"

        conn = sqlite3.connect(rst_dbf)
        conn.close()

        processor = SliceFilterProcesser(
            resource_dir=resource_dir,
            rst_dbf=rst_dbf,
            slice_size=1024,
            protected_resources=set(),
        )

        assert processor._lower_bound == 1024 * 2

    def test_max_slice_size_calculation(self, tmp_path: Path):
        """Test that max slice size is correctly set to 1.5 * slice_size."""
        resource_dir = tmp_path / "resources"
        resource_dir.mkdir()
        rst_dbf = tmp_path / "resource_table.db"

        conn = sqlite3.connect(rst_dbf)
        conn.close()

        processor = SliceFilterProcesser(
            resource_dir=resource_dir,
            rst_dbf=rst_dbf,
            slice_size=1000,
            protected_resources=set(),
        )

        assert processor._max_slice_size == 1500
        assert processor._last_slice_maximum_size == 1500


class TestUpdateOneBatch:
    """Tests for _update_one_batch function."""

    def test_update_one_batch_single_entry(self, mocker):
        """Test updating a batch with a single entry."""
        mock_orm = mocker.MagicMock()
        mock_orm.orm_table_name = "resource_table"

        # Create a batch with one entry
        slice_digest = sha256(b"test").digest()
        batch = [(1, {slice_digest: 100})]

        # Mock orm_execute to return (digest, resource_id) pairs
        mock_orm.orm_execute.return_value = [(slice_digest, 100)]

        result = _update_one_batch(mock_orm, batch)

        # Should return None
        assert result is None
        # Should have called orm_insert_mappings
        mock_orm.orm_insert_mappings.assert_called_once()
        # Should have called orm_execute to get slice->resource_id mapping
        mock_orm.orm_execute.assert_called_once()
        # Should have called orm_update_entries_many
        mock_orm.orm_update_entries_many.assert_called_once()

    def test_update_one_batch_multiple_entries(self, mocker):
        """Test updating a batch with multiple entries."""
        mock_orm = mocker.MagicMock()
        mock_orm.orm_table_name = "resource_table"

        # Create a batch with multiple entries
        slice_digest1 = sha256(b"test1").digest()
        slice_digest2 = sha256(b"test2").digest()
        slice_digest3 = sha256(b"test3").digest()
        batch = [
            (1, {slice_digest1: 100, slice_digest2: 100}),
            (2, {slice_digest3: 200}),
        ]

        # Mock orm_execute to return (digest, resource_id) pairs
        mock_orm.orm_execute.return_value = [
            (slice_digest1, 1),
            (slice_digest2, 2),
            (slice_digest3, 3),
        ]

        result = _update_one_batch(mock_orm, batch)

        # Should return None
        assert result is None
        mock_orm.orm_insert_mappings.assert_called_once()
        mock_orm.orm_execute.assert_called_once()
        mock_orm.orm_update_entries_many.assert_called_once()
