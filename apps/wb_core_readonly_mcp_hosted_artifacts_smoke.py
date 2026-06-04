"""Smoke-check repo-owned hosted artifacts for wb-core read-only MCP."""

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_ROOT = ROOT / "artifacts" / "wb_core_readonly_mcp"


def main() -> None:
    unit = _read("systemd/wb-core-readonly-mcp.service")
    env_example = _read("env/wb-core-readonly-mcp.env.example")
    nginx = _read("nginx/wb-core-readonly-mcp.localhost-proxy.example.conf")
    target = _read("input/hosted_service_target__example.json")
    setup = ARTIFACT_ROOT / "bin" / "setup_hosted_readonly_mcp.sh"

    _assert_contains(unit, "User=wb-core-readonly-mcp")
    _assert_contains(unit, "EnvironmentFile=/opt/wb-core-readonly-mcp/env/wb-core-readonly-mcp.env")
    _assert_contains(unit, "--host 127.0.0.1 --port 8766")
    _assert_contains(unit, "ReadOnlyPaths=/opt/wb-core-readonly-mcp/app /opt/wb-core-readonly-mcp/repo")
    _assert_not_contains(unit, "/opt/wb-core-runtime/app")
    _assert_not_contains(unit, "api.selleros.pro")

    _assert_contains(env_example, "replace-with-runtime-only-token")
    _assert_not_contains(env_example, "gho_")
    _assert_not_contains(env_example, "Bearer ")

    _assert_contains(nginx, "readonly-mcp.example.invalid")
    _assert_contains(nginx, "proxy_pass http://127.0.0.1:8766/mcp;")
    _assert_not_contains(nginx, "api.selleros.pro")

    _assert_contains(target, "\"repo_dir\": \"/opt/wb-core-readonly-mcp/repo\"")
    _assert_contains(target, "\"public_route_policy\": \"do_not_publish_under_api_selleros_pro\"")

    subprocess.run(["bash", "-n", str(setup)], check=True)
    setup_text = setup.read_text(encoding="utf-8")
    _assert_contains(setup_text, "pull --ff-only")
    _assert_not_contains(setup_text, "reset --hard")
    _assert_not_contains(setup_text, "rm -rf")
    _assert_not_contains(setup_text, "/opt/wb-core-runtime/app")
    print("wb-core-readonly-mcp hosted artifacts smoke passed")


def _read(relative: str) -> str:
    return (ARTIFACT_ROOT / relative).read_text(encoding="utf-8")


def _assert_contains(text: str, needle: str) -> None:
    if needle not in text:
        raise AssertionError(f"missing expected text: {needle}")


def _assert_not_contains(text: str, needle: str) -> None:
    if needle in text:
        raise AssertionError(f"forbidden text present: {needle}")


if __name__ == "__main__":
    main()
