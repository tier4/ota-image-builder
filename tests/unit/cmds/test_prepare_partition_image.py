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
"""Unit tests for cmds/prepare_partition_image.py: a rootfs tree in, the blobs and
the spec out. veritysetup is faked so the pipeline runs anywhere; the real one, where
installed, verifies the hash tree it wrote."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
from ota_image_tools.libs import bootimg

from ota_image_builder.cmds.prepare_partition_image import (
    prepare_partition_image_cmd_args,
)
from ota_image_builder.v1 import _partition_blobs as blobs
from ota_image_builder.v1._partition_image import PartitionPayloadSpec

HASH = "5f" * 32
FAKE_HASH_TREE = 4096

pytestmark = pytest.mark.skipif(
    shutil.which("mkfs.ext4") is None, reason="mkfs.ext4 not installed"
)


@pytest.fixture
def tree(tmp_path) -> Path:
    root = tmp_path / "rootfs"
    (root / "etc" / "initramfs-tools" / "hooks").mkdir(parents=True)
    (root / "etc" / "rootfs-version").write_text("1.2.0\n")
    (root / "etc" / "initramfs-tools" / "hooks" / "ota-verity").write_text(
        "# ota-functions\n"
    )
    (root / "boot").mkdir()
    (root / "boot" / "vmlinuz-6.8.0-1-generic").write_bytes(b"KERNEL" * 1000)
    (root / "boot" / "initrd.img-6.8.0-1-generic").write_bytes(b"INITRD" * 1000)
    os.symlink("vmlinuz-6.8.0-1-generic", root / "boot" / "vmlinuz")
    os.symlink("initrd.img-6.8.0-1-generic", root / "boot" / "initrd.img")
    (root / "usr" / "bin").mkdir(parents=True)
    (root / "usr" / "bin" / "true").write_bytes(b"\x7fELF" + b"\0" * 100)
    return root


@pytest.fixture
def fake_veritysetup(tmp_path, monkeypatch) -> None:
    """`format` appends a fake hash tree and prints a root hash the way the real one
    does; `verify` accepts anything."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "veritysetup"
    fake.write_text(
        "#!/bin/sh\n"
        'cmd="$1"; shift\n'
        'case "$cmd" in\n'
        "  format)\n"
        '    for a in "$@"; do case "$a" in --*) ;; *) f="$a";; esac; done\n'
        f'    head -c {FAKE_HASH_TREE} /dev/zero >> "$f"\n'
        '    echo "VERITY header information for $f"\n'
        '    echo "UUID:            	00000000-0000-0000-0000-000000000000"\n'
        '    echo "Hash type:       	1"\n'
        f'    echo "Root hash:      	{HASH}"\n'
        "    ;;\n"
        "  verify) exit 0 ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")


def run(*argv: str) -> None:
    parser = argparse.ArgumentParser()
    prepare_partition_image_cmd_args(parser.add_subparsers())
    args = parser.parse_args(["prepare-partition-image", *argv])
    args.handler(args)


VERSION_FILE = "/etc/rootfs-version"


def build(tree: Path, out: Path, *args: str, platform: str = "grub") -> None:
    run(
        "--platform", platform, "--rootfs-dir", str(tree), "--out", str(out),
        *(() if "--version" in args else ("--version-file", VERSION_FILE)), *args,
    )  # fmt: skip


def refused(capsys, *argv: str) -> str:
    with pytest.raises(SystemExit) as e:
        run(*argv)
    assert e.value.code == 1
    return capsys.readouterr().out


def boot_blob(out: Path) -> dict[str, bytes]:
    with tarfile.open(out / "boot.tar") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers()}  # type: ignore[union-attr]


def test_the_outputs_are_the_blobs_and_the_spec(tree, tmp_path, fake_veritysetup):
    out = tmp_path / "out"
    build(tree, out)

    assert sorted(p.name for p in out.iterdir()) == [
        "boot.tar",
        "rootfs.img",
        "rootfs.img.roothash",
        "spec.json",
    ]
    spec = json.loads((out / "spec.json").read_text())
    assert spec["delivery"] == "direct"
    assert spec["version"] == "1.2.0"
    by_name = {p["name"]: p for p in spec["partitions"]}
    assert set(by_name) == {"rootfs", "boot", "scratch", "identity", "optdata"}
    data_size = (out / "rootfs.img").stat().st_size - FAKE_HASH_TREE
    assert by_name["rootfs"] == {
        "name": "rootfs",
        "action": "write",
        "image": "rootfs.img",
        "filesystem": "ext4",
        "verity": {"root_hash": HASH, "hash_offset": data_size},
    }
    assert by_name["boot"] == {"name": "boot", "action": "write", "image": "boot.tar"}
    assert by_name["scratch"] == {"name": "scratch", "action": "mkfs"}
    assert by_name["identity"] == {"name": "identity", "action": "keep"}
    assert by_name["optdata"] == {"name": "optdata", "action": "keep"}
    for p in spec["partitions"]:
        if "image" in p:
            assert (out / p["image"]).is_file()
    # what add-partition-image reads is what was written
    model = PartitionPayloadSpec.model_validate_json((out / "spec.json").read_text())
    assert {p.name: p.blob_kind for p in model.partitions if p.image} == {
        "rootfs": "partition",
        "boot": "boot-files",
    }


def test_the_rootfs_blob_is_ext4_with_the_hash_tree_appended(
    tree, tmp_path, fake_veritysetup
):
    out = tmp_path / "out"
    build(tree, out)
    img = out / "rootfs.img"
    with open(img, "rb") as f:
        f.seek(0x438)
        assert f.read(2) == b"\x53\xef", "not an ext4 superblock"
    data_size = img.stat().st_size - FAKE_HASH_TREE
    assert data_size % (4 * 1024 * 1024) == 0, "a whole number of 4 MiB"
    assert (out / "rootfs.img.roothash").read_text().strip() == HASH


def test_the_filesystem_uuid_is_derived_from_the_version_string(
    tree, tmp_path, fake_veritysetup
):
    """s_uuid in the ext4 superblock is a function of the version the tree carries,
    the string itself: the newline the version file ends with is not part of it, so a
    build of this tree reproduces the image of every other build of it."""
    out = tmp_path / "out"
    build(tree, out)
    with open(out / "rootfs.img", "rb") as f:
        f.seek(1024 + 0x68)
        s_uuid = f.read(16).hex()
    assert s_uuid == blobs.derived_uuid("rootfs:1.2.0").replace("-", "")
    assert s_uuid != blobs.derived_uuid("rootfs:1.2.0\n").replace("-", "")


def test_the_boot_blob_binds_kernel_initrd_and_hash_to_the_slot_variables(
    tree, tmp_path, fake_veritysetup
):
    out = tmp_path / "out"
    build(tree, out, "--cmdline", "console=ttyS0,115200")
    blob = boot_blob(out)
    assert set(blob) == {"grub.cfg", "vmlinuz", "initrd.img"}
    assert blob["vmlinuz"] == b"KERNEL" * 1000
    assert blob["initrd.img"] == b"INITRD" * 1000

    cfg = blob["grub.cfg"].decode()
    data_size = (out / "rootfs.img").stat().st_size - FAKE_HASH_TREE
    assert "linux /${slot}/vmlinuz " in cfg
    assert "initrd /${slot}/initrd.img" in cfg
    assert "root=/dev/mapper/vroot ro rootfstype=ext4 verity=1" in cfg
    assert f"verityinfo=/dev/disk/by-partlabel/${{slot}}:{HASH}:{data_size}" in cfg
    assert "rw_overlay=/dev/disk/by-partlabel/${scratch}:/mnt/rw_overlay" in cfg
    assert "panic=10" in cfg
    assert "console=ttyS0,115200" in cfg
    assert "rootfs 1.2.0" in cfg
    # every member root-owned with a fixed time: two builds are one tar
    with tarfile.open(out / "boot.tar") as tar:
        for m in tar.getmembers():
            assert (m.uid, m.gid, m.mtime) == (0, 0, 946684800)


def test_the_kernel_is_found_without_the_symlinks(tree, tmp_path, fake_veritysetup):
    (tree / "boot" / "vmlinuz").unlink()
    (tree / "boot" / "initrd.img").unlink()
    (tree / "boot" / "vmlinuz-6.8.0-2-generic").write_bytes(b"NEWER")
    (tree / "boot" / "initrd.img-6.8.0-2-generic").write_bytes(b"NEWER-RD")
    out = tmp_path / "out"
    build(tree, out)
    blob = boot_blob(out)
    assert blob["vmlinuz"] == b"NEWER"
    assert blob["initrd.img"] == b"NEWER-RD"


def test_a_version_that_disagrees_with_the_tree_is_refused(
    tree, tmp_path, fake_veritysetup, capsys
):
    out = refused(
        capsys, "--platform", "grub", "--rootfs-dir", str(tree), "--out",
        str(tmp_path / "out"), "--version", "9.9.9", "--version-file", VERSION_FILE,
    )  # fmt: skip
    assert "says 1.2.0" in out
    assert not (tmp_path / "out" / "rootfs.img").exists()


def test_the_version_comes_from_the_caller_or_a_file_it_names(
    tree, tmp_path, fake_veritysetup, capsys
):
    """Which file in the tree holds the version is the platform installer's
    convention, named by the caller; this tool has none of its own."""
    out = refused(
        capsys, "--platform", "grub", "--rootfs-dir", str(tree), "--out", str(tmp_path / "out")
    )  # fmt: skip
    assert "--version" in out and "--version-file" in out
    (tree / "etc" / "rootfs-version").unlink()
    out = refused(
        capsys, "--platform", "grub", "--rootfs-dir", str(tree), "--out", str(tmp_path / "out"),
        "--version-file", VERSION_FILE,
    )  # fmt: skip
    assert "not in the tree" in out
    build(tree, tmp_path / "out", "--version", "2.0.0")
    assert (
        json.loads((tmp_path / "out" / "spec.json").read_text())["version"] == "2.0.0"
    )


def test_a_tree_unpacked_without_root_is_refused(
    tree, tmp_path, fake_veritysetup, capsys
):
    """A tree extracted by an ordinary user has every file owned by that user and no
    setuid bits, and mkfs.ext4 -d copies that into the image: the device then boots
    with a sudo that refuses to run. The tree's sudo is the tell."""
    sudo = tree / "usr" / "bin" / "sudo"
    sudo.write_bytes(b"\x7fELF" + b"\0" * 100)
    sudo.chmod(0o755)
    out = refused(
        capsys,
        "--platform",
        "grub",
        "--rootfs-dir",
        str(tree),
        "--version-file",
        VERSION_FILE,
        "--out",
        str(tmp_path / "out"),
    )
    assert "unpacked without root" in out
    assert not (tmp_path / "out" / "rootfs.img").exists()


def test_a_tree_without_a_kernel_is_refused(tree, tmp_path, fake_veritysetup, capsys):
    for p in (tree / "boot").iterdir():
        p.unlink()
    out = refused(
        capsys,
        "--platform",
        "grub",
        "--rootfs-dir",
        str(tree),
        "--version-file",
        VERSION_FILE,
        "--out",
        str(tmp_path / "out"),
    )
    assert "no kernel" in out


def test_the_panic_seconds_reach_the_cmdline(tree, tmp_path, fake_veritysetup):
    out = tmp_path / "out"
    build(tree, out, "--panic", "0")
    assert "panic=0" in boot_blob(out)["grub.cfg"].decode()


@pytest.mark.skipif(
    shutil.which("veritysetup") is None, reason="veritysetup not installed"
)
def test_the_real_hash_tree_verifies(tree, tmp_path):
    out = tmp_path / "out"
    build(tree, out)
    root_hash = (out / "rootfs.img.roothash").read_text().strip()
    cfg = boot_blob(out)["grub.cfg"].decode()
    offset = cfg.split(f":{root_hash}:", 1)[1].split()[0]
    img = str(out / "rootfs.img")
    subprocess.run(
        ["veritysetup", "verify", img, img, root_hash, f"--hash-offset={offset}"],
        check=True,
        capture_output=True,
    )


def test_data_images_from_the_built_directory_are_listed_in_the_spec(
    tree, tmp_path, fake_veritysetup
):
    """Every <name>.spec.json build-data-images left goes into spec.json and its image
    beside it. Nothing here knows an image by name."""
    data = tmp_path / "data"
    data.mkdir()
    for name in ("ml_package", "maps"):
        (data / f"{name}.img").write_bytes(b"\0" * 8192)
        (data / f"{name}.spec.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "version": "1",
                    "mount": f"/opt/{name}",
                    "image": f"{name}.img",
                    "filesystem": "squashfs",
                    "verity": {"root_hash": HASH, "hash_offset": 4096},
                }
            )
        )
    out = tmp_path / "out"
    build(tree, out, "--data-images", str(data))

    spec = json.loads((out / "spec.json").read_text())
    assert sorted(d["name"] for d in spec["data_images"]) == ["maps", "ml_package"]
    assert all(d["verity"]["root_hash"] == HASH for d in spec["data_images"])
    assert (out / "ml_package.img").stat().st_size == 8192
    assert (out / "maps.img").stat().st_size == 8192
    assert [p["name"] for p in spec["partitions"]] == [
        "rootfs",
        "boot",
        "scratch",
        "identity",
        "optdata",
    ]


def test_a_data_images_directory_without_any_adds_none(
    tree, tmp_path, fake_veritysetup, caplog
):
    empty = tmp_path / "none"
    empty.mkdir()
    with caplog.at_level(logging.INFO):
        build(tree, tmp_path / "out", "--data-images", str(empty))
    assert "no data images in" in caplog.text
    assert "data_images" not in json.loads((tmp_path / "out" / "spec.json").read_text())


def test_two_builds_of_one_tree_are_one_image(tree, tmp_path, fake_veritysetup):
    """The filesystem UUID, the superblock times and the directory hash seed are
    derived, not drawn. A different version is a different image."""
    first, second = tmp_path / "one", tmp_path / "two"
    build(tree, first)
    build(tree, second)
    a, b = (first / "rootfs.img").read_bytes(), (second / "rootfs.img").read_bytes()
    assert hashlib.sha256(a).hexdigest() == hashlib.sha256(b).hexdigest()
    assert (first / "boot.tar").read_bytes() == (second / "boot.tar").read_bytes()
    (tree / "etc" / "rootfs-version").write_text("1.2.1\n")
    third = tmp_path / "three"
    build(tree, third, "--version", "1.2.1")
    assert (third / "rootfs.img").read_bytes() != a


# ------ l4t ------ #


@pytest.fixture
def l4t_tree(tmp_path) -> Path:
    root = tmp_path / "l4t"
    (root / "etc").mkdir(parents=True)
    (root / "etc" / "rootfs-version").write_text("3.0.0\n")
    (root / "boot").mkdir()
    (root / "boot" / "Image").write_bytes(b"IMAGE" * 1000)
    (root / "boot" / "initrd").write_bytes(b"RD" * 1000)
    (root / "boot" / "tegra234-orin.dtb").write_bytes(b"DTB" * 100)
    return root


def test_the_l4t_boot_blob_and_the_flash_images(l4t_tree, tmp_path, fake_veritysetup):
    out, flash = tmp_path / "out", tmp_path / "flash"
    build(l4t_tree, out, "--installer-out", str(flash), platform="l4t")
    blob = boot_blob(out)
    assert set(blob) == {"Image", "board.dtb", "cmdline", "initrd"}
    cmdline = blob["cmdline"].decode()
    data_size = (out / "rootfs.img").stat().st_size - FAKE_HASH_TREE
    assert "\n" not in cmdline
    assert f"verityinfo=/dev/disk/by-partlabel/${{slot}}:{HASH}:{data_size}" in cmdline
    assert "console=ttyTCU0,115200" in cmdline, "the board's own arguments"
    assert sorted(p.name for p in flash.iterdir()) == [
        "board.dtb",
        "boot_a.img",
        "boot_b.img",
        "rootfs.img",
    ]
    for slot, part, scratch in (("a", "APP", "scratch_a"), ("b", "APP_b", "scratch_b")):
        packed = (flash / f"boot_{slot}.img").read_bytes()
        resolved = bootimg.read_cmdline(packed)
        assert f"by-partlabel/{part}:{HASH}:" in resolved
        assert f"by-partlabel/{scratch}:/mnt/rw_overlay" in resolved
    assert os.path.samefile(flash / "rootfs.img", out / "rootfs.img")


def test_the_firmware_capsule_is_linked_and_named_in_the_spec(
    l4t_tree, tmp_path, fake_veritysetup
):
    cap = tmp_path / "TEGRA_BL_3701.Cap"
    cap.write_bytes(b"\xed\xd5\xcb\x6d" + b"\x00" * 60)
    out = tmp_path / "out"
    build(
        l4t_tree, out, "--firmware", str(cap), "--firmware-version", "39.2.0",
        "--firmware-name", "T4-L4T-BSP", platform="l4t",
    )  # fmt: skip
    spec = json.loads((out / "spec.json").read_text())
    assert spec["firmware"] == {
        "name": "T4-L4T-BSP",
        "version": "39.2.0",
        "format": "nvidia-l4t.uefi-capsule.v1",
        "file": "TEGRA_BL_3701.Cap",
    }
    assert (out / "TEGRA_BL_3701.Cap").read_bytes() == cap.read_bytes()


@pytest.mark.parametrize(
    ("extra", "why"),
    [
        (["--firmware", "CAP"], "--firmware needs --firmware-version"),
        (["--firmware-version", "39.2.0"], "--firmware-version without --firmware"),
        (["--firmware", "MISSING", "--firmware-version", "1"], "no such file"),
    ],
)
def test_a_firmware_option_that_cannot_work_is_refused_before_the_image_is_built(
    l4t_tree, tmp_path, capsys, extra, why
):
    """Before the tree is looked at: an argument error should not wait for mkfs."""
    cap = tmp_path / "x.Cap"
    cap.write_bytes(b"x")
    argv = [
        "--platform", "l4t", "--rootfs-dir", str(l4t_tree), "--out", str(tmp_path / "out"),
        *[str(cap) if a == "CAP" else str(tmp_path / "none.Cap") if a == "MISSING" else a for a in extra],
    ]  # fmt: skip
    assert why in refused(capsys, *argv)
    assert not (tmp_path / "out" / "rootfs.img").exists()


def test_firmware_is_l4t_s(tree, tmp_path, capsys):
    cap = tmp_path / "x.Cap"
    cap.write_bytes(b"x")
    out = refused(
        capsys, "--platform", "grub", "--rootfs-dir", str(tree), "--out", str(tmp_path / "out"),
        "--firmware", str(cap), "--firmware-version", "1",
    )  # fmt: skip
    assert "--firmware is l4t's" in out


# ------ a vendor package ------ #


def test_a_vendor_package_spec(tmp_path, capsys):
    pkg = tmp_path / "image.pkg"
    pkg.write_bytes(b"PKG")
    out = tmp_path / "out"
    run(
        "--vendor-package", str(pkg), "--format", "vendor.package.v1",
        "--version", "1.0.0", "--out", str(out),
    )  # fmt: skip
    spec = json.loads((out / "spec.json").read_text())
    assert spec["delivery"] == "vendor-package"
    assert spec["package"] == {"file": "image.pkg", "format": "vendor.package.v1"}
    assert [p["action"] for p in spec["partitions"]] == [
        "write",
        "write",
        "mkfs",
        "keep",
        "keep",
    ]
    assert "image" not in spec["partitions"][0]
    assert (out / "image.pkg").read_bytes() == b"PKG"
    assert "--format" in refused(
        capsys, "--vendor-package", str(pkg), "--version", "1.0.0", "--out", str(out)
    )
    assert "no --rootfs-dir, --platform" in refused(
        capsys, "--vendor-package", str(pkg), "--format", "x", "--version", "1.0.0",
        "--platform", "grub", "--out", str(out),
    )  # fmt: skip
    assert "--platform is required" in refused(
        capsys, "--rootfs-dir", str(tmp_path), "--version", "1.0.0", "--out", str(out)
    )
