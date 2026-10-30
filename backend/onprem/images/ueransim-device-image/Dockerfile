FROM python:3.11-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e AS build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential cmake libsctp-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /source
COPY . /source
RUN patch -p1 < /source/cityverse-rls-monotonic.patch && cmake -S /source -B /build -DCMAKE_BUILD_TYPE=Release && cmake --build /build --target nr-gnb nr-ue nr-cli --parallel 1

FROM python:3.11-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e
ARG SOURCE_REVISION
ARG SOURCE_TREE_SHA256
LABEL org.opencontainers.image.source="https://github.com/aligungr/UERANSIM" org.opencontainers.image.revision=$SOURCE_REVISION cityverse.source-tree-sha256=$SOURCE_TREE_SHA256
RUN apt-get update && apt-get install -y --no-install-recommends libsctp1 iproute2 iputils-ping && mkdir -p /etc/iproute2 && cp /usr/share/iproute2/rt_tables /etc/iproute2/rt_tables && rm -rf /var/lib/apt/lists/*
COPY --from=build /build/nr-gnb /build/nr-ue /build/nr-cli /ueransim/
WORKDIR /ueransim
