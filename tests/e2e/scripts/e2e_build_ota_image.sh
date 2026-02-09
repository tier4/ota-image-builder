#!/bin/sh
set -eux

BUILDER="$1"
SYS_IMG_ROOTFS="$2"
OTA_IMAGE_DIR="$3"
OTA_IMAGE_ARTIFACT_OUTPUT="$4"
CERT_DIR=${CERT_DIR}
DATA=${DATA:-./tests/data}

# TODO: add otaclient release package

mkdir -p ${OTA_IMAGE_DIR}

echo -e "\n------------ init empty OTA image ------------"
${BUILDER} -d init \
    --annotations-file ${DATA}/full_annotations.yaml \
    ${OTA_IMAGE_DIR}

echo -e "\n------------ prepare the input system image ------------"
${BUILDER} -d prepare-sysimg \
    --rootfs-dir ${SYS_IMG_ROOTFS}

echo -e "\n------------ add image payload(dev) into OTA image ------------"
${BUILDER} -d add-image \
    --annotations-file ${DATA}/full_annotations.yaml \
    --release-key dev \
    --sys-config "autoware:${DATA}/sys_config.yaml" \
    --sys-config "sub:${DATA}/sys_config.yaml" \
    --rootfs ${SYS_IMG_ROOTFS} \
    ${OTA_IMAGE_DIR}

echo -e "\n------------ add image payload(prd) into OTA image ------------"
${BUILDER} -d add-image \
    --annotations-file ${DATA}/full_annotations.yaml \
    --release-key prd \
    --sys-config "autoware:${DATA}/sys_config.yaml" \
    --sys-config "sub:${DATA}/sys_config.yaml" \
    --rootfs ${SYS_IMG_ROOTFS}/var \
    ${OTA_IMAGE_DIR}

echo -e "\n------------ finalize OTA image ------------"
${BUILDER} -d finalize ${OTA_IMAGE_DIR}

echo -e "\n------------ sign OTA image ------------"
${BUILDER} -d sign \
    --sign-cert ${CERT_DIR}/sign.pem \
    --sign-key ${CERT_DIR}/sign.key \
    --ca-cert ${CERT_DIR}/test.interm.pem \
    ${OTA_IMAGE_DIR}
rm -rf ${CERT_DIR}/*.key

echo -e "\n------------ pack OTA image artifact ------------"
${BUILDER} -d pack-artifact \
    -o ${OTA_IMAGE_ARTIFACT_OUTPUT} ${OTA_IMAGE_DIR}

echo -e "\n------------ OTA image build finished! ------------"
