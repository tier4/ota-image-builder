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
"""Unit tests for v1/_resource_process/_db_utils.py module."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ota_image_builder.v1._resource_process._db_utils import (
    DataBaseBuilder,
    ImageStats,
    ImageStatsQuery,
    _FileTableDBInsertHelper,
    _global_shutdown_on_failed,
    _ResourceDBInsertMappingsHelper,
    _set_db_journal_mode,
    count_entries_in_table,
    get_total_entries_size_in_table,
    init_file_table_db,
    init_resource_table_db,
    vacuum_db,
)


class TestImageStats:
    """Tests for ImageStats pydantic model."""

    def test_default_values(self):
        """Test that default values are all zeros."""
        stats = ImageStats()

        assert stats.image_blobs_count == 0
        assert stats.image_blobs_size == 0
        assert stats.sys_image_size == 0
        assert stats.sys_image_regular_files_count == 0
        assert stats.sys_image_non_regular_files_count == 0
        assert stats.sys_image_dirs_count == 0
        assert stats.sys_image_unique_file_entries == 0
        assert stats.sys_image_unique_file_entries_size == 0

    def test_with_custom_values(self):
        """Test with custom values."""
        stats = ImageStats(
            image_blobs_count=100,
            image_blobs_size=1024 * 1024,
            sys_image_size=500 * 1024 * 1024,
            sys_image_regular_files_count=1000,
            sys_image_non_regular_files_count=50,
            sys_image_dirs_count=200,
            sys_image_unique_file_entries=800,
            sys_image_unique_file_entries_size=400 * 1024 * 1024,
        )

        assert stats.image_blobs_count == 100
        assert stats.image_blobs_size == 1024 * 1024
        assert stats.sys_image_size == 500 * 1024 * 1024
        assert stats.sys_image_regular_files_count == 1000
        assert stats.sys_image_non_regular_files_count == 50
        assert stats.sys_image_dirs_count == 200
        assert stats.sys_image_unique_file_entries == 800
        assert stats.sys_image_unique_file_entries_size == 400 * 1024 * 1024

    def test_model_dump(self):
        """Test that model_dump returns all fields."""
        stats = ImageStats(image_blobs_count=10)
        dumped = stats.model_dump()

        assert "image_blobs_count" in dumped
        assert "image_blobs_size" in dumped
        assert "sys_image_size" in dumped
        assert dumped["image_blobs_count"] == 10


class TestVacuumDb:
    """Tests for vacuum_db function."""

    def test_vacuum_empty_db(self, tmp_path: Path):
        """Test vacuum on an empty database."""
        db_file = tmp_path / "test.db"

        # Create empty database
        conn = sqlite3.connect(db_file)
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, data TEXT)")
        conn.close()

        # Should not raise
        vacuum_db(db_file)

        assert db_file.is_file()

    def test_vacuum_with_data(self, tmp_path: Path):
        """Test vacuum on a database with data."""
        db_file = tmp_path / "test.db"

        conn = sqlite3.connect(db_file)
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, data TEXT)")
        for i in range(100):
            conn.execute("INSERT INTO test (data) VALUES (?)", (f"data_{i}",))
        conn.commit()
        # Delete some rows to create fragmentation
        conn.execute("DELETE FROM test WHERE id % 2 = 0")
        conn.commit()
        conn.close()

        vacuum_db(db_file)

        # Verify data integrity after vacuum
        conn = sqlite3.connect(db_file)
        count = conn.execute("SELECT COUNT(*) FROM test").fetchone()[0]
        conn.close()
        assert count == 50


class TestSetDbJournalMode:
    """Tests for _set_db_journal_mode function."""

    def test_set_wal_mode(self, tmp_path: Path):
        """Test setting WAL journal mode."""
        db_file = tmp_path / "test.db"
        conn = sqlite3.connect(db_file)
        conn.execute("CREATE TABLE test (id INTEGER)")

        _set_db_journal_mode(conn, "WAL")

        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode.upper() == "WAL"

    def test_set_delete_mode(self, tmp_path: Path):
        """Test setting DELETE journal mode."""
        db_file = tmp_path / "test.db"
        conn = sqlite3.connect(db_file)
        conn.execute("CREATE TABLE test (id INTEGER)")

        _set_db_journal_mode(conn, "DELETE")

        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode.upper() == "DELETE"


class TestInitFileTableDb:
    """Tests for init_file_table_db function."""

    def test_creates_database(self, tmp_path: Path):
        """Test that init_file_table_db creates and returns a db helper."""
        db_file = tmp_path / "file_table.db"

        result = init_file_table_db(db_file)

        assert result is not None
        assert db_file.is_file()


class TestInitResourceTableDb:
    """Tests for init_resource_table_db function."""

    def test_creates_database(self, tmp_path: Path):
        """Test that init_resource_table_db creates and returns a db helper."""
        db_file = tmp_path / "resource_table.db"

        result = init_resource_table_db(db_file)

        assert result is not None
        assert db_file.is_file()


class TestCountEntriesInTable:
    """Tests for count_entries_in_table function."""

    def test_count_entries_no_where(self):
        """Test counting all entries without where clause."""
        mock_orm = MagicMock()
        mock_orm.orm_table_name = "test_table"
        mock_orm.orm_execute.return_value = [(10,)]

        result = count_entries_in_table(mock_orm)

        assert result == 10
        mock_orm.orm_execute.assert_called_once()

    def test_count_entries_with_where(self):
        """Test counting entries with where clause."""
        mock_orm = MagicMock()
        mock_orm.orm_table_name = "test_table"
        mock_orm.orm_execute.return_value = [(5,)]

        result = count_entries_in_table(mock_orm, where_stmt="WHERE size > 100")

        assert result == 5
        call_args = mock_orm.orm_execute.call_args
        assert "WHERE size > 100" in call_args[0][0]


class TestGetTotalEntriesSizeInTable:
    """Tests for get_total_entries_size_in_table function."""

    def test_get_total_size_no_where(self):
        """Test getting total size without where clause."""
        mock_orm = MagicMock()
        mock_orm.orm_table_name = "test_table"
        mock_orm.orm_execute.return_value = [(1024,)]

        result = get_total_entries_size_in_table(mock_orm)

        assert result == 1024
        mock_orm.orm_execute.assert_called_once()

    def test_get_total_size_with_where(self):
        """Test getting total size with where clause."""
        mock_orm = MagicMock()
        mock_orm.orm_table_name = "test_table"
        mock_orm.orm_execute.return_value = [(512,)]

        result = get_total_entries_size_in_table(
            mock_orm, where_stmt="WHERE type = 'file'"
        )

        assert result == 512
        call_args = mock_orm.orm_execute.call_args
        assert "WHERE type = 'file'" in call_args[0][0]


class TestFileTableDBInsertHelper:
    """Tests for _FileTableDBInsertHelper class."""

    def test_insert_batches_entries(self):
        """Test that entries are batched before insertion."""
        mock_orm = MagicMock()
        helper = _FileTableDBInsertHelper(mock_orm, batch_size=3)

        # Insert 2 entries (below batch size)
        helper.insert("entry1")
        helper.insert("entry2")

        # Should not have called insert yet
        mock_orm.orm_insert_entries.assert_not_called()

        # Insert 2 more entries (exceeds batch size)
        helper.insert("entry3")
        helper.insert("entry4")

        # Now should have flushed the batch
        mock_orm.orm_insert_entries.assert_called_once()

    def test_finalize_flushes_remaining(self):
        """Test that finalize flushes remaining entries."""
        mock_orm = MagicMock()
        captured_entries = []

        def capture_entries(entries, or_option=None):
            # Make a copy since the list will be cleared after the call
            captured_entries.extend(list(entries))

        mock_orm.orm_insert_entries.side_effect = capture_entries
        helper = _FileTableDBInsertHelper(mock_orm, batch_size=10)

        helper.insert("entry1")
        helper.insert("entry2")

        mock_orm.orm_insert_entries.assert_not_called()

        helper.finalize()

        mock_orm.orm_insert_entries.assert_called_once()
        assert captured_entries == ["entry1", "entry2"]

    def test_finalize_empty_store(self):
        """Test that finalize with empty store does nothing."""
        mock_orm = MagicMock()
        helper = _FileTableDBInsertHelper(mock_orm, batch_size=10)

        helper.finalize()

        mock_orm.orm_insert_entries.assert_not_called()


class TestResourceDBInsertMappingsHelper:
    """Tests for _ResourceDBInsertMappingsHelper class."""

    def test_insert_mappings_batches(self):
        """Test that mappings are batched before insertion."""
        mock_orm = MagicMock()
        helper = _ResourceDBInsertMappingsHelper(mock_orm, batch_size=3)

        helper.insert_mappings({"id": 1})
        helper.insert_mappings({"id": 2})

        mock_orm.orm_insert_mappings.assert_not_called()

        helper.insert_mappings({"id": 3})
        helper.insert_mappings({"id": 4})

        mock_orm.orm_insert_mappings.assert_called_once()

    def test_finalize_mappings_flushes(self):
        """Test that finalize_mappings flushes remaining."""
        mock_orm = MagicMock()
        helper = _ResourceDBInsertMappingsHelper(mock_orm, batch_size=10)

        helper.insert_mappings({"id": 1})
        helper.insert_mappings({"id": 2})

        mock_orm.orm_insert_mappings.assert_not_called()

        helper.finalize_mappings()

        mock_orm.orm_insert_mappings.assert_called_once()

    def test_finalize_mappings_empty(self):
        """Test that finalize with empty store does nothing."""
        mock_orm = MagicMock()
        helper = _ResourceDBInsertMappingsHelper(mock_orm, batch_size=10)

        helper.finalize_mappings()

        mock_orm.orm_insert_mappings.assert_not_called()


class TestGlobalShutdownOnFailed:
    """Tests for _global_shutdown_on_failed function."""

    def test_sets_global_interrupted_and_interrupts_main(self):
        """Test that the function sets global flag and interrupts main thread."""
        import ota_image_builder.v1._resource_process._db_utils as db_utils

        # Reset global state
        db_utils._global_interrupted = False

        with patch.object(db_utils, "_thread") as mock_thread:
            _global_shutdown_on_failed(Exception("test error"))

            assert db_utils._global_interrupted is True
            mock_thread.interrupt_main.assert_called_once()

        # Reset for other tests
        db_utils._global_interrupted = False

    def test_only_interrupts_once(self):
        """Test that interrupt_main is only called once."""
        import ota_image_builder.v1._resource_process._db_utils as db_utils

        # Set as already interrupted
        db_utils._global_interrupted = True

        with patch.object(db_utils, "_thread") as mock_thread:
            _global_shutdown_on_failed(Exception("test error"))

            # Should not call interrupt_main again
            mock_thread.interrupt_main.assert_not_called()

        # Reset for other tests
        db_utils._global_interrupted = False


class TestSetDbJournalModeError:
    """Tests for _set_db_journal_mode error handling."""

    def test_raises_on_closed_connection(self, tmp_path: Path):
        """Test that closed connection raises ProgrammingError."""
        db_file = tmp_path / "test.db"
        conn = sqlite3.connect(db_file)
        conn.execute("CREATE TABLE test (id INTEGER)")

        # Close the connection to make it invalid
        conn.close()

        with pytest.raises(sqlite3.ProgrammingError):
            _set_db_journal_mode(conn, "DELETE")


class TestDataBaseBuilder:
    """Tests for DataBaseBuilder class."""

    def test_init(self, tmp_path: Path):
        """Test DataBaseBuilder initialization."""
        from queue import Queue

        ft_dbf = tmp_path / "file_table.db"
        rst_dbf = tmp_path / "resource_table.db"
        que = Queue()

        builder = DataBaseBuilder(ft_dbf, rst_dbf, que)

        assert builder._que is que
        assert builder._ft_db_helper is not None
        assert builder._rst_db_helper is not None

    def test_start_builder_thread(self, tmp_path: Path):
        """Test that start_builder_thread returns a thread."""
        from queue import Queue

        ft_dbf = tmp_path / "file_table.db"
        rst_dbf = tmp_path / "resource_table.db"
        que = Queue()

        # Initialize databases first
        init_file_table_db(ft_dbf)
        init_resource_table_db(rst_dbf)

        builder = DataBaseBuilder(ft_dbf, rst_dbf, que)
        thread = builder.start_builder_thread()

        assert thread is not None
        assert thread.is_alive()

        # Stop the thread by sending None
        que.put(None)
        thread.join(timeout=5)
        assert not thread.is_alive()

    def test_finalize_db_build(self, tmp_path: Path):
        """Test that finalize_db_build sets journal mode to DELETE."""
        from queue import Queue

        ft_dbf = tmp_path / "file_table.db"
        rst_dbf = tmp_path / "resource_table.db"
        que = Queue()

        # Initialize databases first
        init_file_table_db(ft_dbf)
        init_resource_table_db(rst_dbf)

        builder = DataBaseBuilder(ft_dbf, rst_dbf, que)
        builder.finalize_db_build()

        # Check journal mode is DELETE
        conn = sqlite3.connect(ft_dbf)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode.upper() == "DELETE"


class TestImageStatsQuery:
    """Tests for ImageStatsQuery class."""

    def test_init(self, tmp_path: Path):
        """Test ImageStatsQuery initialization."""
        ft_dbf = tmp_path / "file_table.db"
        rst_dbf = tmp_path / "resource_table.db"

        # Initialize database
        init_file_table_db(ft_dbf)

        query = ImageStatsQuery(ft_dbf, rst_dbf)

        assert query._ft_db_helper is not None

    def test_get_stats_after_process(self, tmp_path: Path):
        """Test getting stats after process with mocked data."""
        ft_dbf = tmp_path / "file_table.db"
        rst_dbf = tmp_path / "resource_table.db"

        # Initialize databases
        init_file_table_db(ft_dbf)
        init_resource_table_db(rst_dbf)

        query = ImageStatsQuery(ft_dbf, rst_dbf)

        # Mock the internal method that calculates sys_image_size
        # since empty tables return None for SUM queries
        with patch.object(query, "_cal_sys_image_size", return_value=0):
            with patch(
                "ota_image_builder.v1._resource_process._db_utils.get_total_entries_size_in_table",
                return_value=0,
            ):
                stats = query.get_stats_after_process()

        assert isinstance(stats, ImageStats)
        # Empty database should have zeros
        assert stats.sys_image_regular_files_count == 0
        assert stats.sys_image_dirs_count == 0
