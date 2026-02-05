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
"""Unit tests for _common.py module."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from ota_image_builder._common import (
    WriteThreadSafeDict,
    check_if_valid_ota_image,
    configure_logging,
    count_blobs_in_dir,
    exit_with_err_msg,
    func_call_with_se,
    get_bsp_ver_info,
    human_readable_size,
)


class TestHumanReadableSize:
    """Tests for human_readable_size function."""

    @pytest.mark.parametrize(
        ("input_bytes", "expected"),
        [
            (0, "0 Bytes"),
            (1, "1 Bytes"),
            (512, "512 Bytes"),
            (1024, "1024 Bytes"),  # Exactly 1 KiB stays as Bytes (> 1 check)
            (1025, "1.00 KiB"),  # Slightly over 1 KiB
            (1024**2, "1024.00 KiB"),  # Exactly 1 MiB stays as KiB
            (1024**2 + 512 * 1024, "1.50 MiB"),  # 1.5 MiB
            (1024**3, "1024.00 MiB"),  # Exactly 1 GiB stays as MiB
            (2 * 1024**3 + 512 * 1024**2, "2.50 GiB"),  # 2.5 GiB
        ],
    )
    def test_human_readable_size(self, input_bytes: int, expected: str):
        """Test conversion of bytes to human-readable format."""
        assert human_readable_size(input_bytes) == expected

    def test_large_size(self):
        """Test with very large sizes."""
        size_10gib = 10 * 1024**3
        result = human_readable_size(size_10gib)
        assert "GiB" in result
        assert result.startswith("10.00")


class TestGetBspVerInfo:
    """Tests for get_bsp_ver_info function."""

    def test_valid_nv_tegra_release(self, sample_nv_tegra_release: str):
        """Test parsing valid nv_tegra_release content."""
        result = get_bsp_ver_info(sample_nv_tegra_release)
        assert result == "R35.4.1"

    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            (
                "# R32 (release), REVISION: 7.1, GCID: 12345678, "
                "BOARD: t186ref, EABI: aarch64, DATE: Mon Jan  1 00:00:00 UTC 2024",
                "R32.7.1",
            ),
            (
                "# R36 (release), REVISION: 0.0, GCID: 99999999, "
                "BOARD: t234ref, EABI: aarch64, DATE: Fri Dec 31 23:59:59 UTC 2025",
                "R36.0.0",
            ),
        ],
    )
    def test_various_versions(self, content: str, expected: str):
        """Test parsing various nv_tegra_release versions."""
        result = get_bsp_ver_info(content)
        assert result == expected

    def test_invalid_content(self):
        """Test with invalid content."""
        result = get_bsp_ver_info("invalid content")
        assert result is None

    def test_empty_string(self):
        """Test with empty string."""
        result = get_bsp_ver_info("")
        assert result is None


class TestWriteThreadSafeDict:
    """Tests for WriteThreadSafeDict class."""

    def test_basic_operations(self):
        """Test basic dict operations."""
        d = WriteThreadSafeDict[str, int]()
        d["key1"] = 100
        d["key2"] = 200

        assert d["key1"] == 100
        assert d["key2"] == 200
        assert len(d) == 2

    def test_thread_safety(self):
        """Test that concurrent writes don't cause data corruption."""
        d = WriteThreadSafeDict[int, int]()
        num_threads = 10
        writes_per_thread = 1000

        def writer(thread_id: int):
            for i in range(writes_per_thread):
                key = thread_id * writes_per_thread + i
                d[key] = key

        threads = [
            threading.Thread(target=writer, args=(i,)) for i in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All writes should have succeeded
        assert len(d) == num_threads * writes_per_thread

    def test_initialization_with_data(self):
        """Test initialization with existing data."""
        d = WriteThreadSafeDict({"a": 1, "b": 2})
        assert d["a"] == 1
        assert d["b"] == 2


class TestCountBlobsInDir:
    """Tests for count_blobs_in_dir function."""

    def test_empty_directory(self, tmp_path: Path):
        """Test counting blobs in an empty directory."""
        count, size = count_blobs_in_dir(tmp_path)
        assert count == 0
        assert size == 0

    def test_directory_with_files(self, tmp_path: Path):
        """Test counting blobs in a directory with files."""
        # Create test files
        (tmp_path / "file1.txt").write_text("hello")
        (tmp_path / "file2.txt").write_text("world!")
        (tmp_path / "file3.bin").write_bytes(b"\x00" * 100)

        count, size = count_blobs_in_dir(tmp_path)
        assert count == 3
        assert size == 5 + 6 + 100  # "hello" + "world!" + 100 bytes


class TestCheckIfValidOtaImage:
    """Tests for check_if_valid_ota_image function."""

    def test_valid_ota_image(self, tmp_path: Path):
        """Test with a valid OTA image structure."""
        # Create required structure
        oci_layout = tmp_path / "oci-layout"
        oci_layout.write_text(json.dumps({"imageLayoutVersion": "1.0.0"}))

        index_file = tmp_path / "index.json"
        index_file.write_text(json.dumps({"schemaVersion": 2}))

        # RESOURCE_DIR is "blobs/sha256"
        resource_dir = tmp_path / "blobs" / "sha256"
        resource_dir.mkdir(parents=True)

        result = check_if_valid_ota_image(tmp_path)
        assert result is True

    def test_missing_oci_layout(self, tmp_path: Path):
        """Test with missing oci-layout file."""
        # Create only index and resource dir
        (tmp_path / "index.json").write_text("{}")
        (tmp_path / "blobs").mkdir()

        result = check_if_valid_ota_image(tmp_path)
        assert result is False

    def test_invalid_oci_layout_content(self, tmp_path: Path):
        """Test with invalid oci-layout content."""
        (tmp_path / "oci-layout").write_text(json.dumps({"invalid": "content"}))
        (tmp_path / "index.json").write_text("{}")
        (tmp_path / "blobs").mkdir()

        result = check_if_valid_ota_image(tmp_path)
        assert result is False

    def test_missing_index_file(self, tmp_path: Path):
        """Test with missing index.json file."""
        (tmp_path / "oci-layout").write_text(
            json.dumps({"imageLayoutVersion": "1.0.0"})
        )
        (tmp_path / "blobs").mkdir()

        result = check_if_valid_ota_image(tmp_path)
        assert result is False

    def test_missing_resource_dir(self, tmp_path: Path):
        """Test with missing resource directory."""
        (tmp_path / "oci-layout").write_text(
            json.dumps({"imageLayoutVersion": "1.0.0"})
        )
        (tmp_path / "index.json").write_text("{}")

        result = check_if_valid_ota_image(tmp_path)
        assert result is False

    def test_nonexistent_directory(self, tmp_path: Path):
        """Test with a non-existent directory."""
        nonexistent = tmp_path / "nonexistent"
        result = check_if_valid_ota_image(nonexistent)
        assert result is False


class TestFuncCallWithSe:
    """Tests for func_call_with_se function."""

    def test_acquires_semaphore_before_call(self):
        """Test that semaphore is acquired before the function is called."""
        se = threading.Semaphore(1)
        call_order = []

        def test_func():
            call_order.append("func_called")
            return "result"

        wrapped = func_call_with_se(test_func, se)
        result = wrapped()

        assert result == "result"
        assert call_order == ["func_called"]
        # Semaphore should have been acquired (count reduced by 1)
        # We can verify by checking if we can acquire it again
        assert se.acquire(blocking=False) is False  # Already acquired, not released

    def test_with_arguments(self):
        """Test that arguments are passed correctly."""
        se = threading.Semaphore(2)

        def add(a, b):
            return a + b

        wrapped = func_call_with_se(add, se)
        result = wrapped(3, 5)

        assert result == 8

    def test_with_kwargs(self):
        """Test that keyword arguments are passed correctly."""
        se = threading.Semaphore(1)

        def greet(name, greeting="Hello"):
            return f"{greeting}, {name}!"

        wrapped = func_call_with_se(greet, se)
        result = wrapped("World", greeting="Hi")

        assert result == "Hi, World!"

    def test_semaphore_limits_concurrency(self):
        """Test that semaphore limits concurrent executions.

        Note: func_call_with_se acquires but does NOT release the semaphore.
        This test verifies that behavior by checking that only N calls can proceed
        where N is the semaphore count.
        """
        se = threading.Semaphore(2)
        call_count = [0]
        lock = threading.Lock()

        def test_func():
            with lock:
                call_count[0] += 1
            return True

        wrapped = func_call_with_se(test_func, se)

        # First two calls should succeed (semaphore count is 2)
        result1 = wrapped()
        result2 = wrapped()
        assert result1 is True
        assert result2 is True
        assert call_count[0] == 2

        # Semaphore should now be exhausted (both slots acquired, not released)
        assert se.acquire(blocking=False) is False


class TestConfigureLogging:
    """Tests for configure_logging function."""

    def test_configure_logging_sets_levels(self):
        """Test that configure_logging sets appropriate log levels."""
        import logging

        configure_logging()

        # Check that ota_image_builder logger is set to INFO
        builder_logger = logging.getLogger("ota_image_builder")
        assert builder_logger.level == logging.INFO

        # Check that ota_image_libs logger is set to INFO
        libs_logger = logging.getLogger("ota_image_libs")
        assert libs_logger.level == logging.INFO


class TestExitWithErrMsg:
    """Tests for exit_with_err_msg function."""

    def test_exits_with_default_code(self):
        """Test that exit_with_err_msg exits with default code 1."""
        with pytest.raises(SystemExit) as exc_info:
            exit_with_err_msg("test error")
        assert exc_info.value.code == 1

    def test_exits_with_custom_code(self):
        """Test that exit_with_err_msg exits with custom code."""
        with pytest.raises(SystemExit) as exc_info:
            exit_with_err_msg("test error", exit_code=42)
        assert exc_info.value.code == 42

    def test_prints_error_message(self, capsys):
        """Test that error message is printed."""
        with pytest.raises(SystemExit):
            exit_with_err_msg("my custom error")

        captured = capsys.readouterr()
        assert "ERR: my custom error" in captured.out
