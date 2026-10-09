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


def prepare_partition_image_cmd_args(
    sub_arg_parser: _SubParsersAction[ArgumentParser], *parent_parser: ArgumentParser
) -> None:
    parser = sub_arg_parser.add_parser(
        name="prepare-partition-image",
        help=(
            _help_txt := "Build the blobs of a partition-based payload from a rootfs "
            "tree -- the rootfs image with its verity hash tree, the slot's boot files, "
            "the data images -- or wrap a vendor package, and write the spec JSON "
            "add-partition-image reads"
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
    parser.add_argument("--rootfs-dir", help="The rootfs tree the image is built from.")
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
        "the device will report: the payload carries that version.",
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
        help="l4t: the board DTB (default: the one <tree>/boot/extlinux names, else the "
        "only tegra*.dtb).",
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
        help="l4t: the platform firmware package to carry beside the partitions (the "
        "UEFI capsule for the bootloader chain), linked beside spec.json and named in it.",
    )
    parser.add_argument(
        "--firmware-version",
        help="What the firmware will report once applied; required with --firmware.",
    )
    parser.add_argument(
        "--firmware-name",
        default="l4t-bsp",
        help="The firmware entry's name in the payload.",
    )
    parser.add_argument(
        "--vendor-package",
        help="A package the platform's own updater applies, linked beside spec.json as the "
        "payload: no rootfs tree, no --platform.",
    )
    parser.add_argument(
        "--format", help="The vendor package's format string, opaque to this tool."
    )
    parser.set_defaults(handler=prepare_partition_image_cmd)


def _check_args(args: Namespace) -> None:
    """Argument errors first: before the tree is looked at and before twenty minutes
    of mkfs."""
    if args.vendor_package:
        if not args.format:
            exit_with_err_msg("--vendor-package takes --format")
        if not args.version:
            exit_with_err_msg("--vendor-package needs --version")
        if args.rootfs_dir or args.platform or args.version_file:
            exit_with_err_msg(
                "--vendor-package takes no --rootfs-dir, --platform or --version-file: "
                "the package is the payload"
            )
        if not Path(args.vendor_package).is_file():
            exit_with_err_msg(f"--vendor-package: no such file: {args.vendor_package}")
        return
    if args.format:
        exit_with_err_msg("--format goes with --vendor-package")
    if not args.rootfs_dir:
        exit_with_err_msg("--rootfs-dir is required (or --vendor-package)")
    if not args.platform:
        exit_with_err_msg("--platform is required with --rootfs-dir")
    if args.firmware:
        if args.platform != "l4t":
            exit_with_err_msg(
                "--firmware is l4t's: x86_64 has no updater to hand a package to"
            )
        if not Path(args.firmware).is_file():
            exit_with_err_msg(f"--firmware: no such file: {args.firmware}")
        if not args.firmware_version:
            exit_with_err_msg(
                "--firmware needs --firmware-version (what the firmware will report, e.g. 39.2.0)"
            )
    elif args.firmware_version:
        exit_with_err_msg("--firmware-version without --firmware")
    if args.installer_out and args.platform != "l4t":
        exit_with_err_msg("--installer-out is l4t's: the flash input")
    if args.dtb and args.platform != "l4t":
        exit_with_err_msg("--dtb is l4t's")
    if not blobs.NAME_RE.match(args.name):
        exit_with_err_msg("--name may contain only letters, digits, . _ + -")
    if args.panic < 0:
        exit_with_err_msg("--panic must be a number of seconds")
    if not (args.version or args.version_file):
        exit_with_err_msg(
            "pass --version, or --version-file naming the file in the tree that holds it"
        )


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
    out.mkdir(parents=True, exist_ok=True)
    package = Path(args.vendor_package)
    blobs._link_or_copy(package, out / package.name)
    blobs.write_spec(out, blobs.vendor_spec(args.version, package.name, args.format))
    if args.data_images:
        blobs.add_data_images(out, Path(args.data_images))
    logger.info(
        f"spec.json: {package.name} as the {args.format} package, version {args.version}"
    )


def _direct(args: Namespace, out: Path) -> None:
    tree = Path(args.rootfs_dir)
    if not tree.is_dir():
        raise blobs.BlobBuildError(f"no such directory: {tree}")
    blobs.check_tree_ownership(tree)
    blobs.require_tools("mkfs.ext4", "veritysetup", "du")
    blobs.warn_if_not_root()
    version = blobs.read_version(tree, args.version, args.version_file)
    if args.platform == "grub":
        kernel, initrd = blobs.find_grub_kernel(tree, args.kernel, args.initrd)
        dtb = None
    else:
        kernel = Path(args.kernel) if args.kernel else tree / "boot" / "Image"
        initrd = Path(args.initrd) if args.initrd else tree / "boot" / "initrd"
        if not kernel.is_file():
            raise blobs.BlobBuildError(f"no kernel at {kernel}; pass --kernel")
        if not initrd.is_file():
            raise blobs.BlobBuildError(f"no initramfs at {initrd}; pass --initrd")
        dtb = blobs.find_l4t_dtb(tree, args.dtb)

    out.mkdir(parents=True, exist_ok=True)
    img = out / "rootfs.img"
    data_size, root_hash = blobs.make_verity_rootfs_image(
        tree, img, args.size, version=version
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
            args.firmware_name,
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
