# NOTE: now evaluator builder is based on ubuntu:24.04
ARG UBUNTU_BASE=ubuntu:24.04
ARG BUILD_BASE=python:3.13-bookworm

FROM ${BUILD_BASE} AS app_build

ARG PYINSTALLER_VER=6.16.0

RUN mkdir /build
# minimum required files for building app
COPY ./src /build/src
COPY ./pyproject.toml ./uv.lock ./README.md /build/

COPY --from=ghcr.io/astral-sh/uv:0.9.4 /uv /uvx /bin/

WORKDIR /build
# NOTE: mount .git for hatch-vcs to determine the version
RUN --mount=type=bind,source=./.git,target=/build/.git,ro \
    set -eux; \
    apt-get update -qq; \
    apt-get install -y -qq --no-install-recommends \
        git python3 python3-setuptools; \
    uv run --with=pyinstaller==${PYINSTALLER_VER} --locked --no-managed-python --python=3.13 \
        pyinstaller -D -s --name ota-image-builder src/ota_image_builder/__main__.py; \
    ln -s ota-image-builder /build/dist/ota-image-builder/ota-image-tools

FROM ${UBUNTU_BASE}

ARG BUILDER_INSTALLATION="/ota-image-builder"

COPY --from=app_build /build/dist/ota-image-builder ${BUILDER_INSTALLATION}

ENTRYPOINT [ "/ota-image-builder/ota-image-builder" ]