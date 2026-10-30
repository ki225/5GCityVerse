"""Package a local, immutable gNB PDU-timeout candidate; no cluster writes."""

import json
from pathlib import Path

import package_nas_fix_oci as oci


ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / ".tmp/gnb-pdu-timeout-20261005"
oci.MODES["gnb"] = {
    "base": ROOT / ".tmp/gnb-order-20261004/free-ran-ue-uplink-order-verified.oci.tar",
    "base_archive_sha": "f86109453a47e2377df7192e771abceea25be98163f4745d41e7aa2cd3fb1783",
    "base_manifest": "sha256:165c4afb7d996b9451edf9be19ab477cd9718b46512e7f0ab9bbbda4da65d4dd",
    "binary": ARTIFACTS / "free-ran-ue-pdu-timeout-coordinated",
    "target": "free-ran-ue/free-ran-ue",
    "image": "docker.io/cityverse/free-ran-ue",
    "tag": "docker.io/cityverse/free-ran-ue:pdu-timeout-coordinated-20261005",
    "created_by": "cityverse gNB connection-anchored PDU completion and late-delivery guard",
    "patch": ROOT / "backend/onprem/images/patches/fru-gnb-pdu-timeout.patch",
    "output": ARTIFACTS / "free-ran-ue-pdu-timeout-coordinated.oci.tar",
}


if __name__ == "__main__":
    raise SystemExit(
        "OBSOLETE: sha256:223ce112a9bf9c68236e52053e39540e53df4ada46d1003f1b95d0d1b71bd5ae "
        "has an AMF release completion race; use package_fru_pdu_timeout_release_safe_oci.py"
    )
