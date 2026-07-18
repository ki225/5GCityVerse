#!/usr/bin/env python3
"""Scan the repo for scenario/event definitions scattered across backend, k8s,
and frontend sources, and report fields that disagree with backend/aws-app/config.py.

Usage:
    python scripts/check-scenario-consistency.py [--include-legacy]

Exit code is non-zero if any INCONSISTENCY is found (MISSING fields do not
affect the exit code).

Stdlib only. Parses source files as text/AST/regex -- it never imports the
modules it inspects (several of them pull in AWS/pydantic dependencies that
are not necessarily installed wherever this script runs).
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent

CONFIG_PY = REPO_ROOT / "backend" / "aws-app" / "config.py"
SCALE_MODEL_PY = REPO_ROOT / "backend" / "aws-app" / "scale_model.py"
SCENARIO_VALIDATION_PY = REPO_ROOT / "backend" / "aws-app" / "agent_runtime" / "scenario_validation.py"
EVENT_ENGINE_PY = REPO_ROOT / "backend" / "event-engine" / "index.py"
IPERF3_JOBS_DIR = REPO_ROOT / "k8s" / "iperf3-jobs"
UE_CONFIG_DIR = REPO_ROOT / "k8s" / "ue-config"
FRONTEND_TYPES_TS = REPO_ROOT / "frontend" / "src" / "types" / "index.ts"

# Fields compared across sources, keyed on the EventConfig field name used in config.py.
COMPARED_FIELDS = ["slice_sst", "slice_sd", "slice_type", "ue_count", "dnn", "five_qi", "traffic_profile"]

FIELD_LABELS = {
    "slice_sst": "SST",
    "slice_sd": "SD",
    "slice_type": "slice_type",
    "ue_count": "ue_count",
    "dnn": "DNN",
    "five_qi": "5QI",
    "traffic_profile": "traffic_profile",
}


class Value:
    """A field value plus where it came from, for reporting."""

    __slots__ = ("value", "location")

    def __init__(self, value: Any, location: str):
        self.value = value
        self.location = location

    def __repr__(self) -> str:
        return f"{self.value!r}@{self.location}"


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# Source 1: backend/aws-app/config.py -- EVENT_CONFIG (the reference/primary source)
# ---------------------------------------------------------------------------

def parse_config_py(path: Path) -> dict[str, dict[str, Value]]:
    """Parse EVENT_CONFIG = { EventType.X.value: EventConfig(...), ... }."""
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))

    result: dict[str, dict[str, Value]] = {}

    for node in ast.walk(tree):
        target_name = _assign_target_name(node)
        if target_name != "EVENT_CONFIG":
            continue
        dict_node = node.value
        if not isinstance(dict_node, ast.Dict):
            continue

        for key_node, val_node in zip(dict_node.keys, dict_node.values):
            event_name = _event_key_to_name(key_node)
            if event_name is None or not isinstance(val_node, ast.Call):
                continue
            fields: dict[str, Value] = {}
            for kw in val_node.keywords:
                if kw.arg not in COMPARED_FIELDS:
                    continue
                literal = _literal(kw.value)
                if literal is None and kw.arg == "slice_type":
                    literal = _slice_type_name(kw.value)
                line = kw.value.lineno
                fields[kw.arg] = Value(literal, f"{rel(path)}:{line}")
            result[event_name] = fields

    return result


def _assign_target_name(node: ast.AST) -> str | None:
    """Return the assigned name for `NAME = ...` or `NAME: Type = ...` statements."""
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                return t.id
    elif isinstance(node, ast.AnnAssign):
        if isinstance(node.target, ast.Name) and node.value is not None:
            return node.target.id
    return None


def _event_key_to_name(node: ast.AST) -> str | None:
    """EventType.CONCERT.value -> 'concert'."""
    if isinstance(node, ast.Attribute) and node.attr == "value":
        base = node.value
        if isinstance(base, ast.Attribute):
            return base.attr.lower()
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _slice_type_name(node: ast.AST) -> str | None:
    """SliceType.EMBB -> 'EMBB' (kept as the enum member name; resolved later)."""
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _literal(node: ast.AST) -> Any:
    try:
        return ast.literal_eval(node)
    except Exception:
        return None


# SliceType enum member name -> string value, taken from backend/aws-app/constants.py.
SLICE_TYPE_ENUM = {"EMBB": "eMBB", "URLLC": "URLLC", "MMTC": "mMTC", "V2X": "V2X"}


def resolve_slice_type(raw: Any) -> Any:
    if isinstance(raw, str) and raw in SLICE_TYPE_ENUM:
        return SLICE_TYPE_ENUM[raw]
    return raw


# ---------------------------------------------------------------------------
# Source 2: backend/aws-app/agent_runtime/scenario_validation.py -- SCENARIO_SPECS
# ---------------------------------------------------------------------------

def parse_scenario_validation(path: Path) -> dict[str, dict[str, Value]]:
    """SCENARIO_SPECS entries carry no slice_sst/sd/five_qi/dnn/ue_count/traffic_profile
    fields (only phase/name/steps/nefApis/improvements) -- so every compared field is
    reported as MISSING for this source. We still parse it (rather than hardcoding an
    empty dict) so a future edit that adds one of those fields is picked up automatically.
    """
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))

    result: dict[str, dict[str, Value]] = {}

    for node in ast.walk(tree):
        if _assign_target_name(node) != "SCENARIO_SPECS":
            continue
        dict_node = node.value
        if not isinstance(dict_node, ast.Dict):
            continue

        for key_node, val_node in zip(dict_node.keys, dict_node.values):
            if not (isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)):
                continue
            event_name = key_node.value
            fields: dict[str, Value] = {}
            if isinstance(val_node, ast.Dict):
                for spec_key_node, spec_val_node in zip(val_node.keys, val_node.values):
                    spec_key = spec_key_node.value if isinstance(spec_key_node, ast.Constant) else None
                    # Map JSON-ish key names to our compared field names, if present.
                    mapped = {
                        "sst": "slice_sst",
                        "slice_sst": "slice_sst",
                        "sd": "slice_sd",
                        "slice_sd": "slice_sd",
                        "sliceType": "slice_type",
                        "slice_type": "slice_type",
                        "ueCount": "ue_count",
                        "ue_count": "ue_count",
                        "dnn": "dnn",
                        "fiveQi": "five_qi",
                        "five_qi": "five_qi",
                        "trafficProfile": "traffic_profile",
                        "traffic_profile": "traffic_profile",
                    }.get(spec_key)
                    if mapped:
                        fields[mapped] = Value(_literal(spec_val_node), f"{rel(path)}:{spec_val_node.lineno}")
            result[event_name] = fields

    return result


# ---------------------------------------------------------------------------
# Source 3a: k8s/iperf3-jobs/*.yaml -- bandwidth / parallel streams from the iperf3 command
# ---------------------------------------------------------------------------

IPERF3_FILE_TO_EVENT = {
    "accident": "accident",
    "concert": "concert",
    "iot-surge": "iot_surge",
    "medical": "medical",
    "typhoon": "typhoon",
}


def parse_iperf3_jobs(directory: Path) -> dict[str, dict[str, Value]]:
    result: dict[str, dict[str, Value]] = {}
    if not directory.is_dir():
        return result

    for path in sorted(directory.glob("*.yaml")):
        event_name = IPERF3_FILE_TO_EVENT.get(path.stem)
        if event_name is None:
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        fields: dict[str, Value] = {}
        for idx, line in enumerate(lines, start=1):
            if "command:" not in line:
                continue
            bw_match = re.search(r'"-b",\s*"([^"]+)"', line)
            parallel_match = re.search(r'"-P",\s*"([^"]+)"', line)
            if bw_match:
                profile = f"iperf3 UDP {bw_match.group(1)}"
                if parallel_match:
                    profile += f" x {parallel_match.group(1)} parallel streams"
                fields["traffic_profile"] = Value(profile, f"{rel(path)}:{idx}")
            break
        result[event_name] = fields

    return result


# ---------------------------------------------------------------------------
# Source: backend/aws-app/scale_model.py -- the E13/R2-3 scale-model table
# (event_scale -> ue_count -> iperf3 bandwidth/parallel streams -> expected slice load).
# Compared directly against config.py's EVENT_CONFIG/IPERF3_ARGS and against the raw
# bandwidth/-P values in k8s/iperf3-jobs/*.yaml.
# ---------------------------------------------------------------------------

SCALE_MODEL_FIELDS = ["ue_count", "bandwidth", "parallel_streams", "packet_length"]
RUNTIME_DURATION_SECONDS = 180


def parse_scale_model_py(path: Path) -> dict[str, dict[str, Value]]:
    """Parse the _SCALE_PROFILES = { EventType.X.value: ScaleProfile(...), ... } table."""
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))

    result: dict[str, dict[str, Value]] = {}

    for node in ast.walk(tree):
        if _assign_target_name(node) != "_SCALE_PROFILES":
            continue
        dict_node = node.value
        if not isinstance(dict_node, ast.Dict):
            continue

        for key_node, val_node in zip(dict_node.keys, dict_node.values):
            event_name = _event_key_to_name(key_node)
            if event_name is None or not isinstance(val_node, ast.Call):
                continue
            fields: dict[str, Value] = {}
            for kw in val_node.keywords:
                if kw.arg not in SCALE_MODEL_FIELDS:
                    continue
                literal = _literal(kw.value)
                fields[kw.arg] = Value(literal, f"{rel(path)}:{kw.value.lineno}")
            result[event_name] = fields

    return result


def parse_iperf3_jobs_raw(directory: Path) -> dict[str, dict[str, Value]]:
    """Extract raw -b (bandwidth) and -P (parallel_streams, default 1) values from each
    k8s/iperf3-jobs/*.yaml Job's iperf3 command -- kept separate from parse_iperf3_jobs()
    (which builds a composed traffic_profile string) so scale_model's numeric fields can
    be compared field-by-field instead of via the loose free-text comparison used for
    traffic_profile."""
    result: dict[str, dict[str, Value]] = {}
    if not directory.is_dir():
        return result

    for path in sorted(directory.glob("*.yaml")):
        event_name = IPERF3_FILE_TO_EVENT.get(path.stem)
        if event_name is None:
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        fields: dict[str, Value] = {}
        for idx, line in enumerate(lines, start=1):
            if "command:" not in line:
                continue
            bw_match = re.search(r'"-b",\s*"([^"]+)"', line)
            parallel_match = re.search(r'"-P",\s*"([^"]+)"', line)
            duration_match = re.search(r'"-t",\s*"([^"]+)"', line)
            packet_length_match = re.search(r'"-l",\s*"([^"]+)"', line)
            if bw_match:
                fields["bandwidth"] = Value(bw_match.group(1), f"{rel(path)}:{idx}")
            fields["parallel_streams"] = Value(
                int(parallel_match.group(1)) if parallel_match else 1, f"{rel(path)}:{idx}"
            )
            if duration_match:
                fields["duration_seconds"] = Value(int(duration_match.group(1)), f"{rel(path)}:{idx}")
            if packet_length_match:
                fields["packet_length"] = Value(int(packet_length_match.group(1)), f"{rel(path)}:{idx}")
            break
        result[event_name] = fields

    return result


def config_py_scale_fields(config_data: dict[str, dict[str, Value]], config_path: Path) -> dict[str, dict[str, Value]]:
    """Re-derive ue_count/bandwidth/parallel_streams per event straight from config.py's
    already-parsed EVENT_CONFIG (ue_count, traffic_profile), for comparison against
    scale_model.py. bandwidth/parallel_streams are pulled out of the traffic_profile text
    the same way scale_model.py's own docstring says it derives them."""
    result: dict[str, dict[str, Value]] = {}
    for event_name, fields in config_data.items():
        derived: dict[str, Value] = {}
        if "ue_count" in fields:
            derived["ue_count"] = fields["ue_count"]
        profile_value = fields.get("traffic_profile")
        if profile_value is not None and isinstance(profile_value.value, str):
            bw_match = re.search(r"(\d+(?:\.\d+)?[KMG])", profile_value.value)
            if bw_match:
                derived["bandwidth"] = Value(bw_match.group(1), profile_value.location)
            parallel_match = re.search(r"x (\d+) parallel streams", profile_value.value)
            derived["parallel_streams"] = Value(
                int(parallel_match.group(1)) if parallel_match else 1, profile_value.location
            )
            packet_length_match = re.search(r"(\d+)\s*-?byte", profile_value.value, re.IGNORECASE)
            if packet_length_match:
                derived["packet_length"] = Value(int(packet_length_match.group(1)), profile_value.location)
            elif parallel_match:
                derived["packet_length"] = Value(64, profile_value.location)
        result[event_name] = derived
    return result


def compare_scale_model(
    scale_model_data: dict[str, dict[str, Value]],
    config_scale_data: dict[str, dict[str, Value]],
    iperf3_raw_data: dict[str, dict[str, Value]],
) -> list[Finding]:
    """scale_model.py vs config.py (derived) vs k8s/iperf3-jobs/*.yaml (raw), field by
    field, for ue_count/bandwidth/parallel_streams. Unlike compare()'s traffic_profile
    handling, this does exact comparison since all three sources express these fields as
    plain numbers/short tokens (no free-text phrasing differences to tolerate)."""
    findings: list[Finding] = []

    for event_name, model_fields in scale_model_data.items():
        for field in SCALE_MODEL_FIELDS:
            model_value = model_fields.get(field)
            if model_value is None:
                continue

            values_for_field: dict[str, Value] = {"scale_model.py": model_value}
            has_inconsistency = False
            has_present_other = False

            for source_name, source_data in (
                ("config.py (derived)", config_scale_data),
                ("k8s/iperf3-jobs/*.yaml", iperf3_raw_data),
            ):
                # A Job manifest describes one aggregate generator and has no
                # UE population field; absence there is not schema drift.
                if field == "ue_count" and source_name == "k8s/iperf3-jobs/*.yaml":
                    continue
                source_fields = source_data.get(event_name)
                if source_fields is None:
                    continue
                other_value = source_fields.get(field)
                if other_value is None:
                    values_for_field[source_name] = None
                    continue
                has_present_other = True
                if other_value.value != model_value.value:
                    has_inconsistency = True
                values_for_field[source_name] = other_value

            if not has_present_other:
                continue
            if has_inconsistency:
                findings.append(Finding(event_name, field, "INCONSISTENCY", values_for_field))
            else:
                missing_sources = {k: v for k, v in values_for_field.items() if v is None}
                if missing_sources:
                    findings.append(Finding(event_name, field, "MISSING", values_for_field))

    return findings


# ---------------------------------------------------------------------------
# Source 3b: k8s/ue-config/*.yaml -- SST/SD/DNN from default-nssai + apn
# ---------------------------------------------------------------------------

# Maps the ConfigMap name (as referenced by config.py's UE_CONFIG_MAPS) to the
# k8s/ue-config/*.yaml filename that defines it.
UE_CONFIG_MAP_NAME_TO_FILE = {
    "ueransim-ue-config-embb": "embb.yaml",
    "ueransim-ue-config-urllc": "urllc.yaml",
    "ueransim-ue-config-typhoon": "typhoon.yaml",
    "ueransim-ue-config-mmtc": "mmtc.yaml",
    "ueransim-ue-config-v2x": "v2x.yaml",
}


def parse_ue_config_maps(path: Path) -> dict[str, str]:
    """Parse UE_CONFIG_MAPS = { EventType.X.value: "ueransim-ue-config-..." }."""
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))

    mapping: dict[str, str] = {}
    for node in ast.walk(tree):
        if _assign_target_name(node) != "UE_CONFIG_MAPS":
            continue
        dict_node = node.value
        if not isinstance(dict_node, ast.Dict):
            continue
        for key_node, val_node in zip(dict_node.keys, dict_node.values):
            event_name = _event_key_to_name(key_node)
            configmap_name = _literal(val_node)
            if event_name and isinstance(configmap_name, str):
                mapping[event_name] = configmap_name
    return mapping


def parse_ue_config_yaml(path: Path) -> dict[str, Value]:
    """Extract sst/sd/apn(dnn) from the default-nssai block and the sessions apn."""
    lines = path.read_text(encoding="utf-8").splitlines()
    fields: dict[str, Value] = {}

    in_default_nssai = False
    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()

        if stripped.startswith("apn:") and "dnn" not in fields:
            m = re.search(r'apn:\s*"?([^"\s]+)"?', stripped)
            if m:
                fields["dnn"] = Value(m.group(1), f"{rel(path)}:{idx}")

        if stripped.startswith("default-nssai:"):
            in_default_nssai = True
            continue
        if in_default_nssai:
            # Entries look like:
            #   default-nssai:
            #     - sst: 1
            #       sd: 000001
            # i.e. `sst:` may be preceded by a YAML list-item dash.
            unwrapped = stripped[1:].strip() if stripped.startswith("-") else stripped
            if unwrapped.startswith("sst:"):
                m = re.search(r"sst:\s*(0x[0-9a-fA-F]+|\d+)", unwrapped)
                if m:
                    raw = m.group(1)
                    sst_val = int(raw, 16) if raw.lower().startswith("0x") else int(raw)
                    fields["slice_sst"] = Value(sst_val, f"{rel(path)}:{idx}")
            elif unwrapped.startswith("sd:"):
                m = re.search(r'sd:\s*"?(0x[0-9a-fA-F]+|[0-9]+)"?', unwrapped)
                if m:
                    raw = m.group(1)
                    if raw.lower().startswith("0x"):
                        sd_val = f"{int(raw, 16):06d}"
                    else:
                        # default-nssai sd is written as a bare/quoted decimal-looking
                        # string, e.g. `000001` or `"000004"` -- keep as zero-padded string.
                        sd_val = raw.zfill(6)
                    fields["slice_sd"] = Value(sd_val, f"{rel(path)}:{idx}")
            elif unwrapped and not unwrapped.startswith(("sst:", "sd:")):
                in_default_nssai = False

    return fields


def parse_ue_configs(directory: Path, event_to_configmap: dict[str, str]) -> dict[str, dict[str, Value]]:
    result: dict[str, dict[str, Value]] = {}
    if not directory.is_dir():
        return result

    for event_name, configmap_name in event_to_configmap.items():
        filename = UE_CONFIG_MAP_NAME_TO_FILE.get(configmap_name)
        if not filename:
            continue
        path = directory / filename
        if not path.is_file():
            continue
        result[event_name] = parse_ue_config_yaml(path)

    return result


def merge_k8s_sources(
    iperf3_fields: dict[str, dict[str, Value]], ue_config_fields: dict[str, dict[str, Value]]
) -> dict[str, dict[str, Value]]:
    merged: dict[str, dict[str, Value]] = {}
    for event_name in set(iperf3_fields) | set(ue_config_fields):
        combined: dict[str, Value] = {}
        combined.update(ue_config_fields.get(event_name, {}))
        combined.update(iperf3_fields.get(event_name, {}))
        merged[event_name] = combined
    return merged


# ---------------------------------------------------------------------------
# Source 4: frontend/src/types/index.ts -- CityEventType and SliceType enums
# ---------------------------------------------------------------------------

def parse_frontend_types(path: Path) -> tuple[list[str], list[str], int, int]:
    """Return (event values, slice type values, event_line, slice_line)."""
    lines = path.read_text(encoding="utf-8").splitlines()

    events: list[str] = []
    event_line = 0
    slices: list[str] = []
    slice_line = 0

    idx = 0
    while idx < len(lines):
        stripped = lines[idx].strip()
        if stripped.startswith("export type CityEventType"):
            event_line = idx + 1
            # The union is either on this line or spread across following
            # `| 'value'` lines until a blank line / non-union line.
            events.extend(re.findall(r"'([^']+)'", lines[idx]))
            j = idx + 1
            while j < len(lines) and lines[j].strip().startswith("|"):
                events.extend(re.findall(r"'([^']+)'", lines[j]))
                j += 1
            idx = j
            continue
        if stripped.startswith("export type SliceType"):
            slice_line = idx + 1
            slices.extend(re.findall(r"'([^']+)'", lines[idx]))
            j = idx + 1
            while j < len(lines) and lines[j].strip().startswith("|"):
                slices.extend(re.findall(r"'([^']+)'", lines[j]))
                j += 1
            idx = j
            continue
        idx += 1

    return events, slices, event_line, slice_line


# ---------------------------------------------------------------------------
# Source 5 (--include-legacy only): backend/event-engine/index.py -- EVENT_CONFIG
# ---------------------------------------------------------------------------

def parse_event_engine(path: Path) -> dict[str, dict[str, Value]]:
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))

    result: dict[str, dict[str, Value]] = {}

    for node in ast.walk(tree):
        if _assign_target_name(node) != "EVENT_CONFIG":
            continue
        dict_node = node.value
        if not isinstance(dict_node, ast.Dict):
            continue

        for key_node, val_node in zip(dict_node.keys, dict_node.values):
            if not (isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)):
                continue
            event_name = key_node.value
            if not isinstance(val_node, ast.Dict):
                continue
            fields: dict[str, Value] = {}
            mapped_keys = {
                "slice_sst": "slice_sst",
                "slice_type": "slice_type",
                "ue_count": "ue_count",
            }
            for k_node, v_node in zip(val_node.keys, val_node.values):
                if not (isinstance(k_node, ast.Constant) and isinstance(k_node.value, str)):
                    continue
                mapped = mapped_keys.get(k_node.value)
                if mapped:
                    fields[mapped] = Value(_literal(v_node), f"{rel(path)}:{v_node.lineno}")
            result[event_name] = fields

    return result


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

class Finding:
    def __init__(self, event: str, field: str, kind: str, values: dict[str, Value]):
        self.event = event
        self.field = field
        self.kind = kind  # "INCONSISTENCY" or "MISSING"
        self.values = values

    def format(self) -> str:
        label = FIELD_LABELS.get(self.field, self.field)
        parts = []
        for source_name, v in self.values.items():
            if v is None:
                parts.append(f"{source_name}=MISSING")
            elif v.value is None:
                parts.append(f"{source_name}=MISSING ({v.location})")
            else:
                parts.append(f"{source_name}={v.value!r} ({v.location})")
        return f"[{self.kind}] event={self.event} field={label}: " + "; ".join(parts)


def compare(
    primary: dict[str, dict[str, Value]],
    other_sources: list[tuple[str, dict[str, dict[str, Value]]]],
) -> list[Finding]:
    findings: list[Finding] = []

    for event_name, primary_fields in primary.items():
        for field in COMPARED_FIELDS:
            primary_value = primary_fields.get(field)
            if primary_value is None:
                continue  # nothing to compare against if config.py itself lacks it

            normalized_primary = resolve_slice_type(primary_value.value) if field == "slice_type" else primary_value.value

            values_for_field: dict[str, Value] = {"config.py": primary_value}
            has_inconsistency = False
            has_present_other = False

            for source_name, source_data in other_sources:
                source_fields = source_data.get(event_name)
                if source_fields is None:
                    continue  # source has no knowledge of this event at all -> skip silently
                other_value = source_fields.get(field)
                if other_value is None:
                    values_for_field[source_name] = None
                    continue
                has_present_other = True
                normalized_other = resolve_slice_type(other_value.value) if field == "slice_type" else other_value.value

                if field == "traffic_profile":
                    # Free-text profiles: compare embedded bandwidth numbers loosely
                    # instead of exact string equality (different sources phrase this
                    # differently but should agree on the bandwidth figure).
                    if not _traffic_profiles_agree(str(normalized_primary), str(normalized_other)):
                        has_inconsistency = True
                elif normalized_other != normalized_primary:
                    has_inconsistency = True

                values_for_field[source_name] = other_value

            if not has_present_other:
                continue  # every source is either silent on this event or missing the field
            if has_inconsistency:
                findings.append(Finding(event_name, field, "INCONSISTENCY", values_for_field))
            else:
                missing_sources = {k: v for k, v in values_for_field.items() if v is None}
                if missing_sources:
                    findings.append(Finding(event_name, field, "MISSING", values_for_field))

    return findings


def _traffic_profiles_agree(a: str, b: str) -> bool:
    def bandwidth_tokens(s: str) -> set[str]:
        return set(re.findall(r"\d+(?:\.\d+)?[MK]", s))

    ta, tb = bandwidth_tokens(a), bandwidth_tokens(b)
    if ta and tb and not (ta & tb):
        return False

    def parallel_stream_count(s: str) -> str | None:
        m = re.search(r"x (\d+) parallel streams", s)
        return m.group(1) if m else None

    pa, pb = parallel_stream_count(a), parallel_stream_count(b)
    if pa is not None and pb is not None and pa != pb:
        return False

    return True


def compare_frontend(
    primary: dict[str, dict[str, Value]],
    frontend_events: list[str],
    frontend_slices: list[str],
    frontend_path: Path,
    event_line: int,
    slice_line: int,
) -> list[Finding]:
    findings: list[Finding] = []
    frontend_event_loc = f"{rel(frontend_path)}:{event_line}" if event_line else f"{rel(frontend_path)}"
    frontend_slice_loc = f"{rel(frontend_path)}:{slice_line}" if slice_line else f"{rel(frontend_path)}"

    for event_name, primary_fields in primary.items():
        # Event existence check
        if event_name not in frontend_events:
            findings.append(
                Finding(
                    event_name,
                    "event_declared",
                    "INCONSISTENCY",
                    {
                        "config.py": Value(event_name, "backend/aws-app/config.py"),
                        "frontend/src/types/index.ts": Value(None, frontend_event_loc),
                    },
                )
            )

        # slice_type membership check
        slice_type_value = primary_fields.get("slice_type")
        if slice_type_value is not None:
            resolved = resolve_slice_type(slice_type_value.value)
            if resolved not in frontend_slices:
                findings.append(
                    Finding(
                        event_name,
                        "slice_type",
                        "INCONSISTENCY",
                        {
                            "config.py": Value(resolved, slice_type_value.location),
                            "frontend/src/types/index.ts (SliceType enum)": Value(
                                frontend_slices, frontend_slice_loc
                            ),
                        },
                    )
                )

    return findings


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--include-legacy",
        action="store_true",
        help="Also compare against backend/event-engine/index.py's legacy EVENT_CONFIG.",
    )
    args = parser.parse_args()

    config_data = parse_config_py(CONFIG_PY)
    ue_config_map = parse_ue_config_maps(CONFIG_PY)
    iperf3_data = parse_iperf3_jobs(IPERF3_JOBS_DIR)
    ue_config_data = parse_ue_configs(UE_CONFIG_DIR, ue_config_map)
    k8s_data = merge_k8s_sources(iperf3_data, ue_config_data)
    # Free-text traffic_profile descriptions are not a shared schema. Exact
    # bandwidth/parallel/packet/duration checks are performed below instead.
    for fields in k8s_data.values():
        fields.pop("traffic_profile", None)
    frontend_events, frontend_slices, event_line, slice_line = parse_frontend_types(FRONTEND_TYPES_TS)

    other_sources: list[tuple[str, dict[str, dict[str, Value]]]] = [
        ("k8s/*.yaml", k8s_data),
    ]

    if args.include_legacy:
        if EVENT_ENGINE_PY.is_file():
            event_engine_data = parse_event_engine(EVENT_ENGINE_PY)
            other_sources.append(("backend/event-engine/index.py", event_engine_data))
        else:
            print("legacy source removed 2026-07-05")
            print()

    findings = compare(config_data, other_sources)
    findings.extend(
        compare_frontend(config_data, frontend_events, frontend_slices, FRONTEND_TYPES_TS, event_line, slice_line)
    )

    scale_model_data = parse_scale_model_py(SCALE_MODEL_PY)
    if scale_model_data:
        config_scale_data = config_py_scale_fields(config_data, CONFIG_PY)
        iperf3_raw_data = parse_iperf3_jobs_raw(IPERF3_JOBS_DIR)
        findings.extend(compare_scale_model(scale_model_data, config_scale_data, iperf3_raw_data))
        for event_name, fields in iperf3_raw_data.items():
            duration = fields.get("duration_seconds")
            if duration is not None and duration.value != RUNTIME_DURATION_SECONDS:
                findings.append(Finding(event_name, "duration_seconds", "INCONSISTENCY", {
                    "scenario_environment.py": Value(RUNTIME_DURATION_SECONDS, "backend/aws-app/scenario_environment.py"),
                    "k8s/iperf3-jobs/*.yaml": duration,
                }))

    inconsistencies = [f for f in findings if f.kind == "INCONSISTENCY"]
    missing = [f for f in findings if f.kind == "MISSING"]

    print(f"Scenario consistency check ({'with' if args.include_legacy else 'without'} legacy event-engine)")
    print(f"Repo root: {REPO_ROOT}")
    print()

    if not findings:
        print("No findings. All sources agree on every comparable field.")
        return 0

    if inconsistencies:
        print(f"INCONSISTENCIES ({len(inconsistencies)}):")
        for f in inconsistencies:
            print("  " + f.format())
        print()

    if missing:
        print(f"MISSING ({len(missing)}):")
        for f in missing:
            print("  " + f.format())
        print()

    print(f"Summary: {len(inconsistencies)} inconsistencies, {len(missing)} missing fields.")

    return 1 if inconsistencies else 0


if __name__ == "__main__":
    sys.exit(main())
