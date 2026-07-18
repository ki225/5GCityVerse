import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = {
    "pfd": ROOT / "fn-nef-pfd-create" / "index.py",
    "qos": ROOT / "fn-nef-qos-subscription" / "index.py",
    "traffic": ROOT / "fn-nef-traffic-influence" / "index.py",
}


def load_tool(name: str):
    spec = importlib.util.spec_from_file_location(f"nef_{name}_test", TOOLS[name])
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def result_body(response: dict) -> dict:
    raw = response["response"]["functionResponse"]["responseBody"]["TEXT"]["body"]
    return json.loads(raw)


@pytest.mark.parametrize("name", TOOLS)
@pytest.mark.parametrize(
    "url",
    ["", "http://free5gc-nef.free5gc.svc.cluster.local:8080", "http://placeholder:8080"],
)
def test_unknown_nef_endpoint_fails_closed(monkeypatch, name: str, url: str):
    module = load_tool(name)
    monkeypatch.setattr(module, "NEF_BASE_URL", url)

    def unexpected_request(*_args, **_kwargs):
        raise AssertionError("an unconfigured endpoint must not be called")

    monkeypatch.setattr(module, "_http_post", unexpected_request)
    body = result_body(module.lambda_handler({}, None))

    assert body["success"] is False
    assert body["compensated"] is False
    assert body["native_http_status"] == 503
    assert body["api_endpoint"] == ""


@pytest.mark.parametrize("name", ["pfd", "traffic"])
def test_transport_failure_is_not_reported_as_compensated_success(monkeypatch, name: str):
    module = load_tool(name)
    monkeypatch.setattr(module, "NEF_BASE_URL", "http://internal-nef.example.com:8080")
    monkeypatch.setattr(module, "_http_post", lambda *_args, **_kwargs: (503, {"error": "timeout"}))

    body = result_body(module.lambda_handler({}, None))

    assert body["success"] is False
    assert body["compensated"] is False
    assert body["native_http_status"] == 503


def test_qos_transport_failure_cannot_be_compensated_by_profile(monkeypatch):
    module = load_tool("qos")
    monkeypatch.setattr(module, "NEF_BASE_URL", "http://internal-nef.example.com:8080")
    monkeypatch.setattr(module, "_http_post", lambda *_args, **_kwargs: (503, {"error": "timeout"}))
    monkeypatch.setattr(
        module,
        "_free5gc_qos_state",
        lambda _event_type: {"profileFound": True, "qosFlows": [{"5qi": 1}]},
    )

    body = result_body(module.lambda_handler({}, None))

    assert body["success"] is False
    assert body["compensated"] is False
    assert body["native_http_status"] == 503


@pytest.mark.parametrize(
    ("parameter_value", "expected"),
    [
        ("permit out ip from any to any", ["permit out ip from any to any"]),
        ('["permit in udp from any to any", "permit out udp from any to any"]',
         ["permit in udp from any to any", "permit out udp from any to any"]),
    ],
)
def test_pfd_normalizes_bedrock_string_flow_descriptions(monkeypatch, parameter_value, expected):
    module = load_tool("pfd")
    monkeypatch.setattr(module, "NEF_BASE_URL", "http://internal-nef.example.com:8080")
    captured = {}

    def capture_request(_url, payload):
        captured.update(payload)
        return 201, {"ok": True}

    monkeypatch.setattr(module, "_http_post", capture_request)
    event = {
        "parameters": [
            {"name": "app_id", "value": "validation-app"},
            {"name": "flow_descriptions", "value": parameter_value},
        ]
    }
    body = result_body(module.lambda_handler(event, None))

    assert body["success"] is True
    pfd = next(iter(captured["pfdDatas"]["validation-app"]["pfds"].values()))
    assert pfd["flowDescriptions"] == expected


def test_pfd_retries_only_explicit_duplicate_app_id(monkeypatch):
    module = load_tool("pfd")
    monkeypatch.setattr(module, "NEF_BASE_URL", "http://internal-nef.example.com:8080")
    calls = []

    def duplicate_then_create(_url, payload):
        calls.append(json.loads(json.dumps(payload)))
        if len(calls) == 1:
            return 500, {"APP_ID_DUPLICATED": {"failureCode": "APP_ID_DUPLICATED"}}
        return 201, {"self": "transactions/2"}

    monkeypatch.setattr(module, "_http_post", duplicate_then_create)
    event = {"parameters": [{"name": "app_id", "value": "validation-app"}]}
    body = result_body(module.lambda_handler(event, None))

    assert body["success"] is True
    assert body["duplicate_retry"] is True
    assert body["requested_app_id"] == "validation-app"
    assert body["app_id"].startswith("validation-app-")
    assert len(calls) == 2
    assert "validation-app" in calls[0]["pfdDatas"]
    assert body["app_id"] in calls[1]["pfdDatas"]


def test_traffic_payload_satisfies_free5gc_pcf_mandatory_fields(monkeypatch):
    module = load_tool("traffic")
    monkeypatch.setattr(module, "NEF_BASE_URL", "http://internal-nef.example.com:8080")
    captured = {}

    def capture_request(_url, payload):
        captured.update(payload)
        return 201, {"self": "subscription/1"}

    monkeypatch.setattr(module, "_http_post", capture_request)
    event = {
        "parameters": [
            {"name": "af_service_id", "value": "validation-concert"},
            {"name": "ue_ipv4", "value": "10.101.0.9"},
            {"name": "slice_sst", "value": "1"},
        ]
    }
    body = result_body(module.lambda_handler(event, None))

    assert body["success"] is True
    assert captured["afAppId"] == "validation-concert"
    assert captured["suppFeat"] == "1"
    assert captured["notificationDestination"].startswith(
        "http://internal-nef.example.com:8080/"
    )
    assert captured["trafficFilters"][0]["flowId"] == 1


def test_qos_state_falls_back_to_real_subscriber_session_profile(monkeypatch):
    module = load_tool("qos")
    monkeypatch.setattr(module, "FREE5GC_WEBUI_URL", "http://internal-webui.example.com:5000")
    monkeypatch.setattr(module, "_webui_login", lambda: "token")

    def fake_request(_method, path, _token, _payload=None):
        if path == "/api/profile":
            return 200, []
        assert path == "/api/subscriber/imsi-208930000000004/20893"
        return 200, {
            "ueId": "imsi-208930000000004",
            "SessionManagementSubscriptionData": [
                {
                    "dnnConfigurations": {
                        "citizen": {"5gQosProfile": {"5qi": 9, "priorityLevel": 8}}
                    }
                }
            ],
        }

    monkeypatch.setattr(module, "_webui_request", fake_request)
    state = module._free5gc_qos_state("concert")

    assert state["subscriberFound"] is True
    assert state["sessionQosProfiles"] == [{"5qi": 9, "priorityLevel": 8}]


def test_qos_unsupported_native_api_accepts_verified_subscriber_profile(monkeypatch):
    module = load_tool("qos")
    monkeypatch.setattr(module, "NEF_BASE_URL", "http://internal-nef.example.com:8080")
    monkeypatch.setattr(module, "_http_post", lambda *_args, **_kwargs: (404, {"error": "unsupported"}))
    monkeypatch.setattr(
        module,
        "_free5gc_qos_state",
        lambda _event_type: {
            "profileFound": False,
            "subscriberFound": True,
            "sessionQosProfiles": [{"5qi": 9, "priorityLevel": 8}],
        },
    )

    body = result_body(module.lambda_handler({}, None))

    assert body["success"] is True
    assert body["compensated"] is True
    assert body["http_status"] == 200
