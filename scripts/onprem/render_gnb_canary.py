"""Render an eMBB gNB image-only canary from a frozen live Deployment.

The full runtime render can advance unrelated sidecars or ConfigMaps. This
overlay preserves the captured live Deployment spec and accepts only the
independently rendered immutable eMBB gNB image as its one changed field.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re

import yaml


IMAGE = re.compile(r"docker\.io/cityverse/free-ran-ue@sha256:[0-9a-f]{64}\Z")


def render_canary(live: dict, rendered: dict) -> dict:
    for label, obj in (("live", live), ("rendered", rendered)):
        if obj.get("apiVersion") != "apps/v1" or obj.get("kind") != "Deployment":
            raise ValueError(f"{label} must be an apps/v1 Deployment")
        if obj.get("metadata", {}).get("name") != "gnb-embb":
            raise ValueError(f"{label} must be gnb-embb")
    namespace = live.get("metadata", {}).get("namespace")
    if namespace != "free5gc-onprem" or rendered["metadata"].get("namespace") != namespace:
        raise ValueError("live and rendered namespace mismatch")
    old = live["spec"]["template"]["spec"]["containers"][0]
    new = rendered["spec"]["template"]["spec"]["containers"][0]
    if old.get("name") != "gnb-embb" or new.get("name") != "gnb-embb":
        raise ValueError("first container must be the eMBB gNB")
    if not isinstance(old.get("image"), str) or not IMAGE.fullmatch(old["image"]):
        raise ValueError("live gNB image must be an immutable pinned digest")
    if not isinstance(new.get("image"), str) or not IMAGE.fullmatch(new["image"]):
        raise ValueError("candidate gNB image must be an immutable pinned digest")
    if new["image"] == old["image"]:
        raise ValueError("candidate must differ from the live image")
    manifest = {"apiVersion": "apps/v1", "kind": "Deployment",
                "metadata": {"name": "gnb-embb", "namespace": namespace},
                "spec": deepcopy(live["spec"])}
    manifest["spec"]["template"]["spec"]["containers"][0]["image"] = new["image"]
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", type=Path, required=True)
    parser.add_argument("--rendered", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    live = json.loads(args.live.read_text(encoding="utf-8"))
    rendered = yaml.safe_load(args.rendered.read_text(encoding="utf-8"))
    manifest = render_canary(live, rendered)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    print(json.dumps({"deployment": "gnb-embb", "namespace": "free5gc-onprem",
                      "oldImage": live["spec"]["template"]["spec"]["containers"][0]["image"],
                      "newImage": manifest["spec"]["template"]["spec"]["containers"][0]["image"],
                      "changedSpecFields": ["spec.template.spec.containers[0].image"]}))


if __name__ == "__main__":
    main()
