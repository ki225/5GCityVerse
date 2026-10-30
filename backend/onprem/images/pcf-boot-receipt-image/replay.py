"""Replay the pinned PCF boot-receipt patch and package an offline OCI image.

Run with Python 3 on Linux/WSL with Git and a Docker Buildx docker-container
builder. This never loads an image into Kubernetes or changes live workloads.
"""

import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile


HERE = Path(__file__).resolve().parent
LOCK = json.loads((HERE / "source.lock.json").read_text(encoding="utf-8"))
PATCH = HERE / "source.patch"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def run(*args, cwd=None, timeout=1200):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed ({result.returncode}): {result.stderr[-4000:]}")
    return result.stdout.strip()


def verify_oci(archive):
    with tarfile.open(archive, "r") as outer:
        index = json.load(outer.extractfile("index.json"))
        if index.get("schemaVersion") != 2 or len(index.get("manifests", [])) != 1:
            raise RuntimeError("OCI index must contain exactly one manifest")
        descriptor = index["manifests"][0]
        manifest_bytes = outer.extractfile("blobs/sha256/" + descriptor["digest"].split(":")[1]).read()
        if descriptor["digest"] != "sha256:" + sha256(manifest_bytes):
            raise RuntimeError("OCI manifest digest mismatch")
        manifest = json.loads(manifest_bytes)
        config_descriptor = manifest["config"]
        config_bytes = outer.extractfile("blobs/sha256/" + config_descriptor["digest"].split(":")[1]).read()
        if config_descriptor["digest"] != "sha256:" + sha256(config_bytes):
            raise RuntimeError("OCI config digest mismatch")
        config = json.loads(config_bytes)
        if (config.get("os"), config.get("architecture")) != ("linux", "amd64"):
            raise RuntimeError("OCI platform mismatch")
        last = manifest["layers"][-1]
        layer_bytes = outer.extractfile("blobs/sha256/" + last["digest"].split(":")[1]).read()
        if last["digest"] != "sha256:" + sha256(layer_bytes):
            raise RuntimeError("OCI layer digest mismatch")
        with tarfile.open(fileobj=io.BytesIO(layer_bytes), mode="r:*") as layer:
            matches = [entry for entry in layer if entry.isfile() and entry.name.lstrip("./") == "free5gc/pcf"]
            if len(matches) != 1:
                raise RuntimeError("OCI layer must replace one PCF binary")
            binary = layer.extractfile(matches[0]).read()
        actual = sha256(binary)
        if actual != LOCK["expectedBinarySha256"]:
            raise RuntimeError("OCI binary differs from locked v3 candidate")
        return {"manifestDigest": descriptor["digest"], "configDigest": config_descriptor["digest"],
                "binarySha256": actual, "archiveSha256": sha256(archive.read_bytes())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="new output directory")
    parser.add_argument("--source-repo", default=LOCK["sourceRepository"],
                        help="Git URL or independent mirror; exact pinned commit is enforced")
    parser.add_argument("--builder", help="existing Docker Buildx docker-container builder")
    args = parser.parse_args()
    out = args.output.resolve()
    if out.exists():
        parser.error("output must not exist")
    if sha256(PATCH.read_bytes()) != LOCK["patchSha256"]:
        raise RuntimeError("tracked source patch SHA256 mismatch")
    out.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="cityverse-pcf-replay-") as temp:
        source = Path(temp) / "pcf"
        # The v3 candidate was compiled from a Windows checkout: untouched
        # upstream files had CRLF, while the seven edited files had LF. Go's
        # BuildID hashes those source bytes, so preserve that exact input.
        run("git", "clone", "--config", "core.autocrlf=true", "--filter=blob:none",
            "--no-checkout", args.source_repo, str(source))
        run("git", "checkout", "--detach", LOCK["sourceCommit"], cwd=source)
        if run("git", "rev-parse", "HEAD", cwd=source) != LOCK["sourceCommit"]:
            raise RuntimeError("source commit mismatch")
        run("git", "apply", "--check", str(PATCH), cwd=source)
        run("git", "apply", str(PATCH), cwd=source)
        patched = [line[6:].decode("utf-8") for line in PATCH.read_bytes().splitlines()
                   if line.startswith(b"+++ b/")]
        if patched != LOCK["patchedPaths"]:
            raise RuntimeError("patch paths differ from locked source layout")
        for relative in patched:
            path = source / relative
            content = path.read_bytes()
            path.write_bytes(content.replace(b"\r\n", b"\n"))
        # The patch includes all candidate source, tests, and Dockerfile; a
        # fresh clone avoids mutable ignored checkout content.
        go = LOCK["builderImage"]
        mount = f"{source}:/src"
        common = ["docker", "run", "--rm", "--network", "host", "-v", mount, "-w", "/src",
                  "-e", "GOTOOLCHAIN=local", go, "sh", "-ec"]
        run(*common, "go mod download && go mod verify && CGO_ENABLED=1 go test -race ./internal/context ./internal/sbi ./pkg/service",
            timeout=1800)
        binary_dir = out / "binary"
        binary_dir.mkdir()
        build_mount = f"{binary_dir}:/out"
        build_cmd = ('CGO_ENABLED=0 GOTOOLCHAIN=local go build -trimpath -buildvcs=false '
                     '-ldflags="-X github.com/free5gc/util/version.VERSION=v4.2.2-cityverse-boot-receipt '
                     '-X github.com/free5gc/util/version.COMMIT_HASH=df6c0daa+boot-receipt" '
                     '-o /out/pcf ./cmd')
        run("docker", "run", "--rm", "--network", "host", "-v", mount, "-v", build_mount,
            "-w", "/src", go, "sh", "-ec", build_cmd, timeout=1800)
        binary_sha = sha256((binary_dir / "pcf").read_bytes())
        if binary_sha != LOCK["expectedBinarySha256"]:
            raise RuntimeError("fresh Go binary differs from locked v3 OCI binary")
        oci = out / "pcf-boot-receipt.oci.tar"
        build = ["docker", "buildx", "build", "--network", "host",
                 "--file", str(source / "Dockerfile.boot-receipt"),
                 "--platform", "linux/amd64", "--output", f"type=oci,dest={oci}"]
        if args.builder:
            build.extend(["--builder", args.builder])
        build.append(str(source))
        run(*build, timeout=2400)
        oci_report = verify_oci(oci)
        if oci_report["manifestDigest"] != "sha256:" + LOCK["referenceV3OciManifestSha256"]:
            raise RuntimeError("OCI manifest differs from locked v3 candidate")
        if oci_report["configDigest"] != "sha256:" + LOCK["referenceV3OciConfigSha256"]:
            raise RuntimeError("OCI config differs from locked v3 candidate")
    report = {"sourceCommit": LOCK["sourceCommit"], "patchSha256": LOCK["patchSha256"],
              "goRaceTests": "passed", "freshBinarySha256": binary_sha,
              "oci": oci_report, "referenceV3OciManifestSha256": LOCK["referenceV3OciManifestSha256"],
              "deployed": False, "scope": LOCK["scope"]}
    (out / "replay-receipt.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
