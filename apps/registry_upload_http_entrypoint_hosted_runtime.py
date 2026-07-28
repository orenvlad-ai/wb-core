"""Repo-owned deploy/probe contract for hosted registry upload runtime."""

from __future__ import annotations

import argparse
import base64
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import shlex
import ssl
import subprocess
import sys
import time
from typing import Any, Mapping
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROBE_BODY_LIMIT_BYTES = 768 * 1024
WAREHOUSE_OPENING_READ_TIMEOUT_SECONDS = 300.0
WAREHOUSE_OPENING_MUTATION_TIMEOUT_SECONDS = 1800.0
AUTOANSWERS_READONLY_TIMEOUT_SECONDS = 7200.0
AUTOANSWERS_LIFECYCLE_TIMEOUT_SECONDS = 7200.0
FINANCE_CANONICAL_READ_TIMEOUT_SECONDS = 900.0
FINANCE_CANONICAL_MUTATION_TIMEOUT_SECONDS = 1800.0
FINANCE_STORAGE_SPLIT_READ_TIMEOUT_SECONDS = 3600.0
FINANCE_STORAGE_SPLIT_MUTATION_TIMEOUT_SECONDS = 43_200.0
PARTNER_FINANCE_DIAGNOSTIC_TIMEOUT_SECONDS = 900.0
ADS_HISTORICAL_RECOVERY_TIMEOUT_SECONDS = 3600.0
VITRINA_INCIDENT_REMATERIALIZATION_TIMEOUT_SECONDS = 900.0
WAREHOUSE_RECOVERY_LIFECYCLE_TIMEOUT_SECONDS = 7200.0
WAREHOUSE_FUNCTIONAL_PLAN_ACTIONS = frozenset(
    {
        "cutover-dry-run",
        "sync-dry-run",
        "emergency-dry-run",
        "economics-backfill-dry-run",
        "supplier-certification-dry-run",
    }
)
PROBE_SYSTEM_CA_FILE_CANDIDATES = (
    "/etc/ssl/cert.pem",
    "/private/etc/ssl/cert.pem",
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/ca-bundle.pem",
    "/opt/homebrew/etc/ca-certificates/cert.pem",
)

from packages.adapters.registry_upload_http_entrypoint import (
    DEFAULT_AUTO_UPDATES_PATH,
    DEFAULT_COST_PRICE_UPLOAD_PATH,
    DEFAULT_CNY_ACCOUNT_DOCUMENTS_PATH,
    DEFAULT_FACTORY_ORDER_RECOMMENDATION_PATH,
    DEFAULT_FACTORY_ORDER_STATUS_PATH,
    DEFAULT_FACTORY_ORDER_TEMPLATE_INBOUND_FACTORY_PATH,
    DEFAULT_FACTORY_ORDER_TEMPLATE_INBOUND_FF_TO_WB_PATH,
    DEFAULT_INSTRUCTIONS_UI_PATH,
    DEFAULT_FACTORY_ORDER_TEMPLATE_STOCK_FF_PATH,
    DEFAULT_OWN_PRODUCT_CAPITAL_STATUS_PATH,
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
    DEFAULT_SHEET_SUPPLIER_UI_PATH,
    DEFAULT_SHEET_WEB_VITRINA_BUSINESS_PROJECTION_STATUS_PATH,
    DEFAULT_SHEET_WEB_VITRINA_GROUP_REFRESH_PATH,
    DEFAULT_SHEET_WEB_VITRINA_PAGE_COMPOSITION_SURFACE,
    DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    DEFAULT_SHEET_WEB_VITRINA_USER_CONFIG_PATH,
    DEFAULT_SUPPLIER_SHIPMENTS_PATH,
    DEFAULT_UPLOAD_PATH,
    DEFAULT_WB_REGIONAL_DISTRICT_DOWNLOAD_PREFIX,
    DEFAULT_WB_REGIONAL_RECOMMENDATIONS_ZIP_PATH,
    DEFAULT_WB_REGIONAL_STATUS_PATH,
    DEFAULT_WB_SUPPLIES_PATH,
    DEFAULT_WB_WAREHOUSE_EXCLUSION_OPTIONS_PATH,
    DEFAULT_WB_WAREHOUSE_EXCLUSION_SETTINGS_PATH,
    DEFAULT_WAREHOUSES_PATH,
)
from packages.application.warehouse_functional_maintenance import (
    warehouse_functional_service_is_quiescent,
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
ACTIVE_HOSTED_RUNTIME_TARGET_ID = "wb_core_eu_hosted_runtime_active"
ACTIVE_HOSTED_RUNTIME_ENVIRONMENT_FILE = "/opt/wb-ai/.env"
ACTIVE_HOSTED_RUNTIME_PUBLIC_HOSTS = {"89.191.226.88", "api.selleros.pro"}
CURRENT_LIVE_PUBLIC_BASE_URL = "https://api.selleros.pro"
CURRENT_LIVE_REQUIRED_SERVER_NAMES = ("89.191.226.88", "api.selleros.pro")
CURRENT_LIVE_REQUIRED_TLS_LISTEN = "443 ssl"
CURRENT_LIVE_REQUIRED_TLS_CERTIFICATE_PATH = "/etc/letsencrypt/live/api.selleros.pro/fullchain.pem"
CURRENT_LIVE_REQUIRED_TLS_CERTIFICATE_KEY_PATH = "/etc/letsencrypt/live/api.selleros.pro/privkey.pem"
ACTIVE_HOSTED_RUNTIME_TARGET_DIR = "/opt/wb-core-runtime/app"
ACTIVE_HOSTED_RUNTIME_RUNTIME_DIR = "/opt/wb-core-runtime/state"
ACTIVE_HOSTED_RUNTIME_SERVICE_NAME = "wb-core-registry-http.service"
DEPLOY_METADATA_FILENAME = ".wb-core-deploy.json"
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
    "WB_PRICES_API_BASE_URL",
    "WB_PRICES_WRITE_ENABLED",
    "WB_AUTOANSWERS_FORCE_OFF",
    "WB_SPP_TEST_ENABLED",
    "WB_BUYER_SESSION_VALIDATION_NM_ID",
    "OPENAI_MODEL",
    "OPENAI_API_BASE_URL",
    "OPENAI_TIMEOUT_SECONDS",
    "PROMO_XLSX_COLLECTOR_STORAGE_STATE_PATH",
    "SELLER_PORTAL_CANONICAL_SUPPLIER_ID",
    "SELLER_PORTAL_CANONICAL_SUPPLIER_LABEL",
    "SELLER_PORTAL_RELOGIN_SSH_DESTINATION",
    "SHEET_VITRINA_WEBSOURCE_CURRENT_SYNC_API_BASE_URL",
    "SHEET_VITRINA_WEB_SOURCE_SNAPSHOT_BASE_URL",
    "SHEET_VITRINA_SELLER_FUNNEL_SNAPSHOT_BASE_URL",
]
RUNTIME_PIP_PACKAGES = [
    "openpyxl==3.1.5",
    "xlrd==2.0.1",
    "playwright==1.58.0",
    "pypdf==6.4.1",
    "reportlab==4.4.5",
]
SELLER_PORTAL_RECOVERY_OS_PACKAGES = [
    "python3-pip",
    "python3-venv",
    "xvfb",
    "x11vnc",
    "novnc",
    "websockify",
    "openbox",
]
AUTOANSWERS_NODE_MAJOR = 20
AUTOANSWERS_NODE_VERSION = "22.21.1"
AUTOANSWERS_NODE_DIST_BASE = f"https://nodejs.org/dist/v{AUTOANSWERS_NODE_VERSION}"
AUTOANSWERS_NODE_SHA256 = {
    "amd64": "680d3f30b24a7ff24b98db5e96f294c0070f8f9078df658da1bce1b9c9873c88",
    "arm64": "e660365729b434af422bcd2e8e14228637ecf24a1de2cd7c916ad48f2a0521e1",
}
AUTOANSWERS_BASE_OS_PACKAGES = ["ca-certificates", "curl", "xz-utils", "zstd", "ffmpeg"]
SELLER_PORTAL_RECOVERY_REQUIRED_COMMANDS = [
    "python3",
    "xvfb-run",
    "Xvfb",
    "x11vnc",
    "websockify",
    "openbox",
]
SELLER_PORTAL_RECOVERY_NOVNC_WEB_DIR = "/usr/share/novnc"
SELLER_PORTAL_RECOVERY_WB_WEB_BOT_DIR = "/opt/wb-web-bot"
SELLER_PORTAL_RECOVERY_VENV_DIR = "/opt/wb-web-bot/venv"
SELLER_PORTAL_RECOVERY_VENV_PYTHON = "/opt/wb-web-bot/venv/bin/python"
SELLER_PORTAL_RECOVERY_VENV_PIP_PACKAGES = [
    "playwright==1.58.0",
    "psycopg2-binary==2.9.11",
]
SELLER_PORTAL_OWNER_RUNTIME_OS_PACKAGES = [
    "postgresql",
    "postgresql-client",
]
SELLER_PORTAL_OWNER_WB_AI_DIR = "/opt/wb-ai"
SELLER_PORTAL_OWNER_WB_AI_VENV_DIR = "/opt/wb-ai/venv"
SELLER_PORTAL_OWNER_WB_AI_VENV_PYTHON = "/opt/wb-ai/venv/bin/python"
SELLER_PORTAL_OWNER_WB_AI_VENV_PIP_PACKAGES = [
    "fastapi==0.129.1",
    "uvicorn==0.41.0",
    "psycopg2-binary==2.9.11",
    "requests==2.32.5",
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
    "scratch/",
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
        "install required host OS packages for seller-portal recovery and browser launch",
        "install required host OS packages for seller-portal owner capture runtime",
        "install required Python runtime packages on the hosted system python",
        "create or repair /opt/wb-web-bot/venv for seller-session probes",
        "create or repair /opt/wb-ai/venv for seller-portal owner handoff API",
        "verify owner runtime code/import contract for /opt/wb-web-bot and /opt/wb-ai",
        "ensure Playwright Chromium can launch from both hosted runtime python contexts",
        "install locked frozen Node boundary dependencies and verify Node.js >=20 plus ffmpeg",
        "prepare additive autoanswers schema with a verified backup while effective OFF",
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
    include_auto_updates_status: bool = True,
    include_wb_warehouse_exclusion_options: bool = True,
    include_feedbacks: bool = False,
    feedbacks_date_from: str | None = None,
    feedbacks_date_to: str | None = None,
    timeout_seconds: float,
    auth_cookie: str | None = None,
) -> list[dict[str, Any]]:
    results = [
        _collect_http_probe(
            name="operator",
            method="GET",
            url=f"{base_url}{route_paths['SHEET_VITRINA_OPERATOR_UI_PATH']}",
            timeout_seconds=timeout_seconds,
            auth_cookie=auth_cookie,
        ),
        _collect_http_probe(
            name="operator_reports",
            method="GET",
            url=f"{base_url}{route_paths['SHEET_VITRINA_OPERATOR_UI_PATH']}?embedded_tab=reports",
            timeout_seconds=timeout_seconds,
            auth_cookie=auth_cookie,
        ),
        _collect_http_probe(
            name="operator_factory_order",
            method="GET",
            url=f"{base_url}{route_paths['SHEET_VITRINA_OPERATOR_UI_PATH']}?embedded_tab=factory-order",
            timeout_seconds=timeout_seconds,
            auth_cookie=auth_cookie,
        ),
        _collect_http_probe(
            name="web_vitrina_page",
            method="GET",
            url=f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_UI_PATH}",
            timeout_seconds=timeout_seconds,
            auth_cookie=auth_cookie,
        ),
        _collect_http_probe(
            name="warehouses_overview",
            method="GET",
            url=f"{base_url}{DEFAULT_WAREHOUSES_PATH}",
            timeout_seconds=timeout_seconds,
            auth_cookie=auth_cookie,
        ),
        _collect_http_probe(
            name="warehouse_ff",
            method="GET",
            url=f"{base_url}{DEFAULT_WAREHOUSES_PATH}/ff",
            timeout_seconds=timeout_seconds,
            auth_cookie=auth_cookie,
        ),
        _collect_http_probe(
            name="instructions_page",
            method="GET",
            url=f"{base_url}{DEFAULT_INSTRUCTIONS_UI_PATH}",
            timeout_seconds=timeout_seconds,
            auth_cookie=auth_cookie,
        ),
        _collect_http_probe(
            name="supplier_page",
            method="GET",
            url=f"{base_url}{DEFAULT_SHEET_SUPPLIER_UI_PATH}",
            timeout_seconds=timeout_seconds,
            auth_cookie=auth_cookie,
        ),
        _collect_http_probe(
            name="supplier_shipments_list",
            method="GET",
            url=f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}",
            timeout_seconds=timeout_seconds,
            auth_cookie=auth_cookie,
        ),
        _collect_http_probe(
            name="load_route",
            method="GET",
            url=f"{base_url}{DEFAULT_SHEET_LOAD_PATH}",
            timeout_seconds=timeout_seconds,
            auth_cookie=auth_cookie,
        ),
        _collect_http_probe(
            name="job",
            method="GET",
            url=f"{base_url}{DEFAULT_SHEET_JOB_PATH}?job_id=hosted_runtime_probe",
            timeout_seconds=timeout_seconds,
            auth_cookie=auth_cookie,
        ),
        _collect_http_probe(
            name="status",
            method="GET",
            url=_append_as_of_date(
                f"{base_url}{route_paths['SHEET_VITRINA_STATUS_HTTP_PATH']}",
                as_of_date,
            ),
            timeout_seconds=timeout_seconds,
            auth_cookie=auth_cookie,
        ),
        _collect_http_probe(
            name="own_product_capital_status",
            method="GET",
            url=f"{base_url}{DEFAULT_OWN_PRODUCT_CAPITAL_STATUS_PATH}",
            timeout_seconds=timeout_seconds,
            auth_cookie=auth_cookie,
        ),
        _collect_http_probe(
            name="seller_session_check",
            method="GET",
            url=f"{base_url}{DEFAULT_SELLER_PORTAL_SESSION_CHECK_PATH}",
            timeout_seconds=timeout_seconds,
            auth_cookie=auth_cookie,
        ),
        _collect_http_probe(
            name="web_vitrina_read",
            method="GET",
            url=_append_as_of_date(
                f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_READ_PATH}",
                as_of_date,
            ),
            timeout_seconds=timeout_seconds,
            auth_cookie=auth_cookie,
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
            auth_cookie=auth_cookie,
        ),
        _collect_http_probe(
            name="web_vitrina_user_config",
            method="GET",
            url=f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_USER_CONFIG_PATH}",
            timeout_seconds=timeout_seconds,
            auth_cookie=auth_cookie,
        ),
        _collect_http_probe(
            name="web_vitrina_business_projection_status",
            method="GET",
            url=(
                f"{base_url}"
                f"{DEFAULT_SHEET_WEB_VITRINA_BUSINESS_PROJECTION_STATUS_PATH}"
            ),
            timeout_seconds=timeout_seconds,
            auth_cookie=auth_cookie,
        ),
        _collect_http_probe(
            name="daily_report",
            method="GET",
            url=f"{base_url}{DEFAULT_SHEET_DAILY_REPORT_PATH}",
            timeout_seconds=timeout_seconds,
            auth_cookie=auth_cookie,
        ),
        _collect_http_probe(
            name="stock_report",
            method="GET",
            url=f"{base_url}{DEFAULT_SHEET_STOCK_REPORT_PATH}",
            timeout_seconds=timeout_seconds,
            auth_cookie=auth_cookie,
        ),
        _collect_http_probe(
            name="plan_report",
            method="GET",
            url=_append_query_params(
                f"{base_url}{DEFAULT_SHEET_PLAN_REPORT_PATH}",
                _build_plan_report_probe_params(as_of_date),
            ),
            timeout_seconds=timeout_seconds,
            auth_cookie=auth_cookie,
        ),
        _collect_http_probe(
            name="plan_report_baseline_status",
            method="GET",
            url=f"{base_url}{DEFAULT_SHEET_PLAN_REPORT_BASELINE_STATUS_PATH}",
            timeout_seconds=timeout_seconds,
            auth_cookie=auth_cookie,
        ),
        _collect_http_probe(
            name="plan_report_baseline_template",
            method="GET",
            url=f"{base_url}{DEFAULT_SHEET_PLAN_REPORT_BASELINE_TEMPLATE_PATH}",
            timeout_seconds=timeout_seconds,
            auth_cookie=auth_cookie,
        ),
        _collect_http_probe(
            name="plan",
            method="GET",
            url=_append_as_of_date(
                f"{base_url}{route_paths['SHEET_VITRINA_HTTP_PATH']}",
                as_of_date,
            ),
            timeout_seconds=timeout_seconds,
            auth_cookie=auth_cookie,
        ),
        _collect_http_probe(
            name="factory_order_status",
            method="GET",
            url=f"{base_url}{DEFAULT_FACTORY_ORDER_STATUS_PATH}",
            timeout_seconds=timeout_seconds,
            auth_cookie=auth_cookie,
        ),
        _collect_http_probe(
            name="factory_order_template_stock_ff",
            method="GET",
            url=f"{base_url}{DEFAULT_FACTORY_ORDER_TEMPLATE_STOCK_FF_PATH}",
            timeout_seconds=timeout_seconds,
            auth_cookie=auth_cookie,
        ),
        _collect_http_probe(
            name="factory_order_template_inbound_factory",
            method="GET",
            url=f"{base_url}{DEFAULT_FACTORY_ORDER_TEMPLATE_INBOUND_FACTORY_PATH}",
            timeout_seconds=timeout_seconds,
            auth_cookie=auth_cookie,
        ),
        _collect_http_probe(
            name="factory_order_template_inbound_ff_to_wb",
            method="GET",
            url=f"{base_url}{DEFAULT_FACTORY_ORDER_TEMPLATE_INBOUND_FF_TO_WB_PATH}",
            timeout_seconds=timeout_seconds,
            auth_cookie=auth_cookie,
        ),
        _collect_http_probe(
            name="factory_order_recommendation",
            method="GET",
            url=f"{base_url}{DEFAULT_FACTORY_ORDER_RECOMMENDATION_PATH}",
            timeout_seconds=timeout_seconds,
            auth_cookie=auth_cookie,
        ),
        _collect_http_probe(
            name="wb_regional_status",
            method="GET",
            url=f"{base_url}{DEFAULT_WB_REGIONAL_STATUS_PATH}",
            timeout_seconds=timeout_seconds,
            auth_cookie=auth_cookie,
        ),
        _collect_http_probe(
            name="wb_supplies_list",
            method="GET",
            url=f"{base_url}{DEFAULT_WB_SUPPLIES_PATH}",
            timeout_seconds=timeout_seconds,
            auth_cookie=auth_cookie,
        ),
        _collect_http_probe(
            name="wb_regional_district_central",
            method="GET",
            url=f"{base_url}{DEFAULT_WB_REGIONAL_DISTRICT_DOWNLOAD_PREFIX}/central.xlsx",
            timeout_seconds=timeout_seconds,
            auth_cookie=auth_cookie,
        ),
        _collect_http_probe(
            name="wb_regional_recommendations_zip",
            method="GET",
            url=f"{base_url}{DEFAULT_WB_REGIONAL_RECOMMENDATIONS_ZIP_PATH}",
            timeout_seconds=timeout_seconds,
            auth_cookie=auth_cookie,
        ),
    ]
    if include_wb_warehouse_exclusion_options:
        results.append(
            _collect_http_probe(
                name="wb_warehouse_exclusion_options",
                method="GET",
                url=f"{base_url}{DEFAULT_WB_WAREHOUSE_EXCLUSION_OPTIONS_PATH}",
                timeout_seconds=timeout_seconds,
                auth_cookie=auth_cookie,
            )
        )
        results.append(
            _collect_http_probe(
                name="wb_warehouse_exclusion_settings",
                method="GET",
                url=f"{base_url}{DEFAULT_WB_WAREHOUSE_EXCLUSION_SETTINGS_PATH}",
                timeout_seconds=timeout_seconds,
                auth_cookie=auth_cookie,
            )
        )
    if include_auto_updates_status:
        results.append(
            _collect_http_probe(
                name="auto_updates_status",
                method="GET",
                url=f"{base_url}{DEFAULT_AUTO_UPDATES_PATH}",
                timeout_seconds=timeout_seconds,
                auth_cookie=auth_cookie,
            )
        )
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
                auth_cookie=auth_cookie,
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
                auth_cookie=auth_cookie,
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
    auth_cookie: str | None = None,
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
            auth_cookie=auth_cookie,
        )
        transport = "ssh"
    else:
        raw_results = collect_public_surface(
            base_url=target.loopback_base_url,
            route_paths=target.route_paths,
            as_of_date=as_of_date,
            include_refresh=include_refresh,
            include_auto_updates_status=_probe_include_auto_updates_status(target),
            include_wb_warehouse_exclusion_options=_probe_include_wb_warehouse_exclusion_options(target),
            include_feedbacks=include_feedbacks,
            feedbacks_date_from=feedbacks_date_from,
            feedbacks_date_to=feedbacks_date_to,
            timeout_seconds=timeout_seconds,
            auth_cookie=auth_cookie,
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
    deploy_metadata_command = _build_deploy_metadata_command(
        target,
        deployment_complete=False,
    )
    deploy_completion_metadata_command = _build_deploy_metadata_command(
        target,
        deployment_complete=True,
    )
    restart_command = _remote_shell_command(
        target,
        f"cd {shlex.quote(target.target_dir)} && {target.restart_command}",
    )
    seller_recovery_os_dependencies_command = _build_seller_portal_recovery_os_dependencies_command(target)
    seller_owner_os_dependencies_command = _build_seller_portal_owner_runtime_os_dependencies_command(target)
    runtime_pip_install_command = _build_runtime_pip_install_command(target)
    seller_recovery_venv_command = _build_seller_portal_recovery_venv_command(target)
    seller_owner_venv_command = _build_seller_portal_owner_runtime_venv_command(target)
    seller_owner_contract_command = _build_seller_portal_owner_runtime_contract_command(target)
    seller_recovery_playwright_browser_command = _build_seller_portal_recovery_playwright_browser_command(target)
    autoanswers_os_dependencies_command = _build_autoanswers_os_dependencies_command(target)
    autoanswers_node_dependencies_command = _build_autoanswers_node_dependencies_command(target)
    autoanswers_prepare_capacity_command = _build_autoanswers_prepare_capacity_command(target)
    autoanswers_prepare_deploy_command = _build_autoanswers_prepare_deploy_command(target)
    systemd_commands = _build_managed_systemd_commands(target)
    auth_env_preflight_command = _build_auth_env_preflight_command(target)
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
            "deploy_metadata": deploy_metadata_command,
            "deploy_completion_metadata": deploy_completion_metadata_command,
            "seller_portal_recovery_os_dependencies": seller_recovery_os_dependencies_command,
            "seller_portal_owner_runtime_os_dependencies": seller_owner_os_dependencies_command,
            "runtime_pip_install": runtime_pip_install_command,
            "seller_portal_recovery_venv": seller_recovery_venv_command,
            "seller_portal_owner_runtime_venv": seller_owner_venv_command,
            "seller_portal_owner_runtime_contract": seller_owner_contract_command,
            "seller_portal_recovery_playwright_browser": seller_recovery_playwright_browser_command,
            "autoanswers_os_dependencies": autoanswers_os_dependencies_command,
            "autoanswers_node_dependencies": autoanswers_node_dependencies_command,
            "autoanswers_prepare_capacity": autoanswers_prepare_capacity_command,
            "autoanswers_prepare_deploy": autoanswers_prepare_deploy_command,
            "systemd_install": systemd_commands["install"],
            "systemd_daemon_reload": systemd_commands["daemon_reload"],
            "restart": restart_command,
            "systemd_enable": systemd_commands["enable"],
            "systemd_restart": systemd_commands["restart"],
            "nginx_public_routes_update": nginx_public_routes_command,
            "status": status_command,
            "auth_env_preflight": auth_env_preflight_command,
        },
    }
    if dry_run:
        return summary

    def run_stage(
        stage: str,
        command: list[str],
        *,
        allow_transport_reconciliation: bool = True,
    ) -> None:
        try:
            _run_command(command)
        except subprocess.CalledProcessError as exc:
            if exc.returncode != 255:
                raise
            if not allow_transport_reconciliation:
                raise RuntimeError(
                    f"transport-indeterminate during {stage}; mutation preflight must be rerun idempotently"
                ) from exc
            release_pr = os.environ.get("WB_CORE_RELEASE_PR", "").strip()
            release_head = os.environ.get("WB_CORE_RELEASE_HEAD", "").strip()
            release_merge = _git_output(["git", "rev-parse", "HEAD"]).strip().lower()
            if not release_pr.isdigit() or not release_head:
                raise RuntimeError(
                    f"transport-indeterminate during {stage}; release identity is unavailable"
                ) from exc
            from apps.hosted_runtime_transport_reconcile import reconcile

            reconciliation = reconcile(
                target_file=target_file or resolve_target_file(),
                expected_sha=release_merge,
                pr=int(release_pr),
                head=release_head,
                merge=release_merge,
                failed_stage=stage
                if stage in {"daemon-reload", "restart", "probes", "readback"}
                else "sync",
                require_deployment_complete=False,
            )
            summary["transport_reconciliation"] = reconciliation
            if not bool(reconciliation.get("healthy")):
                raise RuntimeError(
                    f"transport-indeterminate during {stage}; exact-SHA reconciliation halted"
                ) from exc

    # Never let a deploy/restart proceed against a missing hosted auth contour.
    run_stage("auth-preflight", auth_env_preflight_command)
    run_stage("mkdir", mkdir_command)
    run_stage("sync", rsync_plan)
    run_stage("chown", chown_target_dir_command)
    run_stage("metadata", deploy_metadata_command)
    run_stage("dependencies", seller_recovery_os_dependencies_command)
    run_stage("dependencies", seller_owner_os_dependencies_command)
    run_stage("dependencies", runtime_pip_install_command)
    run_stage("dependencies", seller_recovery_venv_command)
    run_stage("dependencies", seller_owner_venv_command)
    run_stage("dependencies", seller_owner_contract_command)
    run_stage("dependencies", seller_recovery_playwright_browser_command)
    run_stage("dependencies", autoanswers_os_dependencies_command)
    run_stage("dependencies", autoanswers_node_dependencies_command)
    run_stage(
        "autoanswers-schema-preflight",
        autoanswers_prepare_deploy_command,
        allow_transport_reconciliation=False,
    )
    if systemd_commands["install"]:
        run_stage("systemd-install", systemd_commands["install"])
        run_stage("daemon-reload", systemd_commands["daemon_reload"])
    if nginx_public_routes_command:
        run_stage("nginx", nginx_public_routes_command)
    run_stage("restart", restart_command)
    if systemd_commands["enable"]:
        run_stage("restart", systemd_commands["enable"])
    if systemd_commands["restart"]:
        run_stage("restart", systemd_commands["restart"])
    if status_command:
        run_stage("readback", status_command)
    # Read back the same contract after all managed-unit operations.
    run_stage("readback", auth_env_preflight_command)
    # The exact SHA markers are written before dependency/schema work so an
    # interrupted rollout is observable, but only this final atomic metadata
    # update proves that every required deploy stage completed.  A disconnect
    # during this write remains fail-closed; the halted reconciler may accept
    # it later only when the completed marker is actually readable.
    run_stage(
        "metadata-complete",
        deploy_completion_metadata_command,
        allow_transport_reconciliation=False,
    )
    return summary


def _build_auth_env_preflight_command(target: HostedRuntimeTarget) -> list[str]:
    """Fail closed on missing required WebCore auth env keys without exposing values."""

    if not target.environment_file:
        raise ValueError("deploy target is missing environment_file for auth preflight")
    env_file = shlex.quote(target.environment_file)
    script = (
        "set -eu; f=" + env_file + "; test -r \"$f\" || { echo 'missing environment file'; exit 78; }; "
        "for k in WB_CORE_WEB_AUTH_USERNAME WB_CORE_WEB_AUTH_PASSWORD_HASH WB_CORE_WEB_AUTH_SESSION_SECRET; do "
        "grep -Eq \"^${k}=[^[:space:]]+\" \"$f\" || { echo \"missing required auth variable: ${k}\"; exit 78; }; "
        "done"
    )
    return _remote_shell_command(target, script)


def _build_autoanswers_os_dependencies_command(target: HostedRuntimeTarget) -> list[str]:
    """Install checksum-pinned official Node binaries plus ffmpeg idempotently."""

    major_check = (
        "command -v node >/dev/null 2>&1 && "
        f"node -e \"if(Number(process.versions.node.split('.')[0])<{AUTOANSWERS_NODE_MAJOR}) process.exit(1)\""
    )
    complete_check = (
        f"{major_check} && command -v npm >/dev/null 2>&1 "
        "&& command -v ffmpeg >/dev/null 2>&1 && command -v zstd >/dev/null 2>&1"
    )
    package_names = " ".join(shlex.quote(item) for item in AUTOANSWERS_BASE_OS_PACKAGES)
    x64_sha = shlex.quote(AUTOANSWERS_NODE_SHA256["amd64"])
    arm64_sha = shlex.quote(AUTOANSWERS_NODE_SHA256["arm64"])
    version = shlex.quote(AUTOANSWERS_NODE_VERSION)
    dist_base = shlex.quote(AUTOANSWERS_NODE_DIST_BASE)
    install = (
        f"apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y {package_names}; "
        f"if ! ({major_check}); then "
        "machine_arch=$(dpkg --print-architecture); "
        f"case \"$machine_arch\" in amd64) node_arch=x64; node_sha={x64_sha} ;; "
        f"arm64) node_arch=arm64; node_sha={arm64_sha} ;; *) exit 78 ;; esac; "
        f"node_version={version}; node_dist_base={dist_base}; "
        "node_archive=/tmp/wb-core-node-${node_version}-linux-${node_arch}.tar.xz; "
        "curl --fail --silent --show-error --location "
        "\"${node_dist_base}/node-v${node_version}-linux-${node_arch}.tar.xz\" "
        "--output \"$node_archive\"; "
        "printf '%s  %s\\n' \"$node_sha\" \"$node_archive\" | sha256sum --check --status; "
        "install -d -m 0755 /opt/wb-core-runtime/node-runtimes; "
        "node_target=/opt/wb-core-runtime/node-runtimes/node-v${node_version}-linux-${node_arch}; "
        "if [ ! -x \"$node_target/bin/node\" ]; then "
        "node_temp=$(mktemp -d /opt/wb-core-runtime/node-runtimes/.install.XXXXXX); "
        "tar -xJf \"$node_archive\" -C \"$node_temp\"; "
        "mv \"$node_temp/node-v${node_version}-linux-${node_arch}\" \"$node_target\"; "
        "rmdir \"$node_temp\"; "
        "fi; "
        "rm -f \"$node_archive\"; "
        "ln -sfn \"$node_target/bin/node\" /usr/local/bin/node; "
        "ln -sfn \"$node_target/bin/npm\" /usr/local/bin/npm; "
        "ln -sfn \"$node_target/bin/npx\" /usr/local/bin/npx; "
        "ln -sfn \"$node_target/bin/corepack\" /usr/local/bin/corepack; "
        "fi; "
        f"{complete_check}"
    )
    return _remote_shell_command(target, f"({complete_check}) || ({install})")


def _build_autoanswers_node_dependencies_command(target: HostedRuntimeTarget) -> list[str]:
    package_dir = (
        f"{target.target_dir.rstrip('/')}/packages/node/"
        "wb_autoanswers_v1_4_2/make_mvp"
    )
    script = (
        "set -eu; command -v node >/dev/null; command -v npm >/dev/null; command -v ffmpeg >/dev/null; "
        f"node -e \"if(Number(process.versions.node.split('.')[0])<{AUTOANSWERS_NODE_MAJOR}) process.exit(78)\"; "
        f"cd {shlex.quote(package_dir)}; npm ci --omit=dev --ignore-scripts --no-audit --no-fund"
    )
    return _remote_shell_command(target, script)


def _build_autoanswers_prepare_deploy_command(target: HostedRuntimeTarget) -> list[str]:
    runtime_dir = str(target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or "").strip()
    command = (
        f"cd {shlex.quote(target.target_dir)} && "
        "/usr/bin/env WB_AUTOANSWERS_FORCE_OFF=true "
        "WB_AUTOANSWERS_DEPLOY_SERVICE_QUIESCE=true "
        "python3 apps/wb_autoanswers_activation.py prepare-deploy "
        f"--runtime-dir {shlex.quote(runtime_dir)}"
    )
    return _remote_shell_command(target, command)


def _build_autoanswers_prepare_capacity_command(target: HostedRuntimeTarget) -> list[str]:
    runtime_dir = str(target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or "").strip()
    command = (
        f"cd {shlex.quote(target.target_dir)} && "
        "/usr/bin/env WB_AUTOANSWERS_FORCE_OFF=true "
        "python3 apps/wb_autoanswers_activation.py prepare-capacity "
        f"--runtime-dir {shlex.quote(runtime_dir)}"
    )
    return _remote_shell_command(target, command)


def _build_deploy_metadata_command(
    target: HostedRuntimeTarget,
    *,
    deployment_complete: bool,
) -> list[str]:
    commit = _git_output(["git", "rev-parse", "HEAD"]).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("deploy requires a valid current checkout commit")
    payload = json.dumps(
        {
            "schema_version": "wb_core_deploy_metadata_v2",
            "commit": commit,
            "deployed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "deployment_complete": bool(deployment_complete),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    target_path = f"{target.target_dir.rstrip('/')}/{DEPLOY_METADATA_FILENAME}"
    temp_path = f"{target_path}.tmp"
    runtime_sha_path = f"{target.target_dir.rstrip('/')}/.wb-core-runtime-sha"
    runtime_sha_temp = f"{runtime_sha_path}.tmp"
    shell = (
        "umask 022 && "
        f"printf '%s\\n' {shlex.quote(payload)} > {shlex.quote(temp_path)} && "
        f"printf '%s\\n' {shlex.quote(commit)} > {shlex.quote(runtime_sha_temp)} && "
        f"mv {shlex.quote(temp_path)} {shlex.quote(target_path)} && "
        f"mv {shlex.quote(runtime_sha_temp)} {shlex.quote(runtime_sha_path)}"
    )
    return _remote_shell_command(target, shell)


def _build_runtime_pip_install_command(target: HostedRuntimeTarget) -> list[str]:
    package_names = " ".join(shlex.quote(item) for item in RUNTIME_PIP_PACKAGES)
    python_check = "python3 -c 'import openpyxl, xlrd, playwright, pypdf, reportlab' >/dev/null 2>&1"
    pip_install = f"python3 -m pip install --break-system-packages {package_names}"
    command = f"{python_check} || {pip_install}"
    return _remote_shell_command(target, command)


def _build_seller_portal_recovery_os_dependencies_command(target: HostedRuntimeTarget) -> list[str]:
    command_checks = " && ".join(
        f"command -v {shlex.quote(command)} >/dev/null 2>&1"
        for command in SELLER_PORTAL_RECOVERY_REQUIRED_COMMANDS
    )
    venv_check = "python3 -m venv --help >/dev/null 2>&1"
    novnc_check = f"test -d {shlex.quote(SELLER_PORTAL_RECOVERY_NOVNC_WEB_DIR)}"
    package_names = " ".join(shlex.quote(item) for item in SELLER_PORTAL_RECOVERY_OS_PACKAGES)
    install = f"apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y {package_names}"
    command = f"({command_checks} && {venv_check} && {novnc_check}) || ({install})"
    return _remote_shell_command(target, command)


def _build_seller_portal_owner_runtime_os_dependencies_command(target: HostedRuntimeTarget) -> list[str]:
    package_names = " ".join(shlex.quote(item) for item in SELLER_PORTAL_OWNER_RUNTIME_OS_PACKAGES)
    checks = "command -v psql >/dev/null 2>&1 && systemctl is-active --quiet postgresql"
    install = (
        f"apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y {package_names} "
        "&& systemctl enable --now postgresql"
    )
    command = f"({checks}) || ({install})"
    return _remote_shell_command(target, command)


def _build_seller_portal_recovery_venv_command(target: HostedRuntimeTarget) -> list[str]:
    package_names = " ".join(shlex.quote(item) for item in SELLER_PORTAL_RECOVERY_VENV_PIP_PACKAGES)
    version_check_script = (
        "import importlib.metadata as m; "
        "checks = {'playwright': '1.58.0', 'psycopg2-binary': '2.9.11'}; "
        "raise SystemExit(0 if all(m.version(name) == version for name, version in checks.items()) else 1)"
    )
    python = shlex.quote(SELLER_PORTAL_RECOVERY_VENV_PYTHON)
    bot_dir = shlex.quote(SELLER_PORTAL_RECOVERY_WB_WEB_BOT_DIR)
    venv_dir = shlex.quote(SELLER_PORTAL_RECOVERY_VENV_DIR)
    version_check = f"{python} -c {shlex.quote(version_check_script)} >/dev/null 2>&1"
    pip_install = f"{python} -m pip install --upgrade {package_names}"
    command = f"install -d {bot_dir} && python3 -m venv {venv_dir} && ({version_check} || {pip_install})"
    return _remote_shell_command(target, command)


def _build_seller_portal_owner_runtime_venv_command(target: HostedRuntimeTarget) -> list[str]:
    package_names = " ".join(shlex.quote(item) for item in SELLER_PORTAL_OWNER_WB_AI_VENV_PIP_PACKAGES)
    version_check_script = (
        "import importlib.metadata as m; "
        "import fastapi, psycopg2, requests, uvicorn; "
        "checks = {"
        "'fastapi': '0.129.1', "
        "'uvicorn': '0.41.0', "
        "'psycopg2-binary': '2.9.11', "
        "'requests': '2.32.5'"
        "}; "
        "raise SystemExit(0 if all(m.version(name) == version for name, version in checks.items()) else 1)"
    )
    python = shlex.quote(SELLER_PORTAL_OWNER_WB_AI_VENV_PYTHON)
    ai_dir = shlex.quote(SELLER_PORTAL_OWNER_WB_AI_DIR)
    venv_dir = shlex.quote(SELLER_PORTAL_OWNER_WB_AI_VENV_DIR)
    version_check = f"{python} -c {shlex.quote(version_check_script)} >/dev/null 2>&1"
    pip_bootstrap = f"{python} -m pip install --upgrade pip setuptools wheel"
    pip_install = f"{python} -m pip install --upgrade {package_names}"
    repair = f"python3 -m venv --clear {venv_dir} && {pip_bootstrap} && {pip_install}"
    command = f"install -d {ai_dir} && ({version_check} || ({repair}))"
    return _remote_shell_command(target, command)


def _build_seller_portal_owner_runtime_contract_command(target: HostedRuntimeTarget) -> list[str]:
    bot_dir = shlex.quote(SELLER_PORTAL_RECOVERY_WB_WEB_BOT_DIR)
    ai_dir = shlex.quote(SELLER_PORTAL_OWNER_WB_AI_DIR)
    bot_python = shlex.quote(SELLER_PORTAL_RECOVERY_VENV_PYTHON)
    ai_python = shlex.quote(SELLER_PORTAL_OWNER_WB_AI_VENV_PYTHON)
    checks = [
        f"test -f {bot_dir}/bot/runner_day.py",
        f"test -f {bot_dir}/bot/runner_sales_funnel_day.py",
        f"test -f {ai_dir}/run_web_source_handoff.py",
        f"test -f {ai_dir}/api.py",
        (
            f"cd {bot_dir} && {bot_python} -c "
            + shlex.quote(
                "import bot.runner_day, bot.runner_sales_funnel_day, bot.db, bot.db_sales_funnel; "
                "import psycopg2, playwright"
            )
            + " >/dev/null 2>&1"
        ),
        (
            f"cd {ai_dir} && {ai_python} -c "
            + shlex.quote(
                "import api, run_web_source_handoff, sync_web_source_handoff, web_source_handoff; "
                "import fastapi, psycopg2, requests, uvicorn"
            )
            + " >/dev/null 2>&1"
        ),
    ]
    return _remote_shell_command(target, " && ".join(checks))


def _build_seller_portal_recovery_playwright_browser_command(target: HostedRuntimeTarget) -> list[str]:
    launch_check_script = (
        "from playwright.sync_api import sync_playwright; "
        "p=sync_playwright().start(); "
        "b=p.chromium.launch(headless=True); "
        "b.close(); "
        "p.stop()"
    )
    system_check = f"python3 -c {shlex.quote(launch_check_script)} >/dev/null 2>&1"
    venv_python = shlex.quote(SELLER_PORTAL_RECOVERY_VENV_PYTHON)
    venv_check = f"{venv_python} -c {shlex.quote(launch_check_script)} >/dev/null 2>&1"
    install = f"python3 -m playwright install --with-deps chromium && {venv_python} -m playwright install chromium"
    command = f"({system_check} && {venv_check}) || ({install})"
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
    client_max_body_size = _nginx_scalar(str(manifest.get("client_max_body_size") or "32m"))
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
        route_proxy_pass_url = str(route.get("proxy_pass_url") or proxy_pass_url)
        lines.extend(
            [
                f"    # {route['name']} ({methods})",
                f"    location {modifier} {route['path']} {{",
                f"        proxy_pass {route_proxy_pass_url};",
                "        proxy_set_header Host $host;",
                "        proxy_set_header X-Real-IP $remote_addr;",
                "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
                "        proxy_set_header X-Forwarded-Proto $scheme;",
                f"        client_max_body_size {client_max_body_size};",
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
        "client_max_body_size": str(manifest.get("client_max_body_size") or "32m"),
        "route_count": len(routes),
        "routes": [
            {
                "name": route["name"],
                "match": route["match"],
                "path": route["path"],
                "methods": route.get("methods") or [],
                **({"proxy_pass_url": route["proxy_pass_url"]} if route.get("proxy_pass_url") else {}),
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
        route_proxy_pass_url = str(raw_route.get("proxy_pass_url") or "").strip()
        if route_proxy_pass_url:
            _safe_proxy_pass_url(route_proxy_pass_url)
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
                **({"proxy_pass_url": route_proxy_pass_url} if route_proxy_pass_url else {}),
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


def _safe_proxy_pass_url(value: str) -> str:
    normalized = value.strip()
    if not re.fullmatch(r"https?://[A-Za-z0-9_.:-]+(?:/[A-Za-z0-9_./:-]*)?", normalized):
        raise ValueError(f"invalid proxy_pass_url {value!r}")
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
    include_refresh = _probe_include_refresh(args)
    auth_cookie = _build_probe_auth_cookie(target, timeout_seconds=args.timeout_seconds)
    raw_results = collect_public_surface(
        base_url=target.public_base_url,
        route_paths=target.route_paths,
        as_of_date=args.as_of_date,
        include_refresh=include_refresh,
        include_auto_updates_status=_probe_include_auto_updates_status(target),
        include_wb_warehouse_exclusion_options=_probe_include_wb_warehouse_exclusion_options(target),
        include_feedbacks=args.include_feedbacks,
        feedbacks_date_from=args.feedbacks_date_from,
        feedbacks_date_to=args.feedbacks_date_to,
        timeout_seconds=args.timeout_seconds,
        auth_cookie=auth_cookie,
    )
    payload = {
        "target_id": target.target_id,
        "base_url": target.public_base_url,
        "as_of_date": args.as_of_date,
        "include_refresh": include_refresh,
        "include_feedbacks": args.include_feedbacks,
        "auth": _probe_auth_summary(auth_cookie),
        **evaluate_surface_results(raw_results, route_paths=target.route_paths),
    }
    _print_json(payload)
    return 0 if payload["ok"] else 1


def run_loopback_probe_command(args: argparse.Namespace) -> int:
    target = load_hosted_runtime_target(args.target_file)
    _warn_if_rollback_read_only_target(target, action="loopback-probe")
    include_refresh = _probe_include_refresh(args)
    auth_cookie = _build_probe_auth_cookie(target, timeout_seconds=args.timeout_seconds)
    payload = {
        "target_id": target.target_id,
        "as_of_date": args.as_of_date,
        "include_refresh": include_refresh,
        "include_feedbacks": args.include_feedbacks,
        "auth": _probe_auth_summary(auth_cookie),
        **collect_loopback_surface(
            target,
            as_of_date=args.as_of_date,
            include_refresh=include_refresh,
            include_feedbacks=args.include_feedbacks,
            feedbacks_date_from=args.feedbacks_date_from,
            feedbacks_date_to=args.feedbacks_date_to,
            timeout_seconds=args.timeout_seconds,
            auth_cookie=auth_cookie,
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
    include_refresh = _probe_include_refresh(args)
    auth_cookie = _build_probe_auth_cookie(target, timeout_seconds=args.timeout_seconds)
    loopback_summary = collect_loopback_surface(
        target,
        as_of_date=args.as_of_date,
        include_refresh=include_refresh,
        include_feedbacks=args.include_feedbacks,
        feedbacks_date_from=args.feedbacks_date_from,
        feedbacks_date_to=args.feedbacks_date_to,
        timeout_seconds=args.timeout_seconds,
        auth_cookie=auth_cookie,
    )
    transport_disconnect = any(
        "ssh exit code 255" in str(route.get("network_error") or "").lower()
        for route in loopback_summary.get("routes") or []
    )
    if transport_disconnect and not args.dry_run:
        release_pr = os.environ.get("WB_CORE_RELEASE_PR", "").strip()
        release_head = os.environ.get("WB_CORE_RELEASE_HEAD", "").strip()
        release_merge = _git_output(["git", "rev-parse", "HEAD"]).strip().lower()
        if release_pr.isdigit() and release_head:
            from apps.hosted_runtime_transport_reconcile import reconcile

            reconciliation = reconcile(
                target_file=target_file,
                expected_sha=release_merge,
                pr=int(release_pr),
                head=release_head,
                merge=release_merge,
                failed_stage="probes",
            )
            deploy_summary["transport_reconciliation"] = reconciliation
            if bool(reconciliation.get("healthy")):
                loopback_summary = collect_loopback_surface(
                    target,
                    as_of_date=args.as_of_date,
                    include_refresh=include_refresh,
                    include_feedbacks=args.include_feedbacks,
                    feedbacks_date_from=args.feedbacks_date_from,
                    feedbacks_date_to=args.feedbacks_date_to,
                    timeout_seconds=args.timeout_seconds,
                    auth_cookie=auth_cookie,
                )
    public_summary = evaluate_surface_results(
        collect_public_surface(
            base_url=target.public_base_url,
            route_paths=target.route_paths,
            as_of_date=args.as_of_date,
            include_refresh=include_refresh,
            include_auto_updates_status=_probe_include_auto_updates_status(target),
            include_wb_warehouse_exclusion_options=_probe_include_wb_warehouse_exclusion_options(target),
            include_feedbacks=args.include_feedbacks,
            feedbacks_date_from=args.feedbacks_date_from,
            feedbacks_date_to=args.feedbacks_date_to,
            timeout_seconds=args.timeout_seconds,
            auth_cookie=auth_cookie,
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
            "include_refresh": include_refresh,
            "include_feedbacks": args.include_feedbacks,
            "auth": _probe_auth_summary(auth_cookie),
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


def run_warehouse_opening_command(args: argparse.Namespace) -> int:
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    action = str(args.warehouse_action)
    plan_path = Path(str(args.plan_file)).resolve() if action == "apply" else None
    payload = _run_remote_warehouse_opening_action(
        target,
        action=action,
        plan_path=plan_path,
        fingerprint=str(getattr(args, "fingerprint", "") or ""),
        diagnostic_nm_ids=tuple(getattr(args, "nm_id", ()) or ()),
    )
    if action == "dry-run" and str(getattr(args, "output", "") or "").strip():
        output_path = Path(str(args.output)).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    runtime_dir = str(target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or "").strip()
    _print_json(
        {
            "target_id": target.target_id,
            "ssh_destination": target.ssh_destination,
            "runtime_dir": runtime_dir,
            "action": action,
            "result": payload,
        }
    )
    return 0


def _run_remote_autoanswers_readonly(
    target: HostedRuntimeTarget,
    *,
    operation: str,
    page_size: int,
    max_pages: int,
    min_request_interval_seconds: float,
) -> dict[str, Any]:
    _ensure_active_hosted_runtime_target(target, action=f"autoanswers-readonly-{operation}")
    runtime_dir = str(target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or "").strip()
    if runtime_dir != ACTIVE_HOSTED_RUNTIME_RUNTIME_DIR:
        raise ValueError("autoanswers read-only runner requires the canonical active runtime dir")
    if not target.environment_file:
        raise ValueError("autoanswers read-only runner requires the hosted environment file")
    if operation not in {"status", "canary", "steady", "backfill", "manual-media-canary"}:
        raise ValueError(f"unsupported autoanswers read-only operation: {operation}")
    runner_args = [
        "python3",
        "apps/wb_autoanswers_readonly.py",
        "--operation",
        operation,
        "--runtime-dir",
        runtime_dir,
        "--page-size",
        str(min(5000, max(1, int(page_size)))),
        "--max-pages",
        str(max(1, int(max_pages))),
        "--min-request-interval-seconds",
        str(max(0.333, float(min_request_interval_seconds))),
    ]
    if operation != "status":
        runner_args.extend(["--env-file", target.environment_file])
    command = " && ".join(
        [
            f"cd {shlex.quote(target.target_dir)}",
            "/usr/bin/env WB_AUTOANSWERS_FORCE_OFF="
            + ("false" if operation == "manual-media-canary" else "true")
            + " WB_AUTOANSWERS_EXTERNAL_IO_ENABLED=true "
            + " ".join(shlex.quote(item) for item in runner_args),
        ]
    )
    result = subprocess.run(
        _remote_shell_command(target, command),
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=AUTOANSWERS_READONLY_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"autoanswers read-only {operation} failed: "
            + (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("autoanswers read-only runner returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("autoanswers read-only runner returned a non-object JSON payload")
    settings = ((payload.get("runtime") or {}).get("settings") or {})
    if operation == "manual-media-canary":
        if (
            not bool(settings.get("master_enabled"))
            or bool(settings.get("force_off"))
            or not bool(settings.get("effective_enabled"))
            or str(settings.get("mode") or "") != "manual"
        ):
            raise RuntimeError("autoanswers media evidence did not prove effective manual mode")
    elif not bool(settings.get("force_off")) or bool(settings.get("effective_enabled")):
        raise RuntimeError("autoanswers read-only evidence did not prove effective force-off")
    capabilities = ((payload.get("runtime") or {}).get("capabilities") or {})
    if bool(capabilities.get("wb_post_patch")) or bool(capabilities.get("openai")):
        raise RuntimeError("autoanswers read-only evidence exposed a forbidden capability")
    if operation == "status":
        backup = ((payload.get("runtime") or {}).get("schema_backup") or {})
        if int(backup.get("count") or 0) < 1 or str(backup.get("integrity_check") or "") != "ok":
            raise RuntimeError("autoanswers schema backup is missing or failed integrity readback")
    return payload


def _run_remote_autoanswers_readonly_timer(target: HostedRuntimeTarget, *, action: str) -> dict[str, Any]:
    _ensure_active_hosted_runtime_target(target, action=f"autoanswers-readonly-timer-{action}")
    if action not in {"status", "enable", "disable"}:
        raise ValueError(f"unsupported autoanswers timer action: {action}")
    unit = "wb-core-autoanswers-readonly-sync.timer"
    if action == "enable":
        status = _run_remote_autoanswers_readonly(
            target,
            operation="status",
            page_size=1,
            max_pages=1,
            min_request_interval_seconds=1.0,
        )
        settings = ((status.get("runtime") or {}).get("settings") or {})
        if not bool(settings.get("force_off")) or bool(settings.get("effective_enabled")):
            raise RuntimeError("GET-only timer cannot be enabled without effective emergency OFF")
        shell = (
            f"systemctl enable --now {shlex.quote(unit)}"
            f" && systemctl is-enabled {shlex.quote(unit)}"
            f" && systemctl is-active {shlex.quote(unit)}"
        )
    elif action == "disable":
        shell = (
            f"systemctl disable --now {shlex.quote(unit)}"
            f" && (systemctl is-enabled {shlex.quote(unit)} || true)"
            f" && (systemctl is-active {shlex.quote(unit)} || true)"
        )
    else:
        shell = (
            f"(systemctl is-enabled {shlex.quote(unit)} || true)"
            f" && (systemctl is-active {shlex.quote(unit)} || true)"
            f" && systemctl show {shlex.quote(unit)} --property=UnitFileState,ActiveState,NextElapseUSecRealtime --no-pager"
        )
    result = subprocess.run(
        _remote_shell_command(target, shell),
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=300.0,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"autoanswers read-only timer {action} failed: "
            + (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")
        )
    return {"status": "ok", "action": action, "unit": unit, "systemctl": result.stdout.strip().splitlines()}


def run_autoanswers_readonly_command(args: argparse.Namespace) -> int:
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    payload = _run_remote_autoanswers_readonly(
        target,
        operation=str(args.operation),
        page_size=int(args.page_size),
        max_pages=int(args.max_pages),
        min_request_interval_seconds=float(args.min_request_interval_seconds),
    )
    _print_json({"target_id": target.target_id, "result": payload})
    return 0


def run_autoanswers_readonly_timer_command(args: argparse.Namespace) -> int:
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    payload = _run_remote_autoanswers_readonly_timer(target, action=str(args.action))
    _print_json({"target_id": target.target_id, "result": payload})
    return 0


def _run_remote_autoanswers_lifecycle(
    target: HostedRuntimeTarget,
    *,
    action: str,
) -> dict[str, Any]:
    """Reconcile feature-owned mode through the canonical two-component lifecycle."""

    _ensure_active_hosted_runtime_target(target, action=f"autoanswers-lifecycle-{action}")
    if action not in {"status", "reconcile", "suspend"}:
        raise ValueError(f"unsupported autoanswers lifecycle action: {action}")
    runtime_dir = str(target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or "").strip()
    force_off = str(target.runtime_env.get("WB_AUTOANSWERS_FORCE_OFF") or "").strip().lower()
    if force_off not in {"true", "false"}:
        raise ValueError("autoanswers lifecycle requires an explicit target force-off boolean")
    app_command = (
        f"cd {shlex.quote(target.target_dir)} && "
        f"/usr/bin/env WB_AUTOANSWERS_FORCE_OFF={shlex.quote(force_off)} "
        f"python3 apps/wb_autoanswers_lifecycle.py {shlex.quote(action)} "
        f"--runtime-dir {shlex.quote(runtime_dir)}"
    )
    result = subprocess.run(
        _remote_shell_command(target, app_command),
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=AUTOANSWERS_LIFECYCLE_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"autoanswers lifecycle {action} failed: "
            + (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("autoanswers lifecycle returned invalid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(
        payload.get("lifecycle"), Mapping
    ):
        raise RuntimeError("autoanswers lifecycle returned incomplete evidence")
    lifecycle = dict(payload["lifecycle"])
    if action != "status" and str(lifecycle.get("drift_status") or "") != "matched":
        raise RuntimeError("autoanswers lifecycle mutation did not confirm component state")
    return payload


def run_autoanswers_lifecycle_command(args: argparse.Namespace) -> int:
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    payload = _run_remote_autoanswers_lifecycle(target, action=str(args.action))
    _print_json({"target_id": target.target_id, "result": payload})
    return 0


def _run_remote_autoanswers_budget_reconciliation(
    target: HostedRuntimeTarget,
    *,
    action: str,
    fingerprint: str = "",
) -> dict[str, Any]:
    _ensure_active_hosted_runtime_target(
        target,
        action=f"autoanswers-budget-reconciliation-{action}",
    )
    if action not in {"dry-run", "apply", "readback"}:
        raise ValueError(f"unsupported Autoanswers budget action: {action}")
    if action == "apply":
        _ensure_target_allows_mutation(
            target,
            action="autoanswers-budget-reconciliation-apply",
            dry_run=False,
        )
        if not fingerprint:
            raise ValueError("budget reconciliation apply requires --fingerprint")
    runtime_dir = str(
        target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""
    ).strip()
    args = [
        "python3",
        "apps/wb_autoanswers_budget_reconciliation.py",
        action,
        "--runtime-dir",
        runtime_dir,
    ]
    if action == "apply":
        args.extend(
            [
                "--fingerprint",
                fingerprint,
                "--actor",
                "release-train",
            ]
        )
    shell = (
        f"cd {shlex.quote(target.target_dir)} && "
        + " ".join(shlex.quote(item) for item in args)
    )
    result = subprocess.run(
        _remote_shell_command(target, shell),
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Autoanswers budget reconciliation {action} failed: "
            + (
                result.stderr.strip()
                or result.stdout.strip()
                or f"exit {result.returncode}"
            )
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Autoanswers budget reconciliation returned invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            "Autoanswers budget reconciliation returned a non-object payload"
        )
    return payload


def run_autoanswers_budget_reconciliation_command(
    args: argparse.Namespace,
) -> int:
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    payload = _run_remote_autoanswers_budget_reconciliation(
        target,
        action=str(args.action),
        fingerprint=str(args.fingerprint or ""),
    )
    _print_json({"target_id": target.target_id, "result": payload})
    return 0


def _run_remote_autoanswers_prefilter_skip_recovery(
    target: HostedRuntimeTarget,
    *,
    action: str,
    transition_run_id: str,
    expected_rows: int,
    fingerprint: str = "",
    source_fingerprint: str = "",
) -> dict[str, Any]:
    _ensure_active_hosted_runtime_target(
        target,
        action=f"autoanswers-prefilter-skip-recovery-{action}",
    )
    if action not in {
        "dry-run",
        "apply",
        "readback",
        "release-dry-run",
        "release-apply",
        "release-readback",
    }:
        raise ValueError(
            f"unsupported Autoanswers prefilter skip recovery action: {action}"
        )
    if not transition_run_id:
        raise ValueError("prefilter skip recovery requires --transition-run-id")
    if expected_rows <= 0:
        raise ValueError("prefilter skip recovery requires positive --expected-rows")
    if action in {"apply", "release-apply"}:
        _ensure_target_allows_mutation(
            target,
            action="autoanswers-prefilter-skip-recovery-apply",
            dry_run=False,
        )
        if not fingerprint:
            raise ValueError("prefilter skip recovery apply requires --fingerprint")
    if action.startswith("release-") and not source_fingerprint:
        raise ValueError(
            "prefilter skip latch recovery requires --source-fingerprint"
        )
    runtime_dir = str(
        target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""
    ).strip()
    command = [
        "python3",
        "apps/wb_autoanswers_prefilter_skip_recovery.py",
        action,
        "--runtime-dir",
        runtime_dir,
        "--transition-run-id",
        transition_run_id,
        "--expected-rows",
        str(expected_rows),
    ]
    if action == "apply":
        command.extend(
            [
                "--fingerprint",
                fingerprint,
                "--actor",
                "release-train",
            ]
        )
    elif action == "release-apply":
        command.extend(
            [
                "--fingerprint",
                fingerprint,
                "--source-fingerprint",
                source_fingerprint,
                "--actor",
                "release-train",
            ]
        )
    elif action.startswith("release-"):
        command.extend(
            [
                "--source-fingerprint",
                source_fingerprint,
            ]
        )
    shell = (
        f"cd {shlex.quote(target.target_dir)} && "
        + " ".join(shlex.quote(item) for item in command)
    )
    result = subprocess.run(
        _remote_shell_command(target, shell),
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Autoanswers prefilter skip recovery {action} failed: "
            + (
                result.stderr.strip()
                or result.stdout.strip()
                or f"exit {result.returncode}"
            )
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Autoanswers prefilter skip recovery returned invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            "Autoanswers prefilter skip recovery returned a non-object payload"
        )
    return payload


def run_autoanswers_prefilter_skip_recovery_command(
    args: argparse.Namespace,
) -> int:
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    payload = _run_remote_autoanswers_prefilter_skip_recovery(
        target,
        action=str(args.action),
        transition_run_id=str(args.transition_run_id or ""),
        expected_rows=int(args.expected_rows),
        fingerprint=str(args.fingerprint or ""),
        source_fingerprint=str(args.source_fingerprint or ""),
    )
    _print_json({"target_id": target.target_id, "result": payload})
    return 0


def run_warehouse_functional_command(args: argparse.Namespace) -> int:
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    action = str(args.warehouse_functional_action)
    plan_path = Path(str(args.plan_file)).resolve() if action in {
        "cutover-apply",
        "sync-apply",
        "emergency-apply",
        "economics-backfill-apply",
        "supplier-certification-apply",
    } else None
    payload = _run_remote_warehouse_functional_action(
        target,
        action=action,
        plan_path=plan_path,
        fingerprint=str(getattr(args, "fingerprint", "") or ""),
        reason=str(getattr(args, "reason", "") or ""),
    )
    output = str(getattr(args, "output", "") or "").strip()
    if action in {
        "cutover-dry-run",
        "sync-dry-run",
        "emergency-dry-run",
        "economics-backfill-dry-run",
        "supplier-certification-dry-run",
    } and output:
        output_path = Path(output).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # The remote cutover dry-run returns diagnostic refresh evidence next to
        # the signed plan. Keep that evidence in stdout, but never inject it
        # into the exact reviewed plan file: apply_plan hashes every plan field.
        reviewed_plan = dict(payload)
        reviewed_plan.pop("preflight_supply_refresh", None)
        output_path.write_text(
            json.dumps(reviewed_plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
    _print_json(
        {
            "target_id": target.target_id,
            "ssh_destination": target.ssh_destination,
            "runtime_dir": str(target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""),
            "action": action,
            "result": payload,
        }
    )
    return 0


def run_warehouse_recovery_canary_command(args: argparse.Namespace) -> int:
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    apply = bool(args.recovery_canary_apply)
    action = (
        "warehouse-recovery-canary-apply"
        if apply
        else "warehouse-recovery-canary-dry-run"
    )
    _ensure_active_hosted_runtime_target(target, action=action)
    if apply:
        _ensure_target_allows_mutation(target, action=action, dry_run=False)
    deployed_sha = str(args.deployed_sha or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", deployed_sha):
        raise ValueError("warehouse recovery canary requires an exact deployed SHA")
    runtime_dir = str(
        target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""
    ).strip()
    if runtime_dir != ACTIVE_HOSTED_RUNTIME_RUNTIME_DIR:
        raise ValueError("warehouse recovery canary requires the canonical runtime dir")
    runner_args = [
        "python3",
        "apps/warehouse_recovery_policy_canary.py",
        "--runtime-dir",
        runtime_dir,
        "--deployed-sha",
        deployed_sha,
    ]
    if apply:
        runner_args.extend(["--apply", "--confirm", str(args.fingerprint)])
    shell_command = " && ".join(
        [
            f"cd {shlex.quote(target.target_dir)}",
            (
                "test \"$(tr -d '\\r\\n' < "
                + shlex.quote(
                    f"{target.target_dir.rstrip('/')}/.wb-core-runtime-sha"
                )
                + ")\" = "
                + shlex.quote(deployed_sha)
            ),
            " ".join(shlex.quote(item) for item in runner_args),
        ]
    )
    result = subprocess.run(
        _remote_shell_command(target, shell_command),
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=WAREHOUSE_RECOVERY_LIFECYCLE_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{action} failed: "
            + (
                result.stderr.strip()
                or result.stdout.strip()
                or f"exit {result.returncode}"
            )
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("warehouse recovery canary returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("warehouse recovery canary returned a non-object payload")
    _print_json(
        {
            "target_id": target.target_id,
            "ssh_destination": target.ssh_destination,
            "runtime_dir": runtime_dir,
            "action": action,
            "result": payload,
        }
    )
    return 0


def run_warehouse_recovery_retention_command(args: argparse.Namespace) -> int:
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    apply = bool(args.recovery_retention_apply)
    action = (
        "warehouse-recovery-retention-apply"
        if apply
        else "warehouse-recovery-retention-dry-run"
    )
    _ensure_active_hosted_runtime_target(target, action=action)
    if apply:
        _ensure_target_allows_mutation(target, action=action, dry_run=False)
    deployed_sha = str(args.deployed_sha or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", deployed_sha):
        raise ValueError("warehouse recovery retention requires an exact deployed SHA")
    runtime_dir = str(
        target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""
    ).strip()
    if runtime_dir != ACTIVE_HOSTED_RUNTIME_RUNTIME_DIR:
        raise ValueError(
            "warehouse recovery retention requires the canonical runtime dir"
        )
    runner_args = [
        "python3",
        "apps/warehouse_recovery_retention.py",
        "apply" if apply else "dry-run",
        "--runtime-dir",
        runtime_dir,
        "--deployed-sha",
        deployed_sha,
    ]
    if apply:
        runner_args.extend(["--fingerprint", str(args.fingerprint)])
    shell_command = " && ".join(
        [
            f"cd {shlex.quote(target.target_dir)}",
            " ".join(shlex.quote(item) for item in runner_args),
        ]
    )
    result = subprocess.run(
        _remote_shell_command(target, shell_command),
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=WAREHOUSE_RECOVERY_LIFECYCLE_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{action} failed: "
            + (
                result.stderr.strip()
                or result.stdout.strip()
                or f"exit {result.returncode}"
            )
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("warehouse recovery retention returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("warehouse recovery retention returned a non-object payload")
    _print_json(
        {
            "target_id": target.target_id,
            "ssh_destination": target.ssh_destination,
            "runtime_dir": runtime_dir,
            "action": action,
            "result": payload,
        }
    )
    return 0


def run_storage_recovery_sanitation_command(args: argparse.Namespace) -> int:
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    sanitation_action = str(args.storage_sanitation_action)
    action = f"storage-recovery-sanitation-{sanitation_action}"
    _ensure_active_hosted_runtime_target(target, action=action)
    if sanitation_action == "apply":
        _ensure_target_allows_mutation(target, action=action, dry_run=False)
    deployed_sha = str(args.deployed_sha or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", deployed_sha):
        raise ValueError("storage sanitation requires an exact deployed SHA")
    runtime_dir = str(
        target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""
    ).strip()
    if runtime_dir != ACTIVE_HOSTED_RUNTIME_RUNTIME_DIR:
        raise ValueError("storage sanitation requires the canonical runtime dir")
    runner_args = [
        "python3",
        "apps/storage_recovery_sanitation.py",
        "--runtime-dir",
        runtime_dir,
        "--root-backups",
        "/opt/wb-core-runtime/backups",
        "--deployed-sha",
        deployed_sha,
        "--deployed-sha-file",
        f"{target.target_dir.rstrip('/')}/.wb-core-runtime-sha",
        sanitation_action,
    ]
    if sanitation_action in {"plan", "apply"}:
        runner_args.extend(
            [
                "--root",
                str(args.sanitation_root),
                "--family",
                str(args.family),
                "--reserved-free-bytes",
                str(int(args.reserved_free_bytes)),
            ]
        )
    if sanitation_action == "apply":
        runner_args.extend(["--fingerprint", str(args.fingerprint)])
    shell_command = " && ".join(
        [
            f"cd {shlex.quote(target.target_dir)}",
            (
                "test \"$(tr -d '\\r\\n' < "
                + shlex.quote(
                    f"{target.target_dir.rstrip('/')}/.wb-core-runtime-sha"
                )
                + ")\" = "
                + shlex.quote(deployed_sha)
            ),
            " ".join(shlex.quote(item) for item in runner_args),
        ]
    )
    result = subprocess.run(
        _remote_shell_command(target, shell_command),
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=WAREHOUSE_RECOVERY_LIFECYCLE_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{action} failed: "
            + (
                result.stderr.strip()
                or result.stdout.strip()
                or f"exit {result.returncode}"
            )
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("storage sanitation returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("storage sanitation returned a non-object payload")
    _print_json(
        {
            "target_id": target.target_id,
            "ssh_destination": target.ssh_destination,
            "runtime_dir": runtime_dir,
            "action": action,
            "result": payload,
        }
    )
    return 0


def run_storage_recovery_sanitation_job_command(
    args: argparse.Namespace,
) -> int:
    """Submit or read one durable detached sanitation job."""

    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    job_action = str(args.sanitation_job_action)
    action = f"storage-recovery-sanitation-{job_action}"
    _ensure_active_hosted_runtime_target(target, action=action)
    if job_action == "submit":
        _ensure_target_allows_mutation(target, action=action, dry_run=False)
    if "wb-core-storage-recovery-sanitation@.service" not in {
        unit.name for unit in target.managed_systemd_units
    }:
        raise ValueError(
            "detached sanitation requires the repo-owned managed systemd template"
        )
    deployed_sha = str(args.deployed_sha or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", deployed_sha):
        raise ValueError("detached sanitation requires an exact deployed SHA")
    job_id = str(args.job_id or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", job_id):
        raise ValueError(
            "detached sanitation requires an exact 64-hex caller-known job id"
        )
    runtime_dir = str(
        target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""
    ).strip()
    if runtime_dir != ACTIVE_HOSTED_RUNTIME_RUNTIME_DIR:
        raise ValueError("detached sanitation requires the canonical runtime dir")
    runner_args = [
        "python3",
        "apps/storage_recovery_sanitation_job.py",
        "--runtime-dir",
        runtime_dir,
        "--root-backups",
        "/opt/wb-core-runtime/backups",
        "--deployed-sha-file",
        f"{target.target_dir.rstrip('/')}/.wb-core-runtime-sha",
        job_action,
        "--job-id",
        job_id,
        "--deployed-sha",
        deployed_sha,
    ]
    if job_action == "submit":
        operation = str(args.operation)
        fingerprint = str(args.fingerprint or "").strip()
        if operation == "apply" and not re.fullmatch(
            r"sha256:[0-9a-f]{64}",
            fingerprint,
        ):
            raise ValueError(
                "detached sanitation apply requires an exact fingerprint"
            )
        if operation == "plan" and fingerprint:
            raise ValueError(
                "detached sanitation plan must not carry an apply fingerprint"
            )
        runner_args.extend(
            [
                "--operation",
                operation,
                "--root",
                str(args.sanitation_root),
                "--family",
                str(args.family),
                "--reserved-free-bytes",
                str(int(args.reserved_free_bytes)),
            ]
        )
        if fingerprint:
            runner_args.extend(["--fingerprint", fingerprint])
    shell_command = " && ".join(
        [
            f"cd {shlex.quote(target.target_dir)}",
            (
                "test \"$(tr -d '\\r\\n' < "
                + shlex.quote(
                    f"{target.target_dir.rstrip('/')}/.wb-core-runtime-sha"
                )
                + ")\" = "
                + shlex.quote(deployed_sha)
            ),
            " ".join(shlex.quote(item) for item in runner_args),
        ]
    )
    result = subprocess.run(
        _remote_shell_command(target, shell_command),
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=60.0,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{action} failed: "
            + (
                result.stderr.strip()
                or result.stdout.strip()
                or f"exit {result.returncode}"
            )
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("detached sanitation returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("detached sanitation returned a non-object payload")
    _print_json(
        {
            "target_id": target.target_id,
            "ssh_destination": target.ssh_destination,
            "runtime_dir": runtime_dir,
            "action": action,
            "job_id": job_id,
            "result": payload,
        }
    )
    return 0


def run_business_data_maintenance_restore_job_command(
    args: argparse.Namespace,
) -> int:
    """Submit or read one exact detached business-data restore."""

    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    job_action = str(args.maintenance_restore_job_action)
    action = f"business-data-maintenance-restore-{job_action}"
    _ensure_active_hosted_runtime_target(target, action=action)
    if job_action == "submit":
        _ensure_target_allows_mutation(target, action=action, dry_run=False)
    if "wb-core-business-data-maintenance-restore@.service" not in {
        unit.name for unit in target.managed_systemd_units
    }:
        raise ValueError(
            "detached maintenance restore requires the repo-owned managed "
            "systemd template"
        )
    deployed_sha = str(args.deployed_sha or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", deployed_sha):
        raise ValueError(
            "detached maintenance restore requires an exact deployed SHA"
        )
    job_id = str(args.job_id or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", job_id):
        raise ValueError(
            "detached maintenance restore requires an exact 64-hex job id"
        )
    runtime_dir = str(
        target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""
    ).strip()
    if runtime_dir != ACTIVE_HOSTED_RUNTIME_RUNTIME_DIR:
        raise ValueError(
            "detached maintenance restore requires the canonical runtime dir"
        )
    if target.environment_file != "/opt/wb-ai/.env":
        raise ValueError(
            "detached maintenance restore requires the canonical environment file"
        )
    runner_args = [
        "python3",
        "apps/business_data_maintenance_restore_job.py",
        "--runtime-dir",
        runtime_dir,
        "--app-dir",
        target.target_dir,
        "--env-file",
        target.environment_file,
        "--deployed-sha-file",
        f"{target.target_dir.rstrip('/')}/.wb-core-runtime-sha",
        job_action,
        "--job-id",
        job_id,
        "--deployed-sha",
        deployed_sha,
    ]
    if job_action == "submit":
        if args.expected_revision is None or int(args.expected_revision) < 0:
            raise ValueError(
                "detached maintenance restore requires an exact policy revision"
            )
        fingerprint = str(args.plan_fingerprint or "").strip().lower()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint):
            raise ValueError(
                "detached maintenance restore requires an exact plan fingerprint"
            )
        continuity_fingerprint = str(
            args.service_continuity_fingerprint or ""
        ).strip().lower()
        if not re.fullmatch(
            r"sha256:[0-9a-f]{64}",
            continuity_fingerprint,
        ):
            raise ValueError(
                "detached maintenance restore requires an exact service "
                "continuity fingerprint"
            )
        window_id = str(args.window_id or "").strip()
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
            window_id,
        ):
            raise ValueError(
                "detached maintenance restore requires an exact window id"
            )
        if not bool(args.allow_pre_hold_service_continuity):
            raise ValueError(
                "detached maintenance restore requires explicit pre-hold "
                "service continuity"
            )
        actor = str(args.actor or "").strip()
        reason = str(args.reason or "").strip()
        if not actor or not reason:
            raise ValueError(
                "detached maintenance restore requires audited actor and reason"
            )
        runner_args.extend(
            [
                "--expected-revision",
                str(int(args.expected_revision)),
                "--window-id",
                window_id,
                "--plan-fingerprint",
                fingerprint,
                "--service-continuity-fingerprint",
                continuity_fingerprint,
                "--actor",
                actor,
                "--reason",
                reason,
                "--allow-pre-hold-service-continuity",
            ]
        )
    shell_command = " && ".join(
        [
            f"cd {shlex.quote(target.target_dir)}",
            (
                "test \"$(tr -d '\\r\\n' < "
                + shlex.quote(
                    f"{target.target_dir.rstrip('/')}/.wb-core-runtime-sha"
                )
                + ")\" = "
                + shlex.quote(deployed_sha)
            ),
            " ".join(shlex.quote(item) for item in runner_args),
        ]
    )
    result = subprocess.run(
        _remote_shell_command(target, shell_command),
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=60.0,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{action} failed: "
            + (
                result.stderr.strip()
                or result.stdout.strip()
                or f"exit {result.returncode}"
            )
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "detached maintenance restore returned invalid JSON"
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("contract_name")
        != "business_data_maintenance_restore_job_v1"
        or str(payload.get("job_id") or "") != job_id
    ):
        raise RuntimeError(
            "detached maintenance restore returned an invalid identity"
        )
    _print_json(
        {
            "target_id": target.target_id,
            "ssh_destination": target.ssh_destination,
            "runtime_dir": runtime_dir,
            "action": action,
            "job_id": job_id,
            "result": payload,
        }
    )
    return 0


def run_promo_archive_gc_command(args: argparse.Namespace) -> int:
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    apply = bool(args.promo_gc_apply)
    action = "promo-archive-gc-apply" if apply else "promo-archive-gc-dry-run"
    _ensure_active_hosted_runtime_target(target, action=action)
    if apply:
        _ensure_target_allows_mutation(target, action=action, dry_run=False)
    deployed_sha = str(args.deployed_sha or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", deployed_sha):
        raise ValueError("promo archive GC requires an exact deployed SHA")
    runtime_dir = str(
        target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""
    ).strip()
    if runtime_dir != ACTIVE_HOSTED_RUNTIME_RUNTIME_DIR:
        raise ValueError("promo archive GC requires the canonical runtime dir")
    runner_args = [
        "python3",
        "apps/promo_campaign_archive_gc.py",
        "apply" if apply else "dry-run",
        "--runtime-dir",
        runtime_dir,
    ]
    if apply:
        runner_args.extend(
            [
                "--confirm",
                "--fingerprint",
                str(args.fingerprint),
                "--deployed-sha",
                deployed_sha,
                "--deployed-sha-file",
                f"{target.target_dir.rstrip('/')}/.wb-core-runtime-sha",
            ]
        )
    shell_command = " && ".join(
        [
            f"cd {shlex.quote(target.target_dir)}",
            (
                "test \"$(tr -d '\\r\\n' < "
                + shlex.quote(
                    f"{target.target_dir.rstrip('/')}/.wb-core-runtime-sha"
                )
                + ")\" = "
                + shlex.quote(deployed_sha)
            ),
            " ".join(shlex.quote(item) for item in runner_args),
        ]
    )
    result = subprocess.run(
        _remote_shell_command(target, shell_command),
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=WAREHOUSE_RECOVERY_LIFECYCLE_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{action} failed: "
            + (
                result.stderr.strip()
                or result.stdout.strip()
                or f"exit {result.returncode}"
            )
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("promo archive GC returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("promo archive GC returned a non-object payload")
    _print_json(
        {
            "target_id": target.target_id,
            "ssh_destination": target.ssh_destination,
            "runtime_dir": runtime_dir,
            "action": action,
            "result": payload,
        }
    )
    return 0


def run_sqlite_backup_archive_command(args: argparse.Namespace) -> int:
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    payload = _run_remote_sqlite_backup_archive(
        target,
        apply=bool(args.archive_apply),
        source=str(args.source),
        fingerprint=str(getattr(args, "fingerprint", "") or ""),
        reserved_free_bytes=int(args.reserved_free_bytes),
    )
    output = str(getattr(args, "output", "") or "").strip()
    if output:
        output_path = Path(output).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n",
            encoding="utf-8",
        )
    _print_json(
        {
            "target_id": target.target_id,
            "ssh_destination": target.ssh_destination,
            "action": (
                "sqlite-backup-archive-apply"
                if bool(args.archive_apply)
                else "sqlite-backup-archive-dry-run"
            ),
            "result": payload,
        }
    )
    return 0


def run_warehouse_cost_queue_replay_command(
    args: argparse.Namespace,
) -> int:
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    apply = bool(args.queue_replay_apply)
    payload = _run_remote_warehouse_cost_queue_replay(
        target,
        apply=apply,
        invoice_numbers=list(args.invoice_no or []),
        plan_path=(
            Path(str(args.plan_file)).resolve()
            if apply
            else None
        ),
        fingerprint=str(getattr(args, "fingerprint", "") or ""),
    )
    output = str(getattr(args, "output", "") or "").strip()
    if output:
        output_path = Path(output).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n",
            encoding="utf-8",
        )
    _print_json(
        {
            "target_id": target.target_id,
            "ssh_destination": target.ssh_destination,
            "action": (
                "warehouse-cost-queue-replay-apply"
                if apply
                else "warehouse-cost-queue-replay-dry-run"
            ),
            "result": payload,
        }
    )
    return 0


def run_finance_canonical_command(args: argparse.Namespace) -> int:
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    action = str(args.finance_canonical_action)
    plan_path = (
        Path(str(args.plan_file)).resolve() if action == "apply" else None
    )
    if plan_path is not None and (plan_path == ROOT or ROOT in plan_path.parents):
        raise ValueError("Finance canonical reviewed plan must stay outside the Git checkout")
    payload = _run_remote_finance_canonical_action(
        target,
        action=action,
        plan_path=plan_path,
        fingerprint=str(getattr(args, "fingerprint", "") or ""),
        approval_reference=str(getattr(args, "approval_reference", "") or ""),
    )
    output = str(getattr(args, "output", "") or "").strip()
    if action == "dry-run" and output:
        output_path = Path(output).resolve()
        if output_path == ROOT or ROOT in output_path.parents:
            raise ValueError("Finance canonical evidence output must stay outside the Git checkout")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        output_path.chmod(0o600)
    _print_json(
        {
            "target_id": target.target_id,
            "ssh_destination": target.ssh_destination,
            "runtime_dir": str(target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""),
            "action": action,
            "result": payload,
        }
    )
    return 0


def _run_remote_finance_canonical_action(
    target: HostedRuntimeTarget,
    *,
    action: str,
    plan_path: Path | None,
    fingerprint: str,
    approval_reference: str,
) -> dict[str, Any]:
    _ensure_active_hosted_runtime_target(target, action=f"finance-canonical-{action}")
    if action == "apply":
        _ensure_target_allows_mutation(target, action="finance-canonical-apply", dry_run=False)
    if action not in {"dry-run", "apply", "readback"}:
        raise ValueError(f"unsupported Finance canonical action: {action}")
    runtime_dir = str(target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or "").strip()
    if runtime_dir != ACTIVE_HOSTED_RUNTIME_RUNTIME_DIR:
        raise ValueError("Finance canonical runner requires the canonical active runtime dir")
    if not target.environment_file:
        raise ValueError("Finance canonical runner requires the hosted environment file")
    runner_args = [
        "python3",
        "apps/wb_finance_weekly.py",
        "canonical-cost-backfill",
        "--runtime-dir",
        runtime_dir,
        "--env-file",
        target.environment_file,
    ]
    if action == "apply":
        if plan_path is None or not plan_path.is_file():
            raise ValueError("Finance canonical apply requires an existing --plan-file")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if not isinstance(plan, dict) or str(plan.get("fingerprint") or "") != fingerprint:
            raise ValueError("Finance canonical plan and --fingerprint do not match")
        if (
            str(plan.get("schema_version") or "") != "wb_finance_canonical_cost_backfill_v2"
            or plan.get("dry_run") is not True
            or not bool(plan.get("apply_allowed"))
        ):
            raise ValueError("Finance canonical reviewed plan is not ready for apply")
        if not approval_reference.strip():
            raise ValueError("Finance canonical apply requires --approval-reference")
        runner_args.extend(
            [
                "--apply",
                "--confirm-fingerprint",
                fingerprint,
                "--backup-dir",
                "/opt/wb-core-runtime/backups/wb-finance-canonical",
                "--approval-reference",
                approval_reference.strip(),
            ]
        )
    shell_command = " && ".join(
        [
            f"cd {shlex.quote(target.target_dir)}",
            " ".join(shlex.quote(item) for item in runner_args),
        ]
    )
    result = subprocess.run(
        _remote_shell_command(target, shell_command),
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=(
            FINANCE_CANONICAL_MUTATION_TIMEOUT_SECONDS
            if action == "apply"
            else FINANCE_CANONICAL_READ_TIMEOUT_SECONDS
        ),
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Finance canonical {action} failed: "
            + (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Finance canonical runner returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Finance canonical runner returned a non-object JSON payload")
    if action == "readback":
        nonzero_deltas = [
            {
                "week_start": week.get("week_start"),
                "delta": week.get("delta"),
            }
            for week in payload.get("weeks") or []
            if any(
                value not in {None, "0.0000"}
                for value in (week.get("delta") or {}).values()
            )
        ]
        if payload.get("blockers") or nonzero_deltas:
            raise RuntimeError(
                "Finance canonical readback has blockers or non-zero derived deltas"
            )
        return {**payload, "status": "ready", "readback": True}
    return payload


def _restore_finance_storage_window(
    target: HostedRuntimeTarget,
    *,
    hold: Mapping[str, Any],
    window_id: str,
    fingerprint: str,
    reason: str,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    evidence["barrier_restoring"] = (
        _run_remote_business_data_maintenance_runner(
            target,
            action="barrier-restoring",
            window_id=window_id,
            plan_fingerprint=fingerprint,
        )
    )
    paused_revision = int(
        ((hold.get("auto_updates") or {}).get("revision") or 0)
    )
    if paused_revision <= 0:
        raise RuntimeError(
            "Finance maintenance hold lacks exact paused policy revision"
        )
    restore = _run_remote_business_data_maintenance_runner(
        target,
        action="restore",
        expected_revision=paused_revision,
        actor="finance_storage_window_runner",
        reason=reason,
    )
    evidence["business_restore"] = restore
    if (
        str(restore.get("status") or "") != "restored"
        or restore.get("exact_prior_state_restored") is not True
    ):
        raise RuntimeError(
            "Finance maintenance exact writer/timer restore is incomplete"
        )
    evidence["warehouse_restore"] = (
        _run_remote_warehouse_functional_maintenance_action(
            target,
            action="restore",
        )
    )
    evidence["barrier_release"] = (
        _run_remote_business_data_maintenance_runner(
            target,
            action="barrier-release",
            actor="finance_storage_window_runner",
            reason="exact pre-window control state restored",
            window_id=window_id,
            plan_fingerprint=fingerprint,
        )
    )
    return evidence


def _abort_finance_storage_window_acquire(
    target: HostedRuntimeTarget,
    *,
    window_id: str,
    fingerprint: str,
    reason: str,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    status = _run_remote_business_data_maintenance_runner(
        target,
        action="status",
    )
    evidence["pre_abort_status"] = status
    paused_revision = int(
        ((status.get("auto_updates") or {}).get("revision") or 0)
    )
    if paused_revision <= 0:
        raise RuntimeError(
            "Finance acquire abort lacks exact paused policy revision"
        )
    restore = _run_remote_business_data_maintenance_runner(
        target,
        action="restore",
        expected_revision=paused_revision,
        actor="finance_storage_window_runner",
        reason=reason,
        allow_pre_hold_service_continuity=True,
    )
    evidence["business_restore"] = restore
    if (
        str(restore.get("status") or "") != "restored"
        or restore.get("exact_prior_state_restored") is not True
    ):
        raise RuntimeError(
            "Finance acquire abort exact control restore is incomplete"
        )
    evidence["warehouse_restore"] = (
        _run_remote_warehouse_functional_maintenance_action(
            target,
            action="restore",
        )
    )
    evidence["barrier_abort"] = (
        _run_remote_business_data_maintenance_runner(
            target,
            action="barrier-abort",
            actor="finance_storage_window_runner",
            reason="unconfirmed Finance window aborted after exact restore",
            window_id=window_id,
            plan_fingerprint=fingerprint,
        )
    )
    return evidence


def _acquire_and_confirm_finance_storage_window(
    target: HostedRuntimeTarget,
    *,
    transition_evidence: dict[str, Any],
    actor: str,
    reason: str,
    window_id: str,
    window_kind: str,
    fingerprint: str,
    approval_reference: str,
) -> dict[str, Any]:
    transition_evidence["barrier_acquire"] = (
        _run_remote_business_data_maintenance_runner(
            target,
            action="barrier-acquire",
            actor=actor,
            reason=reason,
            window_id=window_id,
            window_kind=window_kind,
            plan_fingerprint=fingerprint,
            approval_reference=approval_reference,
        )
    )
    try:
        transition_evidence["core_prepare"] = (
            _run_remote_business_data_maintenance_runner(
                target,
                action="prepare",
                actor=actor,
                reason=reason,
            )
        )
        transition_evidence["warehouse_hold"] = (
            _run_remote_warehouse_functional_maintenance_action(
                target,
                action="hold",
                disable_timer=True,
            )
        )
        hold = _run_remote_business_data_maintenance_runner(
            target,
            action="hold",
            actor=actor,
            reason=reason,
        )
        transition_evidence["business_hold"] = hold
        if (
            str(hold.get("status") or "") != "held"
            or hold.get("quiet") is not True
        ):
            raise RuntimeError(
                "Finance writer/timer hold readback is incomplete"
            )
    except Exception as hold_error:
        try:
            transition_evidence["acquire_abort"] = (
                _abort_finance_storage_window_acquire(
                    target,
                    window_id=window_id,
                    fingerprint=fingerprint,
                    reason=(
                        f"{reason}: drain failed before hold confirmation"
                    ),
                )
            )
        except Exception as recovery_error:
            raise RuntimeError(
                "Finance drain failed before hold confirmation; the barrier "
                "remains active because exact abort restore also failed: "
                f"hold={hold_error}; restore={recovery_error}"
            ) from recovery_error
        raise RuntimeError(
            "Finance drain failed before hold confirmation; exact controls "
            f"and barrier were restored: {hold_error}"
        ) from hold_error
    transition_evidence["barrier_confirm"] = (
        _run_remote_business_data_maintenance_runner(
            target,
            action="barrier-confirm",
            window_id=window_id,
            plan_fingerprint=fingerprint,
        )
    )
    return hold


def _restart_finance_cutover_http_service(
    target: HostedRuntimeTarget,
) -> dict[str, Any]:
    _ensure_active_hosted_runtime_target(
        target,
        action="finance-storage-cutover-http-restart",
    )
    if target.service_name != ACTIVE_HOSTED_RUNTIME_SERVICE_NAME:
        raise ValueError(
            "Finance cutover requires the canonical registry HTTP service"
        )
    command = (
        f"systemctl restart {shlex.quote(ACTIVE_HOSTED_RUNTIME_SERVICE_NAME)}"
        f" && systemctl is-active {shlex.quote(ACTIVE_HOSTED_RUNTIME_SERVICE_NAME)}"
    )
    result = subprocess.run(
        _remote_shell_command(target, command),
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=300.0,
        check=False,
    )
    if result.returncode != 0 or result.stdout.strip().splitlines()[-1:] != [
        "active"
    ]:
        raise RuntimeError(
            "Finance cutover HTTP service restart/readback failed: "
            + (
                result.stderr.strip()
                or result.stdout.strip()
                or f"exit {result.returncode}"
            )
        )
    return {
        "service": ACTIVE_HOSTED_RUNTIME_SERVICE_NAME,
        "status": "active",
        "unrelated_services_restarted": [],
    }


def run_finance_storage_split_command(args: argparse.Namespace) -> int:
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    action = str(args.finance_storage_split_action)
    plan_path = (
        Path(str(args.plan_file)).resolve()
        if action
        in {
            "apply",
            "snapshot-create",
            "cutover-apply",
            "rollback-prepare",
            "rollback-apply",
        }
        else None
    )
    if plan_path is not None and (plan_path == ROOT or ROOT in plan_path.parents):
        raise ValueError("Finance storage reviewed plan must stay outside the Git checkout")
    fingerprint = str(getattr(args, "fingerprint", "") or "")
    approval_reference = str(
        getattr(args, "approval_reference", "") or ""
    )
    source_snapshot_manifest = str(
        getattr(args, "source_snapshot_manifest", "") or ""
    )
    candidate_manifest = str(
        getattr(args, "candidate_manifest", "") or ""
    )
    candidate_plan_fingerprint = str(
        getattr(args, "candidate_plan_fingerprint", "") or ""
    )
    minimum_observation_seconds = int(
        getattr(args, "minimum_observation_seconds", 3600) or 0
    )
    rollback_candidate_evidence = str(
        getattr(args, "rollback_candidate_evidence", "") or ""
    )
    transition_evidence: dict[str, Any] = {}
    if action == "rollback-apply":
        if plan_path is None or not plan_path.is_file():
            raise ValueError(
                "Finance storage rollback requires --plan-file"
            )
        reviewed_plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if (
            not isinstance(reviewed_plan, dict)
            or str(reviewed_plan.get("contract_version") or "")
            != "wb_core_finance_storage_rollback_plan_v1"
            or str(reviewed_plan.get("fingerprint") or "") != fingerprint
            or not bool(
                reviewed_plan.get("apply_allowed_after_candidate_readback")
            )
            or not rollback_candidate_evidence
        ):
            raise ValueError(
                "Finance storage rollback reviewed plan/candidate is not ready"
            )
        window_id = (
            "rollback-" + fingerprint.removeprefix("sha256:")[:20]
        )
        hold = _acquire_and_confirm_finance_storage_window(
            target,
            transition_evidence=transition_evidence,
            actor="finance_storage_rollback_runner",
            reason="Finance split rollback drill",
            window_id=window_id,
            window_kind="rollback_drill",
            fingerprint=fingerprint,
            approval_reference=approval_reference,
        )
        try:
            payload = _run_remote_finance_storage_split_action(
                target,
                action=action,
                plan_path=plan_path,
                fingerprint=fingerprint,
                approval_reference=approval_reference,
                chunk_size=int(
                    getattr(args, "chunk_size", 10_000) or 10_000
                ),
                rollback_candidate_evidence=(
                    rollback_candidate_evidence
                ),
            )
            transition_evidence["http_restart"] = (
                _restart_finance_cutover_http_service(target)
            )
        except Exception as exc:
            health = _run_remote_finance_storage_split_action(
                target,
                action="health",
                plan_path=None,
                fingerprint="",
                approval_reference="",
                chunk_size=int(
                    getattr(args, "chunk_size", 10_000) or 10_000
                ),
            )
            transition_evidence["failure_health_readback"] = health
            if str(health.get("canonical_source") or "") == "monolith":
                raise RuntimeError(
                    "Finance rollback failed after the rollback manifest "
                    "became canonical; the HTTP barrier and writer holds "
                    f"remain active for bounded recovery: {exc}"
                ) from exc
            transition_evidence.update(
                _restore_finance_storage_window(
                    target,
                    hold=hold,
                    window_id=window_id,
                    fingerprint=fingerprint,
                    reason="Finance rollback failed before manifest switch",
                )
            )
            raise RuntimeError(
                "Finance rollback failed before the manifest switch; exact "
                f"controls were restored: {exc}"
            ) from exc
        transition_evidence.update(
            _restore_finance_storage_window(
                target,
                hold=hold,
                window_id=window_id,
                fingerprint=fingerprint,
                reason="Finance rollback and HTTP restart completed",
            )
        )
    elif action == "cutover-apply":
        if plan_path is None or not plan_path.is_file():
            raise ValueError(
                "Finance storage cutover requires --plan-file"
            )
        reviewed_plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if (
            not isinstance(reviewed_plan, dict)
            or str(reviewed_plan.get("contract_version") or "")
            != "wb_core_finance_storage_cutover_plan_v1"
            or str(reviewed_plan.get("fingerprint") or "") != fingerprint
            or str(
                reviewed_plan.get("candidate_plan_fingerprint") or ""
            )
            != candidate_plan_fingerprint
            or not bool(
                reviewed_plan.get("apply_allowed_by_machine_preflight")
            )
        ):
            raise ValueError(
                "Finance storage cutover reviewed plan is not ready"
            )
        window_id = (
            "final-cutover-"
            + fingerprint.removeprefix("sha256:")[:20]
        )
        hold = _acquire_and_confirm_finance_storage_window(
            target,
            transition_evidence=transition_evidence,
            actor="finance_storage_cutover_runner",
            reason="atomic Finance split final cutover",
            window_id=window_id,
            window_kind="final_cutover",
            fingerprint=fingerprint,
            approval_reference=approval_reference,
        )
        try:
            payload = _run_remote_finance_storage_split_action(
                target,
                action=action,
                plan_path=plan_path,
                fingerprint=fingerprint,
                approval_reference=approval_reference,
                chunk_size=int(
                    getattr(args, "chunk_size", 10_000) or 10_000
                ),
                source_snapshot_manifest=source_snapshot_manifest,
                candidate_manifest=candidate_manifest,
                candidate_plan_fingerprint=candidate_plan_fingerprint,
                minimum_observation_seconds=minimum_observation_seconds,
            )
            transition_evidence["http_restart"] = (
                _restart_finance_cutover_http_service(target)
            )
        except Exception as exc:
            health = _run_remote_finance_storage_split_action(
                target,
                action="health",
                plan_path=None,
                fingerprint="",
                approval_reference="",
                chunk_size=int(
                    getattr(args, "chunk_size", 10_000) or 10_000
                ),
            )
            transition_evidence["failure_health_readback"] = health
            if str(health.get("canonical_source") or "") == "split":
                raise RuntimeError(
                    "Finance cutover failed after the split manifest became "
                    "canonical; the HTTP barrier and writer holds remain active "
                    f"for bounded recovery: {exc}"
                ) from exc
            transition_evidence.update(
                _restore_finance_storage_window(
                    target,
                    hold=hold,
                    window_id=window_id,
                    fingerprint=fingerprint,
                    reason="Finance cutover failed before manifest switch",
                )
            )
            raise RuntimeError(
                "Finance cutover failed before the manifest switch; exact "
                f"controls were restored: {exc}"
            ) from exc
        transition_evidence.update(
            _restore_finance_storage_window(
                target,
                hold=hold,
                window_id=window_id,
                fingerprint=fingerprint,
                reason="Finance split cutover and HTTP restart completed",
            )
        )
    elif action == "snapshot-create":
        if plan_path is None or not plan_path.is_file():
            raise ValueError(
                "Finance storage coherent snapshot requires --plan-file"
            )
        reviewed_plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if (
            not isinstance(reviewed_plan, dict)
            or str(reviewed_plan.get("contract_version") or "")
            != "wb_core_finance_storage_snapshot_plan_v1"
            or str(reviewed_plan.get("fingerprint") or "") != fingerprint
            or not bool(
                reviewed_plan.get("snapshot_allowed_by_machine_preflight")
            )
        ):
            raise ValueError(
                "Finance storage coherent snapshot plan is not ready"
            )
        window_id = str(
            (reviewed_plan.get("target_snapshot") or {}).get("window_id")
            or ""
        )
        transition_evidence["barrier_acquire"] = (
            _run_remote_business_data_maintenance_runner(
                target,
                action="barrier-acquire",
                actor="finance_storage_snapshot_runner",
                reason="coherent Finance migration source snapshot",
                window_id=window_id,
                window_kind="snapshot",
                plan_fingerprint=fingerprint,
                approval_reference=approval_reference,
            )
        )
        try:
            transition_evidence["core_prepare"] = (
                _run_remote_business_data_maintenance_runner(
                    target,
                    action="prepare",
                    actor="finance_storage_snapshot_runner",
                    reason="coherent Finance migration source snapshot",
                )
            )
            transition_evidence["warehouse_hold"] = (
                _run_remote_warehouse_functional_maintenance_action(
                    target,
                    action="hold",
                    disable_timer=True,
                )
            )
            hold = _run_remote_business_data_maintenance_runner(
                target,
                action="hold",
                actor="finance_storage_snapshot_runner",
                reason="coherent Finance migration source snapshot",
            )
            transition_evidence["business_hold"] = hold
            if (
                str(hold.get("status") or "") != "held"
                or hold.get("quiet") is not True
            ):
                raise RuntimeError(
                    "Finance snapshot writer/timer hold readback is incomplete"
                )
        except Exception as hold_error:
            try:
                transition_evidence["acquire_abort"] = (
                    _abort_finance_storage_window_acquire(
                        target,
                        window_id=window_id,
                        fingerprint=fingerprint,
                        reason=(
                            "Finance snapshot drain failed before hold "
                            "confirmation"
                        ),
                    )
                )
            except Exception as recovery_error:
                raise RuntimeError(
                    "Finance snapshot drain failed before hold confirmation; "
                    "the barrier remains active because exact abort restore "
                    f"also failed: hold={hold_error}; restore={recovery_error}"
                ) from recovery_error
            raise RuntimeError(
                "Finance snapshot drain failed before hold confirmation; "
                f"exact controls and barrier were restored: {hold_error}"
            ) from hold_error
        transition_evidence["barrier_confirm"] = (
            _run_remote_business_data_maintenance_runner(
                target,
                action="barrier-confirm",
                window_id=window_id,
                plan_fingerprint=fingerprint,
            )
        )
        snapshot_error: Exception | None = None
        try:
            payload = _run_remote_finance_storage_split_action(
                target,
                action=action,
                plan_path=plan_path,
                fingerprint=fingerprint,
                approval_reference=approval_reference,
                chunk_size=int(
                    getattr(args, "chunk_size", 10_000) or 10_000
                ),
                source_snapshot_manifest=source_snapshot_manifest,
                candidate_manifest=candidate_manifest,
                candidate_plan_fingerprint=candidate_plan_fingerprint,
                minimum_observation_seconds=minimum_observation_seconds,
            )
        except Exception as exc:
            snapshot_error = exc
            payload = {}
        transition_evidence["barrier_restoring"] = (
            _run_remote_business_data_maintenance_runner(
                target,
                action="barrier-restoring",
                window_id=window_id,
                plan_fingerprint=fingerprint,
            )
        )
        paused_revision = int(
            ((hold.get("auto_updates") or {}).get("revision") or 0)
        )
        if paused_revision <= 0:
            raise RuntimeError(
                "Finance snapshot hold lacks exact paused policy revision"
            )
        restore = _run_remote_business_data_maintenance_runner(
            target,
            action="restore",
            expected_revision=paused_revision,
            actor="finance_storage_snapshot_runner",
            reason="coherent Finance snapshot completed",
        )
        transition_evidence["business_restore"] = restore
        if (
            str(restore.get("status") or "") != "restored"
            or restore.get("exact_prior_state_restored") is not True
        ):
            raise RuntimeError(
                "Finance snapshot exact writer/timer restore is incomplete"
            )
        transition_evidence["warehouse_restore"] = (
            _run_remote_warehouse_functional_maintenance_action(
                target,
                action="restore",
            )
        )
        transition_evidence["barrier_release"] = (
            _run_remote_business_data_maintenance_runner(
                target,
                action="barrier-release",
                actor="finance_storage_snapshot_runner",
                reason="exact pre-snapshot control state restored",
                window_id=window_id,
                plan_fingerprint=fingerprint,
            )
        )
        if snapshot_error is not None:
            raise RuntimeError(
                "Finance coherent snapshot failed after exact controls were "
                f"restored: {snapshot_error}"
            ) from snapshot_error
    else:
        payload = _run_remote_finance_storage_split_action(
            target,
            action=action,
            plan_path=plan_path,
            fingerprint=fingerprint,
            approval_reference=approval_reference,
            chunk_size=int(
                getattr(args, "chunk_size", 10_000) or 10_000
            ),
            source_snapshot_manifest=source_snapshot_manifest,
            candidate_manifest=candidate_manifest,
            candidate_plan_fingerprint=candidate_plan_fingerprint,
            minimum_observation_seconds=minimum_observation_seconds,
        )
    output = str(getattr(args, "output", "") or "").strip()
    if output:
        output_path = Path(output).resolve()
        if output_path == ROOT or ROOT in output_path.parents:
            raise ValueError("Finance storage evidence output must stay outside the Git checkout")
        _write_private_json(output_path, payload)
    _print_json(
        {
            "target_id": target.target_id,
            "ssh_destination": target.ssh_destination,
            "runtime_dir": str(target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""),
            "action": f"finance-storage-split-{action}",
            "result": payload,
            "maintenance_transition_evidence": transition_evidence,
        }
    )
    return 0


def _run_remote_finance_storage_split_action(
    target: HostedRuntimeTarget,
    *,
    action: str,
    plan_path: Path | None,
    fingerprint: str,
    approval_reference: str,
    chunk_size: int,
    source_snapshot_manifest: str = "",
    candidate_manifest: str = "",
    candidate_plan_fingerprint: str = "",
    minimum_observation_seconds: int = 3600,
    rollback_candidate_evidence: str = "",
) -> dict[str, Any]:
    _ensure_active_hosted_runtime_target(
        target, action=f"finance-storage-split-{action}"
    )
    if action not in {
        "dry-run",
        "health",
        "apply",
        "snapshot-plan",
        "snapshot-create",
        "snapshot-integrity",
        "shadow-status",
        "shadow-activate",
        "shadow-reconcile",
        "shadow-verify",
        "live-tail-apply",
        "shadow-deactivate",
        "cutover-plan",
        "cutover-apply",
        "rollback-plan",
        "rollback-prepare",
        "rollback-apply",
    }:
        raise ValueError(f"unsupported Finance storage split action: {action}")
    if action in {
        "apply",
        "snapshot-create",
        "snapshot-integrity",
        "shadow-activate",
        "shadow-reconcile",
        "shadow-verify",
        "live-tail-apply",
        "shadow-deactivate",
        "cutover-apply",
        "rollback-prepare",
        "rollback-apply",
    }:
        _ensure_target_allows_mutation(
            target,
            action=f"finance-storage-split-{action}",
            dry_run=False,
        )
    runtime_dir = str(target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or "").strip()
    if runtime_dir != ACTIVE_HOSTED_RUNTIME_RUNTIME_DIR:
        raise ValueError("Finance storage split runner requires the canonical active runtime dir")
    if chunk_size <= 0 or chunk_size > 500_000:
        raise ValueError("Finance storage split chunk size must be within 1..500000")
    runner_args = [
        "python3",
        "apps/finance_storage_split.py",
        action,
        "--runtime-dir",
        runtime_dir,
        "--repo-root",
        target.target_dir,
        "--deployed-sha-file",
        f"{target.target_dir.rstrip('/')}/.wb-core-runtime-sha",
        "--chunk-size",
        str(chunk_size),
    ]
    reviewed_plan_json: str | None = None
    if source_snapshot_manifest:
        runner_args.extend(
            [
                "--source-snapshot-manifest",
                source_snapshot_manifest,
            ]
        )
    if action.startswith("shadow-") or action in {
        "live-tail-apply",
        "cutover-plan",
        "cutover-apply",
    }:
        if not candidate_manifest:
            raise ValueError(
                f"Finance storage {action} requires --candidate-manifest"
            )
        runner_args.extend(
            [
                "--candidate-manifest",
                candidate_manifest,
            ]
        )
    if action in {"cutover-plan", "cutover-apply"}:
        if not candidate_plan_fingerprint.startswith("sha256:"):
            raise ValueError(
                f"Finance storage {action} requires "
                "--candidate-plan-fingerprint"
            )
        runner_args.extend(
            [
                "--candidate-plan-fingerprint",
                candidate_plan_fingerprint,
            ]
        )
    if action == "apply":
        if plan_path is None or not plan_path.is_file():
            raise ValueError("Finance storage split apply requires an existing --plan-file")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if (
            not isinstance(plan, dict)
            or str(plan.get("contract_version") or "")
            != "wb_core_finance_storage_split_plan_v1"
            or str(plan.get("fingerprint") or "") != fingerprint
            or str(plan.get("mode") or "") != "dry_run"
            or not bool(plan.get("apply_allowed_by_machine_preflight"))
        ):
            raise ValueError("Finance storage reviewed plan is not ready for apply")
        if not approval_reference.strip():
            raise ValueError("Finance storage split apply requires --approval-reference")
        runner_args.extend(
            [
                "--confirm-fingerprint",
                fingerprint,
                "--approval-reference",
                approval_reference.strip(),
            ]
        )
    elif action == "snapshot-create":
        if plan_path is None or not plan_path.is_file():
            raise ValueError(
                "Finance storage snapshot-create requires an existing --plan-file"
            )
        reviewed_plan_json = plan_path.read_text(encoding="utf-8")
        plan = json.loads(reviewed_plan_json)
        if (
            not isinstance(plan, dict)
            or str(plan.get("contract_version") or "")
            != "wb_core_finance_storage_snapshot_plan_v1"
            or str(plan.get("fingerprint") or "") != fingerprint
            or str(plan.get("mode") or "") != "snapshot_dry_run"
            or not bool(plan.get("snapshot_allowed_by_machine_preflight"))
        ):
            raise ValueError(
                "Finance storage coherent snapshot reviewed plan is invalid"
            )
        if not approval_reference.strip():
            raise ValueError(
                "Finance storage snapshot-create requires --approval-reference"
            )
        runner_args.extend(
            [
                "--snapshot-plan-file",
                "/dev/stdin",
                "--confirm-fingerprint",
                fingerprint,
                "--approval-reference",
                approval_reference.strip(),
            ]
        )
    elif action == "snapshot-integrity":
        if not source_snapshot_manifest:
            raise ValueError(
                "Finance storage snapshot-integrity requires "
                "--source-snapshot-manifest"
            )
    elif action in {
        "shadow-status",
        "shadow-activate",
        "shadow-reconcile",
        "shadow-verify",
        "live-tail-apply",
        "shadow-deactivate",
    }:
        if not fingerprint.startswith("sha256:"):
            raise ValueError(
                f"Finance storage {action} requires exact candidate fingerprint"
            )
        runner_args.extend(
            [
                "--confirm-fingerprint",
                fingerprint,
                "--approval-reference",
                approval_reference or "status-only",
            ]
        )
        if action == "shadow-deactivate":
            runner_args.extend(
                ["--reason", "repo-owned shadow lifecycle transition"]
            )
        elif action == "shadow-verify":
            if minimum_observation_seconds < 0:
                raise ValueError(
                    "minimum observation seconds cannot be negative"
                )
            runner_args.extend(
                [
                    "--minimum-observation-seconds",
                    str(minimum_observation_seconds),
                ]
            )
    elif action == "cutover-apply":
        if plan_path is None or not plan_path.is_file():
            raise ValueError(
                "Finance storage cutover-apply requires --plan-file"
            )
        reviewed_plan_json = plan_path.read_text(encoding="utf-8")
        plan = json.loads(reviewed_plan_json)
        if (
            not isinstance(plan, dict)
            or str(plan.get("contract_version") or "")
            != "wb_core_finance_storage_cutover_plan_v1"
            or str(plan.get("fingerprint") or "") != fingerprint
            or str(plan.get("candidate_plan_fingerprint") or "")
            != candidate_plan_fingerprint
            or not bool(plan.get("apply_allowed_by_machine_preflight"))
        ):
            raise ValueError(
                "Finance storage cutover reviewed plan is invalid"
            )
        if not approval_reference.strip():
            raise ValueError(
                "Finance storage cutover requires --approval-reference"
            )
        runner_args.extend(
            [
                "--cutover-plan-file",
                "/dev/stdin",
                "--confirm-fingerprint",
                fingerprint,
                "--approval-reference",
                approval_reference.strip(),
            ]
        )
    elif action in {"rollback-prepare", "rollback-apply"}:
        if plan_path is None or not plan_path.is_file():
            raise ValueError(
                f"Finance storage {action} requires --plan-file"
            )
        reviewed_plan_json = plan_path.read_text(encoding="utf-8")
        plan = json.loads(reviewed_plan_json)
        if (
            not isinstance(plan, dict)
            or str(plan.get("contract_version") or "")
            != "wb_core_finance_storage_rollback_plan_v1"
            or str(plan.get("fingerprint") or "") != fingerprint
            or not bool(
                plan.get("prepare_allowed_by_machine_preflight")
            )
        ):
            raise ValueError(
                "Finance storage rollback reviewed plan is invalid"
            )
        if not approval_reference.strip():
            raise ValueError(
                "Finance storage rollback requires --approval-reference"
            )
        runner_args.extend(
            [
                "--rollback-plan-file",
                "/dev/stdin",
                "--confirm-fingerprint",
                fingerprint,
                "--approval-reference",
                approval_reference.strip(),
            ]
        )
        if action == "rollback-apply":
            if not rollback_candidate_evidence:
                raise ValueError(
                    "Finance rollback apply requires candidate evidence"
                )
            runner_args.extend(
                [
                    "--rollback-candidate-evidence",
                    rollback_candidate_evidence,
                ]
            )
    shell_command = " && ".join(
        [
            f"cd {shlex.quote(target.target_dir)}",
            " ".join(shlex.quote(item) for item in runner_args),
        ]
    )
    result = subprocess.run(
        _remote_shell_command(target, shell_command),
        text=True,
        capture_output=True,
        input=reviewed_plan_json,
        cwd=ROOT,
        timeout=(
            FINANCE_STORAGE_SPLIT_MUTATION_TIMEOUT_SECONDS
            if action in {
                "apply",
                "snapshot-create",
                "shadow-reconcile",
                "shadow-verify",
                "live-tail-apply",
                "cutover-apply",
                "rollback-prepare",
                "rollback-apply",
            }
            else FINANCE_STORAGE_SPLIT_READ_TIMEOUT_SECONDS
        ),
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Finance storage split {action} failed: "
            + (
                result.stderr.strip()
                or result.stdout.strip()
                or f"exit {result.returncode}"
            )
        )
    stdout_lines = [line for line in result.stdout.splitlines() if line.strip()]
    try:
        payload = json.loads(stdout_lines[-1] if stdout_lines else "")
    except json.JSONDecodeError as exc:
        raise RuntimeError("Finance storage split runner returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Finance storage split runner returned non-object JSON")
    if action in {"dry-run", "health", "snapshot-plan"}:
        if action == "dry-run" and (
            payload.get("query_only_contract", {}).get("production_mutation_count") != 0
            or payload.get("query_only_contract", {}).get("destination_bytes_created") != 0
        ):
            raise RuntimeError("Finance storage dry-run did not prove zero mutation/bytes")
    elif action == "apply" and bool(payload.get("global_manifest_switched")):
        raise RuntimeError("candidate builder unexpectedly switched the global manifest")
    elif action == "cutover-apply" and not bool(
        payload.get("global_manifest_switched")
    ):
        raise RuntimeError(
            "Finance cutover did not switch the global split manifest"
        )
    elif action == "rollback-apply" and (
        not bool(payload.get("global_manifest_switched"))
        or str(payload.get("canonical_source") or "") != "monolith"
    ):
        raise RuntimeError(
            "Finance rollback did not switch the canonical monolith manifest"
        )
    return payload


def run_partner_finance_diagnostic_command(args: argparse.Namespace) -> int:
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    payload = _run_remote_partner_finance_diagnostic(
        target,
        nm_id=str(getattr(args, "nm_id", "") or "").strip(),
        weeks=tuple(str(value).strip() for value in (args.week or []) if str(value).strip()),
    )
    output_path = Path(str(args.output)).resolve()
    if output_path == ROOT or ROOT in output_path.parents:
        raise ValueError("Partner/Finance diagnostic evidence must stay outside the Git checkout")
    _write_private_json(output_path, payload)
    _print_json(
        {
            "target_id": target.target_id,
            "ssh_destination": target.ssh_destination,
            "runtime_dir": str(target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""),
            "action": "partner-finance-diagnostic",
            "result": payload,
        }
    )
    return 0


def _run_remote_partner_finance_diagnostic(
    target: HostedRuntimeTarget,
    *,
    nm_id: str,
    weeks: tuple[str, ...],
) -> dict[str, Any]:
    _ensure_active_hosted_runtime_target(target, action="partner-finance-diagnostic")
    runtime_dir = str(target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or "").strip()
    if runtime_dir != ACTIVE_HOSTED_RUNTIME_RUNTIME_DIR:
        raise ValueError("Partner/Finance diagnostic requires the canonical active runtime dir")
    if not target.environment_file:
        raise ValueError("Partner/Finance diagnostic requires the hosted environment file")
    runner_args = [
        "python3",
        "apps/partner_finance_production_diagnostic.py",
        "--runtime-dir",
        runtime_dir,
        "--env-file",
        target.environment_file,
        "--server-settings",
        "--max-weeks",
        "64",
        "--max-groups",
        "500",
        "--max-examples",
        "5",
    ]
    if nm_id:
        runner_args.extend(["--nm-id", nm_id])
    for week in weeks:
        runner_args.extend(["--week", week])
    shell_command = " && ".join(
        [
            f"cd {shlex.quote(target.target_dir)}",
            " ".join(shlex.quote(item) for item in runner_args),
        ]
    )
    result = subprocess.run(
        _remote_shell_command(target, shell_command),
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=PARTNER_FINANCE_DIAGNOSTIC_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode not in {0, 3}:
        raise RuntimeError(
            "Partner/Finance diagnostic failed: "
            + (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Partner/Finance diagnostic returned invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("status") not in {"ready", "incomplete"}:
        raise RuntimeError("Partner/Finance diagnostic returned an invalid result")
    return payload


def run_ads_historical_recovery_command(args: argparse.Namespace) -> int:
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    action = str(args.ads_historical_action)
    nm_ids = tuple(sorted({int(value) for value in args.nm_id}))
    target_dates = tuple(sorted({str(value).strip() for value in args.target_date}))
    plan_path = Path(str(args.plan_file)).resolve() if action == "apply" else None
    if plan_path is not None and (plan_path == ROOT or ROOT in plan_path.parents):
        raise ValueError("ads historical reviewed plan must stay outside the Git checkout")
    payload = _run_remote_ads_historical_recovery(
        target,
        action=action,
        nm_ids=nm_ids,
        target_dates=target_dates,
        plan_path=plan_path,
        fingerprint=str(getattr(args, "fingerprint", "") or ""),
        approval_reference=str(getattr(args, "approval_reference", "") or ""),
    )
    output = str(getattr(args, "output", "") or "").strip()
    if output:
        output_path = Path(output).resolve()
        if output_path == ROOT or ROOT in output_path.parents:
            raise ValueError("ads historical evidence output must stay outside the Git checkout")
        _write_private_json(output_path, payload)
    _print_json(
        {
            "target_id": target.target_id,
            "ssh_destination": target.ssh_destination,
            "runtime_dir": str(target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""),
            "action": f"ads-historical-{action}",
            "result": payload,
        }
    )
    return 0


def run_vitrina_incident_rematerialization_command(
    args: argparse.Namespace,
) -> int:
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    action = str(args.vitrina_incident_action)
    plan_path = (
        Path(str(args.plan_file)).resolve()
        if action == "apply"
        else None
    )
    if plan_path is not None and (plan_path == ROOT or ROOT in plan_path.parents):
        raise ValueError(
            "Vitrina incident reviewed plan must stay outside the Git checkout"
        )
    payload = _run_remote_vitrina_incident_rematerialization(
        target,
        action=action,
        date_from=str(args.date_from or ""),
        date_to=str(args.date_to or ""),
        max_dates=int(args.max_dates),
        plan_path=plan_path,
        fingerprint=str(args.fingerprint or ""),
        approval_reference=str(args.approval_reference or ""),
        actor=str(args.actor or ""),
    )
    output = str(args.output or "").strip()
    if action == "dry-run" and output:
        output_path = Path(output).resolve()
        if output_path == ROOT or ROOT in output_path.parents:
            raise ValueError(
                "Vitrina incident reviewed plan must stay outside the Git checkout"
            )
        _write_private_json(output_path, payload)
    _print_json(
        {
            "target_id": target.target_id,
            "ssh_destination": target.ssh_destination,
            "runtime_dir": str(
                target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""
            ),
            "action": f"vitrina-incident-rematerialization-{action}",
            "result": payload,
        }
    )
    return 0


def _run_remote_vitrina_incident_rematerialization(
    target: HostedRuntimeTarget,
    *,
    action: str,
    date_from: str,
    date_to: str,
    max_dates: int,
    plan_path: Path | None,
    fingerprint: str,
    approval_reference: str,
    actor: str,
) -> dict[str, Any]:
    _ensure_active_hosted_runtime_target(
        target,
        action=f"vitrina-incident-rematerialization-{action}",
    )
    if action not in {"dry-run", "apply"}:
        raise ValueError(
            f"unsupported Vitrina incident rematerialization action: {action}"
        )
    if action == "apply":
        _ensure_target_allows_mutation(
            target,
            action="vitrina-incident-rematerialization-apply",
            dry_run=False,
        )
    try:
        normalized_from = date.fromisoformat(date_from).isoformat()
        normalized_to = date.fromisoformat(date_to).isoformat()
    except ValueError as exc:
        raise ValueError(
            "Vitrina incident rematerialization dates must be YYYY-MM-DD"
        ) from exc
    if normalized_from > normalized_to:
        raise ValueError(
            "Vitrina incident rematerialization date_from cannot exceed date_to"
        )
    bounded_max_dates = int(max_dates)
    if not 1 <= bounded_max_dates <= 14:
        raise ValueError(
            "Vitrina incident rematerialization max_dates must be within 1..14"
        )
    runtime_dir = str(
        target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""
    ).strip()
    if runtime_dir != ACTIVE_HOSTED_RUNTIME_RUNTIME_DIR:
        raise ValueError(
            "Vitrina incident rematerialization requires the canonical active runtime dir"
        )
    seller_id = str(
        target.runtime_env.get("SELLER_PORTAL_CANONICAL_SUPPLIER_ID") or ""
    ).strip()
    if not seller_id:
        raise ValueError(
            "Vitrina incident rematerialization requires the target-owned canonical seller ID"
        )
    runner_args = [
        "python3",
        "apps/vitrina_incident_rematerialization.py",
        "--runtime-dir",
        runtime_dir,
        "--seller-id",
        seller_id,
    ]
    reviewed_plan_json = ""
    if action == "dry-run":
        runner_args.extend(
            [
                "dry-run",
                "--date-from",
                normalized_from,
                "--date-to",
                normalized_to,
                "--max-dates",
                str(bounded_max_dates),
                "--stdout-plan",
            ]
        )
    else:
        if plan_path is None or not plan_path.is_file():
            raise ValueError(
                "Vitrina incident rematerialization apply requires --plan-file"
            )
        reviewed_plan_json = plan_path.read_text(encoding="utf-8")
        reviewed_plan = json.loads(reviewed_plan_json)
        if not isinstance(reviewed_plan, dict):
            raise ValueError(
                "Vitrina incident rematerialization plan must be an object"
            )
        if (
            reviewed_plan.get("contract_name")
            != "vitrina_incident_rematerialization"
            or int(reviewed_plan.get("contract_version") or 0) != 1
            or reviewed_plan.get("mode") != "dry_run"
            or not reviewed_plan.get("apply_allowed")
            or reviewed_plan.get("date_from_requested") != normalized_from
            or reviewed_plan.get("date_to") != normalized_to
            or int(reviewed_plan.get("max_dates") or 0) != bounded_max_dates
            or str(reviewed_plan.get("fingerprint") or "") != fingerprint
        ):
            raise ValueError(
                "Vitrina incident rematerialization plan does not match the exact apply scope"
            )
        if not approval_reference.strip():
            raise ValueError(
                "Vitrina incident rematerialization apply requires --approval-reference"
            )
        if not actor.strip():
            raise ValueError(
                "Vitrina incident rematerialization apply requires --actor"
            )
        runner_args.extend(
            [
                "apply",
                "--reviewed-plan-stdin",
                "--fingerprint",
                fingerprint,
                "--approval-reference",
                approval_reference.strip(),
                "--actor",
                actor.strip(),
            ]
        )
    shell_command = " && ".join(
        [
            f"cd {shlex.quote(target.target_dir)}",
            " ".join(shlex.quote(item) for item in runner_args),
        ]
    )
    result = subprocess.run(
        _remote_shell_command(target, shell_command),
        text=True,
        capture_output=True,
        input=reviewed_plan_json if action == "apply" else None,
        cwd=ROOT,
        timeout=VITRINA_INCIDENT_REMATERIALIZATION_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Vitrina incident rematerialization {action} failed: "
            + (
                result.stderr.strip()
                or result.stdout.strip()
                or f"exit {result.returncode}"
            )
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Vitrina incident rematerialization runner returned invalid JSON"
        ) from exc
    if not isinstance(payload, dict) or payload.get("status") == "error":
        raise RuntimeError(
            "Vitrina incident rematerialization runner returned an invalid result"
        )
    return payload


def _run_remote_ads_historical_recovery(
    target: HostedRuntimeTarget,
    *,
    action: str,
    nm_ids: tuple[int, ...],
    target_dates: tuple[str, ...],
    plan_path: Path | None,
    fingerprint: str,
    approval_reference: str,
) -> dict[str, Any]:
    _ensure_active_hosted_runtime_target(target, action=f"ads-historical-{action}")
    if action == "apply":
        _ensure_target_allows_mutation(target, action="ads-historical-apply", dry_run=False)
    if action not in {"dry-run", "apply", "readback"}:
        raise ValueError(f"unsupported ads historical action: {action}")
    if not nm_ids or any(value <= 0 for value in nm_ids):
        raise ValueError("ads historical recovery requires exact positive --nm-id values")
    if not target_dates:
        raise ValueError("ads historical recovery requires exact --target-date values")
    for value in target_dates:
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"invalid ads historical target date: {value}") from exc
    runtime_dir = str(target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or "").strip()
    if runtime_dir != ACTIVE_HOSTED_RUNTIME_RUNTIME_DIR:
        raise ValueError("ads historical recovery requires the canonical active runtime dir")
    if not target.environment_file:
        raise ValueError("ads historical recovery requires the hosted environment file")
    runner_args = [
        "python3",
        "apps/ads_historical_recovery.py",
        "--runtime-dir",
        runtime_dir,
        "--env-file",
        target.environment_file,
    ]
    reviewed_plan_json = ""
    for nm_id in nm_ids:
        runner_args.extend(["--nm-id", str(nm_id)])
    for target_date in target_dates:
        runner_args.extend(["--target-date", target_date])
    if action == "readback":
        runner_args.append("--readback")
    elif action == "apply":
        if plan_path is None or not plan_path.is_file():
            raise ValueError("ads historical apply requires an existing --plan-file")
        reviewed_plan_json = plan_path.read_text(encoding="utf-8")
        plan = json.loads(reviewed_plan_json)
        expected_scope = {
            "nm_ids": list(nm_ids),
            "target_dates": list(target_dates),
        }
        if not isinstance(plan, dict) or str(plan.get("fingerprint") or "") != fingerprint:
            raise ValueError("ads historical plan and --fingerprint do not match")
        if (
            str(plan.get("schema_version") or "") != "ads_historical_recovery_v4"
            or plan.get("dry_run") is not True
            or not bool(plan.get("apply_allowed"))
            or plan.get("scope") != expected_scope
        ):
            raise ValueError("ads historical reviewed plan is not ready for this exact scope")
        if not approval_reference.strip():
            raise ValueError("ads historical apply requires --approval-reference")
        runner_args.extend(
            [
                "--apply",
                "--confirm-fingerprint",
                fingerprint,
                "--backup-dir",
                "/opt/wb-core-runtime/backups/ads-historical",
                "--approval-reference",
                approval_reference.strip(),
                "--reviewed-plan-stdin",
            ]
        )
    shell_command = " && ".join(
        [
            f"cd {shlex.quote(target.target_dir)}",
            " ".join(shlex.quote(item) for item in runner_args),
        ]
    )
    result = subprocess.run(
        _remote_shell_command(target, shell_command),
        text=True,
        capture_output=True,
        input=reviewed_plan_json if action == "apply" else None,
        cwd=ROOT,
        timeout=ADS_HISTORICAL_RECOVERY_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ads historical {action} failed: "
            + (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("ads historical runner returned invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("status") == "error":
        raise RuntimeError("ads historical runner returned an invalid result")
    if action == "readback" and (
        payload.get("status") != "ready" or bool(payload.get("blockers"))
    ):
        raise RuntimeError("ads historical readback has blockers")
    return payload


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def run_warehouse_archival_estimate_command(args: argparse.Namespace) -> int:
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    action = str(args.warehouse_archival_estimate_action)
    plan_path = Path(str(args.plan_file)).resolve() if action == "apply" else None
    if plan_path is not None and (plan_path == ROOT or ROOT in plan_path.parents):
        raise ValueError("archival estimate reviewed plan must stay outside the Git checkout")
    payload = _run_remote_warehouse_archival_estimate_action(
        target,
        action=action,
        plan_path=plan_path,
        fingerprint=str(getattr(args, "fingerprint", "") or ""),
        approval_reference=str(getattr(args, "approval_reference", "") or ""),
        reason=str(getattr(args, "reason", "") or ""),
    )
    output = str(getattr(args, "output", "") or "").strip()
    if action == "dry-run" and output:
        output_path = Path(output).resolve()
        if output_path == ROOT or ROOT in output_path.parents:
            raise ValueError("archival estimate evidence must stay outside the Git checkout")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        output_path.chmod(0o600)
    _print_json(
        {
            "target_id": target.target_id,
            "ssh_destination": target.ssh_destination,
            "runtime_dir": str(target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""),
            "action": action,
            "result": payload,
        }
    )
    return 0


def _run_remote_warehouse_archival_estimate_action(
    target: HostedRuntimeTarget,
    *,
    action: str,
    plan_path: Path | None,
    fingerprint: str,
    approval_reference: str,
    reason: str,
) -> dict[str, Any]:
    _ensure_active_hosted_runtime_target(
        target,
        action=f"warehouse-archival-estimate-{action}",
    )
    if action in {"apply", "rollback"}:
        _ensure_target_allows_mutation(
            target,
            action=f"warehouse-archival-estimate-{action}",
            dry_run=False,
        )
    if action not in {"dry-run", "apply", "readback", "rollback"}:
        raise ValueError(f"unsupported archival estimate action: {action}")
    runtime_dir = str(target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or "").strip()
    if runtime_dir != ACTIVE_HOSTED_RUNTIME_RUNTIME_DIR:
        raise ValueError("archival estimate runner requires the canonical active runtime dir")
    runner_args = [
        "python3",
        "apps/warehouse_archival_estimate.py",
        action,
        "--runtime-dir",
        runtime_dir,
    ]
    stdin_text: str | None = None
    if action == "apply":
        if plan_path is None or not plan_path.is_file():
            raise ValueError("archival estimate apply requires --plan-file")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if (
            not isinstance(plan, dict)
            or str(plan.get("plan_fingerprint") or "") != fingerprint
            or str(plan.get("contract_name") or "")
            != "warehouse_business_approved_archival_estimate"
            or not bool(plan.get("apply_allowed"))
        ):
            raise ValueError("archival estimate reviewed plan is not ready for apply")
        if not approval_reference.strip():
            raise ValueError("archival estimate apply requires --approval-reference")
        stdin_text = json.dumps(plan, ensure_ascii=False, sort_keys=True)
        runner_args.extend(
            [
                "--plan-file",
                "/dev/stdin",
                "--fingerprint",
                fingerprint,
                "--approval-reference",
                approval_reference.strip(),
                "--backup-dir",
                "/opt/wb-core-runtime/backups/warehouse-archival-estimate",
            ]
        )
    elif action == "rollback":
        if not fingerprint or not reason.strip():
            raise ValueError("archival estimate rollback requires fingerprint and reason")
        runner_args.extend(
            [
                "--fingerprint",
                fingerprint,
                "--reason",
                reason.strip(),
                "--backup-dir",
                "/opt/wb-core-runtime/backups/warehouse-archival-estimate",
            ]
        )
    command = " && ".join(
        [
            f"cd {shlex.quote(target.target_dir)}",
            " ".join(shlex.quote(item) for item in runner_args),
        ]
    )
    result = subprocess.run(
        _remote_shell_command(target, command),
        text=True,
        input=stdin_text,
        capture_output=True,
        cwd=ROOT,
        timeout=(
            WAREHOUSE_OPENING_MUTATION_TIMEOUT_SECONDS
            if action in {"apply", "rollback"}
            else FINANCE_CANONICAL_READ_TIMEOUT_SECONDS
        ),
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"archival estimate {action} failed: "
            + (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("archival estimate runner returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("archival estimate runner returned a non-object JSON payload")
    return payload


def _run_remote_warehouse_functional_action(
    target: HostedRuntimeTarget,
    *,
    action: str,
    plan_path: Path | None = None,
    fingerprint: str = "",
    reason: str = "",
) -> dict[str, Any]:
    _ensure_active_hosted_runtime_target(target, action=f"warehouse-functional-{action}")
    mutation_actions = {
        "cutover-apply",
        "sync-apply",
        "emergency-apply",
        "economics-backfill-apply",
        "supplier-certification-apply",
        "supplier-certification-rollback",
        "rollback",
        "backup",
        "hourly-sync",
        "manual-sync",
        "enable-hourly",
    }
    if action in mutation_actions:
        _ensure_target_allows_mutation(target, action=f"warehouse-functional-{action}", dry_run=False)
    runtime_dir = str(target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or "").strip()
    if runtime_dir != ACTIVE_HOSTED_RUNTIME_RUNTIME_DIR:
        raise ValueError("warehouse functional runner requires the canonical active runtime dir")
    if not target.environment_file:
        raise ValueError("warehouse functional runner requires the hosted environment file")
    warehouse_sync_backup_dir = str(
        Path(runtime_dir) / "backups" / "warehouse-functional-sync"
    )

    if action == "enable-hourly":
        readback = _run_remote_warehouse_functional_action(target, action="readback")
        if str(readback.get("status") or "") != "ready":
            raise RuntimeError("hourly timer cannot be enabled before successful functional cutover readback")
        if str((readback.get("active_version") or {}).get("version_kind") or "") != "hourly_wb_sync":
            raise RuntimeError("hourly timer cannot be enabled before one successful bounded WB sync")
        economics = _run_remote_warehouse_functional_action(
            target,
            action="economics-backfill-dry-run",
        )
        if int(economics.get("changed_snapshot_count") or 0) != 0:
            raise RuntimeError("hourly timer cannot be enabled while functional economics publication is pending")
        unit = "wb-core-warehouse-functional-sync.timer"
        command = " && ".join(
            [
                f"systemctl enable --now {shlex.quote(unit)}",
                f"systemctl is-enabled {shlex.quote(unit)}",
                f"systemctl is-active {shlex.quote(unit)}",
            ]
        )
        result = subprocess.run(
            _remote_shell_command(target, command),
            text=True,
            capture_output=True,
            cwd=ROOT,
            timeout=WAREHOUSE_OPENING_READ_TIMEOUT_SECONDS,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("hourly timer enable failed: " + (result.stderr.strip() or result.stdout.strip()))
        return {
            "status": "enabled",
            "unit": unit,
            "systemctl": result.stdout.strip().splitlines(),
            "active_version_id": (readback.get("active_version") or {}).get("version_id"),
            "economics_plan_fingerprint": economics.get("plan_fingerprint"),
        }

    runner_args = [
        "python3",
        "apps/warehouse_functional_runner.py",
        "--runtime-dir",
        runtime_dir,
        "--env-file",
        target.environment_file,
        action,
    ]
    stdin_text: str | None = None
    if action in {
        "cutover-apply",
        "sync-apply",
        "emergency-apply",
        "economics-backfill-apply",
        "supplier-certification-apply",
    }:
        if plan_path is None:
            raise ValueError(f"warehouse functional {action} requires a plan path")
        stdin_text = plan_path.read_text(encoding="utf-8")
        plan = json.loads(stdin_text)
        if not isinstance(plan, dict) or str(plan.get("plan_fingerprint") or "") != fingerprint:
            raise ValueError("warehouse functional plan and --fingerprint do not match")
        runner_args.extend(["--plan-file", "/dev/stdin", "--fingerprint", fingerprint])
        if action == "cutover-apply":
            runner_args.extend(["--backup-dir", "/opt/wb-core-runtime/backups/warehouse-functional"])
        elif action == "sync-apply":
            runner_args.extend(
                [
                    "--backup-dir",
                    warehouse_sync_backup_dir,
                ]
            )
        elif action == "emergency-apply":
            runner_args.extend(
                [
                    "--backup-dir",
                    "/opt/wb-core-runtime/backups/warehouse-functional-recovery",
                ]
            )
        elif action == "economics-backfill-apply":
            runner_args.extend(["--backup-dir", "/opt/wb-core-runtime/backups/warehouse-functional-economics"])
        elif action == "supplier-certification-apply":
            runner_args.extend(
                [
                    "--backup-dir",
                    "/opt/wb-core-runtime/backups/warehouse-supplier-certification-replay",
                ]
            )
    elif action == "supplier-certification-rollback":
        if not str(reason or "").strip():
            raise ValueError("supplier certification rollback requires a reason")
        runner_args.extend(
            [
                "--fingerprint",
                fingerprint,
                "--reason",
                reason,
                "--backup-dir",
                "/opt/wb-core-runtime/backups/warehouse-supplier-certification-replay",
            ]
        )
    elif action == "rollback":
        runner_args.extend(
            [
                "--fingerprint",
                fingerprint,
                "--backup-dir",
                "/opt/wb-core-runtime/backups/warehouse-functional",
            ]
        )
    elif action == "backup":
        runner_args.extend(
            [
                "--backup-dir",
                warehouse_sync_backup_dir,
            ]
        )
    elif action == "manual-sync":
        runner_args.extend(
            [
                "--backup-dir",
                warehouse_sync_backup_dir,
            ]
        )

    prefix: list[str] = [f"cd {shlex.quote(target.target_dir)}"]
    if action == "rollback":
        prefix.append("systemctl disable --now wb-core-warehouse-functional-sync.timer || true")
    shell_command = " && ".join([*prefix, " ".join(shlex.quote(item) for item in runner_args)])
    result = subprocess.run(
        _remote_shell_command(target, shell_command),
        input=stdin_text,
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=(
            WAREHOUSE_OPENING_MUTATION_TIMEOUT_SECONDS
            if action in mutation_actions or action in WAREHOUSE_FUNCTIONAL_PLAN_ACTIONS
            else WAREHOUSE_OPENING_READ_TIMEOUT_SECONDS
        ),
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"warehouse functional {action} failed: "
            + (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("warehouse functional runner returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("warehouse functional runner returned a non-object JSON payload")
    return payload


def _run_remote_sqlite_backup_archive(
    target: HostedRuntimeTarget,
    *,
    apply: bool,
    source: str,
    fingerprint: str,
    reserved_free_bytes: int,
) -> dict[str, Any]:
    action = (
        "sqlite-backup-archive-apply"
        if apply
        else "sqlite-backup-archive-dry-run"
    )
    _ensure_active_hosted_runtime_target(target, action=action)
    if apply:
        _ensure_target_allows_mutation(
            target,
            action=action,
            dry_run=False,
        )
    runtime_dir = str(
        target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""
    ).strip()
    if runtime_dir != ACTIVE_HOSTED_RUNTIME_RUNTIME_DIR:
        raise ValueError(
            "SQLite backup archive requires the canonical active runtime dir"
        )
    source_path = Path(str(source))
    backup_root = (Path(runtime_dir) / "backups").resolve()
    if (
        not source_path.is_absolute()
        or source_path.resolve().parent != (
            backup_root / "warehouse-functional-sync"
        )
        or source_path.suffix != ".sqlite3"
    ):
        raise ValueError(
            "SQLite archive source must be one raw warehouse-functional-sync "
            "checkpoint in the canonical runtime backup directory"
        )
    runner_args = [
        "python3",
        "apps/sqlite_backup_archive.py",
        "--source",
        str(source_path),
        "--staging-directory",
        runtime_dir,
        "--reserved-free-bytes",
        str(int(reserved_free_bytes)),
    ]
    if apply:
        if not str(fingerprint or "").strip():
            raise ValueError("SQLite archive apply requires a fingerprint")
        runner_args.extend(
            ["--apply", "--fingerprint", str(fingerprint)]
        )
    command = " && ".join(
        [
            f"cd {shlex.quote(target.target_dir)}",
            " ".join(shlex.quote(item) for item in runner_args),
        ]
    )
    result = subprocess.run(
        _remote_shell_command(target, command),
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=WAREHOUSE_RECOVERY_LIFECYCLE_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{action} failed: "
            + (
                result.stderr.strip()
                or result.stdout.strip()
                or f"exit {result.returncode}"
            )
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "SQLite backup archive returned invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            "SQLite backup archive returned a non-object payload"
        )
    return payload


def _run_remote_warehouse_cost_queue_replay(
    target: HostedRuntimeTarget,
    *,
    apply: bool,
    invoice_numbers: list[str],
    plan_path: Path | None,
    fingerprint: str,
) -> dict[str, Any]:
    action = (
        "warehouse-cost-queue-replay-apply"
        if apply
        else "warehouse-cost-queue-replay-dry-run"
    )
    _ensure_active_hosted_runtime_target(target, action=action)
    if apply:
        _ensure_target_allows_mutation(
            target,
            action=action,
            dry_run=False,
        )
    runtime_dir = str(
        target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""
    ).strip()
    if runtime_dir != ACTIVE_HOSTED_RUNTIME_RUNTIME_DIR:
        raise ValueError(
            "warehouse cost queue replay requires the canonical active runtime dir"
        )
    normalized_invoices = sorted(
        {str(value).strip() for value in invoice_numbers if str(value).strip()}
    )
    if len(normalized_invoices) != len(invoice_numbers):
        raise ValueError(
            "warehouse cost queue replay invoices must be non-empty and unique"
        )
    runner_args = [
        "python3",
        "apps/warehouse_cost_queue_replay.py",
        "--runtime-dir",
        runtime_dir,
    ]
    for invoice in normalized_invoices:
        runner_args.extend(["--invoice-no", invoice])
    stdin_text: str | None = None
    if apply:
        if plan_path is None or not str(fingerprint or "").strip():
            raise ValueError(
                "warehouse cost queue replay apply requires plan and fingerprint"
            )
        stdin_text = plan_path.read_text(encoding="utf-8")
        plan = json.loads(stdin_text)
        if (
            not isinstance(plan, dict)
            or str(plan.get("fingerprint") or "") != fingerprint
        ):
            raise ValueError(
                "warehouse cost queue replay plan and fingerprint do not match"
            )
        runner_args.extend(
            [
                "--apply",
                "--plan-file",
                "/dev/stdin",
                "--fingerprint",
                fingerprint,
            ]
        )
    command = " && ".join(
        [
            f"cd {shlex.quote(target.target_dir)}",
            " ".join(shlex.quote(item) for item in runner_args),
        ]
    )
    result = subprocess.run(
        _remote_shell_command(target, command),
        input=stdin_text,
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=WAREHOUSE_RECOVERY_LIFECYCLE_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{action} failed: "
            + (
                result.stderr.strip()
                or result.stdout.strip()
                or f"exit {result.returncode}"
            )
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "warehouse cost queue replay returned invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            "warehouse cost queue replay returned a non-object payload"
        )
    return payload


def _run_remote_warehouse_functional_maintenance_action(
    target: HostedRuntimeTarget,
    *,
    action: str,
    disable_timer: bool = False,
    allow_outer_hold_recovery: bool = False,
) -> dict[str, Any]:
    """Inspect, hold or restore only the warehouse functional timer boundary."""

    _ensure_active_hosted_runtime_target(
        target, action=f"warehouse-functional-maintenance-{action}"
    )
    if action not in {"status", "hold", "restore"}:
        raise ValueError(f"unsupported warehouse maintenance action: {action}")
    if action in {"hold", "restore"}:
        _ensure_target_allows_mutation(
            target,
            action=f"warehouse-functional-maintenance-{action}",
            dry_run=False,
        )
    runtime_dir = str(
        target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""
    ).strip()
    if runtime_dir != ACTIVE_HOSTED_RUNTIME_RUNTIME_DIR:
        raise ValueError("warehouse maintenance requires the canonical active runtime dir")
    runner_args = [
        "python3",
        "apps/warehouse_functional_maintenance.py",
        action,
        "--runtime-dir",
        runtime_dir,
    ]
    if action == "hold":
        runner_args.extend(
            [
                "--wait-timeout-seconds",
                "1200",
                "--poll-interval-seconds",
                "2",
            ]
        )
        if disable_timer:
            runner_args.append("--disable-timer")
    elif action == "restore" and allow_outer_hold_recovery:
        runner_args.append("--allow-outer-hold-recovery")
    command = " && ".join(
        [
            f"cd {shlex.quote(target.target_dir)}",
            " ".join(shlex.quote(item) for item in runner_args),
        ]
    )
    result = subprocess.run(
        _remote_shell_command(target, command),
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=1500.0 if action == "hold" else 300.0,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"warehouse functional maintenance {action} failed: "
            + (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("warehouse maintenance runner returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("warehouse maintenance runner returned a non-object JSON payload")
    units = payload.get("units") or {}
    timer = units.get("timer") or {}
    service = units.get("service") or {}
    service_active = str(service.get("is_active") or "")
    if action == "hold" and (
        str(payload.get("status") or "") != "held"
        or str(timer.get("is_active") or "") != "inactive"
        or (disable_timer and str(timer.get("is_enabled") or "") != "disabled")
        or not warehouse_functional_service_is_quiescent(service_active)
        or service.get("quiescent") is not True
        or bool((payload.get("warehouse_lock") or {}).get("held"))
        or bool(payload.get("finance_apply_processes"))
    ):
        raise RuntimeError("warehouse maintenance hold readback is incomplete")
    if action == "restore" and str(payload.get("status") or "") != "restored":
        raise RuntimeError("warehouse maintenance restore readback is incomplete")
    return payload


def run_warehouse_functional_maintenance_command(args: argparse.Namespace) -> int:
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    payload = _run_remote_warehouse_functional_maintenance_action(
        target,
        action=str(args.action),
        allow_outer_hold_recovery=bool(
            args.allow_outer_hold_recovery
        ),
    )
    _print_json(
        {
            "target_id": target.target_id,
            "ssh_destination": target.ssh_destination,
            "runtime_dir": str(
                target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""
            ),
            "action": f"warehouse-functional-maintenance-{args.action}",
            "result": payload,
        }
    )
    return 0


def _run_remote_business_data_maintenance_runner(
    target: HostedRuntimeTarget,
    *,
    action: str,
    expected_revision: int | None = None,
    process_key: str = "",
    desired: str = "",
    actor: str = "repo_owned_cli",
    reason: str = "",
    window_id: str = "",
    window_kind: str = "snapshot",
    plan_fingerprint: str = "",
    approval_reference: str = "",
    allow_pre_hold_service_continuity: bool = False,
) -> dict[str, Any]:
    _ensure_active_hosted_runtime_target(
        target, action=f"business-data-maintenance-{action}"
    )
    if action not in {
        "status",
        "prepare",
        "hold",
        "restore",
        "restore-continuity-status",
        "set-process",
        "barrier-status",
        "barrier-acquire",
        "barrier-confirm",
        "barrier-restoring",
        "barrier-release",
        "barrier-abort",
    }:
        raise ValueError(f"unsupported business-data maintenance action: {action}")
    if action in {
        "prepare",
        "hold",
        "restore",
        "set-process",
        "barrier-acquire",
        "barrier-confirm",
        "barrier-restoring",
        "barrier-release",
        "barrier-abort",
    }:
        _ensure_target_allows_mutation(
            target,
            action=f"business-data-maintenance-{action}",
            dry_run=False,
        )
    runtime_dir = str(target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or "").strip()
    if runtime_dir != ACTIVE_HOSTED_RUNTIME_RUNTIME_DIR:
        raise ValueError("business-data maintenance requires the canonical active runtime dir")
    if not target.environment_file:
        raise ValueError("business-data maintenance requires the hosted environment file")
    runner_args = [
        "python3",
        "apps/business_data_maintenance.py",
        action,
        "--runtime-dir",
        runtime_dir,
        "--env-file",
        target.environment_file,
    ]
    if action == "hold":
        runner_args.extend(
            ["--wait-timeout-seconds", "1200", "--poll-interval-seconds", "2"]
        )
    if action in {"prepare", "hold", "restore"}:
        runner_args.extend(
            [
                "--actor",
                str(actor or "repo_owned_cli"),
                "--reason",
                str(reason or "canonical cross-writer maintenance"),
            ]
        )
    if action == "restore":
        if expected_revision is None:
            raise ValueError(
                "business-data maintenance restore requires --expected-revision"
            )
        runner_args.extend(["--expected-revision", str(int(expected_revision))])
        if allow_pre_hold_service_continuity:
            runner_args.append("--allow-pre-hold-service-continuity")
    elif action == "set-process":
        if expected_revision is None or not process_key or desired not in {"on", "off"}:
            raise ValueError(
                "business-data maintenance set-process requires exact revision, "
                "process key and desired on|off"
            )
        runner_args.extend(
            [
                "--expected-revision",
                str(int(expected_revision)),
                "--process-key",
                process_key,
                "--desired",
                desired,
                "--actor",
                str(actor or "repo_owned_cli"),
                "--reason",
                str(reason or "owner-controlled recovery"),
            ]
        )
    elif action in {
        "barrier-acquire",
        "barrier-confirm",
        "barrier-restoring",
        "barrier-release",
        "barrier-abort",
    }:
        if not window_id or not plan_fingerprint:
            raise ValueError(
                f"business-data maintenance {action} requires exact window "
                "identity and plan fingerprint"
            )
        runner_args.extend(
            [
                "--window-id",
                window_id,
                "--plan-fingerprint",
                plan_fingerprint,
            ]
        )
        if action == "barrier-acquire":
            if not approval_reference:
                raise ValueError(
                    "business-data maintenance barrier-acquire requires "
                    "--approval-reference"
                )
            runner_args.extend(
                [
                    "--window-kind",
                    window_kind,
                    "--approval-reference",
                    approval_reference,
                    "--actor",
                    str(actor or "repo_owned_cli"),
                    "--reason",
                    str(reason or "bounded maintenance window"),
                ]
            )
        elif action in {"barrier-release", "barrier-abort"}:
            runner_args.extend(
                [
                    "--actor",
                    str(actor or "repo_owned_cli"),
                    "--reason",
                    str(reason or "exact maintenance restore confirmed"),
                ]
            )
    command = " && ".join(
        [
            f"cd {shlex.quote(target.target_dir)}",
            " ".join(shlex.quote(item) for item in runner_args),
        ]
    )
    result = subprocess.run(
        _remote_shell_command(target, command),
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=1500.0 if action in {"hold", "restore"} else 300.0,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"business-data maintenance {action} failed: "
            + (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("business-data maintenance returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("business-data maintenance returned a non-object JSON payload")
    return payload


def run_business_data_maintenance_command(args: argparse.Namespace) -> int:
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    action = str(args.action)
    evidence: dict[str, Any] = {}
    if action == "hold":
        evidence["core_prepare"] = _run_remote_business_data_maintenance_runner(
            target,
            action="prepare",
            actor=str(args.actor or "repo_owned_cli"),
            reason=str(args.reason or "canonical cross-writer maintenance"),
        )
        evidence["warehouse"] = _run_remote_warehouse_functional_maintenance_action(
            target,
            action="hold",
            disable_timer=True,
        )
        result = _run_remote_business_data_maintenance_runner(
            target,
            action="hold",
            actor=str(args.actor or "repo_owned_cli"),
            reason=str(args.reason or "canonical cross-writer maintenance"),
        )
        if (
            result.get("quiet") is not True
            or str(result.get("status") or "") != "held"
        ):
            raise RuntimeError("business-data maintenance hold readback is incomplete")
        evidence["autoanswers"] = _run_remote_autoanswers_lifecycle(
            target,
            action="status",
        )
    elif action == "restore":
        if args.expected_revision is None:
            raise ValueError(
                "business-data-maintenance restore requires --expected-revision"
            )
        result = _run_remote_business_data_maintenance_runner(
            target,
            action="restore",
            expected_revision=int(args.expected_revision),
            actor=str(args.actor or "repo_owned_cli"),
            reason=str(args.reason or "bounded recovery completed"),
            allow_pre_hold_service_continuity=bool(
                args.allow_pre_hold_service_continuity
            ),
        )
        if (
            str(result.get("status") or "") != "restored"
            or result.get("exact_prior_state_restored") is not True
        ):
            raise RuntimeError("business-data maintenance restore readback is incomplete")
        evidence["warehouse"] = _run_remote_warehouse_functional_maintenance_action(
            target,
            action="status",
        )
        evidence["autoanswers"] = _run_remote_autoanswers_lifecycle(
            target,
            action="status",
        )
        evidence["autoanswers_readonly_timer"] = _run_remote_autoanswers_readonly_timer(
            target,
            action="status",
        )
    elif action == "restore-continuity-status":
        result = _run_remote_business_data_maintenance_runner(
            target,
            action=action,
        )
        continuity = dict(result.get("service_continuity") or {})
        if (
            str(result.get("status") or "") != "ready"
            or not re.fullmatch(
                r"sha256:[0-9a-f]{64}",
                str(continuity.get("fingerprint") or ""),
            )
            or not list(continuity.get("services") or [])
        ):
            raise RuntimeError(
                "business-data restore continuity readback is incomplete"
            )
    elif action == "set-process":
        if args.expected_revision is None:
            raise ValueError(
                "business-data-maintenance set-process requires --expected-revision"
            )
        result = _run_remote_business_data_maintenance_runner(
            target,
            action="set-process",
            expected_revision=int(args.expected_revision),
            process_key=str(args.process_key or ""),
            desired=str(args.desired or ""),
            actor=str(args.actor or "repo_owned_cli"),
            reason=str(args.reason or "owner-controlled recovery"),
        )
        readback = dict(result.get("auto_updates") or {})
        selected = next(
            (
                dict(item)
                for item in readback.get("processes", [])
                if isinstance(item, Mapping)
                and str(item.get("process_key") or "") == str(args.process_key)
            ),
            None,
        )
        if (
            str(result.get("status") or "") != "updated"
            or selected is None
            or bool(selected.get("desired")) != (str(args.desired) == "on")
        ):
            raise RuntimeError(
                "business-data maintenance set-process readback is incomplete"
            )
    elif action.startswith("barrier-"):
        result = _run_remote_business_data_maintenance_runner(
            target,
            action=action,
            actor=str(args.actor or "repo_owned_cli"),
            reason=str(args.reason or ""),
            window_id=str(args.window_id or ""),
            window_kind=str(args.window_kind or "snapshot"),
            plan_fingerprint=str(args.plan_fingerprint or ""),
            approval_reference=str(args.approval_reference or ""),
        )
        if action == "barrier-status":
            if "active" not in result:
                raise RuntimeError(
                    "business-data maintenance barrier status readback is incomplete"
                )
        elif action in {"barrier-release", "barrier-abort"}:
            if result.get("active") is not False:
                raise RuntimeError(
                    "business-data maintenance barrier release/abort readback "
                    "is incomplete"
                )
        elif result.get("active") is not True:
            raise RuntimeError(
                "business-data maintenance active barrier readback is incomplete"
            )
    else:
        result = _run_remote_business_data_maintenance_runner(target, action="status")
        evidence["warehouse"] = _run_remote_warehouse_functional_maintenance_action(
            target,
            action="status",
        )
        evidence["autoanswers"] = _run_remote_autoanswers_lifecycle(
            target,
            action="status",
        )
        evidence["autoanswers_readonly_timer"] = _run_remote_autoanswers_readonly_timer(
            target,
            action="status",
        )
    _print_json(
        {
            "target_id": target.target_id,
            "ssh_destination": target.ssh_destination,
            "runtime_dir": str(target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""),
            "action": f"business-data-maintenance-{action}",
            "result": result,
            "evidence": evidence,
        }
    )
    return 0


def _run_remote_warehouse_opening_action(
    target: HostedRuntimeTarget,
    *,
    action: str,
    plan_path: Path | None = None,
    fingerprint: str = "",
    diagnostic_nm_ids: tuple[int, ...] = (),
) -> dict[str, Any]:
    _ensure_active_hosted_runtime_target(target, action=f"warehouse-opening-{action}")
    if action in {"apply", "rollback"}:
        _ensure_target_allows_mutation(
            target,
            action=f"warehouse-opening-{action}",
            dry_run=False,
        )
    runtime_dir = str(target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or "").strip()
    if runtime_dir != ACTIVE_HOSTED_RUNTIME_RUNTIME_DIR:
        raise ValueError("warehouse opening runner requires the canonical active runtime dir")
    if not target.environment_file:
        raise ValueError("warehouse opening runner requires the hosted environment file")

    runner_args = [
        "python3",
        "apps/warehouse_opening_snapshot.py",
        "--runtime-dir",
        runtime_dir,
        "--env-file",
        target.environment_file,
        action,
    ]
    stdin_text: str | None = None
    if action == "apply":
        if plan_path is None:
            raise ValueError("warehouse opening apply requires a plan path")
        stdin_text = plan_path.read_text(encoding="utf-8")
        parsed_plan = json.loads(stdin_text)
        if not isinstance(parsed_plan, dict):
            raise ValueError("warehouse opening plan must be a JSON object")
        if str(parsed_plan.get("plan_fingerprint") or "") != fingerprint:
            raise ValueError("warehouse opening plan and --fingerprint do not match")
        runner_args.extend(
            [
                "--plan-file",
                "/dev/stdin",
                "--fingerprint",
                fingerprint,
                "--backup-dir",
                "/opt/wb-core-runtime/backups/warehouse-opening",
            ]
        )
    elif action == "rollback":
        runner_args.extend(
            [
                "--fingerprint",
                fingerprint,
                "--backup-dir",
                "/opt/wb-core-runtime/backups/warehouse-opening",
            ]
        )
    elif action == "diagnose-discrepancy":
        if not diagnostic_nm_ids:
            raise ValueError("warehouse discrepancy diagnostic requires at least one nmID")
        for nm_id in diagnostic_nm_ids:
            if int(nm_id) <= 0:
                raise ValueError("warehouse discrepancy diagnostic nmID must be positive")
            runner_args.extend(["--nm-id", str(int(nm_id))])

    shell_command = " && ".join(
        [
            f"cd {shlex.quote(target.target_dir)}",
            " ".join(shlex.quote(item) for item in runner_args),
        ]
    )
    result = subprocess.run(
        _remote_shell_command(target, shell_command),
        input=stdin_text,
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=_warehouse_opening_timeout_seconds(action),
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"warehouse opening {action} failed: "
            + (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("warehouse opening runner returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("warehouse opening runner returned a non-object JSON payload")
    return payload


def _warehouse_opening_timeout_seconds(action: str) -> float:
    if action in {"apply", "rollback"}:
        return WAREHOUSE_OPENING_MUTATION_TIMEOUT_SECONDS
    return WAREHOUSE_OPENING_READ_TIMEOUT_SECONDS


def run_warehouse_functional_failed_backup_cleanup_command(args: argparse.Namespace) -> int:
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    apply = bool(args.cleanup_apply)
    payload = _run_remote_warehouse_functional_failed_backup_cleanup(
        target,
        source=str(args.source),
        apply=apply,
        fingerprint=str(getattr(args, "fingerprint", "") or ""),
    )
    _print_json(
        {
            "target_id": target.target_id,
            "ssh_destination": target.ssh_destination,
            "action": "failed-backup-cleanup-apply" if apply else "failed-backup-cleanup-dry-run",
            "result": payload,
        }
    )
    return 0


def _run_remote_warehouse_functional_failed_backup_cleanup(
    target: HostedRuntimeTarget,
    *,
    source: str,
    apply: bool,
    fingerprint: str,
) -> dict[str, Any]:
    action = (
        "warehouse-functional-failed-backup-cleanup-apply"
        if apply
        else "warehouse-functional-failed-backup-cleanup-dry-run"
    )
    _ensure_active_hosted_runtime_target(target, action=action)
    if apply:
        _ensure_target_allows_mutation(target, action=action, dry_run=False)
    source_path = Path(str(source or ""))
    allowed_candidates = {
        Path("/opt/wb-core-runtime/backups/warehouse-functional"): re.compile(
            r"warehouse_functional_cutover_v1-[0-9TZ]+\.sqlite3"
        ),
        Path("/opt/wb-core-runtime/backups/warehouse-functional-recovery"): re.compile(
            r"warehouse-functional-emergency-[0-9a-f]{16}(?:-[0-9TZ]+)?\.sqlite3"
        ),
    }
    allowed_name = allowed_candidates.get(source_path.parent)
    if (
        not source_path.is_absolute()
        or allowed_name is None
        or allowed_name.fullmatch(source_path.name) is None
    ):
        raise ValueError(
            "failed backup cleanup is restricted to one functional cutover or emergency SQLite candidate"
        )
    runner_args = [
        "python3",
        "apps/sqlite_failed_backup_cleanup.py",
        "--source",
        str(source_path),
    ]
    if apply:
        if not fingerprint:
            raise ValueError("failed backup cleanup apply requires an exact fingerprint")
        runner_args.extend(["--apply", "--fingerprint", fingerprint])
    shell_command = " && ".join(
        [
            f"cd {shlex.quote(target.target_dir)}",
            " ".join(shlex.quote(item) for item in runner_args),
        ]
    )
    result = subprocess.run(
        _remote_shell_command(target, shell_command),
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=WAREHOUSE_OPENING_MUTATION_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{action} failed: "
            + (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("failed backup cleanup runner returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("failed backup cleanup runner returned a non-object JSON payload")
    return payload


def run_warehouse_ui_flow_command(args: argparse.Namespace) -> int:
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    _ensure_active_hosted_runtime_target(target, action="warehouse-ui-flow")
    auth_cookie = _build_probe_auth_cookie(target, timeout_seconds=float(args.timeout_seconds))
    if not auth_cookie:
        raise RuntimeError("warehouse UI flow requires safely available production app-session auth")
    evidence_dir = Path(str(args.evidence_dir)).resolve()
    try:
        evidence_dir.relative_to(ROOT.resolve())
    except ValueError:
        pass
    else:
        raise ValueError("warehouse UI evidence must be stored outside the repository")
    deployed_sha = str(args.deployed_sha or "").strip().lower()
    if str(args.acceptance_profile or "") in {
        "warehouse_recovery_policy_20260726",
        "vitrina_incident_provisional_20260727",
    }:
        if not re.fullmatch(r"[0-9a-f]{40}", deployed_sha):
            raise ValueError(
                "profiled warehouse UI flow requires an exact deployed SHA"
            )
        runtime_sha_path = (
            f"{target.target_dir.rstrip('/')}/.wb-core-runtime-sha"
        )
        verify = subprocess.run(
            _remote_shell_command(
                target,
                "test \"$(tr -d '\\r\\n' < "
                + shlex.quote(runtime_sha_path)
                + ")\" = "
                + shlex.quote(deployed_sha),
            ),
            text=True,
            capture_output=True,
            cwd=ROOT,
            timeout=float(args.timeout_seconds),
            check=False,
        )
        if verify.returncode != 0:
            raise RuntimeError(
                "profiled warehouse UI flow deployed SHA does not match "
                "the canonical runtime marker"
            )
    readback = _run_remote_warehouse_functional_action(target, action="readback")
    from apps.warehouse_stocks_production_ui_flow import run_warehouse_ui_flow

    result = run_warehouse_ui_flow(
        base_url=target.public_base_url,
        auth_cookie=auth_cookie,
        expected_readback=readback,
        evidence_dir=evidence_dir,
        deployed_sha=deployed_sha,
        headless=not bool(args.headed),
        acceptance_profile=str(args.acceptance_profile or "") or None,
    )
    _print_json(
        {
            "target_id": target.target_id,
            "public_base_url": target.public_base_url,
            "auth": _probe_auth_summary(auth_cookie),
            "readback_cutover_id": str((readback.get("cutover") or {}).get("cutover_id") or ""),
            "result": result,
        }
    )
    return 0


def run_finance_ui_flow_command(args: argparse.Namespace) -> int:
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    _ensure_active_hosted_runtime_target(target, action="finance-ui-flow")
    auth_cookie = _build_probe_auth_cookie(
        target,
        timeout_seconds=float(args.timeout_seconds),
    )
    if not auth_cookie:
        raise RuntimeError(
            "Finance UI flow requires safely available production app-session auth"
        )
    evidence_dir = Path(str(args.evidence_dir)).resolve()
    try:
        evidence_dir.relative_to(ROOT.resolve())
    except ValueError:
        pass
    else:
        raise ValueError("Finance UI evidence must be stored outside the repository")
    from apps.finance_partner_production_ui_flow import run_finance_partner_ui_flow

    result = run_finance_partner_ui_flow(
        base_url=target.public_base_url,
        auth_cookie=auth_cookie,
        evidence_dir=evidence_dir,
        headless=not bool(args.headed),
        deployed_sha=str(args.deployed_sha or ""),
    )
    _print_json(
        {
            "target_id": target.target_id,
            "public_base_url": target.public_base_url,
            "auth": _probe_auth_summary(auth_cookie),
            "result": result,
        }
    )
    return 0


def run_autoanswers_ui_flow_command(args: argparse.Namespace) -> int:
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    _ensure_active_hosted_runtime_target(target, action="autoanswers-ui-flow")
    expected_state = str(args.expected_state)
    target_force_off = str(target.runtime_env.get("WB_AUTOANSWERS_FORCE_OFF") or "").strip().lower()
    expected_force_off = "true" if expected_state == "off-force" else "false"
    if target_force_off != expected_force_off:
        raise RuntimeError(
            f"autoanswers UI flow expected target force-off={expected_force_off}, got {target_force_off or '<missing>'}"
        )
    auth_cookie = _build_probe_auth_cookie(target, timeout_seconds=float(args.timeout_seconds))
    if not auth_cookie:
        raise RuntimeError("autoanswers UI flow requires safely available production app-session auth")
    evidence_dir = Path(str(args.evidence_dir)).resolve()
    try:
        evidence_dir.relative_to(ROOT.resolve())
    except ValueError:
        pass
    else:
        raise ValueError("autoanswers UI evidence must be stored outside the repository")
    from apps.wb_autoanswers_production_ui_flow import run_autoanswers_ui_flow

    result = run_autoanswers_ui_flow(
        base_url=target.public_base_url,
        auth_cookie=auth_cookie,
        evidence_dir=evidence_dir,
        headless=not bool(args.headed),
        expected_state=expected_state,
        verify_limit_save=bool(args.verify_limit_save),
    )
    _print_json(
        {
            "target_id": target.target_id,
            "public_base_url": target.public_base_url,
            "auth": _probe_auth_summary(auth_cookie),
            "result": result,
        }
    )
    return 0


def run_autoanswers_store_rollback_command(args: argparse.Namespace) -> int:
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    apply = bool(args.rollback_apply)
    action = (
        "autoanswers-store-rollback-apply"
        if apply
        else "autoanswers-store-rollback-plan"
    )
    _ensure_active_hosted_runtime_target(target, action=action)
    if apply:
        _ensure_target_allows_mutation(target, action=action, dry_run=False)
        if not str(args.fingerprint or "").startswith("sha256:"):
            raise ValueError(
                "Autoanswers store rollback apply requires the exact plan fingerprint"
            )
    runtime_dir = str(
        target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""
    ).strip()
    runner_action = "store-rollback-apply" if apply else "store-rollback-plan"
    environment = (
        "/usr/bin/env WB_AUTOANSWERS_FORCE_OFF=true "
        "WB_AUTOANSWERS_DEPLOY_SERVICE_QUIESCE=true "
        "WB_AUTOANSWERS_STORE_ROLLBACK_FINGERPRINT="
        + shlex.quote(str(args.fingerprint or ""))
        + " "
        if apply
        else ""
    )
    shell_command = (
        f"cd {shlex.quote(target.target_dir)} && "
        + environment
        + "python3 apps/wb_autoanswers_activation.py "
        + runner_action
        + " --runtime-dir "
        + shlex.quote(runtime_dir)
    )
    result = subprocess.run(
        _remote_shell_command(target, shell_command),
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=7200,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{action} failed: "
            + (
                result.stderr.strip()
                or result.stdout.strip()
                or f"exit {result.returncode}"
            )
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Autoanswers store rollback runner returned invalid JSON"
        ) from exc
    _print_json(
        {
            "target_id": target.target_id,
            "ssh_destination": target.ssh_destination,
            "action": action,
            "result": payload,
        }
    )
    return 0


def _observe_autoanswers_background_writer(
    target: HostedRuntimeTarget,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Wait for a real timer-owned Autoanswers process without starting one."""

    units = (
        "wb-core-autoanswers-worker.service",
        "wb-core-autoanswers-readonly-sync.service",
    )
    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        for unit in units:
            result = subprocess.run(
                _remote_shell_command(
                    target,
                    "systemctl show "
                    + shlex.quote(unit)
                    + " --property=ActiveState,SubState,ExecMainStartTimestamp,"
                    "ExecMainExitTimestamp,InvocationID --no-pager",
                ),
                text=True,
                capture_output=True,
                cwd=ROOT,
                timeout=30,
                check=False,
            )
            values = {}
            if result.returncode == 0:
                values = {
                    key: value
                    for line in result.stdout.splitlines()
                    if "=" in line
                    for key, value in [line.split("=", 1)]
                }
            latest = {
                "unit": unit,
                "active_state": str(values.get("ActiveState") or ""),
                "sub_state": str(values.get("SubState") or ""),
                "exec_main_started_at": str(
                    values.get("ExecMainStartTimestamp") or ""
                ),
                "exec_main_exited_at": str(
                    values.get("ExecMainExitTimestamp") or ""
                ),
                "invocation_id_prefix": str(
                    values.get("InvocationID") or ""
                )[:12],
                "observed_at": datetime.now(timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
                "observer_started_service": False,
            }
            if latest["active_state"] in {"active", "activating"}:
                return latest
        time.sleep(1.0)
    raise RuntimeError(
        "No timer-owned Autoanswers writer became active inside the bounded "
        f"{float(timeout_seconds):.0f}s observation window; latest={latest}"
    )


def run_sqlite_contention_ui_flow_command(args: argparse.Namespace) -> int:
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    _ensure_active_hosted_runtime_target(
        target,
        action="sqlite-contention-ui-flow",
    )
    auth_cookie = _build_probe_auth_cookie(
        target,
        timeout_seconds=float(args.timeout_seconds),
    )
    if not auth_cookie:
        raise RuntimeError(
            "SQLite contention UI flow requires safely available production "
            "app-session auth"
        )
    evidence_dir = Path(str(args.evidence_dir)).resolve()
    try:
        evidence_dir.relative_to(ROOT.resolve())
    except ValueError:
        pass
    else:
        raise ValueError(
            "SQLite contention UI evidence must be stored outside the repository"
        )
    background_evidence = _observe_autoanswers_background_writer(
        target,
        timeout_seconds=float(args.background_wait_seconds),
    )
    from apps.sqlite_contention_production_ui_flow import (
        run_sqlite_contention_ui_flow,
    )

    result = run_sqlite_contention_ui_flow(
        base_url=target.public_base_url,
        auth_cookie=auth_cookie,
        evidence_dir=evidence_dir,
        headless=not bool(args.headed),
        deployed_sha=str(args.deployed_sha or ""),
        background_evidence=background_evidence,
    )
    _print_json(
        {
            "target_id": target.target_id,
            "public_base_url": target.public_base_url,
            "auth": _probe_auth_summary(auth_cookie),
            "result": result,
        }
    )
    return 0


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

    autoanswers_readonly = subparsers.add_parser(
        "autoanswers-readonly",
        help="Run a bounded GET-only WB feedback or manual-mode media canary on the active runtime.",
    )
    autoanswers_readonly.add_argument(
        "operation", choices=("status", "canary", "steady", "backfill", "manual-media-canary")
    )
    autoanswers_readonly.add_argument("--page-size", type=int, default=100)
    autoanswers_readonly.add_argument("--max-pages", type=int, default=1000)
    autoanswers_readonly.add_argument("--min-request-interval-seconds", type=float, default=1.0)
    autoanswers_readonly.set_defaults(handler=run_autoanswers_readonly_command)

    autoanswers_readonly_timer = subparsers.add_parser(
        "autoanswers-readonly-timer",
        help="Inspect, enable or disable the installed GET-only force-off background sync timer.",
    )
    autoanswers_readonly_timer.add_argument("action", choices=("status", "enable", "disable"))
    autoanswers_readonly_timer.set_defaults(handler=run_autoanswers_readonly_timer_command)

    autoanswers_lifecycle = subparsers.add_parser(
        "autoanswers-lifecycle",
        help="Inspect or reconcile feature-owned Autoanswers intent and component timers.",
    )
    autoanswers_lifecycle.add_argument(
        "action",
        choices=("status", "reconcile", "suspend"),
    )
    autoanswers_lifecycle.set_defaults(handler=run_autoanswers_lifecycle_command)

    autoanswers_budget_reconciliation = subparsers.add_parser(
        "autoanswers-budget-reconciliation",
        help=(
            "Plan, apply or read back append-only conservative holds for "
            "unknown provider cost."
        ),
    )
    autoanswers_budget_reconciliation.add_argument(
        "action",
        choices=("dry-run", "apply", "readback"),
    )
    autoanswers_budget_reconciliation.add_argument("--fingerprint", default="")
    autoanswers_budget_reconciliation.set_defaults(
        handler=run_autoanswers_budget_reconciliation_command
    )

    autoanswers_prefilter_skip_recovery = subparsers.add_parser(
        "autoanswers-prefilter-skip-recovery",
        help=(
            "Plan, apply or read back bounded restoration of proven "
            "prefilter skips after an invalid policy-epoch requeue."
        ),
    )
    autoanswers_prefilter_skip_recovery.add_argument(
        "action",
        nargs="?",
        choices=(
            "dry-run",
            "apply",
            "readback",
            "release-dry-run",
            "release-apply",
            "release-readback",
        ),
        default="dry-run",
    )
    autoanswers_prefilter_skip_recovery.add_argument(
        "--transition-run-id",
        required=True,
    )
    autoanswers_prefilter_skip_recovery.add_argument(
        "--expected-rows",
        type=int,
        required=True,
    )
    autoanswers_prefilter_skip_recovery.add_argument(
        "--fingerprint",
        default="",
    )
    autoanswers_prefilter_skip_recovery.add_argument(
        "--source-fingerprint",
        default="",
    )
    autoanswers_prefilter_skip_recovery.set_defaults(
        handler=run_autoanswers_prefilter_skip_recovery_command
    )

    finance_canonical_dry_run = subparsers.add_parser(
        "finance-canonical-dry-run",
        help="Build the read-only all-history Finance/ads/canonical-cost plan.",
    )
    finance_canonical_dry_run.add_argument("--output", default="")
    finance_canonical_dry_run.set_defaults(
        handler=run_finance_canonical_command,
        finance_canonical_action="dry-run",
    )

    finance_canonical_apply = subparsers.add_parser(
        "finance-canonical-apply",
        help="Apply one exact reviewed all-history canonical Finance plan.",
    )
    finance_canonical_apply.add_argument("--plan-file", required=True)
    finance_canonical_apply.add_argument("--fingerprint", required=True)
    finance_canonical_apply.add_argument("--approval-reference", required=True)
    finance_canonical_apply.set_defaults(
        handler=run_finance_canonical_command,
        finance_canonical_action="apply",
    )

    finance_canonical_readback = subparsers.add_parser(
        "finance-canonical-readback",
        help="Prove zero all-history Finance deltas/blockers after canonical apply.",
    )
    finance_canonical_readback.set_defaults(
        handler=run_finance_canonical_command,
        finance_canonical_action="readback",
    )

    finance_storage_split_dry_run = subparsers.add_parser(
        "finance-storage-split-dry-run",
        help=(
            "Build the exact query-only Finance raw/operational split plan; "
            "create no destination bytes and keep the monolith canonical."
        ),
    )
    finance_storage_split_dry_run.add_argument("--output", required=True)
    finance_storage_split_dry_run.add_argument("--chunk-size", type=int, default=10_000)
    finance_storage_split_dry_run.add_argument(
        "--source-snapshot-manifest",
        required=True,
        help=(
            "Exact remote integrity-verified coherent snapshot manifest; "
            "live monolith scans are not allowed in this hosted phase."
        ),
    )
    finance_storage_split_dry_run.set_defaults(
        handler=run_finance_storage_split_command,
        finance_storage_split_action="dry-run",
    )

    finance_storage_split_health = subparsers.add_parser(
        "finance-storage-split-health",
        help="Read generation/cursor/lag/mismatch/capacity/rollback health without mutation.",
    )
    finance_storage_split_health.add_argument("--output", default="")
    finance_storage_split_health.add_argument("--chunk-size", type=int, default=10_000)
    finance_storage_split_health.set_defaults(
        handler=run_finance_storage_split_command,
        finance_storage_split_action="health",
    )

    finance_storage_split_apply = subparsers.add_parser(
        "finance-storage-split-apply",
        help=(
            "Build only the reviewed candidate raw/operational generation; "
            "never switch the global manifest or canonical readers."
        ),
    )
    finance_storage_split_apply.add_argument("--plan-file", required=True)
    finance_storage_split_apply.add_argument("--fingerprint", required=True)
    finance_storage_split_apply.add_argument("--approval-reference", required=True)
    finance_storage_split_apply.add_argument("--chunk-size", type=int, default=10_000)
    finance_storage_split_apply.add_argument(
        "--source-snapshot-manifest",
        required=True,
    )
    finance_storage_split_apply.set_defaults(
        handler=run_finance_storage_split_command,
        finance_storage_split_action="apply",
    )

    finance_storage_snapshot_plan = subparsers.add_parser(
        "finance-storage-snapshot-plan",
        help=(
            "Build a bounded metadata/capacity/open-writer plan for one "
            "short coherent-copy window; do not scan the live database."
        ),
    )
    finance_storage_snapshot_plan.add_argument("--output", required=True)
    finance_storage_snapshot_plan.add_argument(
        "--chunk-size",
        type=int,
        default=10_000,
    )
    finance_storage_snapshot_plan.set_defaults(
        handler=run_finance_storage_split_command,
        finance_storage_split_action="snapshot-plan",
    )

    finance_storage_snapshot_apply = subparsers.add_parser(
        "finance-storage-snapshot-apply",
        help=(
            "Automatically acquire the manual write barrier, drain exact "
            "writers/timers, capture one coherent copy, restore exact prior "
            "controls and release the barrier."
        ),
    )
    finance_storage_snapshot_apply.add_argument("--plan-file", required=True)
    finance_storage_snapshot_apply.add_argument("--fingerprint", required=True)
    finance_storage_snapshot_apply.add_argument(
        "--approval-reference",
        required=True,
    )
    finance_storage_snapshot_apply.add_argument(
        "--chunk-size",
        type=int,
        default=10_000,
    )
    finance_storage_snapshot_apply.set_defaults(
        handler=run_finance_storage_split_command,
        finance_storage_split_action="snapshot-create",
    )

    finance_storage_snapshot_integrity = subparsers.add_parser(
        "finance-storage-snapshot-integrity",
        help=(
            "Run full SQLite integrity and foreign-key checks on the coherent "
            "copy outside the maintenance window."
        ),
    )
    finance_storage_snapshot_integrity.add_argument(
        "--source-snapshot-manifest",
        required=True,
    )
    finance_storage_snapshot_integrity.add_argument("--output", required=True)
    finance_storage_snapshot_integrity.add_argument(
        "--chunk-size",
        type=int,
        default=10_000,
    )
    finance_storage_snapshot_integrity.set_defaults(
        handler=run_finance_storage_split_command,
        finance_storage_split_action="snapshot-integrity",
    )

    for command_name, action_name, help_text in (
        (
            "finance-storage-shadow-status",
            "shadow-status",
            "Read exact Finance shadow-ingest state.",
        ),
        (
            "finance-storage-shadow-activate",
            "shadow-activate",
            "Activate additive Finance raw outbox capture for one approved candidate.",
        ),
        (
            "finance-storage-shadow-reconcile",
            "shadow-reconcile",
            "Idempotently reconcile current legacy Finance raw rows into the candidate.",
        ),
        (
            "finance-storage-shadow-verify",
            "shadow-verify",
            "Persist all-week shadow equality, lag and performance evidence.",
        ),
        (
            "finance-storage-live-tail-apply",
            "live-tail-apply",
            "Apply bounded ordered Finance raw outbox events to the candidate.",
        ),
        (
            "finance-storage-shadow-deactivate",
            "shadow-deactivate",
            "Deactivate one exact Finance shadow-ingest generation.",
        ),
    ):
        command = subparsers.add_parser(command_name, help=help_text)
        command.add_argument("--candidate-manifest", required=True)
        command.add_argument("--fingerprint", required=True)
        command.add_argument("--approval-reference", required=True)
        command.add_argument("--output", default="")
        command.add_argument("--chunk-size", type=int, default=10_000)
        if action_name == "shadow-verify":
            command.add_argument(
                "--minimum-observation-seconds",
                type=int,
                default=3600,
            )
        command.set_defaults(
            handler=run_finance_storage_split_command,
            finance_storage_split_action=action_name,
        )

    finance_storage_cutover_plan = subparsers.add_parser(
        "finance-storage-cutover-plan",
        help=(
            "Build an exact final-hold Finance cutover plan without changing "
            "the global manifest."
        ),
    )
    finance_storage_cutover_plan.add_argument(
        "--candidate-manifest",
        required=True,
    )
    finance_storage_cutover_plan.add_argument(
        "--candidate-plan-fingerprint",
        required=True,
    )
    finance_storage_cutover_plan.add_argument("--output", required=True)
    finance_storage_cutover_plan.add_argument(
        "--chunk-size",
        type=int,
        default=10_000,
    )
    finance_storage_cutover_plan.set_defaults(
        handler=run_finance_storage_split_command,
        finance_storage_split_action="cutover-plan",
    )

    finance_storage_cutover_apply = subparsers.add_parser(
        "finance-storage-cutover-apply",
        help=(
            "Run the exact short final hold, fresh operational recopy, "
            "atomic split-manifest switch and exact control restore."
        ),
    )
    finance_storage_cutover_apply.add_argument("--plan-file", required=True)
    finance_storage_cutover_apply.add_argument("--fingerprint", required=True)
    finance_storage_cutover_apply.add_argument(
        "--approval-reference",
        required=True,
    )
    finance_storage_cutover_apply.add_argument(
        "--candidate-manifest",
        required=True,
    )
    finance_storage_cutover_apply.add_argument(
        "--candidate-plan-fingerprint",
        required=True,
    )
    finance_storage_cutover_apply.add_argument(
        "--chunk-size",
        type=int,
        default=10_000,
    )
    finance_storage_cutover_apply.add_argument("--output", required=True)
    finance_storage_cutover_apply.set_defaults(
        handler=run_finance_storage_split_command,
        finance_storage_split_action="cutover-apply",
    )

    finance_storage_rollback_plan = subparsers.add_parser(
        "finance-storage-rollback-plan",
        help=(
            "Build a query-only exact plan for a retained, reconciled "
            "rollback monolith."
        ),
    )
    finance_storage_rollback_plan.add_argument("--output", required=True)
    finance_storage_rollback_plan.add_argument(
        "--chunk-size",
        type=int,
        default=10_000,
    )
    finance_storage_rollback_plan.set_defaults(
        handler=run_finance_storage_split_command,
        finance_storage_split_action="rollback-plan",
    )

    finance_storage_rollback_prepare = subparsers.add_parser(
        "finance-storage-rollback-prepare",
        help=(
            "Build and fully verify the rollback monolith candidate while "
            "normal split operation remains available."
        ),
    )
    finance_storage_rollback_prepare.add_argument(
        "--plan-file",
        required=True,
    )
    finance_storage_rollback_prepare.add_argument(
        "--fingerprint",
        required=True,
    )
    finance_storage_rollback_prepare.add_argument(
        "--approval-reference",
        required=True,
    )
    finance_storage_rollback_prepare.add_argument("--output", required=True)
    finance_storage_rollback_prepare.add_argument(
        "--chunk-size",
        type=int,
        default=10_000,
    )
    finance_storage_rollback_prepare.set_defaults(
        handler=run_finance_storage_split_command,
        finance_storage_split_action="rollback-prepare",
    )

    finance_storage_rollback_apply = subparsers.add_parser(
        "finance-storage-rollback-apply",
        help=(
            "Run the short rollback hold, replay post-prepare raw changes, "
            "recopy operational state and atomically select the monolith."
        ),
    )
    finance_storage_rollback_apply.add_argument(
        "--plan-file",
        required=True,
    )
    finance_storage_rollback_apply.add_argument(
        "--fingerprint",
        required=True,
    )
    finance_storage_rollback_apply.add_argument(
        "--approval-reference",
        required=True,
    )
    finance_storage_rollback_apply.add_argument(
        "--rollback-candidate-evidence",
        required=True,
    )
    finance_storage_rollback_apply.add_argument("--output", required=True)
    finance_storage_rollback_apply.add_argument(
        "--chunk-size",
        type=int,
        default=10_000,
    )
    finance_storage_rollback_apply.set_defaults(
        handler=run_finance_storage_split_command,
        finance_storage_split_action="rollback-apply",
    )

    partner_finance_diagnostic = subparsers.add_parser(
        "partner-finance-diagnostic",
        help=(
            "Run the bounded read-only Partner/Finance raw-operation reconciliation "
            "on the active runtime."
        ),
    )
    partner_finance_diagnostic.add_argument("--nm-id", default="")
    partner_finance_diagnostic.add_argument(
        "--week",
        action="append",
        default=[],
        help="Exact selected Partner week start; repeat as needed.",
    )
    partner_finance_diagnostic.add_argument("--output", required=True)
    partner_finance_diagnostic.set_defaults(
        handler=run_partner_finance_diagnostic_command,
    )

    ads_historical_dry_run = subparsers.add_parser(
        "ads-historical-dry-run",
        help="Build an exact read-only official fullstats recovery plan.",
    )
    ads_historical_dry_run.add_argument("--nm-id", action="append", type=int, required=True)
    ads_historical_dry_run.add_argument("--target-date", action="append", required=True)
    ads_historical_dry_run.add_argument("--output", required=True)
    ads_historical_dry_run.set_defaults(
        handler=run_ads_historical_recovery_command,
        ads_historical_action="dry-run",
    )

    ads_historical_apply = subparsers.add_parser(
        "ads-historical-apply",
        help="Apply one exact reviewed official fullstats recovery plan.",
    )
    ads_historical_apply.add_argument("--nm-id", action="append", type=int, required=True)
    ads_historical_apply.add_argument("--target-date", action="append", required=True)
    ads_historical_apply.add_argument("--plan-file", required=True)
    ads_historical_apply.add_argument("--fingerprint", required=True)
    ads_historical_apply.add_argument("--approval-reference", required=True)
    ads_historical_apply.add_argument("--output", default="")
    ads_historical_apply.set_defaults(
        handler=run_ads_historical_recovery_command,
        ads_historical_action="apply",
    )

    ads_historical_readback = subparsers.add_parser(
        "ads-historical-readback",
        help="Read back the exact recovered ads slots and accepted closure states.",
    )
    ads_historical_readback.add_argument("--nm-id", action="append", type=int, required=True)
    ads_historical_readback.add_argument("--target-date", action="append", required=True)
    ads_historical_readback.add_argument("--output", default="")
    ads_historical_readback.set_defaults(
        handler=run_ads_historical_recovery_command,
        ads_historical_action="readback",
    )

    vitrina_incident_dry_run = subparsers.add_parser(
        "vitrina-incident-rematerialization-dry-run",
        help=(
            "Build a bounded read-only plan for derived Web Vitrina incident metrics "
            "from accepted stock snapshots."
        ),
    )
    vitrina_incident_dry_run.add_argument("--date-from", required=True)
    vitrina_incident_dry_run.add_argument("--date-to", required=True)
    vitrina_incident_dry_run.add_argument("--max-dates", type=int, default=14)
    vitrina_incident_dry_run.add_argument("--output", required=True)
    vitrina_incident_dry_run.add_argument("--plan-file", default="")
    vitrina_incident_dry_run.add_argument("--fingerprint", default="")
    vitrina_incident_dry_run.add_argument("--approval-reference", default="")
    vitrina_incident_dry_run.add_argument("--actor", default="")
    vitrina_incident_dry_run.set_defaults(
        handler=run_vitrina_incident_rematerialization_command,
        vitrina_incident_action="dry-run",
    )

    vitrina_incident_apply = subparsers.add_parser(
        "vitrina-incident-rematerialization-apply",
        help=(
            "Apply one exact reviewed bounded Web Vitrina incident-metric plan "
            "and reconcile it."
        ),
    )
    vitrina_incident_apply.add_argument("--date-from", required=True)
    vitrina_incident_apply.add_argument("--date-to", required=True)
    vitrina_incident_apply.add_argument("--max-dates", type=int, default=14)
    vitrina_incident_apply.add_argument("--plan-file", required=True)
    vitrina_incident_apply.add_argument("--fingerprint", required=True)
    vitrina_incident_apply.add_argument("--approval-reference", required=True)
    vitrina_incident_apply.add_argument("--actor", required=True)
    vitrina_incident_apply.add_argument("--output", default="")
    vitrina_incident_apply.set_defaults(
        handler=run_vitrina_incident_rematerialization_command,
        vitrina_incident_action="apply",
    )

    archival_estimate_dry_run = subparsers.add_parser(
        "warehouse-archival-estimate-dry-run",
        help="Build the exact read-only 18-SKU archival estimate correction plan.",
    )
    archival_estimate_dry_run.add_argument("--output", default="")
    archival_estimate_dry_run.set_defaults(
        handler=run_warehouse_archival_estimate_command,
        warehouse_archival_estimate_action="dry-run",
    )

    archival_estimate_apply = subparsers.add_parser(
        "warehouse-archival-estimate-apply",
        help="Apply one exact reviewed archival estimate correction plan.",
    )
    archival_estimate_apply.add_argument("--plan-file", required=True)
    archival_estimate_apply.add_argument("--fingerprint", required=True)
    archival_estimate_apply.add_argument("--approval-reference", required=True)
    archival_estimate_apply.set_defaults(
        handler=run_warehouse_archival_estimate_command,
        warehouse_archival_estimate_action="apply",
    )

    archival_estimate_readback = subparsers.add_parser(
        "warehouse-archival-estimate-readback",
        help="Read back the active exact-target archival estimate version.",
    )
    archival_estimate_readback.set_defaults(
        handler=run_warehouse_archival_estimate_command,
        warehouse_archival_estimate_action="readback",
    )

    archival_estimate_rollback = subparsers.add_parser(
        "warehouse-archival-estimate-rollback",
        help="Restore exact pre-apply derived rows while preserving version audit.",
    )
    archival_estimate_rollback.add_argument("--fingerprint", required=True)
    archival_estimate_rollback.add_argument("--reason", required=True)
    archival_estimate_rollback.set_defaults(
        handler=run_warehouse_archival_estimate_command,
        warehouse_archival_estimate_action="rollback",
    )

    warehouse_dry_run = subparsers.add_parser(
        "warehouse-opening-dry-run",
        help="Build the exact six-warehouse plan on the active hosted runtime.",
    )
    warehouse_dry_run.add_argument("--output", default="", help="Optional local JSON plan path.")
    warehouse_dry_run.set_defaults(
        handler=run_warehouse_opening_command,
        warehouse_action="dry-run",
    )

    warehouse_apply = subparsers.add_parser(
        "warehouse-opening-apply",
        help="Apply an exact reviewed warehouse plan with backup and fingerprint gate.",
    )
    warehouse_apply.add_argument("--plan-file", required=True)
    warehouse_apply.add_argument("--fingerprint", required=True)
    warehouse_apply.set_defaults(
        handler=run_warehouse_opening_command,
        warehouse_action="apply",
    )

    warehouse_readback = subparsers.add_parser(
        "warehouse-opening-readback",
        help="Read back the opening cutover and reconciliation from the active runtime.",
    )
    warehouse_readback.set_defaults(
        handler=run_warehouse_opening_command,
        warehouse_action="readback",
    )

    warehouse_diagnostic = subparsers.add_parser(
        "warehouse-opening-diagnostic",
        help="Read bounded WB discrepancy evidence on the active runtime without mutation.",
    )
    warehouse_diagnostic.add_argument("--nm-id", action="append", type=int, required=True)
    warehouse_diagnostic.set_defaults(
        handler=run_warehouse_opening_command,
        warehouse_action="diagnose-discrepancy",
    )

    warehouse_rollback = subparsers.add_parser(
        "warehouse-opening-rollback",
        help="Rollback only the opening cutover after exact fingerprint confirmation.",
    )
    warehouse_rollback.add_argument("--fingerprint", required=True)
    warehouse_rollback.set_defaults(
        handler=run_warehouse_opening_command,
        warehouse_action="rollback",
    )

    functional_dry_run = subparsers.add_parser(
        "warehouse-functional-dry-run",
        help="Refresh bounded WB supply sources and build the exact functional cutover plan.",
    )
    functional_dry_run.add_argument("--output", default="")
    functional_dry_run.set_defaults(
        handler=run_warehouse_functional_command,
        warehouse_functional_action="cutover-dry-run",
    )

    functional_apply = subparsers.add_parser(
        "warehouse-functional-apply",
        help="Apply one exact reviewed functional cutover plan.",
    )
    functional_apply.add_argument("--plan-file", required=True)
    functional_apply.add_argument("--fingerprint", required=True)
    functional_apply.set_defaults(
        handler=run_warehouse_functional_command,
        warehouse_functional_action="cutover-apply",
    )

    sqlite_archive_dry_run = subparsers.add_parser(
        "sqlite-backup-archive-dry-run",
        help=(
            "Build an immutable query-only lossless archive plan for one "
            "warehouse-functional-sync checkpoint."
        ),
    )
    sqlite_archive_dry_run.add_argument("--source", required=True)
    sqlite_archive_dry_run.add_argument(
        "--reserved-free-bytes",
        type=int,
        default=256 * 1024 * 1024,
    )
    sqlite_archive_dry_run.add_argument("--output", default="")
    sqlite_archive_dry_run.set_defaults(
        handler=run_sqlite_backup_archive_command,
        archive_apply=False,
    )

    sqlite_archive_apply = subparsers.add_parser(
        "sqlite-backup-archive-apply",
        help=(
            "Apply one exact verified lossless archive plan and remove raw "
            "bytes only after retained readback."
        ),
    )
    sqlite_archive_apply.add_argument("--source", required=True)
    sqlite_archive_apply.add_argument("--fingerprint", required=True)
    sqlite_archive_apply.add_argument(
        "--reserved-free-bytes",
        type=int,
        default=256 * 1024 * 1024,
    )
    sqlite_archive_apply.add_argument("--output", default="")
    sqlite_archive_apply.set_defaults(
        handler=run_sqlite_backup_archive_command,
        archive_apply=True,
    )

    queue_replay_dry_run = subparsers.add_parser(
        "warehouse-cost-queue-replay-dry-run",
        help=(
            "Build a query-only exact multi-invoice supplier-cost queue "
            "replay plan without Finance raw reads or a full backup."
        ),
    )
    queue_replay_dry_run.add_argument(
        "--invoice-no",
        action="append",
        required=True,
    )
    queue_replay_dry_run.add_argument("--output", default="")
    queue_replay_dry_run.set_defaults(
        handler=run_warehouse_cost_queue_replay_command,
        queue_replay_apply=False,
    )

    queue_replay_apply = subparsers.add_parser(
        "warehouse-cost-queue-replay-apply",
        help=(
            "Apply one exact reviewed multi-invoice supplier-cost queue "
            "replay with target-scoped undo and durable audit."
        ),
    )
    queue_replay_apply.add_argument(
        "--invoice-no",
        action="append",
        required=True,
    )
    queue_replay_apply.add_argument("--plan-file", required=True)
    queue_replay_apply.add_argument("--fingerprint", required=True)
    queue_replay_apply.add_argument("--output", default="")
    queue_replay_apply.set_defaults(
        handler=run_warehouse_cost_queue_replay_command,
        queue_replay_apply=True,
    )

    recovery_canary_dry_run = subparsers.add_parser(
        "warehouse-recovery-canary-dry-run",
        help="Plan the business-safe T0/T1/T2 production recovery canary.",
    )
    recovery_canary_dry_run.add_argument("--deployed-sha", required=True)
    recovery_canary_dry_run.set_defaults(
        handler=run_warehouse_recovery_canary_command,
        recovery_canary_apply=False,
    )

    recovery_canary_apply = subparsers.add_parser(
        "warehouse-recovery-canary-apply",
        help="Run the exact recovery canary against the deployed SHA.",
    )
    recovery_canary_apply.add_argument("--deployed-sha", required=True)
    recovery_canary_apply.add_argument("--fingerprint", required=True)
    recovery_canary_apply.set_defaults(
        handler=run_warehouse_recovery_canary_command,
        recovery_canary_apply=True,
    )

    recovery_retention_dry_run = subparsers.add_parser(
        "warehouse-recovery-retention-dry-run",
        help="Build an exact bounded age/count/byte retention plan.",
    )
    recovery_retention_dry_run.add_argument("--deployed-sha", required=True)
    recovery_retention_dry_run.set_defaults(
        handler=run_warehouse_recovery_retention_command,
        recovery_retention_apply=False,
    )

    recovery_retention_apply = subparsers.add_parser(
        "warehouse-recovery-retention-apply",
        help="Apply one exact audited recovery retention plan.",
    )
    recovery_retention_apply.add_argument("--deployed-sha", required=True)
    recovery_retention_apply.add_argument("--fingerprint", required=True)
    recovery_retention_apply.set_defaults(
        handler=run_warehouse_recovery_retention_command,
        recovery_retention_apply=True,
    )

    sanitation_inventory = subparsers.add_parser(
        "storage-recovery-sanitation-inventory",
        help="Inventory both canonical backup roots without mutation.",
    )
    sanitation_inventory.add_argument("--deployed-sha", required=True)
    sanitation_inventory.set_defaults(
        handler=run_storage_recovery_sanitation_command,
        storage_sanitation_action="inventory",
    )

    sanitation_plan = subparsers.add_parser(
        "storage-recovery-sanitation-plan",
        help="Build the next exact action for one allowlisted backup family.",
    )
    sanitation_plan.add_argument("--deployed-sha", required=True)
    sanitation_plan.add_argument(
        "--root", dest="sanitation_root", choices=("root", "backup"), required=True
    )
    sanitation_plan.add_argument("--family", required=True)
    sanitation_plan.add_argument(
        "--reserved-free-bytes", type=int, default=256 * 1024 * 1024
    )
    sanitation_plan.set_defaults(
        handler=run_storage_recovery_sanitation_command,
        storage_sanitation_action="plan",
    )

    sanitation_apply = subparsers.add_parser(
        "storage-recovery-sanitation-apply",
        help="Apply/resume one exact audited family sanitation action.",
    )
    sanitation_apply.add_argument("--deployed-sha", required=True)
    sanitation_apply.add_argument(
        "--root", dest="sanitation_root", choices=("root", "backup"), required=True
    )
    sanitation_apply.add_argument("--family", required=True)
    sanitation_apply.add_argument("--fingerprint", required=True)
    sanitation_apply.add_argument(
        "--reserved-free-bytes", type=int, default=256 * 1024 * 1024
    )
    sanitation_apply.set_defaults(
        handler=run_storage_recovery_sanitation_command,
        storage_sanitation_action="apply",
    )

    sanitation_submit = subparsers.add_parser(
        "storage-recovery-sanitation-submit",
        help=(
            "Persist one exact plan/apply request and start the fixed detached "
            "sanitation worker."
        ),
    )
    sanitation_submit.add_argument("--deployed-sha", required=True)
    sanitation_submit.add_argument("--job-id", required=True)
    sanitation_submit.add_argument(
        "--operation",
        choices=("plan", "apply"),
        required=True,
    )
    sanitation_submit.add_argument(
        "--root",
        dest="sanitation_root",
        choices=("root", "backup"),
        required=True,
    )
    sanitation_submit.add_argument("--family", required=True)
    sanitation_submit.add_argument("--fingerprint", default="")
    sanitation_submit.add_argument(
        "--reserved-free-bytes",
        type=int,
        default=256 * 1024 * 1024,
    )
    sanitation_submit.set_defaults(
        handler=run_storage_recovery_sanitation_job_command,
        sanitation_job_action="submit",
    )

    sanitation_status = subparsers.add_parser(
        "storage-recovery-sanitation-status",
        help="Read one durable detached sanitation job result without mutation.",
    )
    sanitation_status.add_argument("--deployed-sha", required=True)
    sanitation_status.add_argument("--job-id", required=True)
    sanitation_status.set_defaults(
        handler=run_storage_recovery_sanitation_job_command,
        sanitation_job_action="status",
    )

    promo_gc_dry_run = subparsers.add_parser(
        "promo-archive-gc-dry-run",
        help="Build the штатный exact Promo artifact GC plan.",
    )
    promo_gc_dry_run.add_argument("--deployed-sha", required=True)
    promo_gc_dry_run.set_defaults(
        handler=run_promo_archive_gc_command,
        promo_gc_apply=False,
    )

    promo_gc_apply = subparsers.add_parser(
        "promo-archive-gc-apply",
        help="Apply/resume the exact audited Promo artifact GC plan.",
    )
    promo_gc_apply.add_argument("--deployed-sha", required=True)
    promo_gc_apply.add_argument("--fingerprint", required=True)
    promo_gc_apply.set_defaults(
        handler=run_promo_archive_gc_command,
        promo_gc_apply=True,
    )

    functional_failed_backup_cleanup_dry_run = subparsers.add_parser(
        "warehouse-functional-failed-backup-cleanup-dry-run",
        help="Fingerprint one proven-invalid partial functional-cutover backup.",
    )
    functional_failed_backup_cleanup_dry_run.add_argument("--source", required=True)
    functional_failed_backup_cleanup_dry_run.set_defaults(
        handler=run_warehouse_functional_failed_backup_cleanup_command,
        cleanup_apply=False,
    )

    functional_failed_backup_cleanup_apply = subparsers.add_parser(
        "warehouse-functional-failed-backup-cleanup-apply",
        help="Remove one exact proven-invalid partial functional-cutover backup.",
    )
    functional_failed_backup_cleanup_apply.add_argument("--source", required=True)
    functional_failed_backup_cleanup_apply.add_argument("--fingerprint", required=True)
    functional_failed_backup_cleanup_apply.set_defaults(
        handler=run_warehouse_functional_failed_backup_cleanup_command,
        cleanup_apply=True,
    )

    functional_readback = subparsers.add_parser(
        "warehouse-functional-readback",
        help="Read functional cutover, active version and reconciliation.",
    )
    functional_readback.set_defaults(
        handler=run_warehouse_functional_command,
        warehouse_functional_action="readback",
    )

    functional_backup = subparsers.add_parser(
        "warehouse-functional-backup",
        help="Create one coherent integrity-checked pre-sync production backup.",
    )
    functional_backup.set_defaults(
        handler=run_warehouse_functional_command,
        warehouse_functional_action="backup",
    )

    functional_sync = subparsers.add_parser(
        "warehouse-functional-sync",
        help="Run one bounded official WB synchronization and atomic calculation.",
    )
    functional_sync.set_defaults(
        handler=run_warehouse_functional_command,
        warehouse_functional_action="manual-sync",
    )

    functional_sync_dry_run = subparsers.add_parser(
        "warehouse-functional-sync-dry-run",
        help="Build a fresh mutation-free reviewed bounded warehouse recovery plan.",
    )
    functional_sync_dry_run.add_argument("--output", default="")
    functional_sync_dry_run.set_defaults(
        handler=run_warehouse_functional_command,
        warehouse_functional_action="sync-dry-run",
    )

    functional_sync_apply = subparsers.add_parser(
        "warehouse-functional-sync-apply",
        help="Apply one exact reviewed bounded warehouse recovery plan.",
    )
    functional_sync_apply.add_argument("--plan-file", required=True)
    functional_sync_apply.add_argument("--fingerprint", required=True)
    functional_sync_apply.set_defaults(
        handler=run_warehouse_functional_command,
        warehouse_functional_action="sync-apply",
    )

    functional_maintenance = subparsers.add_parser(
        "warehouse-functional-maintenance",
        help=(
            "Inspect, hold or exactly restore only the hourly warehouse timer "
            "through the audited maintenance boundary."
        ),
    )
    functional_maintenance.add_argument(
        "action",
        choices=("status", "hold", "restore"),
    )
    functional_maintenance.add_argument(
        "--allow-outer-hold-recovery",
        action="store_true",
        help=(
            "Reapply the original warehouse timer baseline only for the exact "
            "unconfirmed outer-hold rollback footprint."
        ),
    )
    functional_maintenance.set_defaults(
        handler=run_warehouse_functional_maintenance_command,
    )

    business_data_maintenance = subparsers.add_parser(
        "business-data-maintenance",
        help=(
            "Inspect or establish one audited quiet window across all repo-owned "
            "automatic business-data writers and runtime schedules."
        ),
    )
    business_data_maintenance.add_argument(
        "action",
        choices=(
            "status",
            "hold",
            "restore",
            "restore-continuity-status",
            "set-process",
            "barrier-status",
            "barrier-acquire",
            "barrier-confirm",
            "barrier-restoring",
            "barrier-release",
            "barrier-abort",
        ),
    )
    business_data_maintenance.add_argument(
        "--expected-revision",
        type=int,
        help="Exact owner-policy revision required for restore or set-process.",
    )
    business_data_maintenance.add_argument(
        "--process-key",
        default="",
        help="Allowlisted process key for set-process.",
    )
    business_data_maintenance.add_argument(
        "--desired",
        choices=("on", "off"),
        default="",
        help="Desired owner state for set-process.",
    )
    business_data_maintenance.add_argument(
        "--actor",
        default="repo_owned_cli",
        help="Audited actor for set-process.",
    )
    business_data_maintenance.add_argument(
        "--reason",
        default="",
        help="Audited reason for maintenance or barrier transitions.",
    )
    business_data_maintenance.add_argument(
        "--window-id",
        default="",
        help="Exact bounded maintenance window identity.",
    )
    business_data_maintenance.add_argument(
        "--window-kind",
        choices=("snapshot", "final_cutover", "rollback_drill"),
        default="snapshot",
        help="Bounded maintenance window kind.",
    )
    business_data_maintenance.add_argument(
        "--plan-fingerprint",
        default="",
        help="Exact sha256 fingerprint approved for this window.",
    )
    business_data_maintenance.add_argument(
        "--approval-reference",
        default="",
        help="Exact human approval reference for barrier acquisition.",
    )
    business_data_maintenance.add_argument(
        "--allow-pre-hold-service-continuity",
        action="store_true",
        help=(
            "Restore an unconfirmed acquiring window while an exact "
            "pre-hold service generation continues unchanged."
        ),
    )
    business_data_maintenance.set_defaults(
        handler=run_business_data_maintenance_command,
    )

    maintenance_restore_submit = subparsers.add_parser(
        "business-data-maintenance-restore-submit",
        help=(
            "Persist and start one exact restore in the fixed detached "
            "maintenance worker."
        ),
    )
    maintenance_restore_submit.add_argument("--deployed-sha", required=True)
    maintenance_restore_submit.add_argument("--job-id", required=True)
    maintenance_restore_submit.add_argument(
        "--expected-revision",
        type=int,
        required=True,
    )
    maintenance_restore_submit.add_argument("--window-id", required=True)
    maintenance_restore_submit.add_argument(
        "--plan-fingerprint",
        required=True,
    )
    maintenance_restore_submit.add_argument(
        "--service-continuity-fingerprint",
        required=True,
    )
    maintenance_restore_submit.add_argument("--actor", required=True)
    maintenance_restore_submit.add_argument("--reason", required=True)
    maintenance_restore_submit.add_argument(
        "--allow-pre-hold-service-continuity",
        action="store_true",
        help=(
            "Allow only the exact audited pre-hold service generation "
            "during this unconfirmed-window restore."
        ),
    )
    maintenance_restore_submit.set_defaults(
        handler=run_business_data_maintenance_restore_job_command,
        maintenance_restore_job_action="submit",
    )

    maintenance_restore_status = subparsers.add_parser(
        "business-data-maintenance-restore-status",
        help=(
            "Read one exact durable maintenance restore result without "
            "changing production."
        ),
    )
    maintenance_restore_status.add_argument("--deployed-sha", required=True)
    maintenance_restore_status.add_argument("--job-id", required=True)
    maintenance_restore_status.set_defaults(
        handler=run_business_data_maintenance_restore_job_command,
        maintenance_restore_job_action="status",
    )

    functional_emergency_dry_run = subparsers.add_parser(
        "warehouse-functional-emergency-dry-run",
        help="Build an emergency full rebuild plan from persisted local sources only.",
    )
    functional_emergency_dry_run.add_argument("--output", default="")
    functional_emergency_dry_run.set_defaults(
        handler=run_warehouse_functional_command,
        warehouse_functional_action="emergency-dry-run",
    )

    functional_emergency_apply = subparsers.add_parser(
        "warehouse-functional-emergency-apply",
        help="Apply an exact reviewed local-source emergency rebuild plan.",
    )
    functional_emergency_apply.add_argument("--plan-file", required=True)
    functional_emergency_apply.add_argument("--fingerprint", required=True)
    functional_emergency_apply.set_defaults(
        handler=run_warehouse_functional_command,
        warehouse_functional_action="emergency-apply",
    )

    functional_economics_dry_run = subparsers.add_parser(
        "warehouse-functional-economics-dry-run",
        help="Build the targeted 01.07 functional WB cost/Proxy publication plan.",
    )
    functional_economics_dry_run.add_argument("--output", default="")
    functional_economics_dry_run.set_defaults(
        handler=run_warehouse_functional_command,
        warehouse_functional_action="economics-backfill-dry-run",
    )

    functional_economics_apply = subparsers.add_parser(
        "warehouse-functional-economics-apply",
        help="Apply the exact targeted functional WB cost/Proxy publication plan.",
    )
    functional_economics_apply.add_argument("--plan-file", required=True)
    functional_economics_apply.add_argument("--fingerprint", required=True)
    functional_economics_apply.set_defaults(
        handler=run_warehouse_functional_command,
        warehouse_functional_action="economics-backfill-apply",
    )

    functional_certification_dry_run = subparsers.add_parser(
        "warehouse-functional-supplier-certification-dry-run",
        help="Build an append-only active-version supplier certification replay plan.",
    )
    functional_certification_dry_run.add_argument("--output", default="")
    functional_certification_dry_run.set_defaults(
        handler=run_warehouse_functional_command,
        warehouse_functional_action="supplier-certification-dry-run",
    )

    functional_certification_apply = subparsers.add_parser(
        "warehouse-functional-supplier-certification-apply",
        help="Apply one exact reviewed supplier certification replay plan.",
    )
    functional_certification_apply.add_argument("--plan-file", required=True)
    functional_certification_apply.add_argument("--fingerprint", required=True)
    functional_certification_apply.set_defaults(
        handler=run_warehouse_functional_command,
        warehouse_functional_action="supplier-certification-apply",
    )

    functional_certification_rollback = subparsers.add_parser(
        "warehouse-functional-supplier-certification-rollback",
        help="Append an exact rollback tombstone for one supplier certification replay.",
    )
    functional_certification_rollback.add_argument("--fingerprint", required=True)
    functional_certification_rollback.add_argument("--reason", required=True)
    functional_certification_rollback.set_defaults(
        handler=run_warehouse_functional_command,
        warehouse_functional_action="supplier-certification-rollback",
    )

    functional_enable_hourly = subparsers.add_parser(
        "warehouse-functional-enable-hourly",
        help="Enable the repo-owned hourly timer only after successful cutover readback.",
    )
    functional_enable_hourly.set_defaults(
        handler=run_warehouse_functional_command,
        warehouse_functional_action="enable-hourly",
    )

    functional_rollback = subparsers.add_parser(
        "warehouse-functional-rollback",
        help="Disable hourly sync and rollback only functional derived state.",
    )
    functional_rollback.add_argument("--fingerprint", required=True)
    functional_rollback.set_defaults(
        handler=run_warehouse_functional_command,
        warehouse_functional_action="rollback",
    )

    warehouse_ui_flow = subparsers.add_parser(
        "warehouse-ui-flow",
        help="Run the authorized read-only production Playwright flow for all six warehouses.",
    )
    warehouse_ui_flow.add_argument("--evidence-dir", required=True)
    warehouse_ui_flow.add_argument(
        "--deployed-sha",
        default="",
        help=(
            "Exact deployed commit required by the warehouse recovery "
            "acceptance profile."
        ),
    )
    warehouse_ui_flow.add_argument("--timeout-seconds", type=float, default=180.0)
    warehouse_ui_flow.add_argument("--headed", action="store_true")
    warehouse_ui_flow.add_argument(
        "--acceptance-profile",
        choices=(
            "warehouse_chain_recovery_20260719",
            "warehouse_cost_transparency_20260720",
            "warehouse_recovery_policy_20260726",
            "vitrina_incident_provisional_20260727",
        ),
        default=None,
        help="Optional migration-specific immutable controls; the default Flow remains reusable.",
    )
    warehouse_ui_flow.set_defaults(handler=run_warehouse_ui_flow_command)

    finance_ui_flow = subparsers.add_parser(
        "finance-ui-flow",
        help="Run authenticated read-only production Playwright acceptance for Finance and Partner reports.",
    )
    finance_ui_flow.add_argument("--evidence-dir", required=True)
    finance_ui_flow.add_argument("--timeout-seconds", type=float, default=180.0)
    finance_ui_flow.add_argument("--headed", action="store_true")
    finance_ui_flow.add_argument(
        "--deployed-sha",
        default="",
        help="Exact deployed commit expected for the machine-readable evidence.",
    )
    finance_ui_flow.set_defaults(handler=run_finance_ui_flow_command)

    autoanswers_ui_flow = subparsers.add_parser(
        "autoanswers-ui-flow",
        help="Run authenticated production Playwright acceptance for WB autoanswers.",
    )
    autoanswers_ui_flow.add_argument("--evidence-dir", required=True)
    autoanswers_ui_flow.add_argument("--timeout-seconds", type=float, default=180.0)
    autoanswers_ui_flow.add_argument("--headed", action="store_true")
    autoanswers_ui_flow.add_argument(
        "--expected-state",
        choices=("off-force", "off-unforced", "manual", "auto_all"),
        default="off-force",
    )
    autoanswers_ui_flow.add_argument(
        "--verify-limit-save",
        action="store_true",
        help=(
            "Opt in to one safe same-value limit save with exact readback; "
            "the default flow remains read-only."
        ),
    )
    autoanswers_ui_flow.set_defaults(handler=run_autoanswers_ui_flow_command)

    autoanswers_store_rollback_plan = subparsers.add_parser(
        "autoanswers-store-rollback-plan",
        help=(
            "Plan a read-only export of the isolated Autoanswers store back "
            "to the retained legacy tables before an older-code rollback."
        ),
    )
    autoanswers_store_rollback_plan.set_defaults(
        handler=run_autoanswers_store_rollback_command,
        rollback_apply=False,
        fingerprint="",
    )

    autoanswers_store_rollback_apply = subparsers.add_parser(
        "autoanswers-store-rollback-apply",
        help=(
            "Under the repo-owned quiet window, back up and reconcile current "
            "isolated Autoanswers data into the legacy tables."
        ),
    )
    autoanswers_store_rollback_apply.add_argument(
        "--fingerprint",
        required=True,
    )
    autoanswers_store_rollback_apply.set_defaults(
        handler=run_autoanswers_store_rollback_command,
        rollback_apply=True,
    )

    sqlite_contention_ui_flow = subparsers.add_parser(
        "sqlite-contention-ui-flow",
        help=(
            "Run production contention acceptance while a timer-owned "
            "Autoanswers writer is observed."
        ),
    )
    sqlite_contention_ui_flow.add_argument("--evidence-dir", required=True)
    sqlite_contention_ui_flow.add_argument(
        "--timeout-seconds",
        type=float,
        default=180.0,
    )
    sqlite_contention_ui_flow.add_argument(
        "--background-wait-seconds",
        type=float,
        default=180.0,
    )
    sqlite_contention_ui_flow.add_argument("--headed", action="store_true")
    sqlite_contention_ui_flow.add_argument(
        "--deployed-sha",
        required=True,
        help="Exact deployed commit expected for the machine-readable evidence.",
    )
    sqlite_contention_ui_flow.set_defaults(
        handler=run_sqlite_contention_ui_flow_command
    )

    return parser


def _add_probe_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--as-of-date", default=None, help="Optional as_of_date for plan/status/deep refresh probes.")
    parser.add_argument(
        "--include-refresh",
        action="store_true",
        help="Run explicit deep POST /v1/sheet-vitrina-v1/refresh probe. Canonical probes skip this heavy mutating route by default.",
    )
    parser.add_argument(
        "--skip-refresh",
        action="store_true",
        help="Compatibility flag: force skipping POST /v1/sheet-vitrina-v1/refresh even if --include-refresh is present.",
    )
    parser.add_argument(
        "--include-feedbacks",
        action="store_true",
        help="Also probe GET /v1/sheet-vitrina-v1/feedbacks with a bounded date query.",
    )
    parser.add_argument("--feedbacks-date-from", default=None, help="YYYY-MM-DD start date for feedbacks probe.")
    parser.add_argument("--feedbacks-date-to", default=None, help="YYYY-MM-DD end date for feedbacks probe.")
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=180.0,
        help="HTTP request timeout and remote loopback transport timeout in seconds.",
    )


def _probe_include_refresh(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "include_refresh", False)) and not bool(getattr(args, "skip_refresh", False))


def _probe_include_wb_warehouse_exclusion_options(target: HostedRuntimeTarget) -> bool:
    # The endpoint is intentionally backed by a fresh official WB read. Local
    # contract smokes have no production WB token; active and rollback targets
    # must still prove the complete payload.
    return str(target.target_status or "").strip().lower() != "local_test"


def _probe_include_auto_updates_status(target: HostedRuntimeTarget) -> bool:
    # The status readback inspects the real systemd/schedule control plane.
    # Tokenless local contract smokes prove route publication separately.
    return str(target.target_status or "").strip().lower() != "local_test"


def _probe_auth_summary(auth_cookie: str | None) -> dict[str, Any]:
    return {
        "mode": "app_session_cookie" if auth_cookie else "none",
        "cookie_configured": bool(auth_cookie),
    }


def _build_probe_auth_cookie(target: HostedRuntimeTarget, *, timeout_seconds: float) -> str | None:
    """Build an app-session cookie for auth-protected health probes without logging secrets."""

    timeout_seconds = _validate_probe_timeout_seconds(timeout_seconds)
    _validate_production_target_identity(target, action="auth-cookie")
    if not target.environment_file:
        return None
    if target.ssh_destination:
        result = subprocess.run(
            _ssh_command() + [target.ssh_destination, "python3", "-"],
            input=_build_remote_auth_cookie_script(target.environment_file),
            text=True,
            capture_output=True,
            cwd=ROOT,
            timeout=min(timeout_seconds, 30.0),
            check=False,
        )
        if result.returncode != 0:
            return None
        cookie = result.stdout.strip()
        return cookie if cookie.startswith("wb_core_web_session=") else None
    env_values = _read_env_file_values(Path(target.environment_file))
    return _build_web_auth_cookie_from_env(env_values)


def _build_remote_auth_cookie_script(environment_file: str) -> str:
    return f"""import base64
import hashlib
import hmac
import json
import shlex
import time
from pathlib import Path

ENV_FILE = {environment_file!r}
COOKIE_NAME = "wb_core_web_session"


def _read_env_file(path):
    values = {{}}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        try:
            parsed = shlex.split(value, posix=True)
        except ValueError:
            parsed = []
        values[key] = parsed[0] if parsed else value.strip('"').strip("'")
    return values


def _b64(value):
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


env = _read_env_file(ENV_FILE)
username = str(env.get("WB_CORE_WEB_AUTH_USERNAME") or "").strip()
session_secret = str(env.get("WB_CORE_WEB_AUTH_SESSION_SECRET") or "").strip()
if not username or not session_secret:
    raise SystemExit(0)
try:
    max_age = int(env.get("WB_CORE_WEB_AUTH_SESSION_MAX_AGE_SECONDS") or 600)
except ValueError:
    max_age = 600
max_age = max(60, min(max_age, 600))
payload = _b64(json.dumps({{"u": username, "exp": int(time.time()) + max_age}}, separators=(",", ":")).encode("utf-8"))
signature = _b64(hmac.new(session_secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest())
print(f"{{COOKIE_NAME}}={{payload}}.{{signature}}")
"""


def _read_env_file_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        try:
            parsed = shlex.split(value, posix=True)
        except ValueError:
            parsed = []
        values[key] = parsed[0] if parsed else value.strip('"').strip("'")
    return values


def _build_web_auth_cookie_from_env(env_values: dict[str, str]) -> str | None:
    username = str(env_values.get("WB_CORE_WEB_AUTH_USERNAME") or "").strip()
    session_secret = str(env_values.get("WB_CORE_WEB_AUTH_SESSION_SECRET") or "").strip()
    if not username or not session_secret:
        return None
    try:
        max_age = int(env_values.get("WB_CORE_WEB_AUTH_SESSION_MAX_AGE_SECONDS") or 600)
    except ValueError:
        max_age = 600
    max_age = max(60, min(max_age, 600))
    payload = _base64url_encode(json.dumps({"u": username, "exp": int(time.time()) + max_age}, separators=(",", ":")).encode("utf-8"))
    signature = _base64url_encode(
        hmac.new(session_secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest()
    )
    return f"wb_core_web_session={payload}.{signature}"


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _collect_http_probe(
    *,
    name: str,
    method: str,
    url: str,
    timeout_seconds: float,
    json_payload: dict[str, Any] | None = None,
    auth_cookie: str | None = None,
) -> dict[str, Any]:
    timeout_seconds = _validate_probe_timeout_seconds(timeout_seconds)
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
    if auth_cookie:
        request.add_header("Cookie", auth_cookie)
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
            "Поставки",
            "Отчёты",
            "Отзывы",
            "Загрузить",
            "обн:",
            "свеж:",
            "Загрузка данных",
            "Действия и состояния",
            "data-seller-top-session",
            "Проверить сессию",
            'data-unified-tab-button="vitrina"',
            'data-unified-tab-button="factory-order"',
            'data-unified-tab-button="warehouses"',
            'data-unified-tab-button="reports"',
            'data-unified-tab-button="feedbacks"',
            'data-operator-embed-frame="factory-order"',
            'data-operator-embed-frame="reports"',
            DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
            DEFAULT_SHEET_FEEDBACKS_PATH,
            "/v1/sheet-vitrina-v1/feedbacks/export.xlsx",
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
            "Равномерный годовой план",
            "Прогноз к концу договорного периода при текущем темпе",
            "Исторические данные для отчёта",
            "planReportApplyButton",
            "planReportAnnualEvenCheckbox",
            "planReportProjectionTable",
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

    if route == "operator_factory_order":
        body = str(result.get("body_excerpt", ""))
        tokens = [
            "Поставки",
            "Общий вход для двух расчётов",
            "Заказ на фабрике",
            "Поставка на Wildberries",
            "Остатки ФФ",
            "Направления для расчёта поставки",
            "Скачать все рекомендации",
            "Сводка по направлениям поставки",
            "Рекомендовано / к поставке",
            "Счёт CNY",
            "Конвертации RUB → CNY",
            "data-cny-delete-document",
            "Документ будет удалён. Остаток CNY, рублёвая стоимость остатка, средний курс",
            DEFAULT_FACTORY_ORDER_STATUS_PATH,
            DEFAULT_CNY_ACCOUNT_DOCUMENTS_PATH,
            DEFAULT_WB_REGIONAL_STATUS_PATH,
            DEFAULT_WB_REGIONAL_RECOMMENDATIONS_ZIP_PATH,
            'data-supply-section-button="regional"',
        ]
        missing_tokens = [token for token in tokens if token not in body]
        evaluation["ok"] = status == 200 and "text/html" in content_type and not missing_tokens
        evaluation["detail"] = (
            "operator factory-order embedded panel ok"
            if evaluation["ok"]
            else f"expected 200 text/html with factory/regional supply tokens, missing={missing_tokens}"
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
            "Загрузить",
            "обн:",
            "свеж:",
            DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
            route_paths["SHEET_VITRINA_OPERATOR_UI_PATH"],
            "surface=page_composition",
            "web_vitrina_page_composition",
            "data-global-progress",
            "data-filter-controls",
            "data-loading-table",
            "data-loading-table-head",
            "data-loading-table-body",
            "Действия и состояния",
            "Загрузка данных",
            "Обновить группу",
            "data-seller-top-session",
            "Проверить сессию",
            "Установить сессию",
            "Отзывы",
            "Склады и себестоимость",
            'data-unified-tab-panel="warehouses"',
            DEFAULT_WAREHOUSES_PATH,
            "Загрузить отзывы",
            "AI-промпт разбора",
            "AI-разбор отзывов",
            DEFAULT_SHEET_FEEDBACKS_PATH,
            "/v1/sheet-vitrina-v1/feedbacks/export.xlsx",
            "/v1/sheet-vitrina-v1/feedbacks/ai-prompt",
            "/v1/sheet-vitrina-v1/feedbacks/ai-analyze",
            "Лог",
        ]
        missing_tokens = [token for token in tokens if token not in body]
        removed_tokens = [
            token
            for token in (
                "data-update-summary",
                "data-retry-button",
                "data-status-badge",
                "data-session-recovery-start",
                "data-session-launcher",
                "JSON Connect",
                "Обновление данных",
            )
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

    if route == "instructions_page":
        evaluation["ok"] = status == 200 and "text/html" in content_type
        evaluation["detail"] = (
            "operator instructions page ok"
            if evaluation["ok"]
            else "expected 200 text/html for authorized operator instructions"
        )
        return evaluation

    if route == "supplier_page":
        body = str(result.get("body_excerpt", ""))
        tokens = [
            "订单登记表 / Order registry / Реестр заказов",
            "新增订单 / Add order / Добавить заказ",
            "计划出货日期 / Planned shipment date / Плановая дата отгрузки",
            "实际出货日期 / Actual shipment date / Фактическая дата отгрузки",
            "实际入仓日期 / Actual ФФ acceptance date / Фактическая дата приёмки на ФФ",
            DEFAULT_SUPPLIER_SHIPMENTS_PATH,
            DEFAULT_SUPPLIER_SHIPMENTS_PATH + "/parse",
        ]
        missing_tokens = [token for token in tokens if token not in body]
        evaluation["ok"] = status == 200 and "text/html" in content_type and not missing_tokens
        evaluation["detail"] = (
            "supplier shipments page ok"
            if evaluation["ok"]
            else f"expected 200 text/html with supplier shipment tokens, missing={missing_tokens}"
        )
        return evaluation

    if route in {
        "web_vitrina_page_composition",
        "web_vitrina_user_config",
        "web_vitrina_business_projection_status",
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
        if route == "web_vitrina_user_config":
            json_body = result.get("json_body") or {}
            evaluation["ok"] = (
                status == 200
                and "application/json" in content_type
                and json_body.get("config_key") == "metric_presentation"
                and json_body.get("canonical_store") == "server_runtime_user_config"
                and json_body.get("status") in {"missing", "ok"}
            )
            evaluation["detail"] = (
                "web-vitrina user config route ok"
                if evaluation["ok"]
                else "expected 200 JSON user config payload"
            )
            return evaluation
        if route == "web_vitrina_business_projection_status":
            json_body = result.get("json_body") or {}
            required_keys = {
                "contract_name",
                "contract_version",
                "status",
                "revision_no",
                "revision_id",
                "queue_counts",
                "outbox_counts",
                "updating",
                "latest_failure",
            }
            evaluation["ok"] = (
                status == 200
                and "application/json" in content_type
                and json_body.get("contract_name")
                == "warehouse_business_projection"
                and json_body.get("contract_version") == 1
                and not (required_keys - set(json_body))
            )
            evaluation["detail"] = (
                "web-vitrina business projection status route ok"
                if evaluation["ok"]
                else "expected 200 JSON business projection status payload"
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

    if route == "wb_regional_recommendations_zip" and status == 200:
        evaluation["ok"] = "application/zip" in content_type
        evaluation["detail"] = (
            "wb-regional recommendations ZIP route returned archive"
            if evaluation["ok"]
            else "expected application/zip content-type for successful recommendations ZIP route"
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

    if route == "own_product_capital_status":
        evaluation["ok"], evaluation["detail"] = _validate_json_result(
            status,
            payload,
            success_keys=[
                "contract_name",
                "source",
                "event_count",
                "underaccepted_wb",
                "blockers",
            ],
            error_keys=["error"],
        )
        if evaluation["ok"] and payload.get("contract_name") != "sheet_vitrina_v1_own_product_capital":
            evaluation["ok"] = False
            evaluation["detail"] = "unexpected own-product-capital status contract"
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

    if route == "supplier_shipments_list":
        evaluation["ok"], evaluation["detail"] = _validate_json_result(
            status,
            payload,
            success_keys=[
                "contract_name",
                "status",
                "shipments",
            ],
        )
        if evaluation["ok"] and payload.get("contract_name") != "sheet_vitrina_v1_supplier_shipments":
            evaluation["ok"] = False
            evaluation["detail"] = (
                "expected sheet_vitrina_v1_supplier_shipments contract, "
                f"got {payload.get('contract_name')!r}"
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

    if route == "wb_warehouse_exclusion_options":
        evaluation["ok"], evaluation["detail"] = _validate_json_result(
            status,
            payload,
            success_keys=[
                "snapshot_date",
                "fetched_at",
                "pagination_complete",
                "raw_rows_digest",
                "options",
            ],
        )
        return evaluation

    if route == "wb_warehouse_exclusion_settings":
        evaluation["ok"], evaluation["detail"] = _validate_json_result(
            status,
            payload,
            success_keys=[
                "revision",
                "excluded_wb_warehouse_ids",
                "canonical_store",
            ],
        )
        return evaluation

    if route == "auto_updates_status":
        evaluation["ok"], evaluation["detail"] = _validate_json_result(
            status,
            payload,
            success_keys=[
                "schema_version",
                "revision",
                "master_desired",
                "overall_status",
                "processes",
                "drift_processes",
                "unknown_processes",
            ],
        )
        return evaluation

    if route == "wb_supplies_list":
        evaluation["ok"], evaluation["detail"] = _validate_json_result(
            status,
            payload,
            success_keys=[
                "contract_name",
                "contract_version",
                "meta",
                "filters",
                "summary",
                "pagination",
                "schema",
                "rows",
            ],
        )
        if evaluation["ok"] and payload.get("contract_name") != "sheet_vitrina_v1_wb_supplies":
            evaluation["ok"] = False
            evaluation["detail"] = f"expected sheet_vitrina_v1_wb_supplies contract, got {payload.get('contract_name')!r}"
        return evaluation

    if route == "warehouses_overview":
        evaluation["ok"], evaluation["detail"] = _validate_json_result(
            status,
            payload,
            success_keys=["contract_name", "contract_version", "status", "warehouses"],
        )
        if evaluation["ok"] and (
            payload.get("contract_name") not in {
                "sheet_vitrina_v1_warehouse_functional",
                "sheet_vitrina_v1_warehouses",
            }
            or len(payload.get("warehouses") or []) != 6
        ):
            evaluation["ok"] = False
            evaluation["detail"] = "expected the canonical six-warehouse overview contract"
        return evaluation

    if route == "warehouse_ff":
        truncated = bool(result.get("body_truncated"))
        evaluation["ok"], evaluation["detail"] = _validate_json_result(
            status,
            payload,
            success_keys=(
                ["contract_name", "contract_version", "status", "warehouse", "probe_shape"]
                if truncated
                else [
                    "contract_name",
                    "contract_version",
                    "status",
                    "warehouse",
                    "balances",
                    "documents",
                ]
            ),
        )
        warehouse = payload.get("warehouse")
        probe_shape = payload.get("probe_shape")
        if truncated:
            warehouse = _object_from_truncated_json(
                str(result.get("body_excerpt") or ""),
                key="warehouse",
            )
            probe_shape = _object_from_truncated_json(
                str(result.get("body_excerpt") or ""),
                key="probe_shape",
            )
        warehouse_key = (
            str(warehouse.get("warehouse_key") or "")
            if isinstance(warehouse, Mapping)
            else ""
        )
        if evaluation["ok"] and warehouse_key != "ff":
            evaluation["ok"] = False
            evaluation["detail"] = "expected canonical FF warehouse detail"
        if evaluation["ok"] and truncated and (
            not isinstance(probe_shape, Mapping)
            or str(probe_shape.get("warehouse_key") or "") != "ff"
            or set(probe_shape.get("required_collections") or [])
            != {"balances", "documents"}
        ):
            evaluation["ok"] = False
            evaluation["detail"] = "expected bounded FF warehouse probe shape"
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
        validation_reached = (
            status == 400
            and "source_group_id is required" in error_text
        )
        maintenance_fail_closed = bool(
            status == 423
            and payload.get("contract_name")
            == "wb_core_business_data_write_barrier_v1"
            and payload.get("status") == "blocked"
            and payload.get("active") is True
            and str(payload.get("phase") or "")
            in {"acquiring", "held", "restoring"}
            and payload.get("code") == "business_data_maintenance"
            and payload.get("retryable") is True
            and payload.get("attempt_audited") is True
            and bool(str(payload.get("window_id") or ""))
        )
        evaluation["ok"] = validation_reached or maintenance_fail_closed
        if validation_reached:
            evaluation["detail"] = (
                "web-vitrina group-refresh route is publicly published and "
                "reached app-level validation"
            )
        elif maintenance_fail_closed:
            evaluation["detail"] = (
                "web-vitrina group-refresh route is published and its harmless "
                "POST probe was correctly blocked by the audited maintenance "
                "barrier"
            )
        else:
            evaluation["detail"] = (
                "expected app-level JSON 400 validation or exact audited "
                "maintenance-barrier 423"
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

    if route == "wb_regional_recommendations_zip":
        error_text = str(payload.get("error", "") or "")
        evaluation["ok"] = status == 422 and bool(error_text)
        evaluation["detail"] = (
            "wb-regional recommendations ZIP route published with truthful 422 before calculation"
            if evaluation["ok"]
            else "expected 200 ZIP or 422 JSON error for recommendations ZIP route"
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
    chunks: list[bytes] = []
    remaining = PROBE_BODY_LIMIT_BYTES + 1
    while remaining > 0:
        chunk = response.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    body_truncated = len(raw) > PROBE_BODY_LIMIT_BYTES
    if body_truncated:
        raw = raw[:PROBE_BODY_LIMIT_BYTES]
    return raw.decode("utf-8", errors="replace"), body_truncated, len(raw)


def _synthetic_payload_from_truncated_json(body: str) -> dict[str, Any]:
    return {
        match.group(1): True
        for match in re.finditer(r'"([^"\\]+)"\s*:', body)
    }


def _object_from_truncated_json(body: str, *, key: str) -> Mapping[str, Any] | None:
    """Decode one complete object value retained inside a bounded JSON prefix."""

    marker = re.search(rf'"{re.escape(key)}"\s*:\s*', body)
    if marker is None:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(body, marker.end())
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _collect_remote_loopback_surface(
    target: HostedRuntimeTarget,
    *,
    as_of_date: str | None,
    include_refresh: bool,
    include_feedbacks: bool,
    feedbacks_date_from: str | None,
    feedbacks_date_to: str | None,
    timeout_seconds: float,
    auth_cookie: str | None = None,
) -> list[dict[str, Any]]:
    timeout_seconds = _validate_probe_timeout_seconds(timeout_seconds)
    script = _build_remote_probe_script(
        base_url=target.loopback_base_url,
        route_paths=target.route_paths,
        as_of_date=as_of_date,
        include_refresh=include_refresh,
        include_feedbacks=include_feedbacks,
        feedbacks_date_from=feedbacks_date_from,
        feedbacks_date_to=feedbacks_date_to,
        timeout_seconds=timeout_seconds,
        auth_cookie=auth_cookie,
    )
    command = _ssh_command() + [target.ssh_destination, "python3", "-"]
    try:
        result = subprocess.run(
            command,
            input=script,
            text=True,
            capture_output=True,
            cwd=ROOT,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return [
            _loopback_transport_error(
                target,
                network_error=f"remote loopback probe timed out after {timeout_seconds:g} seconds",
                stdout=exc.stdout,
                stderr=exc.stderr,
                timed_out=True,
            )
        ]
    except OSError as exc:
        return [
            _loopback_transport_error(
                target,
                network_error=f"remote loopback probe failed to start: {exc}",
            )
        ]
    if result.returncode != 0:
        return [
            _loopback_transport_error(
                target,
                network_error=result.stderr.strip() or result.stdout.strip() or f"ssh exit code {result.returncode}",
                stdout=result.stdout,
                stderr=result.stderr,
            )
        ]
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return [
            _loopback_transport_error(
                target,
                network_error=f"remote loopback probe returned invalid JSON: {exc}",
                stdout=result.stdout,
                stderr=result.stderr,
            )
        ]
    if not isinstance(payload, list):
        return [
            _loopback_transport_error(
                target,
                network_error=f"remote loopback probe returned {type(payload).__name__}, expected list",
                stdout=result.stdout,
                stderr=result.stderr,
            )
        ]
    return payload


def _validate_probe_timeout_seconds(timeout_seconds: float) -> float:
    value = float(timeout_seconds)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("--timeout-seconds must be a finite value greater than 0")
    return value


def _coerce_process_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _combine_process_output(*values: Any) -> tuple[str, bool, int]:
    text = "\n".join(
        item
        for item in (_coerce_process_output(value).strip() for value in values)
        if item
    )
    raw = text.encode("utf-8", errors="replace")
    body_truncated = len(raw) > PROBE_BODY_LIMIT_BYTES
    if body_truncated:
        raw = raw[:PROBE_BODY_LIMIT_BYTES]
        text = raw.decode("utf-8", errors="replace")
    return text, body_truncated, len(raw)


def _loopback_transport_error(
    target: HostedRuntimeTarget,
    *,
    network_error: str,
    stdout: Any = "",
    stderr: Any = "",
    timed_out: bool = False,
) -> dict[str, Any]:
    body_excerpt, body_truncated, body_bytes_read = _combine_process_output(stderr, stdout)
    payload: dict[str, Any] = {
        "route": "loopback_transport",
        "method": "SSH",
        "url": target.ssh_destination,
        "http_status": None,
        "content_type": "",
        "body_excerpt": body_excerpt,
        "body_truncated": body_truncated,
        "body_bytes_read": body_bytes_read,
        "json_body": None,
        "network_error": network_error,
    }
    if timed_out:
        payload["timed_out"] = True
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
    auth_cookie: str | None = None,
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
        "auth_cookie": auth_cookie or "",
    }
    payload_json = json.dumps(payload, ensure_ascii=True)
    return f"""import json
import os
import ssl
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
    chunks = []
    remaining = PROBE_BODY_LIMIT_BYTES + 1
    while remaining > 0:
        chunk = response.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
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
    if PAYLOAD.get("auth_cookie"):
        request.add_header("Cookie", PAYLOAD["auth_cookie"])
    if json_payload is not None:
        body = json.dumps(json_payload).encode("utf-8")
        request.data = body
        request.add_header("Content-Type", "application/json; charset=utf-8")
        request.add_header("Content-Length", str(len(body)))
    try:
        with _open_request(request, timeout_seconds=PAYLOAD["timeout_seconds"]) as response:
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
    except Exception as exc:
        return {{
            "route": name,
            "method": method,
            "url": url,
            "http_status": None,
            "content_type": "",
            "body_excerpt": "",
            "json_body": None,
            "network_error": str(exc),
        }}

results = [
    _collect("operator", "GET", PAYLOAD["base_url"] + PAYLOAD["route_paths"]["SHEET_VITRINA_OPERATOR_UI_PATH"]),
    _collect("operator_reports", "GET", PAYLOAD["base_url"] + PAYLOAD["route_paths"]["SHEET_VITRINA_OPERATOR_UI_PATH"] + "?embedded_tab=reports"),
    _collect("operator_factory_order", "GET", PAYLOAD["base_url"] + PAYLOAD["route_paths"]["SHEET_VITRINA_OPERATOR_UI_PATH"] + "?embedded_tab=factory-order"),
    _collect("web_vitrina_page", "GET", PAYLOAD["base_url"] + {DEFAULT_SHEET_WEB_VITRINA_UI_PATH!r}),
    _collect("warehouses_overview", "GET", PAYLOAD["base_url"] + {DEFAULT_WAREHOUSES_PATH!r}),
    _collect("warehouse_ff", "GET", PAYLOAD["base_url"] + {DEFAULT_WAREHOUSES_PATH!r} + "/ff"),
    _collect("instructions_page", "GET", PAYLOAD["base_url"] + {DEFAULT_INSTRUCTIONS_UI_PATH!r}),
    _collect("load_route", "GET", PAYLOAD["base_url"] + "/v1/sheet-vitrina-v1/load"),
    _collect("job", "GET", PAYLOAD["base_url"] + "/v1/sheet-vitrina-v1/job?job_id=hosted_runtime_probe"),
    _collect("status", "GET", _append_as_of_date(PAYLOAD["base_url"] + PAYLOAD["route_paths"]["SHEET_VITRINA_STATUS_HTTP_PATH"], PAYLOAD["as_of_date"])),
    _collect("own_product_capital_status", "GET", PAYLOAD["base_url"] + {DEFAULT_OWN_PRODUCT_CAPITAL_STATUS_PATH!r}),
    _collect("web_vitrina_read", "GET", _append_as_of_date(PAYLOAD["base_url"] + {DEFAULT_SHEET_WEB_VITRINA_READ_PATH!r}, PAYLOAD["as_of_date"])),
    _collect("web_vitrina_page_composition", "GET", _append_query_params(PAYLOAD["base_url"] + {DEFAULT_SHEET_WEB_VITRINA_READ_PATH!r}, {{"as_of_date": PAYLOAD["as_of_date"], "surface": {DEFAULT_SHEET_WEB_VITRINA_PAGE_COMPOSITION_SURFACE!r}}})),
    _collect("web_vitrina_user_config", "GET", PAYLOAD["base_url"] + {DEFAULT_SHEET_WEB_VITRINA_USER_CONFIG_PATH!r}),
    _collect("web_vitrina_business_projection_status", "GET", PAYLOAD["base_url"] + {DEFAULT_SHEET_WEB_VITRINA_BUSINESS_PROJECTION_STATUS_PATH!r}),
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
    _collect("wb_warehouse_exclusion_options", "GET", PAYLOAD["base_url"] + {DEFAULT_WB_WAREHOUSE_EXCLUSION_OPTIONS_PATH!r}),
    _collect("wb_warehouse_exclusion_settings", "GET", PAYLOAD["base_url"] + {DEFAULT_WB_WAREHOUSE_EXCLUSION_SETTINGS_PATH!r}),
    _collect("auto_updates_status", "GET", PAYLOAD["base_url"] + {DEFAULT_AUTO_UPDATES_PATH!r}),
    _collect("wb_supplies_list", "GET", PAYLOAD["base_url"] + {DEFAULT_WB_SUPPLIES_PATH!r}),
    _collect("wb_regional_district_central", "GET", PAYLOAD["base_url"] + "/v1/sheet-vitrina-v1/supply/wb-regional/district/central.xlsx"),
    _collect("wb_regional_recommendations_zip", "GET", PAYLOAD["base_url"] + {DEFAULT_WB_REGIONAL_RECOMMENDATIONS_ZIP_PATH!r}),
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
        if isinstance(ssl_reason, ssl.SSLCertVerificationError):
            system_ca_context = _probe_system_ca_context()
            if system_ca_context is not None:
                try:
                    return urllib_request.urlopen(
                        request,
                        timeout=timeout_seconds,
                        context=system_ca_context,
                    )
                except urllib_error.URLError as retry_exc:
                    exc = retry_exc
                    ssl_reason = getattr(retry_exc, "reason", None)
        if (
            os.environ.get("SELLEROS_HTTP_ALLOW_INSECURE_FALLBACK", "").strip() == "1"
            and isinstance(ssl_reason, ssl.SSLCertVerificationError)
        ):
            return urllib_request.urlopen(
                request,
                timeout=timeout_seconds,
                context=ssl._create_unverified_context(),
            )
        raise exc


def _probe_system_ca_context() -> ssl.SSLContext | None:
    seen: set[str] = set()
    for candidate in (os.environ.get("SSL_CERT_FILE", ""), *PROBE_SYSTEM_CA_FILE_CANDIDATES):
        ca_file = str(candidate or "").strip()
        if not ca_file or ca_file in seen:
            continue
        seen.add(ca_file)
        path = Path(ca_file)
        if not path.is_file():
            continue
        try:
            return ssl.create_default_context(cafile=str(path))
        except (OSError, ssl.SSLError):
            continue
    return None


def _ssh_command() -> list[str]:
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=20",
    ]
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
    _validate_production_target_identity(target, action=action)
    _ensure_active_hosted_runtime_target(target, action=action)


def _validate_production_target_identity(target: HostedRuntimeTarget, *, action: str) -> None:
    """Validate the canonical Europe identity before any production-side action."""

    production_contour = (
        _is_current_live_target(target)
        or _public_base_url_host(target.public_base_url) in ACTIVE_HOSTED_RUNTIME_PUBLIC_HOSTS
        or str(target.ssh_destination).strip() in {ACTIVE_HOSTED_RUNTIME_SSH_DESTINATION, "selleros-root"}
    )
    if not production_contour:
        return
    blockers: list[str] = []
    if target.target_id != ACTIVE_HOSTED_RUNTIME_TARGET_ID:
        blockers.append(f"target_id must be {ACTIVE_HOSTED_RUNTIME_TARGET_ID}, got {target.target_id or '<missing>'}")
    if str(target.target_status).strip().lower() != ACTIVE_TARGET_STATUS:
        blockers.append(f"target_status must be {ACTIVE_TARGET_STATUS}, got {target.target_status or '<missing>'}")
    if str(target.target_role).strip().lower() != PRIMARY_LIVE_TARGET_ROLE:
        blockers.append(f"target_role must be {PRIMARY_LIVE_TARGET_ROLE}, got {target.target_role or '<missing>'}")
    if str(target.target_lifecycle).strip().lower() != CURRENT_LIVE_TARGET_LIFECYCLE:
        blockers.append(f"target_lifecycle must be {CURRENT_LIVE_TARGET_LIFECYCLE}, got {target.target_lifecycle or '<missing>'}")
    if target.ssh_destination != ACTIVE_HOSTED_RUNTIME_SSH_DESTINATION:
        blockers.append(f"ssh_destination must be {ACTIVE_HOSTED_RUNTIME_SSH_DESTINATION}, got {target.ssh_destination or '<missing>'}")
    env_file = str(target.environment_file or "").strip()
    if not env_file or env_file == "__SET_ME__" or "placeholder" in env_file.lower() or "template" in env_file.lower():
        blockers.append("environment_file is empty or placeholder")
    elif env_file != ACTIVE_HOSTED_RUNTIME_ENVIRONMENT_FILE:
        blockers.append(f"environment_file must be {ACTIVE_HOSTED_RUNTIME_ENVIRONMENT_FILE}, got {env_file}")
    if blockers:
        raise ValueError(f"{action} refused: non-canonical production target identity: {'; '.join(blockers)}")


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
    current_live_invariant_blockers = _current_live_publication_invariant_blockers(target)
    if current_live_invariant_blockers:
        blockers.append(_current_live_publication_invariant_error(current_live_invariant_blockers))
    return blockers


def _is_current_live_target(target: HostedRuntimeTarget) -> bool:
    role = str(target.target_role or "").strip().lower()
    lifecycle = str(target.target_lifecycle or "").strip().lower()
    return role == PRIMARY_LIVE_TARGET_ROLE or lifecycle == CURRENT_LIVE_TARGET_LIFECYCLE


def _current_live_publication_invariant_blockers(target: HostedRuntimeTarget) -> list[str]:
    if not _is_current_live_target(target):
        return []

    blockers: list[str] = []
    if target.public_base_url != CURRENT_LIVE_PUBLIC_BASE_URL:
        blockers.append(f"public_base_url={target.public_base_url or '<missing>'}")
    if str(target.runtime_env.get("WB_AUTOANSWERS_FORCE_OFF") or "").strip().lower() != "false":
        blockers.append("runtime_env.WB_AUTOANSWERS_FORCE_OFF must be false")

    if not target.nginx_public_routes:
        blockers.append("nginx_public_routes=<missing>")
        return blockers

    server_names = _nginx_server_names_for_target(target)
    if server_names != CURRENT_LIVE_REQUIRED_SERVER_NAMES:
        blockers.append(f"nginx_public_routes.server_names={list(server_names)!r}")

    rendered_server_name = f"server_name {' '.join(server_names)};"
    required_server_name = f"server_name {' '.join(CURRENT_LIVE_REQUIRED_SERVER_NAMES)};"
    if rendered_server_name != required_server_name:
        blockers.append(f"rendered_server_name={rendered_server_name!r}")

    tls = target.nginx_public_routes.tls
    if tls is None:
        blockers.append("nginx_public_routes.tls=<missing>")
        return blockers

    if CURRENT_LIVE_REQUIRED_TLS_LISTEN not in tls.listen:
        blockers.append(f"nginx_public_routes.tls.listen={list(tls.listen)!r}")
    if tls.certificate_path != CURRENT_LIVE_REQUIRED_TLS_CERTIFICATE_PATH:
        blockers.append(f"nginx_public_routes.tls.certificate_path={tls.certificate_path!r}")
    if tls.certificate_key_path != CURRENT_LIVE_REQUIRED_TLS_CERTIFICATE_KEY_PATH:
        blockers.append(f"nginx_public_routes.tls.certificate_key_path={tls.certificate_key_path!r}")

    try:
        rendered_tls = render_nginx_tls_block(tls)
    except ValueError as exc:
        blockers.append(f"nginx_public_routes.tls render failed: {exc}")
    else:
        if f"listen {CURRENT_LIVE_REQUIRED_TLS_LISTEN};" not in rendered_tls:
            blockers.append("rendered TLS block missing `listen 443 ssl;`")
        if f"ssl_certificate {CURRENT_LIVE_REQUIRED_TLS_CERTIFICATE_PATH};" not in rendered_tls:
            blockers.append("rendered TLS block missing LetsEncrypt fullchain path")
        if f"ssl_certificate_key {CURRENT_LIVE_REQUIRED_TLS_CERTIFICATE_KEY_PATH};" not in rendered_tls:
            blockers.append("rendered TLS block missing LetsEncrypt private key path reference")

    return blockers


def _current_live_publication_invariant_error(blockers: list[str]) -> str:
    return (
        f"current live EU target must publish `{CURRENT_LIVE_PUBLIC_BASE_URL}`; "
        "required server_names: `89.191.226.88`, `api.selleros.pro`; "
        "required TLS: `listen 443 ssl` with LetsEncrypt paths "
        f"`{CURRENT_LIVE_REQUIRED_TLS_CERTIFICATE_PATH}` and "
        f"`{CURRENT_LIVE_REQUIRED_TLS_CERTIFICATE_KEY_PATH}`; "
        f"blockers: {', '.join(blockers)}"
    )


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
