# Local PCF OAM candidate (build-only)

This reconstructs PCF from official commit
`df6c0daacbc7fe5f610f19665e169f013eb05721`, with the reviewed
[OAM patch](../patches/pcf-oam-cors-handler.patch), SHA-256
`be2dcaa1e06acd222b12666e018341785e92e26bbf7c2b2e8e918fc78f7fd220`.
It does **not** establish the source identity of the opaque original runtime
binary. This is an experimental candidate, not a new upstream release.

## Reproduce

From WSL Ubuntu, with an existing Docker daemon and official source checkout:

```text
python3 backend/onprem/images/pcf-oam-image/build.py --source-repo .cache/free5gc-pcf-v4.2.2 --output .cache/pcf-oam-image-build-01
```

Output must not exist; failures and numbered stdout/stderr are preserved.
The script archives the exact commit, never copies the mutable worktree,
verifies the patch SHA, applies it to the archive, resolves the lint image
digest, and builds Linux/amd64 with digest-pinned Go 1.25.5. Build-only host
networking follows the AMF reference because WSL bridge module downloads have
failed; smoke testing has no network. No push, cluster import, or deployment
is performed. The local tag is `docker.io/cityverse/pcf:df6c0daa-oam-be2dcaa1`.

Only `/free5gc/pcf` is copied onto the pinned original runtime image.
The script rejects changes to inherited Docker Config except labels. It runs
read-only, capability-dropped, network-none `--help`, extracts the executable
from a never-started temporary container (then removes that container), and
records `go version -m`, binary SHA, build metadata, size and duration in the
exclusive output directory. `provenance.json` exposes `sourceCommit`,
`patchSha256`, `image`, `imageConfigId` and `binarySha256`.
An image configuration digest is not a registry distribution manifest digest.
No SBI/N5, native policy, or production-health success is claimed by CLI smoke.

## Validation and intentional deviations

The skill validator's Windows checkout fails under Bash because its script
contains CRLF (`pipefail\r`); it was not modified. The build script explicitly
falls back to Hadolint v2.12.0, resolves its digest before execution, and saves
JSON results. Checkov and CVE scans are not run and are not represented as
passing. Source OAM regressions and race evidence are described in the
[patch report](../patches/pcf-oam-cors-handler.md); they are separate from image
smoke. Actual image results belong in the generated provenance, not inferred
from this recipe.

| Deviation | Severity | Reason / risk / mitigation | Review |
|---|---|---|---|
| Inherited UID, health, ports and command retained | warning | Exact runtime compatibility required; no new hardening claim. Smoke drops all capabilities and uses a read-only root filesystem. Native behavior remains untested. | Before deployment |
| Original runtime rather than distroless | warning | Avoid unrelated runtime change; inherits original packages and their unscanned vulnerabilities. | Before deployment |
| No external Dockerfile syntax frontend | info | No advanced syntax needed; avoids another mutable frontend dependency. Docker build validates syntax. | On syntax change |
| Build-only host network | warning | Module download workaround; smoke remains isolated, no secret mounts. | Before CI adoption |
| Checkov/CVE scan skipped | warning | Tooling not configured in this bounded task; no security certification. | Before deployment |

Multi-stage compilation keeps compiler/module cache out of the runtime; module
downloads are cacheable separately from source. Actual original/final size and
build duration are recorded, without claiming a fabricated size reduction.
