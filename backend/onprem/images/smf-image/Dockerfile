FROM golang@sha256:ea341baa9bd5ba6784f6d7161ace70544349a6242d54d34a0fbfd2c4d51c9d58 AS builder

WORKDIR /src
COPY upstream/smf-2232d2992f204207e58830ac8ee55bf0cd832f92.tar /tmp/smf.tar
RUN echo "82fbdd1ca722aaae4f955d855e78d027e8f93d162cc96f900649476a583ad6b0  /tmp/smf.tar" | sha256sum --check \
    && tar -xf /tmp/smf.tar -C /src \
    && find /src -type f -name '*.go' -exec sed -i 's/\r$//' {} + \
    && rm /tmp/smf.tar

COPY patches/onprem-deployed-baseline.patch /tmp/qer-actuator.patch
COPY patches/session-qer-actuator.patch /tmp/session-qer-actuator.patch
RUN sed -i 's/\r$//' /tmp/qer-actuator.patch \
    && git apply --check /tmp/qer-actuator.patch \
    && git apply /tmp/qer-actuator.patch \
    && git apply --check /tmp/session-qer-actuator.patch \
    && git apply /tmp/session-qer-actuator.patch \
    && go get golang.org/x/crypto@v0.52.0 golang.org/x/net@v0.55.0 \
    # Several upstream packages bind the fixed PFCP UDP port 8805 during tests.
    # Run packages serially so Docker's shared build network namespace cannot
    # make otherwise independent package tests race for the same port.
    && go test -mod=mod -p 1 ./internal/context ./internal/pfcp/message ./internal/sbi/... -count=1 \
    && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/smf ./cmd/main.go

FROM gcr.io/distroless/static-debian12:nonroot@sha256:afa5c872c891853ca7fcf1f12c3edb23f7eeef36189728842dd51042ff57f7ab
COPY --from=builder /out/smf /free5gc/smf
WORKDIR /free5gc
ENV F5GC_MODULE=smf
VOLUME ["/free5gc/cert", "/free5gc/config"]
ENTRYPOINT ["/free5gc/smf"]
LABEL org.opencontainers.image.source="https://github.com/ki225/smf" \
      org.opencontainers.image.revision="f91c2cd4b2c78139050797b451f5e3dfc3b9047f" \
      org.opencontainers.image.title="5GCityVerse free5GC SMF PFCP QER actuator"
