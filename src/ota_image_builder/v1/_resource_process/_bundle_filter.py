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
from hashlib import file_digest, new
from pathlib import Path
from typing import NamedTuple

import zstandard
from ota_image_libs._resource_filter import BundleFilter, CompressFilter
from ota_image_libs.common import tmp_fname
from ota_image_libs.v1.consts import SUPPORTED_HASH_ALG, ZSTD_COMPRESSION_ALG
from ota_image_libs.v1.resource_table.db import ResourceTableDBHelper, ResourceTableORM
from ota_image_libs.v1.resource_table.schema import (
    ResourceTableManifest,
    ResourceTableManifestTypedDict,
)
from simple_sqlite3_orm import gen_sql_stmt
from simple_sqlite3_orm.utils import wrap_value

from ota_image_builder._common import human_readable_size
from ota_image_builder._configs import cfg
from ota_image_builder.v1._resource_process._db_utils import count_entries_in_table

from ._common import ResourceID, Sha256DigestBytes

logger = logging.getLogger(__name__)

# If the bundle is smaller than 3MiB, we don't create bundle from it.
MINIMUM_BUNDLE_SIZE_RATIO = 0.05

BundledEntries = dict[tuple[ResourceID, Sha256DigestBytes], tuple[int, int]]
"""
(origin_rs_id, origin_digest), (offset, len)
"""


class EntryToBeBundled(NamedTuple):
    resource_id: int
    digest: bytes
    size: int


class BundleResult(NamedTuple):
    bundle_digest: bytes
    bundle_size: int
    bundled_entries: BundledEntries


class BundleCompressedResult(NamedTuple):
    compressed_digest: bytes
    compressed_size: int


def _batch_entries_with_filter(
    entries_to_bundle_gen: Generator[EntryToBeBundled],
    *,
    expected_bundle_size: int,
    min_bundle_ratio: float = MINIMUM_BUNDLE_SIZE_RATIO,
    excluded_resources: set[bytes],
) -> Generator[tuple[int, list[EntryToBeBundled]]]:
    _batch = []
    _this_batch_size = 0
    for _entry in entries_to_bundle_gen:
        _, _digest, _entry_size = _entry
        if _digest in excluded_resources:
            continue

        _batch.append(_entry)
        _this_batch_size += _entry_size
        if _this_batch_size > expected_bundle_size:
            yield _this_batch_size, _batch
            _batch, _this_batch_size = [], 0

    if _batch and _this_batch_size > expected_bundle_size * min_bundle_ratio:
        yield _this_batch_size, _batch


def _generate_one_bundle(
    entries_to_bundle: tuple[int, list[EntryToBeBundled]],
    *,
    resource_dir: Path,
    cctx: zstandard.ZstdCompressor,
) -> tuple[BundleResult, BundleCompressedResult] | None:
    bundle_size, entries = entries_to_bundle

    _bundled_entries = BundledEntries()
    _tmp_compressed_bundle = resource_dir / tmp_fname()
    bundle_hasher, offset = new(SUPPORTED_HASH_ALG), 0
    with (
        open(_tmp_compressed_bundle, "wb") as _compressed_bundle,
        cctx.stream_writer(_compressed_bundle, size=bundle_size) as compressor,
    ):
        for _rs_id, _entry_digest, _entry_size in entries:
            _bundled_entries[(_rs_id, _entry_digest)] = offset, _entry_size

            _entry_resource = resource_dir / _entry_digest.hex()
            _entry_contents = _entry_resource.read_bytes()
            if len(_entry_contents) != _entry_size:
                raise ValueError(f"mismatch {_entry_size=} and {len(_entry_contents)=}")

            bundle_hasher.update(_entry_contents)
            compressor.write(_entry_contents)
            offset += _entry_size
            _entry_resource.unlink(missing_ok=True)

    bundle_digest = bundle_hasher.digest()

    # check the compressed bundle
    with open(_tmp_compressed_bundle, "rb") as _compressed_bundle:
        _compressed_hasher = file_digest(_compressed_bundle, SUPPORTED_HASH_ALG)
    compressed_digest, compressed_size = (
        _compressed_hasher.digest(),
        _tmp_compressed_bundle.stat().st_size,
    )

    os.replace(_tmp_compressed_bundle, resource_dir / compressed_digest.hex())
    return (
        BundleResult(bundle_digest, offset, _bundled_entries),
        BundleCompressedResult(compressed_digest, compressed_size),
    )


def _commit_one_bundle(
    *,
    next_rs_id: int,
    bundle_res: BundleResult,
    compress_res: BundleCompressedResult,
    rs_orm: ResourceTableORM,
) -> int:
    # NOTE(20260213): need to cover the cases when the newly
    #   generated bundle is already presented in the resource_table.

    # ------ add compressed bundle into resource db ------ #
    if compressed_bundle := rs_orm.orm_select_entry(
        ResourceTableManifestTypedDict(
            digest=compress_res.compressed_digest,
        )
    ):
        compressed_bundle_rs_id = compressed_bundle.resource_id
    else:
        compressed_bundle_rs_id = next_rs_id
        next_rs_id += 1

        rs_orm.orm_insert_entry(
            ResourceTableManifest(
                resource_id=compressed_bundle_rs_id,
                digest=compress_res.compressed_digest,
                size=compress_res.compressed_size,
            )
        )

    # ------ add original bundle to the resource db ------ #
    if original_bundle := rs_orm.orm_select_entry(
        ResourceTableManifestTypedDict(
            digest=bundle_res.bundle_digest,
        )
    ):
        original_bundle_rs_id = original_bundle.resource_id
    # or commit the original bundle into db if not presented
    else:
        original_bundle_rs_id = next_rs_id
        next_rs_id += 1

        rs_orm.orm_insert_entry(
            ResourceTableManifest(
                resource_id=original_bundle_rs_id,
                digest=bundle_res.bundle_digest,
                size=bundle_res.bundle_size,
                filter_applied=CompressFilter(
                    resource_id=compressed_bundle_rs_id,
                    compression_alg=ZSTD_COMPRESSION_ALG,
                ),
            )
        )

    # ------ update the bundled entries rows in db ------ #
    bundled_entries = bundle_res.bundled_entries
    rs_orm.orm_update_entries_many(
        set_cols=("filter_applied",),
        set_cols_value=(
            ResourceTableManifestTypedDict(
                filter_applied=BundleFilter(
                    bundle_resource_id=original_bundle_rs_id,
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

    return next_rs_id


class BundleFilterProcesser:
    def __init__(
        self,
        *,
        resource_dir: Path,
        rst_dbf: Path,
        bundle_lower_bound: int = cfg.BUNDLE_LOWER_THRESHOULD,
        bundle_upper_bound: int = cfg.BUNDLE_UPPER_THRESHOULD,
        bundle_blob_size: int = cfg.BUNDLE_SIZE,
        bundle_compressed_max_sum: int = cfg.BUNDLES_COMPRESSED_MAXIMUM_SUM,
        protected_resources: set[bytes],
    ) -> None:
        self._protected_resources = protected_resources
        self._resource_dir = resource_dir
        self._db_helper = ResourceTableDBHelper(rst_dbf)
        self._lower_bound = bundle_lower_bound
        self._upper_bound = bundle_upper_bound
        self._bundle_blob_size = bundle_blob_size
        self._bundle_compressed_max_sum = bundle_compressed_max_sum

    def process(self):
        with contextlib.closing(self._db_helper.connect_rstable_db()) as conn:
            rs_orm = self._db_helper.get_orm(conn)
            _table_name, _table_spec = rs_orm.orm_table_name, rs_orm.orm_table_spec

            #
            # ------ processing entries and generating bundles ------ #
            #
            # fmt: off
            entries_to_bundle_gen: Generator[EntryToBeBundled]
            entries_to_bundle_gen = rs_orm.orm_select_entries(
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
                _row_factory=sqlite3.Row,
            ) # type: ignore[assignment]
            # fmt: on
            batch_gen = _batch_entries_with_filter(
                entries_to_bundle_gen,
                expected_bundle_size=self._bundle_blob_size,
                excluded_resources=self._protected_resources,
            )

            cctx = zstandard.ZstdCompressor(
                level=cfg.BUNDLE_ZSTD_COMPRESSION_LEVEL,
                write_checksum=True,
                write_content_size=True,
            )
            compressed_bundle_size = 0
            bundle_blobs_count = 0
            total_bundled_f_count = 0
            total_bundled_f_size = 0

            bundle_results: list[tuple[BundleResult, BundleCompressedResult]] = []

            for _batch in batch_gen:
                if compressed_bundle_size > self._bundle_compressed_max_sum:
                    break

                _res = _generate_one_bundle(
                    _batch,
                    resource_dir=self._resource_dir,
                    cctx=cctx,
                )
                if not _res:
                    break  # no bundle is created

                _bundle_res, _compress_res = _res

                compressed_bundle_size += _compress_res.compressed_size
                total_bundled_f_count += len(_bundle_res.bundled_entries)
                total_bundled_f_size += _bundle_res.bundle_size
                bundle_blobs_count += 1

                logger.info(
                    f"#{bundle_blobs_count} bundle generated:\n"
                    f"{len(_bundle_res.bundled_entries)} file entries are bundled.\n"
                    f"bundle size: {human_readable_size(_bundle_res.bundle_size)}.\n"
                    f"bundle compressed size: {human_readable_size(_compress_res.compressed_size)}"
                )
                bundle_results.append((_bundle_res, _compress_res))

            logger.info("all bundles are generated, start to update database ...")

            # finalize the query as we need to do db update next
            with contextlib.suppress(Exception):
                entries_to_bundle_gen.throw(StopIteration)

            #
            # ------ update database ------ #
            #
            # NOTE: resource_id starts from 1
            # NOTE: we cannot execute sqlite3 query when previous query hasn't finished,
            #       so we pre-calculate the next_rs_id here.
            next_rs_id: int = count_entries_in_table(rs_orm) + 1
            for _bundle_res, _compress_res in bundle_results:
                next_rs_id = _commit_one_bundle(
                    next_rs_id=next_rs_id,
                    bundle_res=_bundle_res,
                    compress_res=_compress_res,
                    rs_orm=rs_orm,
                )

            logger.info(
                (
                    f"bundle_filter: total {total_bundled_f_count} files({human_readable_size(total_bundled_f_size)}) are bundled.\n"
                    f"{bundle_blobs_count} bundle blobs are created.\n"
                    f"bundles blobs are compressed to {human_readable_size(compressed_bundle_size)} with zstd."
                )
            )
            logger.info(f"next_rs_id: {count_entries_in_table(rs_orm) + 1}")
