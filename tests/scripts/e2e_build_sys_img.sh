#!/bin/sh

DIND_VER=28-dind
ROOTFS=dind_rootfs

# ------ export the dind image contents ------ #
mkdir ${ROOTFS}
docker create --name 28dind_export docker:${DIND_VER}
docker export | tar -xf - -C ${ROOTFS}

cp `which e2e_setup_sys_img.sh` ${ROOTFS}

# ------ use podman to setup the exported system image ------ #
CONTAINER_REF=setup_dind
podman run -d --name ${CONTAINER_REF} --privileged \
    --rootfs ${ROOTFS} /usr/local/bin/dockerd-entrypoint.sh

podman exec ${CONTAINER_REF} /e2e_setup_sys_img.sh
