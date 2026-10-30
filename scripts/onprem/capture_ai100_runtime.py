"""Read the exact WSL AI100 runtime fingerprint from four live pinned UPFs.

Only Kubernetes GET requests are made. The new JSON output is an input to the
100 UE manifest builder, never a claim that capacity or AI E2E passed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.onprem.agents.startup import _runtime
from scripts.onprem.start_native_backend import UE_IMAGE, INIT_IMAGE, TRAFFIC_IMAGE


class ReadOnlyDriver:
    def __init__(self, kubectl, kubeconfig, context, namespace):
        self.kubectl_path, self.kubeconfig = kubectl, kubeconfig
        self.context, self.namespace = context, namespace
        self.image, self.init_image = UE_IMAGE, INIT_IMAGE

    def kubectl(self, *args):
        if args[:1] != ("get",):
            raise ValueError("Runtime capture permits Kubernetes GET only")
        raw = subprocess.check_output([str(self.kubectl_path), "--kubeconfig", str(self.kubeconfig),
                                       "--context", self.context, "-n", self.namespace, *args],
                                      timeout=20)
        return json.loads(raw)


def capture(kubectl, kubeconfig, context, namespace):
    if (context != "kubernetes-admin@cityverse-wsl" or namespace != "free5gc-onprem"
            or not kubeconfig.is_absolute() or not kubeconfig.is_file()
            or not kubectl.is_absolute() or not kubectl.is_file()):
        raise ValueError("Exact WSL kubeadm target and installed tools are required")
    return _runtime(ReadOnlyDriver(kubectl, kubeconfig, context, namespace), TRAFFIC_IMAGE)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubectl", type=Path, default=Path("/usr/bin/kubectl"))
    parser.add_argument("--kubeconfig", type=Path, default=Path("/etc/kubernetes/admin.conf"))
    parser.add_argument("--context", default="kubernetes-admin@cityverse-wsl")
    parser.add_argument("--namespace", default="free5gc-onprem")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    runtime = capture(args.kubectl, args.kubeconfig, args.context, args.namespace)
    canonical = json.dumps(runtime, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    with args.output.open("xb") as target:
        target.write(json.dumps(runtime, sort_keys=True, indent=2).encode() + b"\n")
    print(json.dumps({"runtimeJson": str(args.output),
                      "sha256": hashlib.sha256(canonical).hexdigest(),
                      "mode": runtime["mode"], "context": runtime["context"],
                      "namespace": runtime["namespace"]}))


if __name__ == "__main__":
    main()
