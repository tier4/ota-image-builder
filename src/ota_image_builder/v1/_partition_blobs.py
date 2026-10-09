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
"""The blobs of a partition-based payload, built from a rootfs tree, and the spec
JSON `add-partition-image` reads (see the README):

    rootfs.img           ext4 image of the tree with its dm-verity hash tree appended
    boot.tar             the slot's boot files, carrying the root hash on the kernel
                         command line (a GRUB menuentry fragment, or the L4T kernel,
                         initramfs, DTB and command line template)
    <name>.img/.env/.spec.json   one data image: a squashfs with its hash tree, and
                         what the device and the spec need beside it
    spec.json            roles, never devices

Reproducible by construction: the filesystem UUID, the directory hash seed, the
superblock times, the verity salt and UUID and every tar timestamp are fixed or
derived from the version, never drawn. Two builds of one tree are one image, so that
an installer medium and a release agree, the first delta applies, and a rebuilt but
unchanged data image keeps its digest (which keys its cache on the device).

External tools, as the platforms ship them: mkfs.ext4 (e2fsprogs), veritysetup
(cryptsetup-bin), mksquashfs (squashfs-tools), du.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

from ota_image_libs.v1.partition_image.schema import DeliveryMode, PartitionAction

from ota_image_builder.v1._partition_image import (
    DataImageSpec,
    FirmwareSpec,
    PartitionPayloadSpec,
    PartitionSpec,
    VendorPackageSpec,
    VeritySpec,
    VersionRangeSpec,
)

logger = logging.getLogger(__name__)

EXT4_HASH_SEED = "6f7461e4-0000-4000-8000-726f6f746673"
VERITY_SALT = "7469657234206f746120726f6f7466732076657269747920736f6c74203031"
REPRODUCIBLE_EPOCH = 1704067200
"""2024-01-01T00:00:00Z: what e2fsprogs writes as the filesystem's times."""
TAR_MTIME = 946684800
"""2000-01-01T00:00:00Z: every member of a boot files tar."""
VERITY_BLOCK = 4096
KEPT_ROLES = ("identity", "optdata")
L4T_KERNEL_PARTITION_BYTES = 128 * 1024 * 1024
"""A_kernel and B_kernel in the flash layout. Fixed there, not here."""
L4T_FIRMWARE_FORMAT = "nvidia-l4t.uefi-capsule.v1"
"""The one format the L4T agent applies: a UEFI capsule for the bootloader chain."""
L4T_ARGS = (
    "rootwait mminit_loglevel=4 console=ttyTCU0,115200 console=ttyAMA0,115200 "
    "firmware_class.path=/etc/firmware fbcon=map:0 video=efifb:off console=tty0 "
    "efi_pstore.pstore_disable=1 pstore.backend=ramoops efi=runtime "
    "nvme.use_threaded_interrupts=1 swiotlb=2048 pci=pcie_bus_perf"
)
"""What the board needs on the command line, taken from a working device's
/proc/cmdline with root= and rw dropped: the root is named by the initramfs, and it
is read-only."""

NAME_RE = re.compile(r"^[A-Za-z0-9._+-]+$")
DATA_IMAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
DATA_IMAGE_VERSION_RE = re.compile(r"^[A-Za-z0-9._+:/-]+$")


class BlobBuildError(Exception):
    """Refused before, or failed during, the build; the message says which."""


# ------ external tools ------ #


def require_tools(*names: str) -> None:
    hints = {
        "mkfs.ext4": "e2fsprogs",
        "veritysetup": "cryptsetup-bin",
        "mksquashfs": "squashfs-tools",
        "du": "coreutils",
    }
    for name in names:
        if shutil.which(name) is None:
            raise BlobBuildError(f"{name} is required ({hints.get(name, name)})")


def _run(cmd: list[str], *, env: dict[str, str] | None = None) -> str:
    """One external tool, its stdout; a failure names the tool and what it said."""
    try:
        res = subprocess.run(cmd, check=True, capture_output=True, text=True, env=env)
    except subprocess.CalledProcessError as e:
        raise BlobBuildError(
            f"{cmd[0]} failed: {(e.stderr or e.stdout or '').strip()}"
        ) from e
    return res.stdout


def warn_if_not_root() -> None:
    if os.geteuid() != 0:
        logger.warning(
            "not running as root; files in the image will be owned by this user, not by "
            "their owners in the tree"
        )


# ------ the tree ------ #


def check_tree_ownership(tree: Path) -> None:
    """A tree unpacked without root is owned by whoever unpacked it and has no setuid
    bits; mkfs.ext4 -d copies that as it finds it, and the image boots with no working
    sudo (seen on a device). The tree's sudo is the tell."""
    for rel in ("usr/bin/sudo", "usr/bin/su", "bin/su"):
        p = tree / rel
        if not p.is_file():
            continue
        st = p.stat()
        if st.st_uid != 0 or not st.st_mode & stat.S_ISUID:
            raise BlobBuildError(
                f"{p} is owned by uid {st.st_uid} with mode {stat.S_IMODE(st.st_mode):o}, "
                "not root and setuid: the tree was unpacked without root (use tar "
                "--numeric-owner as root), and this image would have no working sudo"
            )


def read_version(
    tree: Path, version: str | None, version_file: str | None = None
) -> str:
    """The version the payload carries. `version_file` names a file in the tree, as the
    device sees it (`/etc/...`), holding the version the device will report; `version`
    must agree with it when both are given, and one of the two is required."""
    baked = ""
    if version_file:
        path = tree / version_file.lstrip("/")
        if not path.is_file():
            raise BlobBuildError(f"--version-file {version_file} is not in the tree")
        baked = "".join(path.read_text().split())
        if not baked:
            raise BlobBuildError(f"--version-file {version_file} is empty")
        if version and version != baked:
            raise BlobBuildError(
                f"--version {version} but the tree's {version_file} says {baked}; the "
                "device would report the latter"
            )
        version = baked
    if not version:
        raise BlobBuildError(
            "pass --version, or --version-file naming the file in the tree that holds it"
        )
    if not NAME_RE.match(version):
        raise BlobBuildError("the version may contain only letters, digits, . _ + -")
    return version


def _tree_size_mib(tree: Path) -> int:
    return int(_run(["du", "-sm", "--apparent-size", str(tree)]).split()[0])


# ------ images with verity ------ #


def derived_uuid(seed: str) -> str:
    """A UUID that is a function of its seed, where a tool would draw one. Not an
    identity anyone looks up (the layout finds partitions by label), only bytes that
    must not change between two builds of the same thing."""
    h = hashlib.sha256(seed.encode()).hexdigest()[:32]
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def append_verity_tree(img: Path, data_size: int, seed: str | None = None) -> str:
    """The hash tree after the data; the root hash returned and written beside the
    image as <img>.roothash. The UUID comes from `seed` when given (the rootfs: its
    version), else from the digest of the data (a data image)."""
    if seed is None:
        h = hashlib.sha256()
        with img.open("rb") as f:
            left = data_size
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                h.update(chunk)
                left -= len(chunk)
        seed = h.hexdigest()
    out = _run(
        [
            "veritysetup",
            "format",
            f"--data-block-size={VERITY_BLOCK}",
            f"--hash-block-size={VERITY_BLOCK}",
            f"--hash-offset={data_size}",
            f"--salt={VERITY_SALT}",
            f"--uuid={derived_uuid(f'verity:{seed}')}",
            str(img),
            str(img),
        ]
    )
    m = re.search(r"^Root hash:\s*([0-9a-fA-F]+)", out, re.M)
    if not m:
        raise BlobBuildError(f"veritysetup format printed no root hash:\n{out}")
    root_hash = m.group(1)
    Path(f"{img}.roothash").write_text(root_hash + "\n")
    return root_hash


def make_verity_rootfs_image(
    tree: Path, img: Path, size_mib: int | None = None, *, version: str
) -> tuple[int, str]:
    """The tree as an ext4 image with its hash tree appended: (where the hash tree
    starts, the root hash). `version` seeds the UUIDs: one version and one tree make
    one image."""
    img.unlink(missing_ok=True)
    if size_mib is None:
        # A read-only verity root needs no room to grow: the headroom covers mkfs
        # overhead and small files rounding up to 4 KiB blocks, which du's apparent
        # size does not count. Every spare MiB is written to the slot and carried.
        used = _tree_size_mib(tree)
        size_mib = used + used // 8 + 64
    if size_mib <= 0:
        raise BlobBuildError("--size must be a number of MiB")
    # 4 MiB multiples keep the data size a multiple of the verity block.
    size_mib = (size_mib + 3) // 4 * 4
    logger.info(f"{img.name}: {size_mib} MiB ext4 from {tree}")
    with img.open("wb") as f:
        f.truncate(size_mib * 1024 * 1024)
    uuid_seed = version
    # -d populates from the tree. No journal (the root is read-only under verity), a
    # fixed directory hash seed, and a UUID and superblock times derived rather than
    # drawn: two builds of one tree must be one image.
    _run(
        [
            "mkfs.ext4",
            "-F",
            "-q",
            "-b",
            "4096",
            "-L",
            "rootfs",
            "-O",
            "^has_journal",
            "-U",
            derived_uuid(f"rootfs:{uuid_seed}"),
            "-E",
            f"lazy_itable_init=0,lazy_journal_init=0,hash_seed={EXT4_HASH_SEED}",
            "-d",
            str(tree),
            str(img),
        ],  # fmt: skip
        env={**os.environ, "E2FSPROGS_FAKE_TIME": str(REPRODUCIBLE_EPOCH)},
    )
    data_size = img.stat().st_size
    logger.info(f"verity: hash tree at offset {data_size}")
    root_hash = append_verity_tree(img, data_size, f"rootfs:{uuid_seed}")
    logger.info(f"verity: root hash {root_hash}")
    return data_size, root_hash


def build_data_image(
    source_dir: Path,
    out: Path,
    name: str,
    version: str,
    mount: str,
    *,
    component: str | None = None,
    requires: dict[str, VersionRangeSpec] | None = None,
) -> None:
    """One data image: the directory as a read-only squashfs with its hash tree
    appended, plus <name>.env (what the device reads at boot and the installer
    installs beside the built-in copy) and <name>.spec.json (the spec entry). All
    four outputs go together; a failure leaves none of them."""
    if not source_dir.is_dir():
        raise BlobBuildError(f"no such directory: {source_dir}")
    if not DATA_IMAGE_NAME_RE.match(name) or name in (".", ".."):
        raise BlobBuildError("--name must be a directory name")
    if not DATA_IMAGE_VERSION_RE.match(version):
        raise BlobBuildError("--version may contain only letters, digits, . _ + : / -")
    if not mount.startswith("/") or len(mount) < 2 or any(c in mount for c in " \t\n'"):
        raise BlobBuildError(
            "--mount must be an absolute path and may not contain whitespace or quotes"
        )
    if component is not None and not DATA_IMAGE_NAME_RE.match(component):
        raise BlobBuildError(
            "--component must be a component name (letters, digits, . _ -)"
        )
    require_tools("mksquashfs", "veritysetup")
    out.mkdir(parents=True, exist_ok=True)
    img = out / f"{name}.img"
    outputs = (
        img,
        Path(f"{img}.roothash"),
        out / f"{name}.env",
        out / f"{name}.spec.json",
    )
    for p in outputs:
        p.unlink(missing_ok=True)
    try:
        # Uncompressed, padded to 4 KiB, every entry root-owned with one fixed time, the
        # root directory's mode fixed too, and no xattrs: the bytes depend on the files
        # alone. A block diff then catches a changed file at its 4 KiB blocks.
        _run(
            [
                "mksquashfs",
                str(source_dir),
                str(img),
                "-noappend",
                "-no-xattrs",
                "-all-root",
                "-root-mode",
                "0755",
                "-noI",
                "-noD",
                "-noF",
                "-noX",
                "-mkfs-time",
                "0",
                "-all-time",
                "0",
                "-no-progress",
                "-quiet",
            ]  # fmt: skip
        )
        data_size = img.stat().st_size
        if data_size % VERITY_BLOCK:
            raise BlobBuildError(
                f"{img} is not a multiple of {VERITY_BLOCK} bytes; verity needs whole blocks"
            )
        logger.info(
            f"{img.name}: {data_size // (1024 * 1024)} MiB squashfs from {source_dir}"
        )
        root_hash = append_verity_tree(img, data_size)
        logger.info(f"verity: hash tree at offset {data_size}, root hash {root_hash}")
        digest = hashlib.sha256(img.read_bytes()).hexdigest()
        env_lines = [
            f"NAME='{name}'",
            f"VERSION='{version}'",
            f"MOUNT='{mount}'",
            f"IMAGE_DIGEST='{digest}'",
            f"IMAGE_SIZE='{img.stat().st_size}'",
            f"ROOT_HASH='{root_hash}'",
            f"HASH_OFFSET='{data_size}'",
        ]
        if component:
            env_lines.append(f"COMPONENT='{component}'")
        (out / f"{name}.env").write_text("\n".join(env_lines) + "\n")
        entry = DataImageSpec(
            name=name,
            version=version,
            mount=mount,
            image=f"{name}.img",
            filesystem="squashfs",
            verity=VeritySpec(root_hash=root_hash, hash_offset=data_size),
            requires=requires or {},
        )
        (out / f"{name}.spec.json").write_text(_dump(entry) + "\n")
    except BaseException:
        for p in outputs:
            p.unlink(missing_ok=True)
        raise
    logger.info(f"wrote {out / f'{name}.env'} and {out / f'{name}.spec.json'}")


def parse_requires(specs: list[str]) -> dict[str, VersionRangeSpec]:
    """`WHAT=MIN:MAX`, either bound optional: which rootfs (or other data image)
    versions a data image goes with."""
    out: dict[str, VersionRangeSpec] = {}
    for arg in specs:
        what, sep, bounds = arg.partition("=")
        if not sep or not what:
            raise BlobBuildError(f"--requires takes <what>=<min>:<max>, not {arg!r}")
        lo, _, hi = bounds.partition(":")
        out[what] = VersionRangeSpec(min=lo or None, max=hi or None)
    return out


# ------ boot files ------ #


def _reproducible_tar(out: Path, members: dict[str, bytes | Path]) -> None:
    with tarfile.open(out, "w", format=tarfile.GNU_FORMAT) as tar:
        for name in sorted(members):
            data = members[name]
            payload = data if isinstance(data, bytes) else data.read_bytes()
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mtime = TAR_MTIME
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, _Bytes(payload))


class _Bytes:
    def __init__(self, data: bytes) -> None:
        self._view = memoryview(data)
        self._pos = 0

    def read(self, n: int = -1) -> bytes:
        if n < 0:
            n = len(self._view) - self._pos
        chunk = self._view[self._pos : self._pos + n].tobytes()
        self._pos += len(chunk)
        return chunk


def _verity_cmdline(root_hash: str, data_size: int, panic: int) -> str:
    return (
        f"verityinfo=/dev/disk/by-partlabel/${{slot}}:{root_hash}:{data_size} "
        "rw_overlay=/dev/disk/by-partlabel/${scratch}:/mnt/rw_overlay "
        f"panic={panic}"
    )


def _resolve_in_boot(tree: Path, p: Path) -> Path | None:
    """A path or symlink under <tree>/boot, resolved inside the tree."""
    if p.is_symlink():
        target = os.readlink(p)
        p = tree / target.lstrip("/") if target.startswith("/") else p.parent / target
    return p if p.is_file() else None


def find_grub_kernel(
    tree: Path, kernel: str | None, initrd: str | None
) -> tuple[Path, Path]:
    """<tree>/boot/vmlinuz and initrd.img, else the newest vmlinuz-* and its initrd."""
    k = Path(kernel) if kernel else _resolve_in_boot(tree, tree / "boot" / "vmlinuz")
    if k is None:
        found = sorted(
            (tree / "boot").glob("vmlinuz-*") if (tree / "boot").is_dir() else [],
            key=lambda p: _version_key(p.name),
        )
        found = [p for p in found if p.is_file()]
        k = found[-1] if found else None
    if k is None or not k.is_file():
        raise BlobBuildError(f"no kernel found under {tree / 'boot'}; pass --kernel")
    i = Path(initrd) if initrd else _resolve_in_boot(tree, tree / "boot" / "initrd.img")
    if i is None and k.name.startswith("vmlinuz-"):
        candidate = tree / "boot" / f"initrd.img-{k.name[len('vmlinuz-') :]}"
        i = candidate if candidate.is_file() else None
    if i is None or not i.is_file():
        raise BlobBuildError(
            f"no initramfs found to match {k} under {tree / 'boot'}; pass --initrd"
        )
    hook = tree / "etc" / "initramfs-tools" / "hooks" / "ota-verity"
    if not (hook.is_file() and "ota-functions" in hook.read_text(errors="replace")):
        logger.warning(
            f"the tree has no ota-verity initramfs hook; unless {i} was built with it, "
            "the kernel cannot open /dev/mapper/vroot"
        )
    return k, i


def _version_key(name: str) -> list:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


def grub_boot_tar(
    out: Path,
    kernel: Path,
    initrd: Path,
    *,
    name: str,
    version: str,
    root_hash: str,
    data_size: int,
    panic: int,
    cmdline_extra: str,
) -> None:
    """vmlinuz, initrd.img and grub.cfg: the slot's boot directory, written whole into
    /boot/rootfs_x/. grub.cfg is the menuentry body /boot/grub/grub.cfg sources with
    ${slot} and ${scratch} set; the root hash binds it to rootfs.img of this build."""
    cmdline = f"root=/dev/mapper/vroot ro rootfstype=ext4 verity=1 {_verity_cmdline(root_hash, data_size, panic)}"
    if cmdline_extra:
        cmdline += f" {cmdline_extra}"
    cfg = (
        f"# {name} {version} — sourced by /boot/grub/grub.cfg with ${{slot}} and ${{scratch}} set.\n"
        "# The root hash binds this boot directory to rootfs.img of the same build; a rootfs\n"
        "# written without its boot blob, or the other way round, does not boot.\n"
        f'echo "Loading {name} {version} from ${{slot}}"\n'
        f"linux /${{slot}}/vmlinuz {cmdline}\n"
        "initrd /${slot}/initrd.img\n"
    )
    _reproducible_tar(
        out, {"grub.cfg": cfg.encode(), "initrd.img": initrd, "vmlinuz": kernel}
    )
    logger.info(
        f"boot.tar: vmlinuz ({kernel.stat().st_size} bytes), initrd.img "
        f"({initrd.stat().st_size} bytes), grub.cfg"
    )


def find_l4t_dtb(tree: Path, dtb: str | None) -> Path:
    """The board DTB the image was built to boot with: extlinux.conf names it while the
    tree still has one; after install-platform.sh has removed it, the only tegra DTB
    in /boot. More than one and the build must be told which."""
    if dtb:
        p = Path(dtb)
    else:
        p = None
        extlinux = tree / "boot" / "extlinux" / "extlinux.conf"
        if extlinux.is_file():
            m = re.search(
                r"^\s*FDT\s+(\S+)", extlinux.read_text(errors="replace"), re.M
            )
            if m:
                p = tree / m.group(1).lstrip("/")
        if p is None:
            found = (
                sorted((tree / "boot").glob("tegra*.dtb"))
                if (tree / "boot").is_dir()
                else []
            )
            if len(found) != 1:
                raise BlobBuildError(
                    f"expected one tegra*.dtb in {tree / 'boot'}, found {len(found)}; pass --dtb "
                    "(the board says which it is: cat /proc/device-tree/model)"
                )
            p = found[0]
    if not p.is_file():
        raise BlobBuildError(f"no DTB at {p}; pass --dtb")
    return p


def l4t_boot_tar(
    out: Path,
    kernel: Path,
    initrd: Path,
    dtb: Path,
    *,
    root_hash: str,
    data_size: int,
    panic: int,
    cmdline_extra: str,
) -> str:
    """Image, initrd, the board DTB and the command line template: the agent packs the
    Android boot image on the device, after substituting ${slot} and ${scratch}, so the
    same blob goes to whichever kernel partition is standby. Returns the template."""
    cmdline = f"root=/dev/mapper/vroot ro rootfstype=ext4 {_verity_cmdline(root_hash, data_size, panic)} {L4T_ARGS}"
    if cmdline_extra:
        cmdline += f" {cmdline_extra}"
    cmdline = " ".join(
        cmdline.split()
    )  # one line: it goes into a fixed-width header field
    _reproducible_tar(
        out,
        {
            "Image": kernel,
            "board.dtb": dtb,
            "cmdline": cmdline.encode(),
            "initrd": initrd,
        },
    )
    logger.info(
        f"boot.tar: Image ({kernel.stat().st_size} bytes), initrd ({initrd.stat().st_size} "
        "bytes), board.dtb, cmdline"
    )
    return cmdline


def pack_l4t_installer_images(
    installer_out: Path,
    kernel: Path,
    initrd: Path,
    dtb: Path,
    cmdline: str,
    rootfs_img: Path,
) -> None:
    """What a flash writes: boot_a.img and boot_b.img (the packed boot images for the
    two kernel partitions), board.dtb and a link to rootfs.img. An update packs its own
    on the device; a flash writes both slots and runs where there is no Python."""
    # Only this step packs a boot image, so only this step needs the packer: a libs
    # release without it still serves every other command.
    try:
        from ota_image_tools.libs import bootimg
    except ImportError as e:
        raise BlobBuildError(
            "--installer-out needs ota_image_tools.libs.bootimg (ota-image-libs v0.7.0 or later)"
        ) from e
    installer_out.mkdir(parents=True, exist_ok=True)
    k, r = kernel.read_bytes(), initrd.read_bytes()
    for slot, part, scratch in (("a", "APP", "scratch_a"), ("b", "APP_b", "scratch_b")):
        resolved = cmdline.replace("${slot}", part).replace("${scratch}", scratch)
        try:
            packed = bootimg.pack(k, r, resolved)
        except bootimg.BootImageError as e:
            raise BlobBuildError(str(e)) from e
        (installer_out / f"boot_{slot}.img").write_bytes(packed)
    shutil.copyfile(dtb, installer_out / "board.dtb")
    _link_or_copy(rootfs_img, installer_out / "rootfs.img", symlink_ok=True)
    size = (installer_out / "boot_a.img").stat().st_size
    used = size * 100 // L4T_KERNEL_PARTITION_BYTES
    logger.info(
        f"boot image: {size} bytes, {used}% of the "
        f"{L4T_KERNEL_PARTITION_BYTES // (1024 * 1024)} MiB kernel partition"
    )
    if size > L4T_KERNEL_PARTITION_BYTES:
        raise BlobBuildError(
            "the boot image does not fit the kernel partition; no device could install this"
        )
    if used >= 80:
        logger.warning("little room left in the kernel partition")


def _link_or_copy(src: Path, dst: Path, *, symlink_ok: bool = False) -> None:
    if dst.exists() and os.path.samefile(src, dst):
        return
    dst.unlink(missing_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        if symlink_ok:
            os.symlink(src.resolve(), dst)
        else:
            shutil.copyfile(src, dst)


# ------ the spec ------ #


def _dump(model) -> str:
    return model.model_dump_json(indent=2, by_alias=True, exclude_defaults=True)


def write_spec(out: Path, spec: PartitionPayloadSpec) -> None:
    (out / "spec.json").write_text(_dump(spec) + "\n")


def load_spec(out: Path) -> PartitionPayloadSpec:
    return PartitionPayloadSpec.model_validate_json((out / "spec.json").read_text())


def direct_spec(version: str, root_hash: str, data_size: int) -> PartitionPayloadSpec:
    """Roles, never devices; identity and optdata are listed so that the contract
    says, in the image itself, that they are kept."""
    return PartitionPayloadSpec(
        delivery=DeliveryMode.direct,
        version=version,
        partitions=[
            PartitionSpec(
                name="rootfs",
                action=PartitionAction.write,
                image="rootfs.img",
                filesystem="ext4",
                verity=VeritySpec(root_hash=root_hash, hash_offset=data_size),
            ),
            PartitionSpec(name="boot", action=PartitionAction.write, image="boot.tar"),
            PartitionSpec(name="scratch", action=PartitionAction.mkfs),
            *(PartitionSpec(name=n, action=PartitionAction.keep) for n in KEPT_ROLES),
        ],
    )


def vendor_spec(
    version: str, package_file: str, package_format: str
) -> PartitionPayloadSpec:
    """A vendor package the platform's own updater applies: the roles it writes name
    no image of their own."""
    return PartitionPayloadSpec(
        delivery=DeliveryMode.vendor_package,
        version=version,
        package=VendorPackageSpec(file=package_file, format=package_format),
        partitions=[
            PartitionSpec(name="rootfs", action=PartitionAction.write),
            PartitionSpec(name="boot", action=PartitionAction.write),
            PartitionSpec(name="scratch", action=PartitionAction.mkfs),
            *(PartitionSpec(name=n, action=PartitionAction.keep) for n in KEPT_ROLES),
        ],
    )


def add_data_images(out: Path, data_dir: Path) -> int:
    """Every <name>.spec.json `build-data-images` wrote there goes into spec.json's
    data_images, the image linked beside spec.json. The list is the product's:
    nothing here knows an image by name."""
    if not data_dir.is_dir():
        raise BlobBuildError(f"--data-images: no such directory: {data_dir}")
    spec = load_spec(out)
    n = 0
    for entry_path in sorted(data_dir.glob("*.spec.json")):
        prefix = entry_path.with_name(entry_path.name[: -len(".spec.json")])
        img = Path(f"{prefix}.img")
        if not img.is_file():
            raise BlobBuildError(
                f"--data-images: no {img} beside {entry_path} (build-data-images writes them)"
            )
        entry = DataImageSpec.model_validate_json(entry_path.read_text())
        _link_or_copy(img, out / f"{entry.name}.img")
        spec.data_images = [d for d in spec.data_images if d.name != entry.name] + [
            entry
        ]
        logger.info(f"spec.json: data image {entry.name} ({entry.version})")
        n += 1
    if n == 0:
        logger.info(f"spec.json: no data images in {data_dir}")
    write_spec(out, spec)
    return n


def add_firmware(out: Path, name: str, version: str, fmt: str, file: Path) -> None:
    """The platform firmware's package (on L4T the UEFI capsule), copied beside the
    spec and named as the payload's `firmware` entry."""
    if not file.is_file():
        raise BlobBuildError(f"--firmware: no such file: {file}")
    if not version:
        raise BlobBuildError(
            "--firmware needs --firmware-version (what the firmware will report, e.g. 39.2.0)"
        )
    if not NAME_RE.match(name):
        raise BlobBuildError(
            "--firmware-name may contain only letters, digits, . _ + -"
        )
    _link_or_copy(file, out / file.name)
    spec = load_spec(out)
    spec.firmware = FirmwareSpec(name=name, version=version, format=fmt, file=file.name)
    write_spec(out, spec)
    logger.info(
        f"spec.json: firmware {name} {version} ({fmt}, {file.name}, {file.stat().st_size} bytes)"
    )
