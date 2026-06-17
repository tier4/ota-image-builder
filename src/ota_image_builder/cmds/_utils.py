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
import sys
from pathlib import Path
from typing import Any, Literal, overload

import yaml
from pydantic import BaseModel, ConfigDict

from ota_image_builder._common import exit_with_err_msg

logger = logging.getLogger(__name__)

MODEL_WITH_ALIAS = ConfigDict(
    validate_by_name=True,
    validate_by_alias=True,
    serialize_by_alias=True,
)
"""Enable alias for field validation and serialization."""


@overload
def resolve_cli_input_arg(
    value: str | None, *, inline_prefix: str, label: str, binary: Literal[False] = ...
) -> str: ...


@overload
def resolve_cli_input_arg(
    value: str | None, *, inline_prefix: str, label: str, binary: Literal[True]
) -> bytes: ...


def resolve_cli_input_arg(
    value: str | None, *, inline_prefix: str, label: str, binary: bool = False
) -> str | bytes:
    """Resolve a CLI arg that may be inline content, a file path, or ``-`` (stdin).

    The arg is treated as inline content when (after stripping leading whitespace) it
    starts with ``inline_prefix`` (e.g. ``{`` for JSON, ``-----BEGIN`` for a PEM key);
    as stdin when it is exactly ``-``; otherwise as a path to read. ``label`` names the
    arg in the error messages. With ``binary=True`` the content is returned as ``bytes``
    (stdin/file read in binary mode), otherwise as decoded text.
    """
    if not value:
        exit_with_err_msg(f"empty {label}, abort!")

    if value.lstrip().startswith(inline_prefix):
        return value.encode() if binary else value

    if value == "-":
        try:
            return sys.stdin.buffer.read() if binary else sys.stdin.read()
        except Exception as e:
            exit_with_err_msg(f"failed to read {label} from stdin: {e!r}")

    _f = Path(value)
    try:
        return _f.read_bytes() if binary else _f.read_text()
    except FileNotFoundError:
        exit_with_err_msg(f"the specified {label} file doesn't exist!")
    except Exception as e:
        exit_with_err_msg(f"failed to read {label} file: {e!r}")


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
