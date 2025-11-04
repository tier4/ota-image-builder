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
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from ota_image_builder._common import exit_with_err_msg

logger = logging.getLogger(__name__)


def validate_annotations(
    annotations_file: Path, model: type[BaseModel]
) -> dict[str, Any]:
    """Validate the annotations file agaisnt <model> and return the annotations as a dict.

    This method will verify the input annotations_file, and only take
        known annotations to the returned dict.
    """
    if not annotations_file.is_file():
        exit_with_err_msg(f"Annotations file {annotations_file} does not exist.")

    _loaded = yaml.safe_load(annotations_file.read_text())
    if not isinstance(_loaded, dict):
        exit_with_err_msg(
            f"Annotations file {annotations_file} is not a valid yaml file."
        )

    try:
        _verified = model.model_validate(_loaded)
    except Exception as e:
        logger.debug(f"invalid annotations file: {e}", exc_info=e)
        exit_with_err_msg(
            f"Annotations file {annotations_file} is not a valid annotations file: {e}"
        )
    return _verified.model_dump()
