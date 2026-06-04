"""Local-only smoke-check for HTTP remote mode of wb-core read-only MCP."""

from pathlib import Path
import http.client
import json
import os
import subprocess
import sys
import threading
from tempfile import TemporaryDirectory
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.wb_core_readonly_mcp_smoke import EXPECTED_TOOLS, FORBIDDEN_TOOL_WORDS  # noqa: E402
from packages.application.wb_core_readonly_mcp import (  # noqa: E402
    ReadonlyMcpConfig,
    build_http_server,
)


AUTH_ENV = "WB_CORE_READONLY_MCP_REMOTE_SMOKE_TOKEN"
AUTH_TOKEN = "remote-smoke-token"


def main() -> None:
    previous_token = os.environ.get(AUTH_ENV)
    os.environ[AUTH_ENV] = AUTH_TOKEN
    try:
        with TemporaryDirectory(prefix="wb-core-readonly-mcp-remote-smoke-") as tmp:
            repo_root = Path(tmp) / "managed-clone" / "wb-core"
            _create_managed_clone_fixture(repo_root)
            config = ReadonlyMcpConfig(
                repo_root=repo_root,
                source_mode="managed_clone",
                repo_url="https://github.com/orenvlad-ai/wb-core.git",
                branch="main",
                refresh_policy="external_manual",
                remote_auth_token_env=AUTH_ENV,
            )
            http_server = build_http_server(config, host="127.0.0.1", port=0)
            thread = threading.Thread(target=http_server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()
            try:
                port = int(http_server.server_address[1])
                _assert_auth_required(port)
                _assert_health_and_sse(port)
                _assert_jsonrpc_flow(port)
            finally:
                http_server.shutdown()
                thread.join(timeout=5)
                http_server.server_close()
    finally:
        if previous_token is None:
            os.environ.pop(AUTH_ENV, None)
        else:
            os.environ[AUTH_ENV] = previous_token
    print("wb-core-readonly-mcp remote smoke passed")


def _create_managed_clone_fixture(repo_root: Path) -> None:
    (repo_root / "docs" / "architecture").mkdir(parents=True)
    (repo_root / "packages" / "application").mkdir(parents=True)
    (repo_root / "README.md").write_text("# wb-core remote smoke\n", encoding="utf-8")
    (repo_root / "docs" / "architecture" / "remote.md").write_text(
        "# Remote MCP Smoke\n\nSearch needle: managed clone source.\n",
        encoding="utf-8",
    )
    (repo_root / "packages" / "application" / "sample.py").write_text(
        "VALUE = 'read-only'\n",
        encoding="utf-8",
    )
    _git(repo_root, ["init", "-b", "main"])
    _git(repo_root, ["remote", "add", "origin", "https://github.com/orenvlad-ai/wb-core.git"])
    _git(repo_root, ["add", "README.md", "docs/architecture/remote.md", "packages/application/sample.py"])
    _git(repo_root, ["-c", "user.name=Smoke", "-c", "user.email=smoke@example.invalid", "commit", "-m", "fixture"])


def _git(repo_root: Path, args: list[str]) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=10,
    )


def _assert_auth_required(port: int) -> None:
    status, payload = _post_jsonrpc(port, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, auth=False)
    if status != 401 or payload.get("error") != "unauthorized":
        raise AssertionError(f"unauthenticated request must be rejected, got {status} {payload}")


def _assert_health_and_sse(port: int) -> None:
    status, health = _get_json(port, "/healthz", auth=False)
    if status != 200 or not health.get("ok") or not health.get("auth_required"):
        raise AssertionError(f"healthz must report auth-required HTTP MCP, got {status} {health}")

    status, body = _get_text(port, "/sse", auth=True)
    if status != 200 or "event: endpoint" not in body or "data: /mcp" not in body:
        raise AssertionError(f"sse descriptor must expose /mcp endpoint, got {status} {body!r}")


def _assert_jsonrpc_flow(port: int) -> None:
    status, initialized = _post_jsonrpc(
        port,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "smoke"}},
        },
    )
    if status != 200 or initialized["result"]["serverInfo"]["name"] != "wb-core-readonly-mcp":
        raise AssertionError(f"initialize failed: {status} {initialized}")

    status, tools_payload = _post_jsonrpc(port, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    if status != 200:
        raise AssertionError(f"tools/list HTTP status failed: {status} {tools_payload}")
    names = {tool["name"] for tool in tools_payload["result"]["tools"]}
    if names != EXPECTED_TOOLS:
        raise AssertionError(f"unexpected remote tools: {sorted(names)}")
    forbidden = [name for name in names for word in FORBIDDEN_TOOL_WORDS if word in name]
    if forbidden:
        raise AssertionError(f"mutation-like tools must be absent, got {forbidden}")

    repo_status = _call_tool(port, "repo_status", {})
    if repo_status["source_mode"] != "managed_clone":
        raise AssertionError(f"repo_status must report managed_clone source, got {repo_status}")
    if repo_status["configured_branch"] != "main" or not repo_status["commit"]:
        raise AssertionError(f"repo_status must report branch/commit freshness, got {repo_status}")

    readme = _call_tool(port, "read_file", {"path": "README.md"})
    if "# wb-core remote smoke" not in readme["text"]:
        raise AssertionError(f"read_file returned unexpected text: {readme}")

    search = _call_tool(port, "search_text", {"path": "docs", "query": "managed clone source"})
    if search["match_count"] != 1:
        raise AssertionError(f"search_text must find fixture text, got {search}")

    denied = _call_tool(port, "read_file", {"path": ".env"})
    if denied.get("ok") is not False or denied.get("error") != "denied_sensitive_path":
        raise AssertionError(f"remote mode must preserve denied path behavior, got {denied}")

    unknown = _call_tool(port, "write_file", {"path": "README.md", "text": "nope"})
    if unknown.get("ok") is not False or unknown.get("error") != "unknown_tool":
        raise AssertionError(f"remote mode must not expose mutation tools, got {unknown}")


def _call_tool(port: int, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    status, response = _post_jsonrpc(
        port,
        {
            "jsonrpc": "2.0",
            "id": 100,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    if status != 200:
        raise AssertionError(f"tools/call {name} returned HTTP {status}: {response}")
    text = response["result"]["content"][0]["text"]
    return json.loads(text)


def _post_jsonrpc(port: int, payload: dict[str, Any], *, auth: bool = True) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = f"Bearer {AUTH_TOKEN}"
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("POST", "/mcp", body=body, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
        return response.status, parsed
    finally:
        conn.close()


def _get_json(port: int, path: str, *, auth: bool) -> tuple[int, dict[str, Any]]:
    status, body = _get_text(port, path, auth=auth)
    return status, json.loads(body)


def _get_text(port: int, path: str, *, auth: bool) -> tuple[int, str]:
    headers = {}
    if auth:
        headers["Authorization"] = f"Bearer {AUTH_TOKEN}"
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        body = response.read().decode("utf-8")
        return response.status, body
    finally:
        conn.close()


if __name__ == "__main__":
    main()
