"""Package the reviewed gNB/UDM binaries over exact, already accepted OCI bases.

No registry push or Kubernetes mutation occurs here. The output is immutable
and the receipt pins both its input archive and resulting manifest digest.
"""

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile


ROOT = Path(__file__).resolve().parents[2]
FRU = ROOT / ".cache/free-ran-ue-preflight"
UDM = FRU / "diagnostics/udm-auth-retry"
MODES = {
    "gnb": {
        "base": FRU / "build/free-ran-ue-signal-fixed-6bb32873.oci.tar",
        "base_archive_sha": "c7659809fe643ea184ef8a0d49d1a499400b5d959b1eae553c83c88fae03a3ac",
        "base_manifest": "sha256:1b6d966a0016df5f543f3b132d9ae704946feb87bc79fd5540bde37f6d5af55c",
        "binary": FRU / "build/free-ran-ue-ngap-guard",
        "target": "free-ran-ue/free-ran-ue",
        "image": "docker.io/cityverse/free-ran-ue",
        "tag": "docker.io/cityverse/free-ran-ue:ngap-guard-20261003",
        "patch": ROOT / "backend/onprem/images/patches/fru-gnb-ngap-id-guard.patch",
        "output": FRU / "build/free-ran-ue-ngap-guard-reviewed.oci.tar",
    },
    "udm": {
        "base": UDM / "build/udm-auth-retry.oci.tar",
        "base_archive_sha": "ad5e53054e32b63f12cf2f41fa83c1d4370b7c0196b01a88140c598565ae13d8",
        "base_manifest": "sha256:08e37bd4f030e91d88a54aaba66f97cb5cf88ca87a39af6ce6778aba15bf5397",
        "binary": UDM / "build/udm-auth-get-retry",
        "target": "free5gc/udm",
        "image": "docker.io/cityverse/udm",
        "tag": "docker.io/cityverse/udm:auth-get-retry-20261003",
        "patch": ROOT / "backend/onprem/images/patches/udm-udr-auth-get-retry.patch",
        "output": UDM / "build/udm-auth-get-retry-reviewed.oci.tar",
    },
}


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def stable(data: object) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


def archive_entries(path: Path) -> dict[str, bytes]:
    result = {}
    with tarfile.open(path, "r") as archive:
        for member in archive:
            if not member.isfile():
                continue
            if member.name in result or not member.name.startswith(("blobs/sha256/", "index.json", "oci-layout")):
                raise ValueError("unexpected or repeated OCI member")
            result[member.name] = archive.extractfile(member).read()
    return result


def blob(entries: dict[str, bytes], descriptor: dict) -> bytes:
    expected = descriptor["digest"]
    if not expected.startswith("sha256:"):
        raise ValueError("unsupported OCI digest")
    data = entries["blobs/sha256/" + expected[7:]]
    if digest(data) != expected or len(data) != descriptor["size"]:
        raise ValueError("OCI blob digest or size mismatch")
    return data


def add(archive: tarfile.TarFile, name: str, data: bytes) -> None:
    member = tarfile.TarInfo(name)
    member.size = len(data)
    member.mode = 0o644
    member.uid = member.gid = member.mtime = 0
    archive.addfile(member, io.BytesIO(data))


def package(mode: str) -> dict:
    config = MODES[mode]
    output = config["output"]
    receipt_path = output.with_suffix(".receipt.json")
    if output.exists() or receipt_path.exists():
        raise FileExistsError("candidate output already exists")
    base_raw = config["base"].read_bytes()
    if digest(base_raw) != "sha256:" + config["base_archive_sha"]:
        raise ValueError("base archive drift")
    entries = archive_entries(config["base"])
    index = json.loads(entries["index.json"])
    if len(index["manifests"]) != 1 or index["manifests"][0]["digest"] != config["base_manifest"]:
        raise ValueError("base manifest drift")
    manifest = json.loads(blob(entries, index["manifests"][0]))
    image_config = json.loads(blob(entries, manifest["config"]))
    if image_config.get("os") != "linux" or image_config.get("architecture") != "amd64":
        raise ValueError("unexpected base platform")
    if len(image_config["rootfs"]["diff_ids"]) != len(manifest["layers"]):
        raise ValueError("base layer count mismatch")
    for descriptor, expected in zip(manifest["layers"], image_config["rootfs"]["diff_ids"]):
        layer = blob(entries, descriptor)
        plain = gzip.decompress(layer) if descriptor["mediaType"].endswith("gzip") else layer
        if digest(plain) != expected:
            raise ValueError("base layer mismatch")

    binary = config["binary"].read_bytes()
    if binary[:5] != b"\x7fELF\x02" or int.from_bytes(binary[18:20], "little") != 62:
        raise ValueError("binary is not Linux amd64 ELF")
    layer_stream = io.BytesIO()
    with tarfile.open(fileobj=layer_stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        member = tarfile.TarInfo(config["target"])
        member.size = len(binary)
        member.mode = 0o755
        member.uid = member.gid = member.mtime = 0
        archive.addfile(member, io.BytesIO(binary))
    plain_layer = layer_stream.getvalue()
    layer = gzip.compress(plain_layer, compresslevel=6, mtime=0)
    layer_digest = digest(layer)
    image_config["rootfs"]["diff_ids"].append(digest(plain_layer))
    image_config.setdefault("history", []).append({"created_by": config.get(
        "created_by", "cityverse NAS identity and transport correction")})
    image_config.setdefault("config", {}).setdefault("Labels", {}).update({
        "cityverse.base.digest": config["base_manifest"],
        "cityverse.patch.sha256": digest(config["patch"].read_bytes()),
        "cityverse.binary.sha256": digest(binary),
    })
    new_config = stable(image_config)
    config_descriptor = {"mediaType": manifest["config"]["mediaType"],
                         "size": len(new_config), "digest": digest(new_config)}
    new_manifest = dict(manifest)
    new_manifest["config"] = config_descriptor
    layer_media = manifest["layers"][-1]["mediaType"]
    if not layer_media.endswith("gzip"):
        raise ValueError("unsupported base layer media type")
    new_manifest["layers"] = [*manifest["layers"],
                              {"mediaType": layer_media, "size": len(layer), "digest": layer_digest}]
    manifest_raw = stable(new_manifest)
    image_ref = config["image"] + "@" + digest(manifest_raw)
    new_index = dict(index)
    new_index["manifests"] = [{"mediaType": new_manifest["mediaType"],
                               "digest": digest(manifest_raw), "size": len(manifest_raw),
                               "platform": {"os": "linux", "architecture": "amd64"},
                               "annotations": {"org.opencontainers.image.ref.name": config["tag"]}}]
    output_entries = {"oci-layout": entries["oci-layout"], "index.json": stable(new_index)}
    for descriptor in manifest["layers"]:
        output_entries["blobs/sha256/" + descriptor["digest"][7:]] = blob(entries, descriptor)
    for raw in (new_config, layer, manifest_raw):
        output_entries["blobs/sha256/" + digest(raw)[7:]] = raw
    with tarfile.open(output, "w", format=tarfile.USTAR_FORMAT) as archive:
        for name, raw in sorted(output_entries.items()):
            add(archive, name, raw)
    receipt = {"mode": mode, "baseArchiveSha256": digest(base_raw),
               "baseManifestDigest": config["base_manifest"], "binarySha256": digest(binary),
               "patchSha256": digest(config["patch"].read_bytes()),
               "manifestDigest": digest(manifest_raw), "image": image_ref,
               "archiveSha256": digest(output.read_bytes()), "archive": str(output)}
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=sorted(MODES))
    args = parser.parse_args()
    print(json.dumps(package(args.mode), sort_keys=True))
