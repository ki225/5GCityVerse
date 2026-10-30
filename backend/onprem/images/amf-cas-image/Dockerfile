# Build only through build.py: context is an exact archived commit plus verified patch.
FROM golang:1.25.5@sha256:22bd1c41c958c762e9633f07a5fee8266300e5f25e18e269a6d87a4b870368aa AS builder
WORKDIR /src
ENV CGO_ENABLED=0 GOTOOLCHAIN=local
COPY source/go.mod source/go.sum ./
RUN go mod download && go mod verify
COPY source/ ./
RUN go build -trimpath -buildvcs=false -ldflags="-X github.com/free5gc/util/version.VERSION=v4.2.2-cityverse-cas -X github.com/free5gc/util/version.COMMIT_HASH=02675d94+cas34ccdeeb" -o /out/amf ./cmd

# Intentional compatibility deviation: inherit original USER, command, config,
# certificates and health behavior. This is NOT a non-root/hardening migration.
# Kubernetes SBI/N2 health and rollback are separate deployment gates.
FROM free5gc/amf:v4.2.2@sha256:8fc343349f7b0d55a257ecae328bff4e2d4b6800004ed932c128b2699a3860e7 AS runtime
LABEL org.opencontainers.image.source="https://github.com/free5gc/amf" \
      org.opencontainers.image.revision="02675d949f207a95379af458c7e80fabe802d728" \
      io.cityverse.amf.patch-sha256="34ccdeebf197727bd7246de084836a8cf149c088ae950e13f94598c718cc45e4" \
      io.cityverse.amf.scope="experimental-map-cas-only-not-upstream-release"
COPY --from=builder /out/amf /free5gc/amf
