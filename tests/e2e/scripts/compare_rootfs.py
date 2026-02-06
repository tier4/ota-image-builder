#!/usr/bin/env python3
"""Compare two rootfs directories for equality, including xattrs."""

from __future__ import annotations

import argparse
import os
import stat
import sys
from dataclasses import dataclass, fields
from hashlib import sha256
from itertools import chain
from pathlib import Path
from typing import Optional

READ_SIZE = 8 * 1024 * 1024  # 8MiB


def exit_with_msg(_msg: str, exit_code=1):
    print(_msg, file=sys.stderr)
    sys.exit(exit_code)


def get_xattrs(path: Path) -> tuple[tuple[str, bytes], ...]:
    """Get all extended attributes for a path."""
    try:
        attrs = {}
        for name in os.listxattr(path, follow_symlinks=False):
            attrs[name] = os.getxattr(path, name, follow_symlinks=False)
        return tuple((k, v) for k, v in attrs.items())
    except OSError:
        # Path doesn't support xattrs, for example, a symlink
        return ()


@dataclass
class FileInfo:
    """File metadata including mode, uid, gid, and xattrs."""

    mode: int
    file_type: int
    uid: int
    gid: int
    xattrs: tuple[tuple[str, bytes], ...]
    sha256digest: Optional[bytes] = None
    symlinktarget: Optional[str] = None


file_info_fields = [_fi.name for _fi in fields(FileInfo)]


def get_file_info(path: Path) -> FileInfo:
    """Get file metadata including mode, uid, gid, xattrs,
    and sha256digest for regular file."""
    st = path.lstat()
    st_mode = st.st_mode

    sha256digest = None
    if stat.S_ISREG(st_mode):
        _hasher = sha256()
        with open(path, "rb") as _src:
            while chunk := _src.read(READ_SIZE):
                _hasher.update(chunk)
        sha256digest = _hasher.digest()

    symlink_target = None
    if stat.S_ISLNK(st_mode):
        symlink_target = os.readlink(path)

    return FileInfo(
        mode=stat.S_IMODE(st_mode),
        file_type=stat.S_IFMT(st_mode),
        uid=st.st_uid,
        gid=st.st_gid,
        xattrs=get_xattrs(path),
        sha256digest=sha256digest,
        symlinktarget=symlink_target,
    )


def compare_path(_relative: Path, left_root: Path, right_root: Path) -> int:
    """Compare two files and print differences. Returns count of differences."""
    diff_count = 0

    left_info = get_file_info(left_root / _relative)
    right_info = get_file_info(right_root / _relative)
    for _fn in file_info_fields:
        _left_v, _right_v = getattr(left_info, _fn), getattr(right_info, _fn)
        if _left_v != _right_v:
            print(f"Found diff for {_fn}: {_left_v} != {_right_v}")
    return diff_count


def compare_rootfs(left_rootfs: Path, right_rootfs: Path) -> int:
    """Compare two rootfs directories and print differences. Returns count of differences."""
    diff_count = 0
    left_paths: set[str] = set()

    # compare the right side from left side
    for curdir, dnames, fnames in os.walk(left_rootfs, followlinks=False):
        _relative_curdir = Path(curdir).relative_to(left_rootfs)
        for _name in chain(dnames, fnames):
            _relative_path = _relative_curdir / _name
            left_paths.add(str(_relative_path))
            diff_count += compare_path(_relative_path, left_rootfs, right_rootfs)

    # check right side doesn't have files only present in the right side
    for curdir, dnames, fnames in os.walk(right_rootfs, followlinks=False):
        _relative_curdir = Path(curdir).relative_to(right_rootfs)
        for _name in chain(dnames, fnames):
            _relative_path = str(_relative_curdir / _name)
            if _relative_path not in left_paths:
                print(f"Found paths only presented at right side: {_relative_path}")

    return diff_count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("left_rootfs", type=Path)
    parser.add_argument("right_rootfs", type=Path)
    args = parser.parse_args()

    left_rootfs: Path = args.left_rootfs.resolve()
    right_rootfs: Path = args.right_rootfs.resolve()

    print(f"Comparing {left_rootfs} vs {right_rootfs}")
    diff_count = compare_rootfs(left_rootfs, right_rootfs)
    if diff_count > 0:
        exit_with_msg(f"\nFound {diff_count} difference(s).")
    exit_with_msg("\nDirectories are identical.", 0)


if __name__ == "__main__":
    main()
