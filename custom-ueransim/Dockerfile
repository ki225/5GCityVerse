# syntax=docker/dockerfile:1.7

FROM gcc:14.3.0@sha256:4e2d22617a3b4af1ad0150ba6ca3cb8a975c44c1abac6f9a09870b93cb5c363b AS builder

ARG UERANSIM_COMMIT=ca1a66fffe282767bb08618af9f848e3b68ea47b

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates=20250419 \
        cmake=3.31.6-2 \
        git=1:2.47.3-0+deb13u1 \
        libsctp-dev=1.0.21+dfsg-1 \
        lksctp-tools=1.0.21+dfsg-1 \
        make=4.4.1-2 \
    && rm -rf /var/lib/apt/lists/* \
    && git clone --filter=blob:none https://github.com/aligungr/UERANSIM.git /src/UERANSIM \
    && git -C /src/UERANSIM checkout --detach "${UERANSIM_COMMIT}" \
    && test "$(git -C /src/UERANSIM rev-parse HEAD)" = "${UERANSIM_COMMIT}" \
    && make -C /src/UERANSIM -j"$(nproc)"

FROM bitnami/minideb:trixie@sha256:602e010b5a71f52ff581de84a772bee7bdedb78a79b2c3a576e40c0fef42e5f2 AS runtime

LABEL org.opencontainers.image.source="https://github.com/aligungr/UERANSIM" \
      org.opencontainers.image.revision="ca1a66fffe282767bb08618af9f848e3b68ea47b" \
      org.opencontainers.image.version="v3.2.8"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        iproute2=6.15.0-1 \
        iputils-ping=3:20240905-3 \
        libsctp1=1.0.21+dfsg-1 \
        lksctp-tools=1.0.21+dfsg-1 \
        procps=2:4.0.4-9 \
        psmisc=23.7-2 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 ueransim \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /ueransim ueransim \
    && mkdir -p /ueransim/config /ueransim/binder /etc/iproute2 \
    && touch /etc/iproute2/rt_tables \
    && chown -R 10001:10001 /ueransim

WORKDIR /ueransim

COPY --from=builder --chown=10001:10001 /src/UERANSIM/build/nr-gnb ./nr-gnb
COPY --from=builder --chown=10001:10001 /src/UERANSIM/build/nr-ue ./nr-ue
COPY --from=builder --chown=10001:10001 /src/UERANSIM/build/nr-cli ./nr-cli
COPY --from=builder --chown=10001:10001 /src/UERANSIM/build/nr-binder ./binder/nr-binder
COPY --from=builder --chown=10001:10001 /src/UERANSIM/build/libdevbnd.so ./binder/libdevbnd.so

USER 10001:10001

VOLUME ["/ueransim/config"]
