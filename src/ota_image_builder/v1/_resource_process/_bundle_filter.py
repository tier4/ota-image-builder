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
"""Bundle small files into a single file."""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
from collections.abc import Generator
from hashlib import new
from pathlib import Path

from ota_image_libs._resource_filter import BundleFilter
from ota_image_libs.common import tmp_fname
from ota_image_libs.v1.consts import SUPPORTED_HASH_ALG
from ota_image_libs.v1.resource_table.db import ResourceTableDBHelper
from ota_image_libs.v1.resource_table.schema import (
    ResourceTableManifest,
    ResourceTableManifestTypedDict,
)
from simple_sqlite3_orm import gen_sql_stmt
from simple_sqlite3_orm.utils import wrap_value

from ota_image_builder._common import human_readable_size
from ota_image_builder._configs import cfg
from ota_image_builder.v1._resource_process._db_utils import count_entries_in_table

logger = logging.getLogger(__name__)


class BundleFilterProcesser:
    def __init__(
        self,
        *,
        resource_dir: Path,
        rst_dbf: Path,
        bundle_lower_bound: int = cfg.BUNDLE_LOWER_THRESHOULD,
        bundle_upper_bound: int = cfg.BUNDLE_UPPER_THRESHOULD,
        bundle_max_size: int = cfg.BUNDLE_MAX_SIZE,
    ) -> None:
        self._resource_dir = resource_dir
        self._db_helper = ResourceTableDBHelper(rst_dbf)
        self._lower_bound = bundle_lower_bound
        self._upper_bound = bundle_upper_bound
        self._bundle_max_size = bundle_max_size

    def process(self):
        with contextlib.closing(self._db_helper.connect_rstable_db()) as conn:
            rs_orm = self._db_helper.get_orm(conn)
            _table_name, _table_spec = rs_orm.orm_table_name, rs_orm.orm_table_spec
            # NOTE: resource_id starts from 1
            bundle_rs_id = count_entries_in_table(rs_orm) + 1

            # ------ bundle entries ------ #
            # (origin_rs_id, origin_digest), (offset, len)
            bundled_entries: dict[tuple[int, bytes], tuple[int, int]] = {}

            _tmp_bundle = self._resource_dir / tmp_fname()

            # origin_digest, origin_size
            # fmt: off
            entries_to_bundle: Generator[tuple[int, bytes, int]] = rs_orm.orm_select_entries(
                _stmt=_table_spec.table_select_stmt(
                    select_from=_table_name,
                    select_cols=("resource_id", "digest", "size"),
                    where_stmt=gen_sql_stmt(
                        "WHERE", "size", ">", wrap_value(self._lower_bound),
                        "AND", "size", "<=", wrap_value(self._upper_bound),
                        "AND", "filter_applied", "IS NULL",
                        end_with=None,
                    ),
                ),
                # fmt: on
                _row_factory=sqlite3.Row,
            ) # type: ignore[assignment]

            bundled_count, offset = 0, 0
            bundle_hasher = new(SUPPORTED_HASH_ALG)
            with open(_tmp_bundle, "wb") as bundle_f:
                for _rs_id, _entry_digest, _entry_size in entries_to_bundle:
                    if offset > self._bundle_max_size:
                        break

                    bundled_count += 1
                    bundled_entries[(_rs_id, _entry_digest)] = offset, _entry_size

                    _entry_resource = self._resource_dir / _entry_digest.hex()
                    _entry_contents = _entry_resource.read_bytes()
                    if len(_entry_contents) != _entry_size:
                        raise ValueError(
                            f"mismatch {_entry_size=} and {len(_entry_contents)=}"
                        )
                    bundle_hasher.update(_entry_contents)

                    bundle_f.write(_entry_contents)
                    offset += _entry_size
                    _entry_resource.unlink(missing_ok=True)

                # interrupt unfinished generator
                with contextlib.suppress(Exception):
                    entries_to_bundle.throw(StopIteration)

            if offset <= 0:
                return  # nothing is bundled

            # ------ update bundled entries rows in db ------ #
            rs_orm.orm_update_entries_many(
                set_cols=("filter_applied",),
                set_cols_value=(
                    ResourceTableManifestTypedDict(
                        filter_applied=BundleFilter(
                            bundle_resource_id=bundle_rs_id,
                            offset=_offset,
                            len=_len,
                        )
                    )
                    for _offset, _len in bundled_entries.values()
                ),
                where_cols=("resource_id",),
                where_cols_value=(
                    ResourceTableManifestTypedDict(resource_id=_resource_id)
                    for _resource_id, _ in bundled_entries.keys()
                ),
            )

            # ------ prepare bundle resource add bundle meta into db ------ #
            bundle_digest = bundle_hasher.digest()
            os.replace(_tmp_bundle, self._resource_dir / bundle_digest.hex())
            rs_orm.orm_insert_entry(
                ResourceTableManifest(
                    resource_id=bundle_rs_id,
                    digest=bundle_digest,
                    size=offset,
                )
            )
            logger.info(f"bundle_filter: total {bundled_count} files({human_readable_size(offset)}) are bundled.")
