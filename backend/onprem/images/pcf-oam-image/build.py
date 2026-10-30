"""WSL/Linux local-only PCF OAM build. Never pushes, imports or deploys images.

python3 build.py --source-repo /path/to/pcf --output /path/to/new-artifact-directory
Output must not exist. Exact source archive and frozen patch are independently
verified; mutable repo HEAD/worktree is never used. Runtime compatibility, not
hardening, is intentional. Network-none --help is not an SBI/N5 health test.
"""
import argparse
import hashlib
import io
import json
import pathlib
import shutil
import subprocess
import tarfile
import time

COMMIT = "df6c0daacbc7fe5f610f19665e169f013eb05721"
PATCH_SHA = "be2dcaa1e06acd222b12666e018341785e92e26bbf7c2b2e8e918fc78f7fd220"
BUILDER = "golang:1.25.5@sha256:22bd1c41c958c762e9633f07a5fee8266300e5f25e18e269a6d87a4b870368aa"
RUNTIME = "free5gc/pcf:v4.2.2@sha256:b6208c82a02917cb6d620336d1245c00d875254e2c9bd28416c2e250a43453a4"
IMAGE = "docker.io/cityverse/pcf:df6c0daa-oam-be2dcaa1"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-repo", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    here = pathlib.Path(__file__).resolve().parent
    patch = here.parent / "patches/pcf-oam-cors-handler.patch"
    require(sha(patch.read_bytes()) == PATCH_SHA, "Frozen patch mismatch")
    counter = 0

    def run(argv, *, cwd=None, data=None, timeout=1800):
        nonlocal counter
        counter += 1
        print(f"stage {counter}: {' '.join(map(str, argv[:4]))}", flush=True)
        result = subprocess.run(list(map(str, argv)), cwd=cwd, input=data,
                                capture_output=True, timeout=timeout)
        (out / f"{counter:02d}.stdout").write_bytes(result.stdout)
        (out / f"{counter:02d}.stderr").write_bytes(result.stderr)
        if result.returncode:
            raise RuntimeError(f"Stage {counter} failed, exit={result.returncode}; see artifacts")
        return result.stdout

    require(run(["git", "-C", args.source_repo, "rev-parse", COMMIT]).decode().strip() == COMMIT,
            "Source commit mismatch")
    archive = run(["git", "-C", args.source_repo, "archive", "--format=tar", COMMIT])
    (out / "source.tar").write_bytes(archive)
    context = out / "context"
    source = context / "source"
    source.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        for member in bundle.getmembers():
            path = pathlib.PurePosixPath(member.name)
            require(not path.is_absolute() and ".." not in path.parts, "Unsafe archive path")
            require(member.isfile() or member.isdir(), "Unexpected archive special member")
        bundle.extractall(source)
    run(["git", "init", "-q"], cwd=source)
    run(["git", "apply", "--check", patch], cwd=source)
    run(["git", "apply", patch], cwd=source)
    target = source / "internal/sbi/api_oam.go"
    require("router.Use" not in target.read_text() and "CorsConfigMaxAge" not in target.read_text(), "OAM patch missing")
    for name in ("Dockerfile", "Dockerfile.dockerignore"):
        shutil.copyfile(here / name, context / name)
    # Explicit fallback: no host package installation; pinned-version lint image,
    # resolved digest saved before use. Checkov/CVE scan not represented as passed.
    lint = "hadolint/hadolint:v2.12.0"
    run(["docker", "pull", lint])
    lint_info = json.loads(run(["docker", "image", "inspect", lint]))[0]
    lint_pin = lint_info["RepoDigests"][0]
    run(["docker", "run", "--rm", "--network=none", "-i", lint_pin,
         "hadolint", "--format", "json", "-"], data=(context / "Dockerfile").read_bytes())
    run(["docker", "pull", "--platform=linux/amd64", BUILDER])
    run(["docker", "pull", "--platform=linux/amd64", RUNTIME])
    base = json.loads(run(["docker", "image", "inspect", RUNTIME]))[0]
    started = time.monotonic()
    # WSL bridge HTTPS module downloads reset; host networking is build-only.
    # It changes neither daemon configuration nor the network-none smoke gate.
    run(["docker", "buildx", "build", "--platform=linux/amd64", "--network=host", "--load",
         "--provenance=false", "--metadata-file", out / "build-metadata.json",
         "-t", IMAGE, "-f", context / "Dockerfile", context])
    elapsed = time.monotonic() - started
    built = json.loads(run(["docker", "image", "inspect", IMAGE]))[0]
    before, after = dict(base["Config"]), dict(built["Config"])
    before.pop("Labels", None)
    after.pop("Labels", None)
    require(before == after, "Inherited runtime configuration changed")
    help_text = run(["docker", "run", "--rm", "--network=none", "--read-only",
                     "--cap-drop=ALL", "--entrypoint", "/free5gc/pcf", IMAGE, "--help"], timeout=30)
    require(b"Policy Control" in help_text and b"--config" in help_text, "Unexpected help output")
    # A non-started temporary container is only used to copy the executable.
    container = run(["docker", "create", "--network=none", IMAGE]).decode().strip()
    try:
        run(["docker", "cp", f"{container}:/free5gc/pcf", out / "pcf"])
    finally:
        run(["docker", "rm", "-v", container])
    run(["docker", "run", "--rm", "--network=none", "--read-only", "-v",
         f"{out}:/evidence:ro", BUILDER, "go", "version", "-m", "/evidence/pcf"], timeout=30)
    metadata = json.loads((out / "build-metadata.json").read_text())
    report = dict(sourceCommit=COMMIT, sourceArchiveSha256=sha(archive),
                  patchSha256=PATCH_SHA, builder=BUILDER, rollbackImage=RUNTIME,
                  image=IMAGE, imageConfigId=built["Id"],
                  buildkitReportedDigest=metadata.get("containerimage.digest"),
                  manifestDescriptor=metadata.get("containerimage.descriptor"),
                  digestMeaning=("config-only; distribution manifest not established"
                                 if metadata.get("containerimage.digest") == built["Id"]
                                 else "see BuildKit descriptor; no registry publication"),
                  binarySha256=sha((out / "pcf").read_bytes()), sizeBytes=built["Size"],
                  originalSizeBytes=base["Size"], buildSeconds=elapsed,
                  inheritedRuntimeConfigEqual=True, networkNoneHelpPassed=True,
                  lintImage=lint_pin, lintIterations=1, checkov="skipped: unavailable",
                  vulnerabilityScan="not_run", deployed=False,
                  compatibilityDeviations=["Original runtime UID and health behavior retained; not hardened"],
                  limitations=["SBI/N5 and native UE regression not tested", "No distribution or registry push"])
    (out / "provenance.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
