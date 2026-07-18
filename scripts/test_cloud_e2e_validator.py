from scripts.cloud_e2e_validator import SCENARIOS, build_cases, redact, reset


class FakeResponse:
    def __init__(self, body: dict, status_code: int = 200) -> None:
        self.body = body
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self.body


def test_backend_scenario_enum_and_matrix_shape() -> None:
    assert SCENARIOS == ("concert", "typhoon", "accident", "medical", "iot_surge")
    matrix = build_cases("matrix")
    assert len(matrix) == 45
    assert {name for case in matrix for name, _ in case.scenarios} == set(SCENARIOS)


def test_full_profile_includes_three_five_scenario_batches() -> None:
    full = build_cases("full")
    assert len(full) == 48
    assert [len(case.scenarios) for case in full[-3:]] == [5, 5, 5]


def test_evidence_redacts_shared_token() -> None:
    evidence = {"error": "failed for secret-token", "nested": ["secret-token"]}
    assert redact(evidence, "secret-token") == {
        "error": "failed for [REDACTED]",
        "nested": ["[REDACTED]"],
    }


def test_reset_waits_for_async_success(monkeypatch) -> None:
    statuses = iter([
        FakeResponse({"resetId": "reset-1", "status": "running", "progressPercent": 50}),
        FakeResponse({"resetId": "reset-1", "status": "success", "progressPercent": 100}),
    ])
    monkeypatch.setattr("scripts.cloud_e2e_validator.requests.post", lambda *args, **kwargs: FakeResponse(
        {"resetId": "reset-1", "status": "queued", "progressPercent": 0}
    ))
    monkeypatch.setattr("scripts.cloud_e2e_validator.requests.get", lambda *args, **kwargs: next(statuses))
    monkeypatch.setattr("scripts.cloud_e2e_validator.time.sleep", lambda *_: None)

    result = reset("https://api.example", {"X-Session-Id": "session"}, timeout_seconds=1, poll_seconds=0)

    assert result["status"] == "success"
    assert result["progressPercent"] == 100
