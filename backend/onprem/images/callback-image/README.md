# Internal AF callback dependency image

The callback listens directly with Hypercorn, accepting HTTP/1 health/admin
requests and official PCF HTTP/2 prior-knowledge callbacks on port 9082. There
is no forwarding proxy: the application validates the real TCP peer against
the trusted PCF Pod IP. Source files remain in the audited ConfigMap; secrets
and the SQLite journal are never copied into this image.

Build from this directory:

```text
docker build --platform linux/amd64 -t cityverse/af-callback-deps:hypercorn-0.18.0 .
```

Import the image into the dedicated core node's `k8s.io` containerd namespace,
verify its OCI manifest digest, and update `policy_callback.CALLBACK_IMAGE` only
after h2c tests pass. The current renderer pin is the tested local manifest;
it is not a claim that this image is published to Docker Hub. Preloading is
required. Fresh builds may produce a different manifest digest; never substitute
a tag-only deployment or assume a newly built digest matches the recorded pin.

The Python 3.11 base is digest-pinned. All seven Python dependencies are exact
versions with wheel SHA256 hashes, installed with `--require-hashes`. The
multi-stage image excludes the wheel cache, uses UID/GID 1337, and has an exec
health check. A build-context allowlist excludes all application data/secrets.

Validation on 2026-09-28:

| Iteration | Check | Result |
| --- | --- | --- |
| 1 | Skill validator | CRLF execution failure; normalized stream retry timed out installing Checkov |
| 2 | Pinned hadolint 2.14.0 container | Exit 0, no findings |
| 2 | Actual Docker build and hash-checked pip install | Passed |
| 2 | Rendered resources, strict 1.34 schema and server dry-run | 5/5 passed each |

Scanner coverage is **partial**: Checkov and image-CVE scanning did not finish
and no vulnerability-free claim is made. Manual checks found no secret, root
runtime, tag-only base, shell-form entrypoint, or missing health check. Final
image size was 127,018,420 bytes; dependency layer caching is enabled. There is
no before/after size-reduction claim. Complete vulnerability scanning before
production promotion; this artifact is scoped to the dedicated test lab.

When rolling out, first stop the owned helper after physical UE cleanup, then
update only callback ConfigMap/Deployment. Preserve Secret/PV/PVC identities and
journal records, verify both source checksums and live health, then restart the
owned helper. A successful h2c health check does not prove an official callback
or resolve an unknown policy ownership record.
