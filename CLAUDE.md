# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`ota-image-builder` is a Python CLI tool that builds OTA update images from system rootfs images.
It is based on `ota-image-libs`, following the [OTA image specification version 1](https://github.com/tier4/ota-image-libs/tree/main/spec).

The builder converts a system rootfs into an optimized OTA image by:

- Scanning the rootfs and registering all file entries and resources (blobs) into SQLite databases (file_table, resource_table).
- Deduplicating resources by SHA256 content addressing into a flat blob storage (`blobs/sha256/`).
- At image build finalizing, applying storage optimization filters: bundling small files, compressing blobs with zstd, slicing large files.
- Signing the image index by ES256 JWT with X.509 certificate chains.
- Packing into a reproducible ZIP artifact for distribution.

## Commands for Dev

This project uses [uv](https://docs.astral.sh/uv/) for project management, dependency management, and virtualenv management.

**Testing:**

```bash
uv run pytest                                                                       # Run all unit tests
uv run pytest tests/unit/cmds/test_add_image.py                                    # Run a specific test file
uv run coverage run -m pytest && uv run coverage xml && uv run coverage report -m   # With coverage
```

**Linting & Formatting (ruff):**

```bash
uv run ruff check src/ tests/         # Lint
uv run ruff check --fix src/ tests/   # Auto-fix lint errors
uv run ruff format src/ tests/        # Format
```

**Type Checking:**

```bash
uv run pyright src/
```

**Building standalone executable (PyInstaller):**

```bash
uv run pyinstaller -s -F --optimize=2 --name ota-image-builder src/ota_image_builder/__main__.py
./dist/ota-image-builder version
```

**Building Docker image (`docker/builder_release/`):**

```bash
docker build -f docker/builder_release/Dockerfile -t ota-image-builder .
```

See [`docker/builder_release/README.md`](./docker/builder_release/README.md) for image details and usage.

**Pre-commit:**

```bash
uv run pre-commit install           # Install hooks (once after cloning)
uv run pre-commit run               # Run on changed files only
uv run pre-commit run --all-files   # Run all hooks manually
```

See [`.pre-commit-config.yaml`](./.pre-commit-config.yaml) for the full list of hooks.

## Architecture

### Project Layout

The repository contains a single package under `src/`:

- **`ota_image_builder/`** — OTA image builder CLI and core logic.

The `ota-image-tools` CLI entry point is also registered, but dispatches to `ota_image_tools` from the `ota-image-libs` dependency (detected by `argv[0]` name in `__main__.py`).

### CLI Commands (`ota_image_builder/cmds/`)

The builder provides the following subcommands (registered in `main.py`):

| Command | Module | Purpose |
| --- | --- | --- |
| `version` | (inline) | Print version string |
| `version-info` | (inline) | Print version with ota-image-libs version |
| `prepare-sysimg` | `prepare_sysimg.py` | Clean system rootfs (remove /dev, /proc, /sys, /run, /tmp, logs, docker artifacts) |
| `init` | `init.py` | Initialize empty OTA image (OCI layout, index.json, resources/ dir) |
| `build-annotation` | `build_annotation.py` | Build/merge annotation YAML files with key=value pairs |
| `build-exclude-cfg` | `build_exclude_cfg.py` | Build exclusion glob pattern files |
| `add-image` | `add_image.py` | Add system image to OTA image (core operation — processes rootfs, creates file_table, resource_table, manifests) |
| `add-otaclient-package` | `add_otaclient_package.py` | Add OTAClient release package |
| `add-otaclient-package-compat` | `add_otaclient_package_compat.py` | Legacy OTAClient compatibility |
| `finalize` | `finalize.py` | Optimize blobs (bundle, compress, slice filters) and finalize image |
| `sign` | `sign.py` | Sign finalized image with ES256 JWT |
| `pack-artifact` | `pack_artifact.py` | Package into reproducible ZIP artifact |

All commands accept `-d`/`--debug` for debug logging.

### OTA Image V1 Support (`ota_image_builder/v1/`)

Builder-side logic for composing OTA image v1 metadata and processing resources:

| Module | Purpose |
| --- | --- |
| `_image_index.py` | Image index initialization with annotations |
| `_image_manifest.py` | Image manifest composition per ECU payload |
| `_image_config.py` | Image config composition with rootfs statistics |
| `_resource_process/` | Resource processing pipeline (see below) |

### Resource Processing Pipeline (`v1/_resource_process/`)

Handles the transformation of rootfs files into optimized blob storage:

| Module | Purpose |
| --- | --- |
| `_rootfs_process.py` | Core rootfs scanning — concurrent inode handling, resource registration, hardlink detection, file_table and resource_table DB population |
| `_bundle_filter.py` | Bundles small files into larger blobs with zstd compression |
| `_compression_filter.py` | Applies zstd compression to individual resources above a size threshold |
| `_slice_filter.py` | Slices large files into fixed-size sub-resources |
| `_db_utils.py` | SQLite ORM utilities for file_table and resource_table using `simple-sqlite3-orm` |
| `_common.py` | Processing utilities shared across filters |

The detailed filter configurations like thresholds, compression levels, and concurrency settings are in [`_configs.py`](./src/ota_image_builder/_configs.py) (`ImageBuilderConfig` class).

### Relationship with `ota-image-libs`

The builder is implemented with libraries of schemas, database interfaces, signing utilities, and artifact packing from `ota-image-libs`:

- `ota_image_libs.v1.consts` — File names, OCI layout constants
- `ota_image_libs.v1.annotation_keys` — Standardized annotation key names
- `ota_image_libs.v1.image_index.schema` — `ImageIndex` data structures
- `ota_image_libs.v1.image_manifest.schema` — `ImageManifest`, `ImageIdentifier`, `OTAReleaseKey`
- `ota_image_libs.v1.image_config.schema` — `ImageConfig`, `SysConfig`
- `ota_image_libs.v1.file_table.schema` / `db` — File and inode metadata schemas and DB operations
- `ota_image_libs.v1.resource_table.schema` / `db` — Resource manifest schemas and DB operations
- `ota_image_libs.v1._resource_filter` — `BundleFilter`, `CompressFilter`, `SliceFilter`
- `ota_image_libs.v1.index_jwt.utils` — JWT signing
- `ota_image_libs.v1.artifact.packer` — ZIP artifact packing
- `ota_image_libs._crypto.x509_utils` — Certificate chain handling
- `ota_image_libs.common.metafile_base` — Metadata file export/import helpers

The OTA image specification lives in the `ota-image-libs` repo under `spec/`.
Consult those spec documents when working on schemas or understanding the expected image format.

## CI/CD

Four GitHub Actions workflows live under [`.github/workflows/`](./.github/workflows/):

- **`unit_test.yml`** — Lint (ruff) and unit test with coverage, SonarCloud scan.
- **`e2e_test.yml`** — Full build-verify-deploy pipeline E2E test. Also see [`tests/e2e/README.md`](./tests/e2e/README.md).
- **`release.yml`** — Multi-arch (x86_64, arm64) PyInstaller executables and Docker images, published to GHCR.
- **`lock_file_management.yml`** — Syncs `uv.lock` and exports `requirements.txt` on `pyproject.toml` changes.

## Code Style

Ruff (linting, formatting), pyright (type checking), and coverage settings are all configured in [`pyproject.toml`](./pyproject.toml).
