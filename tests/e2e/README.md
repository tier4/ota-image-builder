# OTA image E2E test

Workflow implementation: [e2e_test.yaml](../../.github/workflows/e2e_test.yml).

## Overview

The E2E test validates the complete OTA image build-and-deploy cycle by:

1. **Building a test system image** that covers all the features we support (empty files, large files, xattrs, Docker images with deletions, special characters, etc.).
2. **Creating an OTA image** from the test system generated in step1 using the ota-image-builder.
3. **Verifying the OTA image** for ensuring the validity and trustworthness of the built image.
4. **Rebuilding the rootfs** from the OTA image payload.
5. **Comparing** the rebuilt rootfs against the original to verify bit-level integrity.

This end-to-end workflow ensures:

- **Payload integrity**: All file contents are preserved correctly.
- **Metadata processing**: File permissions, ownership, and xattrs are recorded into OTA image payload file_table properly.
- **Edge case handling**: Whiteout files, empty files, special characters, and large files are processed correctly.
- **Completeness**: No files are lost or corrupted during the build/deploy cycle.

## Test Implementation

### Phase 1: System Image Creation ([e2e_build_sys_img.sh](scripts/e2e_build_sys_img.sh))

Exports the `docker:28-dind` image as a base rootfs, then runs [e2e_setup_sys_img.sh](scripts/e2e_setup_sys_img.sh) inside a privileged podman container to setup it as follow:

- **10,000+ small/empty files**: For testing small files handling(inline, bundle).
- **500MB large file**: For testing large file handling(compression, slice).
- **Extended attributes**: File with custom xattrs to verify xattr preservation.
- **UTF-8 filenames**: Files with non-ascii and special characters.
- **Docker images with whiteout files**: Builds layered images that remove/modify files from base layers, creating overlay filesystem whiteout files to test Docker image support.

### Phase 2: OTA Image Building ([e2e_build_ota_image.sh](scripts/e2e_build_ota_image.sh))

Executes the full ota-image-builder workflow:

```text
init → prepare-sysimg → add-image (dev) → add-image (prd) → finalize → sign → pack-artifact
```

### Phase 3: Sign cert and signature validation(with `ota-image-tools verify-sign`)

Validates the built OTA image against the root-of-trust, and verifies its signature.

### Phase 4: Deploy the OTA image payload(with `ota-image-tools deploy-image`)

Deploys the OTA image payload to a folder.

### Phase 5: Validation ([compare_rootfs.py](scripts/compare_rootfs.py))

Performs deep comparison of the original vs. deployed rootfs from phase4:

- File content (SHA256 digest).
- File metadata (mode, uid, gid, file type).
- Extended attributes (xattrs).
- Symbolic link targets.
- Directory structure completeness.

Reports any discrepancies and exits with non-zero status on differences.
