"""Fail-closed startup assembly for a receipt-verified 100-UE AI round."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
import re

from backend.onprem.agents.native_runtime import NativeAdaptiveRuntime
from backend.onprem.agents.roles import ResponsesToolModel
from backend.onprem.integrations.openai.openai_client import ALLOWED_MODEL, OpenAIClient
from backend.onprem.policy.group_qer_provider import OnpremGroupQerProvider
from backend.onprem.runtime.ue import SLICES
from backend.onprem.capacity.promotion import verify_capacity_promotion
from backend.onprem.evidence.batch_receipts import same_json


class AdaptiveStartupBlocked(ValueError):
    pass


@dataclass(frozen=True)
class UnverifiedAiTrial:
    """One explicitly configured 100 UE experiment, never a capacity grant."""

    runtime_fingerprint: str
    candidate_max_ues: int = 100


def _path(value, *, must_exist=True):
    path = Path(value) if isinstance(value, str) else None
    if (path is None or not path.is_absolute() or path.is_symlink()
            or (must_exist and not path.is_file())
            or (not must_exist and not path.parent.is_dir())):
        raise AdaptiveStartupBlocked("Pinned adaptive artifact path is unavailable")
    return path


def _read(path, limit):
    raw = _path(path).read_bytes()
    if not raw or len(raw) > limit:
        raise AdaptiveStartupBlocked("Pinned adaptive artifact size is invalid")
    return raw


def _journal_path(value):
    path = _path(value, must_exist=False)
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise AdaptiveStartupBlocked("Adaptive journal parent directory is unavailable")
    return path


def _runtime(driver, traffic_image):
    images = set()
    for name in SLICES:
        deployment = driver.kubectl("get", "deployment", "upf-" + name, "-o", "json")
        if deployment.get("metadata", {}).get("namespace") != driver.namespace:
            raise AdaptiveStartupBlocked("Live UPF deployment scope differs")
        containers = deployment.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        container_names = {"upf", "upf-" + name}
        upf = [item for item in containers if item.get("name") in container_names]
        if len(upf) != 1:
            raise AdaptiveStartupBlocked("Live UPF image is ambiguous")
        container_name = upf[0]["name"]
        image = upf[0].get("image")
        if not isinstance(image, str) or not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image):
            raise AdaptiveStartupBlocked("Live UPF image is not digest pinned")
        pods = driver.kubectl("get", "pods", "-l", "app.kubernetes.io/name=upf-" + name,
                              "-o", "json").get("items", [])
        if not isinstance(pods, list) or len(pods) != 1:
            raise AdaptiveStartupBlocked("Live UPF replica is unavailable")
        pod = pods[0]
        statuses = [item for item in pod.get("status", {}).get("containerStatuses", [])
                    if item.get("name") == container_name]
        if (pod.get("metadata", {}).get("namespace") != driver.namespace
                or pod.get("metadata", {}).get("deletionTimestamp")
                or pod.get("status", {}).get("phase") != "Running"
                or len(statuses) != 1 or statuses[0].get("ready") is not True
                or not str(statuses[0].get("imageID", "")).endswith("@" + image.rsplit("@", 1)[1])):
            raise AdaptiveStartupBlocked("Live UPF pod does not run its pinned image")
        images.add(image)
    if len(images) != 1:
        raise AdaptiveStartupBlocked("Live UPF images differ across slices")
    return {"mode": "kubeadm", "context": driver.context, "namespace": driver.namespace,
            "images": {"ue": driver.image, "init": driver.init_image,
                       "traffic": traffic_image, "upf": images.pop()}}


def _receipt_reader(root):
    directory = Path(root) if isinstance(root, str) else None
    if (directory is None or not directory.is_absolute()
            or not directory.is_dir() or directory.is_symlink()):
        raise AdaptiveStartupBlocked("Capacity receipt root is unavailable")
    resolved = directory.resolve()

    def read(ref):
        if (not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,256}", ref)
                or any(part in ("", ".", "..") for part in ref.split("/"))):
            raise AdaptiveStartupBlocked("Capacity receipt reference is unsafe")
        item = (resolved / ref).resolve()
        if not item.is_relative_to(resolved) or not item.is_file():
            raise AdaptiveStartupBlocked("Capacity receipt escapes its root")
        return _read(str(item), 16 * 1024 * 1024)

    return read


def _verify_ledger_receipts(manifest_bytes, read_receipt, round_ledger):
    """Bind every inspected file receipt to this server's completed round ledger."""
    if not callable(getattr(round_ledger, "completed_snapshot", None)):
        raise AdaptiveStartupBlocked("Completed round ledger reader is unavailable")
    try:
        manifest = json.loads(manifest_bytes)
        entries = manifest["receipts"]
        if not isinstance(entries, list) or not entries:
            raise ValueError("Missing capacity trials")
        for entry in entries:
            snapshots = {}
            for name in ("result", "cleanup"):
                descriptor = entry[name]
                raw = read_receipt(descriptor["ref"])
                if hashlib.sha256(raw).hexdigest() != descriptor["sha256"]:
                    raise ValueError("Capacity receipt changed after inspection")
                snapshots[name] = json.loads(raw)
            result, cleanup = snapshots["result"], snapshots["cleanup"]
            durable = round_ledger.completed_snapshot(result["roundId"])
            if (type(durable.get("sequence")) is not int
                    or durable["sequence"] != cleanup.get("sequence")
                    or cleanup.get("roundId") != durable.get("roundId")
                    or cleanup.get("generation") != durable.get("generation")
                    or cleanup.get("stage") != durable.get("stage")
                    or cleanup.get("cleanupStatus") != durable.get("cleanupStatus")
                    or not same_json(cleanup.get("releaseProof"), durable.get("releaseProof"))
                    or not same_json(cleanup.get("actions"), durable.get("actions"))):
                raise ValueError("Cleanup receipt differs from completed ledger round")
            frozen = ("roundId", "generation", "strategy", "allocation", "perUe",
                      "connectivityEvidence", "counts", "policyRevision", "measurements",
                      "observedAt", "executionOutcome", "errors")
            if any(not same_json(result.get(key), durable.get(key)) for key in frozen):
                raise ValueError("Capacity result differs from durable round")
            actions = result["actions"]
            durable_actions = durable["actions"]
            if (not isinstance(actions, list) or not isinstance(durable_actions, list)
                    or len(durable_actions) < len(actions)
                    or not same_json(actions, durable_actions[:len(actions)])
                    or any(action.get("kind") != "ue_stop" for action in durable_actions[len(actions):])):
                raise ValueError("Capacity actions differ from durable round")
    except (KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
        raise AdaptiveStartupBlocked("Capacity receipts do not match completed local rounds") from exc


def load_adaptive_startup(environ, driver, traffic_image, round_ledger=None):
    """Return grant, revalidation callback and runtime; no config means disabled."""
    filename = environ.get("CITYVERSE_ONPREM_AI100_CONFIG_FILE")
    expected = environ.get("CITYVERSE_ONPREM_AI100_CONFIG_SHA256")
    if filename is None and expected is None:
        return None, None, None
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise AdaptiveStartupBlocked("Pinned adaptive configuration digest is invalid")
    raw = _read(filename, 65536)
    if hashlib.sha256(raw).hexdigest() != expected:
        raise AdaptiveStartupBlocked("Adaptive configuration pin mismatch")
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise AdaptiveStartupBlocked("Duplicate adaptive configuration field")
            value[key] = item
        return value
    try:
        config = json.loads(raw, object_pairs_hook=unique,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (UnicodeError, ValueError):
        raise AdaptiveStartupBlocked("Adaptive configuration JSON is invalid") from None
    if not isinstance(config, dict):
        raise AdaptiveStartupBlocked("Adaptive configuration schema is invalid")
    trial = config.get("schemaVersion") == "cityverse.adaptive-ai100-trial/v1"
    if (trial and set(config) != {"schemaVersion", "runtimeSha256", "qer", "journalPath", "memoryPath"}) or (
            not trial and (config.get("schemaVersion") != "cityverse.adaptive-ai100-startup/v1"
                           or set(config) != {"schemaVersion", "capacity", "qer", "journalPath", "memoryPath"})):
        raise AdaptiveStartupBlocked("Adaptive configuration schema is invalid")
    qer = config["qer"]
    if (not isinstance(qer, dict)
            or set(qer) != {"smfUrl", "patchedImage", "smfService", "secretName", "profiles"}):
        raise AdaptiveStartupBlocked("Adaptive runtime configuration is incomplete")
    if not trial:
        capacity = config["capacity"]
        if not isinstance(capacity, dict) or set(capacity) != {"manifest", "manifestSha256", "receiptsRoot"}:
            raise AdaptiveStartupBlocked("Adaptive capacity configuration is incomplete")
        receipt_reader = _receipt_reader(capacity["receiptsRoot"])
    elif not isinstance(config["runtimeSha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", config["runtimeSha256"]):
        raise AdaptiveStartupBlocked("AI trial runtime pin is invalid")

    def verify_capacity():
        live = _runtime(driver, traffic_image)
        fingerprint = hashlib.sha256(json.dumps(live, sort_keys=True, separators=(",", ":"),
                                                allow_nan=False).encode()).hexdigest()
        if trial:
            if fingerprint != config["runtimeSha256"]:
                raise AdaptiveStartupBlocked("AI trial runtime changed")
            return UnverifiedAiTrial(fingerprint)
        manifest_bytes = _read(capacity["manifest"], 16 * 1024 * 1024)
        if (not isinstance(capacity["manifestSha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", capacity["manifestSha256"])
                or hashlib.sha256(manifest_bytes).hexdigest() != capacity["manifestSha256"]):
            raise AdaptiveStartupBlocked("Capacity manifest pin mismatch")
        grant = verify_capacity_promotion(
            manifest_bytes=manifest_bytes,
            read_receipt=receipt_reader,
            expected_runtime_fingerprint=fingerprint)
        if grant.verified_max_ues != 100:
            raise AdaptiveStartupBlocked("Capacity receipts are not for exactly 100 UEs")
        _verify_ledger_receipts(manifest_bytes, receipt_reader, round_ledger)
        return grant

    grant = verify_capacity()
    profiles = qer["profiles"]
    if (not isinstance(profiles, dict) or not 1 <= len(profiles) <= 3
            or any(not isinstance(key, str) or not key or not isinstance(rates, dict)
                   or set(rates) != {"ul", "dl"}
                   or any(type(value) is not int or not 1 <= value <= 500_000
                          for value in rates.values()) for key, rates in profiles.items())):
        raise AdaptiveStartupBlocked("Commissioned QER profiles are invalid")
    client = OpenAIClient(environ.get("OPENAI_API_KEY", ""),
                          environ.get("OPENAI_MODEL", ALLOWED_MODEL))
    try:
        upf_urls = json.loads(environ["CITYVERSE_ONPREM_UPF_EVIDENCE_URLS"]) if environ.get("CITYVERSE_ONPREM_UPF_EVIDENCE_URLS") else None
    except (TypeError, ValueError):
        raise AdaptiveStartupBlocked("Invalid fixed UPF evidence origins") from None
    provider = OnpremGroupQerProvider(driver, smf_url=qer["smfUrl"],
                                     patched_image=qer["patchedImage"],
                                     smf_service=qer["smfService"],
                                     secret_name=qer["secretName"], upf_urls=upf_urls)
    def budget(name, default, low, high):
        value = environ.get(name, str(default))
        if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or not low <= int(value) <= high:
            raise AdaptiveStartupBlocked("Invalid LangGraph runtime budget: " + name)
        return int(value)
    runtime = NativeAdaptiveRuntime(
        planner=None, qer_provider=provider, profiles=profiles,
        journal_path=_journal_path(config["journalPath"]),
        memory_path=_journal_path(config["memoryPath"]),
        fixture_config_sha256=expected,
        fixture_quarantine_registry=(Path(environ["CITYVERSE_ONPREM_POLICY_QUARANTINE_REGISTRY"])
            if environ.get("CITYVERSE_ONPREM_POLICY_QUARANTINE_REGISTRY") else None),
        maximum_cycles=budget("CITYVERSE_AI_MAX_CYCLES", 5, 1, 5),
        maximum_turns=budget("CITYVERSE_AI_MAX_TURNS", 8, 1, 30),
        maximum_tool_calls=budget("CITYVERSE_AI_MAX_TOOL_CALLS", 24, 1, 100),
        interval_seconds=budget("CITYVERSE_AI_INTERVAL_SECONDS", 1, 0, 60),
        maximum_policy_changes=budget("CITYVERSE_AI_MAX_POLICY_CHANGES", 3, 0, 1000),
        minimum_cycles_between_changes=budget("CITYVERSE_AI_CHANGE_COOLDOWN_CYCLES", 1, 1, 1000),
        role_models={role: ResponsesToolModel(client, model=environ.get("CITYVERSE_" + role.upper() + "_MODEL"))
                     for role in ("planner", "executor", "validator")})
    return grant, verify_capacity, runtime
