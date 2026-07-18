from __future__ import annotations

from typing import Any

from constants import DataSource, EvidenceLevel, SliceType


class SliceCatalog:
    # NOTE: This project does not query NSSF's Nnssf_NSSelection for Allowed NSSAI,
    # so no slice here is ever tagged "allowed" — that level is honestly omitted
    # until NSSF integration is added.
    @staticmethod
    def default_slices() -> list[dict[str, Any]]:
        return [
            {"sst": 1, "type": SliceType.EMBB.value, "sd": "000001", "load": 0, "sessions": 0, "trend": "stable", "dataSource": DataSource.UNAVAILABLE.value, "selectionStage": "configured"},
            {"sst": 2, "type": SliceType.URLLC.value, "sd": "000002", "load": 0, "sessions": 0, "trend": "stable", "dataSource": DataSource.UNAVAILABLE.value, "selectionStage": "configured"},
            {"sst": 3, "type": SliceType.MMTC.value, "sd": "000004", "load": 0, "sessions": 0, "trend": "stable", "dataSource": DataSource.UNAVAILABLE.value, "selectionStage": "configured"},
            {"sst": 4, "type": SliceType.V2X.value, "sd": "000005", "load": 0, "sessions": 0, "trend": "stable", "dataSource": DataSource.UNAVAILABLE.value, "selectionStage": "configured"},
        ]

    @staticmethod
    def slices_from_registered_ues(registered_ues: list[dict[str, Any]]) -> list[dict[str, Any]]:
        slices = SliceCatalog.default_slices()
        for item in slices:
            item["dataSource"] = DataSource.FREE5GC.value
            item["loadSource"] = "estimated-from-registered-ues"
            item["evidenceLevel"] = EvidenceLevel.ESTIMATED.value
        by_sst = {item["sst"]: item for item in slices}
        for ue in registered_ues:
            for sst in SliceCatalog.ssts_from_registered_ue(ue):
                item = by_sst.get(sst)
                if not item:
                    continue
                item["sessions"] += 1
                item["trend"] = "up"
                item["dataSource"] = DataSource.FREE5GC.value
                item["selectionStage"] = "active-session"
        return slices

    @staticmethod
    def ssts_from_registered_ue(ue: dict[str, Any]) -> set[int]:
        ssts: set[int] = set()
        sessions = ue.get("PduSessions") or ue.get("pduSessions") or ue.get("pduSession") or []
        if isinstance(sessions, dict):
            sessions = sessions.values()
        if not isinstance(sessions, list):
            sessions = list(sessions) if sessions else []
        for session in sessions:
            if not isinstance(session, dict):
                continue
            snssai = session.get("SNssai") or session.get("Snssai") or session.get("snssai") or {}
            sst = session.get("Sst") or session.get("sst") or snssai.get("Sst") or snssai.get("sst")
            try:
                if sst is not None:
                    ssts.add(int(sst))
            except (TypeError, ValueError):
                continue
        return ssts

    @staticmethod
    def slices_from_prometheus(raw: dict[int, float | None], sessions: dict[int, int]) -> list[dict[str, Any]] | None:
        if all(value is None for value in raw.values()):
            return None
        labels = {
            1: (SliceType.EMBB.value, "000001"),
            2: (SliceType.URLLC.value, "000002"),
            3: (SliceType.MMTC.value, "000004"),
            4: (SliceType.V2X.value, "000005"),
        }
        total = sum(value or 0.0 for value in raw.values()) or 1.0
        slices = []
        for sst, value in raw.items():
            value = value or 0.0
            slice_type, sd = labels[sst]
            load = round(value / total * 100)
            slices.append(
                {
                    "sst": sst,
                    "type": slice_type,
                    "sd": sd,
                    "load": min(max(load, 0), 100),
                    "sessions": sessions.get(sst, 0),
                    "trend": "up" if value > 0 else "stable",
                    "throughputMbps": round(value * 8 / 1_000_000, 2),
                    "dataSource": DataSource.PROMETHEUS.value,
                    "loadSource": "prometheus",
                    "evidenceLevel": EvidenceLevel.MEASURED.value,
                    "selectionStage": "active-session" if value > 0 else "configured",
                }
            )
        return slices
