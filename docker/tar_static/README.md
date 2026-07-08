# tar static binary builder

Multi-stage Docker build that compiles a fully **statically linked** GNU `tar` binary from source using Alpine / musl.
This is for mounting into an arbitrary docker container and archiving its rootfs without depending on the container's own OS.

## Build

```bash
# Default (tar 1.35)
docker build --no-cache -t tar-static:1.35 docker/tar_static/

# Custom tar version
TAR_VERSION=1.34
docker build --no-cache --build-arg=TAR_VERSION=${TAR_VERSION} -t tar-static:${TAR_VERSION} docker/tar_static/
```

Push to github ecr:
```bash
docker push ghcr.io/tier4/ota-image-builder/tar-static:1.35
```

## Usage

To use the statically linked tar to archive a container's rootfs (preserving xattrs and ACLs):

```bash
# assume the statically linked tar is placed under current workdir

# will archive the rootfs from the container to ./rootfs.tar on the host
# here we dump the ubuntu:24.04 image's rootfs
sudo docker run --rm -v "$(pwd)":/export -v "$(pwd)/tar":/tar:ro \
    --entrypoint /tar ubuntu:24.04 \
        --xattrs --acls --numeric-owner \
        '--exclude=/sys/*' '--exclude=/proc/*' '--exclude=/tmp/*' '--exclude=/dev/*' \
        '--exclude=/export' '--exclude=/tar' '--exclude=/.dockerenv' \
        -cf /export/rootfs.tar -C / .
```
