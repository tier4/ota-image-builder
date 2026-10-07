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

### Partition-based payloads

For a device whose root is read-only and integrity-protected, the payload is not the files of a rootfs but whole partition images (see the [partition-based payload spec](https://github.com/tier4/ota-image-libs/blob/main/spec/partition_image.md)).
The platform's own tooling produces the blobs — a root filesystem image with its dm-verity hash tree, the boot files that carry the root hash — and a small spec JSON beside them; `add-partition-image` takes over from there, in place of step 4:

```bash
ota-image-builder add-partition-image \
  --annotations-file annotations.yaml \
  --release-key dev \
  --sys-config "ecu_id:sys_config.yaml" \
  --spec /path/to/blobs/spec.json \
  ota_image/
```

```json
{
  "delivery": "direct",
  "version": "1.2.0",
  "partitions": [
    {"name": "rootfs", "action": "write", "image": "rootfs.img",
     "filesystem": "ext4", "verity": {"root_hash": "…", "hash_offset": 1468006400}},
    {"name": "boot",     "action": "write", "image": "boot.tar"},
    {"name": "scratch",  "action": "mkfs"},
    {"name": "identity", "action": "keep"},
    {"name": "optdata",  "action": "keep"}
  ]
}
```

A written role named `boot` is the slot's boot files, a tar unpacked into the boot directory; every other written role is a partition image, written as it is.
`"delivery": "vendor-package"` with `"package": {"file": "…", "format": "…"}` describes one opaque package the platform's own updater applies.
Partition images, data images, firmware packages and the vendor package are stored **zstd-compressed** (`--zstd-level`, default 19 with long-range matching; `--no-compress` stores them as they are); the descriptor then names the stored bytes and its annotations what they decode to, and the agent decodes the blob on its way to the partition.
`finalize` never bundles, compresses or slices these blobs and `pack-artifact` stores them as they are, so an agent streams a partition image straight from the artifact.
The `sys_config` of such a payload is informational: its items are applied when the image is built.

A partition may ship a **block diff** against a previous build instead of its image, so that a campaign transfers the change rather than the whole partition. The delta is built by `add-partition-image` from the previous build's image, named in the spec or on the command line, and the payload then carries the delta alone:

```json
{"name": "rootfs", "action": "write", "image": "rootfs.img",
 "filesystem": "ext4", "verity": {"root_hash": "…", "hash_offset": 1468006400},
 "delta": {"from": "../1.1.0/rootfs.img"}}
```

```bash
ota-image-builder add-partition-image ... --delta-from rootfs=/releases/1.1.0/rootfs.img ota_image/
```

The image file is still named so that its digest, size and verity go into the payload: that is what the agent verifies the reconstruction against.
The delta names the bytes it applies to by digest, because on the device those bytes are the committed slot's own partition and a digest identifies them exactly; the agent reads them where they lie, so a delta of any size needs no staging space. A device at another version needs a payload built for it.
Measured on two builds of an 8.15 GiB rootfs image: 109 MB as a delta, 2.40 GB as a compressed image, 8.75 GB raw.

#### Data images

What changes on its own cadence -- a set of ML models, say -- rides beside the partitions as a **data image**: a read-only filesystem image with its verity hash tree appended, which the device keeps as a file outside the slots and mounts at a path. The spec lists them under `data_images`; the agent that applies the payload writes them, under either delivery:

```json
"data_images": [
  {"name": "models", "version": "2026.9.1", "mount": "/opt/models",
   "image": "models.img", "filesystem": "squashfs",
   "verity": {"root_hash": "…", "hash_offset": 209715200},
   "requires": {"rootfs": {"min": "1.2.0", "max": "2.0.0"}}}
]
```

`requires` pins which rootfs (or other data image) versions the image goes with; the device refuses the rest. A data image is stored compressed and may ship as a block diff like a partition (`"delta": {"from": …}` or `--delta-from models=/releases/2026.8.0/models.img`, against the image the device holds; `--delta-from data:NAME=IMAGE` when a data image and a partition role share a name). A data image that did not change since that release ships as a delta from itself -- a few bytes saying so -- which is why the data image build is reproducible (fixed timestamps, derived UUIDs). The same spec also builds the payload that updates the data images **alone**:

```bash
ota-image-builder add-partition-image ... --data-only ota_image/
```

Every partition is then `keep` and no partition blob is stored, so a release spec yields both the rootfs release and the data-image-only campaign, and the two share the data image blob by digest.

#### Firmware

What boots before any partition image is read -- the bootloader chain and the firmware beside it -- is the platform's own updater's to write, from a package in its format. The spec names that package under `firmware`, so that it travels with the release, is verified with it and is judged by the same trial boot; the agent stages it where the platform's updater picks it up (a UEFI capsule on the EFI system partition, say) and the platform applies it on the reboot:

```json
"firmware": {"name": "firmware", "version": "2.0.0", "format": "<the platform updater's package format>",
             "file": "firmware.pkg", "requires": {"rootfs": {"min": "2.2.0"}}}
```

`format` is opaque here; an agent applies the formats its platform takes and refuses the rest. Firmware is slotted with the boot chain where it is slotted at all, so on a platform whose rootfs slot follows the boot chain a firmware update is also a slot switch: the payload carries the slot roles too, if only as a block diff that copies the committed slot. `--data-only` drops it along with the partitions.

### Subcommands

| Command | Description |
| ------- | ----------- |
| `version` | Print the version string |
| `version-info` | Print full version info with ota-image-libs version |
| `prepare-sysimg` | Clean system rootfs for OTA image building |
| `init` | Initialize an empty OTA image |
| `build-annotation` | Build/merge annotation YAML files |
| `build-exclude-cfg` | Build exclusion glob pattern files |
| `add-image` | Add a system image payload (file-based) to the OTA image |
| `add-partition-image` | Add a partition-based payload: whole partition images or a vendor package, from a spec JSON |
| `add-otaclient-package` | Add an OTAClient release package |
| `add-update-agent-package` | Add an update agent's bundle(s) as the image's update agent release package (see the note below) |
| `add-otaclient-package-compat` | Add an OTAClient package in legacy-compatible format |
| `finalize` | Optimize blob storage and finalize the image |
| `sign` | Sign the finalized image with ES256 JWT |
| `pack-artifact` | Package the OTA image into a ZIP artifact |

Use `-d`/`--debug` for debug logging.
Run `ota-image-builder <command> --help` for detailed usage of each subcommand.

**Compatibility note.** A consumer refuses an `index.json` that lists a manifest kind its ota-image-libs does not know, so an image must carry only entries every one of its consumers can read. The update agent release package (`add-update-agent-package`) is known from ota-image-libs 0.6.0 on: an image that otaclient releases before that (v3.14 and earlier) or other tools on an older library must read carries none, and `add-otaclient-package` therefore writes the OTAClient release package only. Partition-based payloads are read as file-based descriptors by older libraries and do not stop them from finding their own payload.

## Specification

This tool builds OTA images conforming to the [OTA image specification version 1](https://github.com/tier4/ota-image-libs/tree/main/spec), defined in the [ota-image-libs](https://github.com/tier4/ota-image-libs) repository.

## Supported Python Versions

Python 3.12, 3.13

## Contributing

See [CLAUDE.md](CLAUDE.md) for development setup, architecture overview, and CI/CD details.

## License

This project is licensed under the Apache License 2.0. See [LICENSE](LICENSE) for details.
