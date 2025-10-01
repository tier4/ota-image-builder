#!/bin/bash

set -eux

OTA_IMAGE_DIR=/ota-image

# gen cert chain
pushd ${CERTS_DIR}
bash ${CERTS_DIR}/gen_certs.sh
popd

# TODO: add otaclient release package

mkdir -p ${OTA_IMAGE_DIR}
ota-image-builder -d init \
    --annotations-file full_annotations.yaml \
    ${OTA_IMAGE_DIR}
ota-image-builder -d add-image \
    --annotations-file full_annotations.yaml \
    --release-key dev \
    --sys-config "autoware:sys_config.yaml" \
    --sys-config "sub:sys_config.yaml" \
    --rootfs ${SYS_IMG_ROOTFS} \
    ${OTA_IMAGE_DIR}
ota-image-builder -d add-image \
    --annotations-file full_annotations.yaml \
    --release-key prd \
    --sys-config "autoware:sys_config.yaml" \
    --sys-config "sub:sys_config.yaml" \
    --rootfs ${SYS_IMG_ROOTFS}/var \
    ${OTA_IMAGE_DIR}
ota-image-builder -d finalize ${OTA_IMAGE_DIR}
ota-image-builder -d sign \
    --sign-cert ${CERTS_DIR}/sign.pem \
    --sign-key ${CERTS_DIR}/sign.key \
    --ca-cert ${CERTS_DIR}/test.interm.pem \
    ${OTA_IMAGE_DIR}
rm -rf ${CERTS_DIR}/*.key

ota-image-tools inspect-index ${OTA_IMAGE_DIR}
