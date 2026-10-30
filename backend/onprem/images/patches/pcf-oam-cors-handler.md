# PCF OAM request-time middleware growth repair

Scope: free5gc PCF source commit `df6c0daacbc7fe5f610f19665e169f013eb05721` (cached v4.2.2 source). This is a source patch and regression test, NOT a deployed image or native acceptance claim.

`internal/sbi/api_oam.go:setCorsHeader` previously called `router.Use(cors.New(...))` on every OAM request. Gin's global handler chain therefore grew without bound, mutating shared router state during requests; rebuilding fallback handlers eventually panicked with `too many handlers`.

The patch removes that request-time registration, its unused cors import and constant only. The four existing response-header assignments remain byte-for-byte unchanged. No global CORS middleware, additional allowed origin/method/header, authentication, policy, N5 or resource change is introduced. Existing wildcard-origin/credentials behavior is preserved, not endorsed or broadened. `go.mod` retains its pinned `go 1.25.5`; no dependency update or downgrade is included.

## Artifacts and application

- `pcf-oam-cors-handler.patch`: source-only unified patch.
- `pcf-oam-cors-regression_test.go`: copy into exact source checkout as `internal/sbi/pcf_oam_cors_regression_test.go` (package `sbi`). Preserve any existing destination first; do not overwrite unrelated work.
- SHA-256 of patch: `be2dcaa1e06acd222b12666e018341785e92e26bbf7c2b2e8e918fc78f7fd220`.

On an independently verified exact source checkout, first run `git apply --check /absolute/path/pcf-oam-cors-handler.patch`, then apply it. The patch does not contain the test file; install the adjacent test separately. No automatic build/deploy integration is added by this change.

Run baseline before applying the patch:

```text
go test ./internal/sbi -run TestOAMCorsSequential -count=1 -v
```

Then run patched checks:

```text
go test ./internal/sbi -run TestOAMCors -count=1 -v
go test -race ./internal/sbi -run TestOAMCors -count=1 -v
```

The regression uses real Gin routing, the production OAM route/handler and real in-memory processor. A narrow embedded app adapter supplies only `Processor()`; no network listener, external NF or database is started. It exercises 256 sequential requests and 256 requests across 16 goroutines, exact existing CORS headers, real unknown-SUPI 404 response, present-SUPI 200 response, missing-SUPI 400 response, and router-level 404. It is not an end-to-end OAuth/PCF deployment test.

## Actual local evidence, 2026-09-29

Evidence directory: `.cache/pcf-oam-cors-review-01/` contains exclusive original/patched/race `.log` and `.json`, a local runner, original `api_oam.go` backup, and original `go.mod` backup.

- Original 256 requests: **FAIL**, first bad request62, 195 failures; handler count **1→257**, `too many handlers=true`.
- Patched sequential256: **PASS**, zero failures; handler count **1→1**.
- Patched concurrent256/16 goroutines: **PASS**, handler count **1→1**.
- Patched statuses/headers: **PASS**.
- Same three tests under `-race`: **PASS**, no race report.
- Patch reverse-check against patched source: **PASS**; only intended source hunk changed semantically. Windows source checkout was clean before work; WSL's preexisting CRLF differences must not be treated as authored changes. `go.mod` remains byte-identical to its backup.

No PCF container build, cluster update, service restart, native UE action or CORS permission expansion was performed. A separately reviewed image/redeployment and real OAM readback remain necessary before claiming the live 500 is resolved.
