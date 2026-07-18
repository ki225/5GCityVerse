from __future__ import annotations

import json
import re
from typing import Any


class DataPlaneEvidence:
    """Normalize PFCP, gtp5g kernel, and measured-effect evidence.

    The actuator deliberately accepts only explicit observations. A successful
    northbound/SM-policy call is never promoted to user-plane success by itself.
    """

    CONFIRMED = "confirmed"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"

    _IDENTIFIER_PATTERN = r"0x[0-9a-f]+|\d+"

    @staticmethod
    def _canonical_identifier(value: Any) -> str | None:
        """Return numeric PFCP/gtp5g identifiers in one comparable form."""
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return str(value) if value >= 0 else None
        if not isinstance(value, str):
            return None
        candidate = value.strip().lower()
        if not re.fullmatch(DataPlaneEvidence._IDENTIFIER_PATTERN, candidate):
            return None
        return str(int(candidate, 16 if candidate.startswith("0x") else 10))

    @classmethod
    def _canonical_matches(cls, pattern: str, text: str) -> list[str]:
        values = (cls._canonical_identifier(value) for value in re.findall(pattern, text, re.IGNORECASE))
        return sorted({value for value in values if value is not None}, key=int)

    @staticmethod
    def _normalized_key(value: Any) -> str:
        return re.sub(r"[^a-z0-9]", "", str(value).lower())

    @classmethod
    def _json_identifiers(cls, value: Any) -> tuple[set[str], set[str], set[str]]:
        teids: set[str] = set()
        seids: set[str] = set()
        qer_ids: set[str] = set()

        def visit(item: Any) -> None:
            if isinstance(item, dict):
                for key, child in item.items():
                    normalized = cls._normalized_key(key)
                    identifier = cls._canonical_identifier(child)
                    if normalized == "teid" and identifier is not None:
                        teids.add(identifier)
                    elif normalized in {"seid", "fseid"} and identifier is not None:
                        seids.add(identifier)
                    elif normalized in {"qerid", "qeridentifier"} and identifier is not None:
                        qer_ids.add(identifier)
                    visit(child)
            elif isinstance(item, list):
                for child in item:
                    visit(child)

        visit(value)
        return teids, seids, qer_ids

    @classmethod
    def _json_section_counts(cls, value: Any) -> dict[str, int]:
        counts = {"pdr": 0, "far": 0, "qer": 0}

        def visit(item: Any, context: str | None = None, entity_root: bool = False) -> None:
            if isinstance(item, dict):
                explicit_sections = {
                    section
                    for section in counts
                    if any(cls._normalized_key(key) in {f"{section}id", f"{section}identifier"} for key in item)
                }
                if entity_root and context:
                    counts[context] += 1
                else:
                    for section in explicit_sections:
                        counts[section] += 1
                for key, child in item.items():
                    normalized = cls._normalized_key(key)
                    if normalized in {"pdr", "pdrs", "far", "fars", "qer", "qers"}:
                        section = normalized.rstrip("s")
                        if isinstance(child, list):
                            for entry in child:
                                visit(entry, section, True)
                        elif isinstance(child, dict):
                            visit(child, section, True)
                    elif not entity_root:
                        visit(child)
            elif isinstance(item, list):
                for child in item:
                    visit(child, context, entity_root)

        visit(value)
        return counts

    @staticmethod
    def parse_pfcp_log(text: str) -> dict[str, Any]:
        lowered = text.lower()
        requests = len(re.findall(r"session\s+modification\s+request", lowered))
        responses = len(re.findall(r"session\s+modification\s+response", lowered))
        value = DataPlaneEvidence._IDENTIFIER_PATTERN
        seids = DataPlaneEvidence._canonical_matches(rf"\b(?:seid|f-seid)\s*[:=]\s*({value})", lowered)
        teids = DataPlaneEvidence._canonical_matches(rf"\bteid\s*[:=]\s*({value})", lowered)
        qer_ids = DataPlaneEvidence._canonical_matches(rf"\bqer(?:id|\s+id)?\s*[:=]\s*({value})", lowered)
        return {
            "sessionModificationRequests": requests,
            "sessionModificationResponses": responses,
            "seids": seids,
            "teids": teids,
            "qerIds": qer_ids,
        }

    @staticmethod
    def parse_gtp5g_dump(text: str) -> dict[str, Any]:
        sections = {"pdr": 0, "far": 0, "qer": 0}
        current = ""
        teids: set[str] = set()
        seids: set[str] = set()
        qer_ids: set[str] = set()
        for line in text.splitlines():
            lowered = line.lower().strip()
            heading = re.fullmatch(r"(?:begin[_ -]?)?(pdr|far|qer)s?", lowered)
            if heading:
                current = heading.group(1)
                continue

            parsed_json: Any = None
            try:
                parsed_json = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                pass
            if parsed_json is not None:
                json_counts = DataPlaneEvidence._json_section_counts(parsed_json)
                for section, count in json_counts.items():
                    sections[section] += count
                if current and not any(json_counts.values()):
                    if isinstance(parsed_json, list):
                        sections[current] += sum(1 for entry in parsed_json if isinstance(entry, (dict, list)))
                    elif isinstance(parsed_json, dict):
                        sections[current] += 1
                json_teids, json_seids, json_qer_ids = DataPlaneEvidence._json_identifiers(parsed_json)
                teids.update(json_teids)
                seids.update(json_seids)
                qer_ids.update(json_qer_ids)
                continue

            if current and re.search(rf"\b{current}(?:-|\s+)?id\s*[:=]", lowered):
                sections[current] += 1
            value = DataPlaneEvidence._IDENTIFIER_PATTERN
            teids.update(DataPlaneEvidence._canonical_matches(rf"\bteid\s*[:=]\s*({value})", lowered))
            seids.update(DataPlaneEvidence._canonical_matches(rf"\b(?:seid|f-seid)\s*[:=]\s*({value})", lowered))
            qer_ids.update(DataPlaneEvidence._canonical_matches(rf"\bqer(?:id|\s+id|-id)?\s*[:=]\s*({value})", lowered))
        return {
            "pdrCount": sections["pdr"],
            "farCount": sections["far"],
            "qerCount": sections["qer"],
            "teids": sorted(teids, key=int),
            "seids": sorted(seids, key=int),
            "qerIds": sorted(qer_ids, key=int),
        }

    @classmethod
    def assess(
        cls,
        trusted_evidence: dict[str, Any] | None,
        before_metrics: dict[str, Any] | None = None,
        after_metrics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Only an independently read collector artifact may enter this method.
        # Lambda/tool payloads and application metrics are intentionally excluded.
        evidence = trusted_evidence if isinstance(trusted_evidence, dict) else {}
        pfcp = cls._pfcp_evidence(evidence.get("pfcp"))
        kernel = cls._kernel_evidence(evidence.get("kernel"))
        effect = cls._effect_evidence(evidence.get("effect"), before_metrics or {}, after_metrics or {})
        statuses = {pfcp["status"], kernel["status"], effect["status"]}
        confirmed = statuses == {cls.CONFIRMED}
        unsupported = cls.UNSUPPORTED in statuses or not confirmed
        return {
            "actuatorStatus": "confirmed" if confirmed else "unsupported" if unsupported else "failed",
            "pfcpEvidence": pfcp,
            "kernelEvidence": kernel,
            "effectEvidence": effect,
        }

    @classmethod
    def _pfcp_evidence(cls, value: Any) -> dict[str, Any]:
        item = value if isinstance(value, dict) else {}
        if item.get("supported") is False:
            return {"status": cls.UNSUPPORTED, "reason": "runtime reports PFCP QER modification unsupported"}
        request_count = int(item.get("sessionModificationRequests") or 0)
        response_count = int(item.get("sessionModificationResponses") or 0)
        seids = list(item.get("seids") or [])
        if request_count > 0 and response_count > 0 and seids:
            return {"status": cls.CONFIRMED, **item}
        return {"status": cls.UNAVAILABLE, "reason": "no matched PFCP Session Modification request/response with SEID"}

    @classmethod
    def _kernel_evidence(cls, value: Any) -> dict[str, Any]:
        item = value if isinstance(value, dict) else {}
        if item.get("supported") is False:
            return {"status": cls.UNSUPPORTED, "reason": "runtime reports gtp5g QER inspection/enforcement unsupported"}
        if all(int(item.get(key) or 0) > 0 for key in ("pdrCount", "farCount", "qerCount")) and item.get("teids"):
            return {"status": cls.CONFIRMED, **item}
        return {"status": cls.UNAVAILABLE, "reason": "no gtp5g PDR/FAR/QER and TEID snapshot"}

    @classmethod
    def _effect_evidence(cls, value: Any, before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
        item = value if isinstance(value, dict) else {}
        if item.get("supported") is False:
            return {"status": cls.UNSUPPORTED, "reason": "runtime reports measured QER effect unsupported"}
        before_mbps = item.get("beforeMbps")
        after_mbps = item.get("afterMbps")
        source = item.get("measurementSource")
        if item.get("expectedMbps") is not None and before_mbps is not None and after_mbps is not None and source == "ue-tun-iperf3":
            expected = float(item["expectedMbps"])
            actual = float(after_mbps)
            baseline = float(before_mbps)
            tolerance = max(0.5, expected * 0.15)
            minimum_delta = max(0.5, expected * 0.10)
            moved_toward_target = (
                (expected > baseline and actual - baseline >= minimum_delta)
                or (expected < baseline and baseline - actual >= minimum_delta)
            )
            if expected > 0 and abs(actual - expected) <= tolerance and moved_toward_target:
                return {
                    "status": cls.CONFIRMED,
                    "beforeMbps": baseline,
                    "afterMbps": actual,
                    "expectedMbps": expected,
                    "measurementSource": source,
                }
        return {"status": cls.UNAVAILABLE, "reason": "no before/after measured throughput matching the requested QER"}
