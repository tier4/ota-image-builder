#!/bin/sh
set -eux

DIND_VER=28-dind
ROOTFS=dind_rootfs
SETUP_SCRIPT=$(dirname "$0")/e2e_setup_sys_img.sh

# ------ export the dind image contents ------ #
mkdir ${ROOTFS}
docker create --name 28dind_export docker:${DIND_VER}
docker export 28dind_export | tar -xf - -C ${ROOTFS}
docker rm 28dind_export

cp ${SETUP_SCRIPT} ${ROOTFS}

# ------ use podman to setup the exported system image ------ #
podman run -d --replace --name setup_dind --privileged \
    --rootfs `realpath ${ROOTFS}` /usr/local/bin/dockerd-entrypoint.sh
# NOTE: wait for the dockerd fully starts up
sleep 16

podman exec setup_dind /e2e_setup_sys_img.sh
podman stop setup_dind
podman rm setup_dind