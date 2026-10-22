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
"""cmds/add_update_agent_package.py: the --bundle spec, and the agent taken from a
partition-based payload's tree."""

from __future__ import annotations

import os
import tarfile
from argparse import Namespace
from pathlib import Path

import pytest
from ota_image_libs.v1.annotation_keys import BUILD_TOOL_VERSION
from ota_image_libs.v1.image_index.utils import ImageIndexHelper
from ota_image_libs.v1.update_agent_package.schema import UpdateAgentPackageManifest

from ota_image_builder.cmds.add_update_agent_package import (
    _parse_bundle,
    add_update_agent_package_cmd,
)
from ota_image_builder.v1 import _partition_blobs as blobs
from ota_image_builder.v1._image_index import init_ota_image


def test_a_bundle_spec_is_file_type_version_and_an_optional_arch(tmp_path: Path):
    f = tmp_path / "agent.tar.gz"
    f.write_bytes(b"x")
    assert _parse_bundle(f"{f}:tier4.ota.agent.v1:2.6.2") == (
        f,
        "tier4.ota.agent.v1",
        "2.6.2",
        None,
    )
    assert _parse_bundle(f"{f}:tier4.ota.agent.v1:2.6.2:aarch64") == (
        f,
        "tier4.ota.agent.v1",
        "2.6.2",
        "aarch64",
    )


def test_a_file_with_a_colon_in_its_path_still_parses(tmp_path: Path):
    d = tmp_path / "host:8080"
    d.mkdir()
    f = d / "agent.tar.gz"
    f.write_bytes(b"x")
    assert _parse_bundle(f"{f}:tier4.ota.agent.v1:2.6.2")[0] == f
    assert _parse_bundle(f"{f}:tier4.ota.agent.v1:2.6.2:aarch64")[3] == "aarch64"


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("agent.tar.gz:type", "wants FILE:TYPE:VERSION"),
        ("{f}::2.6.2", "needs a type and a version"),
        ("{f}:type:", "needs a type and a version"),
        ("/nowhere/agent.tar.gz:type:1.0", "no such bundle file"),
    ],
)
def test_bad_bundle_specs_are_refused(tmp_path: Path, capsys, spec, message):
    f = tmp_path / "agent.tar.gz"
    f.write_bytes(b"x")
    with pytest.raises(SystemExit):
        _parse_bundle(spec.format(f=f))
    assert message in capsys.readouterr().out


# ------ --rootfs-dir ------ #


def _elf(machine: int) -> bytes:
    """Just enough of an ELF header for the architecture to be read from it."""
    return (
        b"\x7fELF\x02\x01\x01" + b"\0" * 11 + machine.to_bytes(2, "little") + b"\0" * 44
    )


def _tree(root: Path, *, version: str = "2.10.0", machine: int = 183) -> Path:
    bundle = root / blobs.PARTITION_AGENT_BUNDLE
    (bundle / "_internal" / "lib").mkdir(parents=True)
    entry = bundle / "rootfs-ua-dpi"
    entry.write_bytes(_elf(machine))
    entry.chmod(0o755)
    (bundle / "AGENT_VERSION").write_text(f"{version}\n")
    (bundle / "_internal" / "lib" / "libpython.so.1").write_bytes(b"lib")
    os.symlink("libpython.so.1", bundle / "_internal" / "lib" / "libpython.so")
    os.link(
        bundle / "_internal" / "lib" / "libpython.so.1", bundle / "_internal" / "hard"
    )
    return root


@pytest.fixture
def image_root(tmp_path: Path) -> Path:
    root = tmp_path / "ota_image"
    init_ota_image(root, {BUILD_TOOL_VERSION: "test"})
    return root


def _add(image_root: Path, *trees: Path, bundles: tuple[str, ...] = ()) -> None:
    add_update_agent_package_cmd(
        Namespace(
            image_root=str(image_root),
            bundle=list(bundles),
            rootfs_dir=[str(t) for t in trees],
        )
    )


def _shipped(image_root: Path) -> UpdateAgentPackageManifest | None:
    helper = ImageIndexHelper(image_root=image_root)
    desc = helper.image_index.find_update_agent_package()
    if desc is None:
        return None
    blob = helper.image_resource_dir / desc.digest.digest_hex
    return UpdateAgentPackageManifest.model_validate_json(blob.read_text())


def test_the_tree_s_agent_is_shipped_with_its_version_and_architecture(
    tmp_path: Path, image_root: Path
):
    _add(image_root, _tree(tmp_path / "l4t"))
    manifest = _shipped(image_root)
    assert manifest is not None
    found = manifest.find_bundle(
        agent_type="tier4.ota.agent.v1", architecture="aarch64"
    )
    assert found is not None
    assert found.annotations.version == "2.10.0"
    assert found.annotations.architecture == "arm64"
    blob = (
        ImageIndexHelper(image_root=image_root).image_resource_dir
        / found.digest.digest_hex
    )
    with tarfile.open(blob, "r:gz") as tar:
        members = {m.name: m for m in tar.getmembers()}
    # what the agent unpacks: files, directories and symlinks under one directory
    assert members["rootfs-ua-dpi/rootfs-ua-dpi"].mode & 0o111
    assert members["rootfs-ua-dpi/_internal/lib/libpython.so"].issym()
    assert members["rootfs-ua-dpi/_internal/hard"].isfile()
    assert all(m.isfile() or m.isdir() or m.issym() for m in members.values())
    assert all((m.uid, m.gid) == (0, 0) for m in members.values())


def test_the_same_bundle_makes_the_same_blob(tmp_path: Path):
    tree = _tree(tmp_path / "t")
    a, _, _ = blobs.agent_bundle_from_tree(tree, tmp_path / "a")  # type: ignore[misc]
    b, _, _ = blobs.agent_bundle_from_tree(tree, tmp_path / "b")  # type: ignore[misc]
    assert a.read_bytes() == b.read_bytes()


def test_one_bundle_per_architecture_across_trees(tmp_path: Path, image_root: Path):
    _add(
        image_root,
        _tree(tmp_path / "x86", machine=62),
        _tree(tmp_path / "arm", machine=183),
        _tree(tmp_path / "arm-again", machine=183),
    )
    manifest = _shipped(image_root)
    assert manifest is not None
    assert sorted(b.annotations.architecture for b in manifest.layers) == [
        "arm64",
        "x86_64",
    ]


def test_two_versions_for_one_architecture_are_refused(
    tmp_path: Path, image_root: Path, capsys
):
    with pytest.raises(SystemExit):
        _add(image_root, _tree(tmp_path / "a"), _tree(tmp_path / "b", version="2.11.0"))
    assert "two arm64 agents, 2.10.0 and 2.11.0" in capsys.readouterr().out
    assert _shipped(image_root) is None


def test_trees_without_an_agent_leave_the_image_as_it_is(
    tmp_path: Path, image_root: Path
):
    (tmp_path / "bare").mkdir()
    before = (image_root / "index.json").read_bytes()
    _add(image_root, tmp_path / "bare")
    assert _shipped(image_root) is None
    assert (image_root / "index.json").read_bytes() == before


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        (lambda b: (b / "AGENT_VERSION").unlink(), "names its version there"),
        (
            lambda b: (b / "rootfs-ua-dpi").write_bytes(b"#!/bin/sh\n"),
            "not an ELF executable",
        ),
        (lambda b: (b / "rootfs-ua-dpi").write_bytes(_elf(3)), "ELF machine 3"),
        (lambda b: (b / "rootfs-ua-dpi").unlink(), "not an agent bundle"),
    ],
)
def test_a_bundle_that_does_not_say_what_it_is_is_refused(
    tmp_path: Path, image_root: Path, capsys, damage, message
):
    tree = _tree(tmp_path / "t")
    damage(tree / blobs.PARTITION_AGENT_BUNDLE)
    with pytest.raises(SystemExit):
        _add(image_root, tree)
    assert message in capsys.readouterr().out


def test_nothing_to_add_is_an_error(image_root: Path, capsys):
    with pytest.raises(SystemExit):
        _add(image_root)
    assert "pass --bundle, or --rootfs-dir" in capsys.readouterr().out
