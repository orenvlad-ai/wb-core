"""Targeted smoke-check for the wb-core read-only MCP boundary."""

from pathlib import Path
import json
import os
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.wb_core_readonly_mcp import (  # noqa: E402
    McpJsonRpcServer,
    ReadonlyMcpConfig,
    RepoReadService,
    build_server,
)


EXPECTED_TOOLS = {
    "repo_status",
    "list_tree",
    "find_files",
    "search_text",
    "read_file",
    "read_file_range",
    "get_file_metadata",
}
FORBIDDEN_TOOL_WORDS = {
    "commit",
    "delete",
    "deploy",
    "exec",
    "merge",
    "push",
    "shell",
    "ssh",
    "write",
}


def main() -> None:
    server = build_server(ReadonlyMcpConfig(repo_root=ROOT))
    _assert_tool_listing(server)
    _assert_success_paths(server)
    _assert_repo_refusals(server)
    _assert_temp_policy_edges()
    print("wb-core-readonly-mcp smoke passed")


def _assert_tool_listing(server: McpJsonRpcServer) -> None:
    response = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    if response is None:
        raise AssertionError("tools/list must return a response")
    tools = response["result"]["tools"]
    names = {tool["name"] for tool in tools}
    if names != EXPECTED_TOOLS:
        raise AssertionError(f"unexpected MCP tools: {sorted(names)}")
    forbidden = [name for name in names for word in FORBIDDEN_TOOL_WORDS if word in name]
    if forbidden:
        raise AssertionError(f"mutation-like tools must be absent, got {forbidden}")


def _assert_success_paths(server: McpJsonRpcServer) -> None:
    _require_ok(server.call_tool("repo_status", {}), "repo_status")

    tree = _require_ok(
        server.call_tool("list_tree", {"path": "docs/architecture", "max_depth": 1}),
        "list_tree",
    )
    tree_paths = {entry["path"] for entry in tree["entries"]}
    if "docs/architecture/11_wb_core_readonly_mcp_contract.md" not in tree_paths:
        raise AssertionError("list_tree must include the read-only MCP contract doc")

    found = _require_ok(
        server.call_tool(
            "find_files",
            {"path": "docs", "pattern": "11_wb_core_readonly_mcp_contract.md"},
        ),
        "find_files",
    )
    if not found["matches"]:
        raise AssertionError("find_files must find the contract doc")

    search = _require_ok(
        server.call_tool(
            "search_text",
            {
                "path": "docs/architecture",
                "query": "WB-Core Read-Only MCP Contract",
                "max_matches": 3,
            },
        ),
        "search_text",
    )
    if search["match_count"] < 1:
        raise AssertionError("search_text must find the contract heading")

    readme = _require_ok(server.call_tool("read_file", {"path": "README.md"}), "read_file")
    if "# wb-core" not in readme["text"]:
        raise AssertionError("read_file must return README text")

    ranged = _require_ok(
        server.call_tool(
            "read_file_range",
            {
                "path": "docs/architecture/11_wb_core_readonly_mcp_contract.md",
                "start_line": 1,
                "end_line": 6,
            },
        ),
        "read_file_range",
    )
    if "WB-Core Read-Only MCP Contract" not in ranged["text"]:
        raise AssertionError("read_file_range must return selected lines")

    metadata = _require_ok(
        server.call_tool("get_file_metadata", {"path": "README.md"}),
        "get_file_metadata",
    )
    if metadata["metadata"]["type"] not in {"text_file", "large_file"}:
        raise AssertionError("get_file_metadata must classify README")


def _assert_repo_refusals(server: McpJsonRpcServer) -> None:
    _require_error(
        server.call_tool("read_file", {"path": "../README.md"}),
        {"denied_outside_repo"},
        "path traversal",
    )
    _require_error(
        server.call_tool("read_file", {"path": ".git/config"}),
        {"denied_generated_or_private_path"},
        "git internals",
    )
    _require_error(
        server.call_tool("read_file", {"path": ".env"}),
        {"denied_sensitive_path"},
        "env path",
    )
    _require_error(
        server.call_tool("read_file", {"path": "wb_core_docs_master/00_INDEX__WEBCORE_PROJECT_DOCS.md"}),
        {"denied_derived_pack"},
        "derived docs pack",
    )
    _require_error(
        server.call_tool("write_file", {"path": "README.md", "text": "nope"}),
        {"unknown_tool"},
        "unknown mutation tool",
    )


def _assert_temp_policy_edges() -> None:
    with TemporaryDirectory(prefix="wb-core-readonly-mcp-smoke-") as tmp:
        root = Path(tmp) / "repo"
        docs = root / "docs"
        docs.mkdir(parents=True)
        (root / ".git").mkdir()
        (root / "README.md").write_text("# temp\n", encoding="utf-8")

        outside = Path(tmp) / "outside.txt"
        outside.write_text("outside secret\n", encoding="utf-8")
        os.symlink(outside, docs / "escape.txt")

        (docs / "large.txt").write_text("0123456789abcdef\n", encoding="utf-8")
        (docs / "binary.bin").write_bytes(b"abc\x00def")
        (docs / "redaction.md").write_text(
            "password = hunter2\nAuthorization: Bearer abcdefghijklmnop\n",
            encoding="utf-8",
        )

        tight = build_server(ReadonlyMcpConfig(repo_root=root, max_file_bytes=8))
        _require_error(
            tight.call_tool("read_file", {"path": "docs/large.txt"}),
            {"denied_size_limit"},
            "large file",
        )

        server = build_server(ReadonlyMcpConfig(repo_root=root, max_file_bytes=1024))
        _require_error(
            server.call_tool("read_file", {"path": "docs/escape.txt"}),
            {"denied_symlink_escape"},
            "symlink escape",
        )
        _require_error(
            server.call_tool("read_file", {"path": "docs/binary.bin"}),
            {"denied_binary"},
            "binary file",
        )
        redacted = _require_ok(
            server.call_tool("read_file", {"path": "docs/redaction.md"}),
            "redaction",
        )
        text = redacted["text"]
        if "hunter2" in text or "abcdefghijklmnop" in text:
            raise AssertionError("read_file must redact secret-like values before returning text")
        if "[REDACTED]" not in text:
            raise AssertionError("redacted output must contain a redaction marker")


def _require_ok(payload: dict[str, object], label: str) -> dict[str, object]:
    if not payload.get("ok"):
        raise AssertionError(f"{label} expected ok payload, got {json.dumps(payload, ensure_ascii=False)}")
    return payload


def _require_error(payload: dict[str, object], expected_errors: set[str], label: str) -> dict[str, object]:
    if payload.get("ok") is not False:
        raise AssertionError(f"{label} expected refusal, got {json.dumps(payload, ensure_ascii=False)}")
    error = str(payload.get("error"))
    if error not in expected_errors:
        raise AssertionError(f"{label} expected {sorted(expected_errors)}, got {error}: {payload}")
    return payload


if __name__ == "__main__":
    main()
