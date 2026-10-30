"""Package the release-safe gNB PDU-timeout candidate; no cluster writes."""

import json
from pathlib import Path

import package_nas_fix_oci as oci


ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / ".tmp/gnb-pdu-timeout-20261005"
oci.MODES["gnb"] = {
    "base": ROOT / ".tmp/gnb-order-20261004/free-ran-ue-uplink-order-verified.oci.tar",
    "base_archive_sha": "f86109453a47e2377df7192e771abceea25be98163f4745d41e7aa2cd3fb1783",
    "base_manifest": "sha256:165c4afb7d996b9451edf9be19ab477cd9718b46512e7f0ab9bbbda4da65d4dd",
    "binary": ARTIFACTS / "free-ran-ue-pdu-timeout-release-safe",
    "target": "free-ran-ue/free-ran-ue",
    "image": "docker.io/cityverse/free-ran-ue",
    "tag": "docker.io/cityverse/free-ran-ue:pdu-timeout-release-safe-20261005",
    "created_by": "cityverse gNB connection-anchored PDU timeout and release-safe AMF completion",
    "patch": ROOT / "backend/onprem/images/patches/fru-gnb-pdu-timeout.patch",
    "output": ARTIFACTS / "free-ran-ue-pdu-timeout-release-safe.oci.tar",
}


if __name__ == "__main__":
    print(json.dumps(oci.package("gnb"), sort_keys=True))
