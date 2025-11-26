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
import re
from pathlib import Path
from pprint import pprint
from typing import TYPE_CHECKING

from ota_image_builder._common import exit_with_err_msg

if TYPE_CHECKING:
    from argparse import ArgumentParser, Namespace, _SubParsersAction


logger = logging.getLogger(__name__)

# fmt: off
invalid_patterns_pa = [
    re.compile(r"^\./*$"),                            # .
    re.compile(r"^\.\./*$"),                          # ..
    re.compile(r"^/+$"),                              # /
    re.compile(r"^/boot/ota.*$"),                     # /boot/ota
    re.compile(r"^/home/autoware.*/build$"),    # /home/autoware/*/build
]
# fmt: on


def build_exclude_cfg_cmd_args(
    sub_arg_parser: _SubParsersAction[ArgumentParser], *parent_parser: ArgumentParser
) -> None:
    build_exclude_cfg_cmd_arg_parser = sub_arg_parser.add_parser(
        name="build-exclude-cfg",
        help="Build extra exclude cfg file for OTA image build. "
        "Note that this command will filter away invalid patterns.",
        description="Build annotation file for OTA image build.",
        parents=parent_parser,
    )
    build_exclude_cfg_cmd_arg_parser.add_argument(
        "-i",
        action="append",
        help="Take one file as input, expected to be a file containing a list of glob patterns. "
        "Can be specified multiple times.",
        required=True,
    )
    build_exclude_cfg_cmd_arg_parser.add_argument(
        "-o",
        help="The output target of the built merged exclude cfg file. "
        "If not specified, will directly output to stdout.",
    )
    build_exclude_cfg_cmd_arg_parser.set_defaults(handler=build_exclude_cfg_cmd)


def _load_input(f: Path) -> set[str]:
    res = set()
    try:
        loaded = (line.strip() for line in f.read_text().splitlines())
        for _pattern in loaded:
            for _filter_pa in invalid_patterns_pa:
                if _filter_pa.match(_pattern):
                    logger.info(f"ignore invalid pattern string: '{_pattern}'")
                    break
            else:
                res.add(_pattern)
        return res
    except Exception as e:
        _err_msg = f"failed to load input file: {e!r}"
        logger.exception(_err_msg)
        exit_with_err_msg(_err_msg)


def build_exclude_cfg_cmd(args: Namespace) -> None:
    logger.debug(f"calling {build_exclude_cfg_cmd.__name__} with {args}")

    # load base
    merged = set()
    for _input_f in args.i:
        _input_f = Path(_input_f)
        if not _input_f.is_file():
            exit_with_err_msg(f"input file {_input_f} specified but not found!")
        merged.update(_load_input(_input_f))

    # output built annotation file
    if output := args.o:
        try:
            with open(output, "w") as f:
                for _pattern in merged:
                    print(_pattern, file=f)
                f.flush()
        except Exception as e:
            _err_msg = f"failed to write to {output}: {e!r}"
            logger.exception(_err_msg)
            exit_with_err_msg(_err_msg)
    else:
        pprint(merged)
