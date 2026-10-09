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
"""`build-data-images`: the data images a release carries, from the list the image
build hands in. The list is the product's own asset declaration; nothing here names
an image.

    data_images:
      - name: models                 # what a campaign addresses
        mount: /opt/autoware/models  # where the device mounts it, and where the image
                                     # build put the files in the rootfs tree
        version: xx1/2.6.1           # or version_file: a file in the tree whose first
                                     # line is the version
        source: /srv/models          # optional: take the files from here, not the mount
        requires:                    # optional: half-open version ranges (min, max)
          rootfs: {min: "2.0.0"}
        component: MODELS            # optional: the name the device reports this image's
                                     # version under, when not the image name

Each entry becomes <out>/<name>.img, .env and .spec.json, and its files are removed
from the tree: the rootfs carries the image once, as the built-in copy the platform
installer installs, and the mount point stays an empty directory.
`prepare-partition-image --data-images <out>` then lists them in spec.json.
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from ota_image_builder._common import exit_with_err_msg
from ota_image_builder.v1 import _partition_blobs as blobs
from ota_image_builder.v1._partition_image import VersionRangeSpec

if TYPE_CHECKING:
    from argparse import ArgumentParser, Namespace, _SubParsersAction

logger = logging.getLogger(__name__)

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
KEYS = {"name", "mount", "source", "version", "version_file", "requires", "component"}


def build_data_images_cmd_args(
    sub_arg_parser: _SubParsersAction[ArgumentParser], *parent_parser: ArgumentParser
) -> None:
    parser = sub_arg_parser.add_parser(
        name="build-data-images",
        help=(
            _help_txt := "Build the data images a product's list declares (one "
            "squashfs with its verity hash tree each) out of a rootfs tree, taking "
            "their files out of the tree"
        ),
        description=_help_txt + ". See the README for the list's shape.",
        parents=parent_parser,
    )
    parser.add_argument("--config", required=True, help="The data image list (yaml).")
    parser.add_argument(
        "--rootfs-dir",
        required=True,
        help="The rootfs tree the files are in; they are moved out.",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="Where the images go, one <name>.img/.env/.spec.json each.",
    )
    parser.add_argument(
        "--version",
        action="append",
        default=[],
        metavar="<name>=<version>",
        help="The version for <name>, over the config's. Can be used multiple times.",
    )
    parser.set_defaults(handler=build_data_images_cmd)


def check_list(
    path: Path, doc: object, overrides: dict[str, str]
) -> list[dict[str, Any]]:
    """The entries, checked and normalised."""

    def fail(msg: str):
        exit_with_err_msg(f"{path}: {msg}")

    if not isinstance(doc, dict):
        fail("expected a mapping with a data_images list")
    assert isinstance(doc, dict)
    unknown = set(doc) - {"data_images"}
    if unknown:
        fail(f"unknown keys {sorted(unknown)}; only data_images is read")
    images = doc.get("data_images")
    if images is None:
        images = []
    if not isinstance(images, list):
        fail("data_images must be a list")

    def scalar(entry: dict, key: str, what: str, *, required: bool = False) -> str:
        v = entry.get(key)
        if v is None:
            if required:
                fail(f"{what}: {key} is required")
            return ""
        if not isinstance(v, str) or not v:
            fail(f"{what}: {key} must be a non-empty string")
        if any(ord(c) < 32 for c in v):
            fail(f"{what}: {key} may not contain control characters")
        return v

    def path_in_tree(
        entry: dict, key: str, what: str, *, required: bool = False
    ) -> str:
        v = scalar(entry, key, what, required=required)
        if v and (not v.startswith("/") or v == "/" or "//" in v or "/../" in v + "/"):
            fail(f"{what}: {key} must be an absolute path in the tree, not {v!r}")
        return v.rstrip("/")

    over = dict(overrides)
    seen: set[str] = set()
    entries = []
    for i, entry in enumerate(images):
        what = f"data_images[{i}]"
        if not isinstance(entry, dict):
            fail(f"{what}: expected a mapping")
        unknown = set(entry) - KEYS
        if unknown:
            fail(f"{what}: unknown keys {sorted(unknown)}; known: {sorted(KEYS)}")
        name = scalar(entry, "name", what, required=True)
        if not NAME.match(name):
            fail(f"{what}: {name!r} is not a data image name (letters, digits, . _ -)")
        if name in seen:
            fail(f"data image {name!r} is listed twice")
        seen.add(name)
        what = f"data image {name}"
        mount = path_in_tree(entry, "mount", what, required=True)
        source = path_in_tree(entry, "source", what) or mount
        version = over.pop(name, None) or scalar(entry, "version", what)
        version_file = path_in_tree(entry, "version_file", what)
        if not version and not version_file:
            fail(
                f"{what}: version or version_file is required (or --version {name}=<version>)"
            )
        if " " in version:
            fail(f"{what}: a version has no spaces: {version!r}")
        component = scalar(entry, "component", what)
        if component and not NAME.match(component):
            fail(
                f"{what}: component {component!r} is not a component name (letters, digits, . _ -)"
            )
        requires = entry.get("requires") or {}
        if not isinstance(requires, dict):
            fail(f"{what}: requires must be a mapping of <what> to {{min, max}}")
        ranges: dict[str, VersionRangeSpec] = {}
        for what_req, bounds in requires.items():
            if not isinstance(what_req, str) or not NAME.match(what_req):
                fail(
                    f"{what}: requires: {what_req!r} is not rootfs or a data image name"
                )
            if (
                not isinstance(bounds, dict)
                or not bounds
                or set(bounds) - {"min", "max"}
            ):
                fail(
                    f"{what}: requires.{what_req} must be a mapping with min and/or max"
                )
            lo, hi = (str(bounds.get(k) or "") for k in ("min", "max"))
            for b in (lo, hi):
                if any(c in b for c in " \t\n:="):
                    fail(f"{what}: requires.{what_req}: {b!r} is not a version")
            ranges[what_req] = VersionRangeSpec(min=lo or None, max=hi or None)
        entries.append(
            {
                "name": name,
                "mount": mount,
                "source": source,
                "version": version,
                "version_file": version_file,
                "component": component,
                "requires": ranges,
            }
        )
    if over:
        fail(f"--version names no data image in the list: {sorted(over)}")
    return entries


def _build_one(entry: dict[str, Any], tree: Path, rootfs_dir: str, out: Path) -> None:
    name, source = entry["name"], entry["source"]
    src = Path(f"{tree}{source}")
    if not src.is_dir():
        exit_with_err_msg(
            f"data image {name}: {source} is not a directory in {rootfs_dir} "
            "(the image build puts the files there first)"
        )
    if not any(src.iterdir()):
        exit_with_err_msg(f"data image {name}: {source} is empty in {rootfs_dir}")
    version = entry["version"]
    if not version:
        vf = Path(f"{tree}{entry['version_file']}")
        if not vf.is_file():
            exit_with_err_msg(
                f"data image {name}: version_file {entry['version_file']} is not in {rootfs_dir}"
            )
        text = vf.read_text()
        version = "".join(text.splitlines()[0].split()) if text else ""
        if not version:
            exit_with_err_msg(f"data image {name}: {entry['version_file']} is empty")
    logger.info(f"== {name} {version} ({source} -> {entry['mount']})")
    try:
        blobs.build_data_image(
            src, out, name, version, entry["mount"],
            component=entry["component"] or None, requires=entry["requires"],
        )  # fmt: skip
    except blobs.BlobBuildError as e:
        exit_with_err_msg(f"data image {name}: {e}")
    # The files now live in the image; a copy left in the tree would be carried twice
    # and hidden under the mount anyway.
    try:
        for child in src.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    except OSError as e:
        exit_with_err_msg(
            f"data image {name}: could not remove the files under {src} from the tree: {e}"
        )
    Path(f"{tree}{entry['mount']}").mkdir(parents=True, exist_ok=True)


def build_data_images_cmd(args: Namespace) -> None:
    logger.debug(f"calling {build_data_images_cmd.__name__} with {args}")
    config = Path(args.config)
    if not config.is_file():
        exit_with_err_msg(f"no such file: {config}")
    if not Path(args.rootfs_dir).is_dir():
        exit_with_err_msg(f"no such directory: {args.rootfs_dir}")
    overrides: dict[str, str] = {}
    for o in args.version:
        name, sep, version = o.partition("=")
        if not sep or not name or not version:
            exit_with_err_msg(f"--version takes <name>=<version>, not '{o}'")
        overrides[name] = version
    try:
        doc = yaml.safe_load(config.read_text())
    except yaml.YAMLError as e:
        exit_with_err_msg(f"{config}: not yaml: {e}")
    tree = Path(args.rootfs_dir).resolve()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    out = out.resolve()

    entries = check_list(config, doc, overrides)
    if not entries:
        logger.info(f"no data images listed in {config}")
        return
    for entry in entries:
        _build_one(entry, tree, args.rootfs_dir, out)
    logger.info(
        f"built {len(entries)} data image(s) in {out}; the tree no longer holds their files"
    )
    logger.info(f"next:    the platform installer's --data-images {out}, then")
    logger.info(
        f"         ota-image-builder prepare-partition-image ... --data-images {out}"
    )
