from __future__ import annotations

from app import CityVerseBackendApp
from config import AppSettings
from scenario_environment import ScenarioEnvironmentService


def _build_app(monkeypatch) -> CityVerseBackendApp:
    monkeypatch.delenv("DYNAMODB_TABLE", raising=False)
    monkeypatch.delenv("APIGW_WS_ENDPOINT", raising=False)
    monkeypatch.delenv("EKS_CLUSTER_NAME", raising=False)
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)
    monkeypatch.delenv("FREE5GC_WEBUI_URL", raising=False)
    return CityVerseBackendApp(AppSettings())


EXPECTED_DURATIONS = {event_type: 180 for event_type in (
    "concert", "typhoon", "accident", "medical", "iot_surge"
)}


def test_iperf_generation_interval_matches_the_three_minute_scenario_window() -> None:
    assert ScenarioEnvironmentService.iperf_duration_seconds_from_profile("800M") == 180
    assert ScenarioEnvironmentService.iperf_args_for_profile("800M")[-4:] == ["-t", "180", "-l", "1200"]


def test_all_event_durations_are_at_least_180_seconds(monkeypatch) -> None:
    backend = _build_app(monkeypatch)
    for event_type in EXPECTED_DURATIONS:
        _, scenario_context = backend.event_config_for_request(event_type)
        assert scenario_context["eventDurationSeconds"] >= 180, event_type


def test_event_durations_match_expected_values(monkeypatch) -> None:
    backend = _build_app(monkeypatch)
    for event_type, expected_seconds in EXPECTED_DURATIONS.items():
        _, scenario_context = backend.event_config_for_request(event_type)
        assert scenario_context["eventDurationSeconds"] == expected_seconds, event_type


def test_all_event_durations_are_identical(monkeypatch) -> None:
    backend = _build_app(monkeypatch)
    durations = {}
    for event_type in EXPECTED_DURATIONS:
        _, scenario_context = backend.event_config_for_request(event_type)
        durations[event_type] = scenario_context["eventDurationSeconds"]

    assert set(durations.values()) == {180}


def test_monitor_event_window_runs_at_least_three_polling_rounds_at_new_durations(monkeypatch) -> None:
    """The 30s poll interval must still fit >=3 rounds inside the shortest
    (accident, 180s) event window."""
    backend = _build_app(monkeypatch)
    monkeypatch.setattr(backend, "event_cancelled_by_reset", lambda _execution_id: False)
    monkeypatch.setattr(backend, "remaining_lambda_millis", lambda _context: 300_000)
    monkeypatch.setattr(backend, "current_metrics", lambda *a, **k: {})
    monkeypatch.setattr(backend, "current_slices", lambda *a, **k: [])
    monkeypatch.setattr(backend, "broadcast", lambda _message: None)

    # Fake wall-clock: each time.sleep(n) call (and each time.time() read)
    # advances a virtual clock by the requested amount, so the 180s window
    # elapses instantly instead of making the test take two real minutes.
    fake_now = {"value": 1_000_000.0}

    def fake_time() -> float:
        return fake_now["value"]

    def fake_sleep(seconds: float) -> None:
        fake_now["value"] += seconds

    monkeypatch.setattr("app.time.time", fake_time)
    monkeypatch.setattr("app.time.sleep", fake_sleep)

    rounds_seen: list[int] = []

    class _RecordingEvents:
        def update_status(self, _execution_id, updates):
            round_number = (updates.get("plannerPoll") or {}).get("round")
            if round_number is not None:
                rounds_seen.append(round_number)

        def get_status(self, _execution_id):
            return None

    backend.events = _RecordingEvents()

    _, scenario_context = backend.event_config_for_request("accident")
    assert scenario_context["eventDurationSeconds"] == 180

    backend.monitor_event_window("exec-1", "accident", type("Cfg", (), {"to_dict": lambda self: {}})(), scenario_context, None)

    assert len(rounds_seen) >= 3


def test_monitor_event_window_uses_backend_traffic_deadline(monkeypatch) -> None:
    """Planning time must consume the already-running traffic window instead
    of starting a second full duration after the decision is ready."""
    backend = _build_app(monkeypatch)
    monkeypatch.setattr(backend, "event_cancelled_by_reset", lambda _execution_id: False)
    monkeypatch.setattr(backend, "remaining_lambda_millis", lambda _context: 300_000)
    monkeypatch.setattr(backend, "current_metrics", lambda *a, **k: {})
    monkeypatch.setattr(backend, "current_slices", lambda *a, **k: [])
    monkeypatch.setattr(backend, "broadcast", lambda _message: None)

    # Traffic was primed at t=1,000,000 and ends 120 seconds later. Simulate
    # the AI decision becoming ready at t=1,000,105, leaving only 15 seconds.
    fake_now = {"value": 1_000_105.0}
    monkeypatch.setattr("app.time.time", lambda: fake_now["value"])
    monkeypatch.setattr("app.time.sleep", lambda seconds: fake_now.__setitem__("value", fake_now["value"] + seconds))

    rounds_seen: list[int] = []

    class _RecordingEvents:
        def update_status(self, _execution_id, updates):
            round_number = (updates.get("plannerPoll") or {}).get("round")
            if round_number is not None:
                rounds_seen.append(round_number)

        def get_status(self, _execution_id):
            return None

    backend.events = _RecordingEvents()
    scenario_context = {
        "eventDurationSeconds": 120,
        "runtimePrime": {"trafficEndsEpochMillis": 1_000_120_000},
    }

    backend.monitor_event_window("exec-1", "network_round", type("Cfg", (), {"to_dict": lambda self: {}})(), scenario_context, None)

    assert fake_now["value"] == 1_000_120.0
    assert rounds_seen == [1]
