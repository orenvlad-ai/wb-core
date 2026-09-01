#!/usr/bin/env python3
"""Stdlib-only canonical target resolver for the WBC0027 incident capsule."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGET_FILE = (
    ROOT
    / "artifacts"
    / "registry_upload_http_entrypoint"
    / "input"
    / "hosted_runtime_target__europe_api.json"
)

ACTIVE_TARGET_STATUS = "active"
CANONICAL_TARGET_ID = "wb_core_eu_hosted_runtime_active"
CANONICAL_TARGET_ROLE = "primary_live"
CANONICAL_TARGET_LIFECYCLE = "current_live"
CANONICAL_SSH_DESTINATION = "wb-core-eu-root"
CANONICAL_SSH_USER = "root"
CANONICAL_PUBLIC_HOSTS = frozenset({"89.191.226.88", "api.selleros.pro"})
CANONICAL_PUBLIC_BASE_URL = "https://api.selleros.pro"
CANONICAL_ENVIRONMENT_FILE = "/opt/wb-ai/.env"
CANONICAL_TARGET_DIR = "/opt/wb-core-runtime/app"
CANONICAL_RUNTIME_DIR = "/opt/wb-core-runtime/state"
CANONICAL_SERVICE_NAME = "wb-core-registry-http.service"
CANONICAL_SERVER_NAMES = ("89.191.226.88", "api.selleros.pro")
CANONICAL_TLS_LISTEN = "443 ssl"
CANONICAL_TLS_CERTIFICATE = "/etc/letsencrypt/live/api.selleros.pro/fullchain.pem"
CANONICAL_TLS_KEY = "/etc/letsencrypt/live/api.selleros.pro/privkey.pem"


@dataclass(frozen=True)
class CapsuleCanonicalTarget:
    target_id: str
    ssh_destination: str
    host_name: str


def load_canonical_target(path: Path) -> CapsuleCanonicalTarget:
    """Load and fail-close the exact active hosted target without app imports."""

    target_path = Path(path).expanduser().resolve()
    if not target_path.is_file():
        raise ValueError(f"capsule canonical target file is missing: {target_path}")
    payload = json.loads(target_path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("capsule canonical target file must contain a JSON object")

    exact_fields = {
        "target_status": ACTIVE_TARGET_STATUS,
        "target_id": CANONICAL_TARGET_ID,
        "target_role": CANONICAL_TARGET_ROLE,
        "target_lifecycle": CANONICAL_TARGET_LIFECYCLE,
        "ssh_destination": CANONICAL_SSH_DESTINATION,
        "environment_file": CANONICAL_ENVIRONMENT_FILE,
        "target_dir": CANONICAL_TARGET_DIR,
        "service_name": CANONICAL_SERVICE_NAME,
    }
    for field, expected in exact_fields.items():
        actual = _text(payload.get(field))
        if actual != expected:
            raise ValueError(f"{field} must be {expected}, got {actual or '<missing>'}")

    public_base_url = _text(payload.get("public_base_url")).rstrip("/")
    if public_base_url != CANONICAL_PUBLIC_BASE_URL:
        raise ValueError(
            f"public_base_url must be {CANONICAL_PUBLIC_BASE_URL}, "
            f"got {public_base_url or '<missing>'}"
        )
    host_name = _text(payload.get("host_ip"))
    if not host_name:
        raise ValueError("host_ip is missing")
    if host_name not in CANONICAL_PUBLIC_HOSTS:
        raise ValueError(f"host_ip is foreign: {host_name}")

    runtime_env = _mapping(payload.get("runtime_env"), field="runtime_env")
    _require_nested_text(
        runtime_env,
        "REGISTRY_UPLOAD_RUNTIME_DIR",
        CANONICAL_RUNTIME_DIR,
        field="runtime_env.REGISTRY_UPLOAD_RUNTIME_DIR",
    )
    _require_nested_text(
        runtime_env,
        "WB_AUTOANSWERS_FORCE_OFF",
        "false",
        field="runtime_env.WB_AUTOANSWERS_FORCE_OFF",
    )

    routes = _mapping(payload.get("nginx_public_routes"), field="nginx_public_routes")
    raw_server_names = routes.get("server_names")
    if not isinstance(raw_server_names, list):
        raise ValueError("nginx_public_routes.server_names must be a JSON array")
    server_names = tuple(_text(item) for item in raw_server_names)
    if server_names != CANONICAL_SERVER_NAMES:
        raise ValueError(
            "nginx_public_routes.server_names must be "
            f"{list(CANONICAL_SERVER_NAMES)!r}, got {list(server_names)!r}"
        )

    tls = _mapping(routes.get("tls"), field="nginx_public_routes.tls")
    raw_listen = tls.get("listen")
    if not isinstance(raw_listen, list):
        raise ValueError("nginx_public_routes.tls.listen must be a JSON array")
    listen = tuple(_text(item) for item in raw_listen)
    if listen != (CANONICAL_TLS_LISTEN,):
        raise ValueError(
            "nginx_public_routes.tls.listen must be "
            f"{[CANONICAL_TLS_LISTEN]!r}, got {list(listen)!r}"
        )
    _require_nested_text(
        tls,
        "certificate_path",
        CANONICAL_TLS_CERTIFICATE,
        field="nginx_public_routes.tls.certificate_path",
    )
    _require_nested_text(
        tls,
        "certificate_key_path",
        CANONICAL_TLS_KEY,
        field="nginx_public_routes.tls.certificate_key_path",
    )
    return CapsuleCanonicalTarget(
        target_id=CANONICAL_TARGET_ID,
        ssh_destination=CANONICAL_SSH_DESTINATION,
        host_name=host_name,
    )


def _mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a JSON object")
    return value


def _require_nested_text(
    payload: Mapping[str, Any],
    key: str,
    expected: str,
    *,
    field: str,
) -> None:
    actual = _text(payload.get(key))
    if actual != expected:
        raise ValueError(f"{field} must be {expected}, got {actual or '<missing>'}")


def _text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()
