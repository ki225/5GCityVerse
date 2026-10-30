"""Package the accepted PCF binary as an offline Docker archive candidate.

This script never builds, pulls, loads, runs, imports, deploys, or mutates a
cluster. It reads an already-present pinned runtime image through Docker
inspect/save, appends one deterministic layer containing /free5gc/pcf, and
validates the resulting archive before writing a proof.
"""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import tempfile
import time


SOURCE_COMMIT = "df6c0daacbc7fe5f610f19665e169f013eb05721"
PATCH_SHA = "be2dcaa1e06acd222b12666e018341785e92e26bbf7c2b2e8e918fc78f7fd220"
BINARY_SHA = "c117583bf585853be74a9ab6c7fe8f901e1f6d819c4dc636be7e8e13a1bb9678"
RUNTIME = "free5gc/pcf:v4.2.2@sha256:b6208c82a02917cb6d620336d1245c00d875254e2c9bd28416c2e250a43453a4"
RUNTIME_REF = "free5gc/pcf@sha256:b6208c82a02917cb6d620336d1245c00d875254e2c9bd28416c2e250a43453a4"
RUNTIME_MANIFEST_DIGEST = "sha256:b6208c82a02917cb6d620336d1245c00d875254e2c9bd28416c2e250a43453a4"
TAG = "docker.io/cityverse/pcf:df6c0daa-oam-be2dcaa1"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise RuntimeError(reason)


def stable_json(data) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def run(argv, *, timeout=300) -> bytes:
    result = subprocess.run(argv, capture_output=True, timeout=timeout)
    if result.returncode:
        stderr = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"command failed: {' '.join(argv)} :: {stderr}")
    return result.stdout


def docker(argv, *, timeout=300) -> bytes:
    return run(["wsl.exe", "-d", "Ubuntu", "--", "docker", *argv], timeout=timeout)


def safe_member_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    require(not path.is_absolute() and ".." not in path.parts, f"unsafe tar path: {name}")
    return path


def read_archive(path: Path):
    entries = {}
    with tarfile.open(path, "r") as archive:
        for member in archive.getmembers():
            safe_member_path(member.name)
            if member.isfile():
                extracted = archive.extractfile(member)
                require(extracted is not None, f"unreadable tar member: {member.name}")
                entries[member.name] = extracted.read()
    return entries


def make_layer(binary: Path) -> bytes:
    payload = binary.read_bytes()
    require(sha(payload) == BINARY_SHA, "accepted binary sha256 mismatch")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as archive:
        info = tarfile.TarInfo("free5gc/pcf")
        info.size = len(payload)
        info.mode = 0o755
        info.uid = 0
        info.gid = 0
        info.uname = "root"
        info.gname = "root"
        info.mtime = 0
        archive.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def tar_add_file(archive: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = 0
    archive.addfile(info, io.BytesIO(data))


def tar_add_dir(archive: tarfile.TarFile, name: str) -> None:
    info = tarfile.TarInfo(name.rstrip("/") + "/")
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = 0
    archive.addfile(info)


def validate_source(binary_dir: Path, root_proof: Path) -> dict:
    provenance_path = binary_dir / "provenance.json"
    root_summary_path = root_proof / "summary.json"
    provenance = json.loads(provenance_path.read_text())
    root_summary = json.loads(root_summary_path.read_text())
    require(provenance["sourceCommit"] == SOURCE_COMMIT, "source commit mismatch")
    require(provenance["patchSha256"] == PATCH_SHA, "patch sha mismatch")
    require(provenance["binarySha256"] == BINARY_SHA, "provenance binary sha mismatch")
    require(root_summary["binarySha256"] == BINARY_SHA, "root proof binary sha mismatch")
    require(root_summary["archiveAndPatchVerified"] is True, "root source proof not accepted")
    require(root_summary["imagePackaged"] is False, "root proof unexpectedly pre-packaged")
    require(root_summary["containerImageSmoke"] == "not_run", "unexpected container smoke status")
    return {"provenance": provenance, "rootSummary": root_summary}


def validate_final_archive(path: Path, expected: dict) -> dict:
    entries = read_archive(path)
    manifest = json.loads(entries["manifest.json"])
    require(len(manifest) == 1, "archive must contain one image manifest")
    manifest_entry = manifest[0]
    require(manifest_entry["RepoTags"] == [TAG], "canonical tag missing from archive manifest")
    config_name = manifest_entry["Config"]
    require(config_name in entries, "archive config missing")
    config_raw = entries[config_name]
    config_digest = "sha256:" + sha(config_raw)
    config = json.loads(config_raw)
    require(config_digest == expected["configDigest"], "config digest changed after write")
    require(config["config"]["Labels"] == expected["labels"], "provenance labels changed")
    require(config["rootfs"]["diff_ids"][-1] == expected["newLayerDiffId"], "new layer diff_id mismatch")
    require(config["history"][-1] == expected["newHistory"], "new history mismatch")
    require(manifest_entry["Layers"][-1] == expected["newLayerPath"], "new layer path mismatch")
    index = json.loads(entries["index.json"])
    require(index["manifests"][0]["digest"] == expected["ociManifestDigest"], "OCI index manifest digest mismatch")
    oci_manifest_path = "blobs/sha256/" + expected["ociManifestDigest"].removeprefix("sha256:")
    oci_manifest_raw = entries[oci_manifest_path]
    require(sha(oci_manifest_raw) == expected["ociManifestDigest"].removeprefix("sha256:"), "OCI manifest blob hash mismatch")
    oci_manifest = json.loads(oci_manifest_raw)
    require(oci_manifest["config"]["digest"] == config_digest, "OCI config digest mismatch")
    require(oci_manifest["layers"][-1]["digest"] == expected["newLayerDiffId"], "OCI new layer digest mismatch")
    layer_raw = entries[expected["newLayerPath"]]
    require(sha(layer_raw) == expected["newLayerTarSha256"], "new layer tar hash mismatch")
    with tarfile.open(fileobj=io.BytesIO(layer_raw), mode="r") as layer_tar:
        members = layer_tar.getmembers()
        require([member.name for member in members] == ["free5gc/pcf"], "new layer must contain only /free5gc/pcf")
        member = members[0]
        require(member.isfile() and member.mode == 0o755 and member.uid == 0 and member.gid == 0, "bad PCF layer metadata")
        extracted = layer_tar.extractfile(member)
        require(extracted is not None and sha(extracted.read()) == BINARY_SHA, "layer binary sha mismatch")
    for layer_path, layer_sha in expected["baseLayerTarSha256"].items():
        require(sha(entries[layer_path]) == layer_sha, f"base layer changed: {layer_path}")
    return {
        "archiveSha256": sha(path.read_bytes()),
        "configDigest": config_digest,
        "layerCount": len(manifest_entry["Layers"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary-dir", required=True, type=Path)
    parser.add_argument("--root-proof", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    tmp = out / "tmp"
    tmp.mkdir()
    source = validate_source(args.binary_dir, args.root_proof)
    binary = args.binary_dir / "pcf"
    require(binary.is_file(), "accepted binary missing")

    inspect = json.loads(docker(["image", "inspect", RUNTIME_REF]))[0]
    require(RUNTIME_REF in inspect.get("RepoDigests", []), "pinned runtime digest not present locally")
    require(inspect["Os"] == "linux" and inspect["Architecture"] == "amd64", "runtime platform mismatch")
    base_config_id = inspect["Id"]
    base_archive = out / "base-runtime.docker-archive.tar"
    docker(["save", "-o", "/mnt/d/projects/5GCityVerse/.cache/pcf-oam-image-package-01/base-runtime.docker-archive.tar", RUNTIME_REF], timeout=600)
    base_entries = read_archive(base_archive)
    base_manifest = json.loads(base_entries["manifest.json"])
    base_index = json.loads(base_entries["index.json"])
    require(len(base_manifest) == 1, "base archive must contain one image")
    require(len(base_index["manifests"]) == 1, "base OCI index must contain one image")
    base_manifest_entry = base_manifest[0]
    base_oci_manifest_digest = base_index["manifests"][0]["digest"]
    base_oci_manifest_name = "blobs/sha256/" + base_oci_manifest_digest.removeprefix("sha256:")
    base_oci_manifest = json.loads(base_entries[base_oci_manifest_name])
    base_config_name = base_manifest_entry["Config"]
    base_config = json.loads(base_entries[base_config_name])
    require("sha256:" + sha(base_entries[base_config_name]) == base_config_id, "base config digest mismatch")

    labels = dict(base_config.get("config", {}).get("Labels") or {})
    labels.update({
        "org.opencontainers.image.revision": SOURCE_COMMIT,
        "io.cityverse.pcf.oam.patch-sha256": PATCH_SHA,
        "io.cityverse.pcf.oam.binary-sha256": BINARY_SHA,
        "io.cityverse.pcf.oam.runtime": RUNTIME,
        "io.cityverse.pcf.oam.root-proof": str(args.root_proof).replace("\\", "/"),
        "io.cityverse.pcf.oam.container-smoke": "not_run",
    })
    candidate_config = json.loads(json.dumps(base_config))
    candidate_config.setdefault("config", {})["Labels"] = labels
    layer = make_layer(binary)
    layer_sha = sha(layer)
    diff_id = "sha256:" + layer_sha
    layer_path = "blobs/sha256/" + layer_sha
    candidate_config.setdefault("rootfs", {}).setdefault("diff_ids", []).append(diff_id)
    history = candidate_config.setdefault("history", [])
    new_history = {
        "created": "1970-01-01T00:00:00Z",
        "created_by": "cityverse offline PCF OAM binary layer append",
        "comment": "contains only /free5gc/pcf from accepted binary c117583bf585",
    }
    history.append(new_history)
    config_raw = stable_json(candidate_config)
    config_digest = "sha256:" + sha(config_raw)
    config_name = "blobs/sha256/" + config_digest.removeprefix("sha256:")
    manifest_entry = {
        "Config": config_name,
        "RepoTags": [TAG],
        "Layers": [*base_manifest_entry["Layers"], layer_path],
    }
    layer_descriptor = {
        "mediaType": "application/vnd.oci.image.layer.v1.tar",
        "digest": diff_id,
        "size": len(layer),
    }
    oci_manifest = json.loads(json.dumps(base_oci_manifest))
    oci_manifest["config"] = {
        "mediaType": "application/vnd.oci.image.config.v1+json",
        "digest": config_digest,
        "size": len(config_raw),
    }
    oci_manifest["layers"] = [*base_oci_manifest["layers"], layer_descriptor]
    oci_manifest_raw = stable_json(oci_manifest)
    oci_manifest_digest = "sha256:" + sha(oci_manifest_raw)
    oci_manifest_name = "blobs/sha256/" + oci_manifest_digest.removeprefix("sha256:")
    index = json.loads(json.dumps(base_index))
    index["manifests"][0] = {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "digest": oci_manifest_digest,
        "size": len(oci_manifest_raw),
        "annotations": {"org.opencontainers.image.ref.name": TAG},
    }
    layer_sources = dict(base_manifest_entry.get("LayerSources") or {})
    layer_sources[diff_id] = layer_descriptor
    manifest_entry["LayerSources"] = layer_sources
    base_layer_hashes = {name: sha(base_entries[name]) for name in base_manifest_entry["Layers"]}
    final_archive = out / "cityverse-pcf-df6c0daa-oam-be2dcaa1.docker-archive.tar"

    with tempfile.NamedTemporaryFile(dir=tmp, delete=False) as handle:
        temp_name = Path(handle.name)
    try:
        with tarfile.open(temp_name, "w", format=tarfile.PAX_FORMAT) as archive:
            tar_add_dir(archive, "blobs")
            tar_add_dir(archive, "blobs/sha256")
            for name in sorted(base_entries):
                if name in {"index.json", "manifest.json"}:
                    continue
                tar_add_file(archive, name, base_entries[name])
            tar_add_file(archive, layer_path, layer)
            tar_add_file(archive, config_name, config_raw)
            tar_add_file(archive, oci_manifest_name, oci_manifest_raw)
            tar_add_file(archive, "index.json", stable_json(index))
            tar_add_file(archive, "manifest.json", stable_json([manifest_entry]))
        shutil.move(temp_name, final_archive)
    finally:
        if temp_name.exists():
            temp_name.unlink()

    expected = {
        "labels": labels,
        "configDigest": config_digest,
        "newLayerDiffId": diff_id,
        "newLayerPath": layer_path,
        "newLayerTarSha256": layer_sha,
        "newHistory": new_history,
        "baseLayerTarSha256": base_layer_hashes,
        "ociManifestDigest": oci_manifest_digest,
    }
    validation = validate_final_archive(final_archive, expected)
    proof = {
        "tag": TAG,
        "runtime": RUNTIME,
        "runtimeRepoDigestPresent": RUNTIME_MANIFEST_DIGEST,
        "baseConfigDigest": base_config_id,
        "candidateConfigDigest": config_digest,
        "candidateArchive": str(final_archive),
        "candidateArchiveSha256": validation["archiveSha256"],
        "candidateLayerDiffId": diff_id,
        "candidateLayerTarSha256": layer_sha,
        "candidateOciManifestDigest": oci_manifest_digest,
        "baseLayerTarSha256": base_layer_hashes,
        "sourceCommit": SOURCE_COMMIT,
        "patchSha256": PATCH_SHA,
        "binarySha256": BINARY_SHA,
        "rootProof": str(args.root_proof),
        "rootSummary": source["rootSummary"],
        "sourceProvenanceReadBack": {
            "sourceArchiveSha256": source["provenance"]["sourceArchiveSha256"],
            "testStatus": source["provenance"]["testStatus"],
        },
        "binaryNetworkIsolatedHelpPassed": source["rootSummary"]["networkIsolatedBinaryHelp"],
        "containerImageSmoke": "not_run",
        "deployed": False,
        "clusterMutation": False,
        "dockerActions": ["image inspect pinned local runtime", "save pinned local runtime"],
        "validation": {
            "archiveSchema": "OCI layout with Docker save manifest.json compatibility",
            "baseLayersByteIdentical": True,
            "newLayerContainsOnly": "/free5gc/pcf",
            "rootfsDiffIdsAndHistoryMatchNewLayer": True,
            "configPreservedExceptLabelsRootfsHistory": True,
        },
        "digestMeanings": {
            "candidateConfigDigest": "sha256 of image config JSON; Docker image ID if loaded later",
            "candidateLayerDiffId": "sha256 of the uncompressed appended layer tar",
            "candidateArchiveSha256": "sha256 of this portable Docker archive file; not a registry manifest digest",
        },
        "createdAtUnix": int(time.time()),
    }
    (out / "proof.json").write_text(json.dumps(proof, indent=2, sort_keys=True) + "\n")
    (out / "config.json").write_bytes(config_raw)
    (out / "manifest.json").write_bytes(stable_json([manifest_entry]))
    (out / "IMPORT_CONTRACT.md").write_text(
        "\n".join([
            "# PCF OAM Candidate Import Contract",
            "",
            f"Tag: `{TAG}`",
            "",
            "Importers must validate the Docker archive before import:",
            "",
            "- `manifest.json` contains exactly one image and RepoTags equals the canonical tag.",
            "- Config digest equals `candidateConfigDigest` in `proof.json`.",
            "- All base layer tar SHA-256 values equal `baseLayerTarSha256` in `proof.json`.",
            "- The final layer tar SHA-256 equals `candidateLayerTarSha256` and contains only `free5gc/pcf`.",
            "- The final layer uncompressed diff ID equals the appended `rootfs.diff_ids` entry.",
            "- The final `history` entry matches the append operation in `proof.json`.",
            "- Binary SHA-256 inside the final layer equals the accepted binary SHA-256.",
            "- `containerImageSmoke` is `not_run`; import must not treat it as passed.",
            "",
            "This package was not loaded, imported, deployed, run, or pushed by the packager.",
            "",
        ])
        + "\n"
    )
    shutil.rmtree(tmp)
    print(json.dumps({
        "archive": str(final_archive),
        "archiveSha256": validation["archiveSha256"],
        "configDigest": config_digest,
        "layerDiffId": diff_id,
        "proof": str(out / "proof.json"),
    }, indent=2))


if __name__ == "__main__":
    main()
