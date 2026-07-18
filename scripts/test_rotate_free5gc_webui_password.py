from __future__ import annotations

import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).with_name("rotate-free5gc-webui-password.py")
SPEC = importlib.util.spec_from_file_location("rotate_free5gc_webui_password", MODULE_PATH)
assert SPEC and SPEC.loader
rotation = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = rotation
SPEC.loader.exec_module(rotation)


class MockWebUi:
    def __init__(self, login_statuses=(200, 200), change_status=200):
        self.login_statuses = list(login_statuses)
        self.change_status = change_status
        self.requests: list[dict] = []

    def handler(self):
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                state.requests.append({"path": self.path, "body": body, "token": self.headers.get("Token")})
                if self.path == "/api/login":
                    status = state.login_statuses.pop(0)
                    response = {"access_token": "mock-token"} if status < 300 else {"error": "denied"}
                elif self.path == "/api/change-password":
                    status = state.change_status
                    response = {"status": "ok"} if status < 300 else {"error": "change rejected"}
                else:
                    status, response = 404, {}
                encoded = json.dumps(response).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        return Handler


@pytest.fixture
def mock_server():
    servers = []

    def start(state):
        server = ThreadingHTTPServer(("127.0.0.1", 0), state.handler())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_port}"

    yield start
    for server in servers:
        server.shutdown()


def config(url):
    return rotation.RotationConfig(url, "admin", "old-sensitive", "new-sensitive", 2)


def test_success_logs_in_changes_and_verifies(mock_server):
    state = MockWebUi()
    rotation.rotate_password(config(mock_server(state)))
    assert [item["path"] for item in state.requests] == ["/api/login", "/api/change-password", "/api/login"]
    assert state.requests[1]["token"] == "mock-token"
    assert state.requests[1]["body"] == {"email": "admin", "encryptedPassword": "new-sensitive"}
    assert state.requests[0]["body"]["password"] == "old-sensitive"
    assert state.requests[2]["body"]["password"] == "new-sensitive"


@pytest.mark.parametrize(
    ("login_statuses", "change_status", "expected_calls"),
    [((401,), 200, 1), ((200,), 500, 2), ((200, 401), 200, 3)],
)
def test_fail_closed_at_each_stage(mock_server, login_statuses, change_status, expected_calls):
    state = MockWebUi(login_statuses, change_status)
    with pytest.raises(rotation.RotationError):
        rotation.rotate_password(config(mock_server(state)))
    assert len(state.requests) == expected_calls


def test_main_never_emits_password_or_token(monkeypatch, mock_server, capsys):
    state = MockWebUi()
    monkeypatch.setenv("FREE5GC_WEBUI_URL", mock_server(state))
    monkeypatch.setenv("FREE5GC_WEBUI_CURRENT_PASSWORD", "old-sensitive")
    monkeypatch.setenv("FREE5GC_WEBUI_PASSWORD", "new-sensitive")
    assert rotation.main() == 0
    output = capsys.readouterr()
    combined = output.out + output.err
    assert "old-sensitive" not in combined
    assert "new-sensitive" not in combined
    assert "mock-token" not in combined


def test_failed_main_never_emits_password_or_token(monkeypatch, mock_server, capsys):
    state = MockWebUi(login_statuses=(401,))
    monkeypatch.setenv("FREE5GC_WEBUI_URL", mock_server(state))
    monkeypatch.setenv("FREE5GC_WEBUI_CURRENT_PASSWORD", "old-sensitive")
    monkeypatch.setenv("FREE5GC_WEBUI_PASSWORD", "new-sensitive")
    assert rotation.main() == 1
    combined = "".join(capsys.readouterr())
    for secret in ("old-sensitive", "new-sensitive", "mock-token"):
        assert secret not in combined


def test_deploy_writes_webui_secret_only_after_verified_rotation():
    deploy = Path(__file__).with_name("deploy.sh").read_text(encoding="utf-8")
    rotation_call = deploy.index('python3 "$ROOT_DIR/scripts/rotate-free5gc-webui-password.py"')
    webui_secret_write = deploy.index('put-secret-value --secret-id "$WEBUI_SECRET_ARN"')
    service_url_sync = deploy.index('log "Updating Lambda service URLs')
    subscriber_seed = deploy.index('python3 "$ROOT_DIR/scripts/seed-subscribers.py"')
    assert rotation_call < webui_secret_write < service_url_sync < subscriber_seed
