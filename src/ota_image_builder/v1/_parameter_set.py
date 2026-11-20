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
"""Parameter sets: per ECU, the files of a few directories of its rootfs (the set's
slots), in a parameter image of their own (`add-parameter-set`).

A set names the slots it fills, each with how a change to it takes effect (`apply`);
it names no base: which base it suits is decided where the release is evaluated. The
format is ota-image-libs' (spec/parameter_set.md): the layer is written by its
`write_parameter_layer`, so that the same slot contents always make the same layer.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import yaml
from ota_image_libs.common import tmp_fname
from ota_image_libs.common.oci_spec import Sha256Digest
from ota_image_libs.v1.annotation_keys import (
    OTA_RELEASE_KEY,
    PARAMETER_SET_VERSION,
    PLATFORM_ECU,
)
from ota_image_libs.v1.image_index.schema import IMAGE_PAYLOAD_DESCRIPTORS, ImageIndex
from ota_image_libs.v1.image_manifest.schema import ImageIdentifier
from ota_image_libs.v1.parameter_set.layer import (
    ParameterLayerInvalid,
    write_parameter_layer,
)
from ota_image_libs.v1.parameter_set.schema import (
    VERSION,
    EmptyDescriptor,
    ParameterLayerDescriptor,
    ParameterSetConfig,
    ParameterSetFileEntry,
    ParameterSetManifest,
)
from ota_image_libs.v1.parameter_set.slots import (
    SLOT_NAME,
    ParameterSlot,
    ParameterSlotRef,
    check_applies_in_set,
    check_slots,
)
from ota_image_libs.v1.update_agent_package.schema import EMPTY_JSON
from pydantic import TypeAdapter

logger = logging.getLogger(__name__)


class ParameterSetError(Exception):
    """The inputs do not make a parameter set; the message says why."""


def _payload_id(image_id: ImageIdentifier) -> str:
    return f"{image_id.ecu_id} ({image_id.release_key.value})"


#
# ------ what a parameter image carries ------ #
#


def carries_base_payload(index: ImageIndex) -> bool:
    """The index lists a file-based or partition-based payload, or the resource table
    of file-based ones: no parameter set goes beside them (spec/parameter_set.md)."""
    return index.image_resource_table is not None or any(
        isinstance(_entry, IMAGE_PAYLOAD_DESCRIPTORS)
        and not isinstance(_entry, ParameterSetManifest.Descriptor)
        for _entry in index.manifests
    )


def carries_parameter_set(index: ImageIndex) -> bool:
    """The index is a parameter image's: no base payload goes beside its sets."""
    return any(
        isinstance(_entry, ParameterSetManifest.Descriptor)
        for _entry in index.manifests
    )


#
# ------ the set's slots ------ #
#


def load_parameter_slots(slots_file: Path) -> list[ParameterSlot]:
    """The slots a set fills, from a YAML or JSON list of `{name, path, apply}`."""
    try:
        _text = slots_file.read_text()
        # YAML takes most JSON, but not the tabs a JSON file may be indented with
        _raw = (
            json.loads(_text)
            if slots_file.suffix.lower() == ".json"
            else yaml.safe_load(_text)
        )
        _slots = TypeAdapter(list[ParameterSlot]).validate_python(_raw)
        check_slots(str(slots_file), _slots)
    except (OSError, ValueError, yaml.YAMLError) as e:
        raise ParameterSetError(f"invalid parameter slots in {slots_file}: {e}") from e
    if not _slots:
        raise ParameterSetError(
            f"{slots_file} names no slot: an empty set is made with --empty"
        )
    return _slots


def check_slots_in_rootfs(rootfs: Path, slots: Sequence[ParameterSlotRef]) -> None:
    """The tree holds each slot's directory at its path, a real one.

    The same rule the layer writer applies (`write_parameter_layer`), checked here so
    that a tree that makes no layer is refused before any slot is compressed.
    """
    _root = os.path.realpath(rootfs)
    for _slot in slots:
        _dir = rootfs / _slot.relpath
        # a link on the way would be followed on the build machine, out of the tree
        if os.path.realpath(_dir) != os.path.join(_root, _slot.relpath):
            raise ParameterSetError(
                f"slot {_slot.name!r}: {_slot.path} is behind a symbolic link in {rootfs}"
            )
        if not _dir.is_dir():
            raise ParameterSetError(
                f"slot {_slot.name!r}: {_slot.path} is not a directory in {rootfs}"
            )


#
# ------ the set ------ #
#


@contextmanager
def _discard_new_blobs_on_failure(resource_dir: Path) -> Iterator[None]:
    """A blob written for a set that then fails would stay in the image, referenced by
    nothing: on failure, remove the blobs that were not there before. Blobs are named
    by their contents, so one that was there already is another entry's too and stays.
    """
    _before = set(os.listdir(resource_dir))
    try:
        yield
    except BaseException:
        for _name in set(os.listdir(resource_dir)) - _before:
            (resource_dir / _name).unlink(missing_ok=True)
        raise


def _check_file_names(files: Sequence[ParameterSetFileEntry]) -> None:
    # the layer can carry any name, the config lists the files' paths as JSON strings
    for _f in files:
        try:
            _f.path.encode("utf-8")
        except UnicodeEncodeError:
            raise ParameterSetError(
                f"{os.fsencode(_f.path)!r}: a file name that is not UTF-8, which the "
                "set's config cannot list"
            ) from None


def _add_layer(
    rootfs: Path, slots: Sequence[ParameterSlotRef], resource_dir: Path
) -> tuple[ParameterLayerDescriptor, Sha256Digest, list[ParameterSetFileEntry]]:
    _tmp = resource_dir / tmp_fname("parameter_layer")
    try:
        with open(_tmp, "wb") as _out:
            _info = write_parameter_layer(rootfs, slots, _out)
        assert _info.digest is not None and _info.diff_id is not None
        _check_file_names(_info.files)
        os.replace(_tmp, resource_dir / _info.digest.digest_hex)
    finally:
        _tmp.unlink(missing_ok=True)
    logger.info(
        f"layer {_info.digest} ({_info.size} bytes): {len(_info.files)} files, "
        f"{_info.files_size} bytes unpacked"
    )
    return (
        ParameterLayerDescriptor(size=_info.size, digest=_info.digest),
        _info.diff_id,
        _info.files,
    )


def _write_set(
    *,
    resource_dir: Path,
    image_id: ImageIdentifier,
    slots: list[ParameterSlot],
    rootfs: Path | None,
    name: str,
    version: str,
) -> ParameterSetManifest.Descriptor:
    _layers: list[ParameterLayerDescriptor | EmptyDescriptor]
    if slots and rootfs is not None:
        _layer, _diff_id, _files = _add_layer(rootfs, slots, resource_dir)
        _layers = [_layer]
        _config = ParameterSetConfig(
            name=name,
            version=version,
            slots=slots,
            diff_ids=[_diff_id],
            files=_files,
        )
    else:
        _layers = [
            EmptyDescriptor.add_contents_to_resource_dir(EMPTY_JSON, resource_dir)
        ]
        _config = ParameterSetConfig(name=name, version=version)
    _annotations = {
        PLATFORM_ECU: image_id.ecu_id,
        OTA_RELEASE_KEY: image_id.release_key.value,
        PARAMETER_SET_VERSION: version,
    }
    _manifest = ParameterSetManifest(
        config=ParameterSetConfig.Descriptor.export_metafile_to_resource_dir(
            _config, resource_dir
        ),
        layers=_layers,
        annotations=ParameterSetManifest.Annotations.model_validate(_annotations),
    )
    return ParameterSetManifest.Descriptor.export_metafile_to_resource_dir(
        _manifest, resource_dir, annotations=_annotations
    )


def add_parameter_set(
    *,
    index: ImageIndex,
    resource_dir: Path,
    image_id: ImageIdentifier,
    slots: list[ParameterSlot],
    rootfs: Path | None,
    name: str,
    version: str,
) -> ParameterSetManifest.Descriptor:
    """Write the set's layer, config and manifest into `resource_dir` and add its
    entry to `index`; no slot makes an empty set.

    Raises ParameterSetError for inputs that make no valid set, without leaving a blob
    behind; anything else that fails is raised as it is, after the set's new blobs are
    removed.
    """
    # what refuses the set is checked before anything is written, so that a refused
    #   set costs no compression; should writing fail all the same, its new blobs go
    if carries_base_payload(index):
        raise ParameterSetError(
            "the image carries a base payload (file-based or partition-based): a "
            "parameter set goes into a parameter image of its own"
        )
    if not SLOT_NAME.fullmatch(name):
        raise ParameterSetError(f"set name {name!r} is not a valid name")
    if not VERSION.fullmatch(version):
        raise ParameterSetError(f"set version {version!r} is not a valid version")
    if index.find_image_payload(image_id) is not None:
        raise ParameterSetError(
            f"the image already carries a payload for {_payload_id(image_id)}"
        )
    try:
        check_slots("parameter set", slots)
    except ValueError as e:
        raise ParameterSetError(str(e)) from e
    if slots and rootfs is None:
        raise ParameterSetError("a set that fills a slot needs the tree")
    if rootfs is not None:
        check_slots_in_rootfs(rootfs, slots)
        # an apply inside the slots is a file of the set: the tree must carry it
        try:
            check_applies_in_set(rootfs, slots)
        except ValueError as e:
            raise ParameterSetError(f"{rootfs}: {e}") from e
    try:
        with _discard_new_blobs_on_failure(resource_dir):
            _descriptor = _write_set(
                resource_dir=resource_dir,
                image_id=image_id,
                slots=slots,
                rootfs=rootfs,
                name=name,
                version=version,
            )
            index.add_image(_descriptor)
    # what the tree or the libs' checks refuse; other errors are raised as they are
    except (ParameterLayerInvalid, ValueError, OSError) as e:
        raise ParameterSetError(f"cannot make the parameter set: {e}") from e
    logger.info(
        f"parameter set {name} {version} for {_payload_id(image_id)}: "
        f"{json.dumps({_s.name: _s.apply for _s in slots})}"
    )
    return _descriptor
