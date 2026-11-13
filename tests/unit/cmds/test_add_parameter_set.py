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
"""`add-parameter-set` and the parameter slots a base declares."""

from __future__ import annotations

import json
from argparse import Namespace
from hashlib import sha256
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile

import pytest
from ota_image_libs.v1.annotation_keys import (
    BUILD_TOOL_VERSION,
    OS_VERSION,
    OTA_IMAGE_BLOBS_COUNT,
    OTA_IMAGE_BLOBS_SIZE,
    OTA_RELEASE_KEY,
    PLATFORM_ECU,
    PLATFORM_ECU_ARCH,
    SYS_IMAGE_BASE_IMAGE,
    SYS_IMAGE_DIRS_COUNT,
    SYS_IMAGE_NON_REGULAR_FILES_COUNT,
    SYS_IMAGE_REGULAR_FILES_COUNT,
    SYS_IMAGE_UNIQUE_FILES_COUNT,
    SYS_IMAGE_UNIQUE_FILES_SIZE,
)
from ota_image_libs.v1.consts import IMAGE_INDEX_FNAME, RESOURCE_DIR
from ota_image_libs.v1.file_table.schema import ZstdCompressedFileTableDescriptor
from ota_image_libs.v1.image_config.schema import ImageConfig
from ota_image_libs.v1.image_index.utils import ImageIndexHelper
from ota_image_libs.v1.image_manifest.schema import (
    ImageIdentifier,
    ImageManifest,
    OTAReleaseKey,
)
from ota_image_libs.v1.parameter_set.layer import OPAQUE_WHITEOUT, read_parameter_layer
from ota_image_libs.v1.parameter_set.schema import (
    ParameterSetConfig,
    ParameterSetManifest,
    check_against_base,
)
from ota_image_libs.v1.parameter_set.slots import ParameterSlot

from ota_image_builder.cmds.add_parameter_set import add_parameter_set_cmd
from ota_image_builder.cmds.finalize import finalize_cmd
from ota_image_builder.v1._image_config import compose_image_config
from ota_image_builder.v1._image_index import init_ota_image
from ota_image_builder.v1._parameter_set import (
    ParameterSetError,
    check_slots_in_rootfs,
    load_parameter_slots,
)

ECU = "autoware"
IMAGE_ID = ImageIdentifier(ECU, OTAReleaseKey.dev)
PARAMS = ParameterSlot(
    name="individual_params",
    path="/opt/autoware/vehicle/vehicle.j6_gen2/individual_params",
    apply="/opt/autoware/bin/apply-parameter-set",
)
LAUNCH = ParameterSlot(
    name="launch",
    path="/opt/autoware/vehicle/vehicle.j6_gen2/j6_gen2_launch",
    apply="reboot",
)
CONFIG_ANNOTATIONS = {
    SYS_IMAGE_BASE_IMAGE: "ubuntu:22.04",
    PLATFORM_ECU_ARCH: "x86_64",
    OS_VERSION: "22.04",
    OTA_IMAGE_BLOBS_COUNT: 1,
    OTA_IMAGE_BLOBS_SIZE: 1,
    SYS_IMAGE_REGULAR_FILES_COUNT: 1,
    SYS_IMAGE_NON_REGULAR_FILES_COUNT: 0,
    SYS_IMAGE_DIRS_COUNT: 1,
    SYS_IMAGE_UNIQUE_FILES_COUNT: 1,
    SYS_IMAGE_UNIQUE_FILES_SIZE: 1,
}


def make_base_image(root: Path, slots: list[ParameterSlot]) -> Path:
    """A base OTA image folder with one file-based payload for ECU declaring `slots`;
    the file table is a stand-in blob, as nothing here reads it."""
    init_ota_image(root, {BUILD_TOOL_VERSION: "test"})
    _resource_dir = root / RESOURCE_DIR
    _file_table = ZstdCompressedFileTableDescriptor.add_contents_to_resource_dir(
        b"a file table", _resource_dir
    )
    _config = compose_image_config(
        file_table_descriptor=_file_table,
        annotations=CONFIG_ANNOTATIONS,
        parameter_slots=slots,
    )
    _manifest = ImageManifest(
        config=ImageConfig.Descriptor.export_metafile_to_resource_dir(
            _config, _resource_dir
        ),
        layers=[_file_table],
        annotations=ImageManifest.Annotations.model_validate(
            {PLATFORM_ECU: ECU, OTA_RELEASE_KEY: "dev", PLATFORM_ECU_ARCH: "x86_64"}
        ),
    )
    _helper = ImageIndexHelper(root)
    _helper.image_index.add_image(
        ImageManifest.Descriptor.export_metafile_to_resource_dir(
            _manifest,
            _resource_dir,
            annotations={PLATFORM_ECU: ECU, OTA_RELEASE_KEY: "dev"},
        )
    )
    _helper.sync_index()
    return root


def make_tree(root: Path, *slots: ParameterSlot) -> Path:
    for _slot in slots:
        _dir = root / _slot.relpath
        (_dir / "default").mkdir(parents=True)
        (_dir / "default" / "vehicle.param.yaml").write_text(f"{_slot.name}: 1\n")
    return root


def args(image_root: Path, base: Path, **kw) -> Namespace:
    _a = dict(
        image_root=str(image_root),
        base_image=str(base),
        ecu_id=ECU,
        release_key="dev",
        rootfs=None,
        slot=[],
        empty=False,
        name="j6_gen2_params",
        version="2026.10.29-1",
    )
    _a.update(kw)
    return Namespace(**_a)


def the_set(image_root: Path) -> tuple[ParameterSetManifest, ParameterSetConfig]:
    _helper = ImageIndexHelper(image_root)
    _descriptor = _helper.image_index.find_parameter_set(IMAGE_ID)
    assert _descriptor is not None
    _manifest = _descriptor.load_metafile_from_resource_dir(_helper.image_resource_dir)
    _config = _manifest.config.load_metafile_from_resource_dir(
        _helper.image_resource_dir
    )
    return _manifest, _config


def base_digest(base: Path):
    _descriptor = ImageIndexHelper(base).image_index.find_image(IMAGE_ID)
    assert _descriptor is not None
    return _descriptor.digest


@pytest.fixture
def base(tmp_path: Path) -> Path:
    return make_base_image(tmp_path / "base", [PARAMS, LAUNCH])


@pytest.fixture
def image_root(tmp_path: Path) -> Path:
    return init_ota_image(tmp_path / "parameter_image", {BUILD_TOOL_VERSION: "test"})


class TestAddParameterSet:
    def test_a_set_fills_every_slot_of_its_base_by_default(
        self, tmp_path: Path, base: Path, image_root: Path
    ):
        _tree = make_tree(tmp_path / "tree", PARAMS, LAUNCH)
        add_parameter_set_cmd(args(image_root, base, rootfs=str(_tree)))

        _manifest, _config = the_set(image_root)
        assert _manifest.subject.digest == base_digest(base)
        assert [_s.name for _s in _config.slots] == ["individual_params", "launch"]
        assert _manifest.version == _config.version == "2026.10.29-1"
        assert len(_config.files) == 2
        _filled = check_against_base(
            _manifest,
            _config,
            base_manifest_digest=base_digest(base),
            base_slots=[PARAMS, LAUNCH],
        )
        assert _filled == [PARAMS, LAUNCH]

        _layer = _manifest.layer
        assert _layer is not None
        _resource_dir = image_root / RESOURCE_DIR
        _out = tmp_path / "out"
        _out.mkdir()
        with open(_resource_dir / _layer.digest.digest_hex, "rb") as _blob:
            read_parameter_layer(
                _blob,
                _config.slots,
                extract_to=_out,
                blob_digest=_layer.digest,
                diff_id=_config.diff_ids[0],
            )
        assert (_out / PARAMS.relpath / "default" / "vehicle.param.yaml").exists()
        assert not (_out / PARAMS.relpath / OPAQUE_WHITEOUT).exists()

        finalize_cmd(
            Namespace(
                image_root=str(image_root),
                tmp_dir=None,
                o_skip_bundle=False,
                o_skip_compression=False,
                o_skip_slice=False,
                slice_size=32,
            )
        )
        _index = ImageIndexHelper(image_root).image_index
        assert _index.image_finalized and _index.image_resource_table is None
        assert (_resource_dir / _layer.digest.digest_hex).is_file()

    def test_a_set_of_some_slots(self, tmp_path: Path, base: Path, image_root: Path):
        _tree = make_tree(tmp_path / "tree", PARAMS)
        add_parameter_set_cmd(
            args(image_root, base, rootfs=str(_tree), slot=["individual_params"])
        )
        assert [_s.name for _s in the_set(image_root)[1].slots] == ["individual_params"]

    def test_the_same_tree_makes_the_same_layer(
        self, tmp_path: Path, base: Path, image_root: Path
    ):
        _tree = make_tree(tmp_path / "tree", PARAMS, LAUNCH)
        add_parameter_set_cmd(args(image_root, base, rootfs=str(_tree)))
        _other = init_ota_image(tmp_path / "other", {BUILD_TOOL_VERSION: "test"})
        add_parameter_set_cmd(args(_other, base, rootfs=str(_tree)))
        assert the_set(image_root)[0].layer == the_set(_other)[0].layer

    def test_an_empty_set_needs_no_tree(self, base: Path, image_root: Path):
        add_parameter_set_cmd(args(image_root, base, empty=True))
        _manifest, _config = the_set(image_root)
        assert _manifest.layer is None and _config.is_empty

    def test_a_base_given_as_an_artifact(
        self, tmp_path: Path, base: Path, image_root: Path
    ):
        _zip = tmp_path / "base.zip"
        with ZipFile(_zip, "w", compression=ZIP_STORED) as zf:
            zf.write(base / IMAGE_INDEX_FNAME, IMAGE_INDEX_FNAME)
            for _blob in (base / RESOURCE_DIR).iterdir():
                zf.write(_blob, f"{RESOURCE_DIR}/{_blob.name}")
        add_parameter_set_cmd(args(image_root, _zip, empty=True))
        assert the_set(image_root)[0].subject.digest == base_digest(base)

    @pytest.mark.parametrize(
        "change, message",
        (
            pytest.param(
                {"slot": ["nope"]}, "declares no slot nope", id="unknown_slot"
            ),
            pytest.param({"rootfs": None}, "--rootfs is required", id="no_tree"),
            pytest.param(
                {"empty": True}, "--empty takes no --rootfs", id="empty_and_tree"
            ),
            pytest.param({"ecu_id": "perception"}, "no base payload", id="other_ecu"),
            pytest.param({"version": "1 0"}, "cannot make", id="bad_version"),
        ),
    )
    def test_what_makes_no_set_is_refused(
        self,
        tmp_path: Path,
        base: Path,
        image_root: Path,
        capsys: pytest.CaptureFixture,
        change: dict,
        message: str,
    ):
        _tree = make_tree(tmp_path / "tree", PARAMS, LAUNCH)
        with pytest.raises(SystemExit):
            add_parameter_set_cmd(
                args(image_root, base, **{"rootfs": str(_tree), **change})
            )
        assert message in capsys.readouterr().out
        assert (
            ImageIndexHelper(image_root).image_index.find_parameter_set(IMAGE_ID)
            is None
        )

    def test_a_base_without_slots_takes_only_an_empty_set(
        self, tmp_path: Path, image_root: Path, capsys: pytest.CaptureFixture
    ):
        _base = make_base_image(tmp_path / "plain", [])
        _tree = make_tree(tmp_path / "tree", PARAMS)
        with pytest.raises(SystemExit):
            add_parameter_set_cmd(args(image_root, _base, rootfs=str(_tree)))
        assert "only take an empty set" in capsys.readouterr().out
        add_parameter_set_cmd(args(image_root, _base, empty=True))

    def test_a_slot_missing_from_the_tree_is_refused(
        self, tmp_path: Path, base: Path, image_root: Path
    ):
        _tree = make_tree(tmp_path / "tree", PARAMS)
        with pytest.raises(SystemExit):
            add_parameter_set_cmd(args(image_root, base, rootfs=str(_tree)))


class TestBaseSlots:
    def test_slots_are_read_from_yaml_or_json(self, tmp_path: Path):
        _yaml = tmp_path / "slots.yaml"
        _yaml.write_text(
            f"- name: individual_params\n  path: {PARAMS.path}\n  apply: {PARAMS.apply}\n"
        )
        assert load_parameter_slots(_yaml) == [PARAMS]
        _json = tmp_path / "slots.json"
        _json.write_text(json.dumps([PARAMS.model_dump(), LAUNCH.model_dump()]))
        assert load_parameter_slots(_json) == [PARAMS, LAUNCH]

    @pytest.mark.parametrize(
        "content",
        (
            "- name: a\n  path: relative\n  apply: reboot\n",
            "- name: a\n  path: /x\n  apply: reboot\n- name: b\n  path: /x/y\n  apply: reboot\n",
            "{not: a list}\n",
        ),
    )
    def test_slots_that_do_not_hold_are_refused(self, tmp_path: Path, content: str):
        _f = tmp_path / "slots.yaml"
        _f.write_text(content)
        with pytest.raises(ParameterSetError):
            load_parameter_slots(_f)

    def test_the_base_ships_each_slots_directory(self, tmp_path: Path):
        _rootfs = make_tree(tmp_path / "rootfs", PARAMS)
        check_slots_in_rootfs(_rootfs, [PARAMS])
        with pytest.raises(ParameterSetError, match="launch"):
            check_slots_in_rootfs(_rootfs, [PARAMS, LAUNCH])
        (_rootfs / LAUNCH.relpath).parent.mkdir(parents=True, exist_ok=True)
        (_rootfs / LAUNCH.relpath).symlink_to(_rootfs / PARAMS.relpath)
        with pytest.raises(ParameterSetError, match="launch"):
            check_slots_in_rootfs(_rootfs, [LAUNCH])

    def test_the_image_config_declares_them(self):
        _file_table = ZstdCompressedFileTableDescriptor(
            size=1, digest=f"sha256:{sha256(b'x').hexdigest()}"
        )
        _config = compose_image_config(
            file_table_descriptor=_file_table,
            annotations=CONFIG_ANNOTATIONS,
            parameter_slots=[PARAMS],
        )
        assert json.loads(_config.export_metafile())["parameter_slots"] == [
            PARAMS.model_dump()
        ]
        _plain = compose_image_config(
            file_table_descriptor=_file_table, annotations=CONFIG_ANNOTATIONS
        )
        assert "parameter_slots" not in json.loads(_plain.export_metafile())
