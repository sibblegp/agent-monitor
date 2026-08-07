# Build environment for the Agent Monitor snap.
#
# Snapcraft normally builds inside a clean LXD or Multipass VM whose OS matches
# the snap's base. On a host with neither — an Arch workstation, a CI runner —
# this image *is* that clean environment, and snapcraft runs in destructive
# mode inside it.
#
# Everything is deliberately pinned to the same generation: Ubuntu 24.04 as the
# host, core24 as the snap's base, and the core24-based snapcraft. Mixing them
# does not work — snapcraft ships its own Python built against its base's glibc,
# so the core24 snapcraft simply will not exec on a 22.04 host, and building a
# core22 snap on 24.04 would link the parts against a glibc the base does not
# have.
#
# Snapcraft is distributed only as a snap, and running snapd inside Docker is a
# swamp, so the snap is unsquashed by hand. That is what Canonical's own CI
# images do.

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# snapd is here for its `snap` binary alone — snapcraft shells out to
# `snap pack --check-skeleton` before packing, which works fine without the
# daemon running (and it cannot run in a container anyway).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl jq squashfs-tools snapd \
        git python3 python3-dev python3-venv python3-pip \
        build-essential rsync \
    && rm -rf /var/lib/apt/lists/*

# Ubuntu 24.04 ships Node 18; electron-builder 25 wants newer.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x -o /tmp/nodesource.sh \
    && bash /tmp/nodesource.sh \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* /tmp/nodesource.sh

ARG SNAPCRAFT_CHANNEL=stable
RUN set -eux; \
    fetch() { \
      curl -fsSL -H 'X-Ubuntu-Series: 16' -H 'X-Ubuntu-Architecture: amd64' \
        "https://api.snapcraft.io/api/v1/snaps/details/$1?channel=$2" \
        | jq -r '.download_url'; \
    }; \
    mkdir -p /snap/core24 /snap/snapcraft; \
    curl -fsSL "$(fetch core24 stable)" -o /tmp/core24.snap; \
    unsquashfs -q -d /snap/core24/current /tmp/core24.snap; \
    curl -fsSL "$(fetch snapcraft "${SNAPCRAFT_CHANNEL}")" -o /tmp/snapcraft.snap; \
    unsquashfs -q -d /snap/snapcraft/current /tmp/snapcraft.snap; \
    rm -f /tmp/*.snap; \
    printf '#!/bin/sh\nexec /snap/snapcraft/current/bin/python /snap/snapcraft/current/bin/snapcraft "$@"\n' \
      > /usr/local/bin/snapcraft; \
    chmod +x /usr/local/bin/snapcraft

# What the snapcraft snap's own snap.yaml sets for itself. craftctl — which the
# parts use to set the version — lives in libexec.
ENV SNAP=/snap/snapcraft/current \
    SNAP_NAME=snapcraft \
    SNAP_ARCH=amd64 \
    PATH=/snap/snapcraft/current/libexec/snapcraft:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

WORKDIR /build
