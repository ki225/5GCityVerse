"""Bounded, read-only native PDU/session readback for functional acceptance."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping

from backend.onprem.storage.readonly import ReadOnlyGenerationStore
from backend.onprem.runtime.ue import EvidenceUnavailable, EvidenceVerifier, SLICES


SCHEMA_VERSION = "cityverse.native-session-readback/v1"
_SUPI = re.compile(r"imsi-[0-9]{15}")


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(raw).hexdigest()


def _upf_epochs(evidence: Mapping[str, Any]) -> dict[str, str]:
    try:
        value = json.loads(evidence["sources"]["upf"]["epoch"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise EvidenceUnavailable("Native UPF source epoch is unavailable") from None
    if (not isinstance(value, dict) or set(value) != set(SLICES)
            or any(not isinstance(item, str) or not item for item in value.values())):
        raise EvidenceUnavailable("Native UPF source epoch is unavailable")
    return value


def _source_times(evidence: Mapping[str, Any]) -> dict[str, Any]:
    sources = evidence.get("sources")
    if not isinstance(sources, Mapping):
        raise EvidenceUnavailable("Native source timestamps are unavailable")
    result = {name: value.get("observedAt") for name, value in sources.items()
              if isinstance(name, str) and isinstance(value, Mapping)}
    if set(result) != {"amf", "smf", "upf", "ue", "traffic"}:
        raise EvidenceUnavailable("Native source timestamps are unavailable")
    return result


def produce_native_session_readback(
    *, run_id: str, epoch: str, generation: int, supi: str, pdu_session_id: str,
    ue_ip: str, tun: str, current: Callable[[], Mapping[str, Any] | None],
    observe: Callable[[int], Mapping[str, Any]], now: Callable[[], float] = time.time,
    maximum_age: float = 15,
) -> dict[str, Any]:
    """Join two native snapshots and fail closed on identity or epoch drift."""
    if not isinstance(run_id, str) or not run_id or len(run_id) > 128:
        raise EvidenceUnavailable("Benchmark run identity is invalid")
    if not isinstance(epoch, str) or not epoch or len(epoch) > 128:
        raise EvidenceUnavailable("Benchmark epoch is invalid")
    if type(generation) is not int or generation < 0 or not _SUPI.fullmatch(supi):
        raise EvidenceUnavailable("Generation or SUPI identity is invalid")
    if (not isinstance(pdu_session_id, str) or not pdu_session_id.isdigit()
            or not 1 <= int(pdu_session_id) <= 255 or not ue_ip or not tun):
        raise EvidenceUnavailable("PDU/TUN identity is invalid")

    before = current()
    if (not isinstance(before, Mapping) or before.get("generation") != generation
            or not any(row.get("supi") == supi for row in before.get("allocation", []))):
        raise EvidenceUnavailable("No matching durable generation/SUPI")

    snapshots = [observe(generation), observe(generation)]
    after = current()
    if (not isinstance(after, Mapping) or after.get("generation") != generation
            or after.get("allocation") != before.get("allocation")):
        raise EvidenceUnavailable("Generation changed during native readback")

    verifier = EvidenceVerifier(max_age=maximum_age, stable_seconds=0)
    verified = [verifier.evaluate(dict(before), dict(item), now()) for item in snapshots]
    if any(item.get("status") != "established" for item in verified):
        raise EvidenceUnavailable("Native evidence does not prove an established session")

    rows = []
    for evidence, status in zip(snapshots, verified):
        matches = [row for row in evidence.get("ues", []) if row.get("supi") == supi]
        summaries = [row for row in status.get("perUe", []) if row.get("supi") == supi]
        if len(matches) != 1 or len(summaries) != 1:
            raise EvidenceUnavailable("Unique native session identity is unavailable")
        row, summary = matches[0], summaries[0]
        if (summary.get("registered") is not True or summary.get("pduEstablished") is not True
                or summary.get("trafficVerified") is not True
                or summary.get("sessionId") != pdu_session_id or summary.get("ip") != ue_ip
                or summary.get("interface") != tun or not summary.get("podUid")):
            raise EvidenceUnavailable("Native session identity does not match benchmark binding")
        rows.append((row, summary))

    epochs = [_upf_epochs(item) for item in snapshots]
    if epochs[0] != epochs[1]:
        raise EvidenceUnavailable("Native UPF epoch changed during readback")
    first_row, first = rows[0]
    _, second = rows[1]
    stable = ("podUid", "sessionId", "ip", "interface", "upf", "pfcpSeid", "upfEpoch")
    if any(first.get(key) != second.get(key) for key in stable):
        raise EvidenceUnavailable("Native session identity changed during readback")

    source_times = [_source_times(item) for item in snapshots]
    receipts = [{"request": {"transport": "in-process", "method": "NativeCollector.observe",
                              "generation": generation},
                 "responseSchemaVersion": item.get("schemaVersion"),
                 "responseSha256": _canonical_sha256(item), "receivedAt": captured,
                 "sourceTimestamps": times}
                for item, captured, times in zip(snapshots, (now(), now()), source_times)]
    return {
        "schemaVersion": SCHEMA_VERSION, "runId": run_id, "epoch": epoch,
        "generation": generation,
        "amf": {"supi": supi, "pduSessionId": pdu_session_id, "state": "registered"},
        "smf": {"supi": supi, "pduSessionId": pdu_session_id, "state": "active"},
        "upf": {"supi": supi, "pduSessionId": pdu_session_id, "state": "active",
                "ueIp": ue_ip, "tun": tun},
        "ue": {"supi": supi, "podUid": first["podUid"], "ueIp": ue_ip, "tun": tun},
        "native": {"upf": first["upf"], "pfcpSeid": first["pfcpSeid"],
                   "sourceEpochs": epochs[0], "sourceTimestamps": source_times[1]},
        "provenance": receipts,
    }


def _required_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise EvidenceUnavailable(f"{name} is required")
    return value


def _configured_read_only_collector(store: ReadOnlyGenerationStore):
    """Build the production collector without its durable SMF-reference writer."""
    if os.environ.get("CITYVERSE_ONPREM_EVIDENCE_MODE") != "native":
        raise EvidenceUnavailable("CITYVERSE_ONPREM_EVIDENCE_MODE must be native")
    if os.environ.get("CITYVERSE_ONPREM_RAN_BACKEND", "free-ran-ue") != "free-ran-ue":
        raise EvidenceUnavailable("Only free-ran-ue native readback is supported")
    from backend.onprem.evidence.collector import (NativeCollector,
        authoritative_inventory_options, verify_authoritative_smf_runtime)
    from backend.onprem.integrations.oam import NativeOamAdapter
    from backend.onprem.runtime.free_ran_ue import FreeRanKubeadmDriver, UE_IMAGE
    from backend.onprem.runtime.kubeadm import INIT_IMAGE

    driver = FreeRanKubeadmDriver(
        os.environ.get("CITYVERSE_ONPREM_KUBE_CONTEXT", ""),
        os.environ.get("CITYVERSE_ONPREM_NAMESPACE", "free5gc-onprem"),
        batch_size=int(os.environ.get("CITYVERSE_ONPREM_UE_BATCH_SIZE", "50")),
        batch_interval=float(os.environ.get("CITYVERSE_ONPREM_UE_BATCH_INTERVAL", "30")),
        tempo_ms=int(os.environ.get("CITYVERSE_ONPREM_UE_TEMPO_MS", "500")),
        image=os.environ.get("CITYVERSE_ONPREM_FREE_RAN_UE_IMAGE", UE_IMAGE),
        init_image=os.environ.get("CITYVERSE_ONPREM_UE_INIT_IMAGE", INIT_IMAGE),
        traffic_enabled=False)
    smf_url = _required_env("CITYVERSE_ONPREM_SMF_OAM_URL").rstrip("/")
    options = authoritative_inventory_options(os.environ, smf_url)
    image = options.pop("inventory_image", None)
    if image is not None:
        verify_authoritative_smf_runtime(driver, image)
    native = NativeOamAdapter(_required_env("CITYVERSE_ONPREM_AMF_OAM_URL"), smf_url,
        os.environ.get("CITYVERSE_ONPREM_OAM_TOKEN", ""), reference_db=None, **options)
    urls = json.loads(os.environ.get("CITYVERSE_ONPREM_UPF_EVIDENCE_URLS", "{}"))
    if not urls:
        urls = {name: f"http://upf-{name}-evidence.{driver.namespace}.svc:9081" for name in SLICES}
    subscribers = SimpleNamespace(collect=lambda: {"verifiedCount": None,
        "credentialsMatched": None, "source": "not-required:session-readback"})
    return NativeCollector(store, driver, native, subscribers, urls)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--epoch", required=True)
    parser.add_argument("--status", action="store_true", required=True)
    args = parser.parse_args(argv)
    try:
        ledger_value = os.environ.get("CITYVERSE_ONPREM_UE_LEDGER") or _required_env("CITYVERSE_ONPREM_UE_STATE_FILE")
        ledger = Path(ledger_value)
        generation = int(_required_env("CITYVERSE_NATIVE_SESSION_GENERATION"))
        store = ReadOnlyGenerationStore(ledger)
        adapter = _configured_read_only_collector(store)
        result = produce_native_session_readback(
            run_id=args.run_id, epoch=args.epoch, generation=generation,
            supi=_required_env("CITYVERSE_NATIVE_SESSION_SUPI"),
            pdu_session_id=_required_env("CITYVERSE_NATIVE_SESSION_PDU_ID"),
            ue_ip=_required_env("CITYVERSE_NATIVE_SESSION_UE_IP"),
            tun=_required_env("CITYVERSE_NATIVE_SESSION_TUN"),
            current=store.current, observe=adapter.observe)
    except (EvidenceUnavailable, OSError, TypeError, ValueError) as exc:
        print(str(exc) or type(exc).__name__, file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
