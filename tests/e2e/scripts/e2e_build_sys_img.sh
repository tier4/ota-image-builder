#!/bin/sh
# setup a test system image
set -eux

DIND_VER=28-dind
ROOTFS=${1}
SETUP_SCRIPT=$(dirname "$0")/e2e_setup_sys_img.sh

# ------ export the dind image contents ------ #
mkdir ${ROOTFS}
docker create --name 28dind_export docker:${DIND_VER}
docker export 28dind_export | tar -xf - -C ${ROOTFS}
docker rm 28dind_export

cp ${SETUP_SCRIPT} ${ROOTFS}

# ------ use podman to setup the exported system image ------ #
podman run -d --replace --name setup_dind --privileged \
    --rootfs "$(realpath ${ROOTFS})" /usr/local/bin/dockerd-entrypoint.sh
# Wait for dockerd inside the container to fully start up
max_wait_seconds=30
elapsed=0
until podman exec setup_dind docker info >/dev/null 2>&1; do
    if [ "${elapsed}" -ge "${max_wait_seconds}" ]; then
        echo "dockerd did not become ready within ${max_wait_seconds} seconds" >&2
        exit 1
    fi
    sleep 1
    elapsed=$((elapsed + 1))
done

podman exec setup_dind /e2e_setup_sys_img.sh
podman stop setup_dind
podman rm setup_dind
