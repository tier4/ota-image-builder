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
"""Pytest configuration for unit tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def sample_nv_tegra_release() -> str:
    """Sample /etc/nv_tegra_release content for testing."""
    return (
        "# R35 (release), REVISION: 4.1, GCID: 33958178, "
        "BOARD: t186ref, EABI: aarch64, DATE: Tue Aug  1 19:57:35 UTC 2023"
    )
