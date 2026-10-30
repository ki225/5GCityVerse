"""Pod-local, read-only RAN/AMF evidence served to the Kubernetes pod proxy.

No Service or external listener is created.  The caller must still pin and
recheck the workload container incarnation around every observation.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import urllib.request


MAX_INFO_BYTES = 1024 * 1024


def snapshot(role: str) -> dict:
    observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    sctp = Path("/proc/net/sctp/assocs").read_text(encoding="ascii")
    if role == "amf":
        return {"schemaVersion": "cityverse.ran-evidence/v1", "role": role,
                "observedAt": observed_at, "sctp": sctp}
    config = json.loads(Path("/etc/cityverse/gnb.json").read_text(encoding="utf-8"))["gnb"]
    with urllib.request.urlopen("http://127.0.0.1:40104/api/gnb/info", timeout=2) as response:
        raw = response.read(MAX_INFO_BYTES + 1)
    if len(raw) > MAX_INFO_BYTES:
        raise ValueError("gNB response exceeds evidence limit")
    info = json.loads(raw)
    return {"schemaVersion": "cityverse.ran-evidence/v1", "role": role,
            "observedAt": observed_at, "config": config, "info": info, "sctp": sctp}


def handler_for(role: str):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/healthz":
                self._respond(200, {"ready": True})
            elif self.path == "/v1/evidence":
                try:
                    self._respond(200, snapshot(role))
                except (OSError, ValueError, KeyError, TimeoutError):
                    self._respond(503, {"error": "native_evidence_unavailable"})
            else:
                self._respond(404, {"error": "not_found"})

        def _respond(self, status: int, body: dict):
            data = json.dumps(body, separators=(",", ":"), allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):
            pass

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=("gnb", "amf"), required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=9083)
    args = parser.parse_args()
    ThreadingHTTPServer((args.host, args.port), handler_for(args.role)).serve_forever()


if __name__ == "__main__":
    main()
