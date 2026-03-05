# OTA Image Builder

The OTA image builder is a builder implementation of the [OTA image specification version 1](https://github.com/tier4/ota-image-libs/tree/main/spec), building OTA image from input system rootfs images.

## Features

- **File-level rootfs processing** — Scans system rootfs and registers all file entries and resources into SQLite databases (file_table, resource_table).
- **Content-addressable blob storage** — Prepares the resources by SHA256 into a flat blob storage (`blobs/sha256/`) with deduplication.
- **Storage optimization** — Optimizes the OTA image blob storage with bundling small files, compressing blobs with zstd, and slicing large files at image finalization.
- **Cryptographic signing** — Signs the image index by ES256 JWT with X.509 certificate chains.
- **Reproducible artifact packing** — Packages the OTA image into a reproducible ZIP artifact.
- **Multi-spec and Multi-payload OTA image support** — Supports building images with multiple ECU payloads and per-ECU system configurations.

## Installation

### From source

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/tier4/ota-image-builder.git
cd ota-image-builder
uv sync

# ota-image-builder and ota-image-tools will become available
```

### Standalone executable

Download the pre-built PyInstaller executable for your platform (x86_64 or arm64) from the [GitHub Releases](https://github.com/tier4/ota-image-builder/releases).

### Docker image

ota-image-builder is also availabe as semi-distroless docker images with multi-arch supports(`x86_64`, `arm64`).

```bash
docker pull ghcr.io/tier4/ota-image-builder/ota-image-builder:<version>
```

See [`docker/builder_release/README.md`](docker/builder_release/README.md) for image details and usage.

## Usage

### Build Pipeline

A typical OTA image build follows these steps:

```bash
# 1. Initialize an empty OTA image
ota-image-builder init \
  --annotations-file annotations.yaml \
  ota_image/

# 2. Clean system rootfs (remove /dev, /proc, /sys, /run, /tmp, etc.)
ota-image-builder prepare-sysimg \
  --rootfs-dir /path/to/rootfs

# 3. (Optional) Add OTAClient release package
ota-image-builder add-otaclient-package \
  --release-dir /path/to/otaclient_release \
  ota_image/

# 4. Add system image payload
ota-image-builder add-image \
  --annotations-file annotations.yaml \
  --release-key dev \
  --sys-config "ecu_id:sys_config.yaml" \
  --rootfs /path/to/rootfs \
  ota_image/

# 5. Finalize (with optimize the blob storage)
ota-image-builder finalize ota_image/

# 6. Sign the finalized image
ota-image-builder sign \
  --sign-cert sign.pem \
  --sign-key sign.key \
  --ca-cert intermediate_ca.pem \
  ota_image/

# 7. Pack into a ZIP artifact
ota-image-builder pack-artifact \
  -o ota_image.zip \
  ota_image/
```

### Subcommands

| Command | Description |
| ------- | ----------- |
| `version` | Print the version string |
| `version-info` | Print full version info with ota-image-libs version |
| `prepare-sysimg` | Clean system rootfs for OTA image building |
| `init` | Initialize an empty OTA image |
| `build-annotation` | Build/merge annotation YAML files |
| `build-exclude-cfg` | Build exclusion glob pattern files |
| `add-image` | Add a system image payload to the OTA image |
| `add-otaclient-package` | Add an OTAClient release package |
| `add-otaclient-package-compat` | Add an OTAClient package in legacy-compatible format |
| `finalize` | Optimize blob storage and finalize the image |
| `sign` | Sign the finalized image with ES256 JWT |
| `pack-artifact` | Package the OTA image into a ZIP artifact |

Use `-d`/`--debug` for debug logging.
Run `ota-image-builder <command> --help` for detailed usage of each subcommand.

## Specification

This tool builds OTA images conforming to the [OTA image specification version 1](https://github.com/tier4/ota-image-libs/tree/main/spec), defined in the [ota-image-libs](https://github.com/tier4/ota-image-libs) repository.

## Supported Python Versions

Python 3.12, 3.13

## Contributing

See [CLAUDE.md](CLAUDE.md) for development setup, architecture overview, and CI/CD details.

## License

This project is licensed under the Apache License 2.0. See [LICENSE](LICENSE) for details.
