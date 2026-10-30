# Matching UERANSIM device/RAN image

The Dockerfile builds `nr-gnb`, `nr-ue` and `nr-cli` from one source tree and
packages the native SCTP, IP/TUN inspection and ping tools required by the
observer. It uses the existing immutable Python runtime base in both stages.
Build context must be the audited UERANSIM checkout, not the CityVerse repository.

Prepared source: `.cache/ueransim-v327-provenance`, commit
`1d1e154f869260b5e98f6905827b1bd9b8663afc`. Verify a clean tracked checkout and
record the source file manifest/tree SHA before building. Pass the actual commit
and tree hash as `SOURCE_REVISION` and `SOURCE_TREE_SHA256` build arguments.
After a successful build, record image digest plus SHA-256 of all three binaries
extracted from the image; use that digest in `DeviceDriverProfile` and the
isolated RAN spec. An unbuilt source profile is not commissioning evidence.

The matching source image was built on 2026-10-28 using one CPU, nice 10 and
`--parallel 1`, then imported into the WSL containerd `k8s.io` namespace:

`docker.io/cityverse/ueransim-device@sha256:df3a8a33dfdf770b48b8780409b7f1264494e4093a89bf09fefa790949885b5b`

The source checkout's 4,267 tracked file hashes match the exact archived build
context byte-for-byte. Actual runtime ELF SHA-256 values:

- `nr-ue`: `a2d92664a930178d038dad0abb87ebc131dc59b5d4f9b04735de7d2e8dcca807`
- `nr-gnb`: `898e30964c69573b002b3baaaa0320ec25f4143a2072fb449ee2e4994b2fd695`
- `nr-cli`: `ce96733a8610a22d24a7a25b4e5f87e4247811f328fed89e8fb4a15c773dfa43`

An inspection container with networking disabled verified the `python` alias,
all binary hashes and dynamic libraries. Neither UE nor gNB main was executed.
The imported OCI manifest's configuration digest matches the built Docker image.
Full provenance, package versions, source manifest and build logs are under
`.tmp/ueransim-device-build/`. Native radio/PDU/traffic commissioning remains
pending its separate calibration window; image import does not establish readiness.
