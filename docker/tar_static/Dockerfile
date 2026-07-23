FROM alpine:3.23 AS builder

ARG TAR_VERSION=1.35

RUN set -eux; \
    apk add --no-cache \
        build-base \
        musl-dev \
        wget \
        acl-dev \
        acl-static \
        attr-dev \
        attr-static; \
    mkdir /build; cd /build; \
    wget https://ftp.gnu.org/gnu/tar/tar-${TAR_VERSION}.tar.gz; \
    tar -xzf tar-${TAR_VERSION}.tar.gz; \
    cd tar-${TAR_VERSION}; \
    FORCE_UNSAFE_CONFIGURE=1 ./configure \
        --prefix=/usr/local \
        CFLAGS="-static -O2" \
        LDFLAGS="-static" \
        --disable-nls; \
    make -j$(nproc); \
    strip src/tar; \
    chmod +x src/tar; mv src/tar /build

FROM scratch

COPY --from=builder /build/tar /tar
ENTRYPOINT ["/tar"]
