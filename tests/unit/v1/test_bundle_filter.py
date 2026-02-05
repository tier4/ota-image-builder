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
"""Unit tests for v1/_resource_process/_bundle_filter.py module."""

from __future__ import annotations

import sqlite3
from hashlib import sha256
from pathlib import Path

import pytest
import zstandard

from ota_image_builder.v1._resource_process._bundle_filter import (
    BundleCompressedResult,
    BundleFilterProcesser,
    BundleResult,
    EntryToBeBundled,
    _batch_entries_with_filter,
    _generate_one_bundle,
)


class TestEntryToBeBundled:
    """Tests for EntryToBeBundled named tuple."""

    def test_create_entry(self):
        """Test creating an EntryToBeBundled."""
        entry = EntryToBeBundled(
            resource_id=1,
            digest=b"\x00" * 32,
            size=1024,
        )

        assert entry.resource_id == 1
        assert entry.digest == b"\x00" * 32
        assert entry.size == 1024

    def test_unpacking(self):
        """Test unpacking an EntryToBeBundled."""
        entry = EntryToBeBundled(
            resource_id=5,
            digest=b"\xff" * 32,
            size=2048,
        )

        resource_id, digest, size = entry

        assert resource_id == 5
        assert digest == b"\xff" * 32
        assert size == 2048


class TestBundleResult:
    """Tests for BundleResult named tuple."""

    def test_create_result(self):
        """Test creating a BundleResult."""
        bundled_entries = {(1, b"digest1"): (0, 100), (2, b"digest2"): (100, 200)}
        result = BundleResult(
            bundle_digest=b"bundle_digest",
            bundle_size=300,
            bundled_entries=bundled_entries,
        )

        assert result.bundle_digest == b"bundle_digest"
        assert result.bundle_size == 300
        assert len(result.bundled_entries) == 2


class TestBundleCompressedResult:
    """Tests for BundleCompressedResult named tuple."""

    def test_create_result(self):
        """Test creating a BundleCompressedResult."""
        result = BundleCompressedResult(
            compressed_digest=b"compressed",
            compressed_size=150,
        )

        assert result.compressed_digest == b"compressed"
        assert result.compressed_size == 150


class TestBatchEntriesWithFilter:
    """Tests for _batch_entries_with_filter function."""

    def test_batches_entries_correctly(self):
        """Test that entries are batched by size."""

        def gen_entries():
            yield EntryToBeBundled(1, b"d1", 100)
            yield EntryToBeBundled(2, b"d2", 100)
            yield EntryToBeBundled(3, b"d3", 100)
            yield EntryToBeBundled(4, b"d4", 100)
            yield EntryToBeBundled(5, b"d5", 100)

        batches = list(
            _batch_entries_with_filter(
                gen_entries(),
                expected_bundle_size=250,
                excluded_resources=set(),
            )
        )

        # With batch size 250 and entries of 100 each,
        # first batch should have 3 entries (300 > 250)
        # second batch should have 2 entries (200) but only if > min_bundle_ratio * 250 = 25
        assert len(batches) == 2
        assert batches[0][0] == 300  # total size
        assert len(batches[0][1]) == 3  # 3 entries
        assert batches[1][0] == 200  # total size
        assert len(batches[1][1]) == 2  # 2 entries

    def test_excludes_resources(self):
        """Test that excluded resources are filtered out."""

        def gen_entries():
            yield EntryToBeBundled(1, b"include1", 100)
            yield EntryToBeBundled(2, b"exclude", 100)
            yield EntryToBeBundled(3, b"include2", 100)

        batches = list(
            _batch_entries_with_filter(
                gen_entries(),
                expected_bundle_size=150,
                excluded_resources={b"exclude"},
            )
        )

        # Only non-excluded entries should be included
        assert len(batches) == 1
        assert batches[0][0] == 200  # 2 entries of 100 each
        all_digests = [e.digest for e in batches[0][1]]
        assert b"exclude" not in all_digests

    def test_empty_input(self):
        """Test with empty input."""

        def gen_entries():
            return
            yield  # noqa: B901

        batches = list(
            _batch_entries_with_filter(
                gen_entries(),
                expected_bundle_size=100,
                excluded_resources=set(),
            )
        )

        assert len(batches) == 0

    def test_min_bundle_ratio_filters_small_batches(self):
        """Test that small final batches are filtered by min_bundle_ratio."""

        def gen_entries():
            yield EntryToBeBundled(1, b"d1", 100)
            yield EntryToBeBundled(2, b"d2", 100)
            yield EntryToBeBundled(3, b"d3", 10)  # Small remaining batch

        batches = list(
            _batch_entries_with_filter(
                gen_entries(),
                expected_bundle_size=150,
                min_bundle_ratio=0.5,  # Need at least 75 bytes
                excluded_resources=set(),
            )
        )

        # First batch: 200 bytes (2 entries)
        # Remaining: 10 bytes, which is < 150 * 0.5 = 75, so filtered out
        assert len(batches) == 1
        assert len(batches[0][1]) == 2

    def test_all_excluded(self):
        """Test when all entries are excluded."""

        def gen_entries():
            yield EntryToBeBundled(1, b"exclude1", 100)
            yield EntryToBeBundled(2, b"exclude2", 100)

        batches = list(
            _batch_entries_with_filter(
                gen_entries(),
                expected_bundle_size=150,
                excluded_resources={b"exclude1", b"exclude2"},
            )
        )

        assert len(batches) == 0


class TestGenerateOneBundle:
    """Tests for _generate_one_bundle function."""

    def test_generates_bundle(self, tmp_path: Path):
        """Test that _generate_one_bundle creates a bundle from entries."""
        resource_dir = tmp_path / "resources"
        resource_dir.mkdir()

        # Create test resource files
        digest1 = sha256(b"content1").digest()
        digest2 = sha256(b"content2").digest()
        (resource_dir / digest1.hex()).write_bytes(b"content1")
        (resource_dir / digest2.hex()).write_bytes(b"content2")

        entries = [
            EntryToBeBundled(1, digest1, 8),
            EntryToBeBundled(2, digest2, 8),
        ]

        cctx = zstandard.ZstdCompressor()
        result = _generate_one_bundle(
            (16, entries),
            resource_dir=resource_dir,
            cctx=cctx,
        )

        assert result is not None
        bundle_res, compress_res = result

        # Bundle should have correct size
        assert bundle_res.bundle_size == 16
        assert len(bundle_res.bundled_entries) == 2

        # Compressed bundle should exist
        compressed_file = resource_dir / compress_res.compressed_digest.hex()
        assert compressed_file.exists()

        # Original files should be deleted
        assert not (resource_dir / digest1.hex()).exists()
        assert not (resource_dir / digest2.hex()).exists()

    def test_raises_on_size_mismatch(self, tmp_path: Path):
        """Test that _generate_one_bundle raises on size mismatch."""
        resource_dir = tmp_path / "resources"
        resource_dir.mkdir()

        # Create test resource file with wrong size
        digest = sha256(b"content").digest()
        (resource_dir / digest.hex()).write_bytes(b"content")

        entries = [
            EntryToBeBundled(1, digest, 100),  # Wrong size
        ]

        cctx = zstandard.ZstdCompressor()

        with pytest.raises(ValueError, match="mismatch"):
            _generate_one_bundle(
                (100, entries),
                resource_dir=resource_dir,
                cctx=cctx,
            )


class TestBundleFilterProcesser:
    """Tests for BundleFilterProcesser class."""

    def test_init(self, tmp_path: Path):
        """Test BundleFilterProcesser initialization."""
        resource_dir = tmp_path / "resources"
        resource_dir.mkdir()
        rst_dbf = tmp_path / "resource_table.db"

        conn = sqlite3.connect(rst_dbf)
        conn.close()

        processor = BundleFilterProcesser(
            resource_dir=resource_dir,
            rst_dbf=rst_dbf,
            bundle_lower_bound=1024,
            bundle_upper_bound=4096,
            bundle_blob_size=2048,
            bundle_compressed_max_sum=10 * 1024 * 1024,
            protected_resources=set(),
        )

        assert processor._resource_dir == resource_dir
        assert processor._lower_bound == 1024
        assert processor._upper_bound == 4096
        assert processor._bundle_blob_size == 2048

    def test_protected_resources_stored(self, tmp_path: Path):
        """Test that protected resources are stored correctly."""
        resource_dir = tmp_path / "resources"
        resource_dir.mkdir()
        rst_dbf = tmp_path / "resource_table.db"

        conn = sqlite3.connect(rst_dbf)
        conn.close()

        protected = {b"digest1", b"digest2"}
        processor = BundleFilterProcesser(
            resource_dir=resource_dir,
            rst_dbf=rst_dbf,
            protected_resources=protected,
        )

        assert processor._protected_resources == protected

    def test_default_values(self, tmp_path: Path):
        """Test default values are set correctly."""
        from ota_image_builder._configs import cfg

        resource_dir = tmp_path / "resources"
        resource_dir.mkdir()
        rst_dbf = tmp_path / "resource_table.db"

        conn = sqlite3.connect(rst_dbf)
        conn.close()

        processor = BundleFilterProcesser(
            resource_dir=resource_dir,
            rst_dbf=rst_dbf,
            protected_resources=set(),
        )

        assert processor._lower_bound == cfg.BUNDLE_LOWER_THRESHOULD
        assert processor._upper_bound == cfg.BUNDLE_UPPER_THRESHOULD
        assert processor._bundle_blob_size == cfg.BUNDLE_SIZE
