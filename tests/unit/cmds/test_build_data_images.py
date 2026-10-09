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
"""Unit tests for cmds/build_data_images.py: the data image list is the product's
file, and nothing here knows an image by name. mksquashfs and veritysetup are stubbed,
so these say what the tooling does with the list -- builds every entry, takes the
files out of the tree, refuses a bad list before touching anything -- not what
squashfs does."""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

import pytest

from ota_image_builder.cmds.build_data_images import build_data_images_cmd_args
from ota_image_builder.v1 import _partition_blobs as blobs

HASH = "5f" * 32

FAKE_MKSQUASHFS = """#!/bin/sh
# <source> <image> [options]: the files as a tar, padded to whole 4 KiB blocks
src="$1"; out="$2"
tar -C "$src" --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner -cf "$out.tar" .
size=$(stat -c %s "$out.tar"); pad=$(( (4096 - size % 4096) % 4096 ))
{ cat "$out.tar"; head -c "$pad" /dev/zero; } > "$out"
rm -f "$out.tar"
"""
FAKE_VERITYSETUP = f"""#!/bin/sh
cmd="$1"; shift
case "$cmd" in
  format)
    for a in "$@"; do case "$a" in --*) ;; *) f="$a";; esac; done
    head -c 4096 /dev/zero >> "$f"
    echo "VERITY header information for $f"
    echo "Root hash:      	{HASH}"
    ;;
  verify) exit 0 ;;
  *) exit 2 ;;
esac
"""

CONFIG = """\
# what this ECU carries beside its rootfs
data_images:
  - name: ml_package
    mount: /opt/autoware/mlmodels
    version_file: /opt/autoware/mlmodels/VERSION
    requires:
      rootfs: {min: "2.0.0"}
    component: T4-ML-PACKAGE
  - name: maps
    mount: /opt/autoware/maps
    source: /srv/maps
    version: tokyo-2026.09
"""


@pytest.fixture
def fake_tools(tmp_path, monkeypatch) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (
        ("mksquashfs", FAKE_MKSQUASHFS),
        ("veritysetup", FAKE_VERITYSETUP),
    ):
        fake = bindir / name
        fake.write_text(body)
        fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return bindir


def make_tree(tmp_path, name="rootfs") -> Path:
    """What the image build left: the models where Autoware reads them, the maps
    somewhere else, and an empty directory where the maps are to be mounted."""
    root = tmp_path / name
    (root / "opt/autoware/mlmodels/centerpoint").mkdir(parents=True)
    (root / "opt/autoware/mlmodels/centerpoint/model.onnx").write_bytes(b"ONNX" * 2000)
    (root / "opt/autoware/mlmodels/VERSION").write_text("xx1/2.6.1\n")
    (root / "srv/maps").mkdir(parents=True)
    (root / "srv/maps/lanelet2_map.osm").write_bytes(b"<osm/>" * 500)
    (root / "opt/autoware/maps").mkdir(parents=True)
    return root


def run(*argv: str) -> None:
    parser = argparse.ArgumentParser()
    build_data_images_cmd_args(parser.add_subparsers())
    args = parser.parse_args(["build-data-images", *argv])
    args.handler(args)


def build(tree: Path, out: Path, config_text: str, *args: str) -> None:
    cfg = out.parent / f"data_images-{out.name}.yaml"
    cfg.write_text(config_text)
    run("--config", str(cfg), "--rootfs-dir", str(tree), "--out", str(out), *args)


def refused(capsys, tree: Path, out: Path, config_text: str, *args: str) -> str:
    with pytest.raises(SystemExit) as e:
        build(tree, out, config_text, *args)
    assert e.value.code == 1
    return capsys.readouterr().out


def test_every_listed_image_is_built_and_its_files_leave_the_tree(
    tmp_path, fake_tools, caplog
):
    tree = make_tree(tmp_path)
    out = tmp_path / "out"
    with caplog.at_level(logging.INFO):
        build(tree, out, CONFIG)

    for name in ("ml_package", "maps"):
        for suffix in (".img", ".env", ".spec.json"):
            assert (out / f"{name}{suffix}").is_file(), f"{name}{suffix}"
    ml_env = (out / "ml_package.env").read_text()
    assert "NAME='ml_package'" in ml_env
    assert "VERSION='xx1/2.6.1'" in ml_env, (
        "read from version_file before the files went"
    )
    assert "MOUNT='/opt/autoware/mlmodels'" in ml_env
    assert "COMPONENT='T4-ML-PACKAGE'" in ml_env, "the eSync name the campaign uses"
    assert "COMPONENT=" not in (out / "maps.env").read_text(), (
        "named like the image: none"
    )
    ml = json.loads((out / "ml_package.spec.json").read_text())
    assert ml["requires"] == {"rootfs": {"min": "2.0.0"}}
    assert ml["verity"] == {
        "root_hash": HASH,
        "hash_offset": (out / "ml_package.img").stat().st_size - 4096,
    }
    maps = json.loads((out / "maps.spec.json").read_text())
    assert maps["version"] == "tokyo-2026.09"
    assert maps["mount"] == "/opt/autoware/maps"
    assert "requires" not in maps
    assert list((tree / "opt/autoware/mlmodels").iterdir()) == []
    assert list((tree / "srv/maps").iterdir()) == []
    assert (tree / "opt/autoware/maps").is_dir(), "the mount point stays for the device"
    assert "built 2 data image(s)" in caplog.text
    assert "--data-images" in caplog.text, "says what consumes the directory next"


def test_a_version_override_wins_over_the_list(tmp_path, fake_tools):
    tree = make_tree(tmp_path)
    out = tmp_path / "out"
    build(
        tree,
        out,
        CONFIG,
        "--version",
        "maps=tokyo-2026.10",
        "--version",
        "ml_package=9.9.9",
    )
    assert "VERSION='tokyo-2026.10'" in (out / "maps.env").read_text()
    assert "VERSION='9.9.9'" in (out / "ml_package.env").read_text()


def test_an_override_for_an_image_not_listed_is_an_error(tmp_path, fake_tools, capsys):
    tree = make_tree(tmp_path)
    out = refused(capsys, tree, tmp_path / "out", CONFIG, "--version", "nope=1")
    assert "names no data image in the list" in out
    assert (tree / "opt/autoware/mlmodels/centerpoint/model.onnx").is_file(), (
        "nothing touched"
    )


@pytest.mark.parametrize(
    "config, message",
    [
        ("data_images:\n  - mount: /x\n    version: '1'\n", "name is required"),
        (
            "data_images:\n  - name: 'bad name'\n    mount: /x\n    version: '1'\n",
            "not a data image name",
        ),
        (
            "data_images:\n  - name: a\n    mount: relative\n    version: '1'\n",
            "absolute path in the tree",
        ),
        (
            "data_images:\n  - name: a\n    mount: /x/../y\n    version: '1'\n",
            "absolute path in the tree",
        ),
        (
            "data_images:\n  - name: a\n    mount: /x\n",
            "version or version_file is required",
        ),
        (
            "data_images:\n  - name: a\n    mount: /x\n    version: '1'\n"
            "  - name: a\n    mount: /y\n    version: '1'\n",
            "listed twice",
        ),
        (
            "data_images:\n  - name: a\n    mount: /x\n    version: '1'\n    colour: red\n",
            "unknown keys",
        ),
        (
            "data_images:\n  - name: a\n    mount: /x\n    version: '1'\n"
            "    requires: {rootfs: '2.0'}\n",
            "mapping with min and/or max",
        ),
        (
            "data_images:\n  - name: a\n    mount: /x\n    version: '1'\n"
            "    requires: {rootfs: {min: '2 0'}}\n",
            "is not a version",
        ),
        ("persist_files: [/etc/hosts]\n", "only data_images is read"),
        (
            "data_images:\n  - name: a\n    mount: /x\n    version: '1'\n    component: 'T4 ML'\n",
            "not a component name",
        ),
        ("data_images: {name: a}\n", "must be a list"),
        ("- name: a\n", "expected a mapping"),
    ],
)
def test_a_bad_list_is_refused_before_anything_is_built(
    tmp_path, fake_tools, capsys, config, message
):
    tree = make_tree(tmp_path)
    out = tmp_path / "out"
    assert message in refused(capsys, tree, out, config)
    assert not any(out.iterdir()), "nothing built"
    assert (tree / "opt/autoware/mlmodels/centerpoint/model.onnx").is_file(), (
        "nothing removed"
    )


def test_a_source_the_tree_does_not_have_is_an_error(tmp_path, fake_tools, capsys):
    tree = make_tree(tmp_path)
    out = refused(
        capsys, tree, tmp_path / "out",
        "data_images:\n  - name: a\n    mount: /nowhere\n    version: '1'\n",
    )  # fmt: skip
    assert "/nowhere is not a directory in" in out
    out = refused(
        capsys, tree, tmp_path / "out2",
        "data_images:\n  - name: a\n    mount: /opt/autoware/maps\n    version: '1'\n",
    )  # fmt: skip
    assert "/opt/autoware/maps is empty in" in out


def test_a_version_file_the_tree_does_not_have_is_an_error(
    tmp_path, fake_tools, capsys
):
    tree = make_tree(tmp_path)
    out = refused(
        capsys, tree, tmp_path / "out",
        "data_images:\n  - name: a\n    mount: /srv/maps\n    version_file: /srv/maps/VERSION\n",
    )  # fmt: skip
    assert "version_file /srv/maps/VERSION is not in" in out
    assert (tree / "srv/maps/lanelet2_map.osm").is_file()


def test_an_empty_list_builds_nothing_and_is_not_an_error(tmp_path, fake_tools, caplog):
    tree = make_tree(tmp_path)
    with caplog.at_level(logging.INFO):
        build(tree, tmp_path / "out", "data_images: []\n")
    assert "no data images listed" in caplog.text
    assert (tree / "opt/autoware/mlmodels/centerpoint/model.onnx").is_file()


# ------ one data image on its own ------ #


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"version": "it's"}, "--version may contain only"),
        (
            {"mount": "/opt/my ml"},
            "--mount must be an absolute path and may not contain",
        ),
        ({"mount": "/opt/m'l"}, "--mount must be an absolute path and may not contain"),
    ],
)
def test_a_version_or_mount_the_env_file_could_not_carry_is_refused(
    tmp_path, fake_tools, kwargs, message
):
    """The env file is `KEY='value'` lines the boot script parses; a quote or whitespace
    in a value would break every boot's mount."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "model.onnx").write_bytes(b"ONNX" * 100)
    args = {"name": "ml", "version": "1.0", "mount": "/opt/ml", **kwargs}
    with pytest.raises(blobs.BlobBuildError, match=message):
        blobs.build_data_image(
            src, tmp_path / "out", args["name"], args["version"], args["mount"]
        )
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").iterdir())


def test_a_failed_build_leaves_none_of_its_four_outputs(tmp_path, fake_tools):
    """A partial image beside the previous run's env and spec would be linked into a
    payload as if it were theirs."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "model.onnx").write_bytes(b"ONNX" * 100)
    out = tmp_path / "out"
    out.mkdir()
    for name in ("ml.img", "ml.img.roothash", "ml.env", "ml.spec.json"):
        (out / name).write_text("previous run\n")
    (fake_tools / "mksquashfs").write_text("#!/bin/sh\nexit 1\n")
    with pytest.raises(blobs.BlobBuildError, match="mksquashfs failed"):
        blobs.build_data_image(src, out, "ml", "1.0", "/opt/ml")
    assert sorted(p.name for p in out.iterdir()) == []
