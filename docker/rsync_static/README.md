# rsync static binary builder

Multi-stage Docker build that compiles a fully **statically linked** `rsync` binary from source using Alpine / musl.
This is for mounted into the abitrary docker container and exports its rootfs without depending on the container's own OS.

## Build

```bash
# Default (rsync 3.4.1)
docker build --no-cache -t rsync-static:3.4.1 docker/rsync_static/

# Custom rsync version
RSYNC_VERSION=3.3.0
docker build --no-cache --build-arg=RSYNC_VERSION=${RSYNC_VERSION} -t rsync-static:${RSYNC_VERSION} docker/rsync_static/
```

## Usage

With the image built, extract the static binary from the built image:

```bash
# extract the rsync binary from the built image
docker create --name rsync-tmp rsync-static:3.4.1
docker cp rsync-tmp:/rsync ./rsync
docker rm rsync-tmp
```

To use the statically linked rsync to dump a container's rootfs:

```bash
# use export folder on the host
mkdir export
# assume the statically linked rsync is placed under current workdir

# will export the rootfs from container to the export folder
# here we dump the ubuntu:24.04 image's rootfs
sudo docker run --rm -v "$(pwd)/export":/export -v "$(pwd)/rsync":/rsync:ro \
    --entrypoint /rsync ubuntu:24.04 \
        -axAXH '--exclude=/sys/***' '--exclude=/proc/***' '--exclude=/tmp/***' \
        '--exclude=/dev/***' '--exclude=/export/***' --exclude=/rsync --exclude=/.dockerenv \
        / /export

# remember to add the important mountpoints place holders back
sudo mkdir -p export/sys export/proc export/tmp export/dev
```