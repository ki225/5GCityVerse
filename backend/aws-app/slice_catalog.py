from __future__ import annotations

from typing import Any

from config import EVENT_CONFIG
from constants import DataSource, SliceType


class SliceCatalog:
    @staticmethod
    def default_slices() -> list[dict[str, Any]]:
        return [
            {"sst": 1, "type": SliceType.EMBB.value, "sd": "000001", "load": 20, "sessions": 120, "trend": "stable"},
            {"sst": 2, "type": SliceType.URLLC.value, "sd": "000002", "load": 10, "sessions": 34, "trend": "stable"},
            {"sst": 3, "type": SliceType.MMTC.value, "sd": "000004", "load": 15, "sessions": 2400, "trend": "stable"},
            {"sst": 4, "type": SliceType.V2X.value, "sd": "000005", "load": 5, "sessions": 18, "trend": "stable"},
        ]

    @staticmethod
    def event_slices(event_type: str) -> list[dict[str, Any]]:
        slices = SliceCatalog.default_slices()
        cfg = EVENT_CONFIG[event_type]
        for item in slices:
            if item["sst"] == cfg.slice_sst:
                item["sd"] = cfg.slice_sd
                item["load"] = min(item["load"] + 55, 100)
                item["sessions"] += cfg.ue_count
                item["trend"] = "up"
        return slices

    @staticmethod
    def slices_from_event_counts(event_counts: dict[str, int]) -> list[dict[str, Any]]:
        slices = SliceCatalog.default_slices()
        for item in slices:
            item["sessions"] = 0
            item["load"] = 5
            item["trend"] = "stable"
            for event_type, count in event_counts.items():
                cfg = EVENT_CONFIG[event_type]
                if cfg.slice_sst == item["sst"]:
                    item["sessions"] += count
                    item["load"] = min(20 + count * 20, 100)
                    item["trend"] = "up"
        return slices

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
                }
            )
        return slices


