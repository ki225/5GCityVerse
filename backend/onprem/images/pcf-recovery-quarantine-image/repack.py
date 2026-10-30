"""Offline Windows cross-build and pinned-base OCI repack for PCF quarantine.

This is a separate packaging method from Docker Buildx. It does not load the
image, alter Kubernetes, or assert equality with a Docker Buildx digest.
"""

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile


HERE = Path(__file__).resolve().parent
LOCK = json.loads((HERE / "source.lock.json").read_text(encoding="utf-8"))
PATCH = HERE / "source.patch"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def run(args, *, cwd=None, env=None):
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed ({result.returncode}): {result.stderr[-2500:]}")
    return result.stdout.strip()


def blob(archive, digest):
    data = archive.extractfile("blobs/sha256/" + digest[7:]).read()
    if digest != "sha256:" + sha(data):
        raise RuntimeError("OCI blob digest mismatch")
    return data


def make_layer(old_layer, binary):
    raw = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(old_layer), mode="r:*") as old:
        entries = old.getmembers()
        if [e.name for e in entries if e.isfile()] != ["free5gc/pcf"]:
            raise RuntimeError("base candidate layer must replace only the PCF binary")
        with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as new:
            for entry in entries:
                if entry.isfile():
                    entry.size = len(binary)
                    new.addfile(entry, io.BytesIO(binary))
                else:
                    new.addfile(entry)
    raw_bytes = raw.getvalue()
    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", filename="", mtime=0) as stream:
        stream.write(raw_bytes)
    return raw_bytes, compressed.getvalue()


def write_tar(path, members):
    with tarfile.open(path, "w") as archive:
        for name, data in members.items():
            entry = tarfile.TarInfo(name)
            entry.mode = 0o644
            entry.mtime = 0
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))


def package(base_path, binary, output_path):
    if sha(base_path.read_bytes()) != LOCK["baseV3ArchiveSha256"]:
        raise RuntimeError("pinned v3 base archive SHA256 mismatch")
    with tarfile.open(base_path) as old:
        index = json.load(old.extractfile("index.json"))
        if index.get("schemaVersion") != 2 or len(index.get("manifests", [])) != 1:
            raise RuntimeError("base OCI index must contain one manifest")
        manifest = json.loads(blob(old, index["manifests"][0]["digest"]))
        config = json.loads(blob(old, manifest["config"]["digest"]))
        layers = manifest["layers"]
        if [x["digest"] for x in layers[:-1]] != LOCK["baseLayerDigests"]:
            raise RuntimeError("eight pinned base layer digests differ")
        if len(layers) != 9 or len(config["rootfs"]["diff_ids"]) != 9:
            raise RuntimeError("unexpected base layer/rootfs count")
        members = {"oci-layout": old.extractfile("oci-layout").read()}
        for desc in layers[:-1]:
            members["blobs/sha256/" + desc["digest"][7:]] = blob(old, desc["digest"])
        old_last = blob(old, layers[-1]["digest"])
        raw, compressed = make_layer(old_last, binary)
        new_layer_digest = "sha256:" + sha(compressed)
        members["blobs/sha256/" + sha(compressed)] = compressed
        layers[-1] = {"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                      "digest": new_layer_digest, "size": len(compressed)}
        config["rootfs"]["diff_ids"][-1] = "sha256:" + sha(raw)
        config["config"].setdefault("Labels", {})["io.cityverse.pcf.scope"] = (
            "offline-recovery-quarantine-candidate")
        config["config"]["Labels"]["io.cityverse.pcf.packaging"] = (
            "windows-go1.25.5-cross-build+python-oci-repack")
        config["created"] = "2026-10-05T00:00:00Z"
        config["history"][-1]["created"] = config["created"]
        config["history"][-1]["created_by"] = "COPY pinned PCF quarantine binary /free5gc/pcf"
        config_bytes = encoded(config)
        config_digest = "sha256:" + sha(config_bytes)
        manifest["config"] = {"mediaType": "application/vnd.oci.image.config.v1+json",
                              "digest": config_digest, "size": len(config_bytes)}
        manifest["annotations"] = {
            "io.cityverse.pcf.patch-sha256": LOCK["patchSha256"],
            "io.cityverse.pcf.binary-sha256": sha(binary),
        }
        manifest_bytes = encoded(manifest)
        manifest_digest = "sha256:" + sha(manifest_bytes)
        members["blobs/sha256/" + sha(config_bytes)] = config_bytes
        members["blobs/sha256/" + sha(manifest_bytes)] = manifest_bytes
        index["manifests"] = [{"mediaType": "application/vnd.oci.image.manifest.v1+json",
                               "digest": manifest_digest, "size": len(manifest_bytes)}]
        members["index.json"] = encoded(index)
    write_tar(output_path, members)
    with tarfile.open(output_path) as check:
        check_index = json.load(check.extractfile("index.json"))
        check_manifest = json.loads(blob(check, check_index["manifests"][0]["digest"]))
        check_config = json.loads(blob(check, check_manifest["config"]["digest"]))
        if [x["digest"] for x in check_manifest["layers"][:-1]] != LOCK["baseLayerDigests"]:
            raise RuntimeError("packaged base layers differ")
        for desc in check_manifest["layers"]:
            blob(check, desc["digest"])
        last = blob(check, check_manifest["layers"][-1]["digest"])
        with tarfile.open(fileobj=io.BytesIO(last), mode="r:*") as layer:
            files = [x for x in layer if x.isfile() and x.name == "free5gc/pcf"]
            if len(files) != 1 or layer.extractfile(files[0]).read() != binary:
                raise RuntimeError("packaged binary is not bit exact")
        if check_config["rootfs"]["diff_ids"][-1] != "sha256:" + sha(gzip.decompress(last)):
            raise RuntimeError("packaged rootfs diffID mismatch")
    return {"manifestDigest": manifest_digest, "configDigest": config_digest,
            "layerDigest": new_layer_digest, "binarySha256": sha(binary),
            "archiveSha256": sha(output_path.read_bytes()), "baseLayersVerified": 8,
            "binaryReadbackBitExact": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-repo", required=True, type=Path)
    parser.add_argument("--base-oci", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--go-mod-cache", type=Path)
    parser.add_argument("--go-build-cache", type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        parser.error("output must not exist")
    if sha(PATCH.read_bytes()) != LOCK["patchSha256"]:
        raise RuntimeError("source patch SHA256 mismatch")
    output.mkdir(parents=True)
    with tempfile.TemporaryDirectory(dir=output, prefix="replay-") as temporary:
        source = Path(temporary) / "pcf"
        run(["git", "-c", "core.autocrlf=true", "clone", "--local", "--no-hardlinks",
             str(args.source_repo.resolve()), str(source)])
        if run(["git", "-C", str(source), "rev-parse", "HEAD"]) != LOCK["sourceCommit"]:
            raise RuntimeError("pinned source commit mismatch")
        run(["git", "-C", str(source), "apply", "--check", str(PATCH)])
        run(["git", "-C", str(source), "apply", str(PATCH)])
        patched = [line[6:].decode("utf-8") for line in PATCH.read_bytes().splitlines()
                   if line.startswith(b"+++ b/")]
        if patched != LOCK["patchedPaths"]:
            raise RuntimeError("patched file list mismatch")
        for relative in patched:
            path = source / relative
            data = path.read_bytes().replace(b"\r\n", b"\n")
            if relative == "internal/context/context.go":
                data = data.replace(b"\n", b"\r\n")
            path.write_bytes(data)
        environment = os.environ.copy()
        environment.update({"GOOS": "linux", "GOARCH": "amd64", "CGO_ENABLED": "0",
                            "GOTOOLCHAIN": "auto",
                            "GOMODCACHE": str((args.go_mod_cache or output / "go-mod").resolve()),
                            "GOCACHE": str((args.go_build_cache or output / "go-build").resolve()),
                            "TMP": str(output / "tmp"), "TEMP": str(output / "tmp")})
        for variable in ("GOMODCACHE", "GOCACHE", "TMP"):
            Path(environment[variable]).mkdir(exist_ok=True)
        version = run(["go", "env", "GOVERSION"], cwd=source, env=environment)
        if version != "go1.25.5":
            raise RuntimeError("Go compiler version differs from pinned builder")
        binary_path = output / "pcf-linux-amd64"
        run(["go", "build", "-trimpath", "-buildvcs=false",
             "-ldflags=-X github.com/free5gc/util/version.VERSION=v4.2.2-cityverse-recovery-quarantine "
             "-X github.com/free5gc/util/version.COMMIT_HASH=df6c0daa+recovery-quarantine",
             "-o", str(binary_path), "./cmd"], cwd=source, env=environment)
        binary = binary_path.read_bytes()
        if sha(binary) != LOCK["expectedBinarySha256"]:
            raise RuntimeError("fresh replay binary differs from source lock")
        binary_metadata = run(["go", "version", "-m", str(binary_path)], cwd=source,
                              env=environment)
        if "GOOS=linux" not in binary_metadata or "GOARCH=amd64" not in binary_metadata:
            raise RuntimeError("cross-built binary platform mismatch")
    archive = output / "pcf-recovery-quarantine.oci.tar"
    report = package(args.base_oci.resolve(), binary, archive)
    for key, actual in (("reviewCandidateManifestSha256", report["manifestDigest"][7:]),
                        ("reviewCandidateConfigSha256", report["configDigest"][7:]),
                        ("reviewCandidateArchiveSha256", report["archiveSha256"])):
        if actual != LOCK[key]:
            raise RuntimeError(f"replayed OCI differs from locked candidate: {key}")
    receipt = {"sourceCommit": LOCK["sourceCommit"], "patchSha256": LOCK["patchSha256"],
               "goVersion": version, "goRaceTests": "separately-passed-in-isolated-WSL-source",
               "packagingMethod": "Windows Go cross-build plus pure Python pinned-base OCI repack",
               "dockerBuildxDigestEquality": "unverified", "deployed": False,
               "oci": report, "scope": LOCK["scope"]}
    (output / "repack-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n",
                                                encoding="utf-8")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
