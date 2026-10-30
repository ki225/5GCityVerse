"""Replay a pinned PCF recovery-quarantine source patch and build an offline OCI.

This program never loads an image into Kubernetes or changes a live workload.
Run on Linux/WSL with Git, Docker, and a running Buildx docker-container builder.
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


def run(*args, cwd=None, timeout=2400):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed ({result.returncode}): {result.stderr[-3000:]}")
    return result.stdout.strip()


def verify_oci(archive):
    with tarfile.open(archive) as outer:
        index = json.load(outer.extractfile("index.json"))
        if index.get("schemaVersion") != 2 or len(index.get("manifests", [])) != 1:
            raise RuntimeError("OCI index must contain exactly one manifest")
        desc = index["manifests"][0]
        manifest_bytes = outer.extractfile("blobs/sha256/" + desc["digest"][7:]).read()
        if desc["digest"] != "sha256:" + sha256(manifest_bytes):
            raise RuntimeError("OCI manifest digest mismatch")
        manifest = json.loads(manifest_bytes)
        config_desc = manifest["config"]
        config_bytes = outer.extractfile("blobs/sha256/" + config_desc["digest"][7:]).read()
        if config_desc["digest"] != "sha256:" + sha256(config_bytes):
            raise RuntimeError("OCI config digest mismatch")
        config = json.loads(config_bytes)
        if (config.get("os"), config.get("architecture")) != ("linux", "amd64"):
            raise RuntimeError("OCI platform mismatch")
        layer_desc = manifest["layers"][-1]
        layer_bytes = outer.extractfile("blobs/sha256/" + layer_desc["digest"][7:]).read()
        if layer_desc["digest"] != "sha256:" + sha256(layer_bytes):
            raise RuntimeError("OCI layer digest mismatch")
        with tarfile.open(fileobj=io.BytesIO(layer_bytes), mode="r:*") as layer:
            matches = [entry for entry in layer if entry.isfile()
                       and entry.name.lstrip("./") == "free5gc/pcf"]
            if len(matches) != 1:
                raise RuntimeError("OCI layer must replace exactly one PCF binary")
            binary = layer.extractfile(matches[0]).read()
        if sha256(binary) != LOCK["expectedBinarySha256"]:
            raise RuntimeError("candidate binary differs from source lock")
        return {"manifestDigest": desc["digest"],
                "configDigest": config_desc["digest"],
                "binarySha256": sha256(binary),
                "archiveSha256": sha256(archive.read_bytes())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-repo", default=LOCK["sourceRepository"])
    parser.add_argument("--builder", default="cv5g-boot-receipt")
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        parser.error("output must not exist")
    if sha256(PATCH.read_bytes()) != LOCK["patchSha256"]:
        raise RuntimeError("source patch SHA256 mismatch")
    output.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="cityverse-pcf-quarantine-") as temp:
        source = Path(temp) / "pcf"
        clone = ["git", "clone", "--config", "core.autocrlf=true", "--no-checkout"]
        if "://" not in args.source_repo:
            clone += ["--local", "--no-hardlinks"]
        else:
            clone += ["--filter=blob:none"]
        run(*clone, args.source_repo, str(source))
        run("git", "checkout", "--detach", LOCK["sourceCommit"], cwd=source)
        if run("git", "rev-parse", "HEAD", cwd=source) != LOCK["sourceCommit"]:
            raise RuntimeError("pinned source mismatch")
        run("git", "apply", "--check", str(PATCH), cwd=source)
        run("git", "apply", str(PATCH), cwd=source)
        patched = [line[6:].decode("utf-8") for line in PATCH.read_bytes().splitlines()
                   if line.startswith(b"+++ b/")]
        if patched != LOCK["patchedPaths"]:
            raise RuntimeError("patched paths differ from source lock")
        for relative in patched:
            path = source / relative
            data = path.read_bytes().replace(b"\r\n", b"\n")
            if relative == "internal/context/context.go":
                data = data.replace(b"\n", b"\r\n")
            path.write_bytes(data)
        image = LOCK["builderImage"]
        mount = f"{source}:/src"
        common = ["docker", "run", "--rm", "--network", "host", "-v", mount,
                  "-w", "/src", "-e", "GOTOOLCHAIN=local", image, "sh", "-ec"]
        run(*common, "go mod download && go mod verify && "
            "CGO_ENABLED=1 go test -race ./internal/context ./internal/sbi ./pkg/service",
            timeout=1800)
        archive = output / "pcf-recovery-quarantine.oci.tar"
        run("docker", "buildx", "build", "--builder", args.builder,
            "--network", "host", "--file", str(source / "Dockerfile.recovery-quarantine"),
            "--platform", "linux/amd64", "--output", f"type=oci,dest={archive}",
            str(source), timeout=2400)
        report = verify_oci(archive)
    receipt = {"sourceCommit": LOCK["sourceCommit"],
               "patchSha256": LOCK["patchSha256"],
               "goRaceTests": "passed", "oci": report, "deployed": False,
               "scope": LOCK["scope"]}
    (output / "replay-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n",
                                                 encoding="utf-8")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
