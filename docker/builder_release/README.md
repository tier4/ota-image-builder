# OTA image builder release by docker image

Distribute OTA image builder by docker image.

## Images

The distro is built with Ubuntu 24.04.

The images are available as follow:
1. `ghcr.io/tier4/ota-image-builder:latest`
2. `ghcr.io/tier4/ota-image-builder:{major}.{minor}.{patch}`, like `ghcr.io/tier4/ota-image-builder:v0.6.1` or `ghcr.io/tier4/ota-image-builder:0.6.1`

Both x86_64 and arm64 platform are supported.

## Installing ota-image-builder from the image

The distribution can be found in `/ota-image-builder` folder in the image,
and the entrypoint binary is `/ota-image-builder/ota-image-builder`.

Your can add `ota-image-builder` to your docker image by updating your dockerfile as follow:

```dockerfile
ARG YOUR_BASE_IMAGE

FROM ${YOUR_BASE_IMAGE}

ARG OTA_IMAGE_BUILDER_VER=v0.6.1

# will automatically choose the x86_64 or arm64 ota-image-builder variants
COPY --from=ghcr.io/tier4/ota-image-builder:${OTA_IMAGE_BUILDER_VER} /ota-image-builder /opt/ota-image-builder
```