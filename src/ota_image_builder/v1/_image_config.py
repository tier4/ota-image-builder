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

import logging
from datetime import datetime
from typing import Any

from ota_image_libs.common import AliasEnabledModel
from ota_image_libs.v1.annotation_keys import (
    OS,
    OS_VERSION,
    PLATFORM_ECU_ARCH,
    SYS_IMAGE_BASE_IMAGE,
)
from ota_image_libs.v1.file_table.schema import (
    ZstdCompressedFileTableDescriptor,
)
from ota_image_libs.v1.image_config.schema import ImageConfig
from ota_image_libs.v1.image_config.sys_config import SysConfig
from pydantic import Field

from ota_image_builder._common import exit_with_err_msg

logger = logging.getLogger(__name__)


class AddImageConfigAnnotations(AliasEnabledModel):
    """Annotations provided by caller which is needed for composing image_config."""

    base_image: str = Field(alias=SYS_IMAGE_BASE_IMAGE)
    description: str | None = None
    created: str | None = None
    architecture: str = Field(alias=PLATFORM_ECU_ARCH)
    os: str | None = Field(alias=OS, default=None)
    os_version: str | None = Field(alias=OS_VERSION, default=None)


def compose_image_config(
    *,
    file_table_descriptor: ZstdCompressedFileTableDescriptor,
    sys_config_descriptor: SysConfig.Descriptor | None = None,
    annotations: dict[str, Any],
) -> ImageConfig:
    try:
        validated_annotations = AddImageConfigAnnotations.model_validate(annotations)
    except Exception as e:
        logger.error(f"Invalid annotations: {e}")
        exit_with_err_msg(
            f"Invalid annotations: {e}",
            exit_code=1,
        )

    if not validated_annotations.created:
        validated_annotations.created = datetime.now().isoformat()

    return ImageConfig(
        description=validated_annotations.description,
        created=validated_annotations.created,
        architecture=validated_annotations.architecture,
        os=validated_annotations.os,
        os_version=validated_annotations.os_version,  # type: ignore
        sys_config=sys_config_descriptor,
        file_table=file_table_descriptor,
        labels=ImageConfig.Annotations.model_validate(annotations),
    )
