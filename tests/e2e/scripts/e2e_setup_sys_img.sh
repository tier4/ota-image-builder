#!/bin/sh
# run on alpine based dind image
set -eux

# check if docker is running properly
docker info

# ------ install deps ------ #
apk update
apk add attr curl ca-certificates

# ------ download otaclient release packages ------ #
OTACLIENT_RELEASE_DIR=/opt/ota/client/otaclient_release
BASE_URL=https://github.com/tier4/ota-client/releases/download/v3.13.1/
mkdir -p "${OTACLIENT_RELEASE_DIR}"
curl -LO --output-dir "${OTACLIENT_RELEASE_DIR}" "${BASE_URL}/manifest.json"
curl -LO --output-dir "${OTACLIENT_RELEASE_DIR}" "${BASE_URL}/otaclient-arm64-v3.13.1.squashfs"
curl -LO --output-dir "${OTACLIENT_RELEASE_DIR}" "${BASE_URL}/otaclient-x86_64-v3.13.1.squashfs"

# ------ files with same contents ------ #
SAME_FILE=/same_file
dd if=/dev/urandom of=${SAME_FILE} bs=1k count=2
cp ${SAME_FILE} "${SAME_FILE}_1" "${SAME_FILE}_2"

# ------ empty files ------ #
# for otaclient PR#492, add a folder that contains lots of empty files
EMPTY_FILE_COUNT=5000
EMPTY_FILE_FOLDER=/empty_files

set +x
mkdir ${EMPTY_FILE_FOLDER}
for i in $(seq 1 ${EMPTY_FILE_COUNT}); do
    touch "${EMPTY_FILE_FOLDER}/file_$i.txt"
done
set -x

# ------ small files ------ #
SMALL_FILES_COUNT=10000
SMALL_FILES_FOLDER=/small_files

set +x
mkdir ${SMALL_FILES_FOLDER}
for i in $(seq 1 ${SMALL_FILES_COUNT}); do
    dd if=/dev/urandom of="${SMALL_FILES_FOLDER}/file_$i.txt" bs=4k count=1 > /dev/null 2>&1
done
set -x

chown -R 1000:42 /small_files

# ------ xattrs support ------ #
touch /file_with_xattrs
setfattr -n user.ota.test -v "test_value" /file_with_xattrs

# ------ large file support ------ #
dd if=/dev/urandom of=/500M.img bs=1M count=500

# ------ utf-8 support ------ #
SPECIAL_FILE="path;adf.ae?qu.er\y=str#fragファイルement"
echo "${SPECIAL_FILE}" > "/${SPECIAL_FILE}"

# ------ docker image pull support ------ #
BUSYBOX_VER=1.37.0
docker image pull busybox:${BUSYBOX_VER}

# ------ docker image build support with whiteout files ------ #
# setup the base image
BASE_IMAGE_REF=baseimage
BASE_DOCKERFILE_CONTENTS=$(cat <<EOF
FROM busybox:${BUSYBOX_VER}

RUN mkdir /lot_of_empty_files; \
    for i in \$(seq 1 5000); do echo "\$i" > "/lot_of_empty_files/file_\$i.txt"; done; \
    mkdir -p /dir_with_subdir/subdir; \
    echo "subdir_file" > /dir_with_subdir/subdir/subdir_files; \
    mkdir -p /dir_contents_changed/dir_to_be_removed; \
    mkdir -p /dir_become_file; \
    echo "used_to_be_a_file" > /file_become_dir

EOF
)
# remove/modify files from base image
UPPER_IMAGE_REF=upperimage
UPPER_DOCKERFILE_CONTENTS=$(cat <<EOF
FROM ${BASE_IMAGE_REF}

RUN rm -rf /lot_of_empty_files/*; \
    rm -rf /dir_with_subdir; \
    rm -rf /dir_contents_changed/dir_to_be_removed; \
    echo "dir_contents_changed" > /dir_contents_changed/new_content; \
    rm -rf /dir_become_file; echo "i_am_a_file_now" > /dir_become_file; \
    rm -rf /file_become_dir; mkdir /file_become_dir

EOF
)

# build base image
echo "${BASE_DOCKERFILE_CONTENTS}" | docker build -t ${BASE_IMAGE_REF} -
# build upper image
echo "${UPPER_DOCKERFILE_CONTENTS}" | docker build -t ${UPPER_IMAGE_REF} -

# inspect the built images
docker image list
