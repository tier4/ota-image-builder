# Copyright 2026 TIER IV, INC. All rights reserved.
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
"""`prepare-partition-image`: the blobs and the spec of a partition-based payload from
a rootfs tree (`ota_image_builder.v1._partition_blobs`), for `add-partition-image`."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ota_image_builder._common import exit_with_err_msg
from ota_image_builder.v1 import _partition_blobs as blobs

if TYPE_CHECKING:
    from argparse import ArgumentParser, Namespace, _SubParsersAction

logger = logging.getLogger(__name__)

PLATFORMS = ("grub", "l4t")
L4T_FIRMWARE_NAME = "l4t-bsp"


def prepare_partition_image_cmd_args(
    sub_arg_parser: _SubParsersAction[ArgumentParser], *parent_parser: ArgumentParser
) -> None:
    parser = sub_arg_parser.add_parser(
        name="prepare-partition-image",
        help=(
            _help_txt := "Build the blobs of a partition-based payload from a rootfs "
            "tree -- the rootfs image with its verity hash tree, the slot's boot files, "
            "the data images, the firmware -- or wrap a vendor package with its data "
            "images and firmware, and write the spec JSON add-partition-image reads"
        ),
        description=_help_txt
        + ". Reproducible: two builds of one tree are one image. Run as root, since "
        "mkfs.ext4 -d copies the tree's ownership as it finds it.",
        parents=parent_parser,
    )
    parser.add_argument(
        "--platform",
        choices=PLATFORMS,
        help="What the boot files are: grub, x86_64 (vmlinuz, initrd and a GRUB "
        "menuentry fragment); l4t, Jetson (Image, initrd, the board DTB and a command "
        "line template). Required with --rootfs-dir; not for a vendor package.",
    )
    parser.add_argument(
        "--rootfs-dir",
        help="The rootfs tree the image is built from. With --vendor-package, the tree "
        "the package was built from, read for --version-file and nothing else.",
    )
    parser.add_argument(
        "--out", required=True, help="Where the blobs and spec.json are written."
    )
    parser.add_argument(
        "--version",
        help="The payload's version. Required unless --version-file names it; must "
        "agree with it when both are given.",
    )
    parser.add_argument(
        "--version-file",
        help="A file in the tree, as the device sees it (/etc/...), holding the version "
        "the device will report, one word: the payload carries that version.",
    )
    parser.add_argument(
        "--name",
        default="rootfs",
        help="What the boot files call the image (the GRUB entry's echo line).",
    )
    parser.add_argument(
        "--kernel",
        help="The kernel for the boot files (default: found under <tree>/boot).",
    )
    parser.add_argument(
        "--initrd",
        help="The initramfs for the boot files (default: found under <tree>/boot).",
    )
    parser.add_argument(
        "--dtb",
        help="l4t: the board DTB (default: the one in <tree>/boot/dtb, else the only "
        "tegra*.dtb in <tree>/boot; an extlinux.conf left in the tree must name the "
        "same one). Taken as given: extlinux.conf is not checked then.",
    )
    parser.add_argument(
        "--size",
        type=int,
        help="The ext4 image's size before the hash tree, in MiB (default: the tree's size plus an eighth).",
    )
    parser.add_argument(
        "--cmdline", default="", help="Extra kernel command line, appended."
    )
    parser.add_argument(
        "--panic",
        type=int,
        default=10,
        help="panic= on the kernel command line: how long the initramfs waits before "
        "rebooting on failure (0 drops to a shell instead).",
    )
    parser.add_argument(
        "--data-images",
        help="The directory build-data-images wrote: each image is linked beside "
        "spec.json and listed in it.",
    )
    parser.add_argument(
        "--installer-out",
        help="l4t: also write there what a flash writes: boot_a.img and boot_b.img, "
        "board.dtb and a link to rootfs.img.",
    )
    parser.add_argument(
        "--firmware",
        help="The platform firmware package to carry beside the partitions, linked "
        "beside spec.json and named in it: on l4t the UEFI capsule for the bootloader "
        "chain; with --vendor-package a package in --firmware-format (not on grub).",
    )
    parser.add_argument(
        "--firmware-version",
        help="What the firmware will report once applied; required with --firmware.",
    )
    parser.add_argument(
        "--firmware-name",
        help="The firmware entry's name in the payload, what a campaign addresses it "
        f"by (default on l4t: {L4T_FIRMWARE_NAME}; required with --vendor-package).",
    )
    parser.add_argument(
        "--firmware-format",
        help="The firmware package's format string, opaque to this tool; required with "
        "--firmware and --vendor-package (l4t's is the UEFI capsule's, "
        f"{blobs.L4T_FIRMWARE_FORMAT}).",
    )
    parser.add_argument(
        "--vendor-package",
        help="A package the platform's own updater applies, linked beside spec.json as the "
        "payload: no --platform, and --rootfs-dir only to read --version-file.",
    )
    parser.add_argument(
        "--format", help="The vendor package's format string, opaque to this tool."
    )
    parser.set_defaults(handler=prepare_partition_image_cmd)


def _check_args(args: Namespace) -> None:
    """Argument errors first: before the tree is looked at and before twenty minutes
    of mkfs."""
    _check_firmware(args)
    if args.vendor_package:
        _check_vendor_args(args)
        return
    if args.format:
        exit_with_err_msg("--format goes with --vendor-package")
    if not args.rootfs_dir:
        exit_with_err_msg("--rootfs-dir is required (or --vendor-package)")
    if not args.platform:
        exit_with_err_msg("--platform is required with --rootfs-dir")
    if args.installer_out and args.platform != "l4t":
        exit_with_err_msg("--installer-out is l4t's: the flash input")
    if args.dtb and args.platform != "l4t":
        exit_with_err_msg("--dtb is l4t's")
    if not blobs.NAME_RE.match(args.name):
        exit_with_err_msg("--name may contain only letters, digits, . _ + -")
    if args.panic < 0:
        exit_with_err_msg("--panic must be a number of seconds")
    try:
        blobs.check_cmdline_extra(args.cmdline)
    except blobs.BlobBuildError as e:
        exit_with_err_msg(str(e))
    if not (args.version or args.version_file):
        exit_with_err_msg(
            "pass --version, or --version-file naming the file in the tree that holds it"
        )


def _check_firmware(args: Namespace) -> None:
    """--firmware and what goes with it, under either delivery: the payload's firmware
    entry is the same whether the agent writes the partitions or a vendor package does."""
    if not args.firmware:
        given = [
            opt
            for opt, value in (
                ("--firmware-version", args.firmware_version),
                ("--firmware-name", args.firmware_name),
                ("--firmware-format", args.firmware_format),
            )
            if value
        ]
        if given:
            exit_with_err_msg(f"{', '.join(given)} without --firmware")
        return
    if args.platform == "grub":
        exit_with_err_msg(
            "--firmware is not grub's: x86_64 has no updater to hand a package to"
        )
    if not Path(args.firmware).is_file():
        exit_with_err_msg(f"--firmware: no such file: {args.firmware}")
    if not args.firmware_version:
        exit_with_err_msg(
            "--firmware needs --firmware-version (what the firmware will report, e.g. 39.2.0)"
        )
    if args.vendor_package:
        # The platform's updater is not this tool's to know: its format and the name a
        # campaign addresses the firmware by come from the caller, as --format does.
        if not args.firmware_format:
            exit_with_err_msg(
                "--firmware with --vendor-package needs --firmware-format (the format "
                "the platform's updater takes, opaque to this tool)"
            )
        if not args.firmware_name:
            exit_with_err_msg(
                "--firmware with --vendor-package needs --firmware-name (what a "
                "campaign addresses the firmware by)"
            )
    elif args.firmware_format:
        exit_with_err_msg(
            "--firmware-format goes with --vendor-package: l4t's firmware is the UEFI "
            f"capsule ({blobs.L4T_FIRMWARE_FORMAT})"
        )
    if not blobs.NAME_RE.match(args.firmware_name or L4T_FIRMWARE_NAME):
        exit_with_err_msg("--firmware-name may contain only letters, digits, . _ + -")


def _check_vendor_args(args: Namespace) -> None:
    if not args.format:
        exit_with_err_msg("--vendor-package takes --format")
    # Refused, not dropped: each of these shapes the agent's own boot files or image,
    # and the platform's tooling built those into the package already.
    given = [
        opt
        for opt, value in (
            ("--platform", args.platform),
            ("--cmdline", args.cmdline),
            ("--kernel", args.kernel),
            ("--initrd", args.initrd),
            ("--dtb", args.dtb),
            ("--size", args.size),
            ("--installer-out", args.installer_out),
        )
        if value
    ]
    if given:
        exit_with_err_msg(
            f"--vendor-package takes no {', '.join(given)}: the package is the payload"
        )
    if args.version_file and not args.rootfs_dir:
        exit_with_err_msg(
            "--version-file with --vendor-package needs --rootfs-dir: the tree the "
            "package was built from"
        )
    if args.rootfs_dir and not args.version_file:
        exit_with_err_msg(
            "--rootfs-dir with --vendor-package is read for --version-file alone; pass "
            "it, or --version without the tree"
        )
    if not (args.version or args.version_file):
        exit_with_err_msg(
            "--vendor-package needs --version, or --rootfs-dir and --version-file"
        )
    if not Path(args.vendor_package).is_file():
        exit_with_err_msg(f"--vendor-package: no such file: {args.vendor_package}")


def prepare_partition_image_cmd(args: Namespace) -> None:
    logger.debug(f"calling {prepare_partition_image_cmd.__name__} with {args}")
    _check_args(args)
    out = Path(args.out)
    try:
        if args.vendor_package:
            _vendor(args, out)
        else:
            _direct(args, out)
    except blobs.BlobBuildError as e:
        exit_with_err_msg(str(e))


def _vendor(args: Namespace, out: Path) -> None:
    version = args.version
    if args.rootfs_dir:
        # Only the version file is read: the package was built from this tree, and the
        # device reports what the tree's file says, so the payload must carry that.
        tree = Path(args.rootfs_dir).resolve()
        if not tree.is_dir():
            raise blobs.BlobBuildError(f"no such directory: {args.rootfs_dir}")
        version = blobs.read_version(tree, args.version, args.version_file)
    out.mkdir(parents=True, exist_ok=True)
    package = Path(args.vendor_package)
    blobs._link_or_copy(package, out / package.name)
    blobs.write_spec(out, blobs.vendor_spec(version, package.name, args.format))
    if args.data_images:
        blobs.add_data_images(out, Path(args.data_images))
    if args.firmware:
        blobs.add_firmware(
            out,
            args.firmware_name,
            args.firmware_version,
            args.firmware_format,
            Path(args.firmware),
        )
    logger.info(
        f"spec.json: {package.name} as the {args.format} package, version {version}"
    )


def _direct(args: Namespace, out: Path) -> None:
    # Resolved: a CI hands over its rsync'd export through a symlink, and `du` on a
    # symlink measures the link, which sized the image at 68 MiB for a 1.6 GB tree.
    tree = Path(args.rootfs_dir).resolve()
    if not tree.is_dir():
        raise blobs.BlobBuildError(f"no such directory: {args.rootfs_dir}")
    blobs.check_tree_ownership(tree)
    blobs.require_tools("mkfs.ext4", "veritysetup", "du")
    blobs.warn_if_not_root()
    version = blobs.read_version(tree, args.version, args.version_file)
    if args.platform == "grub":
        kernel, initrd = blobs.find_grub_kernel(tree, args.kernel, args.initrd)
        dtb = None
    else:
        kernel = (
            Path(args.kernel) if args.kernel else blobs.tree_file(tree, "/boot/Image")
        )
        initrd = (
            Path(args.initrd) if args.initrd else blobs.tree_file(tree, "/boot/initrd")
        )
        if kernel is None or not kernel.is_file():
            raise blobs.BlobBuildError(f"no kernel at {tree}/boot/Image; pass --kernel")
        if initrd is None or not initrd.is_file():
            raise blobs.BlobBuildError(
                f"no initramfs at {tree}/boot/initrd; pass --initrd"
            )
        dtb = blobs.find_l4t_dtb(tree, args.dtb)

    out.mkdir(parents=True, exist_ok=True)
    img = out / "rootfs.img"
    size_mib = blobs.rootfs_image_size_mib(tree, args.size)
    if args.platform == "l4t":
        # The template's length is known before the root hash is: refuse a command
        # line the boot image cannot hold before minutes of mkfs, not after.
        blobs.check_l4t_cmdline_fits(
            blobs.l4t_cmdline(
                "0" * 64,
                size_mib * 1024 * 1024,
                panic=args.panic,
                cmdline_extra=args.cmdline,
            ),
            args.cmdline,
        )
    data_size, root_hash = blobs.make_verity_rootfs_image(
        tree, img, size_mib, version=version
    )

    if args.platform == "grub":
        blobs.grub_boot_tar(
            out / "boot.tar", kernel, initrd,
            name=args.name, version=version, root_hash=root_hash, data_size=data_size,
            panic=args.panic, cmdline_extra=args.cmdline,
        )  # fmt: skip
    else:
        assert dtb is not None
        cmdline = blobs.l4t_boot_tar(
            out / "boot.tar", kernel, initrd, dtb,
            root_hash=root_hash, data_size=data_size, panic=args.panic, cmdline_extra=args.cmdline,
        )  # fmt: skip
        if args.installer_out:
            blobs.pack_l4t_installer_images(
                Path(args.installer_out), kernel, initrd, dtb, cmdline, img
            )

    blobs.write_spec(out, blobs.direct_spec(version, root_hash, data_size))
    if args.data_images:
        blobs.add_data_images(out, Path(args.data_images))
    if args.firmware:
        blobs.add_firmware(
            out,
            args.firmware_name or L4T_FIRMWARE_NAME,
            args.firmware_version,
            blobs.L4T_FIRMWARE_FORMAT,
            Path(args.firmware),
        )
    logger.info(f"built {args.name} {version} in {out}:")
    for f in (img, Path(f"{img}.roothash"), out / "boot.tar", out / "spec.json"):
        logger.info(f"  {f.stat().st_size:>12}  {f}")
    logger.info(
        f"verify:  veritysetup verify {img} {img} {root_hash} --hash-offset={data_size}"
    )
    logger.info(
        f"next:    ota-image-builder add-partition-image --spec {out / 'spec.json'} ... <image root>"
    )
