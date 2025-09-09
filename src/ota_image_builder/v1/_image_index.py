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

from pathlib import Path
from typing import Any

from ota_image_libs.v1.consts import (
    IMAGE_INDEX_FNAME,
    OCI_LAYOUT_F_CONTENT,
    OCI_LAYOUT_FNAME,
    RESOURCE_DIR,
)
from ota_image_libs.v1.image_index.schema import ImageIndex


def _init_index(annotations: dict[str, Any]) -> ImageIndex:
    """Initialize the image index with an empty state."""
    return ImageIndex(
        manifests=[],
        annotations=ImageIndex.Annotations.model_validate(annotations),
    )


def init_ota_image(image_root: Path, annotations: dict[str, Any]):
    """Initialize the OTA image index."""
    image_root.mkdir(parents=True)
    (image_root / RESOURCE_DIR).mkdir(parents=True)
    (image_root / OCI_LAYOUT_FNAME).write_text(OCI_LAYOUT_F_CONTENT)
    (image_root / IMAGE_INDEX_FNAME).write_text(
        _init_index(annotations).export_metafile()
    )
    return image_root
