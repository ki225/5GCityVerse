#!/usr/bin/env python3
"""Rotate and verify the free5GC WebUI administrator password.

Secrets are accepted only through environment variables or a no-echo prompt;
they are never accepted as command-line arguments or included in diagnostics.
"""

from __future__ import annotations

import getpass
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class RotationError(RuntimeError):
    """A fail-closed rotation stage failed."""


@dataclass(frozen=True)
class RotationConfig:
    base_url: str
    username: str
    current_password: str
    new_password: str
    timeout_seconds: float = 20.0


def secret_from_env(name: str, prompt: str) -> str:
    value = os.environ.get(name, "")
    if not value and sys.stdin.isatty():
        value = getpass.getpass(prompt)
    if not value:
        raise RotationError(f"required secret environment variable is missing: {name}")
    return value


def config_from_environment() -> RotationConfig:
    base_url = os.environ.get("FREE5GC_WEBUI_URL", "").strip().rstrip("/")
    if not base_url:
        raise RotationError("FREE5GC_WEBUI_URL is required")
    if not base_url.startswith(("http://", "https://")):
        raise RotationError("FREE5GC_WEBUI_URL must be an HTTP(S) URL")
    return RotationConfig(
        base_url=base_url,
        username=os.environ.get("FREE5GC_WEBUI_USERNAME", "admin").strip() or "admin",
        current_password=secret_from_env("FREE5GC_WEBUI_CURRENT_PASSWORD", "Current free5GC WebUI password: "),
        new_password=secret_from_env("FREE5GC_WEBUI_PASSWORD", "New free5GC WebUI password: "),
        timeout_seconds=float(os.environ.get("FREE5GC_HTTP_TIMEOUT_SECONDS", "20")),
    )


def http_json(config: RotationConfig, method: str, path: str, body: dict[str, Any], token: str | None = None) -> tuple[int, dict[str, Any]]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Token"] = token
    request = urllib.request.Request(
        f"{config.base_url}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw or "{}")
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8")
        try:
            return error.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return error.code, {}
    except (TimeoutError, socket.timeout, urllib.error.URLError) as error:
        raise RotationError("free5GC WebUI network request failed") from error


def login(config: RotationConfig, password: str) -> str:
    status, response = http_json(
        config, "POST", "/api/login", {"username": config.username, "password": password}
    )
    token = response.get("access_token") or response.get("token")
    if status >= 300 or not isinstance(token, str) or not token:
        raise RotationError("free5GC WebUI login failed")
    return token


def rotate_password(config: RotationConfig) -> None:
    token = login(config, config.current_password)
    status, _ = http_json(
        config,
        "POST",
        "/api/change-password",
        {"email": config.username, "encryptedPassword": config.new_password},
        token,
    )
    if status >= 300:
        raise RotationError("free5GC WebUI password change failed")
    login(config, config.new_password)


def main() -> int:
    try:
        rotate_password(config_from_environment())
    except (RotationError, ValueError):
        print("free5GC WebUI password rotation failed; secret was not updated", file=sys.stderr)
        return 1
    print("free5GC WebUI password rotation verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
