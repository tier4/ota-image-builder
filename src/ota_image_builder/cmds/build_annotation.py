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
from functools import partial
from pathlib import Path
from pprint import pprint
from typing import TYPE_CHECKING

import yaml
from ota_image_libs.v1 import annotation_keys
from ota_image_libs.v1.annotation_keys import (
    PILOT_AUTO_PLATFORM,
    PILOT_AUTO_PROJECT_VERSION,
    PLATFORM_ECU_HARDWARE_MODEL,
    PLATFORM_ECU_HARDWARE_SERIES,
)

from ota_image_builder._common import exit_with_err_msg

if TYPE_CHECKING:
    from argparse import ArgumentParser, Namespace, _SubParsersAction


logger = logging.getLogger(__name__)

allowed_user_annotations = frozenset(
    {
        PILOT_AUTO_PLATFORM,
        PILOT_AUTO_PROJECT_VERSION,
        PLATFORM_ECU_HARDWARE_MODEL,
        PLATFORM_ECU_HARDWARE_SERIES,
    }
)


def _load_annotation_keys() -> set[str]:
    _loaded_annotations = set()
    for k, v in annotation_keys.__dict__.items():
        if k.startswith("_") or not isinstance(v, str):
            continue
        _loaded_annotations.add(v)
    return _loaded_annotations


def build_annotation_cmd_args(
    sub_arg_parser: _SubParsersAction[ArgumentParser], *parent_parser: ArgumentParser
) -> None:
    build_annotation_cmd_arg_parser = sub_arg_parser.add_parser(
        name="build-annotation",
        help="Build annotation file for OTA image build. "
        "Note that build-annotation cmd will first process `--add-or`, and then `--add-replace`. "
        "Later specified annotations will override the previous one.",
        description="Build annotation file for OTA image build.",
        parents=parent_parser,
    )
    build_annotation_cmd_arg_parser.add_argument(
        "-i",
        help="The base annotation file to load, expected to be a yaml file containing k-v pairs.",
    )
    build_annotation_cmd_arg_parser.add_argument(
        "-o",
        help="The output target of the built annotations.",
    )
    build_annotation_cmd_arg_parser.add_argument(
        "--add-user-annotation",
        action="append",
        help="Add annotation provided by user(at webauto-ci.yml) by `<k>=<v>`, currently only the following annotations are allowed: "
        "vnd.tier4.pilot-auto.platform, vnd.tier4.pilot-auto.project.version, vnd.tier4.pilot-auto.platform.ecu.hardware-model and "
        "vnd.tier4.pilot-auto.platform.ecu.hardware-series.",
    )
    build_annotation_cmd_arg_parser.add_argument(
        "--add-or",
        action="append",
        help="Add one annotation by `<k>=<v>`, if this annotation already presents, skip adding it.",
    )
    build_annotation_cmd_arg_parser.add_argument(
        "--add-replace",
        action="append",
        help="Add one annotation by `<k>=<v>`, if this annotation already presents, override it.",
    )
    build_annotation_cmd_arg_parser.set_defaults(handler=build_annotation_cmd)


def _parse_kv(_in: list[str], *, available_keys: frozenset[str]) -> dict[str, str]:
    res = {}
    for _raw in _in:
        k, *v = _raw.split("=", maxsplit=1)
        if len(v) != 1:
            logger.info(f"ignore invalid annotation kv pair: {_raw}")
            continue

        if k not in available_keys:
            logger.info(f"ignore invalid annotation key: {k}")
            continue
        res[k] = v[0]
    return res


_parse_user_annotations = partial(_parse_kv, available_keys=allowed_user_annotations)


def _load_base(base_f: Path) -> dict[str, str]:
    try:
        _loaded_raw = yaml.safe_load(base_f.read_text())
        if not isinstance(_loaded_raw, dict):
            raise ValueError("invalid input annotation file, expecting a plain dict")
        return _loaded_raw
    except Exception as e:
        _err_msg = f"failed to load input annotation file: {e!r}"
        logger.exception(_err_msg)
        exit_with_err_msg(_err_msg)


def build_annotation_cmd(args: Namespace) -> None:
    logger.debug(f"calling {build_annotation_cmd.__name__} with {args}")
    available_annotation_keys = frozenset(_load_annotation_keys())

    # load input
    add_or = _parse_kv(args.add_or or [], available_keys=available_annotation_keys)
    add_replace = _parse_kv(
        args.add_replace or [], available_keys=available_annotation_keys
    )
    user_anno = _parse_user_annotations(args.add_user_annotation or [])
    if not add_or and not add_replace and not user_anno:
        logger.warning(
            "none of `--add-or` or `--add-replace` or `--add-user-annotation` is specified"
        )

    # load base
    base = {}
    if base_f := args.i:
        base_f = Path(base_f)
        if not base_f.is_file():
            exit_with_err_msg(f"base file {base_f} specified but not found!")
        base = _load_base(base_f)

    # process add_user_annotation
    for k, v in user_anno.items():
        base.setdefault(k, v)

    # process add_or
    for k, v in add_or.items():
        base.setdefault(k, v)

    # process add_replace
    base.update(add_replace)

    # output built annotation file
    output = args.o
    if output:
        try:
            Path(output).write_text(yaml.dump(base))
        except Exception as e:
            _err_msg = f"failed to write to {output}: {e!r}"
            logger.exception(_err_msg)
            exit_with_err_msg(_err_msg)
    else:
        pprint(base)
