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
"""cmds/add_update_agent_package.py: the --bundle spec."""

from __future__ import annotations

from pathlib import Path

import pytest

from ota_image_builder.cmds.add_update_agent_package import _parse_bundle


def test_a_bundle_spec_is_file_type_version_and_an_optional_arch(tmp_path: Path):
    f = tmp_path / "agent.tar.gz"
    f.write_bytes(b"x")
    assert _parse_bundle(f"{f}:tier4.ota.agent.v1:2.6.2") == (
        f,
        "tier4.ota.agent.v1",
        "2.6.2",
        None,
    )
    assert _parse_bundle(f"{f}:tier4.ota.agent.v1:2.6.2:aarch64") == (
        f,
        "tier4.ota.agent.v1",
        "2.6.2",
        "aarch64",
    )


def test_a_file_with_a_colon_in_its_path_still_parses(tmp_path: Path):
    d = tmp_path / "host:8080"
    d.mkdir()
    f = d / "agent.tar.gz"
    f.write_bytes(b"x")
    assert _parse_bundle(f"{f}:tier4.ota.agent.v1:2.6.2")[0] == f
    assert _parse_bundle(f"{f}:tier4.ota.agent.v1:2.6.2:aarch64")[3] == "aarch64"


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("agent.tar.gz:type", "wants FILE:TYPE:VERSION"),
        ("{f}::2.6.2", "needs a type and a version"),
        ("{f}:type:", "needs a type and a version"),
        ("/nowhere/agent.tar.gz:type:1.0", "no such bundle file"),
    ],
)
def test_bad_bundle_specs_are_refused(tmp_path: Path, capsys, spec, message):
    f = tmp_path / "agent.tar.gz"
    f.write_bytes(b"x")
    with pytest.raises(SystemExit):
        _parse_bundle(spec.format(f=f))
    assert message in capsys.readouterr().out
