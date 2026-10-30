"""Import an exact reviewed OCI archive into one scoped kubeadm node.

Run under WSL. This uses the existing SSH key via a short-lived RAM copy and
streams the archive directly into containerd; no privileged helper Pod is made.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / ".cache/kubeadm-lab"
RECEIPTS = {
    "gnb": (ROOT / ".cache/free-ran-ue-preflight/build/free-ran-ue-ngap-guard-reviewed.oci.receipt.json",
            ROOT / ".cache/free-ran-ue-preflight/build/free-ran-ue-ngap-guard-reviewed.oci.tar", "192.168.240.14"),
    "udm": (ROOT / ".cache/free-ran-ue-preflight/diagnostics/udm-auth-retry/build/udm-auth-get-retry-reviewed.oci.receipt.json",
            ROOT / ".cache/free-ran-ue-preflight/diagnostics/udm-auth-retry/build/udm-auth-get-retry-reviewed.oci.tar", "192.168.240.11"),
}
TAGS = {
    "gnb": "docker.io/cityverse/free-ran-ue:ngap-guard-20261003",
    "udm": "docker.io/cityverse/udm:auth-get-retry-20261003",
}


def sha256_file(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return "sha256:" + value.hexdigest()


def node_image_digest(ssh: list[str], reference: str) -> str | None:
    listed = subprocess.run([*ssh, "sudo ctr -n k8s.io images ls"], capture_output=True,
                            text=True, timeout=30, check=False)
    if listed.returncode:
        raise RuntimeError("containerd image inventory unavailable")
    matches = [line.split() for line in listed.stdout.splitlines()[1:]
               if line.split() and line.split()[0] == reference]
    if len(matches) > 1 or (matches and len(matches[0]) < 3):
        raise RuntimeError("ambiguous containerd image reference")
    return matches[0][2] if matches else None


def main(mode: str, check_only: bool = False, tag_only: bool = False) -> None:
    receipt_path, archive, address = RECEIPTS[mode]
    receipt = json.loads(receipt_path.read_text())
    archive = archive.resolve(strict=True)
    if (archive.parent != receipt_path.parent or receipt["mode"] != mode
            or Path(receipt["archive"].replace("\\", "/")).name != archive.name):
        raise ValueError("archive path or mode does not match scoped receipt")
    if sha256_file(archive) != receipt["archiveSha256"]:
        raise ValueError("archive digest mismatch")
    if receipt["image"].split("@", 1)[-1] != receipt["manifestDigest"]:
        raise ValueError("image reference mismatch")
    known_hosts = CACHE / "known_hosts"
    with tempfile.NamedTemporaryFile(dir="/dev/shm", prefix="cv5g-key-") as identity:
        identity.write((CACHE / "id_ed25519").read_bytes())
        identity.flush()
        os.chmod(identity.name, 0o600)
        ssh = ["ssh", "-i", identity.name, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
               "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + str(known_hosts),
               "ubuntu@" + address]
        ready = subprocess.run([*ssh, "sudo ctr -n k8s.io version"], capture_output=True,
                               text=True, timeout=30, check=False)
        if ready.returncode:
            raise RuntimeError("scoped containerd node is unavailable")
        if check_only:
            listed = subprocess.run([*ssh, "sudo ctr -n k8s.io images ls -q"],
                                    capture_output=True, text=True, timeout=30, check=False)
            if listed.returncode:
                raise RuntimeError("containerd image inventory unavailable")
            refs = listed.stdout.splitlines()
            print(json.dumps({"mode": mode, "node": address, "image": receipt["image"],
                              "archiveSha256": receipt["archiveSha256"], "containerdReady": True,
                              "exactRefPresent": receipt["image"] in refs,
                              "candidateRefs": [ref for ref in refs if ref.startswith(receipt["image"].split("@", 1)[0])]},
                             sort_keys=True))
            return
        if not tag_only:
            with archive.open("rb") as source:
                imported = subprocess.run(
                    [*ssh, "sudo ctr -n k8s.io images import --digests --platform linux/amd64 -"],
                    stdin=source, capture_output=True, timeout=300, check=False)
            if imported.returncode:
                raise RuntimeError("containerd import failed: " + imported.stderr.decode(errors="replace")[-1500:])
        manifest = receipt["manifestDigest"]
        checked = subprocess.run(
            [*ssh, "sudo ctr -n k8s.io content get " + manifest + " | sha256sum"],
            capture_output=True, text=True, timeout=30, check=False)
        if checked.returncode or checked.stdout.split()[0] != manifest[7:]:
            raise RuntimeError("node content-store manifest verification failed")
        source_tag = TAGS[mode]
        source_digest = node_image_digest(ssh, source_tag)
        if source_digest != manifest:
            raise RuntimeError("candidate tag target mismatch: " + source_digest)
        tagged = subprocess.run([*ssh, "sudo ctr -n k8s.io images tag " + source_tag + " " + receipt["image"]],
                                capture_output=True, text=True, timeout=30, check=False)
        if tagged.returncode:
            raise RuntimeError("exact digest reference creation failed: " + tagged.stderr[-1000:])
        if node_image_digest(ssh, receipt["image"]) != manifest:
            raise RuntimeError("exact digest reference readback mismatch")
        print(json.dumps({"mode": mode, "node": address, "image": receipt["image"],
                          "archiveSha256": receipt["archiveSha256"],
                          "manifestVerified": True, "exactRefPresent": True}, sort_keys=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=sorted(RECEIPTS))
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--tag-only", action="store_true")
    args = parser.parse_args()
    main(args.mode, args.check_only, args.tag_only)
