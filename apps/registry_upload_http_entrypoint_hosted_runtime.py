"""Repo-owned deploy/probe contract for hosted registry upload runtime."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shlex
import ssl
import subprocess
import sys
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (
    DEFAULT_COST_PRICE_UPLOAD_PATH,
    DEFAULT_FACTORY_ORDER_RECOMMENDATION_PATH,
    DEFAULT_FACTORY_ORDER_STATUS_PATH,
    DEFAULT_FACTORY_ORDER_TEMPLATE_INBOUND_FACTORY_PATH,
    DEFAULT_FACTORY_ORDER_TEMPLATE_INBOUND_FF_TO_WB_PATH,
    DEFAULT_FACTORY_ORDER_TEMPLATE_STOCK_FF_PATH,
    DEFAULT_SHEET_JOB_PATH,
    DEFAULT_SHEET_LOAD_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_REFRESH_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_UPLOAD_PATH,
)


DEFAULT_TARGET_FILE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "hosted_runtime_target__example.json"
)
TARGET_FILE_ENV = "WB_CORE_HOSTED_RUNTIME_TARGET_FILE"
SSH_IDENTITY_FILE_ENV = "WB_CORE_HOSTED_RUNTIME_SSH_IDENTITY_FILE"
SSH_OPTIONS_ENV = "WB_CORE_HOSTED_RUNTIME_SSH_OPTIONS"

RUNTIME_ENV_CONTRACT = [
    "REGISTRY_UPLOAD_HTTP_HOST",
    "REGISTRY_UPLOAD_HTTP_PORT",
    "REGISTRY_UPLOAD_RUNTIME_DIR",
    "REGISTRY_UPLOAD_HTTP_PATH",
    "COST_PRICE_UPLOAD_HTTP_PATH",
    "SHEET_VITRINA_HTTP_PATH",
    "SHEET_VITRINA_REFRESH_HTTP_PATH",
    "SHEET_VITRINA_STATUS_HTTP_PATH",
    "SHEET_VITRINA_OPERATOR_UI_PATH",
]
REQUIRED_SECRET_CONTRACT = [
    "WB_API_TOKEN",
]
OPTIONAL_RUNTIME_CONTRACT = [
    "WB_OFFICIAL_API_BASE_URL",
    "WB_ADVERT_API_BASE_URL",
    "WB_SELLER_ANALYTICS_API_BASE_URL",
    "WB_STATISTICS_API_BASE_URL",
]
ROUTE_ENV_DEFAULTS = {
    "REGISTRY_UPLOAD_HTTP_PATH": DEFAULT_UPLOAD_PATH,
    "COST_PRICE_UPLOAD_HTTP_PATH": DEFAULT_COST_PRICE_UPLOAD_PATH,
    "SHEET_VITRINA_HTTP_PATH": DEFAULT_SHEET_PLAN_PATH,
    "SHEET_VITRINA_REFRESH_HTTP_PATH": DEFAULT_SHEET_REFRESH_PATH,
    "SHEET_VITRINA_STATUS_HTTP_PATH": DEFAULT_SHEET_STATUS_PATH,
    "SHEET_VITRINA_OPERATOR_UI_PATH": DEFAULT_SHEET_OPERATOR_UI_PATH,
}
RSYNC_EXCLUDES = [
    ".git/",
    ".runtime/",
    "__pycache__/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".venv/",
    ".DS_Store",
]


@dataclass(frozen=True)
class HostedRuntimeTarget:
    target_id: str
    public_base_url: str
    loopback_base_url: str
    ssh_destination: str
    target_dir: str
    service_name: str
    restart_command: str
    status_command: str
    environment_file: str
    runtime_env: dict[str, str] = field(default_factory=dict)

    @property
    def route_paths(self) -> dict[str, str]:
        return {
            env_name: str(self.runtime_env.get(env_name) or default).strip() or default
            for env_name, default in ROUTE_ENV_DEFAULTS.items()
        }


def load_hosted_runtime_target(path: Path | None = None) -> HostedRuntimeTarget:
    target_path = path or resolve_target_file()
    payload = json.loads(target_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("hosted runtime target file must contain a JSON object")

    raw_runtime_env = payload.get("runtime_env") or {}
    if not isinstance(raw_runtime_env, dict):
        raise ValueError("runtime_env must be a JSON object")
    runtime_env = {str(key): str(value) for key, value in raw_runtime_env.items()}

    return HostedRuntimeTarget(
        target_id=str(payload.get("target_id", "")).strip(),
        public_base_url=_normalize_base_url(str(payload.get("public_base_url", "")).strip()),
        loopback_base_url=_normalize_base_url(str(payload.get("loopback_base_url", "")).strip()),
        ssh_destination=str(payload.get("ssh_destination", "")).strip(),
        target_dir=str(payload.get("target_dir", "")).strip(),
        service_name=str(payload.get("service_name", "")).strip(),
        restart_command=str(payload.get("restart_command", "")).strip(),
        status_command=str(payload.get("status_command", "")).strip(),
        environment_file=str(payload.get("environment_file", "")).strip(),
        runtime_env=runtime_env,
    )


def resolve_target_file(raw_value: str | None = None) -> Path:
    candidate = (raw_value or os.environ.get(TARGET_FILE_ENV, "")).strip()
    path = Path(candidate).expanduser() if candidate else DEFAULT_TARGET_FILE
    if not path.exists():
        raise FileNotFoundError(f"hosted runtime target file not found: {path}")
    return path


def build_runtime_contract_summary(target: HostedRuntimeTarget) -> dict[str, Any]:
    return {
        "target": asdict(target),
        "target_file_env": TARGET_FILE_ENV,
        "ssh_identity_file_env": SSH_IDENTITY_FILE_ENV,
        "ssh_options_env": SSH_OPTIONS_ENV,
        "runtime_env_contract": RUNTIME_ENV_CONTRACT,
        "required_secret_contract": REQUIRED_SECRET_CONTRACT,
        "optional_runtime_contract": OPTIONAL_RUNTIME_CONTRACT,
        "route_paths": target.route_paths,
        "git": {
            "branch": _git_output(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
            "commit": _git_output(["git", "rev-parse", "HEAD"]),
            "worktree_root": str(ROOT),
            "dirty": bool(_git_output(["git", "status", "--short"])),
        },
    }


def build_deploy_plan(target: HostedRuntimeTarget) -> dict[str, Any]:
    missing = _missing_for_deploy(target)
    return {
        "target_id": target.target_id,
        "public_base_url": target.public_base_url,
        "loopback_base_url": target.loopback_base_url,
        "ssh_destination": target.ssh_destination or "<local-only>",
        "service_name": target.service_name or "<missing>",
        "target_dir": target.target_dir or "<missing>",
        "environment_file": target.environment_file or "<missing>",
        "route_paths": target.route_paths,
        "runtime_env_contract": RUNTIME_ENV_CONTRACT,
        "required_secret_contract": REQUIRED_SECRET_CONTRACT,
        "optional_runtime_contract": OPTIONAL_RUNTIME_CONTRACT,
        "deploy_sequence": [
            "sync current checked-out worktree to target_dir via rsync",
            "restart hosted runtime via restart_command",
            "probe loopback/runtime contour",
            "probe public contour",
        ],
        "missing_for_deploy": missing,
        "applicable_to_current_checkout_without_merge": True,
    }


def collect_public_surface(
    *,
    base_url: str,
    route_paths: dict[str, str],
    as_of_date: str | None,
    include_refresh: bool,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    results = [
        _collect_http_probe(
            name="operator",
            method="GET",
            url=f"{base_url}{route_paths['SHEET_VITRINA_OPERATOR_UI_PATH']}",
            timeout_seconds=timeout_seconds,
        ),
        _collect_http_probe(
            name="load_route",
            method="GET",
            url=f"{base_url}{DEFAULT_SHEET_LOAD_PATH}",
            timeout_seconds=timeout_seconds,
        ),
        _collect_http_probe(
            name="job",
            method="GET",
            url=f"{base_url}{DEFAULT_SHEET_JOB_PATH}?job_id=hosted_runtime_probe",
            timeout_seconds=timeout_seconds,
        ),
        _collect_http_probe(
            name="status",
            method="GET",
            url=_append_as_of_date(
                f"{base_url}{route_paths['SHEET_VITRINA_STATUS_HTTP_PATH']}",
                as_of_date,
            ),
            timeout_seconds=timeout_seconds,
        ),
        _collect_http_probe(
            name="plan",
            method="GET",
            url=_append_as_of_date(
                f"{base_url}{route_paths['SHEET_VITRINA_HTTP_PATH']}",
                as_of_date,
            ),
            timeout_seconds=timeout_seconds,
        ),
        _collect_http_probe(
            name="factory_order_status",
            method="GET",
            url=f"{base_url}{DEFAULT_FACTORY_ORDER_STATUS_PATH}",
            timeout_seconds=timeout_seconds,
        ),
        _collect_http_probe(
            name="factory_order_template_stock_ff",
            method="GET",
            url=f"{base_url}{DEFAULT_FACTORY_ORDER_TEMPLATE_STOCK_FF_PATH}",
            timeout_seconds=timeout_seconds,
        ),
        _collect_http_probe(
            name="factory_order_template_inbound_factory",
            method="GET",
            url=f"{base_url}{DEFAULT_FACTORY_ORDER_TEMPLATE_INBOUND_FACTORY_PATH}",
            timeout_seconds=timeout_seconds,
        ),
        _collect_http_probe(
            name="factory_order_template_inbound_ff_to_wb",
            method="GET",
            url=f"{base_url}{DEFAULT_FACTORY_ORDER_TEMPLATE_INBOUND_FF_TO_WB_PATH}",
            timeout_seconds=timeout_seconds,
        ),
        _collect_http_probe(
            name="factory_order_recommendation",
            method="GET",
            url=f"{base_url}{DEFAULT_FACTORY_ORDER_RECOMMENDATION_PATH}",
            timeout_seconds=timeout_seconds,
        ),
    ]
    if include_refresh:
        refresh_payload = {"as_of_date": as_of_date} if as_of_date else {}
        results.append(
            _collect_http_probe(
                name="refresh",
                method="POST",
                url=f"{base_url}{route_paths['SHEET_VITRINA_REFRESH_HTTP_PATH']}",
                json_payload=refresh_payload,
                timeout_seconds=timeout_seconds,
            )
        )
    return results


def evaluate_surface_results(results: list[dict[str, Any]], *, route_paths: dict[str, str]) -> dict[str, Any]:
    evaluations = [_evaluate_route_result(result, route_paths=route_paths) for result in results]
    return {
        "ok": all(item["ok"] for item in evaluations),
        "routes": evaluations,
    }


def collect_loopback_surface(
    target: HostedRuntimeTarget,
    *,
    as_of_date: str | None,
    include_refresh: bool,
    timeout_seconds: float,
) -> dict[str, Any]:
    if target.ssh_destination:
        raw_results = _collect_remote_loopback_surface(
            target,
            as_of_date=as_of_date,
            include_refresh=include_refresh,
            timeout_seconds=timeout_seconds,
        )
        transport = "ssh"
    else:
        raw_results = collect_public_surface(
            base_url=target.loopback_base_url,
            route_paths=target.route_paths,
            as_of_date=as_of_date,
            include_refresh=include_refresh,
            timeout_seconds=timeout_seconds,
        )
        transport = "local"
    evaluation = evaluate_surface_results(raw_results, route_paths=target.route_paths)
    evaluation["transport"] = transport
    evaluation["base_url"] = target.loopback_base_url
    return evaluation


def deploy_current_checkout(
    target: HostedRuntimeTarget,
    *,
    dry_run: bool,
    allow_dirty: bool,
) -> dict[str, Any]:
    missing = _missing_for_deploy(target)
    if missing:
        raise ValueError(f"deploy target is incomplete for deploy: {', '.join(missing)}")
    if not allow_dirty:
        _ensure_clean_worktree()

    ssh_command = _ssh_command()
    rsync_plan = [
        "rsync",
        "-az",
        "--delete",
        *[item for pattern in RSYNC_EXCLUDES for item in ("--exclude", pattern)],
        "-e",
        " ".join(ssh_command),
        f"{ROOT}/",
        f"{target.ssh_destination}:{target.target_dir.rstrip('/')}/",
    ]
    mkdir_command = _remote_shell_command(target, f"mkdir -p {shlex.quote(target.target_dir)}")
    restart_command = _remote_shell_command(
        target,
        f"cd {shlex.quote(target.target_dir)} && {target.restart_command}",
    )
    status_command = (
        _remote_shell_command(
            target,
            f"cd {shlex.quote(target.target_dir)} && {target.status_command}",
        )
        if target.status_command
        else None
    )

    summary = {
        "ok": True,
        "dry_run": dry_run,
        "commands": {
            "mkdir": mkdir_command,
            "rsync": rsync_plan,
            "restart": restart_command,
            "status": status_command,
        },
    }
    if dry_run:
        return summary

    _run_command(mkdir_command)
    _run_command(rsync_plan)
    _run_command(restart_command)
    if status_command:
        _run_command(status_command)
    return summary


def run_public_probe_command(args: argparse.Namespace) -> int:
    target = load_hosted_runtime_target(args.target_file)
    raw_results = collect_public_surface(
        base_url=target.public_base_url,
        route_paths=target.route_paths,
        as_of_date=args.as_of_date,
        include_refresh=not args.skip_refresh,
        timeout_seconds=args.timeout_seconds,
    )
    payload = {
        "target_id": target.target_id,
        "base_url": target.public_base_url,
        "as_of_date": args.as_of_date,
        "include_refresh": not args.skip_refresh,
        **evaluate_surface_results(raw_results, route_paths=target.route_paths),
    }
    _print_json(payload)
    return 0 if payload["ok"] else 1


def run_loopback_probe_command(args: argparse.Namespace) -> int:
    target = load_hosted_runtime_target(args.target_file)
    payload = {
        "target_id": target.target_id,
        "as_of_date": args.as_of_date,
        "include_refresh": not args.skip_refresh,
        **collect_loopback_surface(
            target,
            as_of_date=args.as_of_date,
            include_refresh=not args.skip_refresh,
            timeout_seconds=args.timeout_seconds,
        ),
    }
    _print_json(payload)
    return 0 if payload["ok"] else 1


def run_print_plan_command(args: argparse.Namespace) -> int:
    target = load_hosted_runtime_target(args.target_file)
    payload = {
        **build_runtime_contract_summary(target),
        "deploy_plan": build_deploy_plan(target),
    }
    _print_json(payload)
    return 0


def run_deploy_command(args: argparse.Namespace) -> int:
    target = load_hosted_runtime_target(args.target_file)
    payload = {
        "target_id": target.target_id,
        **deploy_current_checkout(
            target,
            dry_run=args.dry_run,
            allow_dirty=args.allow_dirty,
        ),
    }
    _print_json(payload)
    return 0


def run_deploy_and_verify_command(args: argparse.Namespace) -> int:
    target = load_hosted_runtime_target(args.target_file)
    deploy_summary = deploy_current_checkout(
        target,
        dry_run=args.dry_run,
        allow_dirty=args.allow_dirty,
    )
    loopback_summary = collect_loopback_surface(
        target,
        as_of_date=args.as_of_date,
        include_refresh=not args.skip_refresh,
        timeout_seconds=args.timeout_seconds,
    )
    public_summary = evaluate_surface_results(
        collect_public_surface(
            base_url=target.public_base_url,
            route_paths=target.route_paths,
            as_of_date=args.as_of_date,
            include_refresh=not args.skip_refresh,
            timeout_seconds=args.timeout_seconds,
        ),
        route_paths=target.route_paths,
    )
    payload = {
        "target_id": target.target_id,
        "deploy": deploy_summary,
        "loopback_probe": loopback_summary,
        "public_probe": {
            "base_url": target.public_base_url,
            "as_of_date": args.as_of_date,
            "include_refresh": not args.skip_refresh,
            **public_summary,
        },
        "ok": deploy_summary["ok"] and loopback_summary["ok"] and public_summary["ok"],
    }
    _print_json(payload)
    return 0 if payload["ok"] else 1


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Repo-owned deploy/probe contract for hosted registry upload runtime.",
    )
    parser.add_argument(
        "--target-file",
        type=Path,
        default=None,
        help=f"Path to target JSON. Defaults to ${TARGET_FILE_ENV} or {DEFAULT_TARGET_FILE}.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    print_plan = subparsers.add_parser("print-plan", help="Print canonical deploy contract and current checkout plan.")
    print_plan.set_defaults(handler=run_print_plan_command)

    public_probe = subparsers.add_parser("public-probe", help="Probe public hosted routes.")
    _add_probe_args(public_probe)
    public_probe.set_defaults(handler=run_public_probe_command)

    loopback_probe = subparsers.add_parser("loopback-probe", help="Probe loopback/runtime routes locally or via SSH.")
    _add_probe_args(loopback_probe)
    loopback_probe.set_defaults(handler=run_loopback_probe_command)

    deploy = subparsers.add_parser("deploy", help="Sync current checkout to hosted runtime and restart the service.")
    deploy.add_argument("--dry-run", action="store_true", help="Print commands without executing remote update.")
    deploy.add_argument("--allow-dirty", action="store_true", help="Allow deploy from dirty checkout.")
    deploy.set_defaults(handler=run_deploy_command)

    deploy_and_verify = subparsers.add_parser(
        "deploy-and-verify",
        help="Deploy current checkout, then probe loopback and public routes.",
    )
    _add_probe_args(deploy_and_verify)
    deploy_and_verify.add_argument("--dry-run", action="store_true", help="Print deploy commands without executing.")
    deploy_and_verify.add_argument("--allow-dirty", action="store_true", help="Allow deploy from dirty checkout.")
    deploy_and_verify.set_defaults(handler=run_deploy_and_verify_command)

    return parser


def _add_probe_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--as-of-date", default=None, help="Optional as_of_date for plan/status/refresh probes.")
    parser.add_argument("--skip-refresh", action="store_true", help="Skip POST /v1/sheet-vitrina-v1/refresh.")
    parser.add_argument("--timeout-seconds", type=float, default=20.0, help="HTTP probe timeout in seconds.")


def _collect_http_probe(
    *,
    name: str,
    method: str,
    url: str,
    timeout_seconds: float,
    json_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request = urllib_request.Request(
        url=url,
        method=method,
        headers={"Accept": "application/json, text/html;q=0.9"},
    )
    if json_payload is not None:
        body = json.dumps(json_payload, ensure_ascii=False).encode("utf-8")
        request.data = body
        request.add_header("Content-Type", "application/json; charset=utf-8")
        request.add_header("Content-Length", str(len(body)))
    try:
        with _open_request(request, timeout_seconds=timeout_seconds) as response:
            body_text = response.read().decode("utf-8", errors="replace")
            return {
                "route": name,
                "method": method,
                "url": url,
                "http_status": response.getcode(),
                "content_type": response.headers.get("Content-Type", ""),
                "body_excerpt": body_text,
                "json_body": _try_load_json(body_text),
                "network_error": None,
            }
    except urllib_error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        return {
            "route": name,
            "method": method,
            "url": url,
            "http_status": exc.code,
            "content_type": exc.headers.get("Content-Type", ""),
            "body_excerpt": body_text,
            "json_body": _try_load_json(body_text),
            "network_error": None,
        }
    except urllib_error.URLError as exc:
        return {
            "route": name,
            "method": method,
            "url": url,
            "http_status": None,
            "content_type": "",
            "body_excerpt": "",
            "json_body": None,
            "network_error": str(exc.reason),
        }
    except Exception as exc:  # pragma: no cover - bounded network fallback
        return {
            "route": name,
            "method": method,
            "url": url,
            "http_status": None,
            "content_type": "",
            "body_excerpt": "",
            "json_body": None,
            "network_error": str(exc),
        }


def _evaluate_route_result(result: dict[str, Any], *, route_paths: dict[str, str]) -> dict[str, Any]:
    route = str(result["route"])
    evaluation = dict(result)
    if result.get("network_error"):
        evaluation["ok"] = False
        evaluation["detail"] = f"network error: {result['network_error']}"
        return evaluation

    status = int(result["http_status"])
    content_type = str(result.get("content_type", "")).lower()
    if route == "operator":
        body = str(result.get("body_excerpt", ""))
        tokens = [
            "Загрузить данные",
            "Отправить данные",
            "Скачать лог",
            "Расчёт поставок",
            "Заказ на фабрике",
            "Скачать шаблон остатков ФФ",
            "Скачать шаблон товаров в пути от фабрики",
            "Скачать шаблон товаров в пути от ФФ на Wildberries",
            "Рассчитать заказ на фабрике",
            "Скачать рекомендацию",
            "Сервер и расписание",
            "Часовой пояс",
            "Автообновление",
            "Последний автозапуск",
            "Статус последнего автозапуска",
            "Последнее успешное автообновление",
            "Лог",
            route_paths["SHEET_VITRINA_REFRESH_HTTP_PATH"],
            route_paths["SHEET_VITRINA_STATUS_HTTP_PATH"],
            DEFAULT_SHEET_LOAD_PATH,
            DEFAULT_SHEET_JOB_PATH,
        ]
        missing_tokens = [token for token in tokens if token not in body]
        evaluation["ok"] = status == 200 and "text/html" in content_type and not missing_tokens
        evaluation["detail"] = (
            "operator page shape ok"
            if evaluation["ok"]
            else f"expected 200 text/html with operator tokens, missing={missing_tokens}"
        )
        return evaluation

    if route in {
        "factory_order_template_stock_ff",
        "factory_order_template_inbound_factory",
        "factory_order_template_inbound_ff_to_wb",
    }:
        evaluation["ok"] = status == 200 and "spreadsheetml.sheet" in content_type
        evaluation["detail"] = (
            "factory-order template download route ok"
            if evaluation["ok"]
            else "expected 200 XLSX response for factory-order template route"
        )
        return evaluation

    if route == "factory_order_recommendation" and status == 200:
        evaluation["ok"] = "spreadsheetml.sheet" in content_type
        evaluation["detail"] = (
            "factory-order recommendation route returned XLSX"
            if evaluation["ok"]
            else "expected XLSX content-type for successful recommendation route"
        )
        return evaluation

    if "application/json" not in content_type:
        evaluation["ok"] = False
        evaluation["detail"] = f"expected JSON content-type, got {result.get('content_type', '')!r}"
        return evaluation

    payload = result.get("json_body")
    if not isinstance(payload, dict):
        evaluation["ok"] = False
        evaluation["detail"] = "expected JSON object response"
        return evaluation

    if route == "status":
        evaluation["ok"], evaluation["detail"] = _validate_json_result(
            status,
            payload,
            success_keys=[
                "status",
                "bundle_version",
                "activated_at",
                "refreshed_at",
                "as_of_date",
                "date_columns",
                "temporal_slots",
                "snapshot_id",
                "plan_version",
                "sheet_row_counts",
                "server_context",
            ],
            error_keys=["error", "server_context"],
        )
        return evaluation

    if route == "plan":
        evaluation["ok"], evaluation["detail"] = _validate_json_result(
            status,
            payload,
            success_keys=[
                "plan_version",
                "snapshot_id",
                "as_of_date",
                "date_columns",
                "temporal_slots",
                "source_temporal_policies",
                "sheets",
            ],
        )
        return evaluation

    if route == "factory_order_status":
        evaluation["ok"], evaluation["detail"] = _validate_json_result(
            status,
            payload,
            success_keys=[
                "status",
                "active_sku_count",
                "coverage_contract_note",
                "datasets",
                "recommendation_download_path",
            ],
        )
        return evaluation

    if route == "load_route":
        error_text = str(payload.get("error", "") or "")
        evaluation["ok"] = status == 404 and "unsupported path" in error_text
        evaluation["detail"] = (
            "load route is publicly published and reaches app-level 404 on GET"
            if evaluation["ok"]
            else "expected app-level JSON 404 for GET load route probe"
        )
        return evaluation

    if route == "job":
        error_text = str(payload.get("error", "") or "")
        evaluation["ok"] = status == 404 and "operator job not found" in error_text
        evaluation["detail"] = (
            "job route is publicly published"
            if evaluation["ok"]
            else "expected JSON 404 operator job not found for job route probe"
        )
        return evaluation

    if route == "factory_order_recommendation":
        error_text = str(payload.get("error", "") or "")
        evaluation["ok"] = status == 422 and bool(error_text)
        evaluation["detail"] = (
            "factory-order recommendation route published with truthful 422 before calculation"
            if evaluation["ok"]
            else "expected 200 XLSX or 422 JSON error for recommendation route"
        )
        return evaluation

    if route == "refresh":
        evaluation["ok"], evaluation["detail"] = _validate_json_result(
            status,
            payload,
            success_keys=[
                "status",
                "bundle_version",
                "activated_at",
                "refreshed_at",
                "as_of_date",
                "date_columns",
                "temporal_slots",
                "snapshot_id",
                "plan_version",
                "sheet_row_counts",
                "server_context",
            ],
        )
        return evaluation

    evaluation["ok"] = False
    evaluation["detail"] = f"unsupported route name {route!r}"
    return evaluation


def _validate_json_result(
    status: int,
    payload: dict[str, Any],
    *,
    success_keys: list[str],
    error_keys: list[str] | None = None,
) -> tuple[bool, str]:
    if status == 200:
        missing = [key for key in success_keys if key not in payload]
        if missing:
            return False, f"200 JSON missing keys: {missing}"
        return True, "200 JSON shape ok"
    if status == 422:
        required_error_keys = error_keys or ["error"]
        missing = [key for key in required_error_keys if key not in payload]
        if missing:
            return False, f"422 JSON missing keys: {missing}"
        return True, "422 JSON error shape ok"
    return False, f"unexpected HTTP status {status}"


def _collect_remote_loopback_surface(
    target: HostedRuntimeTarget,
    *,
    as_of_date: str | None,
    include_refresh: bool,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    script = _build_remote_probe_script(
        base_url=target.loopback_base_url,
        route_paths=target.route_paths,
        as_of_date=as_of_date,
        include_refresh=include_refresh,
        timeout_seconds=timeout_seconds,
    )
    command = _ssh_command() + [target.ssh_destination, "python3", "-"]
    result = subprocess.run(
        command,
        input=script,
        text=True,
        capture_output=True,
        cwd=ROOT,
    )
    if result.returncode != 0:
        return [
            {
                "route": "loopback_transport",
                "method": "SSH",
                "url": target.ssh_destination,
                "http_status": None,
                "content_type": "",
                "body_excerpt": result.stderr.strip(),
                "json_body": None,
                "network_error": result.stderr.strip() or result.stdout.strip() or f"ssh exit code {result.returncode}",
            }
        ]
    payload = json.loads(result.stdout)
    if not isinstance(payload, list):
        raise ValueError("remote loopback probe must return a JSON list")
    return payload


def _build_remote_probe_script(
    *,
    base_url: str,
    route_paths: dict[str, str],
    as_of_date: str | None,
    include_refresh: bool,
    timeout_seconds: float,
) -> str:
    payload = {
        "base_url": base_url,
        "route_paths": route_paths,
        "as_of_date": as_of_date,
        "include_refresh": include_refresh,
        "timeout_seconds": timeout_seconds,
    }
    payload_json = json.dumps(payload, ensure_ascii=True)
    return f"""import json
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

PAYLOAD = json.loads({payload_json!r})

def _append_as_of_date(url, as_of_date):
    if not as_of_date:
        return url
    query = urllib_parse.urlencode({{"as_of_date": as_of_date}})
    separator = '&' if '?' in url else '?'
    return f"{{url}}{{separator}}{{query}}"

def _try_load_json(body_text):
    try:
        return json.loads(body_text)
    except json.JSONDecodeError:
        return None


def _open_request(request: urllib_request.Request, *, timeout_seconds: float):
    try:
        return urllib_request.urlopen(request, timeout=timeout_seconds)
    except urllib_error.URLError as exc:
        ssl_reason = getattr(exc, "reason", None)
        if (
            os.environ.get("SELLEROS_HTTP_ALLOW_INSECURE_FALLBACK", "").strip() == "1"
            and isinstance(ssl_reason, ssl.SSLCertVerificationError)
        ):
            return urllib_request.urlopen(
                request,
                timeout=timeout_seconds,
                context=ssl._create_unverified_context(),
            )
        raise

def _collect(name, method, url, json_payload=None):
    request = urllib_request.Request(url=url, method=method, headers={{"Accept": "application/json, text/html;q=0.9"}})
    if json_payload is not None:
        body = json.dumps(json_payload).encode("utf-8")
        request.data = body
        request.add_header("Content-Type", "application/json; charset=utf-8")
        request.add_header("Content-Length", str(len(body)))
    try:
        with urllib_request.urlopen(request, timeout=PAYLOAD["timeout_seconds"]) as response:
            body_text = response.read().decode("utf-8", errors="replace")
            return {{
                "route": name,
                "method": method,
                "url": url,
                "http_status": response.getcode(),
                "content_type": response.headers.get("Content-Type", ""),
                "body_excerpt": body_text,
                "json_body": _try_load_json(body_text),
                "network_error": None,
            }}
    except urllib_error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        return {{
            "route": name,
            "method": method,
            "url": url,
            "http_status": exc.code,
            "content_type": exc.headers.get("Content-Type", ""),
                "body_excerpt": body_text,
            "json_body": _try_load_json(body_text),
            "network_error": None,
        }}
    except urllib_error.URLError as exc:
        return {{
            "route": name,
            "method": method,
            "url": url,
            "http_status": None,
            "content_type": "",
            "body_excerpt": "",
            "json_body": None,
            "network_error": str(exc.reason),
        }}

results = [
    _collect("operator", "GET", PAYLOAD["base_url"] + PAYLOAD["route_paths"]["SHEET_VITRINA_OPERATOR_UI_PATH"]),
    _collect("load_route", "GET", PAYLOAD["base_url"] + "/v1/sheet-vitrina-v1/load"),
    _collect("job", "GET", PAYLOAD["base_url"] + "/v1/sheet-vitrina-v1/job?job_id=hosted_runtime_probe"),
    _collect("status", "GET", _append_as_of_date(PAYLOAD["base_url"] + PAYLOAD["route_paths"]["SHEET_VITRINA_STATUS_HTTP_PATH"], PAYLOAD["as_of_date"])),
    _collect("plan", "GET", _append_as_of_date(PAYLOAD["base_url"] + PAYLOAD["route_paths"]["SHEET_VITRINA_HTTP_PATH"], PAYLOAD["as_of_date"])),
    _collect("factory_order_status", "GET", PAYLOAD["base_url"] + "/v1/sheet-vitrina-v1/supply/factory-order/status"),
    _collect("factory_order_template_stock_ff", "GET", PAYLOAD["base_url"] + "/v1/sheet-vitrina-v1/supply/factory-order/template/stock-ff.xlsx"),
    _collect("factory_order_template_inbound_factory", "GET", PAYLOAD["base_url"] + "/v1/sheet-vitrina-v1/supply/factory-order/template/inbound-factory.xlsx"),
    _collect("factory_order_template_inbound_ff_to_wb", "GET", PAYLOAD["base_url"] + "/v1/sheet-vitrina-v1/supply/factory-order/template/inbound-ff-to-wb.xlsx"),
    _collect("factory_order_recommendation", "GET", PAYLOAD["base_url"] + "/v1/sheet-vitrina-v1/supply/factory-order/recommendation.xlsx"),
]
if PAYLOAD["include_refresh"]:
    results.append(
        _collect(
            "refresh",
            "POST",
            PAYLOAD["base_url"] + PAYLOAD["route_paths"]["SHEET_VITRINA_REFRESH_HTTP_PATH"],
            {{"as_of_date": PAYLOAD["as_of_date"]}} if PAYLOAD["as_of_date"] else {{}},
        )
    )
print(json.dumps(results, ensure_ascii=False))
"""


def _append_as_of_date(url: str, as_of_date: str | None) -> str:
    if not as_of_date:
        return url
    query = urllib_parse.urlencode({"as_of_date": as_of_date})
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{query}"


def _try_load_json(body_text: str) -> Any:
    try:
        return json.loads(body_text)
    except json.JSONDecodeError:
        return None


def _open_request(request: urllib_request.Request, *, timeout_seconds: float):
    try:
        return urllib_request.urlopen(request, timeout=timeout_seconds)
    except urllib_error.URLError as exc:
        ssl_reason = getattr(exc, "reason", None)
        if (
            os.environ.get("SELLEROS_HTTP_ALLOW_INSECURE_FALLBACK", "").strip() == "1"
            and isinstance(ssl_reason, ssl.SSLCertVerificationError)
        ):
            return urllib_request.urlopen(
                request,
                timeout=timeout_seconds,
                context=ssl._create_unverified_context(),
            )
        raise


def _ssh_command() -> list[str]:
    command = ["ssh", "-o", "BatchMode=yes"]
    identity = os.environ.get(SSH_IDENTITY_FILE_ENV, "").strip()
    if identity:
        command.extend(["-i", identity])
    extra_options = os.environ.get(SSH_OPTIONS_ENV, "").strip()
    if extra_options:
        command.extend(shlex.split(extra_options))
    return command


def _remote_shell_command(target: HostedRuntimeTarget, shell_snippet: str) -> list[str]:
    return _ssh_command() + [target.ssh_destination, shell_snippet]


def _run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, text=True, cwd=ROOT)


def _ensure_clean_worktree() -> None:
    if _git_output(["git", "status", "--short"]):
        raise ValueError("deploy requires a clean git worktree; use --allow-dirty only when intentional")


def _missing_for_deploy(target: HostedRuntimeTarget) -> list[str]:
    missing: list[str] = []
    required = {
        "target_id": target.target_id,
        "public_base_url": target.public_base_url,
        "loopback_base_url": target.loopback_base_url,
        "ssh_destination": target.ssh_destination,
        "target_dir": target.target_dir,
        "service_name": target.service_name,
        "restart_command": target.restart_command,
    }
    for key, value in required.items():
        if _is_placeholder(value):
            missing.append(key)
    return missing


def _is_placeholder(value: str) -> bool:
    return not str(value).strip() or "__SET_ME__" in str(value)


def _normalize_base_url(value: str) -> str:
    if not value:
        return value
    return value.rstrip("/")


def _git_output(command: list[str]) -> str:
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _default_as_of_date() -> str:
    return (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    if getattr(args, "as_of_date", None) == "AUTO_YESTERDAY":
        args.as_of_date = _default_as_of_date()
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
