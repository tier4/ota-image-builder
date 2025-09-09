ARG UBUNTU_BASE=ubuntu:24.04

FROM ${UBUNTU_BASE} AS image_build

ARG PACKAGE_WHL
ARG OTA_IMAGE_LIBS_WHEEL
COPY ./${PACKAGE_WHL} /tmp/dist

ARG UV_VERSION=0.7.21
ENV INSTALL_DIR="/app"

# Where the uv installation will be located
ENV PATH="${PATH}:/root/.local/bin"
RUN set -eux; \
    export PYTHONDONTWRITEBYTECODE=1; \
    export DEBIAN_FRONTEND=noninteractive; \
    apt-get update; \
    apt-get install --no-install-recommends -y \
        python3 \
        ca-certificates \
        curl; \
    curl -LsSf https://astral.sh/uv/${UV_VERSION}/install.sh | sh; \
    uv venv ${INSTALL_DIR}; \
    . ${INSTALL_DIR}/bin/activate; \
    uv pip install --no-cache ota-image-builder[pinned] -f /tmp/dist; \
    # cleanup
    apt-get clean; \
    \
    # NOTE: copy from docker official python3 dockerfile
	find ${INSTALL_DIR} -depth \
		\( \
			\( -type d -a \( -name test -o -name tests -o -name idle_test -o -name __pycache__ \) \) \
			-o \( -type f -a \( -name '*.pyc' -o -name '*.pyo' -o -name 'libpython*.a' \) \) \
		\) -exec rm -rf '{}' + \
	; \
	\
    rm -rf \
        /root/.cache \
        /tmp/* \
        /var/lib/apt/lists/* \
        /var/tmp/*

FROM ${UBUNTU_BASE}

ARG BUILDER_VERSION
ENV INSTALL_DIR="/app"

COPY --from=image_build ${INSTALL_DIR} ${INSTALL_DIR}

RUN set -eux; \
    export PYTHONDONTWRITEBYTECODE=1; \
    export DEBIAN_FRONTEND=noninteractive; \
    mkdir -p /mnt /workdir /rootfs /output_dir; \
    apt-get update -qq; \
    apt-get install --no-install-recommends -y \
        python3; \
    apt-get clean; \
    rm -rf \
        /root/.cache \
        /tmp/* \
        /var/lib/apt/lists/* \
        /var/tmp/*

LABEL ota_image_builder_version=${BUILDER_VERSION}

ENV PATH="${INSTALL_DIR}/bin:${PATH}"
ENTRYPOINT [ "ota-image-builder" ]