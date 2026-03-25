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

from ota_image_libs.v1.file_table.schema import FileTableInode

import ota_image_builder.v1._resource_process._rootfs_process as rp_module
from ota_image_builder._configs import cfg
from ota_image_builder._consts import EMPTY_FILE_SHA256_BYTE
from ota_image_builder.v1._resource_process._rootfs_process import (
    EMPTY_FILE_RS_ID,
    ResourceRegister,
    SystemImageProcesser,
    XattrProcessor,
    _global_shutdown_on_failed,
)


class TestXattrProcessor:
    """Tests for XattrProcessor class."""

    # -- _namespaced_file_cap_fixup --

    def test_fixup_v3_cap_strips_namespace(self):
        """V3 file cap (24 bytes) should be converted to V2 (20 bytes)."""
        # Build a 24-byte v3 cap: first 3 bytes arbitrary, byte[3]=0x03 (v3 marker),
        # bytes 4..19 are the v2 payload, bytes 20..23 are the rootid to strip.
        prefix = b"\x01\x00\x00"
        v2_payload = bytes(range(16))  # 16 bytes (indices 4..19)
        rootid = b"\xaa\xbb\xcc\xdd"
        v3_cap = prefix + XattrProcessor.FILE_CAP_V3 + v2_payload + rootid

        result = XattrProcessor._namespaced_file_cap_fixup(v3_cap)

        assert len(result) == 20
        # Version byte patched to v2
        assert result[3:4] == XattrProcessor.FILE_CAP_V2
        # Prefix preserved
        assert result[:3] == prefix
        # V2 payload preserved, rootid stripped
        assert result[4:] == v2_payload

    def test_fixup_v2_cap_unchanged(self):
        """V2 file cap (20 bytes) should be returned as-is."""
        v2_cap = b"\x01\x00\x00" + XattrProcessor.FILE_CAP_V2 + bytes(16)

        result = XattrProcessor._namespaced_file_cap_fixup(v2_cap)

        assert result is v2_cap  # identity — no copy made

    def test_fixup_non_v3_24_bytes_unchanged(self):
        """24-byte blob whose version byte is not v3 should be returned as-is."""
        cap = b"\x01\x00\x00" + XattrProcessor.FILE_CAP_V2 + bytes(20)
        assert len(cap) == 24

        result = XattrProcessor._namespaced_file_cap_fixup(cap)

        assert result is cap

    def test_fixup_wrong_length_unchanged(self):
        """Blob that is not exactly 24 bytes should be returned as-is."""
        for length in (0, 10, 20, 25, 100):
            cap = bytes(length)
            result = XattrProcessor._namespaced_file_cap_fixup(cap)
            assert result is cap

    # -- process_xattrs --

    def test_process_xattrs_applies_fixup_to_file_cap(self, mocker):
        """security.capability xattr should go through the fixup path."""
        prefix = b"\x01\x00\x00"
        v2_payload = bytes(16)
        rootid = b"\xaa\xbb\xcc\xdd"
        v3_cap = prefix + XattrProcessor.FILE_CAP_V3 + v2_payload + rootid

        mocker.patch("os.listxattr", return_value=["security.capability"])
        mocker.patch("os.getxattr", return_value=v3_cap)

        result = XattrProcessor.process_xattrs(Path("/fake"))

        assert "security.capability" in result
        assert len(result["security.capability"]) == 20
        assert result["security.capability"][3:4] == XattrProcessor.FILE_CAP_V2

    def test_process_xattrs_non_cap_xattr_passthrough(self, mocker):
        """Non-capability xattrs should be passed through unmodified."""
        raw_value = b"some_xattr_value"
        mocker.patch("os.listxattr", return_value=["user.custom"])
        mocker.patch("os.getxattr", return_value=raw_value)

        result = XattrProcessor.process_xattrs(Path("/fake"))

        assert result == {"user.custom": raw_value}

    def test_process_xattrs_multiple_xattrs(self, mocker):
        """Multiple xattrs should all be included; only cap gets fixup."""
        v2_cap = b"\x01\x00\x00" + XattrProcessor.FILE_CAP_V2 + bytes(16)
        custom_val = b"hello"

        mocker.patch(
            "os.listxattr",
            return_value=["security.capability", "user.foo"],
        )
        mocker.patch(
            "os.getxattr",
            side_effect=lambda _f, name, **kw: (
                v2_cap if name == "security.capability" else custom_val
            ),
        )

        result = XattrProcessor.process_xattrs(Path("/fake"))

        assert len(result) == 2
        # v2 cap is already valid, returned unchanged
        assert result["security.capability"] == v2_cap
        assert result["user.foo"] == custom_val

    def test_process_xattrs_empty(self, mocker):
        """File with no xattrs should return empty dict."""
        mocker.patch("os.listxattr", return_value=[])

        result = XattrProcessor.process_xattrs(Path("/fake"))

        assert result == {}

    def test_process_xattrs_follows_symlinks_false(self, mocker):
        """Both listxattr and getxattr should be called with follow_symlinks=False."""
        mock_getxattr = mocker.patch("os.getxattr", return_value=b"v")
        mock_listxattr = mocker.patch("os.listxattr", return_value=["user.x"])

        XattrProcessor.process_xattrs(Path("/fake"))

        mock_listxattr.assert_called_once_with(Path("/fake"), follow_symlinks=False)
        mock_getxattr.assert_called_once_with(
            Path("/fake"), "user.x", follow_symlinks=False
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

    def test_sets_global_interrupted_and_interrupts_main(self, mocker):
        """Test that the function sets global flag and interrupts main thread."""
        # Reset global state
        rp_module._global_interrupted = False

        mock_thread = mocker.patch.object(rp_module, "_thread")
        _global_shutdown_on_failed(Exception("test error"))

        assert rp_module._global_interrupted is True
        mock_thread.interrupt_main.assert_called_once()

        # Reset for other tests
        rp_module._global_interrupted = False

    def test_only_interrupts_once(self, mocker):
        """Test that interrupt_main is only called once."""
        # Set as already interrupted
        rp_module._global_interrupted = True

        mock_thread = mocker.patch.object(rp_module, "_thread")
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

    def test_process_inode_non_hardlinked_file(self, tmp_path: Path, mocker):
        """Test _process_inode correctly records uid and gid for non-hardlinked file."""
        que = Queue()
        src = tmp_path / "src"
        src.mkdir()
        resource_dir = tmp_path / "resources"
        resource_dir.mkdir()

        test_file = src / "test_file.txt"
        test_file.write_text("test content")

        processor = SystemImageProcesser(
            que,
            src=src,
            resource_dir=resource_dir,
        )

        # Mock stat to return different uid and gid values
        mock_stat = mocker.MagicMock()
        mock_stat.st_uid = 1000
        mock_stat.st_gid = 2000
        mock_stat.st_mode = 0o644
        mock_stat.st_nlink = 1  # Non-hardlinked

        mocker.patch.object(Path, "stat", return_value=mock_stat)
        mocker.patch("os.listxattr", return_value=[])

        # Process the inode
        inode_id = processor._process_inode(test_file)

        # Get the FileTableInode from queue
        inode_entry = que.get_nowait()

        assert isinstance(inode_entry, FileTableInode)
        assert inode_entry.inode_id == inode_id
        assert inode_entry.uid == 1000
        assert inode_entry.gid == 2000
        assert inode_entry.mode == 0o644

    def test_process_inode_directory(self, tmp_path: Path, mocker):
        """Test _process_inode correctly records uid and gid for directory."""
        que = Queue()
        src = tmp_path / "src"
        src.mkdir()
        resource_dir = tmp_path / "resources"
        resource_dir.mkdir()

        test_dir = src / "test_dir"
        test_dir.mkdir()

        processor = SystemImageProcesser(
            que,
            src=src,
            resource_dir=resource_dir,
        )

        # Mock stat to return different uid and gid values
        # Directory has st_nlink >= 2 but is_dir() returns True, so it goes through non-hardlinked path
        mock_stat = mocker.MagicMock()
        mock_stat.st_uid = 1001
        mock_stat.st_gid = 2001
        mock_stat.st_mode = 0o755
        mock_stat.st_nlink = 3  # Directories typically have nlink >= 2

        mocker.patch.object(Path, "stat", return_value=mock_stat)
        mocker.patch.object(Path, "is_symlink", return_value=False)
        mocker.patch.object(Path, "is_dir", return_value=True)
        mocker.patch("os.listxattr", return_value=[])

        # Process the inode
        inode_id = processor._process_inode(test_dir)

        # Get the FileTableInode from queue
        inode_entry = que.get_nowait()

        assert isinstance(inode_entry, FileTableInode)
        assert inode_entry.inode_id == inode_id
        assert inode_entry.uid == 1001
        assert inode_entry.gid == 2001
        assert inode_entry.mode == 0o755

    def test_process_inode_hardlinked_file(self, tmp_path: Path, mocker):
        """Test _process_inode correctly records uid and gid for hardlinked file."""
        que = Queue()
        src = tmp_path / "src"
        src.mkdir()
        resource_dir = tmp_path / "resources"
        resource_dir.mkdir()

        test_file = src / "original.txt"
        test_file.write_text("test content")

        processor = SystemImageProcesser(
            que,
            src=src,
            resource_dir=resource_dir,
        )

        # Mock stat to return hardlinked file with different uid and gid
        mock_stat = mocker.MagicMock()
        mock_stat.st_uid = 1002
        mock_stat.st_gid = 2002
        mock_stat.st_mode = 0o644
        mock_stat.st_nlink = 3  # Hardlinked (nlink > 1)
        mock_stat.st_ino = 12345

        mocker.patch.object(Path, "stat", return_value=mock_stat)
        mocker.patch.object(Path, "is_symlink", return_value=False)
        mocker.patch.object(Path, "is_dir", return_value=False)
        mocker.patch("os.listxattr", return_value=[])

        # Process the inode for the hardlinked file
        inode_id = processor._process_inode(test_file)

        # Get the FileTableInode from queue
        inode_entry = que.get_nowait()

        assert isinstance(inode_entry, FileTableInode)
        # For hardlinked files, inode_id should be -st_ino
        assert inode_entry.inode_id == -12345
        assert inode_id == -12345
        assert inode_entry.uid == 1002
        assert inode_entry.gid == 2002
        assert inode_entry.mode == 0o644

    def test_process_inode_symlink(self, tmp_path: Path, mocker):
        """Test _process_inode correctly records uid and gid for symlink."""
        que = Queue()
        src = tmp_path / "src"
        src.mkdir()
        resource_dir = tmp_path / "resources"
        resource_dir.mkdir()

        symlink = src / "symlink.txt"
        symlink.symlink_to("target.txt")

        processor = SystemImageProcesser(
            que,
            src=src,
            resource_dir=resource_dir,
        )

        # Mock stat to return different uid and gid values
        # Symlinks have st_nlink == 1, so they go through the non-hardlinked path
        mock_stat = mocker.MagicMock()
        mock_stat.st_uid = 1003
        mock_stat.st_gid = 2003
        mock_stat.st_mode = 0o777
        mock_stat.st_nlink = 1

        mocker.patch.object(Path, "stat", return_value=mock_stat)
        mocker.patch("os.listxattr", return_value=[])

        # Process the inode
        inode_id = processor._process_inode(symlink)

        # Get the FileTableInode from queue
        inode_entry = que.get_nowait()

        assert isinstance(inode_entry, FileTableInode)
        assert inode_entry.inode_id == inode_id
        assert inode_entry.uid == 1003
        assert inode_entry.gid == 2003
        assert inode_entry.mode == 0o777
