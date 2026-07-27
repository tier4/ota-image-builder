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
"""Determinism tests for the finalize phase resource filters.

Filter results must be a pure function of the resource table contents. Two things
must not leak into them:

1. The SQLite query plan. A SELECT without ORDER BY has no guaranteed row order,
   it only happens to walk the table in `resource_id` order as long as the planner
   picks a full table scan (`resource_id` is an INTEGER PRIMARY KEY, hence a rowid
   alias). Any index the planner prefers changes the visiting order.
2. The worker thread completion order, which decides in which order the filter
   results are applied back to the database, and therefore which `resource_id` is
   assigned to each newly inserted resource.

Both perturb the `resource_id` assignment, and the resource table blob digest is
recorded in the image index, so either one changes the digest of the shipped image.

NOTE: ordering is always on `resource_id`, never on path or digest. `resource_id`
is assigned in rootfs walk order, and preserving that clustering is what keeps OTA
over-fetch low.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from hashlib import sha256
from pathlib import Path

from ota_image_builder._common import WriteThreadSafeDict
from ota_image_builder.v1._resource_process._bundle_filter import BundleFilterProcesser
from ota_image_builder.v1._resource_process._compression_filter import (
    CompressionFilterProcesser,
)
from ota_image_builder.v1._resource_process._db_utils import (
    init_resource_table_db,
    vacuum_db,
)
from ota_image_builder.v1._resource_process._slice_filter import (
    SliceFilterProcesser,
    SliceResult,
)

RST_TABLE_NAME = "rs_manifest"

SIZE_BASE = 4096
SIZE_STEP = 16
"""Entry sizes are anti-correlated with resource_id: entry N is smaller than entry
N-1. A query plan that walks a `size` index therefore visits the entries in exactly
the reverse of `resource_id` order, which makes a dependency on the SELECT row order
observable.
"""


def _blob_content(resource_id: int, size: int, *, compressible: bool) -> bytes:
    """Build deterministic, per-resource unique content of exactly <size> bytes."""
    prefix = f"resource-{resource_id}:".encode()
    if compressible:
        return (prefix + b"A" * size)[:size]

    # Not compressible, and every slice of it is unique across resources.
    _buf, _block = bytearray(), sha256(prefix).digest()
    while len(_buf) < size:
        _buf += _block
        _block = sha256(_block).digest()
    return bytes(_buf[:size])


def _make_entries(count: int, *, compressible: bool) -> list[tuple[int, bytes]]:
    """Build <count> (resource_id, contents) pairs, resource_id starting from 1."""
    return [
        (
            _resource_id,
            _blob_content(
                _resource_id,
                SIZE_BASE - _resource_id * SIZE_STEP,
                compressible=compressible,
            ),
        )
        for _resource_id in range(1, count + 1)
    ]


def _seed_db(
    rst_dbf: Path,
    entries: list[tuple[int, bytes]],
    *,
    perturbed_where: str | None = None,
) -> None:
    """Bootstrap a resource table DB and insert <entries> with explicit resource_ids.

    Args:
        perturbed_where: when set, an index over `size` is created to make the
            planner abandon the rowid ordered table scan, and the helper asserts
            that the perturbation actually changed the row order of
            `SELECT resource_id FROM <table> <perturbed_where>`. Without that
            self-check the determinism assertions would silently go vacuous if a
            future SQLite release stopped picking the index.
    """
    init_resource_table_db(rst_dbf)
    with closing(sqlite3.connect(rst_dbf)) as conn:
        for _resource_id, _contents in entries:
            conn.execute(
                f"INSERT INTO {RST_TABLE_NAME} (resource_id, digest, size) VALUES (?,?,?)",
                (_resource_id, sha256(_contents).digest(), len(_contents)),
            )

        if perturbed_where is not None:
            conn.execute(f"CREATE INDEX idx_size ON {RST_TABLE_NAME}(size)")
        conn.commit()

        if perturbed_where is not None:
            _visit_order = [
                _row[0]
                for _row in conn.execute(
                    f"SELECT resource_id FROM {RST_TABLE_NAME} {perturbed_where}"
                )
            ]
            assert _visit_order != sorted(_visit_order), (
                "the query plan perturbation is not effective, "
                "the determinism assertion would be vacuous"
            )


def _write_blobs(resource_dir: Path, entries: list[tuple[int, bytes]]) -> None:
    resource_dir.mkdir(parents=True, exist_ok=True)
    for _, _contents in entries:
        (resource_dir / sha256(_contents).hexdigest()).write_bytes(_contents)


def _dump_table(rst_dbf: Path) -> list[tuple]:
    with closing(sqlite3.connect(rst_dbf)) as conn:
        return list(
            conn.execute(
                f"SELECT resource_id, digest, size, filter_applied FROM {RST_TABLE_NAME}"
                " ORDER BY resource_id"
            )
        )


def _db_digest(rst_dbf: Path) -> str:
    vacuum_db(rst_dbf)
    return sha256(rst_dbf.read_bytes()).hexdigest()


class TestSelectOrderDeterminism:
    """The filters must visit resources in resource_id order, not in query plan order."""

    def test_bundle_packs_in_resource_id_order(self, tmp_path: Path):
        entries = _make_entries(64, compressible=True)
        lower_bound, upper_bound = 100, SIZE_BASE

        rst_dbf = tmp_path / "resource_table.db"
        resource_dir = tmp_path / "resources"
        _seed_db(
            rst_dbf,
            entries,
            perturbed_where=(
                f"WHERE size > {lower_bound} AND size <= {upper_bound}"
                " AND filter_applied IS NULL"
            ),
        )
        _write_blobs(resource_dir, entries)

        processer = BundleFilterProcesser(
            resource_dir=resource_dir,
            rst_dbf=rst_dbf,
            bundle_lower_bound=lower_bound,
            bundle_upper_bound=upper_bound,
            # each entry is around 4KiB, so each bundle takes exactly 2 entries
            bundle_blob_size=5000,
            bundle_compressed_max_sum=64 * 1024 * 1024,
            protected_resources=set(),
        )
        bundle_result = processer._process_bundle()

        packed_resource_ids = [
            _resource_id
            for _bundle_res, _ in bundle_result
            for _resource_id, _ in _bundle_res.bundled_entries
        ]
        assert packed_resource_ids == [_resource_id for _resource_id, _ in entries]

    def test_compression_visits_in_resource_id_order(self, tmp_path: Path):
        """NOTE: driven with a single worker thread and a single concurrent job, so
        that the completion order equals the SELECT row order and this test is about
        the SELECT alone.
        """
        entries = _make_entries(32, compressible=True)
        lower_bound = 100

        rst_dbf = tmp_path / "resource_table.db"
        resource_dir = tmp_path / "resources"
        _seed_db(
            rst_dbf,
            entries,
            perturbed_where=f"WHERE size > {lower_bound} AND filter_applied IS NULL",
        )
        _write_blobs(resource_dir, entries)

        processer = CompressionFilterProcesser(
            resource_dir=resource_dir,
            rst_dbf=rst_dbf,
            size_lower_bound=lower_bound,
            compression_ratio_threshold=1.1,
            worker_threads=1,
            concurrent_jobs=1,
            protected_resources=set(),
        )
        _, compressed = processer._process_compression()

        assert list(compressed) == [_resource_id for _resource_id, _ in entries]

    def test_slice_visits_in_resource_id_order(self, tmp_path: Path):
        """NOTE: driven with a single worker thread and a single concurrent task, so
        that the completion order equals the SELECT row order and this test is about
        the SELECT alone.
        """
        entries = _make_entries(32, compressible=False)
        slice_size = 512

        rst_dbf = tmp_path / "resource_table.db"
        resource_dir = tmp_path / "resources"
        _seed_db(
            rst_dbf,
            entries,
            perturbed_where=(
                f"WHERE size > {slice_size * 2} AND filter_applied IS NULL"
            ),
        )
        _write_blobs(resource_dir, entries)

        processer = SliceFilterProcesser(
            resource_dir=resource_dir,
            rst_dbf=rst_dbf,
            slice_size=slice_size,
            worker_threads=1,
            concurrent_tasks=1,
            protected_resources=set(),
        )
        _, _, slice_result = processer._process_slicing()

        assert [_resource_id for _resource_id, _ in slice_result] == [
            _resource_id for _resource_id, _ in entries
        ]


class TestUpdateOrderDeterminism:
    """Applying filter results must not depend on the thread completion order."""

    def test_compression_update_is_completion_order_independent(self, tmp_path: Path):
        entries = _make_entries(16, compressible=True)
        compression_results = [
            (
                _resource_id,
                (
                    sha256(f"compressed-{_resource_id}".encode()).digest(),
                    128 + _resource_id,
                ),
            )
            for _resource_id, _ in entries
        ]

        db_files: list[Path] = []
        for _name, _completion_order in (
            ("in_order", compression_results),
            ("reversed", list(reversed(compression_results))),
        ):
            rst_dbf = tmp_path / f"resource_table_{_name}.db"
            _seed_db(rst_dbf, entries)

            compressed = WriteThreadSafeDict()
            for _resource_id, _result in _completion_order:
                compressed[_resource_id] = _result

            CompressionFilterProcesser(
                resource_dir=tmp_path,
                rst_dbf=rst_dbf,
                protected_resources=set(),
            )._update_db(compressed)
            db_files.append(rst_dbf)

        assert _dump_table(db_files[0]) == _dump_table(db_files[1])
        assert _db_digest(db_files[0]) == _db_digest(db_files[1])

    def test_slice_update_is_append_order_independent(self, tmp_path: Path):
        entries = _make_entries(16, compressible=False)
        slice_results = [
            (
                _resource_id,
                {
                    sha256(f"slice-{_resource_id}-{_idx}".encode()).digest(): 256 + _idx
                    for _idx in range(2)
                },
            )
            for _resource_id, _ in entries
        ]

        db_files: list[Path] = []
        for _name, _completion_order in (
            ("in_order", slice_results),
            ("reversed", list(reversed(slice_results))),
        ):
            rst_dbf = tmp_path / f"resource_table_{_name}.db"
            _seed_db(rst_dbf, entries)

            sliced = SliceResult()
            for _result in _completion_order:
                sliced.append(_result)

            SliceFilterProcesser(
                resource_dir=tmp_path,
                rst_dbf=rst_dbf,
                # smaller than the number of results, so that the batching itself
                # also depends on the append order
                db_update_batch_size=4,
                protected_resources=set(),
            )._update_db(sliced)
            db_files.append(rst_dbf)

        assert _dump_table(db_files[0]) == _dump_table(db_files[1])
        assert _db_digest(db_files[0]) == _db_digest(db_files[1])
