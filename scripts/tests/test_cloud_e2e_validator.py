from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


MODULE_PATH = Path(__file__).resolve().parents[1] / "cloud_e2e_validator.py"
SPEC = importlib.util.spec_from_file_location("cloud_e2e_validator", MODULE_PATH)
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = validator
SPEC.loader.exec_module(validator)


def test_combinations_profile_is_ai_only_and_covers_all_scenarios() -> None:
    cases = validator.build_cases("combinations")

    assert [case.name for case in cases] == [
        "combo-capacity-ai",
        "combo-critical-ai",
        "combo-all-five-ai",
    ]
    assert all(case.strategy == "ai" for case in cases)
    assert set(cases[-1].scenarios) == {(scenario, 50) for scenario in validator.SCENARIOS}


def test_unavailable_paths_reports_nested_markers() -> None:
    payload = {"metrics": {"source": "unavailable"}, "items": [{"status": "ok"}]}

    assert validator.unavailable_paths(payload) == ["$.metrics.source"]


def test_unavailable_paths_accepts_measured_payload() -> None:
    payload = {"metrics": {"source": "eks+iperf3"}, "scenarioTraffic": []}

    assert validator.unavailable_paths(payload) == []
