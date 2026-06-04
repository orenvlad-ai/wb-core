"""Loopback/authenticated probe for a running hosted wb-core read-only MCP."""

from __future__ import annotations

from pathlib import Path
import argparse
import http.client
import json
import os
import sys
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.wb_core_readonly_mcp_smoke import EXPECTED_TOOLS, FORBIDDEN_TOOL_WORDS  # noqa: E402


DEFAULT_TOKEN_ENV = "WB_CORE_READONLY_MCP_TOKEN"


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe a running wb-core-readonly-mcp HTTP service.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8766", help="Loopback base URL, without /mcp.")
    parser.add_argument("--token-env", default=DEFAULT_TOKEN_ENV, help="Env var containing the bearer token.")
    args = parser.parse_args()

    token = os.environ.get(args.token_env, "")
    if not token:
        raise SystemExit(f"missing bearer token env: {args.token_env}")
    client = HttpMcpClient(args.base_url, token)
    health = client.get_json("/healthz")
    if not health.get("ok") or not health.get("auth_required"):
        raise AssertionError(f"unexpected healthz response: {health}")
    client.initialize()
    names = client.tool_names()
    if names != EXPECTED_TOOLS:
        raise AssertionError(f"unexpected tool list: {sorted(names)}")
    forbidden = [name for name in names for word in FORBIDDEN_TOOL_WORDS if word in name]
    if forbidden:
        raise AssertionError(f"mutation-like tools must be absent: {forbidden}")
    status = client.call_tool("repo_status", {})
    if status.get("source_mode") != "managed_clone" or status.get("configured_branch") != "main":
        raise AssertionError(f"repo_status must prove managed main clone: {status}")
    readme = client.call_tool("read_file_range", {"path": "README.md", "start_line": 1, "end_line": 5})
    if not readme.get("ok") or "wb-core" not in str(readme.get("text", "")):
        raise AssertionError(f"read_file_range returned unexpected payload: {readme}")
    search = client.call_tool("search_text", {"path": "docs/architecture", "query": "WB-Core Read-Only MCP Contract", "max_matches": 3})
    if not search.get("ok") or int(search.get("match_count", 0)) < 1:
        raise AssertionError(f"search_text did not find architecture contract: {search}")
    denied = client.call_tool("read_file", {"path": ".env"})
    if denied.get("ok") is not False or denied.get("error") != "denied_sensitive_path":
        raise AssertionError(f"denied path behavior regressed: {denied}")
    unknown = client.call_tool("write_file", {"path": "README.md", "text": "nope"})
    if unknown.get("ok") is not False or unknown.get("error") != "unknown_tool":
        raise AssertionError(f"mutation-like unknown tool was not rejected: {unknown}")
    print("wb-core-readonly-mcp hosted probe passed")


class HttpMcpClient:
    def __init__(self, base_url: str, token: str) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("base URL must be http/https")
        if parsed.scheme == "https":
            raise ValueError("hosted probe is loopback/http-only; terminate TLS before loopback")
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 80
        self.base_path = parsed.path.rstrip("/")
        self.token = token
        self.request_id = 0

    def get_json(self, path: str) -> dict[str, Any]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            conn.request("GET", f"{self.base_path}{path}")
            response = conn.getresponse()
            body = response.read().decode("utf-8")
            if response.status != 200:
                raise AssertionError(f"GET {path} returned {response.status}: {body}")
            return json.loads(body)
        finally:
            conn.close()

    def initialize(self) -> None:
        response = self.rpc(
            "initialize",
            {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "hosted-probe"}},
        )
        if response["result"]["serverInfo"]["name"] != "wb-core-readonly-mcp":
            raise AssertionError(f"unexpected initialize response: {response}")

    def tool_names(self) -> set[str]:
        response = self.rpc("tools/list", {})
        return {tool["name"] for tool in response["result"]["tools"]}

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = self.rpc("tools/call", {"name": name, "arguments": arguments})
        text = response["result"]["content"][0]["text"]
        return json.loads(text)

    def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.request_id += 1
        payload = {"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params}
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            conn.request("POST", f"{self.base_path}/mcp", body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read().decode("utf-8")
            if response.status != 200:
                raise AssertionError(f"MCP {method} returned HTTP {response.status}: {raw}")
            parsed = json.loads(raw)
            if "error" in parsed:
                raise AssertionError(f"MCP {method} returned JSON-RPC error: {parsed}")
            return parsed
        finally:
            conn.close()


if __name__ == "__main__":
    main()
