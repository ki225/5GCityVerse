FROM networkstatic/iperf3@sha256:c1e4a239a83d1d60975bce1c9b7661af5517e362bf335f66a2c5b6adaeb4f19f AS base
COPY cityverse-traffic-manager /usr/local/bin/cityverse-traffic-manager
ENTRYPOINT ["/usr/local/bin/cityverse-traffic-manager"]

FROM base AS udp-single-listener
COPY iperf3 /usr/local/bin/iperf3

# Keep the stock image as the default build target.
FROM base AS standard
