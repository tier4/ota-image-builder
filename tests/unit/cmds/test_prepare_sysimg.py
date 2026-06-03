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
"""Unit tests for cmds/prepare_sysimg.py module."""

from __future__ import annotations

import os
from argparse import Namespace
from pathlib import Path

import pytest

from ota_image_builder.cmds.prepare_sysimg import (
    RESOLV_CONF_RELPATH,
    RESOLV_CONF_SYMLINK_TARGET,
    RootfsImagePreparer,
    _is_valid_resolv_conf,
    prepare_sysimg_cmd,
)


def _write_resolv_conf(rootfs: Path, content: str) -> Path:
    """Create <rootfs>/etc/resolv.conf as a regular file with `content`."""
    resolv_conf = rootfs / RESOLV_CONF_RELPATH
    resolv_conf.parent.mkdir(parents=True, exist_ok=True)
    resolv_conf.write_text(content)
    return resolv_conf


class TestIsValidResolvConf:
    """Tests for the _is_valid_resolv_conf helper."""

    @pytest.mark.parametrize(
        "content",
        [
            "nameserver 8.8.8.8\n",
            "nameserver 1.1.1.1",
            "  nameserver 192.168.0.1\n",  # leading whitespace tolerated
            "nameserver 2001:4860:4860::8888\n",  # IPv6
            "nameserver fe80::1%eth0\n",  # IPv6 with zone index
            "# generated file\nsearch example.com\nnameserver 8.8.4.4\n",
            "options edns0\nnameserver 8.8.8.8 # trailing comment\n",
        ],
    )
    def test_valid_content(self, content: str):
        assert _is_valid_resolv_conf(content) is True

    @pytest.mark.parametrize(
        "content",
        [
            "",  # empty
            "\n\n  \n",  # only blank lines
            "# nameserver 8.8.8.8\n",  # commented out
            "search example.com\noptions edns0\n",  # no nameserver
            "nameserver\n",  # nameserver directive without an address
            "nameservers 8.8.8.8\n",  # not the nameserver directive
            "nameserver not-an-ip\n",  # addr doesn't look like an IP
        ],
    )
    def test_invalid_content(self, content: str):
        assert _is_valid_resolv_conf(content) is False


class TestFixupResolvConf:
    """Tests for RootfsImagePreparer._fixup_resolv_conf."""

    def _assert_fixed_up(self, rootfs: Path) -> None:
        resolv_conf = rootfs / RESOLV_CONF_RELPATH
        assert resolv_conf.is_symlink()
        assert os.readlink(resolv_conf) == RESOLV_CONF_SYMLINK_TARGET

    def test_missing_is_fixed(self, tmp_path: Path):
        (tmp_path / "etc").mkdir()
        RootfsImagePreparer(tmp_path)._fixup_resolv_conf()
        self._assert_fixed_up(tmp_path)

    def test_missing_etc_dir_is_created(self, tmp_path: Path):
        # /etc itself does not exist yet
        RootfsImagePreparer(tmp_path)._fixup_resolv_conf()
        self._assert_fixed_up(tmp_path)

    def test_empty_file_is_fixed(self, tmp_path: Path):
        _write_resolv_conf(tmp_path, "")
        RootfsImagePreparer(tmp_path)._fixup_resolv_conf()
        self._assert_fixed_up(tmp_path)

    def test_no_nameserver_file_is_fixed(self, tmp_path: Path):
        _write_resolv_conf(tmp_path, "search example.com\noptions edns0\n")
        RootfsImagePreparer(tmp_path)._fixup_resolv_conf()
        self._assert_fixed_up(tmp_path)

    def test_broken_symlink_is_fixed(self, tmp_path: Path):
        etc = tmp_path / "etc"
        etc.mkdir()
        (etc / "resolv.conf").symlink_to("does-not-exist")
        RootfsImagePreparer(tmp_path)._fixup_resolv_conf()
        self._assert_fixed_up(tmp_path)

    def test_directory_is_replaced(self, tmp_path: Path):
        etc = tmp_path / "etc"
        etc.mkdir()
        bogus_dir = etc / "resolv.conf"
        bogus_dir.mkdir()
        (bogus_dir / "leftover").write_text("junk")
        RootfsImagePreparer(tmp_path)._fixup_resolv_conf()
        self._assert_fixed_up(tmp_path)

    def test_special_file_is_fixed(self, tmp_path: Path):
        """A non-regular file (e.g. a FIFO) at the path is removed and fixed."""
        resolv_conf = tmp_path / RESOLV_CONF_RELPATH
        resolv_conf.parent.mkdir(parents=True)
        os.mkfifo(resolv_conf)
        RootfsImagePreparer(tmp_path)._fixup_resolv_conf()
        self._assert_fixed_up(tmp_path)

    def test_undecodable_file_is_fixed(self, tmp_path: Path):
        resolv_conf = tmp_path / RESOLV_CONF_RELPATH
        resolv_conf.parent.mkdir(parents=True)
        resolv_conf.write_bytes(b"\xff\xfe\x00\x80 not utf-8")
        RootfsImagePreparer(tmp_path)._fixup_resolv_conf()
        self._assert_fixed_up(tmp_path)

    def test_valid_regular_file_is_untouched(self, tmp_path: Path):
        content = "# static config\nnameserver 8.8.8.8\nnameserver 8.8.4.4\n"
        resolv_conf = _write_resolv_conf(tmp_path, content)
        RootfsImagePreparer(tmp_path)._fixup_resolv_conf()

        assert not resolv_conf.is_symlink()
        assert resolv_conf.read_text() == content

    def test_symlink_to_valid_file_is_normalized(self, tmp_path: Path):
        """Any symlink, even one resolving to valid content, is normalized."""
        etc = tmp_path / "etc"
        etc.mkdir()
        (etc / "resolv.conf.real").write_text("nameserver 1.1.1.1\n")
        (etc / "resolv.conf").symlink_to("resolv.conf.real")

        RootfsImagePreparer(tmp_path)._fixup_resolv_conf()

        self._assert_fixed_up(tmp_path)
        # the symlink target file itself is left in place
        assert (etc / "resolv.conf.real").read_text() == "nameserver 1.1.1.1\n"

    def test_idempotent(self, tmp_path: Path):
        (tmp_path / "etc").mkdir()
        preparer = RootfsImagePreparer(tmp_path)
        preparer._fixup_resolv_conf()
        preparer._fixup_resolv_conf()  # running again must not raise
        self._assert_fixed_up(tmp_path)


class TestPrepare:
    """End-to-end tests for RootfsImagePreparer.prepare()."""

    def test_prepare_fixes_resolv_conf(self, tmp_path: Path):
        # a typical dirty rootfs: runtime dirs present, resolv.conf empty
        for d in ("dev", "proc", "sys", "run", "tmp", "etc"):
            (tmp_path / d).mkdir()
        _write_resolv_conf(tmp_path, "")

        RootfsImagePreparer(tmp_path).prepare()

        resolv_conf = tmp_path / RESOLV_CONF_RELPATH
        assert resolv_conf.is_symlink()
        assert os.readlink(resolv_conf) == RESOLV_CONF_SYMLINK_TARGET
        # runtime dirs are recreated (and emptied) by prepare()
        for d in ("dev", "proc", "sys", "run", "tmp"):
            assert (tmp_path / d).is_dir()

    def test_prepare_keeps_valid_resolv_conf(self, tmp_path: Path):
        for d in ("dev", "proc", "sys", "run", "tmp", "etc"):
            (tmp_path / d).mkdir()
        content = "nameserver 10.0.0.1\n"
        _write_resolv_conf(tmp_path, content)

        RootfsImagePreparer(tmp_path).prepare()

        resolv_conf = tmp_path / RESOLV_CONF_RELPATH
        assert not resolv_conf.is_symlink()
        assert resolv_conf.read_text() == content


class TestPrepareSysimgCmd:
    """Tests for the prepare_sysimg_cmd entrypoint."""

    def test_cmd_fixes_resolv_conf(self, tmp_path: Path):
        rootfs = tmp_path / "rootfs"
        (rootfs / "etc").mkdir(parents=True)
        # missing /etc/resolv.conf

        args = Namespace(rootfs_dir=str(rootfs), cleanup_pattern_file=None)
        prepare_sysimg_cmd(args)

        resolv_conf = rootfs / RESOLV_CONF_RELPATH
        assert resolv_conf.is_symlink()
        assert os.readlink(resolv_conf) == RESOLV_CONF_SYMLINK_TARGET

    def test_cmd_rejects_non_directory(self, tmp_path: Path):
        not_a_dir = tmp_path / "rootfs"
        not_a_dir.write_text("i am a file")

        args = Namespace(rootfs_dir=str(not_a_dir), cleanup_pattern_file=None)
        with pytest.raises(SystemExit):
            prepare_sysimg_cmd(args)
