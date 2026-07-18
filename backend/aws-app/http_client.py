from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


class HttpJsonClient:
    def request(
        self,
        method: str,
        url: str,
        body: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, Any]:
        if timeout is None:
            timeout = float(os.environ.get("HTTP_JSON_TIMEOUT_SECONDS", "3"))
        data = None
        req_headers = {"Content-Type": "application/json"}
        if headers:
            req_headers.update(headers)
        if body is not None:
            data = json.dumps(body).encode("utf-8")

        req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                raw = res.read().decode("utf-8")
                return res.status, json.loads(raw or "{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                parsed = json.loads(raw or "{}")
            except json.JSONDecodeError:
                parsed = {"raw": raw}
            return exc.code, parsed
        except urllib.error.URLError as exc:
            return 0, {"error": self.format_network_error(exc)}
        except OSError as exc:
            return 0, {"error": self.format_network_error(exc)}

    @staticmethod
    def format_network_error(exc: BaseException) -> str:
        reason = getattr(exc, "reason", exc)
        message = str(reason)
        if "Device or resource busy" in message:
            return "free5GC WebUI is unreachable: device or resource is busy. Check the Terraform-managed load balancer and retry."
        return f"free5GC WebUI is unreachable: {message}"
