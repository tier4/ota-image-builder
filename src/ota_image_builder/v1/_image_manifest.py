# Copyright 2025 TIER IV, INC. All rights reserved.
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

from __future__ import annotations

from typing import Any

from ota_image_libs.v1.annotation_keys import (
    OTA_RELEASE_KEY,
    PILOT_AUTO_PLATFORM,
    PLATFORM_ECU_ARCH,
    PLATFORM_ECU_HARDWARE_MODEL,
    PLATFORM_ECU_HARDWARE_SERIES,
)
from ota_image_libs.v1.file_table.schema import (
    FileTableDescriptor,
    ZstdCompressedFileTableDescriptor,
)
from ota_image_libs.v1.image_config.schema import ImageConfig
from ota_image_libs.v1.image_manifest.schema import ImageManifest, OTAReleaseKey
from pydantic import Field


class AddImageManifestAnnotations:
    """Annotations needed for composing image manifest."""

    # fmt: off
    ota_release_key: OTAReleaseKey | None = Field(alias=OTA_RELEASE_KEY, default=None)
    pilot_auto_platform: str | None = Field(alias=PILOT_AUTO_PLATFORM, default=None)
    pilot_auto_platform_ecu_hardware: str | None = Field(alias=PLATFORM_ECU_HARDWARE_MODEL, default=None)
    pilot_auto_platform_ecu_hardware_series: str | None = Field(alias=PLATFORM_ECU_HARDWARE_SERIES, default=None)
    pilot_auto_platform_ecu_arch: str | None = Field(alias=PLATFORM_ECU_ARCH, default=None)
    # fmt: on


def compose_image_manifest(
    *,
    image_config_descriptor: ImageConfig.Descriptor,
    file_table_descriptor: FileTableDescriptor | ZstdCompressedFileTableDescriptor,
    annotations: dict[str, Any],
) -> ImageManifest:
    return ImageManifest(
        config=image_config_descriptor,
        layers=[file_table_descriptor],
        annotations=ImageManifest.Annotations.model_validate(annotations),
    )
