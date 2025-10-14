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

from __future__ import annotations

import logging
import re
import shutil
from itertools import chain
from pathlib import Path
from typing import TYPE_CHECKING

from ota_image_builder._common import check_if_valid_ota_image, exit_with_err_msg

if TYPE_CHECKING:
    from argparse import ArgumentParser, Namespace, _SubParsersAction


logger = logging.getLogger(__name__)

# Dirs that we should cleanup, but the dir itself should be preserved.
DEFAULT_IGNORE_DIRS_SHOULD_PRESERVED = [
    # dynamic mount points at boot
    "/dev",
    "/proc",
    "/sys",
    # directories only hold runtime tmp files
    "/run",
    "/tmp",
]

# Entries that we should completely remove.
DEFAULT_IGNORE_ENTRIES_REMOVED = [
    "/lost+found",
    "/.dockerenv",
]

DEFAULT_EXTRA_IGNORE_PATTERNS = [
    "/var/log/**/*.log*",
    "/var/log/**/*.journal*",
    "/var/log/dmesg*",
    "/var/log/lastlog",
    "/var/log/syslog",
    "/var/log/syslog.*",
]

BACKWARD_COMPAT_PA = [
    re.compile(r"^/?home/autoware/[\w\*]+/build"),
    re.compile(r"^/?boot/ota[/\w\.]*")
]

GLOB_SPECIAL_CHARS = re.compile(r"[\*\?\[\]\|]")


class RootfsImagePreparer:
    def __init__(
        self,
        rootfs_dir: Path,
        *,
        extra_ignore_patterns: list[str] | None = None,
    ) -> None:
        self._rootfs_dir = rootfs_dir
        self._extra_ignore_patterns = extra_ignore_patterns or []

        self._extra_ignore_patterns.extend(DEFAULT_EXTRA_IGNORE_PATTERNS)

    def _delete_one_entry(self, path: Path) -> None:
        path = self._rootfs_dir / path
        if not path.is_symlink() and path.is_dir():
            logger.debug(f"Removing directory: {path}")
            shutil.rmtree(path, ignore_errors=True)
        else:
            logger.debug(f"Removing non-directory: {path}")
            path.unlink(missing_ok=True)

    def _process_cleanup(self) -> None:
        """Cleanup the system rootfs image."""
        for entry in chain(
            DEFAULT_IGNORE_DIRS_SHOULD_PRESERVED, DEFAULT_IGNORE_ENTRIES_REMOVED
        ):
            path = self._rootfs_dir / entry.lstrip("/")
            self._delete_one_entry(path)

        for entry in self._extra_ignore_patterns:
            if entry.startswith("/"):
                entry = entry.lstrip("/")
                if not GLOB_SPECIAL_CHARS.search(entry):  # for exact matching
                    path = self._rootfs_dir / entry
                    self._delete_one_entry(path)
                    continue
                for path in self._rootfs_dir.glob(entry):
                    self._delete_one_entry(path)
            else:
                for path in self._rootfs_dir.rglob(entry):
                    self._delete_one_entry(path)

    def _prepare_image(self) -> None:
        for entry in DEFAULT_IGNORE_DIRS_SHOULD_PRESERVED:
            entry_path = self._rootfs_dir / entry.lstrip("/")
            entry_path.mkdir(exist_ok=True)

    def prepare(self) -> None:
        """Prepare the system rootfs image."""
        logger.info(f"Preparing system rootfs image: {self._rootfs_dir}")
        self._process_cleanup()
        self._prepare_image()
        logger.info("System rootfs image prepared successfully.")


def prepare_sysimg_cmd_args(
    sub_arg_parser: _SubParsersAction[ArgumentParser], *parent_parser: ArgumentParser
) -> None:
    prepare_sysimg_cmd_args = sub_arg_parser.add_parser(
        name="prepare-sysimg",
        help=(
            _help_txt
            := "Prepare a system image to make it ready for adding into OTA image. "
            "This command will do some basic cleanup to the system rootfs image, "
            "optionally do further cleanup with `--cleanup-pattern-file` specified."
        ),
        description=_help_txt,
        parents=parent_parser,
    )
    prepare_sysimg_cmd_args.add_argument(
        "--cleanup-pattern-file",
        help="The file which holds a list of glob patterns.",
    )
    prepare_sysimg_cmd_args.add_argument(
        "--rootfs-dir",
        help="The folder which holds a system rootfs image.",
        required=True,
    )
    prepare_sysimg_cmd_args.set_defaults(handler=prepare_sysimg_cmd)


def prepare_sysimg_cmd(args: Namespace) -> None:
    logger.debug(f"calling {prepare_sysimg_cmd.__name__} with {args}")
    rootfs_dir = Path(args.rootfs_dir)
    if check_if_valid_ota_image(rootfs_dir):
        exit_with_err_msg(
            f"{rootfs_dir} is an OTA image dir! It should be a system rootfs image directory."
        )

    if not rootfs_dir.is_dir():
        exit_with_err_msg(f"{rootfs_dir} is not a directory!")

    _extra_patterns = None
    if _pattern_f := args.cleanup_pattern_file:
        _pattern_f = Path(_pattern_f)
        if not _pattern_f.is_file():
            exit_with_err_msg(
                f"Extra pattern file is specified, but {_pattern_f} is not a file!"
            )
        _extra_patterns = _pattern_f.read_text().splitlines()

    _preparer = RootfsImagePreparer(
        rootfs_dir,
        extra_ignore_patterns=_extra_patterns,
    )
    _preparer.prepare()
