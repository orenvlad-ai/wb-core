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
EU_TARGET = (
    ROOT
    / "artifacts"
    / "registry_upload_http_entrypoint"
    / "input"
    / "hosted_runtime_target__europe_api.json"
)


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
        "/v1/sheet-vitrina-v1/web-vitrina/seller-portal-recovery/start",
        "/v1/sheet-vitrina-v1/refresh",
        "/v1/sheet-vitrina-v1/daily-report",
        "/v1/sheet-vitrina-v1/stock-report",
        "/v1/sheet-vitrina-v1/plan-report",
        "/v1/sheet-vitrina-v1/plan-report/baseline-template.xlsx",
        "/v1/sheet-vitrina-v1/plan-report/baseline-upload",
        "/v1/sheet-vitrina-v1/plan-report/baseline-status",
        "/v1/sheet-vitrina-v1/seller-portal-session/check",
        "/v1/sheet-vitrina-v1/feedbacks",
        "/v1/sheet-vitrina-v1/feedbacks/export.xlsx",
        "/v1/sheet-vitrina-v1/feedbacks/ai-prompt",
        "/v1/sheet-vitrina-v1/feedbacks/ai-analyze",
        "/v1/sheet-vitrina-v1/feedbacks/complaints",
        "/v1/sheet-vitrina-v1/feedbacks/complaints/sync-status",
        "/v1/sheet-vitrina-v1/feedbacks/complaints/sync-status/job",
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
    if rendered.count("location = /v1/sheet-vitrina-v1/feedbacks/export.xlsx {") != 1:
        raise AssertionError("rendered nginx block must include feedbacks export exactly once")
    if rendered.count("location = /v1/sheet-vitrina-v1/feedbacks/ai-prompt {") != 1:
        raise AssertionError("rendered nginx block must include feedbacks AI prompt exactly once")
    if rendered.count("location = /v1/sheet-vitrina-v1/feedbacks/ai-analyze {") != 1:
        raise AssertionError("rendered nginx block must include feedbacks AI analyze exactly once")
    if rendered.count("location = /v1/sheet-vitrina-v1/feedbacks/complaints {") != 1:
        raise AssertionError("rendered nginx block must include feedbacks complaints exactly once")
    if rendered.count("location = /v1/sheet-vitrina-v1/feedbacks/complaints/sync-status {") != 1:
        raise AssertionError("rendered nginx block must include feedbacks complaints sync exactly once")
    if rendered.count("location = /v1/sheet-vitrina-v1/feedbacks/complaints/sync-status/job {") != 1:
        raise AssertionError("rendered nginx block must include feedbacks complaints sync job exactly once")
    if rendered.count("location ^~ /v1/sheet-vitrina-v1/supply/factory-order/ {") != 1:
        raise AssertionError("rendered nginx block must include factory-order prefix exactly once")
    if rendered.count("location = /v1/sheet-vitrina-v1/web-vitrina/seller-portal-recovery/start {") != 1:
        raise AssertionError("rendered nginx block must include web-vitrina seller recovery start exactly once")

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

    pre_cutover_nginx = """server {
    listen 80;
    server_name 89.191.226.88;
}
"""
    pre_cutover_apply = hosted_runtime.apply_managed_nginx_public_routes_to_text(
        pre_cutover_nginx,
        managed_block=rendered,
        routes=routes,
        server_names=("89.191.226.88", "api.selleros.pro"),
    )
    if "server_name 89.191.226.88 api.selleros.pro;" not in pre_cutover_apply:
        raise AssertionError("managed nginx output must publish explicit pre-cutover and future production host names")
    if pre_cutover_apply.count("server_name ") != 1:
        raise AssertionError("managed nginx output must rewrite the target server_name line without duplicating it")

    tls_config = hosted_runtime.NginxTlsConfig(
        listen=("443 ssl",),
        certificate_path="/etc/letsencrypt/live/api.selleros.pro/fullchain.pem",
        certificate_key_path="/etc/letsencrypt/live/api.selleros.pro/privkey.pem",
    )
    tls_rendered = hosted_runtime.render_nginx_tls_block(tls_config)
    pre_cutover_https_apply = hosted_runtime.apply_managed_nginx_public_routes_to_text(
        pre_cutover_nginx,
        managed_block=rendered,
        tls_block=tls_rendered,
        routes=routes,
        server_names=("89.191.226.88", "api.selleros.pro"),
    )
    second_https_apply = hosted_runtime.apply_managed_nginx_public_routes_to_text(
        pre_cutover_https_apply,
        managed_block=rendered,
        tls_block=tls_rendered,
        routes=routes,
        server_names=("89.191.226.88", "api.selleros.pro"),
    )
    if pre_cutover_https_apply != second_https_apply:
        raise AssertionError("managed nginx TLS + route application must be idempotent")
    if pre_cutover_https_apply.count("# BEGIN WB-CORE MANAGED TLS") != 1:
        raise AssertionError("managed nginx output must include one TLS block")
    if "listen 443 ssl;" not in pre_cutover_https_apply:
        raise AssertionError("managed nginx output must listen on 443 ssl")
    if "ssl_certificate /etc/letsencrypt/live/api.selleros.pro/fullchain.pem;" not in pre_cutover_https_apply:
        raise AssertionError("managed nginx output must include configured certificate path")
    if "ssl_certificate_key /etc/letsencrypt/live/api.selleros.pro/privkey.pem;" not in pre_cutover_https_apply:
        raise AssertionError("managed nginx output must include configured certificate key path")

    with TemporaryDirectory(prefix="hosted-public-routes-smoke-") as tmp:
        target_file = Path(tmp) / "target.json"
        target_file.write_text(
            json.dumps(
                {
                    "target_status": "active",
                    "target_id": "public_routes_smoke",
                    "public_base_url": "http://89.191.226.88",
                    "loopback_base_url": "http://127.0.0.1:8765",
                    "ssh_destination": "wb-core-eu-root",
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
                        "server_names": ["127.0.0.1", "89.191.226.88"],
                        "tls": {
                            "listen": ["443 ssl"],
                            "certificate_path": "/etc/letsencrypt/live/api.selleros.pro/fullchain.pem",
                            "certificate_key_path": "/etc/letsencrypt/live/api.selleros.pro/privkey.pem",
                        },
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
        if "/v1/sheet-vitrina-v1/feedbacks/export.xlsx" not in {route["path"] for route in plan_routes}:
            raise AssertionError("print-plan must expose feedbacks export in nginx public routes")
        plan_server_names = print_plan["deploy_plan"]["nginx_public_routes"]["server_names"]
        if plan_server_names != ["127.0.0.1", "89.191.226.88"]:
            raise AssertionError(f"print-plan must expose configured nginx server_names, got {plan_server_names}")
        plan_tls = print_plan["deploy_plan"]["nginx_public_routes"]["tls"]
        if not plan_tls["configured"] or plan_tls["listen"] != ["443 ssl"]:
            raise AssertionError(f"print-plan must expose configured nginx TLS, got {plan_tls}")
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

    eu_target = hosted_runtime.load_hosted_runtime_target(EU_TARGET)
    if eu_target.public_base_url != "https://api.selleros.pro":
        raise AssertionError(f"EU target must probe the current HTTPS domain, got {eu_target.public_base_url}")
    if eu_target.nginx_public_routes is None:
        raise AssertionError("EU target must publish managed nginx public routes")
    eu_server_names = list(hosted_runtime._nginx_server_names_for_target(eu_target))
    if eu_server_names != ["89.191.226.88", "api.selleros.pro"]:
        raise AssertionError(f"EU target must publish IP and current domain server_names, got {eu_server_names}")
    eu_tls = eu_target.nginx_public_routes.tls
    if eu_tls is None or eu_tls.listen != ("443 ssl",):
        raise AssertionError(f"EU target must publish managed TLS on 443 ssl, got {eu_tls}")
    if eu_tls.certificate_path != "/etc/letsencrypt/live/api.selleros.pro/fullchain.pem":
        raise AssertionError("EU target must reference the api.selleros.pro certificate chain path")
    if eu_tls.certificate_key_path != "/etc/letsencrypt/live/api.selleros.pro/privkey.pem":
        raise AssertionError("EU target must reference the api.selleros.pro private key path without reading it")
    eu_print_plan = _run_json(
        [
            sys.executable,
            str(SCRIPT),
            "--target-file",
            str(EU_TARGET),
            "print-plan",
        ]
    )
    if eu_print_plan["deploy_plan"]["public_base_url"] != "https://api.selleros.pro":
        raise AssertionError("EU print-plan must expose current HTTPS production URL")
    if eu_print_plan["deploy_plan"]["target_role"] != "primary_live":
        raise AssertionError("EU print-plan must expose primary_live target_role")
    if eu_print_plan["deploy_plan"]["target_lifecycle"] != "current_live":
        raise AssertionError("EU print-plan must expose current_live lifecycle")
    eu_plan_routes = eu_print_plan["deploy_plan"]["nginx_public_routes"]
    if eu_plan_routes["server_names"] != ["89.191.226.88", "api.selleros.pro"]:
        raise AssertionError("EU print-plan must expose current domain and IP server_names")
    if eu_plan_routes["tls"]["configured"] is not True:
        raise AssertionError("EU print-plan must expose configured managed TLS")
    if eu_print_plan["deploy_plan"]["target_action_blockers"]:
        raise AssertionError(
            f"EU print-plan must have no target action blockers, got {eu_print_plan['deploy_plan']['target_action_blockers']}"
        )

    with TemporaryDirectory(prefix="hosted-current-live-invariant-smoke-") as tmp:
        invariant_dir = Path(tmp)
        missing_domain = _write_current_live_fixture(
            invariant_dir / "missing_domain.json",
            server_names=["89.191.226.88"],
        )
        missing_tls = _write_current_live_fixture(
            invariant_dir / "missing_tls.json",
            tls=None,
        )
        ip_http_only = _write_current_live_fixture(
            invariant_dir / "ip_http_only.json",
            public_base_url="http://89.191.226.88",
        )
        _assert_current_live_invariant_failure(
            [
                sys.executable,
                str(SCRIPT),
                "--target-file",
                str(missing_domain),
                "deploy",
                "--allow-dirty",
            ],
            required_fragments=[
                "current live EU target must publish `https://api.selleros.pro`",
                "required server_names: `89.191.226.88`, `api.selleros.pro`",
                "nginx_public_routes.server_names",
            ],
        )
        _assert_current_live_invariant_failure(
            [
                sys.executable,
                str(SCRIPT),
                "--target-file",
                str(missing_tls),
                "apply-nginx-routes",
            ],
            required_fragments=[
                "required TLS: `listen 443 ssl` with LetsEncrypt paths",
                "nginx_public_routes.tls=<missing>",
            ],
        )
        _assert_current_live_invariant_failure(
            [
                sys.executable,
                str(SCRIPT),
                "--target-file",
                str(ip_http_only),
                "deploy-and-verify",
                "--allow-dirty",
                "--skip-refresh",
            ],
            required_fragments=[
                "current live EU target must publish `https://api.selleros.pro`",
                "public_base_url=http://89.191.226.88",
            ],
        )

    print(f"public_route_manifest: ok -> {len(routes)} routes")
    print("public_route_render: ok -> feedbacks and supply prefixes included")
    print("public_route_apply_idempotent: ok")
    print("public_route_server_names: ok -> explicit active IP host names rendered")
    print("public_route_tls: ok -> explicit managed 443 ssl block rendered")
    print("public_route_deploy_dry_run: ok")
    print("public_route_eu_target_https_domain: ok -> api.selleros.pro + TLS configured")
    print("current_live_publication_invariant: ok -> bad current-live targets fail before mutation")


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


def _write_current_live_fixture(
    path: Path,
    *,
    public_base_url: str = "https://api.selleros.pro",
    server_names: list[str] | None = None,
    tls: dict[str, object] | None | bool = True,
) -> Path:
    nginx_public_routes: dict[str, object] = {
        "server_config_path": "/tmp/wb-core-current-live-invariant-smoke-nginx.conf",
        "backup_dir": "/tmp",
        "test_command": "nginx -t",
        "reload_command": "systemctl reload nginx",
        "manifest_path": "artifacts/registry_upload_http_entrypoint/nginx/public_route_allowlist.json",
        "server_names": server_names or ["89.191.226.88", "api.selleros.pro"],
    }
    if tls is True:
        nginx_public_routes["tls"] = {
            "listen": ["443 ssl"],
            "certificate_path": "/etc/letsencrypt/live/api.selleros.pro/fullchain.pem",
            "certificate_key_path": "/etc/letsencrypt/live/api.selleros.pro/privkey.pem",
        }
    elif isinstance(tls, dict):
        nginx_public_routes["tls"] = tls

    path.write_text(
        json.dumps(
            {
                "target_status": "active",
                "target_role": "primary_live",
                "target_lifecycle": "current_live",
                "mutation_policy": "routine_writes_allowed",
                "host_ip": "89.191.226.88",
                "public_domain": "api.selleros.pro",
                "target_id": "current_live_invariant_smoke",
                "public_base_url": public_base_url,
                "loopback_base_url": "http://127.0.0.1:8765",
                "ssh_destination": "wb-core-eu-root",
                "target_dir": "/opt/wb-core-runtime/app",
                "service_name": "wb-core-registry-http.service",
                "restart_command": "systemctl restart wb-core-registry-http.service",
                "status_command": "systemctl status --no-pager --full wb-core-registry-http.service",
                "environment_file": "/opt/wb-ai/.env",
                "systemd_unit_directory": "/etc/systemd/system",
                "systemd_units_source_dir": "artifacts/registry_upload_http_entrypoint/systemd",
                "nginx_public_routes": nginx_public_routes,
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
    return path


def _assert_current_live_invariant_failure(command: list[str], *, required_fragments: list[str]) -> None:
    result = subprocess.run(command, cwd=ROOT, check=False, capture_output=True, text=True)
    if result.returncode == 0:
        raise AssertionError(f"command unexpectedly succeeded: {' '.join(command)}")
    output = result.stdout + result.stderr
    for fragment in required_fragments:
        if fragment not in output:
            raise AssertionError(
                f"current-live invariant failure missing {fragment!r}; output was:\n{output}"
            )


if __name__ == "__main__":
    main()
