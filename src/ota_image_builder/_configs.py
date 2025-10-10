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
"""Configuration for the OTA Image Builder."""


class ImageBuilderConfig:
    READ_SIZE = 8 * 1024**2  # 8 MiB
    WORKER_THREADS = 6
    DB_ZSTD_COMPRESSION_LEVEL = 12

    INIT_PROCESS_MAX_CONCURRENT_TASKS = 256
    INIT_PROCESS_BATCH_WRITE_SIZE = 1024

    INLINE_THRESHOULD = 64  # bytes

    BUNDLE_LOWER_THRESHOULD = 64  # bytes
    BUNDLE_UPPER_THRESHOULD = 8192  # 8KiB
    # each bundle's size(uncompressed)
    BUNDLE_SIZE = 64 * 1024**2  # 64MiB
    # the upper bound of sum of all compressed bundles.
    #   take compression ratio of 6(15% of original size),
    #   we might have around 6~8 bundles.
    BUNDLES_COMPRESSED_MAXIMUM_SUM = 64 * 1024**2  # 64MiB
    BUNDLE_ZSTD_COMPRESSION_LEVEL = 12

    COMPRESSION_LOWER_THRESHOLD = 1024  # bytes
    COMPRESSION_MIN_RATIO = 1.25
    COMPRESSION_RESOURCE_SCAN_WORKER_THREADS = 6
    ZSTD_COMPRESSION_LEVEL = 9
    COMPRESSION_MAX_CONCURRENT = COMPRESSION_RESOURCE_SCAN_WORKER_THREADS * 2

    SLICE_SIZE = 32 * 1024**2  # 32MiB
    SLICE_CONCURRENT_TASKS = 32
    SLICE_UPDATE_BATCH_SIZE = 16


cfg = ImageBuilderConfig()
