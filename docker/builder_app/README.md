# Build OTA Image Builder container image

##

```shell
sudo docker buildx create \
  --name container-builder \
  --driver docker-container \
  --bootstrap --use
```

```shell
VERSION=0.0.21-dev0
BASE_URI=ghcr.io/tier4/ota-image-builder/ota-image-builder
sudo docker build \
    --builder container-builder \
    --platform linux/amd64,linux/arm64 \
    -f ../docker/Dockerfile \
    --no-cache \
    --progress plain \
    --build-arg=BUILDER_VERSION=${VERSION} \
    --build-arg=PACKAGE_WHL=ota_image_builder-0.0.21.dev0-py3-none-any.whl \
    --output type=image,name=${BASE_URI}:${VERSION},compression=zstd,compression-level=19,oci-mediatypes=true,force-compression=true,push=true \
    .
```
