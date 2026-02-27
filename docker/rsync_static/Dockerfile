FROM alpine:3.23 AS builder

ARG RSYNC_VERSION=3.4.1

RUN set -eux; \
    apk add --no-cache \
        build-base \
        musl-dev \
        autoconf \
        automake \
        wget \
        popt-dev \
        popt-static \
        acl-dev \
        acl-static \
        attr-dev \
        attr-static; \
    mkdir /build; cd /build; \
    wget https://download.samba.org/pub/rsync/src/rsync-${RSYNC_VERSION}.tar.gz; \
    tar -xzf rsync-${RSYNC_VERSION}.tar.gz; \
    cd rsync-${RSYNC_VERSION}; \
    ./configure \
        --prefix=/usr/local \
        CFLAGS="-static -O2" \
        LDFLAGS="-static" \
        --disable-shared \
        --without-included-popt \
        --disable-zlib \
        --disable-lz4 \
        --disable-zstd \
        --disable-xxhash \
        --disable-openssl \
        --disable-md2man \
        --enable-acl-support \
        --enable-xattr-support; \
    make -j$(nproc); \
    strip rsync;\
    chmod +x rsync; mv rsync /build

FROM scratch

COPY --from=builder /build/rsync /rsync
ENTRYPOINT ["/rsync"]