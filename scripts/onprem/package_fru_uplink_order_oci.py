"""Package the offline gNB uplink-order candidate over the exact live OCI image.

This writes a candidate archive and receipt only. It does not import, push, or
change any Kubernetes resource. Build the binary from the reviewed source first.
"""

import json
from pathlib import Path

import package_nas_fix_oci as oci


ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / ".tmp/gnb-order-20261004"
oci.MODES["gnb"] = {
    "base": ROOT / ".cache/free-ran-ue-preflight/build/free-ran-ue-ngap-guard-reviewed.oci.tar",
    "base_archive_sha": "e6c54012a2f7717f65597d297d0d09faec01f355a566c874beabe059cc26a728",
    "base_manifest": "sha256:be15100a4ce6fa74161ee982657d3f61ff4438b11378160992a7b09a32682228",
    "binary": ARTIFACTS / "free-ran-ue-candidate",
    "target": "free-ran-ue/free-ran-ue",
    "image": "docker.io/cityverse/free-ran-ue",
    "tag": "docker.io/cityverse/free-ran-ue:uplink-order-20261004",
    "created_by": "cityverse gNB uplink ordering correction",
    "patch": ROOT / "backend/onprem/images/patches/fru-gnb-uplink-order.patch",
    "output": ARTIFACTS / "free-ran-ue-uplink-order-verified.oci.tar",
}


if __name__ == "__main__":
    print(json.dumps(oci.package("gnb"), sort_keys=True))
