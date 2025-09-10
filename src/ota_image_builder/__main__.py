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

import os
import sys
from multiprocessing import freeze_support

OTA_IMAGE_TOOLS = "ota_image_tools"

if __name__ == "__main__":
    freeze_support()

if __name__ == "__main__":
    from ota_image_builder._common import configure_logging

    configure_logging()

    # special treatment when the program is called with name ota_image_tools.
    _cli_name = os.path.basename(sys.argv[0])
    if _cli_name.replace("-", "_").startswith(OTA_IMAGE_TOOLS):
        from ota_image_tools.__main__ import main

        main()

    else:
        from ota_image_builder.main import main

        main()
