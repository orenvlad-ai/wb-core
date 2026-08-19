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
from typing import Any, Callable, Mapping
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
AUTOANSWERS_BACKLOG_RECOVERY_TIMEOUT_SECONDS = 7200.0
FINANCE_CANONICAL_READ_TIMEOUT_SECONDS = 3600.0
FINANCE_CANONICAL_MUTATION_TIMEOUT_SECONDS = 7200.0
FINANCE_CANONICAL_TRANSPORT_GRACE_SECONDS = 60.0
FINANCE_CANONICAL_STATUS_POLL_SECONDS = 15.0
FINANCE_CANONICAL_OPERATION_ID_PATTERN = re.compile(r"^[a-f0-9]{24,64}$")
FINANCE_STORAGE_SPLIT_READ_TIMEOUT_SECONDS = 3600.0
FINANCE_STORAGE_SPLIT_MUTATION_TIMEOUT_SECONDS = 43_200.0
FINANCE_STORAGE_TRANSPORT_GRACE_SECONDS = 120.0
FINANCE_STORAGE_TRANSPORT_STATUS_POLL_SECONDS = 5.0
FINANCE_STORAGE_DURABLE_HOLD_ACTIONS = frozenset(
    {
        "snapshot-create",
        "snapshot-retention-apply",
        "cutover-apply",
        "rollback-apply",
    }
)
PARTNER_FINANCE_DIAGNOSTIC_TIMEOUT_SECONDS = 900.0
ADS_HISTORICAL_RECOVERY_TIMEOUT_SECONDS = 3600.0
FF_STAGE_7A_PRODUCTION_TIMEOUT_SECONDS = 7200.0
FF_POOL_ZERO_PHYSICAL_PRODUCTION_TIMEOUT_SECONDS = 1800.0
FF_POOL_CUTOVER_PRODUCTION_TIMEOUT_SECONDS = 7200.0
FF_POOL_RECOVERY_SUPERSESSION_TIMEOUT_SECONDS = 1800.0
VITRINA_INCIDENT_REMATERIALIZATION_TIMEOUT_SECONDS = 900.0
FF_INVENTORY_RECONCILIATION_TIMEOUT_SECONDS = 1800.0
WAREHOUSE_RECOVERY_LIFECYCLE_TIMEOUT_SECONDS = 7200.0
DEPLOY_STATUS_READBACK_ATTEMPTS = 37
DEPLOY_STATUS_READBACK_RETRY_SECONDS = 5.0
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
    DEFAULT_FBS_FULFILLMENT_ORDER_RECOMMENDATION_PATH,
    DEFAULT_FBS_FULFILLMENT_ORDER_STATUS_PATH,
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
from packages.application.finance_migration_deploy_lease import (
    validate_finance_migration_deploy_lease,
)
from packages.application.finance_storage_recovery_contract import (
    MUTATION_ACTIONS as FINANCE_STORAGE_MUTATION_ACTIONS,
)
from packages.application.ff_pool_cutover_production import (
    CONTRACT_NAME as FF_POOL_CUTOVER_PRODUCTION_CONTRACT_NAME,
    CONTRACT_VERSION as FF_POOL_CUTOVER_PRODUCTION_CONTRACT_VERSION,
)
from packages.application.ff_pool_cutover_recovery_supersession import (
    CONTRACT_NAME as FF_POOL_RECOVERY_SUPERSESSION_CONTRACT_NAME,
    CONTRACT_VERSION as FF_POOL_RECOVERY_SUPERSESSION_CONTRACT_VERSION,
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
    "WB_FBS_API_BASE_URL",
    "WB_FBS_COLLECTOR_ENABLED",
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
    finance_generation_filesystem: dict[str, Any] = field(
        default_factory=dict
    )
    systemd_unit_directory: str = ""
    systemd_units_source_dir: str = ""
    managed_systemd_units: tuple[ManagedSystemdUnit, ...] = field(default_factory=tuple)
    retired_systemd_units: tuple[str, ...] = field(default_factory=tuple)
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
    raw_finance_generation_filesystem = (
        payload.get("finance_generation_filesystem") or {}
    )
    if not isinstance(raw_finance_generation_filesystem, dict):
        raise ValueError(
            "finance_generation_filesystem must be a JSON object"
        )
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
    raw_retired_systemd_units = payload.get("retired_systemd_units") or []
    if not isinstance(raw_retired_systemd_units, list):
        raise ValueError("retired_systemd_units must be a JSON array")
    retired_systemd_units = tuple(
        str(unit_name or "").strip() for unit_name in raw_retired_systemd_units
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
        finance_generation_filesystem={
            str(key): value
            for key, value in raw_finance_generation_filesystem.items()
        },
        systemd_unit_directory=str(payload.get("systemd_unit_directory", "")).strip(),
        systemd_units_source_dir=str(payload.get("systemd_units_source_dir", "")).strip(),
        managed_systemd_units=tuple(managed_systemd_units),
        retired_systemd_units=retired_systemd_units,
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
    deploy_sequence: list[str] = []
    if target.retired_systemd_units:
        deploy_sequence.extend(
            [
                "disable, stop and remove explicitly retired systemd units before runtime sync",
                "daemon-reload systemd after retired unit removal",
            ]
        )
    deploy_sequence.extend([
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
    ])
    if target.has_managed_systemd_units:
        deploy_sequence.append("install repo-owned systemd units into systemd_unit_directory")
    if target.has_managed_systemd_units:
        deploy_sequence.append("daemon-reload systemd and apply managed unit changes")
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
        "retired_systemd_units": list(target.retired_systemd_units),
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
            name="fbs_fulfillment_order_status",
            method="GET",
            url=f"{base_url}{DEFAULT_FBS_FULFILLMENT_ORDER_STATUS_PATH}",
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
            name="fbs_fulfillment_order_recommendation",
            method="GET",
            url=f"{base_url}{DEFAULT_FBS_FULFILLMENT_ORDER_RECOMMENDATION_PATH}",
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
            "systemd_retire": systemd_commands["retire"],
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

    def reconcile_transport_failure(
        stage: str,
        exc: subprocess.CalledProcessError,
        *,
        allow_transport_reconciliation: bool = True,
    ) -> None:
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
            failed_stage=(
                "readback"
                if stage == "metadata-complete"
                else stage
                if stage in {"daemon-reload", "restart", "probes", "readback"}
                else "sync"
            ),
            require_deployment_complete=stage == "metadata-complete",
            allow_repairs=stage != "metadata-complete",
        )
        summary["transport_reconciliation"] = reconciliation
        if not bool(reconciliation.get("healthy")):
            raise RuntimeError(
                f"transport-indeterminate during {stage}; exact-SHA reconciliation halted"
            ) from exc

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
            reconcile_transport_failure(
                stage,
                exc,
                allow_transport_reconciliation=allow_transport_reconciliation,
            )

    # Never let a deploy/restart proceed against a missing hosted auth contour.
    run_stage("auth-preflight", auth_env_preflight_command)
    # Retire obsolete schedulers before sync/dependency work.  This closes the
    # window in which a legacy timer could start a removed writer flow during
    # a long deployment.
    if systemd_commands["retire"]:
        run_stage("systemd-retire", systemd_commands["retire"])
        run_stage("daemon-reload", systemd_commands["daemon_reload"])
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
    if systemd_commands["install"]:
        run_stage("daemon-reload", systemd_commands["daemon_reload"])
    if nginx_public_routes_command:
        run_stage("nginx", nginx_public_routes_command)
    run_stage("restart", restart_command)
    if systemd_commands["enable"]:
        run_stage("restart", systemd_commands["enable"])
    if systemd_commands["restart"]:
        run_stage("restart", systemd_commands["restart"])
    if status_command:
        try:
            _run_deploy_status_readback(status_command)
        except subprocess.CalledProcessError as exc:
            if exc.returncode != 255:
                raise
            reconcile_transport_failure("readback", exc)
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
    )
    return summary


def _run_deploy_status_readback(
    command: list[str],
    *,
    attempts: int = DEPLOY_STATUS_READBACK_ATTEMPTS,
    retry_seconds: float = DEPLOY_STATUS_READBACK_RETRY_SECONDS,
    runner: Callable[[list[str]], Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Wait boundedly for an asynchronously restarting systemd service.

    ``systemctl restart`` can succeed before the new process finishes startup.
    A short-lived SQLite writer may then make the process fail and systemd may
    recover it through ``Restart=always``. Only the read-only status command is
    repeated here; transport-indeterminate SSH exits remain owned by the exact
    SHA reconciler and every other deploy stage stays single-attempt.
    """

    if attempts <= 0:
        raise ValueError("deploy status readback attempts must be positive")
    execute = runner or _run_command
    last_error: subprocess.CalledProcessError | None = None
    for attempt in range(1, attempts + 1):
        try:
            execute(command)
            return
        except subprocess.CalledProcessError as exc:
            if exc.returncode == 255:
                raise
            last_error = exc
        if attempt < attempts:
            sleep(retry_seconds)
    if last_error is not None:
        raise last_error


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
    if str(getattr(args, "output", "") or "").strip():
        _write_private_json(Path(str(args.output)).resolve(), payload)
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


def run_warehouse_july_recovery_command(args: argparse.Namespace) -> int:
    """Run one exact July warehouse recovery submanifest on the active target."""

    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    action = str(args.warehouse_july_action)
    batch = str(args.batch)
    plan_path = (
        Path(str(args.plan_file)).resolve()
        if action == "apply"
        else None
    )
    if plan_path is not None and (plan_path == ROOT or ROOT in plan_path.parents):
        raise ValueError(
            "July warehouse reviewed plan must stay outside the Git checkout"
        )
    payload = _run_remote_warehouse_july_recovery_action(
        target,
        action=action,
        batch=batch,
        plan_path=plan_path,
        fingerprint=str(getattr(args, "fingerprint", "") or ""),
        approval_reference=str(
            getattr(args, "approval_reference", "") or ""
        ),
        reason=str(getattr(args, "reason", "") or ""),
        batch_a_fingerprint=str(
            getattr(args, "batch_a_fingerprint", "") or ""
        ),
        backup_path=str(getattr(args, "backup_path", "") or ""),
        source_sha256=str(getattr(args, "source_sha256", "") or ""),
        business_date=str(getattr(args, "business_date", "") or ""),
    )
    output = str(getattr(args, "output", "") or "").strip()
    if action == "dry-run" and output:
        output_path = Path(output).resolve()
        if output_path == ROOT or ROOT in output_path.parents:
            raise ValueError(
                "July warehouse evidence must stay outside the Git checkout"
            )
        _write_private_json(output_path, payload)
    _print_json(
        {
            "target_id": target.target_id,
            "ssh_destination": target.ssh_destination,
            "runtime_dir": str(
                target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""
            ),
            "action": action,
            "batch": batch,
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


def _external_json_path(value: str, *, label: str) -> Path:
    path = Path(str(value)).resolve()
    if path == ROOT or ROOT in path.parents:
        raise ValueError(f"{label} must stay outside the Git checkout")
    return path


def _load_autoanswers_t0_manifest(path: Path) -> dict[str, Any]:
    from apps.wb_autoanswers_backlog_recovery import validate_manifest

    if not path.is_file():
        raise ValueError("Autoanswers T0 manifest file does not exist")
    payload = json.loads(path.read_text(encoding="utf-8"))
    manifest = payload.get("manifest") if isinstance(payload, Mapping) else None
    if manifest is None:
        manifest = payload
    return validate_manifest(manifest)


def _run_remote_autoanswers_backlog_recovery(
    target: HostedRuntimeTarget,
    *,
    action: str,
    expected_deployed_sha: str,
    manifest_path: Path | None = None,
    reviewed_plan_path: Path | None = None,
    fingerprint: str = "",
    approval_reference: str = "",
    actor: str = "release-train",
) -> dict[str, Any]:
    _ensure_active_hosted_runtime_target(
        target,
        action=f"autoanswers-backlog-recovery-{action}",
    )
    if action not in {"capture", "dry-run", "apply", "readback"}:
        raise ValueError(f"unsupported Autoanswers backlog recovery action: {action}")
    if action == "apply":
        _ensure_target_allows_mutation(
            target,
            action="autoanswers-backlog-recovery-apply",
            dry_run=False,
        )
    deployed_sha = str(expected_deployed_sha).strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}", deployed_sha) is None:
        raise ValueError("Autoanswers backlog recovery requires --expected-deployed-sha")
    runtime_dir = str(
        target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""
    ).strip()
    if runtime_dir != ACTIVE_HOSTED_RUNTIME_RUNTIME_DIR:
        raise ValueError("Autoanswers backlog recovery requires the canonical active runtime dir")
    if not target.environment_file:
        raise ValueError("Autoanswers backlog recovery requires the hosted environment file")

    manifest: dict[str, Any] | None = None
    manifest_json: str | None = None
    if action != "capture":
        if manifest_path is None:
            raise ValueError(f"Autoanswers backlog recovery {action} requires --manifest-file")
        manifest = _load_autoanswers_t0_manifest(manifest_path)
        manifest_json = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    runner_args = [
        "python3",
        "apps/wb_autoanswers_backlog_recovery.py",
        action,
        "--runtime-dir",
        runtime_dir,
        "--env-file",
        target.environment_file,
        "--expected-deployed-sha",
        deployed_sha,
    ]
    if manifest is not None:
        runner_args.append("--manifest-stdin")
    if action == "apply":
        if re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint) is None:
            raise ValueError("Autoanswers backlog recovery apply requires --fingerprint")
        if reviewed_plan_path is None or not reviewed_plan_path.is_file():
            raise ValueError(
                "Autoanswers backlog recovery apply requires --reviewed-plan-file"
            )
        reviewed_payload = json.loads(reviewed_plan_path.read_text(encoding="utf-8"))
        reviewed_plan = (
            reviewed_payload.get("result")
            if isinstance(reviewed_payload, Mapping)
            and isinstance(reviewed_payload.get("result"), Mapping)
            else reviewed_payload
        )
        if (
            not isinstance(reviewed_plan, Mapping)
            or reviewed_plan.get("coverage_confirmed") is not True
            or str(reviewed_plan.get("plan_fingerprint") or "") != fingerprint
            or str(reviewed_plan.get("manifest_sha256") or "")
            != str(manifest["manifest_sha256"])
            or str(
                (reviewed_plan.get("deployed_runtime") or {}).get("runtime_sha")
                if isinstance(reviewed_plan.get("deployed_runtime"), Mapping)
                else ""
            )
            != deployed_sha
        ):
            raise ValueError("reviewed Autoanswers backlog plan does not match exact apply scope")
        if not str(approval_reference).strip():
            raise ValueError(
                "Autoanswers backlog recovery apply requires --approval-reference"
            )
        runner_args.extend(
            [
                "--expected-fingerprint",
                fingerprint,
                "--approval-reference",
                str(approval_reference).strip(),
                "--actor",
                str(actor).strip() or "release-train",
            ]
        )
    shell = (
        f"cd {shlex.quote(target.target_dir)} && "
        "/usr/bin/env WB_AUTOANSWERS_EXTERNAL_IO_ENABLED=true "
        + " ".join(shlex.quote(item) for item in runner_args)
    )
    result = subprocess.run(
        _remote_shell_command(target, shell),
        text=True,
        capture_output=True,
        input=manifest_json,
        cwd=ROOT,
        timeout=AUTOANSWERS_BACKLOG_RECOVERY_TIMEOUT_SECONDS,
        check=False,
    )
    allowed_returncodes = {0, 2} if action == "readback" else {0}
    if result.returncode not in allowed_returncodes:
        raise RuntimeError(
            f"Autoanswers backlog recovery {action} failed: "
            + (
                result.stderr.strip()
                or result.stdout.strip()
                or f"exit {result.returncode}"
            )
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Autoanswers backlog recovery returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Autoanswers backlog recovery returned a non-object payload")
    if action == "dry-run" and payload.get("coverage_confirmed") is not True:
        raise RuntimeError("Autoanswers backlog recovery dry-run is not apply-ready")
    return payload


def run_autoanswers_backlog_recovery_command(args: argparse.Namespace) -> int:
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    action = str(args.action)
    manifest_path = (
        _external_json_path(str(args.manifest_file), label="Autoanswers T0 manifest")
        if str(args.manifest_file or "").strip()
        else None
    )
    reviewed_plan_path = (
        _external_json_path(
            str(args.reviewed_plan_file),
            label="Autoanswers reviewed recovery plan",
        )
        if str(args.reviewed_plan_file or "").strip()
        else None
    )
    payload = _run_remote_autoanswers_backlog_recovery(
        target,
        action=action,
        expected_deployed_sha=str(args.expected_deployed_sha),
        manifest_path=manifest_path,
        reviewed_plan_path=reviewed_plan_path,
        fingerprint=str(args.fingerprint or ""),
        approval_reference=str(args.approval_reference or ""),
        actor=str(args.actor or "release-train"),
    )
    if str(args.output or "").strip():
        output_path = _external_json_path(
            str(args.output),
            label="Autoanswers backlog recovery evidence",
        )
        _write_private_json(output_path, payload)
    _print_json(
        {
            "target_id": target.target_id,
            "ssh_destination": target.ssh_destination,
            "runtime_dir": str(
                target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""
            ),
            "action": f"autoanswers-backlog-recovery-{action}",
            "result": payload,
        }
    )
    return 0 if payload.get("status") != "pending" else 2


def _run_remote_autoanswers_policy_v5_reconciliation(
    target: HostedRuntimeTarget,
    *,
    action: str,
    expected_deployed_sha: str,
    reviewed_plan_path: Path | None = None,
    fingerprint: str = "",
    actor: str = "release-train",
) -> dict[str, Any]:
    """Run the v5 owner-policy gate while the publication worker is held."""

    _ensure_active_hosted_runtime_target(
        target,
        action=f"autoanswers-policy-v5-reconciliation-{action}",
    )
    if action not in {"dry-run", "apply", "readback"}:
        raise ValueError(f"unsupported Autoanswers policy v5 action: {action}")
    if action == "apply":
        _ensure_target_allows_mutation(
            target,
            action="autoanswers-policy-v5-reconciliation-apply",
            dry_run=False,
        )
    deployed_sha = str(expected_deployed_sha).strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}", deployed_sha) is None:
        raise ValueError(
            "Autoanswers policy v5 reconciliation requires --expected-deployed-sha"
        )
    runtime_dir = str(
        target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""
    ).strip()
    if runtime_dir != ACTIVE_HOSTED_RUNTIME_RUNTIME_DIR:
        raise ValueError(
            "Autoanswers policy v5 reconciliation requires the canonical runtime dir"
        )

    lifecycle = _run_remote_autoanswers_lifecycle(target, action="status")
    components = dict((lifecycle.get("lifecycle") or {}).get("components") or {})
    worker = dict(components.get("worker") or {})
    readonly_sync = dict(components.get("readonly_sync") or {})
    worker_timer = dict(worker.get("timer") or {})
    worker_service = dict(worker.get("service") or {})
    worker_hold = {
        "timer_enabled": str(worker_timer.get("is_enabled") or ""),
        "timer_active": str(worker_timer.get("is_active") or ""),
        "service_active": str(worker_service.get("is_active") or ""),
    }
    readonly_timer = dict(readonly_sync.get("timer") or {})
    get_only_feedback_sync = {
        "actual": bool(readonly_sync.get("actual")),
        "timer_enabled": str(readonly_timer.get("is_enabled") or ""),
        "timer_active": str(readonly_timer.get("is_active") or ""),
    }
    get_only_feedback_sync["confirmed"] = bool(
        get_only_feedback_sync["actual"]
        and get_only_feedback_sync["timer_enabled"] == "enabled"
        and get_only_feedback_sync["timer_active"] == "active"
    )
    if (
        worker_hold["timer_enabled"] == "enabled"
        or worker_hold["timer_active"] in {"active", "activating", "reloading"}
        or worker_hold["service_active"] in {"active", "activating", "reloading"}
    ):
        raise RuntimeError(
            "Autoanswers policy v5 reconciliation requires the worker timer/service hold"
        )

    reviewed_plan_json: str | None = None
    runner_args = [
        "python3",
        "apps/wb_autoanswers_policy_v5_reconciliation.py",
        action,
        "--runtime-dir",
        runtime_dir,
        "--expected-deployed-sha",
        deployed_sha,
    ]
    if action in {"apply", "readback"}:
        if re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint) is None:
            raise ValueError(
                f"Autoanswers policy v5 {action} requires --fingerprint"
            )
        if reviewed_plan_path is None or not reviewed_plan_path.is_file():
            raise ValueError(
                f"Autoanswers policy v5 {action} requires --reviewed-plan-file"
            )
        reviewed_payload = json.loads(reviewed_plan_path.read_text(encoding="utf-8"))
        reviewed_plan = (
            reviewed_payload.get("result")
            if isinstance(reviewed_payload, Mapping)
            and isinstance(reviewed_payload.get("result"), Mapping)
            else reviewed_payload
        )
        if (
            not isinstance(reviewed_plan, Mapping)
            or reviewed_plan.get("coverage_confirmed") is not True
            or str(reviewed_plan.get("plan_fingerprint") or "") != fingerprint
            or str(
                (reviewed_plan.get("deployed_runtime") or {}).get("runtime_sha")
                if isinstance(reviewed_plan.get("deployed_runtime"), Mapping)
                else ""
            )
            != deployed_sha
        ):
            raise ValueError(
                "reviewed Autoanswers policy v5 plan does not match the exact release"
            )
        reviewed_plan_json = json.dumps(
            reviewed_plan,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        runner_args.extend(
            [
                "--expected-fingerprint",
                fingerprint,
                "--reviewed-plan-stdin",
            ]
        )
    if action == "apply":
        runner_args.extend(
            [
                "--worker-hold-confirmed",
                "--actor",
                str(actor).strip() or "release-train",
            ]
        )
    hold_script = (
        "if /usr/bin/systemctl is-enabled --quiet wb-core-autoanswers-worker.timer; "
        "then exit 41; fi; "
        "if /usr/bin/systemctl is-active --quiet wb-core-autoanswers-worker.timer; "
        "then exit 42; fi; "
        "if /usr/bin/systemctl is-active --quiet wb-core-autoanswers-worker.service; "
        "then exit 43; fi; "
    )
    shell = (
        hold_script
        + f"cd {shlex.quote(target.target_dir)} && "
        + " ".join(shlex.quote(item) for item in runner_args)
    )
    result = subprocess.run(
        _remote_shell_command(target, shell),
        text=True,
        capture_output=True,
        input=reviewed_plan_json,
        cwd=ROOT,
        timeout=600,
        check=False,
    )
    allowed_returncodes = {0, 2} if action == "readback" else {0}
    if result.returncode not in allowed_returncodes:
        raise RuntimeError(
            f"Autoanswers policy v5 reconciliation {action} failed: "
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
            "Autoanswers policy v5 reconciliation returned invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            "Autoanswers policy v5 reconciliation returned a non-object payload"
        )
    payload["worker_hold"] = worker_hold
    payload["get_only_feedback_sync"] = get_only_feedback_sync
    get_only_delta = payload.get("get_only_observed_delta")
    if (
        action == "readback"
        and isinstance(get_only_delta, Mapping)
        and get_only_delta.get("changed") is True
        and get_only_feedback_sync["confirmed"] is not True
    ):
        payload["status"] = "blocked"
        payload["blockers"] = list(payload.get("blockers") or []) + [
            "get_only_feedback_delta_without_active_readonly_sync"
        ]
    if action == "dry-run" and payload.get("coverage_confirmed") is not True:
        raise RuntimeError(
            "Autoanswers policy v5 reconciliation dry-run is not apply-ready"
        )
    return payload


def run_autoanswers_policy_v5_reconciliation_command(
    args: argparse.Namespace,
) -> int:
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    action = str(args.action)
    reviewed_plan_path = (
        _external_json_path(
            str(args.reviewed_plan_file),
            label="Autoanswers policy v5 reviewed plan",
        )
        if str(args.reviewed_plan_file or "").strip()
        else None
    )
    payload = _run_remote_autoanswers_policy_v5_reconciliation(
        target,
        action=action,
        expected_deployed_sha=str(args.expected_deployed_sha),
        reviewed_plan_path=reviewed_plan_path,
        fingerprint=str(args.fingerprint or ""),
        actor=str(args.actor or "release-train"),
    )
    if str(args.output or "").strip():
        output_path = _external_json_path(
            str(args.output),
            label="Autoanswers policy v5 reconciliation evidence",
        )
        _write_private_json(output_path, payload)
    _print_json(
        {
            "target_id": target.target_id,
            "ssh_destination": target.ssh_destination,
            "runtime_dir": str(
                target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""
            ),
            "action": f"autoanswers-policy-v5-reconciliation-{action}",
            "result": payload,
        }
    )
    return 0 if payload.get("status") != "blocked" else 2


def _load_autoanswers_answered_inventory_manifest(path: Path) -> dict[str, Any]:
    from apps.wb_autoanswers_answered_inventory_recovery import validate_manifest

    if not path.is_file():
        raise ValueError("Autoanswers answered-inventory manifest file does not exist")
    payload = json.loads(path.read_text(encoding="utf-8"))
    manifest = payload.get("manifest") if isinstance(payload, Mapping) else None
    if manifest is None:
        manifest = payload
    return validate_manifest(manifest)


def _run_remote_autoanswers_answered_inventory_recovery(
    target: HostedRuntimeTarget,
    *,
    action: str,
    expected_deployed_sha: str,
    manifest_path: Path | None = None,
    reviewed_plan_path: Path | None = None,
    fingerprint: str = "",
    approval_reference: str = "",
    actor: str = "release-train",
) -> dict[str, Any]:
    _ensure_active_hosted_runtime_target(
        target,
        action=f"autoanswers-answered-inventory-recovery-{action}",
    )
    if action not in {"capture", "dry-run", "apply", "readback"}:
        raise ValueError(
            f"unsupported Autoanswers answered-inventory recovery action: {action}"
        )
    if action == "apply":
        _ensure_target_allows_mutation(
            target,
            action="autoanswers-answered-inventory-recovery-apply",
            dry_run=False,
        )
    deployed_sha = str(expected_deployed_sha).strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}", deployed_sha) is None:
        raise ValueError(
            "Autoanswers answered-inventory recovery requires --expected-deployed-sha"
        )
    runtime_dir = str(
        target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""
    ).strip()
    if runtime_dir != ACTIVE_HOSTED_RUNTIME_RUNTIME_DIR:
        raise ValueError(
            "Autoanswers answered-inventory recovery requires the canonical active runtime dir"
        )
    if not target.environment_file:
        raise ValueError(
            "Autoanswers answered-inventory recovery requires the hosted environment file"
        )

    manifest: dict[str, Any] | None = None
    manifest_json: str | None = None
    if action != "capture":
        if manifest_path is None:
            raise ValueError(
                f"Autoanswers answered-inventory recovery {action} requires --manifest-file"
            )
        manifest = _load_autoanswers_answered_inventory_manifest(manifest_path)
        manifest_json = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    runner_args = [
        "python3",
        "apps/wb_autoanswers_answered_inventory_recovery.py",
        action,
        "--runtime-dir",
        runtime_dir,
        "--env-file",
        target.environment_file,
        "--expected-deployed-sha",
        deployed_sha,
    ]
    if manifest is not None:
        runner_args.append("--manifest-stdin")
    if action == "apply":
        if re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint) is None:
            raise ValueError(
                "Autoanswers answered-inventory recovery apply requires --fingerprint"
            )
        if reviewed_plan_path is None or not reviewed_plan_path.is_file():
            raise ValueError(
                "Autoanswers answered-inventory recovery apply requires --reviewed-plan-file"
            )
        reviewed_payload = json.loads(reviewed_plan_path.read_text(encoding="utf-8"))
        reviewed_plan = (
            reviewed_payload.get("result")
            if isinstance(reviewed_payload, Mapping)
            and isinstance(reviewed_payload.get("result"), Mapping)
            else reviewed_payload
        )
        if (
            not isinstance(reviewed_plan, Mapping)
            or reviewed_plan.get("coverage_confirmed") is not True
            or str(reviewed_plan.get("plan_fingerprint") or "") != fingerprint
            or str(reviewed_plan.get("manifest_sha256") or "")
            != str(manifest["manifest_sha256"])
            or str(reviewed_plan.get("deployed_sha") or "") != deployed_sha
        ):
            raise ValueError(
                "reviewed Autoanswers answered-inventory plan does not match exact apply scope"
            )
        if not str(approval_reference).strip():
            raise ValueError(
                "Autoanswers answered-inventory recovery apply requires --approval-reference"
            )
        runner_args.extend(
            [
                "--expected-fingerprint",
                fingerprint,
                "--approval-reference",
                str(approval_reference).strip(),
                "--actor",
                str(actor).strip() or "release-train",
            ]
        )
    shell = (
        f"cd {shlex.quote(target.target_dir)} && "
        "/usr/bin/env WB_AUTOANSWERS_EXTERNAL_IO_ENABLED=true "
        + " ".join(shlex.quote(item) for item in runner_args)
    )
    result = subprocess.run(
        _remote_shell_command(target, shell),
        text=True,
        capture_output=True,
        input=manifest_json,
        cwd=ROOT,
        timeout=AUTOANSWERS_BACKLOG_RECOVERY_TIMEOUT_SECONDS,
        check=False,
    )
    allowed_returncodes = {0, 2} if action == "readback" else {0}
    if result.returncode not in allowed_returncodes:
        raise RuntimeError(
            f"Autoanswers answered-inventory recovery {action} failed: "
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
            "Autoanswers answered-inventory recovery returned invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            "Autoanswers answered-inventory recovery returned a non-object payload"
        )
    if action == "dry-run" and payload.get("coverage_confirmed") is not True:
        raise RuntimeError(
            "Autoanswers answered-inventory recovery dry-run is not apply-ready"
        )
    return payload


def run_autoanswers_answered_inventory_recovery_command(
    args: argparse.Namespace,
) -> int:
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    action = str(args.action)
    manifest_path = (
        _external_json_path(
            str(args.manifest_file),
            label="Autoanswers answered-inventory manifest",
        )
        if str(args.manifest_file or "").strip()
        else None
    )
    reviewed_plan_path = (
        _external_json_path(
            str(args.reviewed_plan_file),
            label="Autoanswers answered-inventory reviewed plan",
        )
        if str(args.reviewed_plan_file or "").strip()
        else None
    )
    payload = _run_remote_autoanswers_answered_inventory_recovery(
        target,
        action=action,
        expected_deployed_sha=str(args.expected_deployed_sha),
        manifest_path=manifest_path,
        reviewed_plan_path=reviewed_plan_path,
        fingerprint=str(args.fingerprint or ""),
        approval_reference=str(args.approval_reference or ""),
        actor=str(args.actor or "release-train"),
    )
    if str(args.output or "").strip():
        output_path = _external_json_path(
            str(args.output),
            label="Autoanswers answered-inventory recovery evidence",
        )
        _write_private_json(output_path, payload)
    _print_json(
        {
            "target_id": target.target_id,
            "ssh_destination": target.ssh_destination,
            "runtime_dir": str(
                target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""
            ),
            "action": f"autoanswers-answered-inventory-recovery-{action}",
            "result": payload,
        }
    )
    return 0 if payload.get("status") != "pending" else 2


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
    if job_action in {"submit", "resume"}:
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
    elif job_action == "resume":
        failure_digest = str(
            args.expected_failure_digest or ""
        ).strip().lower()
        continuity_fingerprint = str(
            args.service_continuity_fingerprint or ""
        ).strip().lower()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", failure_digest):
            raise ValueError(
                "same-job restore resume requires the exact first failure "
                "digest"
            )
        if not re.fullmatch(
            r"sha256:[0-9a-f]{64}",
            continuity_fingerprint,
        ):
            raise ValueError(
                "same-job restore resume requires the original exact "
                "continuity fingerprint"
            )
        actor = str(args.actor or "").strip()
        reason = str(args.reason or "").strip()
        if not actor or not reason:
            raise ValueError(
                "same-job restore resume requires audited actor and reason"
            )
        runner_args.extend(
            [
                "--expected-failure-digest",
                failure_digest,
                "--service-continuity-fingerprint",
                continuity_fingerprint,
                "--actor",
                actor,
                "--reason",
                reason,
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


def _run_remote_business_data_maintenance_restore_job(
    target: HostedRuntimeTarget,
    *,
    job_action: str,
    deployed_sha: str,
    job_id: str = "",
    expected_revision: int | None = None,
    window_id: str = "",
    plan_fingerprint: str = "",
    service_continuity_fingerprint: str = "",
    actor: str = "",
    reason: str = "",
    allow_absent: bool = False,
) -> dict[str, Any]:
    """Submit or inspect one exact durable restore for a hosted workflow."""

    action = f"business-data-maintenance-restore-{job_action}"
    _ensure_active_hosted_runtime_target(target, action=action)
    if job_action == "submit":
        _ensure_target_allows_mutation(target, action=action, dry_run=False)
    if job_action not in {"inventory", "submit", "status"}:
        raise ValueError(
            "internal durable restore helper supports only inventory/submit/status"
        )
    if "wb-core-business-data-maintenance-restore@.service" not in {
        unit.name for unit in target.managed_systemd_units
    }:
        raise ValueError(
            "detached maintenance restore requires the repo-owned managed "
            "systemd template"
        )
    exact_sha = str(deployed_sha or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", exact_sha):
        raise ValueError(
            "detached maintenance restore requires an exact deployed SHA"
        )
    exact_job_id = str(job_id or "").strip().lower()
    if (
        job_action != "inventory"
        and not re.fullmatch(r"[0-9a-f]{64}", exact_job_id)
    ):
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
    ]
    if job_action != "inventory":
        runner_args.extend(
            [
                "--job-id",
                exact_job_id,
                "--deployed-sha",
                exact_sha,
            ]
        )
    if job_action == "status":
        if allow_absent:
            runner_args.append("--allow-absent")
    elif job_action == "submit":
        if expected_revision is None or int(expected_revision) < 0:
            raise ValueError(
                "detached maintenance restore requires an exact policy revision"
            )
        exact_fingerprint = str(plan_fingerprint or "").strip().lower()
        exact_continuity = str(
            service_continuity_fingerprint or ""
        ).strip().lower()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", exact_fingerprint):
            raise ValueError(
                "detached maintenance restore requires an exact plan fingerprint"
            )
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", exact_continuity):
            raise ValueError(
                "detached maintenance restore requires an exact service "
                "continuity fingerprint"
            )
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
            str(window_id or ""),
        ):
            raise ValueError(
                "detached maintenance restore requires an exact window id"
            )
        exact_actor = str(actor or "").strip()
        exact_reason = str(reason or "").strip()
        if not exact_actor or not exact_reason:
            raise ValueError(
                "detached maintenance restore requires audited actor and reason"
            )
        runner_args.extend(
            [
                "--expected-revision",
                str(int(expected_revision)),
                "--window-id",
                str(window_id),
                "--plan-fingerprint",
                exact_fingerprint,
                "--service-continuity-fingerprint",
                exact_continuity,
                "--actor",
                exact_actor,
                "--reason",
                exact_reason,
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
                + shlex.quote(exact_sha)
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
    expected_contract = (
        "business_data_maintenance_restore_inventory_v1"
        if job_action == "inventory"
        else "business_data_maintenance_restore_job_v1"
    )
    if (
        not isinstance(payload, dict)
        or payload.get("contract_name") != expected_contract
        or (
            job_action != "inventory"
            and str(payload.get("job_id") or "") != exact_job_id
        )
    ):
        raise RuntimeError(
            "detached maintenance restore returned an invalid identity"
        )
    return payload


def _finance_snapshot_restore_job_id(
    *,
    deployed_sha: str,
    window_id: str,
    plan_fingerprint: str,
) -> str:
    material = "\n".join(
        [
            "wb_core_finance_snapshot_restore_job_v1",
            str(deployed_sha).strip().lower(),
            str(window_id).strip(),
            str(plan_fingerprint).strip().lower(),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _wait_for_finance_snapshot_restore_job(
    target: HostedRuntimeTarget,
    *,
    deployed_sha: str,
    job_id: str,
    initial_status: Mapping[str, Any],
) -> dict[str, Any]:
    deadline = time.monotonic() + 10_800.0
    current = dict(initial_status)
    while True:
        observation = dict(current.get("worker_observation") or {})
        classification = str(observation.get("classification") or "")
        if (
            str(current.get("status") or "") == "succeeded"
            and current.get("terminal") is True
            and classification == "terminal_succeeded"
        ):
            request = dict(current.get("request") or {})
            binding = dict(current.get("deployment_binding") or {})
            result = dict(current.get("result") or {})
            readback = dict(result.get("readback") or {})
            if (
                str(request.get("job_id") or "") != job_id
                or str(binding.get("deployed_sha") or "") != deployed_sha
                or str(result.get("status") or "") != "restored"
                or readback.get("exact_prior_state_restored") is not True
                or str(readback.get("maintenance_phase") or "")
                != "restored"
                or readback.get("master_desired") is not True
                or readback.get("barrier_active") is not True
                or str(readback.get("barrier_phase") or "")
                != "restoring"
            ):
                raise RuntimeError(
                    "Finance snapshot durable restore terminal readback is incomplete"
                )
            return current
        if (
            str(current.get("status") or "") in {
                "failed",
                "start_failed",
                "absent",
            }
            or classification
            not in {"active_worker", "awaiting_systemd_start"}
        ):
            raise RuntimeError(
                "Finance snapshot durable restore is fail-closed: "
                f"status={current.get('status')}; "
                f"classification={classification}"
            )
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "Finance snapshot durable restore observation deadline expired"
            )
        time.sleep(5.0)
        current = _run_remote_business_data_maintenance_restore_job(
            target,
            job_action="status",
            deployed_sha=deployed_sha,
            job_id=job_id,
        )


def _restore_finance_snapshot_window_durably(
    target: HostedRuntimeTarget,
    *,
    transition_evidence: dict[str, Any],
    deployed_sha: str,
    window_id: str,
    plan_fingerprint: str,
) -> dict[str, Any]:
    job_id = _finance_snapshot_restore_job_id(
        deployed_sha=deployed_sha,
        window_id=window_id,
        plan_fingerprint=plan_fingerprint,
    )
    initial = _run_remote_business_data_maintenance_restore_job(
        target,
        job_action="status",
        deployed_sha=deployed_sha,
        job_id=job_id,
        allow_absent=True,
    )
    transition_evidence["business_restore_job_initial_status"] = initial
    if str(initial.get("status") or "") == "absent":
        continuity = _run_remote_business_data_maintenance_runner(
            target,
            action="restore-continuity-status",
        )
        transition_evidence["business_restore_continuity"] = continuity
        maintenance = dict(continuity.get("maintenance") or {})
        service_continuity = dict(
            continuity.get("service_continuity") or {}
        )
        expected_revision = int(
            (maintenance.get("auto_updates") or {}).get("revision") or -1
        )
        continuity_fingerprint = str(
            service_continuity.get("fingerprint") or ""
        )
        if (
            str(continuity.get("status") or "") != "ready"
            or str(maintenance.get("status") or "") != "quiet"
            or maintenance.get("quiet") is not True
            or expected_revision < 0
            or not re.fullmatch(
                r"sha256:[0-9a-f]{64}",
                continuity_fingerprint,
            )
        ):
            raise RuntimeError(
                "Finance snapshot durable restore continuity is incomplete"
            )
        initial = _run_remote_business_data_maintenance_restore_job(
            target,
            job_action="submit",
            deployed_sha=deployed_sha,
            job_id=job_id,
            expected_revision=expected_revision,
            window_id=window_id,
            plan_fingerprint=plan_fingerprint,
            service_continuity_fingerprint=continuity_fingerprint,
            actor="finance_storage_snapshot_runner",
            reason="coherent Finance snapshot completed",
        )
        transition_evidence["business_restore_job_submit"] = initial
    terminal = _wait_for_finance_snapshot_restore_job(
        target,
        deployed_sha=deployed_sha,
        job_id=job_id,
        initial_status=initial,
    )
    transition_evidence["business_restore_job_terminal"] = terminal
    return terminal


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
        operation_id=str(getattr(args, "operation_id", "") or ""),
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
    operation_id: str = "",
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
    remote_timeout_seconds = (
        FINANCE_CANONICAL_MUTATION_TIMEOUT_SECONDS
        if action == "apply"
        else FINANCE_CANONICAL_READ_TIMEOUT_SECONDS
    )
    runtime_sha_path = f"{target.target_dir.rstrip('/')}/.wb-core-runtime-sha"
    try:
        runtime_sha_result = subprocess.run(
            _remote_shell_command(
                target,
                f"cat {shlex.quote(runtime_sha_path)}",
            ),
            text=True,
            capture_output=True,
            cwd=ROOT,
            timeout=60.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            "Finance canonical deployed-SHA preflight failed"
        ) from exc
    deployed_sha = runtime_sha_result.stdout.strip()
    if (
        runtime_sha_result.returncode != 0
        or re.fullmatch(r"[0-9a-f]{40}", deployed_sha) is None
    ):
        raise RuntimeError(
            "Finance canonical deployed-SHA preflight failed: "
            + (
                runtime_sha_result.stderr.strip()
                or runtime_sha_result.stdout.strip()
                or f"exit {runtime_sha_result.returncode}"
            )
        )
    remote_runner_command = " ".join(shlex.quote(item) for item in runner_args)
    exact_operation_id = operation_id.strip() or hashlib.sha256(
        os.urandom(32)
    ).hexdigest()[:24]
    if not FINANCE_CANONICAL_OPERATION_ID_PATTERN.fullmatch(
        exact_operation_id
    ):
        raise ValueError(
            "Finance canonical --operation-id must contain 24..64 "
            "lowercase hexadecimal characters"
        )
    operation_root = (
        f"{runtime_dir.rstrip('/')}/.finance-canonical-operations"
    )
    operation_dir = f"{operation_root}/{exact_operation_id}"
    request = {
        "action": action,
        "deployed_sha": deployed_sha,
        "operation_id": exact_operation_id,
        "remote_timeout_seconds": int(remote_timeout_seconds),
        "runner_args": runner_args,
    }
    request_json = json.dumps(
        request,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    request_sha256 = hashlib.sha256(
        request_json.encode("utf-8")
    ).hexdigest()
    worker_command = "; ".join(
        [
            f"cd {shlex.quote(target.target_dir)}",
            (
                "set +e; "
                "timeout --signal=TERM --kill-after=30s "
                f"{int(remote_timeout_seconds)}s {remote_runner_command}"
                f" >{shlex.quote(operation_dir + '/result.tmp')}"
                f" 2>{shlex.quote(operation_dir + '/stderr.tmp')}"
            ),
            "exit_code=$?",
            (
                f"if [ \"$exit_code\" -eq 0 ]; then "
                f"mv {shlex.quote(operation_dir + '/result.tmp')} "
                f"{shlex.quote(operation_dir + '/result.json')} "
                "|| exit_code=75; fi"
            ),
            (
                f"printf '%s\\n' \"$exit_code\" "
                f">{shlex.quote(operation_dir + '/exit_code.tmp')}"
            ),
            (
                f"mv {shlex.quote(operation_dir + '/exit_code.tmp')} "
                f"{shlex.quote(operation_dir + '/exit_code')}"
            ),
        ]
    )
    start_command = "; ".join(
        [
            (
                f"if [ \"$(cat {shlex.quote(runtime_sha_path)} "
                f"2>/dev/null)\" != {shlex.quote(deployed_sha)} ]; then "
                "printf 'deployed-sha-mismatch\\n'; exit 75; fi"
            ),
            "umask 077",
            f"install -d -m 0700 {shlex.quote(operation_root)}",
            (
                f"if mkdir -m 0700 {shlex.quote(operation_dir)}; then "
                f"printf '%s\\n' {shlex.quote(request_json)} "
                f">{shlex.quote(operation_dir + '/request.json')}; "
                f"printf '%s\\n' {shlex.quote(request_sha256)} "
                f">{shlex.quote(operation_dir + '/request.sha256')}; "
                f"nohup sh -c {shlex.quote(worker_command)} "
                "</dev/null >/dev/null 2>&1 & "
                f"printf '%s\\n' \"$!\" >"
                f"{shlex.quote(operation_dir + '/pid')}; "
                "printf 'started\\n'; "
                f"elif [ \"$(cat {shlex.quote(operation_dir + '/request.sha256')} "
                f"2>/dev/null)\" = {shlex.quote(request_sha256)} ]; then "
                "printf 'resumed\\n'; "
                "else printf 'request-mismatch\\n'; exit 74; fi"
            ),
        ]
    )
    print(
        "Finance canonical exact operation "
        f"{exact_operation_id}: starting or resuming",
        file=sys.stderr,
        flush=True,
    )
    started_at = time.monotonic()
    try:
        start_result = subprocess.run(
            _remote_shell_command(target, start_command),
            text=True,
            capture_output=True,
            cwd=ROOT,
            timeout=60.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        start_result = subprocess.CompletedProcess(
            args=[],
            returncode=255,
            stdout="",
            stderr=str(exc),
        )
    status_command = "; ".join(
        [
            f"operation_dir={shlex.quote(operation_dir)}",
            (
                "if [ ! -d \"$operation_dir\" ]; then "
                "printf 'missing\\n'; "
                "elif [ \"$(cat \"$operation_dir/request.sha256\" "
                f"2>/dev/null)\" != {shlex.quote(request_sha256)} ]; then "
                "printf 'request-mismatch\\n'; "
                "elif [ -f \"$operation_dir/exit_code\" ]; then "
                "exit_code=$(cat \"$operation_dir/exit_code\"); "
                "printf 'complete\\n%s\\n' \"$exit_code\"; "
                "if [ \"$exit_code\" -eq 0 ]; then "
                "cat \"$operation_dir/result.json\"; "
                "else cat \"$operation_dir/stderr.tmp\"; fi; "
                "elif [ -f \"$operation_dir/pid\" ]; then "
                "worker_pid=$(cat \"$operation_dir/pid\"); "
                "if kill -0 \"$worker_pid\" 2>/dev/null "
                "&& tr '\\000' ' ' <\"/proc/$worker_pid/cmdline\" "
                "| grep -F -- \"$operation_dir\" >/dev/null; then "
                "printf 'running\\n'; "
                "else printf 'incomplete\\n'; fi; "
                "else printf 'incomplete\\n'; fi"
            ),
        ]
    )
    deadline = (
        started_at
        + remote_timeout_seconds
        + FINANCE_CANONICAL_TRANSPORT_GRACE_SECONDS
    )
    last_transport_error = (
        start_result.stderr.strip()
        or start_result.stdout.strip()
        or (
            f"start exit {start_result.returncode}"
            if start_result.returncode
            else ""
        )
    )
    incomplete_observations = 0
    while time.monotonic() < deadline:
        try:
            status_result = subprocess.run(
                _remote_shell_command(target, status_command),
                text=True,
                capture_output=True,
                cwd=ROOT,
                timeout=60.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            last_transport_error = str(exc)
            time.sleep(FINANCE_CANONICAL_STATUS_POLL_SECONDS)
            continue
        if status_result.returncode != 0:
            last_transport_error = (
                status_result.stderr.strip()
                or status_result.stdout.strip()
                or f"status exit {status_result.returncode}"
            )
            time.sleep(FINANCE_CANONICAL_STATUS_POLL_SECONDS)
            continue
        status_lines = status_result.stdout.splitlines()
        state = status_lines[0].strip() if status_lines else ""
        if state == "running":
            incomplete_observations = 0
            time.sleep(FINANCE_CANONICAL_STATUS_POLL_SECONDS)
            continue
        if state == "missing":
            raise RuntimeError(
                f"Finance canonical {action} exact operation "
                f"{exact_operation_id} was not created: "
                + (last_transport_error or "remote start failed")
            )
        if state == "request-mismatch":
            raise RuntimeError(
                f"Finance canonical {action} exact operation "
                f"{exact_operation_id} belongs to a different request"
            )
        if state == "incomplete":
            incomplete_observations += 1
            if incomplete_observations < 2:
                time.sleep(FINANCE_CANONICAL_STATUS_POLL_SECONDS)
                continue
            raise RuntimeError(
                f"Finance canonical {action} exact operation "
                f"{exact_operation_id} lost its bounded worker before "
                "terminal evidence"
            )
        if state != "complete" or len(status_lines) < 2:
            raise RuntimeError(
                f"Finance canonical {action} exact operation "
                f"{exact_operation_id} returned invalid status"
            )
        try:
            remote_exit_code = int(status_lines[1])
        except ValueError as exc:
            raise RuntimeError(
                f"Finance canonical {action} exact operation "
                f"{exact_operation_id} returned an invalid exit code"
            ) from exc
        result_body = "\n".join(status_lines[2:]).strip()
        if remote_exit_code != 0:
            raise RuntimeError(
                f"Finance canonical {action} exact operation "
                f"{exact_operation_id} failed: "
                + (result_body or f"exit {remote_exit_code}")
            )
        try:
            payload = json.loads(result_body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Finance canonical exact operation "
                f"{exact_operation_id} returned invalid JSON"
            ) from exc
        break
    else:
        raise RuntimeError(
            f"Finance canonical {action} exact operation "
            f"{exact_operation_id} exceeded its bounded remote deadline: "
            + (last_transport_error or "no terminal status")
        )
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
    try:
        evidence["barrier_abort"] = (
            _run_remote_business_data_maintenance_runner(
                target,
                action="barrier-abort",
                actor="finance_storage_window_runner",
                reason=(
                    "unconfirmed Finance window aborted before any "
                    "maintenance hold started"
                ),
                window_id=window_id,
                plan_fingerprint=fingerprint,
            )
        )
    except Exception as unstarted_abort_error:
        evidence["unstarted_hold_abort"] = {
            "status": "not_applicable",
            "error": str(unstarted_abort_error),
        }
    else:
        evidence["business_restore"] = {
            "status": "not_required",
            "boundary_kind": "no_maintenance_hold_started",
        }
        evidence["warehouse_restore"] = {
            "status": "not_required",
            "boundary_kind": "core_prepare_preceded_warehouse_hold",
        }
        return evidence
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
        f" && systemctl show --property MainPID --value "
        f"{shlex.quote(ACTIVE_HOSTED_RUNTIME_SERVICE_NAME)}"
    )
    result = subprocess.run(
        _remote_shell_command(target, command),
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=300.0,
        check=False,
    )
    output_lines = [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip()
    ]
    try:
        main_pid = int(output_lines[-1])
    except (IndexError, ValueError):
        main_pid = 0
    if (
        result.returncode != 0
        or output_lines[:1] != ["active"]
        or main_pid <= 0
    ):
        raise RuntimeError(
            "Finance cutover HTTP service restart/readback failed: "
            + (
                result.stderr.strip()
                or result.stdout.strip()
                or f"exit {result.returncode}"
            )
        )
    storage_health = _run_remote_finance_storage_split_action(
        target,
        action="health",
        plan_path=None,
        fingerprint="",
        approval_reference="",
        chunk_size=10_000,
    )
    raw_openers = list(
        (storage_health.get("raw") or {}).get("openers") or []
    )
    operational_openers = list(
        (storage_health.get("operational") or {}).get("openers") or []
    )
    raw_bound = any(
        int(item.get("pid") or 0) == main_pid
        for item in raw_openers
        if isinstance(item, dict)
    )
    operational_bound = any(
        int(item.get("pid") or 0) == main_pid
        for item in operational_openers
        if isinstance(item, dict)
    )
    if not raw_bound or not operational_bound:
        raise RuntimeError(
            "Finance cutover HTTP service is active but its MainPID is not "
            "bound to both canonical manifest stores; barrier remains active"
        )
    return {
        "service": ACTIVE_HOSTED_RUNTIME_SERVICE_NAME,
        "status": "active",
        "main_pid": main_pid,
        "canonical_source": storage_health.get("canonical_source"),
        "generation_epoch": storage_health.get("generation_epoch"),
        "raw_path": (storage_health.get("raw") or {}).get("path"),
        "operational_path": (
            storage_health.get("operational") or {}
        ).get("path"),
        "raw_bound": raw_bound,
        "operational_bound": operational_bound,
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
            "snapshot-retention-apply",
            "snapshot-retention-readback",
            "candidate-abort-apply",
            "candidate-abort-readback",
            "stale-writer-stop",
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
    candidate_generation_epoch = str(
        getattr(args, "candidate_generation_epoch", "") or ""
    )
    expected_retained_generation = str(
        getattr(args, "expected_retained_generation", "") or ""
    )
    minimum_observation_seconds = int(
        getattr(args, "minimum_observation_seconds", 3600) or 0
    )
    rollback_candidate_evidence = str(
        getattr(args, "rollback_candidate_evidence", "") or ""
    )
    deploy_lease: dict[str, Any] | None = None
    deploy_lease_path = str(
        getattr(args, "finance_deploy_lease_evidence", "") or ""
    ).strip()
    if action not in {
        "health",
        "recovery-contract",
        "snapshot-status",
    }:
        if not deploy_lease_path:
            raise ValueError(
                "Finance storage migration requires a fresh "
                "--finance-deploy-lease-evidence readback"
            )
        evidence_path = Path(deploy_lease_path).expanduser().resolve()
        if evidence_path == ROOT or ROOT in evidence_path.parents:
            raise ValueError(
                "Finance deploy-lease evidence must stay outside the Git checkout"
            )
        loaded_lease = json.loads(evidence_path.read_text(encoding="utf-8"))
        if not isinstance(loaded_lease, dict):
            raise ValueError(
                "Finance deploy-lease evidence must contain a JSON object"
            )
        bound_sha = str(
            ((loaded_lease.get("lease") or {}).get("deployed_sha") or "")
        )
        deploy_lease = validate_finance_migration_deploy_lease(
            loaded_lease,
            deployed_sha=bound_sha,
        )
    transition_evidence: dict[str, Any] = {}
    if action in FINANCE_STORAGE_MUTATION_ACTIONS:
        transition_evidence["recovery_preflight"] = (
            _run_remote_finance_storage_split_action(
                target,
                action="recovery-preflight",
                recovery_action=action,
                plan_path=plan_path,
                fingerprint=fingerprint,
                approval_reference=approval_reference,
                chunk_size=int(
                    getattr(args, "chunk_size", 10_000) or 10_000
                ),
                source_snapshot_manifest=source_snapshot_manifest,
                candidate_manifest=candidate_manifest,
                candidate_plan_fingerprint=candidate_plan_fingerprint,
                candidate_generation_epoch=candidate_generation_epoch,
                expected_retained_generation=(
                    expected_retained_generation
                ),
                minimum_observation_seconds=minimum_observation_seconds,
                rollback_candidate_evidence=rollback_candidate_evidence,
                deploy_lease=deploy_lease,
            )
        )
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
                deploy_lease=deploy_lease,
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
                deploy_lease=deploy_lease,
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
                deploy_lease=deploy_lease,
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
                deploy_lease=deploy_lease,
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
        lease_sha = str(
            ((deploy_lease or {}).get("lease") or {}).get(
                "deployed_sha"
            )
            or ""
        )
        if not re.fullmatch(r"[0-9a-f]{40}", lease_sha):
            raise RuntimeError(
                "Finance snapshot deploy lease lacks the exact deployed SHA"
            )
        boundary_classification = str(
            (
                transition_evidence.get("recovery_preflight") or {}
            ).get("boundary_classification")
            or ""
        )
        restore_job_id = _finance_snapshot_restore_job_id(
            deployed_sha=lease_sha,
            window_id=window_id,
            plan_fingerprint=fingerprint,
        )
        restore_inventory = (
            _run_remote_business_data_maintenance_restore_job(
                target,
                job_action="inventory",
                deployed_sha=lease_sha,
            )
        )
        transition_evidence["business_restore_job_inventory"] = (
            restore_inventory
        )
        exact_restore_status = (
            _run_remote_business_data_maintenance_restore_job(
                target,
                job_action="status",
                deployed_sha=lease_sha,
                job_id=restore_job_id,
                allow_absent=True,
            )
        )
        transition_evidence["business_restore_job_preflight"] = (
            exact_restore_status
        )
        if boundary_classification == "fresh_acquire":
            if (
                int(restore_inventory.get("nonterminal_job_count") or 0)
                != 0
                or restore_inventory.get("locks_free") is not True
                or restore_inventory.get("new_restore_submit_allowed")
                is not True
            ):
                raise RuntimeError(
                    "Finance snapshot pre-barrier restore inventory is not "
                    "quiescent"
                )
            if str(exact_restore_status.get("status") or "") != "absent":
                raise RuntimeError(
                    "Finance snapshot exact restore job already exists after "
                    "barrier release; the reviewed plan cannot be replayed"
                )
        elif boundary_classification == "exact_restore_release_resume":
            if str(exact_restore_status.get("status") or "") == "absent":
                if (
                    int(
                        restore_inventory.get(
                            "nonterminal_job_count"
                        )
                        or 0
                    )
                    != 0
                    or restore_inventory.get("locks_free") is not True
                    or restore_inventory.get(
                        "new_restore_submit_allowed"
                    )
                    is not True
                ):
                    raise RuntimeError(
                        "Finance snapshot exact restoring boundary has an "
                        "ambiguous restore inventory"
                    )
        elif boundary_classification == "exact_idempotent_resume":
            if (
                str(exact_restore_status.get("status") or "") != "absent"
                or int(
                    restore_inventory.get("nonterminal_job_count") or 0
                )
                != 0
                or restore_inventory.get("locks_free") is not True
                or restore_inventory.get("new_restore_submit_allowed")
                is not True
            ):
                raise RuntimeError(
                    "Finance snapshot held/acquiring resume has an ambiguous "
                    "restore inventory"
                )
        else:
            raise RuntimeError(
                "Finance snapshot recovery boundary classification is unsupported"
            )

        snapshot_error: Exception | None = None
        payload: dict[str, Any] = {}
        if boundary_classification != "exact_restore_release_resume":
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
                        f"also failed: hold={hold_error}; "
                        f"restore={recovery_error}"
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
                    deploy_lease=deploy_lease,
                )
            except Exception as exc:
                snapshot_error = exc
            transition_evidence["barrier_restoring"] = (
                _run_remote_business_data_maintenance_runner(
                    target,
                    action="barrier-restoring",
                    window_id=window_id,
                    plan_fingerprint=fingerprint,
                )
            )
        else:
            transition_evidence["outer_resume"] = {
                "classification": "exact_restore_release_resume",
                "barrier_acquire_replayed": False,
                "writer_hold_replayed": False,
                "snapshot_create_replayed": False,
                "restore_job_id": restore_job_id,
            }

        terminal_restore = _restore_finance_snapshot_window_durably(
            target,
            transition_evidence=transition_evidence,
            deployed_sha=lease_sha,
            window_id=window_id,
            plan_fingerprint=fingerprint,
        )
        post_restore_inventory = (
            _run_remote_business_data_maintenance_restore_job(
                target,
                job_action="inventory",
                deployed_sha=lease_sha,
            )
        )
        transition_evidence["business_restore_job_post_terminal_inventory"] = (
            post_restore_inventory
        )
        if (
            int(
                post_restore_inventory.get("nonterminal_job_count")
                or 0
            )
            != 0
            or post_restore_inventory.get("locks_free") is not True
        ):
            raise RuntimeError(
                "Finance snapshot restore terminal inventory is not quiescent"
            )
        post_restore_status = (
            _run_remote_business_data_maintenance_runner(
                target,
                action="status",
            )
        )
        transition_evidence["business_restore_independent_readback"] = (
            post_restore_status
        )
        terminal_policy_revision = int(
            (
                (
                    terminal_restore.get("result") or {}
                ).get("readback")
                or {}
            ).get("policy_revision")
            or -1
        )
        current_policy = dict(
            post_restore_status.get("auto_updates") or {}
        )
        writer_locks = dict(
            post_restore_status.get("writer_locks") or {}
        )
        locks_busy = any(
            (
                bool((value or {}).get("busy"))
                if key == "seller_portal"
                else bool((value or {}).get("held"))
            )
            for key, value in writer_locks.items()
            if isinstance(value, Mapping)
        )
        if (
            current_policy.get("master_desired") is not True
            or int(current_policy.get("revision") or -1)
            != terminal_policy_revision
            or list(
                current_policy.get("unknown_processes") or []
            )
            or list(current_policy.get("drift_processes") or [])
            or list(
                post_restore_status.get("unknown_wb_core_timers") or []
            )
            or locks_busy
        ):
            raise RuntimeError(
                "Finance snapshot independent writer/timer/policy readback "
                "is incomplete"
            )
        transition_evidence["warehouse_restore"] = (
            _run_remote_warehouse_functional_maintenance_action(
                target,
                action="restore",
            )
        )
        try:
            payload = _run_remote_finance_storage_split_action(
                target,
                action="snapshot-status",
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
                deploy_lease=deploy_lease,
            )
        except Exception as exc:
            if snapshot_error is None:
                snapshot_error = exc
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
            candidate_generation_epoch=candidate_generation_epoch,
            expected_retained_generation=(
                expected_retained_generation
            ),
            minimum_observation_seconds=minimum_observation_seconds,
            deploy_lease=deploy_lease,
            resume_failed_transport=bool(
                getattr(args, "finance_transport_resume", False)
            ),
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


def _finance_storage_transport_identity_args(
    runner_args: list[str],
) -> list[str]:
    stable: list[str] = []
    skip_transport_value = False
    for item in runner_args:
        if skip_transport_value:
            stable.append("<fresh-deploy-lease-transport>")
            skip_transport_value = False
            continue
        stable.append(item)
        if item == "--deploy-lease-json":
            skip_transport_value = True
    return stable


def _run_remote_finance_storage_transport_job(
    target: HostedRuntimeTarget,
    *,
    action: str,
    runner_args: list[str],
    reviewed_plan_json: str | None,
    deployed_sha: str,
    timeout_seconds: float,
    resume_failed: bool = False,
) -> dict[str, Any]:
    if (
        action not in FINANCE_STORAGE_DURABLE_HOLD_ACTIONS
        or re.fullmatch(r"[0-9a-f]{40}", deployed_sha) is None
    ):
        raise ValueError(
            "Finance durable transport requires an exact hold action/SHA"
        )
    stdin_text = reviewed_plan_json
    stdin_sha256 = (
        "sha256:"
        + hashlib.sha256(
            (stdin_text or "").encode("utf-8")
        ).hexdigest()
    )
    identity: dict[str, Any] = {
        "contract_name": (
            "wb_core_finance_storage_transport_identity_v1"
        ),
        "action": action,
        "deployed_sha": deployed_sha,
        "runner_args": _finance_storage_transport_identity_args(
            runner_args
        ),
        "stdin_sha256": stdin_sha256,
        "timeout_seconds": int(timeout_seconds),
    }
    job_id = hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    identity["job_id"] = job_id
    request_identity = "sha256:" + hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    request = {
        "contract_name": (
            "wb_core_finance_storage_transport_request_v1"
        ),
        "job_id": job_id,
        "request_identity": request_identity,
        "identity": identity,
        "action": action,
        "deployed_sha": deployed_sha,
        "repo_root": target.target_dir,
        "runner_args": runner_args,
        "stdin_text": stdin_text,
        "timeout_seconds": int(timeout_seconds),
    }
    runtime_sha_path = (
        f"{target.target_dir.rstrip('/')}/.wb-core-runtime-sha"
    )

    def remote_job(action_name: str) -> subprocess.CompletedProcess[str]:
        job_args = (
                "cd",
                target.target_dir,
                "python3",
                "apps/finance_storage_transport_job.py",
                action_name,
                "--runtime-dir",
                ACTIVE_HOSTED_RUNTIME_RUNTIME_DIR,
                "--job-id",
                job_id,
                "--deployed-sha-file",
                runtime_sha_path,
        )
        command = (
            f"cd {shlex.quote(job_args[1])} && "
            + " ".join(
                shlex.quote(item)
                for item in job_args[2:]
            )
        )
        return subprocess.run(
            _remote_shell_command(target, command),
            text=True,
            capture_output=True,
            input=(
                json.dumps(
                    request,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if action_name in {"submit", "resume"}
                else None
            ),
            cwd=ROOT,
            timeout=60.0,
            check=False,
        )

    print(
        "Finance storage exact transport job "
        f"{job_id}: starting or observing",
        file=sys.stderr,
        flush=True,
    )
    started_at = time.monotonic()
    last_transport_error = ""
    try:
        submitted = remote_job("resume" if resume_failed else "submit")
        if submitted.returncode != 0:
            last_transport_error = (
                submitted.stderr.strip()
                or submitted.stdout.strip()
                or f"submit exit {submitted.returncode}"
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        last_transport_error = str(exc)
    deadline = (
        started_at
        + float(timeout_seconds)
        + FINANCE_STORAGE_TRANSPORT_GRACE_SECONDS
    )
    while time.monotonic() < deadline:
        try:
            observed = remote_job("status")
        except (OSError, subprocess.TimeoutExpired) as exc:
            last_transport_error = str(exc)
            time.sleep(FINANCE_STORAGE_TRANSPORT_STATUS_POLL_SECONDS)
            continue
        if observed.returncode != 0:
            last_transport_error = (
                observed.stderr.strip()
                or observed.stdout.strip()
                or f"status exit {observed.returncode}"
            )
            time.sleep(FINANCE_STORAGE_TRANSPORT_STATUS_POLL_SECONDS)
            continue
        try:
            status = json.loads(observed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Finance storage durable transport returned invalid status"
            ) from exc
        if (
            not isinstance(status, dict)
            or str(status.get("job_id") or "") != job_id
            or str(status.get("deployed_sha") or "") != deployed_sha
        ):
            raise RuntimeError(
                "Finance storage durable transport identity drifted"
            )
        classification = str(
            status.get("worker_classification") or ""
        )
        if classification == "active_worker":
            time.sleep(FINANCE_STORAGE_TRANSPORT_STATUS_POLL_SECONDS)
            continue
        if (
            classification == "terminal_succeeded"
            and status.get("terminal") is True
            and str(status.get("status") or "") == "succeeded"
            and isinstance(status.get("result"), dict)
        ):
            return {
                **dict(status["result"]),
                "transport_job": {
                    "contract_name": status.get("contract_name"),
                    "job_id": job_id,
                    "request_identity": request_identity,
                    "deployed_sha": deployed_sha,
                    "status": "succeeded",
                    "transport_disconnect_recovered": bool(
                        last_transport_error
                    ),
                },
            }
        if classification == "terminal_failed":
            raise RuntimeError(
                f"Finance storage split {action} durable job failed: "
                + (
                    str(status.get("error") or "")
                    or f"exit {status.get('exit_code')}"
                )
            )
        raise RuntimeError(
            f"Finance storage split {action} durable job is fail-closed: "
            f"status={status.get('status')}; "
            f"classification={classification}"
        )
    raise RuntimeError(
        f"Finance storage split {action} durable job exceeded its "
        "bounded deadline: "
        + (last_transport_error or "no terminal status")
    )


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
    candidate_generation_epoch: str = "",
    expected_retained_generation: str = "",
    minimum_observation_seconds: int = 3600,
    rollback_candidate_evidence: str = "",
    deploy_lease: Mapping[str, Any] | None = None,
    recovery_action: str = "",
    resume_failed_transport: bool = False,
) -> dict[str, Any]:
    _ensure_active_hosted_runtime_target(
        target, action=f"finance-storage-split-{action}"
    )
    if resume_failed_transport and action != "snapshot-retention-apply":
        raise ValueError(
            "Finance transport resume is limited to snapshot-retention-apply"
        )
    if action not in {
        "dry-run",
        "health",
        "apply",
        "snapshot-plan",
        "snapshot-status",
        "snapshot-create",
        "snapshot-integrity",
        "snapshot-retention-plan",
        "snapshot-retention-apply",
        "snapshot-retention-readback",
        "candidate-abort-plan",
        "candidate-abort-apply",
        "candidate-abort-readback",
        "stale-writer-plan",
        "stale-writer-stop",
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
        "post-manifest-recovery-readback",
        "recovery-contract",
        "recovery-preflight",
    }:
        raise ValueError(f"unsupported Finance storage split action: {action}")
    if action in FINANCE_STORAGE_MUTATION_ACTIONS:
        _ensure_target_allows_mutation(
            target,
            action=f"finance-storage-split-{action}",
            dry_run=False,
        )
    effective_action = action
    if action == "recovery-preflight":
        effective_action = str(recovery_action or "").strip()
        if effective_action not in FINANCE_STORAGE_MUTATION_ACTIONS:
            raise ValueError(
                "Finance recovery preflight requires an exact mutation action"
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
    generation_filesystem_contract = dict(
        target.finance_generation_filesystem
    )
    if not generation_filesystem_contract:
        raise ValueError(
            "canonical Finance migration execution requires the target-owned "
            "generation filesystem contract"
        )
    runner_args.extend(
        [
            "--generation-filesystem-contract-json",
            json.dumps(
                generation_filesystem_contract,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ]
    )
    if action == "recovery-preflight":
        runner_args.extend(["--recovery-action", effective_action])
    if action not in {
        "health",
        "recovery-contract",
        "snapshot-status",
    }:
        if not isinstance(deploy_lease, Mapping):
            raise ValueError(
                "canonical Finance migration execution requires active "
                "deploy-lease evidence"
            )
        runner_args.extend(
            [
                "--deploy-lease-json",
                json.dumps(
                    deploy_lease,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ]
        )
    reviewed_plan_json: str | None = None
    if source_snapshot_manifest:
        runner_args.extend(
            [
                "--source-snapshot-manifest",
                source_snapshot_manifest,
            ]
        )
    if effective_action.startswith("candidate-abort-"):
        if not re.fullmatch(r"[0-9a-f]{20}", candidate_generation_epoch):
            raise ValueError(
                "Finance candidate abort requires an exact generation epoch"
            )
        if not re.fullmatch(
            r"sha256:[0-9a-f]{64}",
            candidate_plan_fingerprint,
        ):
            raise ValueError(
                "Finance candidate abort requires the exact old candidate "
                "plan fingerprint"
            )
        runner_args.extend(
            [
                "--candidate-generation-epoch",
                candidate_generation_epoch,
                "--candidate-plan-fingerprint",
                candidate_plan_fingerprint,
            ]
        )
    if effective_action == "post-manifest-recovery-readback":
        if not re.fullmatch(
            r"(?:rollback-)?[0-9a-f]{20}",
            expected_retained_generation,
        ):
            raise ValueError(
                "Finance post-manifest recovery readback requires the exact "
                "retained split generation"
            )
        runner_args.extend(
            [
                "--expected-retained-generation",
                expected_retained_generation,
            ]
        )
    if effective_action.startswith("shadow-") or effective_action in {
        "live-tail-apply",
        "cutover-plan",
        "cutover-apply",
    }:
        if not candidate_manifest:
            raise ValueError(
                f"Finance storage {effective_action} requires "
                "--candidate-manifest"
            )
        runner_args.extend(
            [
                "--candidate-manifest",
                candidate_manifest,
            ]
        )
    if effective_action in {"cutover-plan", "cutover-apply"}:
        if not candidate_plan_fingerprint.startswith("sha256:"):
            raise ValueError(
                f"Finance storage {effective_action} requires "
                "--candidate-plan-fingerprint"
            )
        runner_args.extend(
            [
                "--candidate-plan-fingerprint",
                candidate_plan_fingerprint,
            ]
        )
    if effective_action == "apply":
        if plan_path is None or not plan_path.is_file():
            raise ValueError("Finance storage split apply requires an existing --plan-file")
        reviewed_plan_json = plan_path.read_text(encoding="utf-8")
        plan = json.loads(reviewed_plan_json)
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
                "--migration-plan-file",
                "/dev/stdin",
                "--confirm-fingerprint",
                fingerprint,
                "--approval-reference",
                approval_reference.strip(),
            ]
        )
    elif effective_action in {"snapshot-create", "snapshot-status"}:
        if plan_path is None or not plan_path.is_file():
            raise ValueError(
                f"Finance storage {effective_action} requires an existing "
                "--plan-file"
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
                f"Finance storage {effective_action} requires "
                "--approval-reference"
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
    elif effective_action == "snapshot-integrity":
        if not source_snapshot_manifest:
            raise ValueError(
                "Finance storage snapshot-integrity requires "
                "--source-snapshot-manifest"
            )
    elif effective_action in {
        "snapshot-retention-apply",
        "snapshot-retention-readback",
    }:
        if plan_path is None or not plan_path.is_file():
            raise ValueError(
                f"Finance storage {effective_action} requires --plan-file"
            )
        reviewed_plan_json = plan_path.read_text(encoding="utf-8")
        plan = json.loads(reviewed_plan_json)
        if (
            not isinstance(plan, dict)
            or str(plan.get("contract_version") or "")
            != "wb_core_finance_storage_snapshot_retention_plan_v1"
            or str(plan.get("mode") or "")
            != "snapshot_retention_dry_run"
            or str(plan.get("fingerprint") or "") != fingerprint
            or not bool(plan.get("apply_allowed_by_machine_preflight"))
            or list(plan.get("blockers") or [])
        ):
            raise ValueError(
                "Finance snapshot retention reviewed plan is invalid"
            )
        if (
            effective_action == "snapshot-retention-apply"
            and not approval_reference.strip()
        ):
            raise ValueError(
                "Finance snapshot retention apply requires "
                "--approval-reference"
            )
        runner_args.extend(
            [
                "--snapshot-retention-plan-file",
                "/dev/stdin",
                "--confirm-fingerprint",
                fingerprint,
            ]
        )
        if effective_action == "snapshot-retention-apply":
            runner_args.extend(
                [
                    "--approval-reference",
                    approval_reference.strip(),
                ]
            )
    elif effective_action in {
        "candidate-abort-apply",
        "candidate-abort-readback",
    }:
        if plan_path is None or not plan_path.is_file():
            raise ValueError(
                f"Finance storage {effective_action} requires --plan-file"
            )
        reviewed_plan_json = plan_path.read_text(encoding="utf-8")
        plan = json.loads(reviewed_plan_json)
        if (
            not isinstance(plan, dict)
            or str(plan.get("contract_version") or "")
            != "wb_core_finance_storage_candidate_abort_plan_v1"
            or str(plan.get("mode") or "")
            != "candidate_abort_dry_run"
            or str(plan.get("fingerprint") or "") != fingerprint
            or str(plan.get("generation_epoch") or "")
            != candidate_generation_epoch
            or str(plan.get("candidate_plan_fingerprint") or "")
            != candidate_plan_fingerprint
            or not bool(
                plan.get(
                    "candidate_abort_allowed_by_machine_preflight"
                )
            )
        ):
            raise ValueError(
                "Finance candidate abort reviewed plan is invalid"
            )
        if (
            effective_action == "candidate-abort-apply"
            and not approval_reference.strip()
        ):
            raise ValueError(
                "Finance candidate abort apply requires "
                "--approval-reference"
            )
        runner_args.extend(
            [
                "--candidate-abort-plan-file",
                "/dev/stdin",
                "--confirm-fingerprint",
                fingerprint,
            ]
        )
        if effective_action == "candidate-abort-apply":
            runner_args.extend(
                [
                    "--approval-reference",
                    approval_reference.strip(),
                ]
            )
    elif effective_action == "stale-writer-stop":
        if plan_path is None or not plan_path.is_file():
            raise ValueError(
                "Finance stale-writer stop requires --plan-file"
            )
        reviewed_plan_json = plan_path.read_text(encoding="utf-8")
        plan = json.loads(reviewed_plan_json)
        if (
            not isinstance(plan, dict)
            or str(plan.get("contract_version") or "")
            != "wb_core_finance_storage_stale_writer_recovery_plan_v1"
            or str(plan.get("fingerprint") or "") != fingerprint
            or str(plan.get("mode") or "")
            != "stale_writer_recovery_dry_run"
            or not bool(plan.get("stop_allowed_by_machine_preflight"))
        ):
            raise ValueError(
                "Finance stale-writer recovery plan is not ready"
            )
        if not approval_reference.strip():
            raise ValueError(
                "Finance stale-writer stop requires --approval-reference"
            )
        runner_args.extend(
            [
                "--stale-writer-plan-file",
                "/dev/stdin",
                "--confirm-fingerprint",
                fingerprint,
                "--approval-reference",
                approval_reference.strip(),
            ]
        )
    elif effective_action in {
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
        if effective_action == "shadow-deactivate":
            runner_args.extend(
                ["--reason", "repo-owned shadow lifecycle transition"]
            )
        elif effective_action == "shadow-verify":
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
    elif effective_action == "cutover-apply":
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
    elif effective_action in {"rollback-prepare", "rollback-apply"}:
        if plan_path is None or not plan_path.is_file():
            raise ValueError(
                f"Finance storage {effective_action} requires --plan-file"
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
        if effective_action == "rollback-apply":
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
    remote_timeout_seconds = (
        FINANCE_STORAGE_SPLIT_MUTATION_TIMEOUT_SECONDS
        if action in {
                "apply",
                "snapshot-create",
                "snapshot-retention-plan",
                "snapshot-retention-apply",
                "snapshot-retention-readback",
                "candidate-abort-apply",
                "candidate-abort-readback",
                "stale-writer-stop",
                "shadow-reconcile",
                "shadow-verify",
                "live-tail-apply",
                "cutover-apply",
                "rollback-prepare",
                "rollback-apply",
            }
        else FINANCE_STORAGE_SPLIT_READ_TIMEOUT_SECONDS
    )
    if action in FINANCE_STORAGE_DURABLE_HOLD_ACTIONS:
        deployed_sha = str(
            ((deploy_lease or {}).get("lease") or {}).get(
                "deployed_sha"
            )
            or ""
        )
        payload = _run_remote_finance_storage_transport_job(
            target,
            action=action,
            runner_args=runner_args,
            reviewed_plan_json=reviewed_plan_json,
            deployed_sha=deployed_sha,
            timeout_seconds=remote_timeout_seconds,
            resume_failed=resume_failed_transport,
        )
        result = None
    else:
        result = subprocess.run(
            _remote_shell_command(target, shell_command),
            text=True,
            capture_output=True,
            input=reviewed_plan_json,
            cwd=ROOT,
            timeout=remote_timeout_seconds,
            check=False,
        )
    if result is None:
        stdout_lines: list[str] = []
    else:
        if result.returncode != 0:
            raise RuntimeError(
                f"Finance storage split {action} failed: "
                + (
                    result.stderr.strip()
                    or result.stdout.strip()
                    or f"exit {result.returncode}"
                )
            )
        stdout_lines = [
            line
            for line in result.stdout.splitlines()
            if line.strip()
        ]
        try:
            payload = json.loads(
                stdout_lines[-1] if stdout_lines else ""
            )
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Finance storage split runner returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError(
                "Finance storage split runner returned non-object JSON"
            )
    if action in {
        "dry-run",
        "health",
        "snapshot-plan",
        "snapshot-retention-plan",
        "snapshot-retention-readback",
        "candidate-abort-plan",
        "candidate-abort-readback",
        "stale-writer-plan",
        "recovery-contract",
        "recovery-preflight",
        "post-manifest-recovery-readback",
    }:
        if action == "dry-run" and (
            payload.get("query_only_contract", {}).get("production_mutation_count") != 0
            or payload.get("query_only_contract", {}).get("destination_bytes_created") != 0
        ):
            raise RuntimeError("Finance storage dry-run did not prove zero mutation/bytes")
        if action == "stale-writer-plan" and (
            (payload.get("action") or {}).get(
                "business_data_mutation_count"
            )
            != 0
            or (payload.get("action") or {}).get(
                "finance_storage_mutation_count"
            )
            != 0
        ):
            raise RuntimeError(
                "Finance stale-writer plan did not prove zero business/data mutation"
            )
        if action == "snapshot-retention-plan" and (
            payload.get("query_only_contract", {}).get(
                "business_data_mutation_count"
            )
            != 0
            or payload.get("query_only_contract", {}).get(
                "snapshot_byte_mutation_count"
            )
            != 0
            or payload.get("query_only_contract", {}).get(
                "archive_byte_mutation_count"
            )
            != 0
        ):
            raise RuntimeError(
                "Finance snapshot retention plan did not prove zero mutation"
            )
        if action == "snapshot-retention-readback" and (
            payload.get("status") != "readback_verified"
            or payload.get("capacity_sufficient") is not True
            or payload.get("live_monolith_touched") is not False
            or payload.get("split_generation_touched") is not False
        ):
            raise RuntimeError(
                "Finance snapshot retention readback is incomplete"
            )
        if action == "candidate-abort-plan" and (
            payload.get(
                "candidate_abort_allowed_by_machine_preflight"
            )
            is not True
            or payload.get("canonical_source") != "monolith"
            or payload.get("canonical_manifest_switch_planned") is not False
            or payload.get("query_only_contract", {}).get(
                "business_data_mutation_count"
            )
            != 0
            or payload.get("query_only_contract", {}).get(
                "candidate_byte_mutation_count"
            )
            != 0
            or payload.get("query_only_contract", {}).get(
                "manifest_mutation_count"
            )
            != 0
        ):
            raise RuntimeError(
                "Finance candidate abort plan lacks zero-mutation proof"
            )
        if action == "candidate-abort-readback" and (
            payload.get("status") != "completed"
            or payload.get("readback", {}).get(
                "candidate_root_absent"
            )
            is not True
            or payload.get("readback", {}).get(
                "global_manifest_absent"
            )
            is not True
            or payload.get("readback", {}).get(
                "non_target_unchanged"
            )
            is not True
        ):
            raise RuntimeError(
                "Finance candidate abort readback is incomplete"
            )
        if action == "recovery-contract" and (
            payload.get("status") != "ready"
            or re.fullmatch(
                r"[0-9a-f]{40}",
                str(payload.get("deployed_sha") or ""),
            )
            is None
            or payload.get("fail_closed_default") is not True
            or payload.get("second_restore_job_allowed") is not False
            or not str(payload.get("fingerprint") or "").startswith(
                "sha256:"
            )
        ):
            raise RuntimeError(
                "Finance recovery contract lacks fail-closed capability proof"
            )
        if action == "post-manifest-recovery-readback" and (
            payload.get("query_only") is not True
            or payload.get("canonical_source") != "monolith"
            or payload.get("raw", {}).get("match") is not True
            or payload.get("operational", {}).get(
                "non_cache_match"
            )
            is not True
            or payload.get("cache", {}).get(
                "semantic_mismatch_count"
            )
            != 0
            or payload.get("cache", {}).get(
                "direct_row_copy_allowed"
            )
            is not False
        ):
            raise RuntimeError(
                "Finance post-manifest recovery readback is incomplete"
            )
        if action == "recovery-preflight" and (
            payload.get("status") != "ready"
            or payload.get("fail_closed") is not True
            or payload.get("action") != effective_action
            or payload.get("phase") != "pre_barrier"
        ):
            raise RuntimeError(
                "Finance recovery preflight lacks exact ready evidence"
            )
    elif action == "stale-writer-stop" and (
        str(payload.get("status") or "")
        not in {"stopped", "already_completed"}
        or int(payload.get("stop_count") or 0) not in {0, 1}
    ):
        raise RuntimeError(
            "Finance stale-writer stop lacks exact terminal readback"
        )
    elif action == "snapshot-retention-apply" and (
        str(payload.get("status") or "") != "completed"
        or (
            str(payload.get("strategy") or "")
            == "post_cutover_atomic_replace_v1"
            and (
                payload.get("replacement_verified") is not True
                or int(payload.get("retained_backup_count") or 0) != 1
            )
        )
        or (
            str(payload.get("strategy") or "")
            != "post_cutover_atomic_replace_v1"
            and int(payload.get("archived_snapshot_count") or 0) <= 0
        )
        or payload.get("live_monolith_touched") is not False
        or payload.get("split_generation_touched") is not False
    ):
        raise RuntimeError(
            "Finance snapshot retention apply lacks exact terminal readback"
        )
    elif action == "snapshot-retention-readback" and (
        str(payload.get("status") or "") != "readback_verified"
        or payload.get("live_monolith_touched") is not False
        or payload.get("split_generation_touched") is not False
        or (
            str(payload.get("strategy") or "")
            == "post_cutover_atomic_replace_v1"
            and (
                payload.get("replacement_verified") is not True
                or payload.get("restore_drill_verified") is not True
                or int(payload.get("retained_backup_count") or 0) != 1
                or int(payload.get("root_long_lived_snapshot_count") or 0) != 0
                or int(payload.get("backup_legacy_snapshot_count") or 0) != 0
            )
        )
    ):
        raise RuntimeError(
            "Finance snapshot retention readback lacks exact terminal proof"
        )
    elif action == "candidate-abort-apply" and (
        str(payload.get("status") or "") != "completed"
        or payload.get("readback", {}).get("candidate_root_absent")
        is not True
        or payload.get("readback", {}).get("global_manifest_absent")
        is not True
        or payload.get("readback", {}).get("non_target_unchanged")
        is not True
    ):
        raise RuntimeError(
            "Finance candidate abort apply lacks exact terminal readback"
        )
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


def run_ff_stage_7a_production_command(args: argparse.Namespace) -> int:
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    action = str(args.ff_stage_7a_action)
    plan_path = Path(str(args.plan_file)).resolve() if action == "apply" else None
    if plan_path is not None and (plan_path == ROOT or ROOT in plan_path.parents):
        raise ValueError("Stage 7A reviewed plan must stay outside the Git checkout")
    payload = _run_remote_ff_stage_7a_production(
        target,
        action=action,
        deployed_sha=str(args.deployed_sha).strip().lower(),
        plan_path=plan_path,
        fingerprint=str(getattr(args, "fingerprint", "") or ""),
        approval_reference=str(getattr(args, "approval_reference", "") or ""),
        actor=str(getattr(args, "actor", "") or ""),
    )
    output = str(getattr(args, "output", "") or "").strip()
    if output:
        output_path = Path(output).resolve()
        if output_path == ROOT or ROOT in output_path.parents:
            raise ValueError("Stage 7A evidence output must stay outside the Git checkout")
        _write_private_json(output_path, payload)
    _print_json(
        {
            "target_id": target.target_id,
            "ssh_destination": target.ssh_destination,
            "runtime_dir": str(target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""),
            "action": f"ff-stage-7a-production-{action}",
            "result": payload,
        }
    )
    return 0


def run_ff_pool_zero_physical_production_command(args: argparse.Namespace) -> int:
    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    action = str(args.ff_pool_zero_physical_action)
    plan_path = Path(str(args.plan_file)).resolve() if action == "apply" else None
    if plan_path is not None and (plan_path == ROOT or ROOT in plan_path.parents):
        raise ValueError("zero-physical reviewed plan must stay outside the Git checkout")
    payload = _run_remote_ff_pool_zero_physical_production(
        target,
        action=action,
        deployed_sha=str(args.deployed_sha).strip().lower(),
        plan_path=plan_path,
        fingerprint=str(getattr(args, "fingerprint", "") or ""),
        approval_reference=str(getattr(args, "approval_reference", "") or ""),
        actor=str(getattr(args, "actor", "") or ""),
    )
    output = str(getattr(args, "output", "") or "").strip()
    if output:
        output_path = Path(output).resolve()
        if output_path == ROOT or ROOT in output_path.parents:
            raise ValueError("zero-physical evidence output must stay outside the Git checkout")
        _write_private_json(output_path, payload)
    _print_json(
        {
            "target_id": target.target_id,
            "ssh_destination": target.ssh_destination,
            "runtime_dir": str(
                target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""
            ),
            "action": f"ff-pool-zero-physical-production-{action}",
            "result": payload,
        }
    )
    return 0


def run_ff_pool_cutover_production_command(args: argparse.Namespace) -> int:
    """Run the canonical dry-run/readback or owner-gated Stage 7C apply."""

    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    action = str(args.ff_pool_cutover_action)
    plan_path = Path(str(args.plan_file)).resolve() if action == "apply" else None
    if plan_path is not None and (plan_path == ROOT or ROOT in plan_path.parents):
        raise ValueError("Stage 7C reviewed plan must stay outside the Git checkout")
    payload = _run_remote_ff_pool_cutover_production(
        target,
        action=action,
        deployed_sha=str(args.deployed_sha).strip().lower(),
        excluded_shipment_ids=tuple(
            str(item).strip() for item in getattr(args, "excluded_shipment_id", [])
        ),
        opening_facility_id=str(getattr(args, "opening_facility_id", "") or ""),
        proposed_window_minutes=int(
            getattr(args, "proposed_window_minutes", 15) or 15
        ),
        plan_path=plan_path,
        fingerprint=str(getattr(args, "fingerprint", "") or ""),
        approval_reference=str(
            getattr(args, "approval_reference", "") or ""
        ),
        actor=str(getattr(args, "actor", "") or ""),
    )
    output_path = Path(str(args.output)).resolve()
    if output_path == ROOT or ROOT in output_path.parents:
        raise ValueError("Stage 7C evidence output must stay outside the Git checkout")
    _write_private_json(output_path, payload)
    _print_json(
        {
            "target_id": target.target_id,
            "ssh_destination": target.ssh_destination,
            "runtime_dir": str(
                target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""
            ),
            "action": f"ff-pool-cutover-production-{action}",
            "result": payload,
        }
    )
    return 0


def run_ff_pool_recovery_supersession_command(args: argparse.Namespace) -> int:
    """Run query-only proof/readback or one exact owner-gated supersession."""

    target_file = args.target_file or resolve_target_file()
    target = load_hosted_runtime_target(target_file)
    action = str(args.ff_pool_recovery_supersession_action)
    plan_path = Path(str(args.plan_file)).resolve() if action == "apply" else None
    if plan_path is not None and (plan_path == ROOT or ROOT in plan_path.parents):
        raise ValueError(
            "Stage 7C recovery supersession plan must stay outside the Git checkout"
        )
    payload = _run_remote_ff_pool_recovery_supersession(
        target,
        action=action,
        deployed_sha=str(args.deployed_sha).strip().lower(),
        operation_id=str(getattr(args, "operation_id", "") or ""),
        plan_path=plan_path,
        fingerprint=str(getattr(args, "fingerprint", "") or ""),
        approval_reference=str(
            getattr(args, "approval_reference", "") or ""
        ),
        actor=str(getattr(args, "actor", "") or ""),
    )
    output_path = Path(str(args.output)).resolve()
    if output_path == ROOT or ROOT in output_path.parents:
        raise ValueError(
            "Stage 7C recovery supersession evidence must stay outside the Git checkout"
        )
    _write_private_json(output_path, payload)
    _print_json(
        {
            "target_id": target.target_id,
            "ssh_destination": target.ssh_destination,
            "runtime_dir": str(
                target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""
            ),
            "action": f"ff-pool-recovery-supersession-{action}",
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


def run_ff_inventory_reconciliation_command(args: argparse.Namespace) -> int:
    target = load_hosted_runtime_target(args.target_file or resolve_target_file())
    action = str(args.ff_inventory_action)
    source_path = Path(str(args.source_file)).resolve()
    if not source_path.is_file():
        raise ValueError("FF inventory source workbook does not exist")
    source_bytes = source_path.read_bytes()
    payload = _run_remote_ff_inventory_reconciliation(
        target,
        action=action,
        source_bytes=source_bytes,
        source_filename=str(args.source_filename or source_path.name),
        business_date=str(args.business_date),
        return_supply_ids=tuple(str(item) for item in args.return_supply_id),
        fingerprint=str(args.fingerprint or ""),
        approval_reference=str(args.approval_reference or ""),
        created_by=str(args.created_by or "operator"),
        rollback_reason=str(args.rollback_reason or ""),
    )
    output = str(args.output or "").strip()
    if output:
        output_path = Path(output).resolve()
        if output_path == ROOT or ROOT in output_path.parents:
            raise ValueError("FF inventory evidence output must stay outside the Git checkout")
        _write_private_json(output_path, payload)
    _print_json(
        {
            "target_id": target.target_id,
            "ssh_destination": target.ssh_destination,
            "action": f"ff-inventory-reconciliation-{action}",
            "source_sha256": "sha256:" + hashlib.sha256(source_bytes).hexdigest(),
            "result": payload,
        }
    )
    return 0


def _run_remote_ff_inventory_reconciliation(
    target: HostedRuntimeTarget,
    *,
    action: str,
    source_bytes: bytes,
    source_filename: str,
    business_date: str,
    return_supply_ids: tuple[str, ...],
    fingerprint: str,
    approval_reference: str,
    created_by: str,
    rollback_reason: str,
) -> dict[str, Any]:
    _ensure_active_hosted_runtime_target(
        target,
        action=f"ff-inventory-reconciliation-{action}",
    )
    if action not in {"dry-run", "apply", "readback", "rollback"}:
        raise ValueError(f"unsupported FF inventory action: {action}")
    if action in {"apply", "rollback"}:
        _ensure_target_allows_mutation(
            target,
            action=f"ff-inventory-reconciliation-{action}",
            dry_run=False,
        )
    normalized_date = date.fromisoformat(business_date).isoformat()
    runtime_dir = str(target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or "").strip()
    if runtime_dir != ACTIVE_HOSTED_RUNTIME_RUNTIME_DIR:
        raise ValueError("FF inventory reconciliation requires the canonical active runtime dir")
    if not source_bytes:
        raise ValueError("FF inventory source workbook is empty")
    runner_args = [
        "python3",
        "apps/ff_inventory_reconciliation.py",
        "--runtime-dir",
        runtime_dir,
        "--source-base64-stdin",
        "--source-filename",
        source_filename,
        "--business-date",
        normalized_date,
        "--created-by",
        created_by,
        "--compact",
    ]
    for supply_id in sorted({item.strip() for item in return_supply_ids if item.strip()}):
        runner_args.extend(["--return-supply-id", supply_id])
    if action == "apply":
        if not fingerprint or not approval_reference:
            raise ValueError("FF inventory apply requires exact fingerprint and approval reference")
        runner_args.extend(
            [
                "--apply",
                "--confirm-fingerprint",
                fingerprint,
                "--approval-reference",
                approval_reference,
            ]
        )
    elif action == "readback":
        runner_args.append("--readback")
    elif action == "rollback":
        if not fingerprint or not approval_reference or not rollback_reason:
            raise ValueError("FF inventory rollback requires fingerprint, approval reference and reason")
        runner_args.extend(
            [
                "--rollback",
                "--confirm-fingerprint",
                fingerprint,
                "--approval-reference",
                approval_reference,
                "--rollback-reason",
                rollback_reason,
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
        input=base64.b64encode(source_bytes).decode("ascii"),
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=FF_INVENTORY_RECONCILIATION_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode not in ({0, 2} if action == "dry-run" else {0}):
        raise RuntimeError(
            f"FF inventory reconciliation {action} failed: "
            + (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("FF inventory reconciliation returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("FF inventory reconciliation returned a non-object")
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


def _run_remote_ff_stage_7a_production(
    target: HostedRuntimeTarget,
    *,
    action: str,
    deployed_sha: str,
    plan_path: Path | None,
    fingerprint: str,
    approval_reference: str,
    actor: str,
) -> dict[str, Any]:
    _ensure_active_hosted_runtime_target(target, action=f"ff-stage-7a-production-{action}")
    if action not in {"dry-run", "apply", "readback"}:
        raise ValueError(f"unsupported Stage 7A production action: {action}")
    if action == "apply":
        _ensure_target_allows_mutation(
            target,
            action="ff-stage-7a-production-apply",
            dry_run=False,
        )
    if not re.fullmatch(r"[0-9a-f]{40}", deployed_sha):
        raise ValueError("Stage 7A production action requires an exact deployed SHA")
    runtime_dir = str(target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or "").strip()
    if runtime_dir != ACTIVE_HOSTED_RUNTIME_RUNTIME_DIR:
        raise ValueError("Stage 7A production action requires the canonical active runtime dir")
    if target.environment_file != ACTIVE_HOSTED_RUNTIME_ENVIRONMENT_FILE:
        raise ValueError("Stage 7A production action requires the canonical environment file")
    if target.service_name != ACTIVE_HOSTED_RUNTIME_SERVICE_NAME:
        raise ValueError("Stage 7A production action requires the canonical HTTP service")

    runner_args = [
        "python3",
        "apps/ff_stage_7a_production.py",
        "--runtime-dir",
        runtime_dir,
        "--env-file",
        target.environment_file,
        "--deployed-sha",
        deployed_sha,
        "--compact",
        action,
    ]
    reviewed_plan_json = ""
    if action == "apply":
        if plan_path is None or not plan_path.is_file():
            raise ValueError("Stage 7A production apply requires an existing --plan-file")
        reviewed_plan_json = plan_path.read_text(encoding="utf-8")
        try:
            plan = json.loads(reviewed_plan_json)
        except json.JSONDecodeError as exc:
            raise ValueError("Stage 7A reviewed plan is invalid JSON") from exc
        if (
            not isinstance(plan, dict)
            or plan.get("contract_name") != "ff_stage_7a_production_mutation_v1"
            or int(plan.get("contract_version") or 0) != 1
            or plan.get("mode") != "dry_run"
            or plan.get("apply_allowed") is not True
            or str(plan.get("deployed_sha") or "") != deployed_sha
            or str(plan.get("fingerprint") or "") != fingerprint
        ):
            raise ValueError("Stage 7A reviewed plan does not match this exact apply")
        if not approval_reference.strip() or not actor.strip():
            raise ValueError("Stage 7A production apply requires approval reference and actor")
        runner_args.extend(
            [
                "--reviewed-plan-stdin",
                "--fingerprint",
                fingerprint,
                "--approval-reference",
                approval_reference.strip(),
                "--actor",
                actor.strip(),
                "--backup-dir",
                "/opt/wb-core-runtime/state/backups/ff-stage-7a-production",
            ]
        )
    runtime_sha_path = f"{target.target_dir.rstrip('/')}/.wb-core-runtime-sha"
    shell_command = " && ".join(
        [
            f"test \"$(cat {shlex.quote(runtime_sha_path)})\" = {shlex.quote(deployed_sha)}",
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
        timeout=FF_STAGE_7A_PRODUCTION_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Stage 7A production {action} failed: "
            + (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Stage 7A production runner returned invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("status") in {"blocked", "error"}:
        raise RuntimeError("Stage 7A production runner returned an invalid result")

    if action == "apply":
        restart = _restart_ff_stage_7a_http_service(target)
        readback = _run_remote_ff_stage_7a_production(
            target,
            action="readback",
            deployed_sha=deployed_sha,
            plan_path=None,
            fingerprint="",
            approval_reference="",
            actor="",
        )
        names = {str(item.get("name") or ""): bool(item.get("active")) for item in readback.get("facilities") or []}
        state = readback.get("collector_state") or {}
        if (
            names != {"FF Москва": True, "FF Оренбург": False}
            or not (readback.get("collector_configuration") or {}).get("enabled")
            or state.get("last_status") != "success"
            or state.get("complete") is not True
            or int(state.get("next_cursor") or 0) != 0
        ):
            raise RuntimeError("Stage 7A post-restart query-only readback is incomplete")
        payload = {
            **payload,
            "http_service_restart": restart,
            "post_restart_readback": readback,
        }
    return payload


def _run_remote_ff_pool_zero_physical_production(
    target: HostedRuntimeTarget,
    *,
    action: str,
    deployed_sha: str,
    plan_path: Path | None,
    fingerprint: str,
    approval_reference: str,
    actor: str,
) -> dict[str, Any]:
    _ensure_active_hosted_runtime_target(
        target, action=f"ff-pool-zero-physical-production-{action}"
    )
    if action not in {"dry-run", "apply", "readback"}:
        raise ValueError(f"unsupported zero-physical production action: {action}")
    if action == "apply":
        _ensure_target_allows_mutation(
            target,
            action="ff-pool-zero-physical-production-apply",
            dry_run=False,
        )
    if not re.fullmatch(r"[0-9a-f]{40}", deployed_sha):
        raise ValueError("zero-physical production action requires an exact deployed SHA")
    runtime_dir = str(
        target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""
    ).strip()
    if runtime_dir != ACTIVE_HOSTED_RUNTIME_RUNTIME_DIR:
        raise ValueError("zero-physical action requires the canonical active runtime dir")
    if target.environment_file != ACTIVE_HOSTED_RUNTIME_ENVIRONMENT_FILE:
        raise ValueError("zero-physical action requires the canonical environment file")
    if target.service_name != ACTIVE_HOSTED_RUNTIME_SERVICE_NAME:
        raise ValueError("zero-physical action requires the canonical HTTP service")

    runner_args = [
        "python3",
        "apps/ff_pool_zero_physical_production.py",
        "--runtime-dir",
        runtime_dir,
        "--deployed-sha",
        deployed_sha,
        "--compact",
        action,
    ]
    reviewed_plan_json = ""
    if action == "apply":
        if plan_path is None or not plan_path.is_file():
            raise ValueError("zero-physical apply requires an existing --plan-file")
        reviewed_plan_json = plan_path.read_text(encoding="utf-8")
        try:
            plan = json.loads(reviewed_plan_json)
        except json.JSONDecodeError as exc:
            raise ValueError("zero-physical reviewed plan is invalid JSON") from exc
        if (
            not isinstance(plan, dict)
            or plan.get("contract_name") != "ff_pool_zero_physical_production_v1"
            or int(plan.get("contract_version") or 0) != 1
            or plan.get("mode") != "dry_run"
            or plan.get("apply_allowed") is not True
            or str(plan.get("deployed_sha") or "") != deployed_sha
            or str(plan.get("fingerprint") or "") != fingerprint
            or str((plan.get("scope") or {}).get("facility_name") or "")
            != "FF Москва"
            or str((plan.get("scope") or {}).get("pool") or "") != "FBS"
            or list((plan.get("scope") or {}).get("nm_ids") or [])
            != [497413772, 497415593, 497416931]
            or (plan.get("scope") or {}).get("absolute_physical_target") != 0
        ):
            raise ValueError("zero-physical reviewed plan does not match the exact apply")
        if not approval_reference.strip() or not actor.strip():
            raise ValueError("zero-physical apply requires approval reference and actor")
        runner_args.extend(
            [
                "--reviewed-plan-stdin",
                "--fingerprint",
                fingerprint,
                "--approval-reference",
                approval_reference.strip(),
                "--actor",
                actor.strip(),
                "--evidence-dir",
                "/opt/wb-core-runtime/backups/ff-pool-zero-physical-production",
            ]
        )
    runtime_sha_path = f"{target.target_dir.rstrip('/')}/.wb-core-runtime-sha"
    shell_command = " && ".join(
        [
            f"test \"$(cat {shlex.quote(runtime_sha_path)})\" = {shlex.quote(deployed_sha)}",
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
        timeout=FF_POOL_ZERO_PHYSICAL_PRODUCTION_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"zero-physical production {action} failed: "
            + (
                result.stderr.strip()
                or result.stdout.strip()
                or f"exit {result.returncode}"
            )
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("zero-physical production runner returned invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("status") in {"blocked", "error"}:
        raise RuntimeError("zero-physical production runner returned an invalid result")
    if action == "apply":
        readback = _run_remote_ff_pool_zero_physical_production(
            target,
            action="readback",
            deployed_sha=deployed_sha,
            plan_path=None,
            fingerprint="",
            approval_reference="",
            actor="",
        )
        target_rows = list(readback.get("target_rows") or [])
        status = readback.get("fbs_status_read_model") or {}
        if (
            [int(item.get("nm_id") or 0) for item in target_rows]
            != [497413772, 497415593, 497416931]
            or any(item.get("state") != "explicit_zero" for item in target_rows)
            or status.get("target_nm_ids_unblocked") is not True
            or list(status.get("target_nm_ids_missing") or [])
        ):
            raise RuntimeError("post-apply zero-physical query-only readback is incomplete")
        payload = {**payload, "post_apply_readback": readback}
    return payload


def _run_remote_ff_pool_cutover_production(
    target: HostedRuntimeTarget,
    *,
    action: str,
    deployed_sha: str,
    excluded_shipment_ids: tuple[str, ...],
    opening_facility_id: str,
    proposed_window_minutes: int,
    plan_path: Path | None,
    fingerprint: str,
    approval_reference: str,
    actor: str,
) -> dict[str, Any]:
    """Own the exact-SHA Stage 7C runner and its canonical write barriers."""

    _ensure_active_hosted_runtime_target(
        target, action=f"ff-pool-cutover-production-{action}"
    )
    if action not in {"dry-run", "apply", "readback"}:
        raise ValueError(f"unsupported Stage 7C production action: {action}")
    if action == "apply":
        _ensure_target_allows_mutation(
            target, action="ff-pool-cutover-production-apply", dry_run=False
        )
    if not re.fullmatch(r"[0-9a-f]{40}", deployed_sha):
        raise ValueError("Stage 7C production action requires an exact deployed SHA")
    runtime_dir = str(
        target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""
    ).strip()
    if runtime_dir != ACTIVE_HOSTED_RUNTIME_RUNTIME_DIR:
        raise ValueError("Stage 7C production action requires the canonical runtime dir")
    if target.environment_file != ACTIVE_HOSTED_RUNTIME_ENVIRONMENT_FILE:
        raise ValueError("Stage 7C production action requires the canonical environment file")
    if target.service_name != ACTIVE_HOSTED_RUNTIME_SERVICE_NAME:
        raise ValueError("Stage 7C production action requires the canonical HTTP service")

    reviewed_plan: dict[str, Any] | None = None
    if action == "dry-run":
        if not excluded_shipment_ids:
            raise ValueError("Stage 7C dry-run requires an excluded pending shipment")
    elif action == "apply":
        if plan_path is None or not plan_path.is_file():
            raise ValueError("Stage 7C apply requires an existing reviewed plan")
        try:
            reviewed_plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("Stage 7C reviewed plan is invalid JSON") from exc
        if (
            not isinstance(reviewed_plan, dict)
            or reviewed_plan.get("contract_name")
            != FF_POOL_CUTOVER_PRODUCTION_CONTRACT_NAME
            or int(reviewed_plan.get("contract_version") or 0)
            != FF_POOL_CUTOVER_PRODUCTION_CONTRACT_VERSION
            or reviewed_plan.get("mode") != "dry_run_owner_gate"
            or reviewed_plan.get("apply_allowed") is not True
            or bool(reviewed_plan.get("blockers"))
            or str(reviewed_plan.get("deployed_sha") or "") != deployed_sha
            or str(reviewed_plan.get("fingerprint") or "") != fingerprint
        ):
            raise ValueError("Stage 7C reviewed plan does not match this exact apply")
        if not approval_reference.strip() or not actor.strip():
            raise ValueError("Stage 7C apply requires approval reference and actor")

    if action != "apply":
        return _run_remote_ff_pool_cutover_runner(
            target,
            action=action,
            deployed_sha=deployed_sha,
            excluded_shipment_ids=excluded_shipment_ids,
            opening_facility_id=opening_facility_id,
            proposed_window_minutes=proposed_window_minutes,
            reviewed_envelope=None,
            fingerprint="",
            approval_reference="",
            actor="",
        )

    window_id = "ff-pool-cutover-" + fingerprint.removeprefix("sha256:")[:24]
    transition_evidence: dict[str, Any] = {}
    fbs_before = _read_remote_fbs_collector_timer(target)
    if fbs_before.get("active") is not True or fbs_before.get("enabled") is not True:
        raise RuntimeError(
            "Stage 7C requires the five-minute FBS collector to remain active"
        )
    hold = _acquire_and_confirm_finance_storage_window(
        target,
        transition_evidence=transition_evidence,
        actor="ff_pool_cutover_runner",
        reason="owner-gated exact facility/pool opening and FBS cutover",
        window_id=window_id,
        window_kind="final_cutover",
        fingerprint=fingerprint,
        approval_reference=approval_reference,
    )
    fbs_during = _read_remote_fbs_collector_timer(target)
    barrier_confirm = dict(transition_evidence.get("barrier_confirm") or {})
    warehouse_hold = dict(transition_evidence.get("warehouse_hold") or {})
    warehouse_lock = dict(warehouse_hold.get("warehouse_lock") or {})
    external_barrier = {
        "maintenance_quiet": hold.get("quiet") is True,
        "http_write_barrier_active": (
            barrier_confirm.get("active") is True
            and barrier_confirm.get("hold_confirmed") is True
            and str(barrier_confirm.get("phase") or "") == "held"
        ),
        "warehouse_timer_held": str(warehouse_hold.get("status") or "") == "held",
        "warehouse_lock_held": bool(warehouse_lock.get("held")),
        "supplier_acceptance_writer_held": (
            hold.get("quiet") is True
            and barrier_confirm.get("hold_confirmed") is True
        ),
        "fbs_collector_continues": (
            fbs_before.get("active") is True
            and fbs_before.get("enabled") is True
            and fbs_during.get("active") is True
            and fbs_during.get("enabled") is True
        ),
        "canonical_target": True,
        "evidence": {
            "window_id": window_id,
            "business_hold_fingerprint": _ff_pool_evidence_fingerprint(hold),
            "barrier_fingerprint": _ff_pool_evidence_fingerprint(barrier_confirm),
            "warehouse_hold_fingerprint": _ff_pool_evidence_fingerprint(warehouse_hold),
            "fbs_timer_before": fbs_before,
            "fbs_timer_under_barrier": fbs_during,
        },
    }
    if not all(
        external_barrier[key] is True
        for key in (
            "maintenance_quiet",
            "http_write_barrier_active",
            "warehouse_timer_held",
            "supplier_acceptance_writer_held",
            "fbs_collector_continues",
        )
    ) or external_barrier["warehouse_lock_held"] is not False:
        raise RuntimeError(
            "Stage 7C exact external barrier evidence is incomplete; barriers remain held"
        )
    reviewed_envelope = {
        "reviewed_plan": reviewed_plan,
        "external_barrier": external_barrier,
    }
    try:
        applied = _run_remote_ff_pool_cutover_runner(
            target,
            action="apply",
            deployed_sha=deployed_sha,
            excluded_shipment_ids=(),
            opening_facility_id="",
            proposed_window_minutes=15,
            reviewed_envelope=reviewed_envelope,
            fingerprint=fingerprint,
            approval_reference=approval_reference,
            actor=actor,
        )
    except Exception as exc:
        raise RuntimeError(
            "Stage 7C apply failed or became ambiguous; canonical barriers remain held "
            f"for exact readback/recovery: {exc}"
        ) from exc
    if str(applied.get("status") or "") not in {
        "applied_reconciled",
        "already_applied_reconciled",
    }:
        raise RuntimeError("Stage 7C apply did not reach reconciled state; barriers remain held")
    restore = _restore_ff_pool_cutover_window(
        target,
        hold=hold,
        window_id=window_id,
        fingerprint=fingerprint,
    )
    fbs_after = _read_remote_fbs_collector_timer(target)
    if fbs_after.get("active") is not True or fbs_after.get("enabled") is not True:
        raise RuntimeError("Stage 7C restored controls but the five-minute FBS collector is not active")
    readback = _run_remote_ff_pool_cutover_runner(
        target,
        action="readback",
        deployed_sha=deployed_sha,
        excluded_shipment_ids=(),
        opening_facility_id="",
        proposed_window_minutes=15,
        reviewed_envelope=None,
        fingerprint="",
        approval_reference="",
        actor="",
    )
    cutover = dict(readback.get("cutover") or {})
    if (
        str(readback.get("status") or "") != "applied"
        or str((cutover.get("readback") or {}).get("status") or "") != "pass"
    ):
        raise RuntimeError("Stage 7C post-restore exact readback failed")
    return {
        **applied,
        "canonical_barrier_acquire": transition_evidence,
        "canonical_barrier_restore": restore,
        "fbs_collector_before": fbs_before,
        "fbs_collector_under_barrier": fbs_during,
        "fbs_collector_after": fbs_after,
        "post_restore_readback": readback,
    }


def _run_remote_ff_pool_cutover_runner(
    target: HostedRuntimeTarget,
    *,
    action: str,
    deployed_sha: str,
    excluded_shipment_ids: tuple[str, ...],
    opening_facility_id: str,
    proposed_window_minutes: int,
    reviewed_envelope: Mapping[str, Any] | None,
    fingerprint: str,
    approval_reference: str,
    actor: str,
) -> dict[str, Any]:
    runtime_dir = str(target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or "").strip()
    runner_args = [
        "python3",
        "apps/ff_pool_cutover_production.py",
        "--runtime-dir",
        runtime_dir,
        "--env-file",
        target.environment_file,
        "--deployed-sha",
        deployed_sha,
        "--compact",
        action,
    ]
    runner_input: str | None = None
    if action == "dry-run":
        for shipment_id in excluded_shipment_ids:
            runner_args.extend(["--excluded-shipment-id", shipment_id])
        if opening_facility_id:
            runner_args.extend(["--opening-facility-id", opening_facility_id])
        runner_args.extend(
            ["--proposed-window-minutes", str(max(5, min(proposed_window_minutes, 60)))]
        )
    elif action == "apply":
        if reviewed_envelope is None:
            raise ValueError("Stage 7C apply requires the reviewed envelope")
        runner_args.extend(
            [
                "--reviewed-envelope-stdin",
                "--fingerprint",
                fingerprint,
                "--approval-reference",
                approval_reference,
                "--actor",
                actor,
                "--backup-dir",
                "/opt/wb-core-runtime/state/backups/ff-pool-cutover-production",
            ]
        )
        runner_input = json.dumps(reviewed_envelope, ensure_ascii=False, sort_keys=True)
    runtime_sha_path = f"{target.target_dir.rstrip('/')}/.wb-core-runtime-sha"
    shell_command = " && ".join(
        [
            f"test \"$(cat {shlex.quote(runtime_sha_path)})\" = {shlex.quote(deployed_sha)}",
            f"cd {shlex.quote(target.target_dir)}",
            " ".join(shlex.quote(item) for item in runner_args),
        ]
    )
    result = subprocess.run(
        _remote_shell_command(target, shell_command),
        text=True,
        capture_output=True,
        input=runner_input,
        cwd=ROOT,
        timeout=FF_POOL_CUTOVER_PRODUCTION_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Stage 7C production {action} failed: "
            + (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Stage 7C production runner returned invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("status") in {"blocked", "error"}:
        raise RuntimeError("Stage 7C production runner returned an invalid result")
    if action == "dry-run" and (
        payload.get("contract_name") != FF_POOL_CUTOVER_PRODUCTION_CONTRACT_NAME
        or int(payload.get("contract_version") or 0)
        != FF_POOL_CUTOVER_PRODUCTION_CONTRACT_VERSION
        or payload.get("mode") != "dry_run_owner_gate"
    ):
        raise RuntimeError("Stage 7C dry-run contract mismatch")
    return payload


def _run_remote_ff_pool_recovery_supersession(
    target: HostedRuntimeTarget,
    *,
    action: str,
    deployed_sha: str,
    operation_id: str,
    plan_path: Path | None,
    fingerprint: str,
    approval_reference: str,
    actor: str,
) -> dict[str, Any]:
    """Bind supersession proof/apply to the canonical runtime and exact code SHA."""

    action_name = f"ff-pool-recovery-supersession-{action}"
    _ensure_active_hosted_runtime_target(target, action=action_name)
    if action not in {"dry-run", "apply", "readback"}:
        raise ValueError(f"unsupported Stage 7C recovery supersession action: {action}")
    if action == "apply":
        _ensure_target_allows_mutation(target, action=action_name, dry_run=False)
    if not re.fullmatch(r"[0-9a-f]{40}", deployed_sha):
        raise ValueError(
            "Stage 7C recovery supersession requires an exact deployed SHA"
        )
    runtime_dir = str(
        target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""
    ).strip()
    if runtime_dir != ACTIVE_HOSTED_RUNTIME_RUNTIME_DIR:
        raise ValueError(
            "Stage 7C recovery supersession requires the canonical runtime dir"
        )
    if target.service_name != ACTIVE_HOSTED_RUNTIME_SERVICE_NAME:
        raise ValueError(
            "Stage 7C recovery supersession requires the canonical HTTP service"
        )

    runner_input: str | None = None
    runner_args = [
        "python3",
        "apps/ff_pool_cutover_recovery_supersession.py",
        "--runtime-dir",
        runtime_dir,
        "--deployed-sha",
        deployed_sha,
        "--compact",
        action,
    ]
    if action in {"dry-run", "readback"}:
        if not re.fullmatch(r"recovery_[0-9a-f]{32}(?:_g[0-9]+)?", operation_id):
            raise ValueError(
                "Stage 7C recovery supersession requires an exact recovery operation id"
            )
        runner_args.extend(["--operation-id", operation_id])
    else:
        if plan_path is None or not plan_path.is_file():
            raise ValueError(
                "Stage 7C recovery supersession apply requires an existing reviewed plan"
            )
        try:
            reviewed_plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                "Stage 7C recovery supersession reviewed plan is invalid JSON"
            ) from exc
        if (
            not isinstance(reviewed_plan, dict)
            or reviewed_plan.get("contract_name")
            != FF_POOL_RECOVERY_SUPERSESSION_CONTRACT_NAME
            or int(reviewed_plan.get("contract_version") or 0)
            != FF_POOL_RECOVERY_SUPERSESSION_CONTRACT_VERSION
            or reviewed_plan.get("mode") != "dry_run_exact_supersession"
            or reviewed_plan.get("status") != "ready"
            or reviewed_plan.get("apply_allowed") is not True
            or reviewed_plan.get("would_change") is not True
            or bool(reviewed_plan.get("blockers"))
            or str(reviewed_plan.get("deployed_sha") or "") != deployed_sha
            or str(reviewed_plan.get("fingerprint") or "") != fingerprint
        ):
            raise ValueError(
                "Stage 7C recovery supersession plan does not match this exact apply"
            )
        if not approval_reference.strip() or not actor.strip():
            raise ValueError(
                "Stage 7C recovery supersession apply requires approval reference and actor"
            )
        runner_args.extend(
            [
                "--reviewed-plan-stdin",
                "--fingerprint",
                fingerprint,
                "--approval-reference",
                approval_reference,
                "--actor",
                actor,
            ]
        )
        runner_input = json.dumps(reviewed_plan, ensure_ascii=False, sort_keys=True)

    runtime_sha_path = f"{target.target_dir.rstrip('/')}/.wb-core-runtime-sha"
    shell_command = " && ".join(
        [
            f"test \"$(cat {shlex.quote(runtime_sha_path)})\" = {shlex.quote(deployed_sha)}",
            f"cd {shlex.quote(target.target_dir)}",
            " ".join(shlex.quote(item) for item in runner_args),
        ]
    )
    result = subprocess.run(
        _remote_shell_command(target, shell_command),
        text=True,
        capture_output=True,
        input=runner_input,
        cwd=ROOT,
        timeout=FF_POOL_RECOVERY_SUPERSESSION_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Stage 7C recovery supersession {action} failed: "
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
            "Stage 7C recovery supersession runner returned invalid JSON"
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("contract_name")
        != FF_POOL_RECOVERY_SUPERSESSION_CONTRACT_NAME
        or int(payload.get("contract_version") or 0)
        != FF_POOL_RECOVERY_SUPERSESSION_CONTRACT_VERSION
        or payload.get("status") in {"blocked", "error", "not_superseded", "missing"}
    ):
        raise RuntimeError(
            "Stage 7C recovery supersession runner returned an invalid result"
        )
    if action == "dry-run" and (
        payload.get("mode") != "dry_run_exact_supersession"
        or payload.get("status") not in {"ready", "already_applied"}
        or str(payload.get("deployed_sha") or "") != deployed_sha
    ):
        raise RuntimeError(
            "Stage 7C recovery supersession dry-run contract mismatch"
        )
    if action == "readback" and payload.get("status") != "superseded_verified":
        raise RuntimeError(
            "Stage 7C recovery supersession readback did not verify terminal state"
        )
    return payload


def _read_remote_fbs_collector_timer(target: HostedRuntimeTarget) -> dict[str, Any]:
    unit = "wb-core-fbs-shadow-collector.timer"
    result = subprocess.run(
        _remote_shell_command(
            target,
            f"systemctl show {shlex.quote(unit)} --property=ActiveState,UnitFileState --no-pager",
        ),
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=300.0,
        check=False,
    )
    properties = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            properties[key.strip()] = value.strip()
    if result.returncode != 0 or not properties:
        raise RuntimeError(
            "FBS collector timer readback failed: "
            + (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")
        )
    return {
        "unit": unit,
        "active": properties.get("ActiveState") == "active",
        "enabled": properties.get("UnitFileState") == "enabled",
        "properties": properties,
    }


def _ff_pool_evidence_fingerprint(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _restore_ff_pool_cutover_window(
    target: HostedRuntimeTarget,
    *,
    hold: Mapping[str, Any],
    window_id: str,
    fingerprint: str,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    evidence["barrier_restoring"] = _run_remote_business_data_maintenance_runner(
        target,
        action="barrier-restoring",
        window_id=window_id,
        plan_fingerprint=fingerprint,
    )
    paused_revision = int(((hold.get("auto_updates") or {}).get("revision") or 0))
    if paused_revision <= 0:
        raise RuntimeError("Stage 7C hold lacks the exact paused policy revision")
    restore = _run_remote_business_data_maintenance_runner(
        target,
        action="restore",
        expected_revision=paused_revision,
        actor="ff_pool_cutover_runner",
        reason="Stage 7C exact readback passed",
    )
    evidence["business_restore"] = restore
    if (
        str(restore.get("status") or "") != "restored"
        or restore.get("exact_prior_state_restored") is not True
    ):
        raise RuntimeError("Stage 7C exact writer/timer restore is incomplete")
    evidence["warehouse_restore"] = _run_remote_warehouse_functional_maintenance_action(
        target, action="restore"
    )
    evidence["barrier_release"] = _run_remote_business_data_maintenance_runner(
        target,
        action="barrier-release",
        actor="ff_pool_cutover_runner",
        reason="Stage 7C exact prior controls restored",
        window_id=window_id,
        plan_fingerprint=fingerprint,
    )
    return evidence


def _restart_ff_stage_7a_http_service(target: HostedRuntimeTarget) -> dict[str, Any]:
    command = (
        f"systemctl restart {shlex.quote(ACTIVE_HOSTED_RUNTIME_SERVICE_NAME)}"
        f" && systemctl is-active {shlex.quote(ACTIVE_HOSTED_RUNTIME_SERVICE_NAME)}"
        f" && systemctl show --property MainPID --value "
        f"{shlex.quote(ACTIVE_HOSTED_RUNTIME_SERVICE_NAME)}"
    )
    result = subprocess.run(
        _remote_shell_command(target, command),
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=300.0,
        check=False,
    )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    try:
        pid = int(lines[-1])
    except (IndexError, ValueError):
        pid = 0
    if result.returncode != 0 or lines[:1] != ["active"] or pid <= 0:
        raise RuntimeError(
            "Stage 7A HTTP service restart/readback failed: "
            + (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")
        )
    return {"service": ACTIVE_HOSTED_RUNTIME_SERVICE_NAME, "active": True, "main_pid": pid}


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
        boundary_kind = str(continuity.get("boundary_kind") or "")
        services = list(continuity.get("services") or [])
        supported_boundary = (
            not boundary_kind and bool(services)
        ) or (
            boundary_kind == "quiet_confirmed_hold" and not services
        )
        if (
            str(result.get("status") or "") != "ready"
            or not re.fullmatch(
                r"sha256:[0-9a-f]{64}",
                str(continuity.get("fingerprint") or ""),
            )
            or not supported_boundary
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


def _run_remote_warehouse_july_recovery_action(
    target: HostedRuntimeTarget,
    *,
    action: str,
    batch: str,
    plan_path: Path | None = None,
    fingerprint: str = "",
    approval_reference: str = "",
    reason: str = "",
    batch_a_fingerprint: str = "",
    backup_path: str = "",
    source_sha256: str = "",
    business_date: str = "",
) -> dict[str, Any]:
    action_name = f"warehouse-july-recovery-{action}"
    _ensure_active_hosted_runtime_target(target, action=action_name)
    if action not in {"dry-run", "apply", "rollback"}:
        raise ValueError(f"unsupported July warehouse action: {action}")
    if batch not in {"a", "b", "transit", "projection"}:
        raise ValueError(f"unsupported July warehouse batch: {batch}")
    if action in {"apply", "rollback"}:
        _ensure_target_allows_mutation(
            target,
            action=action_name,
            dry_run=False,
        )
    if batch == "transit" and action != "dry-run":
        raise ValueError(
            "transit backup evidence is query-only; recovery requires fresh "
            "Seller Portal ingestion or a separately reviewed contract"
        )
    runtime_dir = str(
        target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""
    ).strip()
    if runtime_dir != ACTIVE_HOSTED_RUNTIME_RUNTIME_DIR:
        raise ValueError(
            "July warehouse recovery requires the canonical active runtime dir"
        )
    runner_args = [
        "python3",
        "apps/warehouse_historical_recovery.py",
        "--runtime-dir",
        runtime_dir,
        "--batch",
        batch,
    ]
    if batch == "projection" and action != "rollback":
        if not source_sha256 or not business_date:
            raise ValueError(
                "projection recovery requires exact source SHA and business date"
            )
        runner_args.extend(
            [
                "--source-sha256",
                source_sha256,
                "--business-date",
                business_date,
            ]
        )
    if batch == "transit":
        normalized_backup = Path(str(backup_path or ""))
        allowed_roots = (
            Path("/opt/wb-core-runtime/backups"),
            Path("/opt/wb-core-runtime/state/backups"),
        )
        if (
            not normalized_backup.is_absolute()
            or not any(
                normalized_backup == root or root in normalized_backup.parents
                for root in allowed_roots
            )
            or normalized_backup.suffix not in {".sqlite3", ".zst"}
        ):
            raise ValueError(
                "transit backup path must be one exact canonical backup file"
            )
        runner_args.extend(
            [
                "--backup-path",
                str(normalized_backup),
            ]
        )
    if action == "apply":
        if (
            plan_path is None
            or not fingerprint
            or not approval_reference.strip()
        ):
            raise ValueError(
                "July warehouse apply requires reviewed plan, exact fingerprint "
                "and approval reference"
            )
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        expected_contract = {
            "a": "warehouse_historical_recovery_2026_07_v2",
            "b": "warehouse_early_wb_recovery_2026_07_v1",
            "projection": (
                "warehouse_business_projection_exact_functional_recovery_v1"
            ),
        }[batch]
        if (
            not isinstance(plan, dict)
            or str(plan.get("contract_name") or "") != expected_contract
            or str(plan.get("fingerprint") or "") != fingerprint
            or (
                batch == "projection"
                and (
                    str(plan.get("source_sha256") or "") != source_sha256
                    or str(plan.get("business_date") or "") != business_date
                )
            )
        ):
            raise ValueError(
                "July warehouse reviewed plan identity/fingerprint mismatch"
            )
        runner_args.extend(
            [
                "--apply",
                "--fingerprint",
                fingerprint,
                "--approval-reference",
                approval_reference,
            ]
        )
        if batch == "b":
            if not batch_a_fingerprint:
                raise ValueError(
                    "Batch B apply requires reconciled Batch A fingerprint"
                )
            runner_args.extend(
                ["--batch-a-fingerprint", batch_a_fingerprint]
            )
    elif action == "rollback":
        if not fingerprint or not reason.strip():
            raise ValueError(
                "July warehouse rollback requires exact fingerprint and reason"
            )
        runner_args.extend(
            [
                "--rollback",
                "--fingerprint",
                fingerprint,
                "--reason",
                reason,
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
        timeout=_warehouse_opening_timeout_seconds(action),
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"July warehouse {batch} {action} failed: "
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
            "July warehouse runner returned invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            "July warehouse runner returned a non-object JSON payload"
        )
    return payload


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
        "ff_inventory_capital_20260803",
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


def _add_finance_migration_deploy_lease_argument(
    parser: argparse.ArgumentParser,
) -> None:
    parser.add_argument(
        "--finance-deploy-lease-evidence",
        required=True,
        help=(
            "Fresh private JSON readback from the GitHub-owned global "
            "Finance migration deploy lease."
        ),
    )


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
    deploy_and_verify.add_argument(
        "--output",
        type=Path,
        help="Write the exact deploy/probe evidence as a private JSON artifact.",
    )
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

    autoanswers_backlog_recovery = subparsers.add_parser(
        "autoanswers-backlog-recovery",
        help=(
            "Capture, plan, explicitly apply or query-only reconcile an exact "
            "Autoanswers unanswered T0 cohort."
        ),
    )
    autoanswers_backlog_recovery.add_argument(
        "action",
        choices=("capture", "dry-run", "apply", "readback"),
    )
    autoanswers_backlog_recovery.add_argument(
        "--expected-deployed-sha",
        required=True,
    )
    autoanswers_backlog_recovery.add_argument("--manifest-file", default="")
    autoanswers_backlog_recovery.add_argument("--reviewed-plan-file", default="")
    autoanswers_backlog_recovery.add_argument("--fingerprint", default="")
    autoanswers_backlog_recovery.add_argument("--approval-reference", default="")
    autoanswers_backlog_recovery.add_argument("--actor", default="release-train")
    autoanswers_backlog_recovery.add_argument("--output", default="")
    autoanswers_backlog_recovery.set_defaults(
        handler=run_autoanswers_backlog_recovery_command
    )

    autoanswers_policy_v5_reconciliation = subparsers.add_parser(
        "autoanswers-policy-v5-reconciliation",
        help=(
            "Plan, atomically apply or query-only read back owner-policy v5 "
            "for every zero-write publication while the worker is held."
        ),
    )
    autoanswers_policy_v5_reconciliation.add_argument(
        "action",
        choices=("dry-run", "apply", "readback"),
    )
    autoanswers_policy_v5_reconciliation.add_argument(
        "--expected-deployed-sha",
        required=True,
    )
    autoanswers_policy_v5_reconciliation.add_argument(
        "--reviewed-plan-file",
        default="",
    )
    autoanswers_policy_v5_reconciliation.add_argument("--fingerprint", default="")
    autoanswers_policy_v5_reconciliation.add_argument(
        "--actor",
        default="release-train",
    )
    autoanswers_policy_v5_reconciliation.add_argument("--output", default="")
    autoanswers_policy_v5_reconciliation.set_defaults(
        handler=run_autoanswers_policy_v5_reconciliation_command
    )

    autoanswers_answered_inventory_recovery = subparsers.add_parser(
        "autoanswers-answered-inventory-recovery",
        help=(
            "Capture, plan, explicitly apply or query-only reconcile stale "
            "local observations from the full official WB answered inventory."
        ),
    )
    autoanswers_answered_inventory_recovery.add_argument(
        "action",
        choices=("capture", "dry-run", "apply", "readback"),
    )
    autoanswers_answered_inventory_recovery.add_argument(
        "--expected-deployed-sha",
        required=True,
    )
    autoanswers_answered_inventory_recovery.add_argument(
        "--manifest-file",
        default="",
    )
    autoanswers_answered_inventory_recovery.add_argument(
        "--reviewed-plan-file",
        default="",
    )
    autoanswers_answered_inventory_recovery.add_argument(
        "--fingerprint",
        default="",
    )
    autoanswers_answered_inventory_recovery.add_argument(
        "--approval-reference",
        default="",
    )
    autoanswers_answered_inventory_recovery.add_argument(
        "--actor",
        default="release-train",
    )
    autoanswers_answered_inventory_recovery.add_argument("--output", default="")
    autoanswers_answered_inventory_recovery.set_defaults(
        handler=run_autoanswers_answered_inventory_recovery_command
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
    finance_canonical_dry_run.add_argument("--operation-id", default="")
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
    finance_canonical_apply.add_argument("--operation-id", default="")
    finance_canonical_apply.set_defaults(
        handler=run_finance_canonical_command,
        finance_canonical_action="apply",
    )

    finance_canonical_readback = subparsers.add_parser(
        "finance-canonical-readback",
        help="Prove zero all-history Finance deltas/blockers after canonical apply.",
    )
    finance_canonical_readback.add_argument("--operation-id", default="")
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
    _add_finance_migration_deploy_lease_argument(
        finance_storage_split_dry_run
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

    finance_storage_post_manifest_recovery = subparsers.add_parser(
        "finance-storage-post-manifest-recovery-readback",
        help=(
            "Prove query-only core equality and bounded regenerable cache "
            "drift for an exact retained split generation."
        ),
    )
    finance_storage_post_manifest_recovery.add_argument(
        "--expected-retained-generation",
        required=True,
    )
    finance_storage_post_manifest_recovery.add_argument(
        "--output",
        required=True,
    )
    finance_storage_post_manifest_recovery.add_argument(
        "--chunk-size",
        type=int,
        default=10_000,
    )
    _add_finance_migration_deploy_lease_argument(
        finance_storage_post_manifest_recovery
    )
    finance_storage_post_manifest_recovery.set_defaults(
        handler=run_finance_storage_split_command,
        finance_storage_split_action=(
            "post-manifest-recovery-readback"
        ),
    )

    finance_storage_recovery_contract = subparsers.add_parser(
        "finance-storage-recovery-contract",
        help=(
            "Read the deployed fail-closed Finance recovery matrix and "
            "runner-version capability fingerprint without mutation."
        ),
    )
    finance_storage_recovery_contract.add_argument("--output", default="")
    finance_storage_recovery_contract.add_argument(
        "--chunk-size",
        type=int,
        default=10_000,
    )
    finance_storage_recovery_contract.set_defaults(
        handler=run_finance_storage_split_command,
        finance_storage_split_action="recovery-contract",
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
    _add_finance_migration_deploy_lease_argument(
        finance_storage_split_apply
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
    _add_finance_migration_deploy_lease_argument(
        finance_storage_snapshot_plan
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
    _add_finance_migration_deploy_lease_argument(
        finance_storage_snapshot_apply
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
    _add_finance_migration_deploy_lease_argument(
        finance_storage_snapshot_integrity
    )
    finance_storage_snapshot_integrity.set_defaults(
        handler=run_finance_storage_split_command,
        finance_storage_split_action="snapshot-integrity",
    )

    finance_storage_snapshot_retention_plan = subparsers.add_parser(
        "finance-storage-snapshot-retention-plan",
        help=(
            "Plan exact pre-cutover archival or post-cutover atomic Finance "
            "backup replacement and superseded-snapshot release."
        ),
    )
    finance_storage_snapshot_retention_plan.add_argument(
        "--output",
        required=True,
    )
    finance_storage_snapshot_retention_plan.add_argument(
        "--chunk-size",
        type=int,
        default=10_000,
    )
    _add_finance_migration_deploy_lease_argument(
        finance_storage_snapshot_retention_plan
    )
    finance_storage_snapshot_retention_plan.set_defaults(
        handler=run_finance_storage_split_command,
        finance_storage_split_action="snapshot-retention-plan",
    )

    finance_storage_snapshot_retention_apply = subparsers.add_parser(
        "finance-storage-snapshot-retention-apply",
        help=(
            "Apply one exact durable Finance snapshot/backup retention plan; "
            "post-cutover selection precedes every superseded release."
        ),
    )
    finance_storage_snapshot_retention_apply.add_argument(
        "--plan-file",
        required=True,
    )
    finance_storage_snapshot_retention_apply.add_argument(
        "--fingerprint",
        required=True,
    )
    finance_storage_snapshot_retention_apply.add_argument(
        "--approval-reference",
        required=True,
    )
    finance_storage_snapshot_retention_apply.add_argument(
        "--chunk-size",
        type=int,
        default=10_000,
    )
    _add_finance_migration_deploy_lease_argument(
        finance_storage_snapshot_retention_apply
    )
    finance_storage_snapshot_retention_apply.set_defaults(
        handler=run_finance_storage_split_command,
        finance_storage_split_action="snapshot-retention-apply",
    )

    finance_storage_snapshot_retention_resume = subparsers.add_parser(
        "finance-storage-snapshot-retention-resume",
        help=(
            "Resume the same exact failed durable post-cutover Finance "
            "retention job without creating a second request identity."
        ),
    )
    finance_storage_snapshot_retention_resume.add_argument(
        "--plan-file",
        required=True,
    )
    finance_storage_snapshot_retention_resume.add_argument(
        "--fingerprint",
        required=True,
    )
    finance_storage_snapshot_retention_resume.add_argument(
        "--approval-reference",
        required=True,
    )
    finance_storage_snapshot_retention_resume.add_argument(
        "--chunk-size",
        type=int,
        default=10_000,
    )
    _add_finance_migration_deploy_lease_argument(
        finance_storage_snapshot_retention_resume
    )
    finance_storage_snapshot_retention_resume.set_defaults(
        handler=run_finance_storage_split_command,
        finance_storage_split_action="snapshot-retention-apply",
        finance_transport_resume=True,
    )

    finance_storage_snapshot_retention_readback = subparsers.add_parser(
        "finance-storage-snapshot-retention-readback",
        help=(
            "Independently verify retained restore bytes, terminal "
            "transactions, released superseded copies and recovered capacity."
        ),
    )
    finance_storage_snapshot_retention_readback.add_argument(
        "--plan-file",
        required=True,
    )
    finance_storage_snapshot_retention_readback.add_argument(
        "--fingerprint",
        required=True,
    )
    finance_storage_snapshot_retention_readback.add_argument(
        "--output",
        required=True,
    )
    finance_storage_snapshot_retention_readback.add_argument(
        "--chunk-size",
        type=int,
        default=10_000,
    )
    _add_finance_migration_deploy_lease_argument(
        finance_storage_snapshot_retention_readback
    )
    finance_storage_snapshot_retention_readback.set_defaults(
        handler=run_finance_storage_split_command,
        finance_storage_split_action="snapshot-retention-readback",
    )

    finance_storage_candidate_abort_plan = subparsers.add_parser(
        "finance-storage-candidate-abort-plan",
        help=(
            "Build an exact query-only recovery plan for one unselected "
            "pre-manifest partial Finance candidate."
        ),
    )
    finance_storage_candidate_abort_plan.add_argument(
        "--candidate-generation-epoch",
        required=True,
    )
    finance_storage_candidate_abort_plan.add_argument(
        "--candidate-plan-fingerprint",
        required=True,
    )
    finance_storage_candidate_abort_plan.add_argument(
        "--output",
        required=True,
    )
    finance_storage_candidate_abort_plan.add_argument(
        "--chunk-size",
        type=int,
        default=10_000,
    )
    _add_finance_migration_deploy_lease_argument(
        finance_storage_candidate_abort_plan
    )
    finance_storage_candidate_abort_plan.set_defaults(
        handler=run_finance_storage_split_command,
        finance_storage_split_action="candidate-abort-plan",
    )

    finance_storage_candidate_abort_apply = subparsers.add_parser(
        "finance-storage-candidate-abort-apply",
        help=(
            "Durably release only the exact reviewed unselected partial "
            "candidate; never touch the monolith, snapshots, or manifests."
        ),
    )
    finance_storage_candidate_abort_apply.add_argument(
        "--plan-file",
        required=True,
    )
    finance_storage_candidate_abort_apply.add_argument(
        "--fingerprint",
        required=True,
    )
    finance_storage_candidate_abort_apply.add_argument(
        "--approval-reference",
        required=True,
    )
    finance_storage_candidate_abort_apply.add_argument(
        "--candidate-generation-epoch",
        required=True,
    )
    finance_storage_candidate_abort_apply.add_argument(
        "--candidate-plan-fingerprint",
        required=True,
    )
    finance_storage_candidate_abort_apply.add_argument(
        "--output",
        required=True,
    )
    finance_storage_candidate_abort_apply.add_argument(
        "--chunk-size",
        type=int,
        default=10_000,
    )
    _add_finance_migration_deploy_lease_argument(
        finance_storage_candidate_abort_apply
    )
    finance_storage_candidate_abort_apply.set_defaults(
        handler=run_finance_storage_split_command,
        finance_storage_split_action="candidate-abort-apply",
    )

    finance_storage_candidate_abort_readback = subparsers.add_parser(
        "finance-storage-candidate-abort-readback",
        help=(
            "Independently prove the exact candidate is absent and the "
            "canonical monolith/snapshots/non-target state stayed unchanged."
        ),
    )
    finance_storage_candidate_abort_readback.add_argument(
        "--plan-file",
        required=True,
    )
    finance_storage_candidate_abort_readback.add_argument(
        "--fingerprint",
        required=True,
    )
    finance_storage_candidate_abort_readback.add_argument(
        "--candidate-generation-epoch",
        required=True,
    )
    finance_storage_candidate_abort_readback.add_argument(
        "--candidate-plan-fingerprint",
        required=True,
    )
    finance_storage_candidate_abort_readback.add_argument(
        "--output",
        required=True,
    )
    finance_storage_candidate_abort_readback.add_argument(
        "--chunk-size",
        type=int,
        default=10_000,
    )
    _add_finance_migration_deploy_lease_argument(
        finance_storage_candidate_abort_readback
    )
    finance_storage_candidate_abort_readback.set_defaults(
        handler=run_finance_storage_split_command,
        finance_storage_split_action="candidate-abort-readback",
    )

    finance_storage_stale_writer_plan = subparsers.add_parser(
        "finance-storage-stale-writer-plan",
        help=(
            "Build a read-only exact-generation recovery plan for the "
            "bounded closure-retry oneshot; never stop a process."
        ),
    )
    finance_storage_stale_writer_plan.add_argument("--output", required=True)
    _add_finance_migration_deploy_lease_argument(
        finance_storage_stale_writer_plan
    )
    finance_storage_stale_writer_plan.set_defaults(
        handler=run_finance_storage_split_command,
        finance_storage_split_action="stale-writer-plan",
    )

    finance_storage_stale_writer_stop = subparsers.add_parser(
        "finance-storage-stale-writer-stop",
        help=(
            "Stop only one reviewed stale closure-retry generation while "
            "preserving its timer and owner policy."
        ),
    )
    finance_storage_stale_writer_stop.add_argument(
        "--plan-file",
        required=True,
    )
    finance_storage_stale_writer_stop.add_argument(
        "--fingerprint",
        required=True,
    )
    finance_storage_stale_writer_stop.add_argument(
        "--approval-reference",
        required=True,
    )
    _add_finance_migration_deploy_lease_argument(
        finance_storage_stale_writer_stop
    )
    finance_storage_stale_writer_stop.set_defaults(
        handler=run_finance_storage_split_command,
        finance_storage_split_action="stale-writer-stop",
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
        _add_finance_migration_deploy_lease_argument(command)
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
    _add_finance_migration_deploy_lease_argument(
        finance_storage_cutover_plan
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
    _add_finance_migration_deploy_lease_argument(
        finance_storage_cutover_apply
    )
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
    _add_finance_migration_deploy_lease_argument(
        finance_storage_rollback_plan
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
    _add_finance_migration_deploy_lease_argument(
        finance_storage_rollback_prepare
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
    _add_finance_migration_deploy_lease_argument(
        finance_storage_rollback_apply
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

    ff_stage_7a_dry_run = subparsers.add_parser(
        "ff-stage-7a-production-dry-run",
        help="Build the exact official-ID facility/FBS shadow production plan.",
    )
    ff_stage_7a_dry_run.add_argument("--deployed-sha", required=True)
    ff_stage_7a_dry_run.add_argument("--output", required=True)
    ff_stage_7a_dry_run.set_defaults(
        handler=run_ff_stage_7a_production_command,
        ff_stage_7a_action="dry-run",
    )

    ff_stage_7a_apply = subparsers.add_parser(
        "ff-stage-7a-production-apply",
        help="Apply one exact reviewed owner-gated Stage 7A production plan.",
    )
    ff_stage_7a_apply.add_argument("--deployed-sha", required=True)
    ff_stage_7a_apply.add_argument("--plan-file", required=True)
    ff_stage_7a_apply.add_argument("--fingerprint", required=True)
    ff_stage_7a_apply.add_argument("--approval-reference", required=True)
    ff_stage_7a_apply.add_argument("--actor", required=True)
    ff_stage_7a_apply.add_argument("--output", required=True)
    ff_stage_7a_apply.set_defaults(
        handler=run_ff_stage_7a_production_command,
        ff_stage_7a_action="apply",
    )

    ff_stage_7a_readback = subparsers.add_parser(
        "ff-stage-7a-production-readback",
        help="Run query-only Stage 7A facility/FBS shadow reconciliation.",
    )
    ff_stage_7a_readback.add_argument("--deployed-sha", required=True)
    ff_stage_7a_readback.add_argument("--output", required=True)
    ff_stage_7a_readback.set_defaults(
        handler=run_ff_stage_7a_production_command,
        ff_stage_7a_action="readback",
    )

    zero_physical_dry_run = subparsers.add_parser(
        "ff-pool-zero-physical-production-dry-run",
        help="Build the exact query-only Moscow FBS confirmed-zero manifest.",
    )
    zero_physical_dry_run.add_argument("--deployed-sha", required=True)
    zero_physical_dry_run.add_argument("--output", required=True)
    zero_physical_dry_run.set_defaults(
        handler=run_ff_pool_zero_physical_production_command,
        ff_pool_zero_physical_action="dry-run",
    )

    zero_physical_apply = subparsers.add_parser(
        "ff-pool-zero-physical-production-apply",
        help="Apply one exact owner-gated Moscow FBS confirmed-zero manifest.",
    )
    zero_physical_apply.add_argument("--deployed-sha", required=True)
    zero_physical_apply.add_argument("--plan-file", required=True)
    zero_physical_apply.add_argument("--fingerprint", required=True)
    zero_physical_apply.add_argument("--approval-reference", required=True)
    zero_physical_apply.add_argument("--actor", required=True)
    zero_physical_apply.add_argument("--output", required=True)
    zero_physical_apply.set_defaults(
        handler=run_ff_pool_zero_physical_production_command,
        ff_pool_zero_physical_action="apply",
    )

    zero_physical_readback = subparsers.add_parser(
        "ff-pool-zero-physical-production-readback",
        help="Read exact Moscow FBS confirmed-zero reconciliation evidence.",
    )
    zero_physical_readback.add_argument("--deployed-sha", required=True)
    zero_physical_readback.add_argument("--output", required=True)
    zero_physical_readback.set_defaults(
        handler=run_ff_pool_zero_physical_production_command,
        ff_pool_zero_physical_action="readback",
    )

    ff_pool_cutover_dry_run = subparsers.add_parser(
        "ff-pool-cutover-production-dry-run",
        help=(
            "Build the query-only exact Stage 7C owner-gate manifest with "
            "frozen local T and compound observation W."
        ),
    )
    ff_pool_cutover_dry_run.add_argument("--deployed-sha", required=True)
    ff_pool_cutover_dry_run.add_argument(
        "--excluded-shipment-id", action="append", required=True
    )
    ff_pool_cutover_dry_run.add_argument("--opening-facility-id", default="")
    ff_pool_cutover_dry_run.add_argument(
        "--proposed-window-minutes", type=int, default=15
    )
    ff_pool_cutover_dry_run.add_argument("--output", required=True)
    ff_pool_cutover_dry_run.set_defaults(
        handler=run_ff_pool_cutover_production_command,
        ff_pool_cutover_action="dry-run",
    )

    ff_pool_cutover_apply = subparsers.add_parser(
        "ff-pool-cutover-production-apply",
        help=(
            "Acquire canonical barriers and apply one exact owner-approved "
            "Stage 7C manifest."
        ),
    )
    ff_pool_cutover_apply.add_argument("--deployed-sha", required=True)
    ff_pool_cutover_apply.add_argument("--plan-file", required=True)
    ff_pool_cutover_apply.add_argument("--fingerprint", required=True)
    ff_pool_cutover_apply.add_argument("--approval-reference", required=True)
    ff_pool_cutover_apply.add_argument("--actor", required=True)
    ff_pool_cutover_apply.add_argument("--output", required=True)
    ff_pool_cutover_apply.set_defaults(
        handler=run_ff_pool_cutover_production_command,
        ff_pool_cutover_action="apply",
    )

    ff_pool_cutover_readback = subparsers.add_parser(
        "ff-pool-cutover-production-readback",
        help="Run query-only exact Stage 7C opening/lifecycle reconciliation.",
    )
    ff_pool_cutover_readback.add_argument("--deployed-sha", required=True)
    ff_pool_cutover_readback.add_argument("--output", required=True)
    ff_pool_cutover_readback.set_defaults(
        handler=run_ff_pool_cutover_production_command,
        ff_pool_cutover_action="readback",
    )

    ff_pool_recovery_supersession_dry_run = subparsers.add_parser(
        "ff-pool-recovery-supersession-dry-run",
        help=(
            "Build query-only exact proof that a later reconciled Stage 7C "
            "cutover supersedes one stale failed recovery."
        ),
    )
    ff_pool_recovery_supersession_dry_run.add_argument(
        "--deployed-sha", required=True
    )
    ff_pool_recovery_supersession_dry_run.add_argument(
        "--operation-id", required=True
    )
    ff_pool_recovery_supersession_dry_run.add_argument("--output", required=True)
    ff_pool_recovery_supersession_dry_run.set_defaults(
        handler=run_ff_pool_recovery_supersession_command,
        ff_pool_recovery_supersession_action="dry-run",
    )

    ff_pool_recovery_supersession_apply = subparsers.add_parser(
        "ff-pool-recovery-supersession-apply",
        help=(
            "Append one exact owner-approved recovery supersession relation "
            "without replaying warehouse business effects."
        ),
    )
    ff_pool_recovery_supersession_apply.add_argument(
        "--deployed-sha", required=True
    )
    ff_pool_recovery_supersession_apply.add_argument("--plan-file", required=True)
    ff_pool_recovery_supersession_apply.add_argument(
        "--fingerprint", required=True
    )
    ff_pool_recovery_supersession_apply.add_argument(
        "--approval-reference", required=True
    )
    ff_pool_recovery_supersession_apply.add_argument("--actor", required=True)
    ff_pool_recovery_supersession_apply.add_argument("--output", required=True)
    ff_pool_recovery_supersession_apply.set_defaults(
        handler=run_ff_pool_recovery_supersession_command,
        ff_pool_recovery_supersession_action="apply",
    )

    ff_pool_recovery_supersession_readback = subparsers.add_parser(
        "ff-pool-recovery-supersession-readback",
        help="Read back the immutable relation and preserved recovery artifacts.",
    )
    ff_pool_recovery_supersession_readback.add_argument(
        "--deployed-sha", required=True
    )
    ff_pool_recovery_supersession_readback.add_argument(
        "--operation-id", required=True
    )
    ff_pool_recovery_supersession_readback.add_argument("--output", required=True)
    ff_pool_recovery_supersession_readback.set_defaults(
        handler=run_ff_pool_recovery_supersession_command,
        ff_pool_recovery_supersession_action="readback",
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

    for command_name, action, help_text in (
        (
            "ff-inventory-reconciliation-dry-run",
            "dry-run",
            "Build the fresh query-only FF inventory/return manifest from the exact manager workbook.",
        ),
        (
            "ff-inventory-reconciliation-apply",
            "apply",
            "Apply the exact approved FF inventory manifest through append-only documents.",
        ),
        (
            "ff-inventory-reconciliation-readback",
            "readback",
            "Read back the exact FF inventory reconciliation and target balances.",
        ),
        (
            "ff-inventory-reconciliation-rollback",
            "rollback",
            "Append exact compensating FF inventory documents without deleting audit history.",
        ),
    ):
        command = subparsers.add_parser(command_name, help=help_text)
        command.add_argument("--source-file", required=True)
        command.add_argument("--source-filename", default="")
        command.add_argument("--business-date", required=True)
        command.add_argument("--return-supply-id", action="append", default=[])
        command.add_argument("--fingerprint", default="")
        command.add_argument("--approval-reference", default="")
        command.add_argument("--created-by", default="operator")
        command.add_argument("--rollback-reason", default="")
        command.add_argument("--output", default="")
        command.set_defaults(
            handler=run_ff_inventory_reconciliation_command,
            ff_inventory_action=action,
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

    july_recovery_dry_run = subparsers.add_parser(
        "warehouse-july-recovery-dry-run",
        help="Build one exact July warehouse recovery submanifest.",
    )
    july_recovery_dry_run.add_argument(
        "--batch", choices=("a", "b", "transit", "projection"), required=True
    )
    july_recovery_dry_run.add_argument("--backup-path", default="")
    july_recovery_dry_run.add_argument("--source-sha256", default="")
    july_recovery_dry_run.add_argument("--business-date", default="")
    july_recovery_dry_run.add_argument("--output", default="")
    july_recovery_dry_run.set_defaults(
        handler=run_warehouse_july_recovery_command,
        warehouse_july_action="dry-run",
    )

    july_recovery_apply = subparsers.add_parser(
        "warehouse-july-recovery-apply",
        help="Apply one exact human-gated July warehouse recovery batch.",
    )
    july_recovery_apply.add_argument(
        "--batch", choices=("a", "b", "projection"), required=True
    )
    july_recovery_apply.add_argument("--plan-file", required=True)
    july_recovery_apply.add_argument("--fingerprint", required=True)
    july_recovery_apply.add_argument("--approval-reference", required=True)
    july_recovery_apply.add_argument("--batch-a-fingerprint", default="")
    july_recovery_apply.add_argument("--source-sha256", default="")
    july_recovery_apply.add_argument("--business-date", default="")
    july_recovery_apply.set_defaults(
        handler=run_warehouse_july_recovery_command,
        warehouse_july_action="apply",
    )

    july_recovery_rollback = subparsers.add_parser(
        "warehouse-july-recovery-rollback",
        help="Restore exact before-images for one July warehouse batch.",
    )
    july_recovery_rollback.add_argument(
        "--batch", choices=("a", "b", "projection"), required=True
    )
    july_recovery_rollback.add_argument("--fingerprint", required=True)
    july_recovery_rollback.add_argument("--reason", required=True)
    july_recovery_rollback.set_defaults(
        handler=run_warehouse_july_recovery_command,
        warehouse_july_action="rollback",
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
            "Allow only the exact audited pre-hold service generation or "
            "quiet confirmed-hold boundary during this restore."
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

    maintenance_restore_resume = subparsers.add_parser(
        "business-data-maintenance-restore-resume",
        help=(
            "Explicitly append the next bounded recovery binding and resume "
            "the same failed durable restore after a reviewed recovery deploy "
            "and exact boundary readback."
        ),
    )
    maintenance_restore_resume.add_argument("--deployed-sha", required=True)
    maintenance_restore_resume.add_argument("--job-id", required=True)
    maintenance_restore_resume.add_argument(
        "--expected-failure-digest",
        required=True,
    )
    maintenance_restore_resume.add_argument(
        "--service-continuity-fingerprint",
        required=True,
    )
    maintenance_restore_resume.add_argument("--actor", required=True)
    maintenance_restore_resume.add_argument("--reason", required=True)
    maintenance_restore_resume.set_defaults(
        handler=run_business_data_maintenance_restore_job_command,
        maintenance_restore_job_action="resume",
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
            "ff_inventory_capital_20260803",
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
            "data-table-snapshot-summary",
            "data-table-summary-updated",
            "Загрузка данных",
            "Действия и состояния",
            "С инцидентами",
            "Снимок:",
            "обн:",
            ">Столбцы</summary>",
            "data-column-visibility-controls",
            ">Метрики</button>",
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
            "data-table-summary-freshness",
            "свеж:",
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
            "Заказ на фулфилмент (FBS)",
            "Остатки WB не учитываются",
            "Последние N дней",
            "Произвольный период",
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
            DEFAULT_FBS_FULFILLMENT_ORDER_STATUS_PATH,
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
            "data-table-snapshot-summary",
            "data-table-summary-updated",
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
            "Повторить сбор",
            "С инцидентами",
            "Снимок:",
            "обн:",
            ">Столбцы</summary>",
            "data-column-visibility-controls",
            ">Метрики</button>",
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
                "data-table-summary-freshness",
                "свеж:",
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

    if route == "fbs_fulfillment_order_recommendation" and status == 200:
        evaluation["ok"] = "spreadsheetml.sheet" in content_type
        evaluation["detail"] = (
            "FBS fulfillment-order recommendation route returned XLSX"
            if evaluation["ok"]
            else "expected XLSX content-type for successful FBS recommendation route"
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

    if route == "fbs_fulfillment_order_status":
        facilities = payload.get("facilities")
        evaluation["ok"] = (
            status == 200
            and isinstance(payload.get("status"), str)
            and isinstance(payload.get("active_sku_count"), int)
            and payload.get("national_demand_scope") == "russia_total_orderCount"
            and payload.get("wb_stock_used") is False
            and isinstance(facilities, list)
            and all(
                isinstance(item, dict)
                and bool(str(item.get("facility_id") or ""))
                and bool(str(item.get("name") or ""))
                and isinstance(item.get("calculation_enabled"), bool)
                and isinstance(item.get("blockers"), list)
                for item in facilities
            )
            and isinstance(payload.get("sales_history_coverage"), dict)
            and isinstance(payload.get("defaults"), dict)
        )
        evaluation["detail"] = (
            "FBS fulfillment-order status route ok"
            if evaluation["ok"]
            else "expected 200 JSON independent FBS planner status contract"
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

    if route == "fbs_fulfillment_order_recommendation":
        error_text = str(payload.get("error", "") or "")
        evaluation["ok"] = status == 422 and bool(error_text)
        evaluation["detail"] = (
            "FBS fulfillment-order recommendation route published with truthful 422 before calculation"
            if evaluation["ok"]
            else "expected 200 XLSX or 422 JSON error for FBS recommendation route"
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
    _collect("fbs_fulfillment_order_status", "GET", PAYLOAD["base_url"] + {DEFAULT_FBS_FULFILLMENT_ORDER_STATUS_PATH!r}),
    _collect("factory_order_template_stock_ff", "GET", PAYLOAD["base_url"] + "/v1/sheet-vitrina-v1/supply/factory-order/template/stock-ff.xlsx"),
    _collect("factory_order_template_inbound_factory", "GET", PAYLOAD["base_url"] + "/v1/sheet-vitrina-v1/supply/factory-order/template/inbound-factory.xlsx"),
    _collect("factory_order_template_inbound_ff_to_wb", "GET", PAYLOAD["base_url"] + "/v1/sheet-vitrina-v1/supply/factory-order/template/inbound-ff-to-wb.xlsx"),
    _collect("factory_order_recommendation", "GET", PAYLOAD["base_url"] + "/v1/sheet-vitrina-v1/supply/factory-order/recommendation.xlsx"),
    _collect("fbs_fulfillment_order_recommendation", "GET", PAYLOAD["base_url"] + {DEFAULT_FBS_FULFILLMENT_ORDER_RECOMMENDATION_PATH!r}),
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
    managed_names = {unit.name for unit in target.managed_systemd_units}
    retired_names = set(target.retired_systemd_units)
    if managed_names & retired_names:
        raise ValueError(
            "systemd units cannot be both managed and retired: "
            + ",".join(sorted(managed_names & retired_names))
        )
    unit_name_pattern = re.compile(r"^[A-Za-z0-9_.@-]+\.(?:service|timer)$")
    for unit_name in sorted(managed_names | retired_names):
        if not unit_name_pattern.fullmatch(unit_name):
            raise ValueError(f"invalid systemd unit name: {unit_name!r}")
    if target.has_managed_systemd_units:
        source_dir = _resolve_repo_relative_dir(target.systemd_units_source_dir)
        if not source_dir.exists():
            raise FileNotFoundError(f"managed systemd unit source dir not found: {source_dir}")
        for unit in target.managed_systemd_units:
            unit_path = source_dir / unit.name
            if not unit_path.exists():
                raise FileNotFoundError(f"managed systemd unit file not found: {unit_path}")


def _build_managed_systemd_commands(target: HostedRuntimeTarget) -> dict[str, list[str] | None]:
    if not target.has_managed_systemd_units and not target.retired_systemd_units:
        return {
            "install": None,
            "retire": None,
            "daemon_reload": None,
            "enable": None,
            "restart": None,
        }

    install_steps: list[str] = []
    if target.has_managed_systemd_units:
        install_steps.append(f"install -d {shlex.quote(target.systemd_unit_directory)}")
        for unit in target.managed_systemd_units:
            install_steps.append(
                "install -m 0644 "
                f"{shlex.quote(_remote_systemd_unit_source_path(target, unit.name))} "
                f"{shlex.quote(_remote_systemd_unit_destination_path(target, unit.name))}"
            )

    retire_steps: list[str] = ["set -eu"]
    for unit_name in target.retired_systemd_units:
        quoted_name = shlex.quote(unit_name)
        quoted_path = shlex.quote(_remote_systemd_unit_destination_path(target, unit_name))
        retire_steps.append(
            f"if systemctl cat {quoted_name} >/dev/null 2>&1; then "
            f"systemctl disable --now {quoted_name}; fi; rm -f {quoted_path}"
        )

    enable_names = [shlex.quote(unit.name) for unit in target.managed_systemd_units if unit.enable]
    restart_names = [shlex.quote(unit.name) for unit in target.managed_systemd_units if unit.restart]
    return {
        "install": _remote_shell_command(target, " && ".join(install_steps)) if install_steps else None,
        "retire": _remote_shell_command(target, "; ".join(retire_steps)) if target.retired_systemd_units else None,
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
    if target.has_managed_systemd_units or target.retired_systemd_units:
        required["systemd_unit_directory"] = target.systemd_unit_directory
    if target.has_managed_systemd_units:
        required["systemd_units_source_dir"] = target.systemd_units_source_dir
    for key, value in required.items():
        if _is_placeholder(value):
            missing.append(key)
    if target.has_managed_systemd_units:
        for unit in target.managed_systemd_units:
            if _is_placeholder(unit.name):
                missing.append("managed_systemd_units[].name")
    for unit_name in target.retired_systemd_units:
        if _is_placeholder(unit_name):
            missing.append("retired_systemd_units[]")
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
