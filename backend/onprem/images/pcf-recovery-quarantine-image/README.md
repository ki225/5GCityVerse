# PCF recovery quarantine candidate (offline)

`source.patch` applies to free5GC PCF commit `df6c0daacbc7fe5f610f19665e169f013eb05721`. The patch includes the earlier boot receipt and the opt-in recovery quarantine. `source.lock.json` binds the source patch, compiler version, binary, eight pinned runtime base layers, and reviewed OCI digests. The original boot-receipt image and default on-prem renderer are unchanged.

With `CITYVERSE_PCF_RECOVERY_QUARANTINE=1`, the process captures its pre-listener boot receipt, closes a shared mutation gate on the UE, application session, BDT policy, and AMF status subscription pools, and starts an SBI listener that serves only local `GET /npcf-oam/v1/boot-receipt` and `GET /npcf-oam/v1/recovery-current`. All other SBI requests return 503 before policy/callback handlers. The process skips NRF registration and, on shutdown, skips NRF deregistration. An invalid opt-in value fails startup. Without the opt-in environment variable, the existing code path remains active.

The current receipt records all four pool counts and per-pool attempted/denied Store/Delete counts under the same gate. It binds `podUid`, `bootEpoch`, `gateEpoch`, binary SHA, and `observedAt`. Its scope is **this PCF process only**. Earlier PCF state, prior NRF profiles, AMF subscriptions outside this process, AMF/SMF caches, and downstream policy state remain `unknown`; a current-zero receipt is not proof of 5GC-wide zero state.

## Rebuild and verify

`replay.py` is the Docker Buildx path. It replays the exact source patch and runs Go race tests before building an OCI. Docker/WSL was unavailable during final replay; its digest equality is unverified.

`repack.py` is the verified fallback. On Windows with Go 1.25.5, run it with a clean source mirror, the pinned v3 boot-receipt OCI, an unused output directory, and D: based Go caches. It clones the pinned commit, applies the patch, preserves checked line endings, cross compiles `linux/amd64`, verifies the binary hash, checks all eight base layer digests, replaces only the PCF binary layer, and reads back every blob and the binary bit for bit. Its OCI digest is a separate reviewed candidate; it is **not** asserted to equal Docker Buildx output.

Example (PowerShell, from repo root):

```powershell
python backend/onprem/images/pcf-recovery-quarantine-image/repack.py `
  --source-repo .cache/pcf-boot-attest-20261005 `
  --base-oci .cache/pcf-boot-attest-20261005-artifact-v3/pcf-boot-receipt.oci.tar `
  --output .tmp/pcf-recovery-quarantine-20261005/repack-new `
  --go-mod-cache .cache/go-mod --go-build-cache .cache/go-build
```

`repack-v1` and `repack-v2` independently produced archive SHA256 `5fb64251a09e8565747a3ed7c8a559bf7343aa7e3629141a1e82f57bd1d6fffc`; manifest digest `sha256:88a07a99ee88e5ed5f7830f4e4e605787acc67a8ca08f6d695e04153828163c2`. The old pre-NRF-skip OCI is stale and must not be used.

The renderer has a separate `pcfRecoveryQuarantine` selection requiring `enabled: true` and an explicit immutable `docker.io/cityverse/pcf@sha256:...` registry digest. It rejects simultaneous `pcfBootReceipt`. Its candidate template keeps the fixed `PCF_NF_INSTANCE_ID`, adds the Pod UID and quarantine opt-in environment variables, uses a Recreate singleton Deployment, and probes both loopback OAM routes. On a saved 97-resource fixture, default composition is byte-equal to the pre-change renderer; the opt-in changes only the PCF Deployment. The OCI manifest digest above is a local artifact digest; any registry distribution digest must be verified separately before selection.

The candidate remains offline. A separate reviewer must inspect the source, OCI, renderer, observer, authorizer, and current-zero reconciliation before any rollout.
