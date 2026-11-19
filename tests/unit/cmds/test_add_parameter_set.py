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
"""`add-parameter-set`: a set for one ECU, naming the slots it fills and their applies."""

from __future__ import annotations

import json
import os
from argparse import Namespace
from collections.abc import Callable
from pathlib import Path

import pytest
from ota_image_libs.v1.annotation_keys import (
    BUILD_TOOL_VERSION,
    OTA_RELEASE_KEY,
    PLATFORM_ECU_ARCH,
    SYS_IMAGE_BASE_IMAGE,
)
from ota_image_libs.v1.consts import RESOURCE_DIR
from ota_image_libs.v1.image_index.schema import ImageIndex
from ota_image_libs.v1.image_index.utils import ImageIndexHelper
from ota_image_libs.v1.image_manifest.schema import ImageIdentifier, OTAReleaseKey
from ota_image_libs.v1.parameter_set.layer import OPAQUE_WHITEOUT, read_parameter_layer
from ota_image_libs.v1.parameter_set.schema import (
    ParameterSetConfig,
    ParameterSetManifest,
)
from ota_image_libs.v1.parameter_set.slots import ParameterSlot
from ota_image_libs.v1.resource_table.schema import ResourceTableDescriptor

from ota_image_builder.cmds.add_image import add_image_cmd
from ota_image_builder.cmds.add_parameter_set import add_parameter_set_cmd
from ota_image_builder.cmds.add_partition_image import add_partition_image_cmd
from ota_image_builder.cmds.finalize import (
    _collect_protected_resources_digest,
    finalize_cmd,
)
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
    path="/opt/autoware/vehicle/sample_vehicle/individual_params",
    apply="/opt/autoware/vehicle/sample_vehicle/apply.sh",
)
LAUNCH = ParameterSlot(
    name="launch",
    path="/opt/autoware/vehicle/sample_vehicle/launch",
    apply="reboot",
)


def make_tree(root: Path, *slots: ParameterSlot) -> Path:
    for _slot in slots:
        _dir = root / _slot.relpath
        (_dir / "default").mkdir(parents=True)
        (_dir / "default" / "vehicle.param.yaml").write_text(f"{_slot.name}: 1\n")
    return root


def slots_file(path: Path, *slots: ParameterSlot) -> Path:
    path.write_text(json.dumps([_s.model_dump() for _s in slots]))
    return path


def args(image_root: Path, **kw) -> Namespace:
    _a = dict(
        image_root=str(image_root),
        ecu_id=ECU,
        release_key="dev",
        slots=None,
        rootfs=None,
        empty=False,
        name="sample_vehicle",
        version="6.1.0",
    )
    _a.update(kw)
    return Namespace(**_a)


def finalize(image_root: Path) -> None:
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


def blobs(image_root: Path) -> list[Path]:
    return sorted((image_root / RESOURCE_DIR).iterdir())


def add_vendor_package(image_root: Path, tmp_path: Path, ecu_id: str) -> None:
    """A partition-based payload for `ecu_id`: the smallest one, a vendor package."""
    _d = tmp_path / f"vendor_{ecu_id}"
    _d.mkdir()
    (_d / "update.pkg").write_bytes(b"opaque" * 100)
    (_d / "spec.json").write_text(
        json.dumps(
            {
                "delivery": "vendor-package",
                "version": "1.0.0",
                "package": {"file": "update.pkg", "format": "example"},
                "partitions": [{"name": "rootfs", "action": "write"}],
            }
        )
    )
    _annotations = {
        SYS_IMAGE_BASE_IMAGE: "ubuntu:24.04",
        PLATFORM_ECU_ARCH: "x86_64",
        OTA_RELEASE_KEY: "dev",
    }
    (_d / "annotations.yaml").write_text(
        "".join(f'"{_k}": "{_v}"\n' for _k, _v in _annotations.items())
    )
    add_partition_image_cmd(
        Namespace(
            image_root=str(image_root),
            spec=str(_d / "spec.json"),
            annotations_file=str(_d / "annotations.yaml"),
            sys_config=[f"{ecu_id}:"],
            release_key=None,
            no_compress=True,
            zstd_level=3,
            delta_from=[],
            data_only=False,
        )
    )


def the_set(
    image_root: Path, image_id: ImageIdentifier = IMAGE_ID
) -> tuple[ParameterSetManifest, ParameterSetConfig]:
    _helper = ImageIndexHelper(image_root)
    _descriptor = _helper.image_index.find_parameter_set(image_id)
    assert _descriptor is not None
    _manifest = _descriptor.load_metafile_from_resource_dir(_helper.image_resource_dir)
    _config = _manifest.config.load_metafile_from_resource_dir(
        _helper.image_resource_dir
    )
    return _manifest, _config


@pytest.fixture
def image_root(tmp_path: Path) -> Path:
    return init_ota_image(tmp_path / "parameter_image", {BUILD_TOOL_VERSION: "test"})


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    return make_tree(tmp_path / "tree", PARAMS, LAUNCH)


@pytest.fixture
def slots(tmp_path: Path) -> Path:
    return slots_file(tmp_path / "slots.json", PARAMS, LAUNCH)


class TestAddParameterSet:
    def test_a_set_fills_the_slots_it_names_with_their_applies(
        self, tmp_path: Path, tree: Path, slots: Path, image_root: Path
    ):
        add_parameter_set_cmd(args(image_root, slots=str(slots), rootfs=str(tree)))

        _manifest, _config = the_set(image_root)
        assert _config.slots == [PARAMS, LAUNCH]
        assert [_s.apply for _s in _config.slots] == [PARAMS.apply, "reboot"]
        assert _manifest.version == _config.version == "6.1.0"
        assert _manifest.annotations.pilot_auto_platform_ecu == ECU
        assert len(_config.files) == 2

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

    def test_the_set_names_no_base(self, tree: Path, slots: Path, image_root: Path):
        add_parameter_set_cmd(args(image_root, slots=str(slots), rootfs=str(tree)))
        _helper = ImageIndexHelper(image_root)
        _descriptor = _helper.image_index.find_parameter_set(IMAGE_ID)
        assert _descriptor is not None
        _raw = json.loads(
            (_helper.image_resource_dir / _descriptor.digest.digest_hex).read_text()
        )
        assert "subject" not in _raw
        _config = json.loads(
            (
                _helper.image_resource_dir / _raw["config"]["digest"].split(":")[1]
            ).read_text()
        )
        assert _config["slots"] == [PARAMS.model_dump(), LAUNCH.model_dump()]

    def test_a_set_of_some_slots(self, tmp_path: Path, image_root: Path):
        _tree = make_tree(tmp_path / "tree", PARAMS)
        _slots = slots_file(tmp_path / "slots.json", PARAMS)
        add_parameter_set_cmd(args(image_root, slots=str(_slots), rootfs=str(_tree)))
        assert the_set(image_root)[1].slots == [PARAMS]

    def test_the_slots_are_read_from_yaml(
        self, tmp_path: Path, tree: Path, image_root: Path
    ):
        _yaml = tmp_path / "slots.yaml"
        _yaml.write_text(
            f"- name: {PARAMS.name}\n  path: {PARAMS.path}\n  apply: {PARAMS.apply}\n"
        )
        add_parameter_set_cmd(args(image_root, slots=str(_yaml), rootfs=str(tree)))
        assert the_set(image_root)[1].slots == [PARAMS]

    def test_the_same_tree_makes_the_same_layer(
        self, tmp_path: Path, tree: Path, slots: Path, image_root: Path
    ):
        add_parameter_set_cmd(args(image_root, slots=str(slots), rootfs=str(tree)))
        _other = init_ota_image(tmp_path / "other", {BUILD_TOOL_VERSION: "test"})
        add_parameter_set_cmd(args(_other, slots=str(slots), rootfs=str(tree)))
        assert the_set(image_root)[0].layer == the_set(_other)[0].layer

    def test_an_empty_set_needs_no_slots_and_no_tree(self, image_root: Path):
        add_parameter_set_cmd(args(image_root, empty=True))
        _manifest, _config = the_set(image_root)
        assert _manifest.layer is None and _config.is_empty
        _empty = image_root / RESOURCE_DIR / _manifest.layers[0].digest.digest_hex
        assert _empty.read_bytes() == b"{}"

    def test_sets_for_several_ecus_and_release_keys_go_into_one_image(
        self, tree: Path, slots: Path, image_root: Path
    ):
        add_parameter_set_cmd(args(image_root, slots=str(slots), rootfs=str(tree)))
        add_parameter_set_cmd(
            args(image_root, slots=str(slots), rootfs=str(tree), release_key="prd")
        )
        add_parameter_set_cmd(args(image_root, empty=True, ecu_id="perception"))
        finalize(image_root)

        _prd = ImageIdentifier(ECU, OTAReleaseKey.prd)
        _perception = ImageIdentifier("perception", OTAReleaseKey.dev)
        assert ImageIndexHelper(image_root).image_index.image_identifiers == [
            IMAGE_ID,
            _prd,
            _perception,
        ]
        _manifest, _config = the_set(image_root, _prd)
        assert _manifest.annotations.ota_release_key == OTAReleaseKey.prd
        assert _config.slots == [PARAMS, LAUNCH]
        assert the_set(image_root, _perception)[1].is_empty

    @pytest.mark.parametrize(
        "change, message",
        (
            pytest.param({"slots": None}, "--slots is required", id="no_slots"),
            pytest.param({"rootfs": None}, "--rootfs is required", id="no_tree"),
            pytest.param(
                {"empty": True, "rootfs": None},
                "--empty takes no --slots",
                id="empty_and_slots",
            ),
            pytest.param(
                {"empty": True, "slots": None},
                "--empty takes no --slots and no --rootfs",
                id="empty_and_tree",
            ),
            pytest.param({"version": "1 0"}, "not a valid version", id="bad_version"),
            pytest.param({"name": "a set"}, "not a valid name", id="bad_name"),
        ),
    )
    def test_what_makes_no_set_is_refused(
        self,
        tree: Path,
        slots: Path,
        image_root: Path,
        capsys: pytest.CaptureFixture,
        change: dict,
        message: str,
    ):
        with pytest.raises(SystemExit):
            add_parameter_set_cmd(
                args(image_root, **{"slots": str(slots), "rootfs": str(tree), **change})
            )
        assert message in capsys.readouterr().out
        assert (
            ImageIndexHelper(image_root).image_index.find_parameter_set(IMAGE_ID)
            is None
        )
        assert list((image_root / RESOURCE_DIR).iterdir()) == [], "no blob left over"

    def test_a_slot_dir_missing_from_the_tree_is_refused(
        self,
        tmp_path: Path,
        slots: Path,
        image_root: Path,
        capsys: pytest.CaptureFixture,
    ):
        _tree = make_tree(tmp_path / "tree", PARAMS)
        with pytest.raises(SystemExit):
            add_parameter_set_cmd(args(image_root, slots=str(slots), rootfs=str(_tree)))
        assert "'launch'" in capsys.readouterr().out
        assert list((image_root / RESOURCE_DIR).iterdir()) == []

    def test_a_slot_dir_behind_a_link_in_the_tree_is_refused(
        self,
        tmp_path: Path,
        slots: Path,
        image_root: Path,
        capsys: pytest.CaptureFixture,
    ):
        _tree = make_tree(tmp_path / "tree", PARAMS)
        _elsewhere = make_tree(tmp_path / "elsewhere", LAUNCH)
        (_tree / LAUNCH.relpath).symlink_to(_elsewhere / LAUNCH.relpath)
        with pytest.raises(SystemExit):
            add_parameter_set_cmd(args(image_root, slots=str(slots), rootfs=str(_tree)))
        assert "symbolic link" in capsys.readouterr().out
        assert list((image_root / RESOURCE_DIR).iterdir()) == []

    def test_a_second_set_for_the_same_ecu_is_refused_before_writing(
        self,
        tmp_path: Path,
        tree: Path,
        slots: Path,
        image_root: Path,
        capsys: pytest.CaptureFixture,
    ):
        add_parameter_set_cmd(args(image_root, slots=str(slots), rootfs=str(tree)))
        _blobs = sorted((image_root / RESOURCE_DIR).iterdir())
        _other = make_tree(tmp_path / "other", PARAMS, LAUNCH)
        (_other / PARAMS.relpath / "extra").write_text("changes the layer\n")
        with pytest.raises(SystemExit):
            add_parameter_set_cmd(
                args(image_root, slots=str(slots), rootfs=str(_other))
            )
        assert "already carries a payload" in capsys.readouterr().out
        assert sorted((image_root / RESOURCE_DIR).iterdir()) == _blobs

    def test_a_finalized_image_takes_no_set(
        self,
        tree: Path,
        slots: Path,
        image_root: Path,
        capsys: pytest.CaptureFixture,
    ):
        add_parameter_set_cmd(args(image_root, empty=True))
        finalize(image_root)
        _blobs = blobs(image_root)
        with pytest.raises(SystemExit):
            add_parameter_set_cmd(
                args(
                    image_root, slots=str(slots), rootfs=str(tree), ecu_id="perception"
                )
            )
        assert "NOT allowed" in capsys.readouterr().out
        assert blobs(image_root) == _blobs

    def test_an_image_with_file_based_payloads_takes_no_set(
        self, image_root: Path, capsys: pytest.CaptureFixture
    ):
        """An image with a resource table holds file-based payloads: a parameter
        image is one of its own."""
        _helper = ImageIndexHelper(image_root)
        _helper.image_index.update_resource_table(
            ResourceTableDescriptor.add_contents_to_resource_dir(
                b"a resource table", _helper.image_resource_dir
            )
        )
        _helper.sync_index()
        _blobs = blobs(image_root)
        with pytest.raises(SystemExit):
            add_parameter_set_cmd(args(image_root, empty=True))
        assert "parameter image of its own" in capsys.readouterr().out
        assert blobs(image_root) == _blobs

    @pytest.mark.parametrize("ecu_id", (ECU, "perception"))
    @pytest.mark.parametrize("empty", (False, True))
    def test_an_image_with_a_partition_based_payload_takes_no_set(
        self,
        tmp_path: Path,
        tree: Path,
        slots: Path,
        image_root: Path,
        capsys: pytest.CaptureFixture,
        ecu_id: str,
        empty: bool,
    ):
        """Of the payload's own ECU or another's, refused before a blob is written."""
        add_vendor_package(image_root, tmp_path, ECU)
        _blobs = blobs(image_root)
        _set = {"empty": True} if empty else {"slots": str(slots), "rootfs": str(tree)}
        with pytest.raises(SystemExit):
            add_parameter_set_cmd(args(image_root, ecu_id=ecu_id, **_set))
        assert "partition-based" in capsys.readouterr().out
        assert blobs(image_root) == _blobs

    @pytest.mark.parametrize(
        "break_it, message",
        (
            pytest.param(
                lambda d: (d / "link").symlink_to("default"),
                "only regular files and directories",
                id="symlink",
            ),
            pytest.param(
                lambda d: os.chmod(d / "default" / "vehicle.param.yaml", 0o4755),
                "setuid",
                id="setuid",
            ),
            pytest.param(
                lambda d: (d / ".wh.default").write_text(""),
                "whiteout",
                id="whiteout_name",
            ),
            pytest.param(
                lambda d: (d / os.fsdecode(b"caf\xe9.yaml")).write_text(""),
                "not UTF-8",
                id="not_utf8",
            ),
        ),
    )
    def test_what_the_layer_cannot_hold_is_refused_and_leaves_no_blob(
        self,
        tree: Path,
        slots: Path,
        image_root: Path,
        capsys: pytest.CaptureFixture,
        break_it: Callable[[Path], object],
        message: str,
    ):
        """Found while the layer is written: the half-written layer goes too."""
        break_it(tree / LAUNCH.relpath)
        with pytest.raises(SystemExit):
            add_parameter_set_cmd(args(image_root, slots=str(slots), rootfs=str(tree)))
        assert message in capsys.readouterr().out
        assert blobs(image_root) == []

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads any directory")
    def test_a_directory_that_cannot_be_read_is_refused(
        self,
        tree: Path,
        slots: Path,
        image_root: Path,
        capsys: pytest.CaptureFixture,
    ):
        _unreadable = tree / LAUNCH.relpath / "default"
        _unreadable.chmod(0)
        try:
            with pytest.raises(SystemExit):
                add_parameter_set_cmd(
                    args(image_root, slots=str(slots), rootfs=str(tree))
                )
        finally:
            _unreadable.chmod(0o755)
        assert "cannot read" in capsys.readouterr().out
        assert blobs(image_root) == []

    @pytest.mark.parametrize(
        "error, raised",
        (
            pytest.param(ValueError("refused"), SystemExit, id="refused"),
            pytest.param(RuntimeError("a bug"), RuntimeError, id="unexpected"),
        ),
    )
    def test_a_set_that_fails_once_written_leaves_no_blob(
        self,
        tree: Path,
        slots: Path,
        image_root: Path,
        monkeypatch: pytest.MonkeyPatch,
        error: Exception,
        raised: type[BaseException],
    ):
        """What the libs refuse is reported; anything else is raised as it is, for
        the command's handler to log with its traceback."""

        def _fail(*_, **__):
            raise error

        monkeypatch.setattr(ImageIndex, "add_image", _fail)
        with pytest.raises(raised):
            add_parameter_set_cmd(args(image_root, slots=str(slots), rootfs=str(tree)))
        assert blobs(image_root) == []

    def test_a_blob_another_entry_shares_stays_when_a_set_fails(
        self, image_root: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The empty descriptor's `{}` is every empty set's: a failed one keeps it."""
        add_parameter_set_cmd(args(image_root, empty=True))
        _blobs = blobs(image_root)

        def _fail(*_, **__):
            raise ValueError("refused")

        monkeypatch.setattr(ImageIndex, "add_image", _fail)
        with pytest.raises(SystemExit):
            add_parameter_set_cmd(args(image_root, empty=True, ecu_id="perception"))
        assert blobs(image_root) == _blobs


class TestParameterImage:
    """A parameter image carries parameter sets only: no base payload goes into it."""

    @pytest.fixture(autouse=True)
    def parameter_image(self, image_root: Path) -> None:
        add_parameter_set_cmd(args(image_root, empty=True))

    def test_it_takes_no_partition_based_payload(
        self, tmp_path: Path, image_root: Path, capsys: pytest.CaptureFixture
    ):
        _blobs = blobs(image_root)
        with pytest.raises(SystemExit):
            add_vendor_package(image_root, tmp_path, "perception")
        assert "parameter image" in capsys.readouterr().out
        assert blobs(image_root) == _blobs

    def test_it_takes_no_file_based_payload(
        self, tmp_path: Path, image_root: Path, capsys: pytest.CaptureFixture
    ):
        _blobs = blobs(image_root)
        with pytest.raises(SystemExit):
            add_image_cmd(Namespace(image_root=str(image_root), rootfs=str(tmp_path)))
        assert "parameter image" in capsys.readouterr().out
        assert blobs(image_root) == _blobs


class TestFinalize:
    def test_finalize_keeps_the_sets_config_and_layer_out_of_the_filters(
        self, tree: Path, slots: Path, image_root: Path
    ):
        add_parameter_set_cmd(args(image_root, slots=str(slots), rootfs=str(tree)))
        _manifest, _ = the_set(image_root)
        _protected = _collect_protected_resources_digest(ImageIndexHelper(image_root))
        assert _manifest.config.digest.digest in _protected
        assert _manifest.layers[0].digest.digest in _protected

    def test_finalize_keeps_the_sets_blobs_as_stored(
        self, tree: Path, slots: Path, image_root: Path
    ):
        add_parameter_set_cmd(args(image_root, slots=str(slots), rootfs=str(tree)))
        _manifest, _config = the_set(image_root)
        _resource_dir = image_root / RESOURCE_DIR
        _layer = _manifest.layer
        assert _layer is not None
        _layer_bytes = (_resource_dir / _layer.digest.digest_hex).read_bytes()

        finalize(image_root)

        _index = ImageIndexHelper(image_root).image_index
        assert _index.image_finalized and _index.image_resource_table is None
        assert (_resource_dir / _layer.digest.digest_hex).read_bytes() == _layer_bytes
        assert (_resource_dir / _manifest.config.digest.digest_hex).is_file()
        assert the_set(image_root) == (_manifest, _config)


class TestSlotsFile:
    def test_slots_are_read_from_yaml_or_json(self, tmp_path: Path):
        _yaml = tmp_path / "slots.yaml"
        _yaml.write_text(
            f"- name: individual_params\n  path: {PARAMS.path}\n  apply: {PARAMS.apply}\n"
        )
        assert load_parameter_slots(_yaml) == [PARAMS]
        _json = slots_file(tmp_path / "slots.json", PARAMS, LAUNCH)
        assert load_parameter_slots(_json) == [PARAMS, LAUNCH]

    def test_a_json_file_may_be_indented_with_tabs(self, tmp_path: Path):
        _json = tmp_path / "slots.json"
        _json.write_text(json.dumps([PARAMS.model_dump()], indent="\t"))
        assert load_parameter_slots(_json) == [PARAMS]

    @pytest.mark.parametrize(
        "content",
        (
            pytest.param(
                "- name: a\n  path: relative\n  apply: reboot\n", id="relative"
            ),
            pytest.param(
                "- name: a\n  path: /x\n  apply: reboot\n"
                "- name: b\n  path: /x/y\n  apply: reboot\n",
                id="nested",
            ),
            pytest.param("- name: a\n  path: /x\n", id="no_apply"),
            pytest.param("- name: a\n  path: /x\n  apply: run.sh\n", id="bad_apply"),
            pytest.param(
                "- name: a\n  path: /x\n  apply: /x/apply.sh\n", id="apply_in_a_slot"
            ),
            pytest.param(
                "- name: a\n  path: /opt/ota\n  apply: reboot\n", id="reserved"
            ),
            pytest.param("{not: a list}\n", id="not_a_list"),
            pytest.param("[]\n", id="no_slot"),
        ),
    )
    def test_slots_that_do_not_hold_are_refused(self, tmp_path: Path, content: str):
        _f = tmp_path / "slots.yaml"
        _f.write_text(content)
        with pytest.raises(ParameterSetError):
            load_parameter_slots(_f)

    def test_the_tree_holds_each_slots_directory(self, tmp_path: Path):
        _rootfs = make_tree(tmp_path / "rootfs", PARAMS)
        check_slots_in_rootfs(_rootfs, [PARAMS])
        with pytest.raises(ParameterSetError, match="launch"):
            check_slots_in_rootfs(_rootfs, [PARAMS, LAUNCH])
        (_rootfs / LAUNCH.relpath).symlink_to(_rootfs / PARAMS.relpath)
        with pytest.raises(ParameterSetError, match="launch"):
            check_slots_in_rootfs(_rootfs, [LAUNCH])
