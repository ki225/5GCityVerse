from __future__ import annotations

from typing import Any

from app import CityVerseBackendApp
from config import AppSettings
from scenario_environment import ScenarioEnvironmentService


def _pod(name: str, scenario: str | None, container: str, started_at: str = "2026-07-04T00:00:00Z") -> dict[str, Any]:
    labels: dict[str, str] = {"app.kubernetes.io/component": "iperf3"}
    if scenario is not None:
        labels["5gcityverse.io/scenario"] = scenario
    return {
        "metadata": {"name": name, "labels": labels},
        "status": {"phase": "Running", "startTime": started_at},
        "spec": {"containers": [{"name": container}]},
    }


# The baseline server has no per-scenario label (ensure_iperf3_server sets only
# {"app": "iperf3-server"}), and only prints interval lines for a connection when
# the connecting client requested periodic reporting via -i.
BASELINE_SERVER_LOG_STREAMING = "\n".join(
    f"[  5]   {i}.00-{i + 1}.00 sec  1.20 MBytes  10.0 Mbits/sec  0.500 ms  0/845 (0%)" for i in range(15)
)

# Before the fix: iperf3 -c ... --json -t 3600 with no -i buffers all output as a
# single JSON document emitted only when the 3600s test completes, so a log
# fetched mid-run (tailLines/sinceSeconds window) is empty.
BASELINE_CLIENT_LOG_JSON_MIDRUN = ""

# The medical event's client (recreate_iperf3_job) streams "SCENARIO=medical"
# followed by -i 1 --forceflush interval lines every second.
MEDICAL_CLIENT_LOG = "SCENARIO=medical\n" + "\n".join(
    f"[  5]   {i}.00-{i + 1}.00 sec  1.20 MBytes  50.0 Mbits/sec  0.300 ms  0/845 (0%)" for i in range(15)
)


class _FakeK8s:
    def __init__(self, logs: dict[str, str]) -> None:
        self._logs = logs

    def request_text(self, method: str, path: str, ignore_404: bool = False) -> tuple[int, str]:
        for pod_name, log_text in self._logs.items():
            if pod_name in path:
                return 200, log_text
        return 200, ""


def _pods_for_baseline_and_medical(baseline_client_container: str = "iperf3-client") -> list[dict[str, Any]]:
    pods = [
        _pod("iperf3-baseline-abc123", "baseline", baseline_client_container),
        _pod("iperf3-server-xyz", None, "iperf3"),
        _pod("ueransim-medical-7c9c", "medical", "iperf3-client", started_at="2026-07-04T00:05:00Z"),
        _pod("iperf3-server-medical-abc", "medical", "iperf3-server", started_at="2026-07-04T00:05:00Z"),
    ]
    pods[2]["spec"]["containers"].append({"name": "ueransim-ue"})
    return pods


def test_baseline_client_command_streams_interval_output_instead_of_buffering_json() -> None:
    """Root cause guard: the baseline client must use -i/--forceflush (like every
    scenario's recreate_iperf3_job client) instead of --json with no -i, because
    --json buffers the entire report until the (here, 3600s) test completes,
    producing an empty client log for virtually the whole run."""
    service = ScenarioEnvironmentService.__new__(ScenarioEnvironmentService)
    service.namespace = "free5gc"

    class _RecordingK8s:
        def __init__(self) -> None:
            self.created_job: dict[str, Any] | None = None

        def request(self, method: str, path: str, body: Any = None, ignore_404: bool = False) -> tuple[int, Any]:
            if method == "GET":
                return 200, {"items": []}
            self.created_job = body
            return 201, {}

        def delete(self, path: str, ignore_404: bool = False) -> None:
            pass

    k8s = _RecordingK8s()

    service.ensure_baseline_iperf3_job(k8s)

    command = k8s.created_job["spec"]["template"]["spec"]["containers"][0]["command"]
    assert "--json" not in command
    assert "-i" in command
    assert "--forceflush" in command


def test_scenario_traffic_contains_baseline_sample_while_medical_event_runs() -> None:
    """Reproduces the citizen-protection verify gap: during a medical event, the
    baseline job/pod is still Running, but with the old --json/no -i baseline
    client command its log is empty mid-run. The fix relies on the generic
    iperf3-server's streamed interval log (present because the fixed client now
    passes -i) as the evidence source, so scenarioTraffic must still carry a
    scenario=="baseline" sample."""
    backend = CityVerseBackendApp(AppSettings())
    pods = _pods_for_baseline_and_medical()
    k8s = _FakeK8s(
        {
            "iperf3-baseline-abc123": BASELINE_CLIENT_LOG_JSON_MIDRUN,
            "iperf3-server-xyz": BASELINE_SERVER_LOG_STREAMING,
            "ueransim-medical-7c9c": MEDICAL_CLIENT_LOG,
            "iperf3-server-medical-abc": "",
        }
    )

    metrics = backend.iperf3_runtime_metrics(k8s, pods)

    traffic = metrics.get("scenarioTraffic")
    assert isinstance(traffic, list)
    baseline_samples = [s for s in traffic if s.get("scenario") == "baseline"]
    assert len(baseline_samples) == 1
    assert baseline_samples[0]["throughputMbps"] > 0
    medical_samples = [s for s in traffic if s.get("scenario") == "medical"]
    assert len(medical_samples) == 1


def test_standalone_baseline_does_not_claim_citizen_tun_throughput() -> None:
    """A pod-to-Service baseline is load evidence, not a UE/UPF path."""
    backend = CityVerseBackendApp(AppSettings())
    pods = _pods_for_baseline_and_medical()
    k8s = _FakeK8s(
        {
            "iperf3-baseline-abc123": BASELINE_CLIENT_LOG_JSON_MIDRUN,
            "iperf3-server-xyz": BASELINE_SERVER_LOG_STREAMING,
            "ueransim-medical-7c9c": MEDICAL_CLIENT_LOG,
            "iperf3-server-medical-abc": "",
        }
    )

    metrics = backend.iperf3_runtime_metrics(k8s, pods)

    baseline_sample = next(item for item in metrics["scenarioTraffic"] if item["scenario"] == "baseline")
    assert baseline_sample["transport"] == "cluster-network"
    assert CityVerseBackendApp.baseline_scenario_throughput(metrics) is None
    assert not any(edge.get("scenario") == "baseline" for edge in backend.scenario_edges_from_metrics(metrics))


def test_resident_baseline_sidecar_is_tun_bound_citizen_evidence() -> None:
    backend = CityVerseBackendApp(AppSettings())
    resident = _pod("ueransim-city-ue-abc", None, "ue")
    resident["spec"]["containers"].append({"name": "resident-baseline-iperf3"})
    log = (
        "RESIDENT_BASELINE state=running scenario=resident-baseline "
        "transport=free5gc-tun interface=uesimtun0 rate=120M\n"
        + BASELINE_SERVER_LOG_STREAMING
    )

    metrics = backend.iperf3_runtime_metrics(_FakeK8s({"ueransim-city-ue-abc": log}), [resident])

    sample = metrics["scenarioTraffic"][0]
    assert sample["scenario"] == "baseline"
    assert sample["pod"] == "ueransim-city-ue-abc"
    assert sample["transport"] == "free5gc-tun"
    assert sample["interface"] == "uesimtun0"
    assert metrics["userPlaneThroughputMbps"] > 0
    assert metrics["nonUserPlaneThroughputMbps"] == 0
    assert CityVerseBackendApp.baseline_scenario_throughput(metrics) > 0
    assert any(edge.get("scenario") == "baseline" for edge in backend.scenario_edges_from_metrics(metrics))
