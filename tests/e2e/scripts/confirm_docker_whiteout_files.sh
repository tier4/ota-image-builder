#!/bin/bash
# See e2e_setup_sys_img.sh about how upperimage is generated.
set -eux

echo "------------ verify the built docker image ------------"
echo "Confirm the whiteout files are properly preserved and working"

ROOTFS=${1}

# start the dockerd in exported rootfs
podman run --rm -d --replace --name verify_dind --privileged \
    --rootfs "$(realpath ${ROOTFS})" /usr/local/bin/dockerd-entrypoint.sh

set +x
max_wait_seconds=30
elapsed=0
until podman exec verify_dind docker info >/dev/null 2>&1; do
    if [ "${elapsed}" -ge "${max_wait_seconds}" ]; then
        echo "dockerd did not become ready within ${max_wait_seconds} seconds" >&2
        exit 1
    fi
    sleep 1
    elapsed=$((elapsed + 1))
done
set -x

# verify the built upperimage
# all files under /lot_of_empty_files are removed at upperimage
podman exec verify_dind docker run --rm upperimage find /lot_of_empty_files -maxdepth 0 -empty | grep -q .
! podman exec verify_dind docker run --rm upperimage test -d /dir_with_subdir
! podman exec verify_dind docker run --rm upperimage test -d /dir_contents_changed/dir_to_be_removed
podman exec verify_dind docker run --rm upperimage test -d /file_become_dir
podman exec verify_dind docker run --rm upperimage test -f /dir_become_file

podman stop verify_dind