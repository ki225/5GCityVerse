"""Run inside Linux/WSL: python3 -m unittest discover -s scripts/onprem/tests -p test_service_launcher.py."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import unittest
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[3]


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipUnless(sys.platform == "linux", "Linux /proc process ownership")
class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cityverse-launcher-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.state = self.directory / "runtime.json"
        self.backend_port, self.frontend_port = port(), port()
        self.env = {**os.environ, "OPENAI_API_KEY": "launcher-test-placeholder"}
        # No production dotenv or native infra is touched by these fixtures.
        self.fixture = self.directory / "fixture.py"
        self.fixture.write_text('''import os
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ready")
    def log_message(self, *args): pass
if "PORT" in os.environ:
    assert "OPENAI_API_KEY" not in os.environ
    assert os.environ["CITYVERSE_BACKEND_URL"].startswith("http://127.0.0.1:")
HTTPServer(("127.0.0.1", int(os.environ.get("PORT", os.environ.get("CITYVERSE_PORT", "0")))) , Handler).serve_forever()
''')
        self.command = f"exec {shlex.quote(sys.executable)} {shlex.quote(str(self.fixture))}"
        self.addCleanup(self.stop)

    def run_script(self, name, *args):
        return subprocess.run(["bash", str(ROOT / "scripts/onprem" / name),
                               "--state-file", str(self.state), *args],
                              env=self.env, cwd="/tmp", capture_output=True, text=True, timeout=45)

    def start(self, *args):
        return self.run_script("start-onprem.sh", "--env-file", str(self.directory / "absent.env"),
                               "--backend-port", str(self.backend_port), "--frontend-port", str(self.frontend_port),
                               "--backend-command", self.command, "--frontend-command", self.command,
                               "--readiness-timeout", "4", *args)

    def stop(self):
        return self.run_script("stop-onprem.sh")

    def test_managed_services_start_stop_and_duplicate_rejected(self):
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["status"], "ready")
        self.assertNotIn("launcher-test-placeholder", self.state.read_text())
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(self.start().returncode, 0)
        self.assertEqual(self.stop().returncode, 0)
        self.assertFalse(self.state.exists())
        for service_port in (self.backend_port, self.frontend_port):
            with socket.socket() as sock:
                self.assertNotEqual(sock.connect_ex(("127.0.0.1", service_port)), 0)

    def test_existing_backend_survives_without_api_key(self):
        external = subprocess.Popen([sys.executable, str(self.fixture)],
                                    env={**os.environ, "CITYVERSE_PORT": str(self.backend_port)})
        self.addCleanup(lambda: (external.terminate(), external.wait(timeout=5)))
        self.env.pop("OPENAI_API_KEY", None)
        self.assertEqual(self.start("--use-existing-backend").returncode, 0)
        self.assertIsNone(json.loads(self.state.read_text())["backend"])
        self.assertEqual(self.stop().returncode, 0)
        with urlopen(f"http://127.0.0.1:{self.backend_port}/health", timeout=2) as response:
            self.assertEqual(response.status, 200)

    def test_frontend_failure_cleans_backend(self):
        result = self.start("--frontend-command", "exit 7")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.state.exists())
        with socket.socket() as sock:
            self.assertNotEqual(sock.connect_ex(("127.0.0.1", self.backend_port)), 0)

    def test_readiness_timeout_cleans_backend(self):
        result = self.start("--backend-command", "exec sleep 60", "--readiness-timeout", "1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Readiness timed out", result.stderr)
        self.assertFalse(self.state.exists())

    def test_stale_identity_does_not_stop_unrelated_process(self):
        external = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(lambda: (external.terminate(), external.wait(timeout=5)))
        self.state.write_text(json.dumps({"backend": {"pid": external.pid, "pgrp": external.pid,
                                                     "start_ticks": "wrong", "boot_id": "wrong"}}))
        self.assertEqual(self.stop().returncode, 0)
        self.assertIsNone(external.poll())

    def test_dotenv_values_are_literal(self):
        spec = importlib.util.spec_from_file_location("launcher", ROOT / "scripts/onprem/service_launcher.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        marker = self.directory / "must-not-exist"
        envfile = self.directory / "literal.env"
        envfile.write_text(f'A="$(touch {marker})"\nB=hello # comment\nC=\'literal $HOME\'\n')
        values = module.dotenv(envfile)
        self.assertEqual(values["A"], f"$(touch {marker})")
        self.assertEqual(values["B"], "hello")
        self.assertEqual(values["C"], "literal $HOME")
        self.assertFalse(marker.exists())

    def test_recently_closed_listener_port_is_reusable_but_active_listener_is_not(self):
        spec = importlib.util.spec_from_file_location("launcher", ROOT / "scripts/onprem/service_launcher.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with socket.socket() as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", 0))
            service_port = server.getsockname()[1]
            server.listen()
            with self.assertRaises(OSError):
                module.free_port("127.0.0.1", service_port)
            with socket.create_connection(("127.0.0.1", service_port)) as client:
                peer, _ = server.accept()
                peer.close()
                self.assertEqual(client.recv(1), b"")
        # The former server side is now in TIME_WAIT, but no listener owns the port.
        module.free_port("127.0.0.1", service_port)


if __name__ == "__main__":
    unittest.main()
