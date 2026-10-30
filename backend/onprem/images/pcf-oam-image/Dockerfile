# Exact archived source plus verified patch; build through build.py only.
FROM golang:1.25.5@sha256:22bd1c41c958c762e9633f07a5fee8266300e5f25e18e269a6d87a4b870368aa AS builder
WORKDIR /src
ENV CGO_ENABLED=0 GOTOOLCHAIN=local
COPY source/go.mod source/go.sum ./
RUN go mod download && go mod verify
COPY source/ ./
RUN go build -trimpath -buildvcs=false -ldflags="-X github.com/free5gc/util/version.VERSION=v4.2.2-cityverse-oam -X github.com/free5gc/util/version.COMMIT_HASH=df6c0daa+oambe2dcaa1" -o /out/pcf ./cmd

# Compatibility exception: preserve all inherited runtime Config except labels.
# The opaque original binary's source identity is not asserted.
FROM free5gc/pcf:v4.2.2@sha256:b6208c82a02917cb6d620336d1245c00d875254e2c9bd28416c2e250a43453a4 AS runtime
LABEL org.opencontainers.image.source="https://github.com/free5gc/pcf" \
      org.opencontainers.image.revision="df6c0daacbc7fe5f610f19665e169f013eb05721" \
      io.cityverse.pcf.patch-sha256="be2dcaa1e06acd222b12666e018341785e92e26bbf7c2b2e8e918fc78f7fd220" \
      io.cityverse.pcf.scope="experimental-oam-handler-fix-not-upstream-release"
COPY --from=builder /out/pcf /free5gc/pcf
