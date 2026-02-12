#!/bin/sh
set -eux

BUILDER="$1"
SYS_IMG_ROOTFS="$2"
OTA_IMAGE_DIR="$3"
OTA_IMAGE_ARTIFACT_OUTPUT="$4"
CERT_DIR=${CERT_DIR}
DATA=${DATA:-./tests/data}

mkdir -p ${OTA_IMAGE_DIR}

echo "------------ init empty OTA image ------------"
${BUILDER} -d init \
    --annotations-file ${DATA}/full_annotations.yaml \
    ${OTA_IMAGE_DIR}

echo "------------ prepare the input system image ------------"
${BUILDER} -d prepare-sysimg \
    --rootfs-dir ${SYS_IMG_ROOTFS}

# NOTE: Order of calling cmds matters!
#   prepare-sysimg -> add-otaclient-package -> add-image

echo "------------ add otaclient package into OTA image ------------"
${BUILDER} -d add-otaclient-package \
    --release-dir "${SYS_IMG_ROOTFS}/opt/ota/client/otaclient_release"  "${SYS_IMG_ROOTFS}"

${BUILDER} -d add-otaclient-package-legacy-compat \
    --release-dir "${SYS_IMG_ROOTFS}/opt/ota/client/otaclient_release"  "${SYS_IMG_ROOTFS}"

echo "------------ add image payload(dev) into OTA image ------------"
${BUILDER} -d add-image \
    --annotations-file ${DATA}/full_annotations.yaml \
    --release-key dev \
    --sys-config "autoware:${DATA}/sys_config.yaml" \
    --sys-config "sub:${DATA}/sys_config.yaml" \
    --rootfs ${SYS_IMG_ROOTFS} \
    ${OTA_IMAGE_DIR}

echo "------------ add image payload(prd) into OTA image ------------"
${BUILDER} -d add-image \
    --annotations-file ${DATA}/full_annotations.yaml \
    --release-key prd \
    --sys-config "autoware:${DATA}/sys_config.yaml" \
    --sys-config "sub:${DATA}/sys_config.yaml" \
    --rootfs ${SYS_IMG_ROOTFS}/var \
    ${OTA_IMAGE_DIR}

echo "------------ finalize OTA image ------------"
${BUILDER} -d finalize ${OTA_IMAGE_DIR}

echo "------------ sign OTA image ------------"
${BUILDER} -d sign \
    --sign-cert ${CERT_DIR}/sign.pem \
    --sign-key ${CERT_DIR}/sign.key \
    --ca-cert ${CERT_DIR}/test.interm.pem \
    ${OTA_IMAGE_DIR}

echo "------------ pack OTA image artifact ------------"
${BUILDER} -d pack-artifact \
    -o ${OTA_IMAGE_ARTIFACT_OUTPUT} ${OTA_IMAGE_DIR}

echo "------------ OTA image build finished! ------------"
