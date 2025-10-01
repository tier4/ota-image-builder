ARG SYS_IMG=ghcr.io/tier4/ota-client/sys_img_for_test:ubuntu_22.04
ARG UBUNTU_BASE=ubuntu:22.04
ARG UV_VERSION=0.8.22

FROM ${SYS_IMG} AS sys_img

#
# ------ stage 1: build the ota-image-builder ------ #
#

FROM ${UBUNTU_BASE} AS app_builder

# copy minimum required files for build
COPY ./src ./pyproject.toml ./.python-version ./uv.lock /project/

WORKDIR /project

RUN set -eux; \
    apt-get update; \
    apt-get install -y -qq --no-install-recommends \
        git ca-certificates wget python3 python3-setuptools; \
    export UV_INSTALL_DIR=/usr/bin; \
    wget -qO- "https://astral.sh/uv/${UV_VERSION}/install.sh" | sh; \
    uv run --locked pyinstaller -s -F --optimize=2 \
          --name ota-image-builder \
          src/ota_image_builder/__main__.py

#
# ------ stage 2: setup test environment ------ #
#

FROM ${UBUNTU_BASE}

COPY --chmod=755 --from=app_builder /project/dist/ota-image-builder /usr/bin/
COPY --from=sys_img / /rootfs

ENV SYS_IMG_ROOTFS=/rootfs
ENV CERT_DIR=/certs
RUN set -eux; \
    ln -s /usr/bin/ota-image-builder /usr/bin/ota-image-tools; \
    apt-get update; \
    apt-get install -y -qq --no-install-recommends \
        openssl; \
    apt-get clean; \
    rm -rf; \
        /tmp/* \
        /var/lib/apt/lists/* \
        /var/tmp/*

ENTRYPOINT [ "/usr/bin/ota-image-builder" ]
