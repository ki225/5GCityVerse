from __future__ import annotations

from typing import Any

from app import CityVerseBackendApp
from config import AppSettings


def _build_app() -> CityVerseBackendApp:
    return CityVerseBackendApp(AppSettings())


def _metrics_for_scenario(scenario: str) -> dict[str, Any]:
    return {
        "activeScenarios": [scenario],
        "scenarioTraffic": [
            {
                "scenario": scenario,
                "throughputMbps": 10.0,
                "uplinkMbps": 5.0,
                "downlinkMbps": 5.0,
                "transport": "free5gc-tun",
            }
        ],
    }


def test_concert_scenario_ran_hop_uses_gnb1() -> None:
    """concert maps to the mall UE source (scenario_flow_metadata), which attaches to
    gnb1 per the frontend's STATIC_LINKS access topology. Regression test for the
    RAN-hop edge being hardcoded to "gnb2" regardless of the scenario's real source node.
    """
    backend = _build_app()

    edges = backend.scenario_edges_from_metrics(_metrics_for_scenario("concert"))

    ran_edge = next(edge for edge in edges if edge["id"].endswith("-ran"))
    assert ran_edge["sourceNodeId"] == "mall"
    assert ran_edge["targetNodeId"] == "gnb1"
    n3_edge = next(edge for edge in edges if edge["id"].endswith("-n3"))
    assert n3_edge["sourceNodeId"] == "gnb1"
    assert n3_edge["targetNodeId"] == "upf"


def test_iot_surge_scenario_ran_hop_uses_the_single_gnb() -> None:
    backend = _build_app()

    edges = backend.scenario_edges_from_metrics(_metrics_for_scenario("iot_surge"))

    ran_edge = next(edge for edge in edges if edge["id"].endswith("-ran"))
    assert ran_edge["sourceNodeId"] == "factory"
    assert ran_edge["targetNodeId"] == "gnb1"
    n3_edge = next(edge for edge in edges if edge["id"].endswith("-n3"))
    assert n3_edge["sourceNodeId"] == "gnb1"
    assert n3_edge["targetNodeId"] == "upf"


def test_active_batch_keeps_resident_citizen_baseline_tun_path_visible() -> None:
    backend = _build_app()
    metrics = _metrics_for_scenario("concert")
    metrics["scenarioTraffic"].append(
        {
            "scenario": "baseline",
            "throughputMbps": 1.0,
            "transport": "free5gc-tun",
        }
    )

    edges = backend.scenario_edges_from_metrics(metrics)

    assert any(edge["sourceNodeId"] == "residential" and edge["scenario"] == "baseline" for edge in edges)
    assert {edge["scenario"] for edge in edges} == {"concert", "baseline"}


def test_typhoon_and_medical_use_distinct_source_nodes() -> None:
    backend = _build_app()

    typhoon_edges = backend.scenario_edges_from_metrics(_metrics_for_scenario("typhoon"))
    medical_edges = backend.scenario_edges_from_metrics(_metrics_for_scenario("medical"))

    assert next(edge for edge in typhoon_edges if edge["id"].endswith("-ran"))["sourceNodeId"] == "disaster"
    assert next(edge for edge in medical_edges if edge["id"].endswith("-ran"))["sourceNodeId"] == "hospital"
