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
"""Parameter slots of a base payload and the parameter sets made for it.

A base declares its slots (`add-image --parameter-slots`); a parameter image carries,
per ECU, a set of those slots' files made for exactly that base (`add-parameter-set`).
The format is ota-image-libs' (spec/parameter_set.md): the layer is written by its
`write_parameter_layer`, so that the same slot contents always make the same layer.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from ota_image_libs.common import tmp_fname
from ota_image_libs.common.oci_spec import Sha256Digest
from ota_image_libs.v1.annotation_keys import (
    OTA_RELEASE_KEY,
    PARAMETER_SET_VERSION,
    PLATFORM_ECU,
)
from ota_image_libs.v1.artifact.reader import OTAImageArtifactReader
from ota_image_libs.v1.image_config.schema import ImageConfig
from ota_image_libs.v1.image_index.schema import ImageIndex
from ota_image_libs.v1.image_index.utils import ImageIndexHelper
from ota_image_libs.v1.image_manifest.schema import ImageIdentifier, ImageManifest
from ota_image_libs.v1.parameter_set.layer import write_parameter_layer
from ota_image_libs.v1.parameter_set.schema import (
    EmptyDescriptor,
    FileBasedSubjectDescriptor,
    ParameterLayerDescriptor,
    ParameterSetConfig,
    ParameterSetManifest,
    PartitionBasedSubjectDescriptor,
    SubjectDescriptor,
)
from ota_image_libs.v1.parameter_set.slots import (
    ParameterSlot,
    ParameterSlotRef,
    check_slots_disjoint,
)
from ota_image_libs.v1.partition_image.schema import (
    PartitionImageConfig,
    PartitionImageManifest,
)
from ota_image_libs.v1.update_agent_package.schema import EMPTY_JSON
from pydantic import TypeAdapter

logger = logging.getLogger(__name__)


class ParameterSetError(Exception):
    """The inputs do not make a parameter set; the message says why."""


#
# ------ the base's slots ------ #
#


def load_parameter_slots(slots_file: Path) -> list[ParameterSlot]:
    """The slots a base declares, from a YAML or JSON list of `{name, path, apply}`."""
    try:
        _raw = yaml.safe_load(slots_file.read_text())
        _slots = TypeAdapter(list[ParameterSlot]).validate_python(_raw)
        check_slots_disjoint(str(slots_file), _slots)
    except Exception as e:
        raise ParameterSetError(f"invalid parameter slots in {slots_file}: {e}") from e
    return _slots


def check_slots_in_rootfs(rootfs: Path, slots: list[ParameterSlot]) -> None:
    """The base ships each slot's defaults: its directory is in the rootfs, a real one."""
    for _slot in slots:
        _dir = rootfs / _slot.relpath
        if _dir.is_symlink() or not _dir.is_dir():
            raise ParameterSetError(
                f"slot {_slot.name!r}: {_slot.path} is not a directory in {rootfs}; "
                "the base ships the slot's default contents there"
            )


#
# ------ the base payload a set is made for ------ #
#


@dataclass
class BasePayload:
    subject: SubjectDescriptor
    slots: list[ParameterSlot]


def _base_payload(
    index: ImageIndex, image_id: ImageIdentifier, read_text
) -> BasePayload:
    _descriptor = index.find_image_payload(image_id)
    if isinstance(_descriptor, ImageManifest.Descriptor):
        _manifest = ImageManifest.parse_metafile(read_text(_descriptor.digest))
        _config = ImageConfig.parse_metafile(read_text(_manifest.config.digest))
        return BasePayload(
            subject=FileBasedSubjectDescriptor(
                size=_descriptor.size, digest=_descriptor.digest
            ),
            slots=_config.parameter_slots or [],
        )
    if isinstance(_descriptor, PartitionImageManifest.Descriptor):
        _manifest = PartitionImageManifest.parse_metafile(read_text(_descriptor.digest))
        _config = PartitionImageConfig.parse_metafile(
            read_text(_manifest.config.digest)
        )
        return BasePayload(
            subject=PartitionBasedSubjectDescriptor(
                size=_descriptor.size, digest=_descriptor.digest
            ),
            slots=_config.parameter_slots or [],
        )
    raise ParameterSetError(f"the base image carries no base payload for {image_id}")


def load_base_payload(base_image: Path, image_id: ImageIdentifier) -> BasePayload:
    """The payload of `image_id` in the base OTA image (a folder or an artifact ZIP):
    the subject a set made for it names, and the slots it declares."""
    if base_image.is_dir():
        _helper = ImageIndexHelper(base_image)
        _resource_dir = _helper.image_resource_dir
        return _base_payload(
            _helper.image_index,
            image_id,
            lambda _d: (_resource_dir / _d.digest_hex).read_text(),
        )
    with OTAImageArtifactReader(base_image) as _reader:
        if not _reader.is_valid_image():
            raise ParameterSetError(f"{base_image} is not an OTA image")
        return _base_payload(
            _reader.parse_index(),
            image_id,
            lambda _d: _reader.read_blob_as_text(_d.digest_hex),
        )


#
# ------ the set ------ #
#


def select_slots(
    base: BasePayload, names: list[str], *, empty: bool
) -> list[ParameterSlot]:
    """The base's slots the set fills: the named ones, every one by default, none for
    an empty set."""
    if empty:
        if names:
            raise ParameterSetError("an empty set names no slot")
        return []
    if not base.slots:
        raise ParameterSetError(
            "the base declares no parameter slot: it can only take an empty set"
        )
    if not names:
        return list(base.slots)
    _by_name = {_s.name: _s for _s in base.slots}
    _unknown = [_n for _n in names if _n not in _by_name]
    if _unknown:
        raise ParameterSetError(
            f"the base declares no slot {', '.join(_unknown)} "
            f"(it declares {', '.join(_by_name)})"
        )
    return [_by_name[_n] for _n in dict.fromkeys(names)]


def _add_layer(
    rootfs: Path, slots: list[ParameterSlotRef], resource_dir: Path
) -> tuple[ParameterLayerDescriptor, Sha256Digest, list]:
    _tmp = resource_dir / tmp_fname("parameter_layer")
    try:
        with open(_tmp, "wb") as _out:
            _info = write_parameter_layer(rootfs, slots, _out)
        assert _info.digest is not None and _info.diff_id is not None
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


def add_parameter_set(
    *,
    index: ImageIndex,
    resource_dir: Path,
    image_id: ImageIdentifier,
    base: BasePayload,
    slots: list[ParameterSlot],
    rootfs: Path | None,
    name: str,
    version: str,
) -> ParameterSetManifest.Descriptor:
    """Write the set's layer, config and manifest into `resource_dir` and add its
    entry to `index`. Raises ParameterSetError for inputs that make no valid set."""
    _refs = [ParameterSlotRef(name=_s.name, path=_s.path) for _s in slots]
    _layers: list[ParameterLayerDescriptor | EmptyDescriptor]
    try:
        if _refs:
            if rootfs is None:
                raise ParameterSetError("a set that fills a slot needs the tree")
            _layer, _diff_id, _files = _add_layer(rootfs, _refs, resource_dir)
            _layers = [_layer]
            _config = ParameterSetConfig(
                name=name,
                version=version,
                slots=_refs,
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
            subject=base.subject,
            annotations=ParameterSetManifest.Annotations.model_validate(_annotations),
        )
        _descriptor = ParameterSetManifest.Descriptor.export_metafile_to_resource_dir(
            _manifest, resource_dir, annotations=_annotations
        )
        index.add_image(_descriptor)
    except ParameterSetError:
        raise
    except Exception as e:
        raise ParameterSetError(f"cannot make the parameter set: {e}") from e
    logger.info(
        f"parameter set {name} {version} for {image_id}: "
        f"{json.dumps([_s.name for _s in slots])} on base {base.subject.digest}"
    )
    return _descriptor
