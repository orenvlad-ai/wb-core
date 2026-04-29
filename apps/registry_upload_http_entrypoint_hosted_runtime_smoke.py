"""Smoke-check for hosted runtime deploy/probe contract."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from urllib import request as urllib_request


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "apps" / "registry_upload_http_entrypoint_hosted_runtime.py"
LIVE_RUNNER = ROOT / "apps" / "registry_upload_http_entrypoint_live.py"
INPUT_BUNDLE = ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import apps.registry_upload_http_entrypoint_hosted_runtime as hosted_runtime  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.contracts.sheet_vitrina_v1 import (  # noqa: E402
    SheetVitrinaV1Envelope,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
)

STATUS_HEADER = [
    "source_key",
    "kind",
    "freshness",
    "snapshot_date",
    "date",
    "date_from",
    "date_to",
    "requested_count",
    "covered_count",
    "missing_nm_ids",
    "note",
]


def main() -> None:
    with TemporaryDirectory(prefix="hosted-runtime-contract-smoke-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        target_file = Path(tmp) / "target.json"
        deploy_target_file = Path(tmp) / "deploy_target.json"
        archived_target_file = Path(tmp) / "archived_target.json"
        port = _reserve_free_port()
        base_url = f"http://127.0.0.1:{port}"
        base_target_payload = {
            "target_status": "local_test",
            "target_id": "local_smoke_target",
            "public_base_url": base_url,
            "loopback_base_url": base_url,
            "ssh_destination": "",
            "target_dir": "/srv/wb-core",
            "service_name": "wb-core-registry-upload",
            "restart_command": "sudo systemctl restart wb-core-registry-upload",
            "status_command": "sudo systemctl status --no-pager wb-core-registry-upload",
            "environment_file": "/etc/wb-core/registry-upload.env",
            "systemd_unit_directory": "/etc/systemd/system",
            "systemd_units_source_dir": "artifacts/registry_upload_http_entrypoint/systemd",
            "nginx_public_routes": {
                "server_config_path": "/etc/nginx/sites-enabled/wb-ai",
                "backup_dir": "/etc/nginx/sites-enabled",
                "test_command": "nginx -t",
                "reload_command": "systemctl reload nginx",
                "manifest_path": "artifacts/registry_upload_http_entrypoint/nginx/public_route_allowlist.json",
                "managed_block_label": "WB-CORE MANAGED PUBLIC ROUTES",
            },
            "managed_systemd_units": [
                {
                    "name": "wb-core-sheet-vitrina-refresh.service",
                    "enable": False,
                    "restart": False,
                },
                {
                    "name": "wb-core-sheet-vitrina-refresh.timer",
                    "enable": True,
                    "restart": True,
                },
            ],
            "runtime_env": {
                "REGISTRY_UPLOAD_HTTP_HOST": "127.0.0.1",
                "REGISTRY_UPLOAD_HTTP_PORT": str(port),
                "REGISTRY_UPLOAD_RUNTIME_DIR": str(runtime_dir),
                "REGISTRY_UPLOAD_HTTP_PATH": "/v1/registry-upload/bundle",
                "COST_PRICE_UPLOAD_HTTP_PATH": "/v1/cost-price/upload",
                "SHEET_VITRINA_HTTP_PATH": "/v1/sheet-vitrina-v1/plan",
                "SHEET_VITRINA_REFRESH_HTTP_PATH": "/v1/sheet-vitrina-v1/refresh",
                "SHEET_VITRINA_STATUS_HTTP_PATH": "/v1/sheet-vitrina-v1/status",
                "SHEET_VITRINA_OPERATOR_UI_PATH": "/sheet-vitrina-v1/operator",
            },
        }
        target_file.write_text(
            json.dumps(base_target_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        deploy_target_payload = dict(base_target_payload)
        deploy_target_payload.update(
            {
                "target_status": "active",
                "public_base_url": "https://api.selleros.pro",
                "ssh_destination": "wb-core-eu-root",
                "target_dir": "/opt/wb-core-runtime/app",
                "service_name": "wb-core-registry-http.service",
                "restart_command": "systemctl restart wb-core-registry-http.service",
                "status_command": "systemctl status --no-pager --full wb-core-registry-http.service",
            }
        )
        deploy_target_payload["runtime_env"] = {
            **base_target_payload["runtime_env"],
            "REGISTRY_UPLOAD_RUNTIME_DIR": "/opt/wb-core-runtime/state",
        }
        deploy_target_file.write_text(
            json.dumps(deploy_target_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        archived_target_payload = dict(deploy_target_payload)
        archived_target_payload.update(
            {
                "target_status": "archived",
                "target_role": "rollback_only",
                "target_lifecycle": "deprecated_live_target",
                "mutation_policy": "do_not_deploy_without_emergency_rollback_override",
                "provider_side_label_recommendation": "ROLLBACK-ONLY_DO-NOT-DEPLOY_wb-core-old-selleros",
                "target_id": "archived_selleros_target",
                "public_base_url": "https://api.selleros.pro",
                "ssh_destination": "selleros-root",
            }
        )
        archived_target_file.write_text(
            json.dumps(archived_target_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        env = os.environ.copy()
        env.update(
            {
                "REGISTRY_UPLOAD_HTTP_PORT": str(port),
                "REGISTRY_UPLOAD_RUNTIME_DIR": str(runtime_dir),
            }
        )
        process = subprocess.Popen(
            [sys.executable, str(LIVE_RUNNER)],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            _wait_until_ready(f"{base_url}/sheet-vitrina-v1/operator")
            _post_bundle(f"{base_url}/v1/registry-upload/bundle")
            _seed_ready_snapshot(runtime_dir)

            print_plan = _run_json(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--target-file",
                    str(target_file),
                    "print-plan",
                ]
            )
            if print_plan["deploy_plan"]["applicable_to_current_checkout_without_merge"] is not True:
                raise AssertionError("print-plan must confirm applicability to current checkout")
            if "WB_API_TOKEN" not in print_plan["required_secret_contract"]:
                raise AssertionError("print-plan must expose canonical secret contract")
            if "PROMO_XLSX_COLLECTOR_STORAGE_STATE_PATH" not in print_plan["optional_runtime_contract"]:
                raise AssertionError("print-plan must expose promo collector storage-state override contract")
            if "SELLER_PORTAL_CANONICAL_SUPPLIER_ID" not in print_plan["optional_runtime_contract"]:
                raise AssertionError("print-plan must expose canonical seller supplier id contract")
            if "SELLER_PORTAL_RELOGIN_SSH_DESTINATION" not in print_plan["optional_runtime_contract"]:
                raise AssertionError("print-plan must expose seller recovery SSH destination contract")
            if len(print_plan["deploy_plan"]["managed_systemd_units"]) != 2:
                raise AssertionError("print-plan must expose managed systemd units when configured")
            nginx_routes = print_plan["deploy_plan"].get("nginx_public_routes") or {}
            if nginx_routes.get("route_count", 0) < 20:
                raise AssertionError("print-plan must expose nginx public route allowlist")
            if "/v1/sheet-vitrina-v1/feedbacks" not in {item["path"] for item in nginx_routes.get("routes", [])}:
                raise AssertionError("nginx public route allowlist must include feedbacks route")

            deploy_dry_run = _run_json(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--target-file",
                    str(deploy_target_file),
                    "deploy",
                    "--dry-run",
                    "--allow-dirty",
                ]
            )
            if deploy_dry_run["dry_run"] is not True:
                raise AssertionError("deploy --dry-run must stay dry-run")
            if "rsync" not in " ".join(deploy_dry_run["commands"]["rsync"]):
                raise AssertionError("deploy --dry-run must expose rsync command")
            if "chown -R root:root /opt/wb-core-runtime/app" not in " ".join(
                deploy_dry_run["commands"]["chown_target_dir"]
            ):
                raise AssertionError("deploy --dry-run must expose target_dir ownership normalization")
            if "openpyxl==3.1.5" not in " ".join(deploy_dry_run["commands"]["runtime_pip_install"]):
                raise AssertionError("deploy --dry-run must expose runtime pip install command for openpyxl")
            if "playwright==1.58.0" not in " ".join(deploy_dry_run["commands"]["runtime_pip_install"]):
                raise AssertionError("deploy --dry-run must expose runtime pip install command for playwright")
            if "import openpyxl, playwright" not in " ".join(deploy_dry_run["commands"]["runtime_pip_install"]):
                raise AssertionError("deploy --dry-run must guard on both openpyxl and playwright imports")
            if "install" not in " ".join(deploy_dry_run["commands"]["systemd_install"]):
                raise AssertionError("deploy --dry-run must expose systemd install command")
            if "daemon-reload" not in " ".join(deploy_dry_run["commands"]["systemd_daemon_reload"]):
                raise AssertionError("deploy --dry-run must expose daemon-reload command")
            if "enable" not in " ".join(deploy_dry_run["commands"]["systemd_enable"]):
                raise AssertionError("deploy --dry-run must expose systemd enable command")
            if "restart" not in " ".join(deploy_dry_run["commands"]["systemd_restart"]):
                raise AssertionError("deploy --dry-run must expose systemd restart command")
            if "apply-nginx-routes" not in " ".join(deploy_dry_run["commands"]["nginx_public_routes_update"]):
                raise AssertionError("deploy --dry-run must expose nginx public route update command")

            archived_print_plan = _run_json(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--target-file",
                    str(archived_target_file),
                    "print-plan",
                ]
            )
            archived_plan = archived_print_plan["deploy_plan"]
            if archived_plan["target_role"] != "rollback_only":
                raise AssertionError("archived selleros print-plan must expose rollback_only target_role")
            if archived_plan["target_lifecycle"] != "deprecated_live_target":
                raise AssertionError("archived selleros print-plan must expose deprecated lifecycle")
            mutation_guard = archived_plan["target_mutation_guard"]
            if mutation_guard["mutating_actions_require_override"] is not True:
                raise AssertionError("archived selleros target must require explicit mutation override")
            if (
                mutation_guard["override_env"]
                != hosted_runtime.ROLLBACK_TARGET_WRITE_OVERRIDE_ENV
            ):
                raise AssertionError("archived selleros target must expose rollback override env")

            archived_deploy_dry_run = _run_json(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--target-file",
                    str(archived_target_file),
                    "deploy",
                    "--dry-run",
                    "--allow-dirty",
                ]
            )
            if archived_deploy_dry_run["dry_run"] is not True:
                raise AssertionError("archived selleros deploy --dry-run must stay allowed and dry")
            if "selleros-root" not in " ".join(archived_deploy_dry_run["commands"]["rsync"]):
                raise AssertionError("archived selleros deploy --dry-run must expose, not execute, selleros command plan")

            archived_deploy = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--target-file",
                    str(archived_target_file),
                    "deploy",
                    "--allow-dirty",
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            if archived_deploy.returncode == 0:
                raise AssertionError("archived selleros real deploy target must fail fast")
            archived_output = archived_deploy.stdout + archived_deploy.stderr
            if "rollback-only after EU migration" not in archived_output:
                raise AssertionError("archived deploy failure must name EU migration rollback-only guard")
            if "hosted_runtime_target__europe_api.json" not in archived_output:
                raise AssertionError("archived deploy failure must point operators to the EU target file")
            if hosted_runtime.ROLLBACK_TARGET_WRITE_OVERRIDE_ENV not in archived_output:
                raise AssertionError("archived deploy failure must name explicit emergency rollback override")

            archived_nginx_apply = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--target-file",
                    str(archived_target_file),
                    "apply-nginx-routes",
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            if archived_nginx_apply.returncode == 0:
                raise AssertionError("archived selleros apply-nginx-routes must fail fast")
            archived_nginx_output = archived_nginx_apply.stdout + archived_nginx_apply.stderr
            if "rollback-only after EU migration" not in archived_nginx_output:
                raise AssertionError("archived nginx apply failure must name rollback-only guard")

            archived_deploy_and_verify = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--target-file",
                    str(archived_target_file),
                    "deploy-and-verify",
                    "--allow-dirty",
                    "--skip-refresh",
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            if archived_deploy_and_verify.returncode == 0:
                raise AssertionError("archived selleros deploy-and-verify must fail fast")
            archived_deploy_and_verify_output = (
                archived_deploy_and_verify.stdout + archived_deploy_and_verify.stderr
            )
            if "rollback-only after EU migration" not in archived_deploy_and_verify_output:
                raise AssertionError("archived deploy-and-verify failure must name rollback-only guard")

            archived_target = hosted_runtime.load_hosted_runtime_target(archived_target_file)
            previous_override = os.environ.get(hosted_runtime.ROLLBACK_TARGET_WRITE_OVERRIDE_ENV)
            try:
                os.environ[hosted_runtime.ROLLBACK_TARGET_WRITE_OVERRIDE_ENV] = (
                    hosted_runtime.ROLLBACK_TARGET_WRITE_OVERRIDE_VALUE
                )
                hosted_runtime._ensure_target_allows_mutation(
                    archived_target,
                    action="deploy",
                    dry_run=False,
                )
            finally:
                if previous_override is None:
                    os.environ.pop(hosted_runtime.ROLLBACK_TARGET_WRITE_OVERRIDE_ENV, None)
                else:
                    os.environ[hosted_runtime.ROLLBACK_TARGET_WRITE_OVERRIDE_ENV] = previous_override

            public_probe = _run_json(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--target-file",
                    str(target_file),
                    "public-probe",
                    "--as-of-date",
                    "2026-04-12",
                    "--skip-refresh",
                ]
            )
            if public_probe["ok"] is not True:
                raise AssertionError("public probe must succeed against local live runner")
            route_map = {item["route"]: item for item in public_probe["routes"]}
            if route_map["operator_reports"]["http_status"] != 200:
                raise AssertionError("operator reports embedded panel must be publicly readable")
            if route_map["load_route"]["http_status"] != 404:
                raise AssertionError("GET load-route probe must reach app-level 404")
            if route_map["job"]["http_status"] != 404:
                raise AssertionError("job-route probe must reach app-level 404 for fake job id")
            if route_map["status"]["http_status"] != 200:
                raise AssertionError("status after seeded snapshot must be publicly readable")
            if route_map["web_vitrina_page"]["http_status"] != 200:
                raise AssertionError("web-vitrina page route must be publicly readable")
            if route_map["web_vitrina_read"]["http_status"] != 200:
                raise AssertionError("web-vitrina read route with seeded snapshot must be publicly readable")
            if route_map["web_vitrina_page_composition"]["http_status"] != 200:
                raise AssertionError("web-vitrina page composition surface must be publicly readable")
            if route_map["daily_report"]["http_status"] != 200:
                raise AssertionError("daily-report route must be publicly readable")
            if route_map["stock_report"]["http_status"] != 200:
                raise AssertionError("stock-report route must be publicly readable")
            if route_map["plan_report"]["http_status"] != 200:
                raise AssertionError("plan-report route must be publicly readable")
            if route_map["plan_report_baseline_status"]["http_status"] != 200:
                raise AssertionError("plan-report baseline status route must be publicly readable")
            if route_map["plan_report_baseline_template"]["http_status"] != 200:
                raise AssertionError("plan-report baseline template route must be publicly readable")
            if route_map["plan"]["http_status"] != 200:
                raise AssertionError("plan with seeded snapshot must be publicly readable")
            if route_map["factory_order_status"]["http_status"] != 200:
                raise AssertionError("factory-order status route must be publicly readable")
            if route_map["factory_order_template_stock_ff"]["http_status"] != 200:
                raise AssertionError("stock_ff template route must be publicly readable")
            if route_map["factory_order_template_inbound_factory"]["http_status"] != 200:
                raise AssertionError("inbound_factory template route must be publicly readable")
            if route_map["factory_order_template_inbound_ff_to_wb"]["http_status"] != 200:
                raise AssertionError("inbound_ff_to_wb template route must be publicly readable")
            if route_map["factory_order_recommendation"]["http_status"] != 422:
                raise AssertionError("recommendation route without calculation must stay truthful 422")
            if route_map["wb_regional_status"]["http_status"] != 200:
                raise AssertionError("wb-regional status route must be publicly readable")
            if route_map["wb_regional_district_central"]["http_status"] != 422:
                raise AssertionError("district route without calculation must stay truthful 422")
            loopback_probe = _run_json(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--target-file",
                    str(target_file),
                    "loopback-probe",
                    "--as-of-date",
                    "2026-04-12",
                    "--skip-refresh",
                ]
            )
            if loopback_probe["ok"] is not True:
                raise AssertionError("loopback probe must succeed against local loopback target")
            loopback_routes = {item["route"]: item for item in loopback_probe["routes"]}
            if loopback_routes["status"]["http_status"] != 200:
                raise AssertionError("status with seeded snapshot must stay 200")
            if loopback_routes["web_vitrina_read"]["http_status"] != 200:
                raise AssertionError("web-vitrina read route with seeded snapshot must stay 200")
            if loopback_routes["operator_reports"]["http_status"] != 200:
                raise AssertionError("operator reports embedded panel must stay 200")
            if loopback_routes["web_vitrina_page_composition"]["http_status"] != 200:
                raise AssertionError("web-vitrina page composition surface must stay 200")
            if loopback_routes["daily_report"]["http_status"] != 200:
                raise AssertionError("daily-report route must stay 200")
            if loopback_routes["stock_report"]["http_status"] != 200:
                raise AssertionError("stock-report route must stay 200")
            if loopback_routes["plan_report"]["http_status"] != 200:
                raise AssertionError("plan-report route must stay 200")
            if loopback_routes["plan_report_baseline_status"]["http_status"] != 200:
                raise AssertionError("plan-report baseline status route must stay 200")
            if loopback_routes["plan_report_baseline_template"]["http_status"] != 200:
                raise AssertionError("plan-report baseline template route must stay 200")
            if loopback_routes["plan"]["http_status"] != 200:
                raise AssertionError("plan with seeded snapshot must stay 200")

            print(f"print_plan: ok -> {print_plan['deploy_plan']['target_id']}")
            print(f"deploy_dry_run: ok -> {deploy_dry_run['commands']['restart'][-1]}")
            print(f"deploy_dry_run_runtime_pip: ok -> {deploy_dry_run['commands']['runtime_pip_install'][-1]}")
            print(
                "deploy_dry_run_systemd: ok -> "
                f"{deploy_dry_run['commands']['systemd_restart'][-1]}"
            )
            print(
                "deploy_dry_run_nginx_routes: ok -> "
                f"{deploy_dry_run['commands']['nginx_public_routes_update'][-1]}"
            )
            print("archived_target_print_plan: ok -> rollback_only metadata exposed")
            print("archived_target_deploy_dry_run: ok -> command plan only")
            print("archived_target_guard: ok -> real deploy/apply-nginx refused")
            print("archived_target_override_guard: ok -> explicit env override recognized without remote mutation")
            print(f"public_probe_web_vitrina_page: ok -> {route_map['web_vitrina_page']['http_status']}")
            print(f"public_probe_operator_reports: ok -> {route_map['operator_reports']['http_status']}")
            print(f"public_probe_web_vitrina_read: ok -> {route_map['web_vitrina_read']['http_status']}")
            print(
                "public_probe_web_vitrina_page_composition: ok -> "
                f"{route_map['web_vitrina_page_composition']['http_status']}"
            )
            print(f"public_probe_stock_report: ok -> {route_map['stock_report']['http_status']}")
            print(f"public_probe_plan_report: ok -> {route_map['plan_report']['http_status']}")
            print(f"public_probe_plan_baseline: ok -> {route_map['plan_report_baseline_status']['http_status']}/{route_map['plan_report_baseline_template']['http_status']}")
            print(f"factory_order_status: ok -> {route_map['factory_order_status']['http_status']}")
            print(f"wb_regional_status: ok -> {route_map['wb_regional_status']['http_status']}")
            print(f"loopback_probe_status: ok -> {loopback_routes['status']['http_status']}")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def _post_bundle(url: str) -> None:
    payload = INPUT_BUNDLE.read_bytes()
    request = urllib_request.Request(
        url,
        method="POST",
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib_request.urlopen(request, timeout=10) as response:
        if response.getcode() != 200:
            raise AssertionError(f"bundle upload must return 200, got {response.getcode()}")


def _seed_ready_snapshot(runtime_dir: Path) -> None:
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    current_state = runtime.load_current_state()
    enabled = [item for item in current_state.config_v2 if item.enabled]
    runtime.save_sheet_vitrina_ready_snapshot(
        current_state=current_state,
        refreshed_at="2026-04-20T09:05:00Z",
        plan=_build_plan(
            first_nm_id=enabled[0].nm_id,
            second_nm_id=enabled[1].nm_id,
            first_group=enabled[0].group,
        ),
    )


def _build_plan(
    *,
    first_nm_id: int,
    second_nm_id: int,
    first_group: str,
) -> SheetVitrinaV1Envelope:
    return SheetVitrinaV1Envelope(
        plan_version="delivery_contract_v1__sheet_scaffold_v1",
        snapshot_id="hosted-runtime-web-vitrina-fixture",
        as_of_date="2026-04-12",
        date_columns=["2026-04-12", "2026-04-20"],
        temporal_slots=[
            SheetVitrinaV1TemporalSlot(
                slot_key="yesterday_closed",
                slot_label="Yesterday closed",
                column_date="2026-04-12",
            ),
            SheetVitrinaV1TemporalSlot(
                slot_key="today_current",
                slot_label="Today current",
                column_date="2026-04-20",
            ),
        ],
        source_temporal_policies={
            "seller_funnel_snapshot": "dual_day_capable",
            "prices_snapshot": "accepted_current_rollover",
        },
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name="DATA_VITRINA",
                write_start_cell="A1",
                write_rect="A1:D5",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=["label", "key", "2026-04-12", "2026-04-20"],
                rows=[
                    ["Итого: Показы в воронке", "TOTAL|total_view_count", 100, 140],
                    [f"Группа {first_group}: Показы в воронке", f"GROUP:{first_group}|view_count", 40, 55],
                    [f"SKU A: Показы в воронке", f"SKU:{first_nm_id}|view_count", 20, 30],
                    [f"SKU B: Заказы, шт.", f"SKU:{second_nm_id}|orderSum", 5, 7],
                ],
                row_count=4,
                column_count=4,
            ),
            SheetVitrinaWriteTarget(
                sheet_name="STATUS",
                write_start_cell="A1",
                write_rect="A1:K2",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=STATUS_HEADER,
                rows=[
                    [
                        "seller_funnel_snapshot",
                        "success",
                        "fresh",
                        "2026-04-20",
                        "2026-04-20",
                        "2026-04-20",
                        "2026-04-20",
                        2,
                        2,
                        "",
                        "",
                    ]
                ],
                row_count=1,
                column_count=len(STATUS_HEADER),
            ),
        ],
    )


def _wait_until_ready(url: str) -> None:
    deadline = time.time() + 10
    last_error = ""
    while time.time() < deadline:
        try:
            with urllib_request.urlopen(url, timeout=1.5) as response:
                if response.getcode() == 200:
                    return
        except Exception as exc:  # pragma: no cover - bounded smoke retry
            last_error = str(exc)
            time.sleep(0.1)
    raise AssertionError(f"local live runner did not become ready: {last_error}")


def _reserve_free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run_json(command: list[str]) -> dict[str, object]:
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
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
