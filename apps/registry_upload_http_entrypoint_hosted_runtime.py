"""Repo-owned deploy/probe contract for hosted registry upload runtime."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
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

PROBE_BODY_LIMIT_BYTES = 768 * 1024

from packages.adapters.registry_upload_http_entrypoint import (
    DEFAULT_COST_PRICE_UPLOAD_PATH,
    DEFAULT_FACTORY_ORDER_RECOMMENDATION_PATH,
    DEFAULT_FACTORY_ORDER_STATUS_PATH,
    DEFAULT_FACTORY_ORDER_TEMPLATE_INBOUND_FACTORY_PATH,
    DEFAULT_FACTORY_ORDER_TEMPLATE_INBOUND_FF_TO_WB_PATH,
    DEFAULT_FACTORY_ORDER_TEMPLATE_STOCK_FF_PATH,
    DEFAULT_SELLER_PORTAL_SESSION_CHECK_PATH,
    DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH,
    DEFAULT_SELLER_PORTAL_RECOVERY_START_PATH,
    DEFAULT_SELLER_PORTAL_RECOVERY_STATUS_PATH,
    DEFAULT_SELLER_PORTAL_RECOVERY_STOP_PATH,
    DEFAULT_SHEET_DAILY_REPORT_PATH,
    DEFAULT_SHEET_FEEDBACKS_PATH,
    DEFAULT_SHEET_JOB_PATH,
    DEFAULT_SHEET_LOAD_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_PLAN_REPORT_BASELINE_STATUS_PATH,
    DEFAULT_SHEET_PLAN_REPORT_BASELINE_TEMPLATE_PATH,
    DEFAULT_SHEET_PLAN_REPORT_PATH,
    DEFAULT_SHEET_REFRESH_PATH,
    DEFAULT_SHEET_STOCK_REPORT_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_WEB_VITRINA_GROUP_REFRESH_PATH,
    DEFAULT_SHEET_WEB_VITRINA_PAGE_COMPOSITION_SURFACE,
    DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    DEFAULT_UPLOAD_PATH,
    DEFAULT_WB_REGIONAL_DISTRICT_DOWNLOAD_PREFIX,
    DEFAULT_WB_REGIONAL_STATUS_PATH,
)


DEFAULT_TARGET_FILE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "hosted_runtime_target__europe_api.json"
)
DEFAULT_PUBLIC_ROUTE_ALLOWLIST_FILE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "nginx" / "public_route_allowlist.json"
)
TARGET_FILE_ENV = "WB_CORE_HOSTED_RUNTIME_TARGET_FILE"
SSH_IDENTITY_FILE_ENV = "WB_CORE_HOSTED_RUNTIME_SSH_IDENTITY_FILE"
SSH_OPTIONS_ENV = "WB_CORE_HOSTED_RUNTIME_SSH_OPTIONS"
DEFAULT_NGINX_MANAGED_BLOCK_LABEL = "WB-CORE MANAGED PUBLIC ROUTES"
DEFAULT_NGINX_MANAGED_TLS_BLOCK_LABEL = "WB-CORE MANAGED TLS"
ACTIVE_TARGET_STATUS = "active"
ARCHIVED_TARGET_STATUS = "archived"
LOCAL_TEST_TARGET_STATUS = "local_test"
PRIMARY_LIVE_TARGET_ROLE = "primary_live"
CURRENT_LIVE_TARGET_LIFECYCLE = "current_live"
ROLLBACK_ONLY_TARGET_ROLE = "rollback_only"
ROLLBACK_ONLY_TARGET_LIFECYCLE = "deprecated_live_target"
ROLLBACK_ONLY_MUTATION_POLICY = "do_not_deploy_without_emergency_rollback_override"
ROLLBACK_TARGET_WRITE_OVERRIDE_ENV = "WB_CORE_ALLOW_ROLLBACK_TARGET_WRITE"
ROLLBACK_TARGET_WRITE_OVERRIDE_VALUE = "I_UNDERSTAND_SELLEROS_IS_ROLLBACK_ONLY"
CURRENT_LIVE_TARGET_FILE_HINT = "artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__europe_api.json"
ACTIVE_HOSTED_RUNTIME_SSH_DESTINATION = "wb-core-eu-root"
ACTIVE_HOSTED_RUNTIME_PUBLIC_HOSTS = {"89.191.226.88", "api.selleros.pro"}
ACTIVE_HOSTED_RUNTIME_TARGET_DIR = "/opt/wb-core-runtime/app"
ACTIVE_HOSTED_RUNTIME_RUNTIME_DIR = "/opt/wb-core-runtime/state"
ACTIVE_HOSTED_RUNTIME_SERVICE_NAME = "wb-core-registry-http.service"
ARCHIVED_HOSTED_RUNTIME_SSH_DESTINATIONS = {"selleros-root"}
ARCHIVED_HOSTED_RUNTIME_PUBLIC_HOSTS = {"178.72.152.177"}
ROLLBACK_ONLY_STATUSES = {ARCHIVED_TARGET_STATUS, "rollback_only", "deprecated"}
ROLLBACK_ONLY_ROLES = {ROLLBACK_ONLY_TARGET_ROLE, "do_not_deploy", "deprecated_live_target"}
ROLLBACK_ONLY_LIFECYCLES = {ROLLBACK_ONLY_TARGET_LIFECYCLE, "rollback_only", "archived"}
LOCAL_TEST_PUBLIC_HOSTS = {"127.0.0.1", "localhost", "::1"}

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
    "OPENAI_API_KEY",
]
OPTIONAL_RUNTIME_CONTRACT = [
    "WB_OFFICIAL_API_BASE_URL",
    "WB_ADVERT_API_BASE_URL",
    "WB_SELLER_ANALYTICS_API_BASE_URL",
    "WB_STATISTICS_API_BASE_URL",
    "WB_FEEDBACKS_API_BASE_URL",
    "OPENAI_MODEL",
    "OPENAI_API_BASE_URL",
    "OPENAI_TIMEOUT_SECONDS",
    "PROMO_XLSX_COLLECTOR_STORAGE_STATE_PATH",
    "SELLER_PORTAL_CANONICAL_SUPPLIER_ID",
    "SELLER_PORTAL_CANONICAL_SUPPLIER_LABEL",
    "SELLER_PORTAL_RELOGIN_SSH_DESTINATION",
]
RUNTIME_PIP_PACKAGES = [
    "openpyxl==3.1.5",
    "playwright==1.58.0",
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
    ".clasp.json",
    "__pycache__/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".venv/",
    ".DS_Store",
]


@dataclass(frozen=True)
class ManagedSystemdUnit:
    name: str
    enable: bool = False
    restart: bool = False


@dataclass(frozen=True)
class NginxTlsConfig:
    listen: tuple[str, ...]
    certificate_path: str
    certificate_key_path: str
    managed_block_label: str = DEFAULT_NGINX_MANAGED_TLS_BLOCK_LABEL


@dataclass(frozen=True)
class NginxPublicRoutesConfig:
    server_config_path: str
    backup_dir: str
    test_command: str
    reload_command: str
    manifest_path: str
    managed_block_label: str = DEFAULT_NGINX_MANAGED_BLOCK_LABEL
    server_names: tuple[str, ...] = field(default_factory=tuple)
    tls: NginxTlsConfig | None = None


@dataclass(frozen=True)
class HostedRuntimeTarget:
    target_status: str
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
    systemd_unit_directory: str = ""
    systemd_units_source_dir: str = ""
    managed_systemd_units: tuple[ManagedSystemdUnit, ...] = field(default_factory=tuple)
    nginx_public_routes: NginxPublicRoutesConfig | None = None
    target_role: str = ""
    target_lifecycle: str = ""
    mutation_policy: str = ""
    host_ip: str = ""
    legacy_host_ip: str = ""
    public_domain: str = ""
    archive_note: str = ""
    provider_side_label_recommendation: str = ""

    @property
    def route_paths(self) -> dict[str, str]:
        return {
            env_name: str(self.runtime_env.get(env_name) or default).strip() or default
            for env_name, default in ROUTE_ENV_DEFAULTS.items()
        }

    @property
    def has_managed_systemd_units(self) -> bool:
        return bool(self.managed_systemd_units)

    @property
    def has_nginx_public_routes(self) -> bool:
        return self.nginx_public_routes is not None

    @property
    def remote_systemd_units_source_dir(self) -> str:
        if not self.systemd_units_source_dir:
            return ""
        return f"{self.target_dir.rstrip('/')}/{self.systemd_units_source_dir.strip('/')}"


def load_hosted_runtime_target(path: Path | None = None) -> HostedRuntimeTarget:
    target_path = path or resolve_target_file()
    payload = json.loads(target_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("hosted runtime target file must contain a JSON object")

    raw_runtime_env = payload.get("runtime_env") or {}
    if not isinstance(raw_runtime_env, dict):
        raise ValueError("runtime_env must be a JSON object")
    runtime_env = {str(key): str(value) for key, value in raw_runtime_env.items()}
    raw_managed_systemd_units = payload.get("managed_systemd_units") or []
    if not isinstance(raw_managed_systemd_units, list):
        raise ValueError("managed_systemd_units must be a JSON array")
    managed_systemd_units: list[ManagedSystemdUnit] = []
    for raw_unit in raw_managed_systemd_units:
        if not isinstance(raw_unit, dict):
            raise ValueError("managed_systemd_units entries must be JSON objects")
        managed_systemd_units.append(
            ManagedSystemdUnit(
                name=str(raw_unit.get("name", "")).strip(),
                enable=bool(raw_unit.get("enable", False)),
                restart=bool(raw_unit.get("restart", False)),
            )
        )
    raw_nginx_public_routes = payload.get("nginx_public_routes")
    nginx_public_routes: NginxPublicRoutesConfig | None = None
    if raw_nginx_public_routes is not None:
        if not isinstance(raw_nginx_public_routes, dict):
            raise ValueError("nginx_public_routes must be a JSON object")
        nginx_public_routes = NginxPublicRoutesConfig(
            server_config_path=str(raw_nginx_public_routes.get("server_config_path", "")).strip(),
            backup_dir=str(raw_nginx_public_routes.get("backup_dir", "")).strip(),
            test_command=str(raw_nginx_public_routes.get("test_command", "")).strip(),
            reload_command=str(raw_nginx_public_routes.get("reload_command", "")).strip(),
            manifest_path=str(
                raw_nginx_public_routes.get(
                    "manifest_path",
                    "artifacts/registry_upload_http_entrypoint/nginx/public_route_allowlist.json",
                )
            ).strip(),
            managed_block_label=str(
                raw_nginx_public_routes.get("managed_block_label", DEFAULT_NGINX_MANAGED_BLOCK_LABEL)
            ).strip()
            or DEFAULT_NGINX_MANAGED_BLOCK_LABEL,
            server_names=_configured_nginx_server_names(raw_nginx_public_routes.get("server_names")),
            tls=_configured_nginx_tls_config(raw_nginx_public_routes.get("tls")),
        )

    return HostedRuntimeTarget(
        target_status=str(payload.get("target_status", ACTIVE_TARGET_STATUS)).strip() or ACTIVE_TARGET_STATUS,
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
        systemd_unit_directory=str(payload.get("systemd_unit_directory", "")).strip(),
        systemd_units_source_dir=str(payload.get("systemd_units_source_dir", "")).strip(),
        managed_systemd_units=tuple(managed_systemd_units),
        nginx_public_routes=nginx_public_routes,
        target_role=str(payload.get("target_role", "")).strip(),
        target_lifecycle=str(payload.get("target_lifecycle", "")).strip(),
        mutation_policy=str(payload.get("mutation_policy", "")).strip(),
        host_ip=str(payload.get("host_ip", "")).strip(),
        legacy_host_ip=str(payload.get("legacy_host_ip", "")).strip(),
        public_domain=str(payload.get("public_domain", "")).strip(),
        archive_note=str(payload.get("archive_note", "")).strip(),
        provider_side_label_recommendation=str(payload.get("provider_side_label_recommendation", "")).strip(),
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
    target_blockers = _target_action_blockers(target)
    mutation_guard = _describe_target_mutation_guard(target)
    deploy_sequence = [
        "sync current checked-out worktree to target_dir via rsync",
        "install required Python runtime packages on the hosted system python",
    ]
    if target.has_managed_systemd_units:
        deploy_sequence.extend(
            [
                "install repo-owned systemd units into systemd_unit_directory",
                "daemon-reload systemd and apply managed unit changes",
            ]
        )
    if target.has_nginx_public_routes:
        deploy_sequence.append(
            "render repo-owned nginx public route allowlist, backup server config, validate nginx config and reload nginx"
        )
    deploy_sequence.extend(
        [
            "restart hosted runtime via restart_command",
            "probe loopback/runtime contour",
            "probe public contour",
        ]
    )
    return {
        "target_status": target.target_status,
        "target_id": target.target_id,
        "target_role": target.target_role or None,
        "target_lifecycle": target.target_lifecycle or None,
        "mutation_policy": target.mutation_policy or None,
        "host_ip": target.host_ip or None,
        "legacy_host_ip": target.legacy_host_ip or None,
        "public_domain": target.public_domain or None,
        "archive_note": target.archive_note or None,
        "provider_side_label_recommendation": target.provider_side_label_recommendation or None,
        "public_base_url": target.public_base_url,
        "loopback_base_url": target.loopback_base_url,
        "ssh_destination": target.ssh_destination or "<local-only>",
        "service_name": target.service_name or "<missing>",
        "target_dir": target.target_dir or "<missing>",
        "environment_file": target.environment_file or "<missing>",
        "systemd_unit_directory": target.systemd_unit_directory or None,
        "systemd_units_source_dir": target.systemd_units_source_dir or None,
        "managed_systemd_units": _describe_managed_systemd_units(target),
        "nginx_public_routes": _describe_nginx_public_routes(target),
        "route_paths": target.route_paths,
        "runtime_env_contract": RUNTIME_ENV_CONTRACT,
        "required_secret_contract": REQUIRED_SECRET_CONTRACT,
        "optional_runtime_contract": OPTIONAL_RUNTIME_CONTRACT,
        "deploy_sequence": deploy_sequence,
        "missing_for_deploy": missing,
        "target_action_blockers": target_blockers,
        "target_mutation_guard": mutation_guard,
        "applicable_to_current_checkout_without_merge": True,
    }


def collect_public_surface(
    *,
    base_url: str,
    route_paths: dict[str, str],
    as_of_date: str | None,
    include_refresh: bool,
    include_feedbacks: bool = False,
    feedbacks_date_from: str | None = None,
    feedbacks_date_to: str | None = None,
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
            name="operator_reports",
            method="GET",
            url=f"{base_url}{route_paths['SHEET_VITRINA_OPERATOR_UI_PATH']}?embedded_tab=reports",
            timeout_seconds=timeout_seconds,
        ),
        _collect_http_probe(
            name="web_vitrina_page",
            method="GET",
            url=f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_UI_PATH}",
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
            name="seller_session_check",
            method="GET",
            url=f"{base_url}{DEFAULT_SELLER_PORTAL_SESSION_CHECK_PATH}",
            timeout_seconds=timeout_seconds,
        ),
        _collect_http_probe(
            name="web_vitrina_read",
            method="GET",
            url=_append_as_of_date(
                f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_READ_PATH}",
                as_of_date,
            ),
            timeout_seconds=timeout_seconds,
        ),
        _collect_http_probe(
            name="web_vitrina_page_composition",
            method="GET",
            url=_append_query_params(
                f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_READ_PATH}",
                {
                    "as_of_date": as_of_date,
                    "surface": DEFAULT_SHEET_WEB_VITRINA_PAGE_COMPOSITION_SURFACE,
                },
            ),
            timeout_seconds=timeout_seconds,
        ),
        _collect_http_probe(
            name="daily_report",
            method="GET",
            url=f"{base_url}{DEFAULT_SHEET_DAILY_REPORT_PATH}",
            timeout_seconds=timeout_seconds,
        ),
        _collect_http_probe(
            name="stock_report",
            method="GET",
            url=f"{base_url}{DEFAULT_SHEET_STOCK_REPORT_PATH}",
            timeout_seconds=timeout_seconds,
        ),
        _collect_http_probe(
            name="plan_report",
            method="GET",
            url=_append_query_params(
                f"{base_url}{DEFAULT_SHEET_PLAN_REPORT_PATH}",
                _build_plan_report_probe_params(as_of_date),
            ),
            timeout_seconds=timeout_seconds,
        ),
        _collect_http_probe(
            name="plan_report_baseline_status",
            method="GET",
            url=f"{base_url}{DEFAULT_SHEET_PLAN_REPORT_BASELINE_STATUS_PATH}",
            timeout_seconds=timeout_seconds,
        ),
        _collect_http_probe(
            name="plan_report_baseline_template",
            method="GET",
            url=f"{base_url}{DEFAULT_SHEET_PLAN_REPORT_BASELINE_TEMPLATE_PATH}",
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
        _collect_http_probe(
            name="wb_regional_status",
            method="GET",
            url=f"{base_url}{DEFAULT_WB_REGIONAL_STATUS_PATH}",
            timeout_seconds=timeout_seconds,
        ),
        _collect_http_probe(
            name="wb_regional_district_central",
            method="GET",
            url=f"{base_url}{DEFAULT_WB_REGIONAL_DISTRICT_DOWNLOAD_PREFIX}/central.xlsx",
            timeout_seconds=timeout_seconds,
        ),
    ]
    if include_feedbacks:
        date_from, date_to = _default_feedbacks_probe_window(
            date_from=feedbacks_date_from,
            date_to=feedbacks_date_to,
        )
        results.append(
            _collect_http_probe(
                name="feedbacks",
                method="GET",
                url=_append_query_params(
                    f"{base_url}{DEFAULT_SHEET_FEEDBACKS_PATH}",
                    {
                        "date_from": date_from,
                        "date_to": date_to,
                        "stars": "1,2,3,4,5",
                        "is_answered": "all",
                    },
                ),
                timeout_seconds=timeout_seconds,
            )
        )
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
    include_feedbacks: bool = False,
    feedbacks_date_from: str | None = None,
    feedbacks_date_to: str | None = None,
    timeout_seconds: float,
) -> dict[str, Any]:
    if target.ssh_destination:
        raw_results = _collect_remote_loopback_surface(
            target,
            as_of_date=as_of_date,
            include_refresh=include_refresh,
            include_feedbacks=include_feedbacks,
            feedbacks_date_from=feedbacks_date_from,
            feedbacks_date_to=feedbacks_date_to,
            timeout_seconds=timeout_seconds,
        )
        transport = "ssh"
    else:
        raw_results = collect_public_surface(
            base_url=target.loopback_base_url,
            route_paths=target.route_paths,
            as_of_date=as_of_date,
            include_refresh=include_refresh,
            include_feedbacks=include_feedbacks,
            feedbacks_date_from=feedbacks_date_from,
            feedbacks_date_to=feedbacks_date_to,
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
    target_file: Path | None,
    dry_run: bool,
    allow_dirty: bool,
    action: str = "deploy",
) -> dict[str, Any]:
    _ensure_target_allows_mutation(target, action=action, dry_run=dry_run)
    missing = _missing_for_deploy(target)
    if missing:
        raise ValueError(f"deploy target is incomplete for deploy: {', '.join(missing)}")
    if not allow_dirty:
        _ensure_clean_worktree()
    _validate_managed_systemd_units(target)

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
    chown_target_dir_command = _remote_shell_command(target, f"chown -R root:root {shlex.quote(target.target_dir)}")
    restart_command = _remote_shell_command(
        target,
        f"cd {shlex.quote(target.target_dir)} && {target.restart_command}",
    )
    runtime_pip_install_command = _build_runtime_pip_install_command(target)
    systemd_commands = _build_managed_systemd_commands(target)
    nginx_public_routes_command = _build_nginx_public_routes_command(target, target_file=target_file, dry_run=dry_run)
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
            "chown_target_dir": chown_target_dir_command,
            "runtime_pip_install": runtime_pip_install_command,
            "systemd_install": systemd_commands["install"],
            "systemd_daemon_reload": systemd_commands["daemon_reload"],
            "restart": restart_command,
            "systemd_enable": systemd_commands["enable"],
            "systemd_restart": systemd_commands["restart"],
            "nginx_public_routes_update": nginx_public_routes_command,
            "status": status_command,
        },
    }
    if dry_run:
        return summary

    _run_command(mkdir_command)
    _run_command(rsync_plan)
    _run_command(chown_target_dir_command)
    _run_command(runtime_pip_install_command)
    if systemd_commands["install"]:
        _run_command(systemd_commands["install"])
        _run_command(systemd_commands["daemon_reload"])
    if nginx_public_routes_command:
        _run_command(nginx_public_routes_command)
    _run_command(restart_command)
    if systemd_commands["enable"]:
        _run_command(systemd_commands["enable"])
    if systemd_commands["restart"]:
        _run_command(systemd_commands["restart"])
    if status_command:
        _run_command(status_command)
    return summary


def _build_runtime_pip_install_command(target: HostedRuntimeTarget) -> list[str]:
    package_names = " ".join(shlex.quote(item) for item in RUNTIME_PIP_PACKAGES)
    python_check = "python3 -c 'import openpyxl, playwright' >/dev/null 2>&1"
    pip_install = f"python3 -m pip install --break-system-packages {package_names}"
    command = f"{python_check} || {pip_install}"
    return _remote_shell_command(target, command)


def apply_nginx_public_routes(target: HostedRuntimeTarget, *, dry_run: bool) -> dict[str, Any]:
    _ensure_target_allows_mutation(target, action="apply-nginx-routes", dry_run=dry_run)
    if not target.nginx_public_routes:
        return {
            "ok": True,
            "configured": False,
            "changed": False,
            "detail": "nginx public route publisher is not configured for this target",
        }

    config = target.nginx_public_routes
    manifest = load_public_route_manifest(_resolve_repo_relative_path(config.manifest_path))
    routes = _validated_public_routes(manifest)
    proxy_pass_url = _normalize_proxy_pass_url(target.loopback_base_url)
    managed_block = render_nginx_public_route_block(
        manifest,
        proxy_pass_url=proxy_pass_url,
        managed_block_label=config.managed_block_label,
    )
    tls_block = ""
    if config.tls:
        tls_block = render_nginx_tls_block(
            config.tls,
            managed_block_label=config.tls.managed_block_label,
        )
    server_path = Path(config.server_config_path)
    if not server_path.exists():
        raise FileNotFoundError(f"nginx server config path not found: {server_path}")
    current_text = server_path.read_text(encoding="utf-8")
    next_text = apply_managed_nginx_public_routes_to_text(
        current_text,
        managed_block=managed_block,
        tls_block=tls_block,
        routes=routes,
        server_names=_nginx_server_names_for_target(target),
        managed_block_label=config.managed_block_label,
        managed_tls_block_label=config.tls.managed_block_label if config.tls else DEFAULT_NGINX_MANAGED_TLS_BLOCK_LABEL,
    )
    changed = current_text != next_text
    summary: dict[str, Any] = {
        "ok": True,
        "configured": True,
        "dry_run": dry_run,
        "changed": changed,
        "server_config_path": config.server_config_path,
        "manifest_path": config.manifest_path,
        "route_count": len(routes),
        "managed_block_label": config.managed_block_label,
        "server_names": list(_nginx_server_names_for_target(target)),
        "proxy_pass_url": proxy_pass_url,
        "tls": _describe_nginx_tls(config.tls),
    }
    if dry_run:
        return summary
    backup_path = None
    if changed:
        backup_dir = Path(config.backup_dir or str(server_path.parent))
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = backup_dir / f"{server_path.name}.bak_wb_core_routes_{timestamp}"
        backup_path.write_text(current_text, encoding="utf-8")
        server_path.write_text(next_text, encoding="utf-8")
    test_result = _run_shell_command(config.test_command)
    if test_result.returncode != 0:
        if changed:
            server_path.write_text(current_text, encoding="utf-8")
        raise RuntimeError(
            "nginx config validation failed"
            f"\nstdout:\n{test_result.stdout}\nstderr:\n{test_result.stderr}"
        )
    reload_result = _run_shell_command(config.reload_command)
    if reload_result.returncode != 0:
        raise RuntimeError(
            "nginx reload failed"
            f"\nstdout:\n{reload_result.stdout}\nstderr:\n{reload_result.stderr}"
        )
    summary["backup_path"] = str(backup_path) if backup_path else None
    summary["nginx_test_stdout"] = test_result.stdout.strip()
    summary["nginx_test_stderr"] = test_result.stderr.strip()
    summary["nginx_reload_stdout"] = reload_result.stdout.strip()
    summary["nginx_reload_stderr"] = reload_result.stderr.strip()
    return summary


def load_public_route_manifest(path: Path = DEFAULT_PUBLIC_ROUTE_ALLOWLIST_FILE) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("public route allowlist manifest must contain a JSON object")
    _validated_public_routes(payload)
    return payload


def render_nginx_public_route_block(
    manifest: dict[str, Any],
    *,
    proxy_pass_url: str,
    managed_block_label: str = DEFAULT_NGINX_MANAGED_BLOCK_LABEL,
) -> str:
    routes = _validated_public_routes(manifest)
    read_timeout = _nginx_scalar(str(manifest.get("proxy_read_timeout") or "180s"))
    send_timeout = _nginx_scalar(str(manifest.get("proxy_send_timeout") or "180s"))
    lines = [
        f"    # BEGIN {managed_block_label}",
        "    # Generated by wb-core deploy runner from repo-owned public route allowlist.",
        "    # Do not edit this block manually; edit the manifest and redeploy.",
    ]
    for route in routes:
        modifier = "=" if route["match"] == "exact" else "^~"
        methods = ", ".join(route.get("methods") or [])
        lines.extend(
            [
                f"    # {route['name']} ({methods})",
                f"    location {modifier} {route['path']} {{",
                f"        proxy_pass {proxy_pass_url};",
                "        proxy_set_header Host $host;",
                "        proxy_set_header X-Real-IP $remote_addr;",
                "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
                f"        proxy_read_timeout {read_timeout};",
                f"        proxy_send_timeout {send_timeout};",
                "    }",
                "",
            ]
        )
    lines.append(f"    # END {managed_block_label}")
    return "\n".join(lines) + "\n"


def render_nginx_tls_block(
    config: NginxTlsConfig,
    *,
    managed_block_label: str = DEFAULT_NGINX_MANAGED_TLS_BLOCK_LABEL,
) -> str:
    listen_directives = tuple(_safe_nginx_listen_directive(item) for item in config.listen)
    if not listen_directives:
        raise ValueError("nginx_public_routes.tls.listen must contain at least one directive")
    certificate_path = _safe_nginx_absolute_path(
        config.certificate_path,
        field_name="nginx_public_routes.tls.certificate_path",
    )
    certificate_key_path = _safe_nginx_absolute_path(
        config.certificate_key_path,
        field_name="nginx_public_routes.tls.certificate_key_path",
    )
    lines = [
        f"    # BEGIN {managed_block_label}",
        "    # Generated by wb-core deploy runner from target TLS config.",
        "    # Do not edit this block manually; edit the target and redeploy.",
    ]
    for listen in listen_directives:
        lines.append(f"    listen {listen};")
    lines.extend(
        [
            f"    ssl_certificate {certificate_path};",
            f"    ssl_certificate_key {certificate_key_path};",
            f"    # END {managed_block_label}",
        ]
    )
    return "\n".join(lines) + "\n"


def apply_managed_nginx_public_routes_to_text(
    text: str,
    *,
    managed_block: str,
    routes: list[dict[str, Any]],
    tls_block: str = "",
    server_name: str | None = None,
    server_names: tuple[str, ...] | list[str] | None = None,
    managed_block_label: str = DEFAULT_NGINX_MANAGED_BLOCK_LABEL,
    managed_tls_block_label: str = DEFAULT_NGINX_MANAGED_TLS_BLOCK_LABEL,
) -> str:
    desired_server_names = _normalize_nginx_server_names(server_names or ([server_name] if server_name else []))
    route_keys = {_nginx_route_key(route) for route in routes}
    route_keys.update(("", str(route["path"])) for route in routes)
    without_managed = _remove_managed_nginx_block(text, managed_block_label)
    without_managed = _remove_managed_nginx_block(without_managed, managed_tls_block_label)
    without_route_blocks = _remove_nginx_location_blocks(without_managed, route_keys)
    return _insert_managed_nginx_block(
        without_route_blocks,
        managed_block=managed_block,
        tls_block=tls_block,
        server_names=desired_server_names,
    )


def _build_nginx_public_routes_command(
    target: HostedRuntimeTarget,
    *,
    target_file: Path | None,
    dry_run: bool,
) -> list[str] | None:
    if not target.has_nginx_public_routes:
        return None
    try:
        remote_target_file = _remote_repo_relative_path(target, target_file or DEFAULT_TARGET_FILE)
    except ValueError:
        if not dry_run:
            raise
        remote_target_file = (
            f"{target.target_dir.rstrip('/')}/"
            "artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__example.json"
        )
    command = (
        f"cd {shlex.quote(target.target_dir)} && "
        "python3 apps/registry_upload_http_entrypoint_hosted_runtime.py "
        f"--target-file {shlex.quote(remote_target_file)} apply-nginx-routes"
    )
    return _remote_shell_command(target, command)


def _describe_nginx_public_routes(target: HostedRuntimeTarget) -> dict[str, Any] | None:
    if not target.nginx_public_routes:
        return None
    manifest = load_public_route_manifest(_resolve_repo_relative_path(target.nginx_public_routes.manifest_path))
    routes = _validated_public_routes(manifest)
    return {
        "server_config_path": target.nginx_public_routes.server_config_path,
        "backup_dir": target.nginx_public_routes.backup_dir,
        "test_command": target.nginx_public_routes.test_command,
        "reload_command": target.nginx_public_routes.reload_command,
        "manifest_path": target.nginx_public_routes.manifest_path,
        "managed_block_label": target.nginx_public_routes.managed_block_label,
        "server_names": list(_nginx_server_names_for_target(target)),
        "tls": _describe_nginx_tls(target.nginx_public_routes.tls),
        "route_count": len(routes),
        "routes": [
            {
                "name": route["name"],
                "match": route["match"],
                "path": route["path"],
                "methods": route.get("methods") or [],
            }
            for route in routes
        ],
    }


def _describe_nginx_tls(config: NginxTlsConfig | None) -> dict[str, Any]:
    if not config:
        return {"configured": False}
    return {
        "configured": True,
        "listen": list(config.listen),
        "certificate_path": config.certificate_path,
        "certificate_key_path": config.certificate_key_path,
        "managed_block_label": config.managed_block_label,
    }


def _validated_public_routes(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    raw_routes = manifest.get("routes")
    if not isinstance(raw_routes, list) or not raw_routes:
        raise ValueError("public route allowlist manifest must contain a non-empty routes array")
    routes: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, raw_route in enumerate(raw_routes):
        if not isinstance(raw_route, dict):
            raise ValueError(f"public route #{index + 1} must be a JSON object")
        name = _safe_route_name(raw_route.get("name"))
        match = str(raw_route.get("match") or "").strip()
        path = _safe_nginx_path(raw_route.get("path"))
        if match not in {"exact", "prefix"}:
            raise ValueError(f"public route {name} must use match exact or prefix")
        if match == "prefix" and not path.endswith("/"):
            raise ValueError(f"public route {name} prefix path must end with /")
        methods = raw_route.get("methods") or []
        if not isinstance(methods, list) or not methods:
            raise ValueError(f"public route {name} must include methods")
        normalized_methods = []
        for method in methods:
            normalized_method = str(method).strip().upper()
            if not re.fullmatch(r"[A-Z]+", normalized_method):
                raise ValueError(f"public route {name} has invalid method {method!r}")
            normalized_methods.append(normalized_method)
        key = (match, path)
        if key in seen:
            raise ValueError(f"duplicate public route location for {match} {path}")
        seen.add(key)
        routes.append(
            {
                "name": name,
                "match": match,
                "path": path,
                "methods": normalized_methods,
            }
        )
    return routes


def _safe_route_name(value: Any) -> str:
    name = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError(f"invalid public route name {value!r}")
    return name


def _safe_nginx_path(value: Any) -> str:
    path = str(value or "").strip()
    if not path.startswith("/") or re.search(r"[\s{};]", path):
        raise ValueError(f"invalid public route path {value!r}")
    return path


def _nginx_scalar(value: str) -> str:
    normalized = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", normalized):
        raise ValueError(f"invalid nginx scalar value {value!r}")
    return normalized


def _nginx_route_key(route: dict[str, Any]) -> tuple[str, str]:
    modifier = "=" if route["match"] == "exact" else "^~"
    return modifier, str(route["path"])


def _remove_managed_nginx_block(text: str, managed_block_label: str) -> str:
    pattern = re.compile(
        rf"\n?[ \t]*# BEGIN {re.escape(managed_block_label)}\n.*?[ \t]*# END {re.escape(managed_block_label)}\n?",
        re.DOTALL,
    )
    return pattern.sub("\n", text)


def _remove_nginx_location_blocks(text: str, route_keys: set[tuple[str, str]]) -> str:
    location_pattern = re.compile(
        r"(?ms)^[ \t]*location\s+(?:(=|\^~)\s+)?(/[^ \t{]+)\s*\{\n.*?^[ \t]*\}\n?"
    )

    def replace(match: re.Match[str]) -> str:
        modifier = match.group(1) or ""
        path = match.group(2)
        key = (modifier, path)
        if key in route_keys:
            return ""
        return match.group(0)

    return location_pattern.sub(replace, text)


def _insert_managed_nginx_block(
    text: str,
    *,
    managed_block: str,
    server_names: tuple[str, ...],
    tls_block: str = "",
) -> str:
    desired_server_names = _normalize_nginx_server_names(server_names)
    desired_set = set(desired_server_names)
    pattern = re.compile(r"(?m)^([ \t]*)server_name\s+([^;]+);[ \t]*$")
    for match in pattern.finditer(text):
        current_names = set(match.group(2).split())
        if not (current_names & desired_set):
            continue
        directive = f"{match.group(1)}server_name {' '.join(desired_server_names)};"
        rewritten = text[:match.start()] + directive + text[match.end():]
        insertion_point = match.start() + len(directive)
        tail = re.sub(r"^\n+", "\n", rewritten[insertion_point:])
        blocks = [block.rstrip() for block in (tls_block, managed_block) if block.strip()]
        return rewritten[:insertion_point] + "\n\n" + "\n\n".join(blocks) + "\n" + tail
    raise ValueError(f"none of server_names {list(desired_server_names)!r} found in nginx server config")


def _configured_nginx_server_names(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("nginx_public_routes.server_names must be a JSON array when provided")
    return _normalize_nginx_server_names(str(item) for item in value)


def _configured_nginx_tls_config(value: Any) -> NginxTlsConfig | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("nginx_public_routes.tls must be a JSON object when provided")
    raw_listen = value.get("listen", ["443 ssl"])
    if not isinstance(raw_listen, list):
        raise ValueError("nginx_public_routes.tls.listen must be a JSON array")
    listen = tuple(_safe_nginx_listen_directive(str(item)) for item in raw_listen)
    if not listen:
        raise ValueError("nginx_public_routes.tls.listen must contain at least one directive")
    return NginxTlsConfig(
        listen=listen,
        certificate_path=_safe_nginx_absolute_path(
            value.get("certificate_path"),
            field_name="nginx_public_routes.tls.certificate_path",
        ),
        certificate_key_path=_safe_nginx_absolute_path(
            value.get("certificate_key_path"),
            field_name="nginx_public_routes.tls.certificate_key_path",
        ),
        managed_block_label=str(value.get("managed_block_label", DEFAULT_NGINX_MANAGED_TLS_BLOCK_LABEL)).strip()
        or DEFAULT_NGINX_MANAGED_TLS_BLOCK_LABEL,
    )


def _nginx_server_names_for_target(target: HostedRuntimeTarget) -> tuple[str, ...]:
    if target.nginx_public_routes and target.nginx_public_routes.server_names:
        return target.nginx_public_routes.server_names
    return (_safe_nginx_server_name(_server_name_from_public_base_url(target.public_base_url)),)


def _normalize_nginx_server_names(values: Any) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values or []:
        server_name = _safe_nginx_server_name(value)
        if server_name not in normalized:
            normalized.append(server_name)
    if not normalized:
        raise ValueError("at least one nginx server_name is required")
    return tuple(normalized)


def _safe_nginx_server_name(value: Any) -> str:
    server_name = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", server_name):
        raise ValueError(f"invalid nginx server_name {value!r}")
    if server_name == "_" or ".." in server_name:
        raise ValueError(f"invalid nginx server_name {value!r}")
    return server_name


def _safe_nginx_listen_directive(value: Any) -> str:
    normalized = " ".join(str(value or "").strip().split())
    if not normalized:
        raise ValueError("nginx listen directive must not be empty")
    if any(char in normalized for char in "{};\n\r\t"):
        raise ValueError(f"invalid nginx listen directive {value!r}")
    for token in normalized.split(" "):
        if not re.fullmatch(r"[A-Za-z0-9_.:\[\]-]+", token):
            raise ValueError(f"invalid nginx listen directive {value!r}")
    return normalized


def _safe_nginx_absolute_path(value: Any, *, field_name: str) -> str:
    path = str(value or "").strip()
    if not path.startswith("/"):
        raise ValueError(f"{field_name} must be an absolute path")
    if any(char in path for char in "{};\n\r\t "):
        raise ValueError(f"invalid {field_name} value")
    if "/../" in path or path.endswith("/..") or path.startswith("/.."):
        raise ValueError(f"invalid {field_name} value")
    if not re.fullmatch(r"/[A-Za-z0-9_./:@=+-]+", path):
        raise ValueError(f"invalid {field_name} value")
    return path


def _normalize_proxy_pass_url(loopback_base_url: str) -> str:
    parsed = urllib_parse.urlparse(loopback_base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"invalid loopback_base_url for nginx proxy_pass: {loopback_base_url!r}")
    return f"{parsed.scheme}://{parsed.netloc}"


def _server_name_from_public_base_url(public_base_url: str) -> str:
    parsed = urllib_parse.urlparse(public_base_url)
    if not parsed.hostname:
        raise ValueError(f"invalid public_base_url for nginx server_name: {public_base_url!r}")
    return parsed.hostname


def _run_shell_command(command: str) -> subprocess.CompletedProcess[str]:
    if _is_placeholder(command):
        raise ValueError("nginx command is not configured")
    return subprocess.run(
        command,
        shell=True,
        check=False,
        capture_output=True,
        text=True,
    )


def run_public_probe_command(args: argparse.Namespace) -> int:
    target = load_hosted_runtime_target(args.target_file)
    _warn_if_rollback_read_only_target(target, action="public-probe")
    raw_results = collect_public_surface(
        base_url=target.public_base_url,
        route_paths=target.route_paths,
        as_of_date=args.as_of_date,
        include_refresh=not args.skip_refresh,
        include_feedbacks=args.include_feedbacks,
        feedbacks_date_from=args.feedbacks_date_from,
        feedbacks_date_to=args.feedbacks_date_to,
        timeout_seconds=args.timeout_seconds,
    )
    payload = {
        "target_id": target.target_id,
        "base_url": target.public_base_url,
        "as_of_date": args.as_of_date,
        "include_refresh": not args.skip_refresh,
        "include_feedbacks": args.include_feedbacks,
        **evaluate_surface_results(raw_results, route_paths=target.route_paths),
    }
    _print_json(payload)
    return 0 if payload["ok"] else 1


def run_loopback_probe_command(args: argparse.Namespace) -> int:
    target = load_hosted_runtime_target(args.target_file)
    _warn_if_rollback_read_only_target(target, action="loopback-probe")
    payload = {
        "target_id": target.target_id,
        "as_of_date": args.as_of_date,
        "include_refresh": not args.skip_refresh,
        "include_feedbacks": args.include_feedbacks,
        **collect_loopback_surface(
            target,
            as_of_date=args.as_of_date,
            include_refresh=not args.skip_refresh,
            include_feedbacks=args.include_feedbacks,
            feedbacks_date_from=args.feedbacks_date_from,
            feedbacks_date_to=args.feedbacks_date_to,
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
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    payload = {
        "target_id": target.target_id,
        **deploy_current_checkout(
            target,
            target_file=target_file,
            dry_run=args.dry_run,
            allow_dirty=args.allow_dirty,
            action="deploy",
        ),
    }
    _print_json(payload)
    return 0


def run_deploy_and_verify_command(args: argparse.Namespace) -> int:
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    deploy_summary = deploy_current_checkout(
        target,
        target_file=target_file,
        dry_run=args.dry_run,
        allow_dirty=args.allow_dirty,
        action="deploy-and-verify",
    )
    loopback_summary = collect_loopback_surface(
        target,
        as_of_date=args.as_of_date,
        include_refresh=not args.skip_refresh,
        include_feedbacks=args.include_feedbacks,
        feedbacks_date_from=args.feedbacks_date_from,
        feedbacks_date_to=args.feedbacks_date_to,
        timeout_seconds=args.timeout_seconds,
    )
    public_summary = evaluate_surface_results(
        collect_public_surface(
            base_url=target.public_base_url,
            route_paths=target.route_paths,
            as_of_date=args.as_of_date,
            include_refresh=not args.skip_refresh,
            include_feedbacks=args.include_feedbacks,
            feedbacks_date_from=args.feedbacks_date_from,
            feedbacks_date_to=args.feedbacks_date_to,
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
            "include_feedbacks": args.include_feedbacks,
            **public_summary,
        },
        "ok": deploy_summary["ok"] and loopback_summary["ok"] and public_summary["ok"],
    }
    _print_json(payload)
    return 0 if payload["ok"] else 1


def run_apply_nginx_routes_command(args: argparse.Namespace) -> int:
    target = load_hosted_runtime_target(args.target_file)
    payload = {
        "target_id": target.target_id,
        "nginx_public_routes": apply_nginx_public_routes(target, dry_run=args.dry_run),
    }
    payload["ok"] = bool(payload["nginx_public_routes"].get("ok"))
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

    apply_nginx_routes = subparsers.add_parser(
        "apply-nginx-routes",
        help="Apply repo-owned nginx public route allowlist on this host.",
    )
    apply_nginx_routes.add_argument("--dry-run", action="store_true", help="Render/compare without writing nginx config.")
    apply_nginx_routes.set_defaults(handler=run_apply_nginx_routes_command)

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
    parser.add_argument(
        "--include-feedbacks",
        action="store_true",
        help="Also probe GET /v1/sheet-vitrina-v1/feedbacks with a bounded date query.",
    )
    parser.add_argument("--feedbacks-date-from", default=None, help="YYYY-MM-DD start date for feedbacks probe.")
    parser.add_argument("--feedbacks-date-to", default=None, help="YYYY-MM-DD end date for feedbacks probe.")
    parser.add_argument("--timeout-seconds", type=float, default=180.0, help="HTTP probe timeout in seconds.")


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
            body_text, body_truncated, body_bytes_read = _read_probe_response_body(response)
            return {
                "route": name,
                "method": method,
                "url": url,
                "http_status": response.getcode(),
                "content_type": response.headers.get("Content-Type", ""),
                "body_excerpt": body_text,
                "body_truncated": body_truncated,
                "body_bytes_read": body_bytes_read,
                "json_body": _try_load_json(body_text),
                "network_error": None,
            }
    except urllib_error.HTTPError as exc:
        body_text, body_truncated, body_bytes_read = _read_probe_response_body(exc)
        return {
            "route": name,
            "method": method,
            "url": url,
            "http_status": exc.code,
            "content_type": exc.headers.get("Content-Type", ""),
            "body_excerpt": body_text,
            "body_truncated": body_truncated,
            "body_bytes_read": body_bytes_read,
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
            "Web-витрина",
            "Витрина",
            "Расчет поставок",
            "Отчеты",
            "Отзывы",
            "Загрузить и обновить",
            "Загрузка данных",
            "Действия и состояния",
            "Проверить сессию",
            "Восстановить сессию",
            "Скачать лаунчер",
            'data-unified-tab-button="vitrina"',
            'data-unified-tab-button="factory-order"',
            'data-unified-tab-button="reports"',
            'data-unified-tab-button="feedbacks"',
            'data-operator-embed-frame="factory-order"',
            'data-operator-embed-frame="reports"',
            DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
            DEFAULT_SHEET_FEEDBACKS_PATH,
            "/v1/sheet-vitrina-v1/feedbacks/ai-prompt",
            "/v1/sheet-vitrina-v1/feedbacks/ai-analyze",
            "surface=page_composition",
            route_paths["SHEET_VITRINA_REFRESH_HTTP_PATH"],
            DEFAULT_SHEET_JOB_PATH,
        ]
        missing_tokens = [token for token in tokens if token not in body]
        forbidden_tokens = [
            "dailyReportToggle",
            "stockReportToggle",
            "report-accordion",
            "<h1>",
        ]
        present_forbidden = [token for token in forbidden_tokens if token in body]
        evaluation["ok"] = status == 200 and "text/html" in content_type and not missing_tokens and not present_forbidden
        evaluation["detail"] = (
            "operator page shape ok"
            if evaluation["ok"]
            else f"expected 200 text/html with operator tokens, missing={missing_tokens}, forbidden={present_forbidden}"
        )
        return evaluation

    if route == "operator_reports":
        body = str(result.get("body_excerpt", ""))
        tokens = [
            "Отчёты",
            "Ежедневные отчёты",
            "Отчёт по остаткам",
            "Выполнение плана",
            "Исторические данные для отчёта",
            "planReportApplyButton",
            "planReportBaselineTemplateButton",
            "planReportBaselineFileInput",
            "planReportBaselineStatus",
            DEFAULT_SHEET_DAILY_REPORT_PATH,
            DEFAULT_SHEET_STOCK_REPORT_PATH,
            DEFAULT_SHEET_PLAN_REPORT_PATH,
            DEFAULT_SHEET_PLAN_REPORT_BASELINE_STATUS_PATH,
            DEFAULT_SHEET_PLAN_REPORT_BASELINE_TEMPLATE_PATH,
            'data-report-section-button="daily"',
            'data-report-section-button="stock"',
            'data-report-section-button="plan"',
            'data-report-section-panel="plan"',
        ]
        missing_tokens = [token for token in tokens if token not in body]
        evaluation["ok"] = status == 200 and "text/html" in content_type and not missing_tokens
        evaluation["detail"] = (
            "operator reports embedded panel ok"
            if evaluation["ok"]
            else f"expected 200 text/html with reports/baseline tokens, missing={missing_tokens}"
        )
        return evaluation

    if route == "seller_session_check":
        json_body = result.get("json_body") or {}
        allowed_statuses = {
            "session_valid_canonical",
            "session_valid_wrong_org",
            "session_invalid",
            "session_missing",
            "session_probe_error",
        }
        evaluation["ok"] = (
            status == 200
            and "application/json" in content_type
            and str(json_body.get("status") or "") in allowed_statuses
        )
        evaluation["detail"] = (
            "seller session-check route ok"
            if evaluation["ok"]
            else "expected 200 JSON seller session-check route with truthful session status"
        )
        return evaluation

    if route == "web_vitrina_page":
        body = str(result.get("body_excerpt", ""))
        tokens = [
            "Web-витрина",
            "Загрузить и обновить",
            DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
            route_paths["SHEET_VITRINA_OPERATOR_UI_PATH"],
            "surface=page_composition",
            "web_vitrina_page_composition",
            "data-top-panel",
            "data-global-progress",
            "data-filter-controls",
            "data-loading-table",
            "data-loading-table-head",
            "data-loading-table-body",
            "Действия и состояния",
            "Загрузка данных",
            "Обновить группу",
            "Проверить сессию",
            "Восстановить сессию",
            "Скачать лаунчер",
            "Отзывы",
            "Загрузить отзывы",
            "AI-промпт разбора",
            "AI-разбор отзывов",
            DEFAULT_SHEET_FEEDBACKS_PATH,
            "/v1/sheet-vitrina-v1/feedbacks/ai-prompt",
            "/v1/sheet-vitrina-v1/feedbacks/ai-analyze",
            "Лог",
        ]
        missing_tokens = [token for token in tokens if token not in body]
        removed_tokens = [
            token
            for token in ("data-update-summary", "data-retry-button", "data-status-badge", "JSON Connect", "Обновление данных")
            if token in body
        ]
        evaluation["ok"] = (
            status == 200
            and "text/html" in content_type
            and not missing_tokens
            and not removed_tokens
        )
        evaluation["detail"] = (
            "web-vitrina page composition shell ok"
            if evaluation["ok"]
            else (
                "expected 200 text/html with web-vitrina page tokens, "
                f"missing={missing_tokens}, removed_tokens_present={removed_tokens}"
            )
        )
        return evaluation

    if route in {
        "web_vitrina_page_composition",
        "factory_order_template_stock_ff",
        "factory_order_template_inbound_factory",
        "factory_order_template_inbound_ff_to_wb",
        "plan_report_baseline_template",
    }:
        if route == "web_vitrina_page_composition":
            json_body = result.get("json_body") or {}
            evaluation["ok"] = (
                status == 200
                and "application/json" in content_type
                and json_body.get("composition_name") == "web_vitrina_page_composition"
                and json_body.get("composition_version") == "v1"
                and isinstance(json_body.get("table_surface"), dict)
                and isinstance((json_body.get("activity_surface") or {}).get("loading_table"), dict)
                and "update_summary" not in (json_body.get("activity_surface") or {})
            )
            evaluation["detail"] = (
                "web-vitrina page composition surface ok"
                if evaluation["ok"]
                else "expected 200 JSON page composition surface on web-vitrina read route"
            )
            return evaluation
        evaluation["ok"] = status == 200 and "spreadsheetml.sheet" in content_type
        label = "plan-report baseline template" if route == "plan_report_baseline_template" else "factory-order template"
        evaluation["detail"] = (
            f"{label} download route ok"
            if evaluation["ok"]
            else f"expected 200 XLSX response for {label} route"
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

    if route == "wb_regional_district_central" and status == 200:
        evaluation["ok"] = "spreadsheetml.sheet" in content_type
        evaluation["detail"] = (
            "wb-regional district route returned XLSX"
            if evaluation["ok"]
            else "expected XLSX content-type for successful district route"
        )
        return evaluation

    if "application/json" not in content_type:
        evaluation["ok"] = False
        evaluation["detail"] = f"expected JSON content-type, got {result.get('content_type', '')!r}"
        return evaluation

    payload = result.get("json_body")
    if not isinstance(payload, dict):
        if result.get("body_truncated"):
            payload = _synthetic_payload_from_truncated_json(str(result.get("body_excerpt") or ""))
        else:
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
                "manual_context",
            ],
            error_keys=["error", "server_context", "manual_context"],
        )
        return evaluation

    if route == "web_vitrina_read":
        evaluation["ok"], evaluation["detail"] = _validate_json_result(
            status,
            payload,
            success_keys=[
                "contract_name",
                "contract_version",
                "page_route",
                "read_route",
                "meta",
                "status_summary",
                "schema",
                "rows",
                "capabilities",
            ],
        )
        return evaluation

    if route == "daily_report":
        evaluation["ok"], evaluation["detail"] = _validate_json_result(
            status,
            payload,
            success_keys=[
                "status",
                "business_timezone",
                "current_business_date",
                "comparison_basis",
                "newer_closed_date",
                "older_closed_date",
                "notes",
            ],
        )
        return evaluation

    if route == "stock_report":
        evaluation["ok"], evaluation["detail"] = _validate_json_result(
            status,
            payload,
            success_keys=[
                "status",
                "business_timezone",
                "current_business_date",
                "report_date",
                "threshold_lt",
                "districts",
                "source_of_truth",
                "notes",
            ],
        )
        return evaluation

    if route == "plan_report":
        evaluation["ok"], evaluation["detail"] = _validate_json_result(
            status,
            payload,
            success_keys=[
                "status",
                "business_timezone",
                "current_business_date",
                "reference_date",
                "selected_period_key",
                "selected_period_label",
                "source_of_truth",
                "coverage",
                "periods",
                "notes",
            ],
        )
        return evaluation

    if route == "plan_report_baseline_status":
        evaluation["ok"], evaluation["detail"] = _validate_json_result(
            status,
            payload,
            success_keys=[
                "status",
                "source_kind",
                "row_count",
                "months",
                "totals",
                "warning",
            ],
        )
        return evaluation

    if route == "plan_report_baseline_template":
        evaluation["ok"] = status == 200 and "spreadsheetml.sheet" in content_type
        evaluation["detail"] = (
            "plan-report baseline template route ok"
            if evaluation["ok"]
            else "expected 200 XLSX plan-report baseline template route"
        )
        return evaluation

    if route == "feedbacks":
        evaluation["ok"], evaluation["detail"] = _validate_json_result(
            status,
            payload,
            success_keys=[
                "contract_name",
                "contract_version",
                "meta",
                "summary",
                "schema",
                "rows",
            ],
        )
        if evaluation["ok"] and payload.get("contract_name") != "sheet_vitrina_v1_feedbacks":
            evaluation["ok"] = False
            evaluation["detail"] = f"expected sheet_vitrina_v1_feedbacks contract, got {payload.get('contract_name')!r}"
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

    if route == "wb_regional_status":
        evaluation["ok"], evaluation["detail"] = _validate_json_result(
            status,
            payload,
            success_keys=[
                "status",
                "active_sku_count",
                "methodology_note",
                "shared_datasets",
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

    if route == "web_vitrina_group_refresh_missing_group":
        error_text = str(payload.get("error", "") or payload.get("detail", "") or "")
        evaluation["ok"] = status == 400 and "source_group_id is required" in error_text
        evaluation["detail"] = (
            "web-vitrina group-refresh route is publicly published and reached app-level validation"
            if evaluation["ok"]
            else "expected app-level JSON 400 for POST group-refresh without source_group_id"
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

    if route == "wb_regional_district_central":
        error_text = str(payload.get("error", "") or "")
        evaluation["ok"] = status == 422 and bool(error_text)
        evaluation["detail"] = (
            "wb-regional district route published with truthful 422 before calculation"
            if evaluation["ok"]
            else "expected 200 XLSX or 422 JSON error for district route"
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


def _read_probe_response_body(response: Any) -> tuple[str, bool, int]:
    content_type = str(response.headers.get("Content-Type", "") or "").lower()
    if any(
        binary_type in content_type
        for binary_type in (
            "spreadsheetml.sheet",
            "application/zip",
            "application/octet-stream",
        )
    ):
        return "", False, 0
    raw = response.read(PROBE_BODY_LIMIT_BYTES + 1)
    body_truncated = len(raw) > PROBE_BODY_LIMIT_BYTES
    if body_truncated:
        raw = raw[:PROBE_BODY_LIMIT_BYTES]
    return raw.decode("utf-8", errors="replace"), body_truncated, len(raw)


def _synthetic_payload_from_truncated_json(body: str) -> dict[str, Any]:
    return {
        match.group(1): True
        for match in re.finditer(r'"([^"\\]+)"\s*:', body)
    }


def _collect_remote_loopback_surface(
    target: HostedRuntimeTarget,
    *,
    as_of_date: str | None,
    include_refresh: bool,
    include_feedbacks: bool,
    feedbacks_date_from: str | None,
    feedbacks_date_to: str | None,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    script = _build_remote_probe_script(
        base_url=target.loopback_base_url,
        route_paths=target.route_paths,
        as_of_date=as_of_date,
        include_refresh=include_refresh,
        include_feedbacks=include_feedbacks,
        feedbacks_date_from=feedbacks_date_from,
        feedbacks_date_to=feedbacks_date_to,
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
    include_feedbacks: bool,
    feedbacks_date_from: str | None,
    feedbacks_date_to: str | None,
    timeout_seconds: float,
) -> str:
    normalized_feedbacks_date_from = None
    normalized_feedbacks_date_to = None
    if include_feedbacks:
        normalized_feedbacks_date_from, normalized_feedbacks_date_to = _default_feedbacks_probe_window(
            date_from=feedbacks_date_from,
            date_to=feedbacks_date_to,
        )
    payload = {
        "base_url": base_url,
        "route_paths": route_paths,
        "as_of_date": as_of_date,
        "include_refresh": include_refresh,
        "include_feedbacks": include_feedbacks,
        "feedbacks_date_from": normalized_feedbacks_date_from,
        "feedbacks_date_to": normalized_feedbacks_date_to,
        "timeout_seconds": timeout_seconds,
    }
    payload_json = json.dumps(payload, ensure_ascii=True)
    return f"""import json
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

PAYLOAD = json.loads({payload_json!r})
PROBE_BODY_LIMIT_BYTES = {PROBE_BODY_LIMIT_BYTES!r}

def _append_as_of_date(url, as_of_date):
    if not as_of_date:
        return url
    query = urllib_parse.urlencode({{"as_of_date": as_of_date}})
    separator = '&' if '?' in url else '?'
    return f"{{url}}{{separator}}{{query}}"

def _append_query_params(url, params):
    normalized = {{
        str(key): str(value)
        for key, value in params.items()
        if value not in {{None, ''}}
    }}
    if not normalized:
        return url
    query = urllib_parse.urlencode(normalized)
    separator = '&' if '?' in url else '?'
    return f"{{url}}{{separator}}{{query}}"

def _plan_report_params(as_of_date):
    params = {{
        "period": "current_month",
        "h1_buyout_plan_rub": "272000",
        "h2_buyout_plan_rub": "638000",
        "plan_drr_pct": "10",
    }}
    if as_of_date:
        params["as_of_date"] = as_of_date
    return params

def _try_load_json(body_text):
    try:
        return json.loads(body_text)
    except json.JSONDecodeError:
        return None

def _read_probe_response_body(response):
    content_type = str(response.headers.get("Content-Type", "") or "").lower()
    if any(
        binary_type in content_type
        for binary_type in (
            "spreadsheetml.sheet",
            "application/zip",
            "application/octet-stream",
        )
    ):
        return "", False, 0
    raw = response.read(PROBE_BODY_LIMIT_BYTES + 1)
    body_truncated = len(raw) > PROBE_BODY_LIMIT_BYTES
    if body_truncated:
        raw = raw[:PROBE_BODY_LIMIT_BYTES]
    return raw.decode("utf-8", errors="replace"), body_truncated, len(raw)


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
            body_text, body_truncated, body_bytes_read = _read_probe_response_body(response)
            return {{
                "route": name,
                "method": method,
                "url": url,
                "http_status": response.getcode(),
                "content_type": response.headers.get("Content-Type", ""),
                "body_excerpt": body_text,
                "body_truncated": body_truncated,
                "body_bytes_read": body_bytes_read,
                "json_body": _try_load_json(body_text),
                "network_error": None,
            }}
    except urllib_error.HTTPError as exc:
        body_text, body_truncated, body_bytes_read = _read_probe_response_body(exc)
        return {{
            "route": name,
            "method": method,
            "url": url,
            "http_status": exc.code,
            "content_type": exc.headers.get("Content-Type", ""),
            "body_excerpt": body_text,
            "body_truncated": body_truncated,
            "body_bytes_read": body_bytes_read,
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
    _collect("operator_reports", "GET", PAYLOAD["base_url"] + PAYLOAD["route_paths"]["SHEET_VITRINA_OPERATOR_UI_PATH"] + "?embedded_tab=reports"),
    _collect("web_vitrina_page", "GET", PAYLOAD["base_url"] + {DEFAULT_SHEET_WEB_VITRINA_UI_PATH!r}),
    _collect("load_route", "GET", PAYLOAD["base_url"] + "/v1/sheet-vitrina-v1/load"),
    _collect("job", "GET", PAYLOAD["base_url"] + "/v1/sheet-vitrina-v1/job?job_id=hosted_runtime_probe"),
    _collect("status", "GET", _append_as_of_date(PAYLOAD["base_url"] + PAYLOAD["route_paths"]["SHEET_VITRINA_STATUS_HTTP_PATH"], PAYLOAD["as_of_date"])),
    _collect("web_vitrina_read", "GET", _append_as_of_date(PAYLOAD["base_url"] + {DEFAULT_SHEET_WEB_VITRINA_READ_PATH!r}, PAYLOAD["as_of_date"])),
    _collect("web_vitrina_page_composition", "GET", _append_query_params(PAYLOAD["base_url"] + {DEFAULT_SHEET_WEB_VITRINA_READ_PATH!r}, {{"as_of_date": PAYLOAD["as_of_date"], "surface": {DEFAULT_SHEET_WEB_VITRINA_PAGE_COMPOSITION_SURFACE!r}}})),
    _collect("web_vitrina_group_refresh_missing_group", "POST", PAYLOAD["base_url"] + {DEFAULT_SHEET_WEB_VITRINA_GROUP_REFRESH_PATH!r}, {{}}),
    _collect("daily_report", "GET", PAYLOAD["base_url"] + "/v1/sheet-vitrina-v1/daily-report"),
    _collect("stock_report", "GET", PAYLOAD["base_url"] + "/v1/sheet-vitrina-v1/stock-report"),
    _collect("plan_report", "GET", _append_query_params(PAYLOAD["base_url"] + "/v1/sheet-vitrina-v1/plan-report", _plan_report_params(PAYLOAD["as_of_date"]))),
    _collect("plan_report_baseline_status", "GET", PAYLOAD["base_url"] + "/v1/sheet-vitrina-v1/plan-report/baseline-status"),
    _collect("plan_report_baseline_template", "GET", PAYLOAD["base_url"] + "/v1/sheet-vitrina-v1/plan-report/baseline-template.xlsx"),
    _collect("plan", "GET", _append_as_of_date(PAYLOAD["base_url"] + PAYLOAD["route_paths"]["SHEET_VITRINA_HTTP_PATH"], PAYLOAD["as_of_date"])),
    _collect("factory_order_status", "GET", PAYLOAD["base_url"] + "/v1/sheet-vitrina-v1/supply/factory-order/status"),
    _collect("factory_order_template_stock_ff", "GET", PAYLOAD["base_url"] + "/v1/sheet-vitrina-v1/supply/factory-order/template/stock-ff.xlsx"),
    _collect("factory_order_template_inbound_factory", "GET", PAYLOAD["base_url"] + "/v1/sheet-vitrina-v1/supply/factory-order/template/inbound-factory.xlsx"),
    _collect("factory_order_template_inbound_ff_to_wb", "GET", PAYLOAD["base_url"] + "/v1/sheet-vitrina-v1/supply/factory-order/template/inbound-ff-to-wb.xlsx"),
    _collect("factory_order_recommendation", "GET", PAYLOAD["base_url"] + "/v1/sheet-vitrina-v1/supply/factory-order/recommendation.xlsx"),
    _collect("wb_regional_status", "GET", PAYLOAD["base_url"] + "/v1/sheet-vitrina-v1/supply/wb-regional/status"),
    _collect("wb_regional_district_central", "GET", PAYLOAD["base_url"] + "/v1/sheet-vitrina-v1/supply/wb-regional/district/central.xlsx"),
]
if PAYLOAD["include_feedbacks"]:
    results.append(
        _collect(
            "feedbacks",
            "GET",
            _append_query_params(
                PAYLOAD["base_url"] + {DEFAULT_SHEET_FEEDBACKS_PATH!r},
                {{
                    "date_from": PAYLOAD["feedbacks_date_from"],
                    "date_to": PAYLOAD["feedbacks_date_to"],
                    "stars": "1,2,3,4,5",
                    "is_answered": "all",
                }},
            ),
        )
    )
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


def _build_plan_report_probe_params(as_of_date: str | None) -> dict[str, str]:
    params = {
        "period": "current_month",
        "h1_buyout_plan_rub": "272000",
        "h2_buyout_plan_rub": "638000",
        "plan_drr_pct": "10",
    }
    if as_of_date:
        params["as_of_date"] = as_of_date
    return params


def _default_feedbacks_probe_window(
    *,
    date_from: str | None,
    date_to: str | None,
) -> tuple[str, str]:
    if bool(date_from) != bool(date_to):
        raise ValueError("feedbacks probe requires both --feedbacks-date-from and --feedbacks-date-to")
    if date_from and date_to:
        return date_from, date_to
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=6)
    return start.isoformat(), end.isoformat()


def _append_query_params(url: str, params: dict[str, str | None]) -> str:
    normalized = {
        str(key): str(value)
        for key, value in params.items()
        if value not in {None, ""}
    }
    if not normalized:
        return url
    query = urllib_parse.urlencode(normalized)
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


def _describe_managed_systemd_units(target: HostedRuntimeTarget) -> list[dict[str, Any]]:
    if not target.has_managed_systemd_units:
        return []
    return [
        {
            "name": unit.name,
            "enable": unit.enable,
            "restart": unit.restart,
            "source_path": _remote_systemd_unit_source_path(target, unit.name),
            "destination_path": _remote_systemd_unit_destination_path(target, unit.name),
        }
        for unit in target.managed_systemd_units
    ]


def _validate_managed_systemd_units(target: HostedRuntimeTarget) -> None:
    if not target.has_managed_systemd_units:
        return
    source_dir = _resolve_repo_relative_dir(target.systemd_units_source_dir)
    if not source_dir.exists():
        raise FileNotFoundError(f"managed systemd unit source dir not found: {source_dir}")
    for unit in target.managed_systemd_units:
        unit_path = source_dir / unit.name
        if not unit_path.exists():
            raise FileNotFoundError(f"managed systemd unit file not found: {unit_path}")


def _build_managed_systemd_commands(target: HostedRuntimeTarget) -> dict[str, list[str] | None]:
    if not target.has_managed_systemd_units:
        return {
            "install": None,
            "daemon_reload": None,
            "enable": None,
            "restart": None,
        }

    install_steps = [f"install -d {shlex.quote(target.systemd_unit_directory)}"]
    for unit in target.managed_systemd_units:
        install_steps.append(
            "install -m 0644 "
            f"{shlex.quote(_remote_systemd_unit_source_path(target, unit.name))} "
            f"{shlex.quote(_remote_systemd_unit_destination_path(target, unit.name))}"
        )

    enable_names = [shlex.quote(unit.name) for unit in target.managed_systemd_units if unit.enable]
    restart_names = [shlex.quote(unit.name) for unit in target.managed_systemd_units if unit.restart]
    return {
        "install": _remote_shell_command(target, " && ".join(install_steps)),
        "daemon_reload": _remote_shell_command(target, "systemctl daemon-reload"),
        "enable": (
            _remote_shell_command(target, f"systemctl enable {' '.join(enable_names)}")
            if enable_names
            else None
        ),
        "restart": (
            _remote_shell_command(target, f"systemctl restart {' '.join(restart_names)}")
            if restart_names
            else None
        ),
    }


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
    if target.has_managed_systemd_units:
        required["systemd_unit_directory"] = target.systemd_unit_directory
        required["systemd_units_source_dir"] = target.systemd_units_source_dir
    for key, value in required.items():
        if _is_placeholder(value):
            missing.append(key)
    if target.has_managed_systemd_units:
        for unit in target.managed_systemd_units:
            if _is_placeholder(unit.name):
                missing.append("managed_systemd_units[].name")
    if target.nginx_public_routes:
        nginx_required = {
            "nginx_public_routes.server_config_path": target.nginx_public_routes.server_config_path,
            "nginx_public_routes.backup_dir": target.nginx_public_routes.backup_dir,
            "nginx_public_routes.test_command": target.nginx_public_routes.test_command,
            "nginx_public_routes.reload_command": target.nginx_public_routes.reload_command,
            "nginx_public_routes.manifest_path": target.nginx_public_routes.manifest_path,
        }
        for key, value in nginx_required.items():
            if _is_placeholder(value):
                missing.append(key)
        if not _is_placeholder(target.nginx_public_routes.manifest_path):
            try:
                _resolve_repo_relative_path(target.nginx_public_routes.manifest_path)
            except Exception:
                missing.append("nginx_public_routes.manifest_path")
    return missing


def _ensure_active_hosted_runtime_target(target: HostedRuntimeTarget, *, action: str) -> None:
    blockers = _target_action_blockers(target)
    if blockers:
        raise ValueError(
            f"{action} refused for non-active hosted runtime target "
            f"{target.target_id!r}: {'; '.join(blockers)}"
        )


def _ensure_target_allows_mutation(target: HostedRuntimeTarget, *, action: str, dry_run: bool) -> None:
    if dry_run:
        return
    if _is_rollback_only_target(target):
        if _rollback_target_write_override_enabled():
            _warn_rollback_target_write_override(target, action=action)
            return
        raise ValueError(_rollback_only_target_mutation_error(target, action=action))
    _ensure_active_hosted_runtime_target(target, action=action)


def _describe_target_mutation_guard(target: HostedRuntimeTarget) -> dict[str, Any]:
    rollback_only = _is_rollback_only_target(target)
    blockers = _target_action_blockers(target)
    return {
        "current_live_target_file": CURRENT_LIVE_TARGET_FILE_HINT,
        "current_live_ssh_destination": ACTIVE_HOSTED_RUNTIME_SSH_DESTINATION,
        "current_live_public_hosts": sorted(ACTIVE_HOSTED_RUNTIME_PUBLIC_HOSTS),
        "rollback_only": rollback_only,
        "mutating_actions_blocked_by_default": rollback_only or bool(blockers),
        "mutating_actions_require_override": rollback_only,
        "override_env": ROLLBACK_TARGET_WRITE_OVERRIDE_ENV if rollback_only else None,
        "override_value": ROLLBACK_TARGET_WRITE_OVERRIDE_VALUE if rollback_only else None,
        "target_action_blockers": blockers,
        "read_only_actions_allowed": [
            "print-plan",
            "deploy --dry-run",
            "apply-nginx-routes --dry-run",
            "public-probe",
            "loopback-probe",
        ],
    }


def _is_rollback_only_target(target: HostedRuntimeTarget) -> bool:
    status = str(target.target_status or "").strip().lower()
    role = str(target.target_role or "").strip().lower()
    lifecycle = str(target.target_lifecycle or "").strip().lower()
    mutation_policy = str(target.mutation_policy or "").strip().lower()
    ssh_destination = str(target.ssh_destination or "").strip()
    public_host = _public_base_url_host(target.public_base_url)
    return (
        status in ROLLBACK_ONLY_STATUSES
        or role in ROLLBACK_ONLY_ROLES
        or lifecycle in ROLLBACK_ONLY_LIFECYCLES
        or "do_not_deploy" in mutation_policy
        or "rollback_only" in mutation_policy
        or ssh_destination in ARCHIVED_HOSTED_RUNTIME_SSH_DESTINATIONS
        or public_host in ARCHIVED_HOSTED_RUNTIME_PUBLIC_HOSTS
    )


def _rollback_target_write_override_enabled() -> bool:
    return os.environ.get(ROLLBACK_TARGET_WRITE_OVERRIDE_ENV, "") == ROLLBACK_TARGET_WRITE_OVERRIDE_VALUE


def _rollback_only_target_mutation_error(target: HostedRuntimeTarget, *, action: str) -> str:
    return (
        f"{action} refused for rollback-only selleros hosted runtime target {target.target_id!r}: "
        "old selleros target is rollback-only after EU migration; "
        f"use {CURRENT_LIVE_TARGET_FILE_HINT} "
        f"({ACTIVE_HOSTED_RUNTIME_SSH_DESTINATION} / {sorted(ACTIVE_HOSTED_RUNTIME_PUBLIC_HOSTS)[0]}) "
        "for current live deploy/apply-nginx/restart/update actions; "
        "mutation requires explicit emergency rollback override "
        f"{ROLLBACK_TARGET_WRITE_OVERRIDE_ENV}={ROLLBACK_TARGET_WRITE_OVERRIDE_VALUE}; "
        f"target ssh_destination={target.ssh_destination or '<missing>'}, "
        f"public_base_url={target.public_base_url or '<missing>'}, "
        f"target_status={target.target_status or '<missing>'}, "
        f"target_role={target.target_role or '<missing>'}, "
        f"target_lifecycle={target.target_lifecycle or '<missing>'}"
    )


def _warn_rollback_target_write_override(target: HostedRuntimeTarget, *, action: str) -> None:
    print(
        "WARNING: emergency rollback override enabled for rollback-only selleros hosted runtime target "
        f"{target.target_id!r}; action={action}; current live target remains "
        f"{CURRENT_LIVE_TARGET_FILE_HINT} / {ACTIVE_HOSTED_RUNTIME_SSH_DESTINATION} / "
        f"{sorted(ACTIVE_HOSTED_RUNTIME_PUBLIC_HOSTS)[0]}.",
        file=sys.stderr,
    )


def _warn_if_rollback_read_only_target(target: HostedRuntimeTarget, *, action: str) -> None:
    if not _is_rollback_only_target(target):
        return
    print(
        "WARNING: read-only action against rollback-only selleros hosted runtime target "
        f"{target.target_id!r}; action={action}; do not use this target for routine deploy/apply/restart/update. "
        f"Current live target is {CURRENT_LIVE_TARGET_FILE_HINT} / {ACTIVE_HOSTED_RUNTIME_SSH_DESTINATION}.",
        file=sys.stderr,
    )


def _target_action_blockers(target: HostedRuntimeTarget) -> list[str]:
    blockers: list[str] = []
    status = str(target.target_status or "").strip().lower()
    ssh_destination = str(target.ssh_destination or "").strip()
    public_host = _public_base_url_host(target.public_base_url)
    if status == LOCAL_TEST_TARGET_STATUS and public_host in LOCAL_TEST_PUBLIC_HOSTS and not ssh_destination:
        return blockers
    if status != ACTIVE_TARGET_STATUS:
        blockers.append(f"target_status={status or '<missing>'}")
    if ssh_destination in ARCHIVED_HOSTED_RUNTIME_SSH_DESTINATIONS:
        blockers.append(f"archived ssh_destination={ssh_destination}")
    elif ssh_destination and ssh_destination != ACTIVE_HOSTED_RUNTIME_SSH_DESTINATION:
        blockers.append(
            f"ssh_destination must be {ACTIVE_HOSTED_RUNTIME_SSH_DESTINATION}, got {ssh_destination}"
        )
    if public_host in ARCHIVED_HOSTED_RUNTIME_PUBLIC_HOSTS:
        blockers.append(f"archived public_base_url host={public_host}")
    elif public_host and public_host not in ACTIVE_HOSTED_RUNTIME_PUBLIC_HOSTS:
        blockers.append(
            f"public_base_url host must be one of {sorted(ACTIVE_HOSTED_RUNTIME_PUBLIC_HOSTS)}, got {public_host}"
        )
    if str(target.target_dir).strip() != ACTIVE_HOSTED_RUNTIME_TARGET_DIR:
        blockers.append(
            f"target_dir must be {ACTIVE_HOSTED_RUNTIME_TARGET_DIR}, got {target.target_dir}"
        )
    runtime_dir = str(target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or "").strip()
    if runtime_dir != ACTIVE_HOSTED_RUNTIME_RUNTIME_DIR:
        blockers.append(
            f"REGISTRY_UPLOAD_RUNTIME_DIR must be {ACTIVE_HOSTED_RUNTIME_RUNTIME_DIR}, got {runtime_dir}"
        )
    if str(target.service_name).strip() != ACTIVE_HOSTED_RUNTIME_SERVICE_NAME:
        blockers.append(
            f"service_name must be {ACTIVE_HOSTED_RUNTIME_SERVICE_NAME}, got {target.service_name}"
        )
    return blockers


def _public_base_url_host(public_base_url: str) -> str:
    parsed = urllib_parse.urlparse(str(public_base_url or ""))
    return str(parsed.hostname or "").strip().lower()


def _is_placeholder(value: str) -> bool:
    return not str(value).strip() or "__SET_ME__" in str(value)


def _normalize_base_url(value: str) -> str:
    if not value:
        return value
    return value.rstrip("/")


def _resolve_repo_relative_dir(raw_value: str) -> Path:
    relative_path = Path(raw_value.strip())
    if relative_path.is_absolute():
        raise ValueError("managed systemd unit source dir must be repo-relative")
    return ROOT / relative_path


def _resolve_repo_relative_path(raw_value: str) -> Path:
    relative_path = Path(raw_value.strip())
    if relative_path.is_absolute():
        raise ValueError("repo-owned path must be repo-relative")
    path = ROOT / relative_path
    if not path.exists():
        raise FileNotFoundError(f"repo-owned path not found: {path}")
    return path


def _remote_repo_relative_path(target: HostedRuntimeTarget, path: Path) -> str:
    resolved = path.resolve()
    try:
        relative_path = resolved.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise ValueError(f"target file must be inside repo for remote deploy: {path}") from exc
    return f"{target.target_dir.rstrip('/')}/{str(relative_path)}"


def _remote_systemd_unit_source_path(target: HostedRuntimeTarget, unit_name: str) -> str:
    return f"{target.remote_systemd_units_source_dir.rstrip('/')}/{unit_name}"


def _remote_systemd_unit_destination_path(target: HostedRuntimeTarget, unit_name: str) -> str:
    return f"{target.systemd_unit_directory.rstrip('/')}/{unit_name}"


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
    try:
        return int(args.handler(args))
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
