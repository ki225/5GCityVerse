"""
Probe the official free5GC MCP HTTP server from the project-agent side.

This intentionally does not implement a free5GC MCP server. It is a tiny MCP
client used to verify that an agent can connect to the official server declared
in mcp.json / .vscode/mcp.json and invoke MCP methods.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = "2025-03-26"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


class McpHttpClient:
    def __init__(self, url: str) -> None:
        self.url = url
        self.request_id = 0
        self.session_id: str | None = None

    def initialize(self) -> dict[str, Any]:
        result, headers = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "5gcityverse-agent-probe", "version": "0.1.0"},
            },
            include_headers=True,
        )
        self.session_id = headers.get("mcp-session-id") or headers.get("Mcp-Session-Id")
        self.notify("notifications/initialized", {})
        return result

    def list_tools(self) -> dict[str, Any]:
        return self.request("tools/list", {})

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def notify(self, method: str, params: dict[str, Any]) -> None:
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        self._post(body, expect_response=False)

    def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        include_headers: bool = False,
    ) -> Any:
        self.request_id += 1
        body = {"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params}
        payload, headers = self._post(body, expect_response=True)
        if "error" in payload:
            raise RuntimeError(f"MCP error from {method}: {payload['error']}")
        result = payload.get("result", payload)
        if include_headers:
            return result, headers
        return result

    def _post(self, body: dict[str, Any], *, expect_response: bool) -> tuple[dict[str, Any], dict[str, str]]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        req = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                if not expect_response:
                    return {}, dict(resp.headers)
                return _parse_mcp_body(raw), dict(resp.headers)
        except urllib.error.URLError as exc:
            raise ConnectionError(f"Cannot reach official free5GC MCP server at {self.url}: {exc}") from exc


def load_server_url(config_path: Path, server_name: str) -> str:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    servers = config.get("servers") or config.get("mcpServers") or {}
    server = servers.get(server_name)
    if not server:
        raise KeyError(f"MCP server {server_name!r} not found in {config_path}")
    url = server.get("url")
    if not url:
        raise KeyError(f"MCP server {server_name!r} does not define a url in {config_path}")
    return url


def _parse_mcp_body(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if not text:
        return {}
    if text.startswith("data:"):
        chunks = []
        for line in text.splitlines():
            if line.startswith("data:"):
                chunks.append(line.removeprefix("data:").strip())
        text = "\n".join(chunks).strip()
    return json.loads(text)


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe the official free5GC MCP HTTP server.")
    parser.add_argument("--config", default="mcp.json", help="MCP config path")
    parser.add_argument("--server", default="free5gc-mcp", help="MCP server name")
    parser.add_argument("--call-tool", help="Tool name to call after tools/list")
    parser.add_argument("--arguments", default="{}", help="JSON object for tools/call arguments")
    parser.add_argument("--quiet-tools", action="store_true", help="Only print tool names for tools/list")
    args = parser.parse_args()

    url = load_server_url(Path(args.config), args.server)
    client = McpHttpClient(url)

    print(f"Connecting to official free5GC MCP: {url}")
    init_result = client.initialize()
    print("initialize:", json.dumps(init_result, indent=2, ensure_ascii=False))

    tools_result = client.list_tools()
    if args.quiet_tools:
        tool_names = [tool.get("name") for tool in tools_result.get("tools", [])]
        print("tools/list:", json.dumps(tool_names, indent=2, ensure_ascii=False))
    else:
        print("tools/list:", json.dumps(tools_result, indent=2, ensure_ascii=False))

    if args.call_tool:
        arguments = json.loads(args.arguments)
        call_result = client.call_tool(args.call_tool, arguments)
        print(f"tools/call {args.call_tool}:", json.dumps(call_result, indent=2, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
