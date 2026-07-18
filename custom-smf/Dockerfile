FROM golang@sha256:ea341baa9bd5ba6784f6d7161ace70544349a6242d54d34a0fbfd2c4d51c9d58 AS builder

WORKDIR /src
COPY upstream/smf-2232d2992f204207e58830ac8ee55bf0cd832f92.tar /tmp/smf.tar
RUN echo "82fbdd1ca722aaae4f955d855e78d027e8f93d162cc96f900649476a583ad6b0  /tmp/smf.tar" | sha256sum --check \
    && tar -xf /tmp/smf.tar -C /src \
    && find /src -type f -name '*.go' -exec sed -i 's/\r$//' {} + \
    && rm /tmp/smf.tar

COPY patches/qer-actuator.patch /tmp/qer-actuator.patch
RUN sed -i 's/\r$//' /tmp/qer-actuator.patch \
    && git apply --check /tmp/qer-actuator.patch \
    && git apply /tmp/qer-actuator.patch \
    && go get golang.org/x/crypto@v0.52.0 golang.org/x/net@v0.55.0 \
    # Several upstream packages bind the fixed PFCP UDP port 8805 during tests.
    # Run packages serially so Docker's shared build network namespace cannot
    # make otherwise independent package tests race for the same port.
    && go test -p 1 ./internal/pfcp/message ./internal/sbi/... -count=1 \
    && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/smf ./cmd/main.go

FROM alpine@sha256:25109184c71bdad752c8312a8623239686a9a2071e8825f20acb8f2198c3f659
RUN apk upgrade --no-cache \
    && apk add --no-cache ca-certificates tini \
    && mkdir -p /free5gc/cert /free5gc/config \
    && chown -R 1000:1000 /free5gc
COPY --from=builder /out/smf /free5gc/smf
WORKDIR /free5gc
ENV F5GC_MODULE=smf
VOLUME ["/free5gc/cert", "/free5gc/config"]
CMD ["/bin/sh"]
LABEL org.opencontainers.image.source="https://github.com/free5gc/smf" \
      org.opencontainers.image.revision="2232d2992f204207e58830ac8ee55bf0cd832f92" \
      org.opencontainers.image.title="5GCityVerse free5GC SMF PFCP QER actuator"
