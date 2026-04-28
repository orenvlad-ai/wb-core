"""Smoke-check repo-owned public route allowlist rendering for hosted runtime."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import apps.registry_upload_http_entrypoint_hosted_runtime as hosted_runtime  # noqa: E402


SCRIPT = ROOT / "apps" / "registry_upload_http_entrypoint_hosted_runtime.py"
MANIFEST = ROOT / "artifacts" / "registry_upload_http_entrypoint" / "nginx" / "public_route_allowlist.json"


def main() -> None:
    manifest = hosted_runtime.load_public_route_manifest(MANIFEST)
    routes = hosted_runtime._validated_public_routes(manifest)
    route_paths = {route["path"] for route in routes}
    required_paths = {
        "/sheet-vitrina-v1/vitrina",
        "/sheet-vitrina-v1/operator",
        "/v1/registry-upload/bundle",
        "/v1/cost-price/upload",
        "/v1/sheet-vitrina-v1/status",
        "/v1/sheet-vitrina-v1/job",
        "/v1/sheet-vitrina-v1/web-vitrina",
        "/v1/sheet-vitrina-v1/web-vitrina/group-refresh",
        "/v1/sheet-vitrina-v1/refresh",
        "/v1/sheet-vitrina-v1/daily-report",
        "/v1/sheet-vitrina-v1/stock-report",
        "/v1/sheet-vitrina-v1/plan-report",
        "/v1/sheet-vitrina-v1/plan-report/baseline-template.xlsx",
        "/v1/sheet-vitrina-v1/plan-report/baseline-upload",
        "/v1/sheet-vitrina-v1/plan-report/baseline-status",
        "/v1/sheet-vitrina-v1/seller-portal-session/check",
        "/v1/sheet-vitrina-v1/feedbacks",
        "/v1/sheet-vitrina-v1/feedbacks/ai-prompt",
        "/v1/sheet-vitrina-v1/feedbacks/ai-analyze",
        "/v1/sheet-vitrina-v1/supply/factory-order/",
        "/v1/sheet-vitrina-v1/supply/wb-regional/",
    }
    missing = sorted(required_paths - route_paths)
    if missing:
        raise AssertionError(f"public route allowlist missing required paths: {missing}")

    rendered = hosted_runtime.render_nginx_public_route_block(
        manifest,
        proxy_pass_url="http://127.0.0.1:8765",
    )
    if rendered.count("location = /v1/sheet-vitrina-v1/feedbacks {") != 1:
        raise AssertionError("rendered nginx block must include feedbacks exactly once")
    if rendered.count("location = /v1/sheet-vitrina-v1/feedbacks/ai-prompt {") != 1:
        raise AssertionError("rendered nginx block must include feedbacks AI prompt exactly once")
    if rendered.count("location = /v1/sheet-vitrina-v1/feedbacks/ai-analyze {") != 1:
        raise AssertionError("rendered nginx block must include feedbacks AI analyze exactly once")
    if rendered.count("location ^~ /v1/sheet-vitrina-v1/supply/factory-order/ {") != 1:
        raise AssertionError("rendered nginx block must include factory-order prefix exactly once")

    sample_nginx = """server {
    server_name api.selleros.pro;

    location /v1/sheet-vitrina-v1/status {
        proxy_pass http://127.0.0.1:8765;
    }

    location = /v1/sheet-vitrina-v1/feedbacks {
        proxy_pass http://127.0.0.1:8765;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
    }
}
"""
    first_apply = hosted_runtime.apply_managed_nginx_public_routes_to_text(
        sample_nginx,
        managed_block=rendered,
        routes=routes,
        server_name="api.selleros.pro",
    )
    second_apply = hosted_runtime.apply_managed_nginx_public_routes_to_text(
        first_apply,
        managed_block=rendered,
        routes=routes,
        server_name="api.selleros.pro",
    )
    if first_apply != second_apply:
        raise AssertionError("nginx route allowlist application must be idempotent")
    if first_apply.count("location = /v1/sheet-vitrina-v1/feedbacks {") != 1:
        raise AssertionError("managed nginx output must not duplicate feedbacks location")
    if "location / {" not in first_apply:
        raise AssertionError("managed nginx output must preserve unrelated fallback location")

    with TemporaryDirectory(prefix="hosted-public-routes-smoke-") as tmp:
        target_file = Path(tmp) / "target.json"
        target_file.write_text(
            json.dumps(
                {
                    "target_id": "public_routes_smoke",
                    "public_base_url": "https://api.selleros.pro",
                    "loopback_base_url": "http://127.0.0.1:8765",
                    "ssh_destination": "example-host",
                    "target_dir": "/opt/wb-core-runtime/app",
                    "service_name": "wb-core-registry-http.service",
                    "restart_command": "systemctl restart wb-core-registry-http.service",
                    "status_command": "systemctl status --no-pager --full wb-core-registry-http.service",
                    "environment_file": "/opt/wb-ai/.env",
                    "systemd_unit_directory": "/etc/systemd/system",
                    "systemd_units_source_dir": "artifacts/registry_upload_http_entrypoint/systemd",
                    "nginx_public_routes": {
                        "server_config_path": "/etc/nginx/sites-enabled/wb-ai",
                        "backup_dir": "/etc/nginx/sites-enabled",
                        "test_command": "nginx -t",
                        "reload_command": "systemctl reload nginx",
                        "manifest_path": "artifacts/registry_upload_http_entrypoint/nginx/public_route_allowlist.json",
                    },
                    "managed_systemd_units": [],
                    "runtime_env": {
                        "REGISTRY_UPLOAD_HTTP_HOST": "127.0.0.1",
                        "REGISTRY_UPLOAD_HTTP_PORT": "8765",
                        "REGISTRY_UPLOAD_RUNTIME_DIR": "/opt/wb-core-runtime/state",
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print_plan = _run_json([sys.executable, str(SCRIPT), "--target-file", str(target_file), "print-plan"])
        plan_routes = print_plan["deploy_plan"]["nginx_public_routes"]["routes"]
        if "/v1/sheet-vitrina-v1/feedbacks" not in {route["path"] for route in plan_routes}:
            raise AssertionError("print-plan must expose feedbacks in nginx public routes")
        dry_run = _run_json(
            [
                sys.executable,
                str(SCRIPT),
                "--target-file",
                str(target_file),
                "deploy",
                "--dry-run",
                "--allow-dirty",
            ]
        )
        command_text = " ".join(dry_run["commands"]["nginx_public_routes_update"])
        if "apply-nginx-routes" not in command_text:
            raise AssertionError("deploy dry-run must include nginx route update command")

    print(f"public_route_manifest: ok -> {len(routes)} routes")
    print("public_route_render: ok -> feedbacks and supply prefixes included")
    print("public_route_apply_idempotent: ok")
    print("public_route_deploy_dry_run: ok")


def _run_json(command: list[str]) -> dict[str, object]:
    result = subprocess.run(command, cwd=ROOT, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(
            "command failed: "
            + " ".join(command)
            + f"\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise AssertionError("runner must emit a JSON object")
    return payload


if __name__ == "__main__":
    main()
