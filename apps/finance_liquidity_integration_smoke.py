#!/usr/bin/env python3
"""Verify Finance navigation and dormant release artifacts stay isolated."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    WEB_AUTH_ROLE_ADMIN,
    WEB_AUTH_ROLE_SUPPLIER,
    _finance_navigation_is_available,
    _render_sheet_vitrina_web_vitrina_ui,
)


def _render(*, role: str, grants: list[str], enabled: bool, read_enabled: bool) -> str:
    flags = {
        "FINANCE_LIQUIDITY_ENABLED": "1" if enabled else "0",
        "FINANCE_LIQUIDITY_READ_ENABLED": "1" if read_enabled else "0",
    }
    with patch.dict(os.environ, flags, clear=False):
        return _render_sheet_vitrina_web_vitrina_ui(
            read_path="/v1/sheet-vitrina-v1/web-vitrina",
            operator_path="/sheet-vitrina-v1/operator",
            refresh_path="/v1/sheet-vitrina-v1/refresh",
            job_path="/v1/sheet-vitrina-v1/job",
            role=role,
            allowed_sections=grants,
        )


def _navigation_checks() -> None:
    assert 'href="/finance/">Финансы</a>' not in _render(
        role=WEB_AUTH_ROLE_ADMIN,
        grants=[],
        enabled=True,
        read_enabled=True,
    )
    assert 'href="/finance/">Финансы</a>' not in _render(
        role=WEB_AUTH_ROLE_ADMIN,
        grants=["finance"],
        enabled=False,
        read_enabled=True,
    )
    assert 'href="/finance/">Финансы</a>' not in _render(
        role=WEB_AUTH_ROLE_ADMIN,
        grants=["finance"],
        enabled=True,
        read_enabled=False,
    )
    assert 'href="/finance/">Финансы</a>' not in _render(
        role=WEB_AUTH_ROLE_SUPPLIER,
        grants=["finance_admin"],
        enabled=True,
        read_enabled=True,
    )
    available = _render(
        role=WEB_AUTH_ROLE_ADMIN,
        grants=["finance_operate"],
        enabled=True,
        read_enabled=True,
    )
    assert 'href="/finance/">Финансы</a>' in available
    with patch.dict(
        os.environ,
        {"FINANCE_LIQUIDITY_ENABLED": "1", "FINANCE_LIQUIDITY_READ_ENABLED": "1"},
        clear=False,
    ):
        assert _finance_navigation_is_available(
            role=WEB_AUTH_ROLE_ADMIN,
            allowed_sections=["finance_admin"],
        )


def _dormant_artifact_checks() -> None:
    candidate_dir = ROOT / "artifacts" / "finance_liquidity_cash" / "dormant"
    unit = (candidate_dir / "systemd" / "wb-core-finance-liquidity.service").read_text(
        encoding="utf-8"
    )
    assert "EnvironmentFile=/opt/wb-ai/.env" in unit
    assert "ConditionPathExists=/opt/wb-core-runtime/state/finance-liquidity/finance-liquidity.sqlite3" in unit
    assert "FINANCE_LIQUIDITY_ENABLED=0" in unit
    assert "FINANCE_LIQUIDITY_READ_ENABLED=0" in unit
    assert "FINANCE_LIQUIDITY_WRITE_ENABLED=0" in unit
    assert "--host 127.0.0.1 --port 8767" in unit
    assert "--runtime-dir /opt/wb-core-runtime/state" in unit
    routes = (candidate_dir / "nginx" / "finance-liquidity.routes.candidate.md").read_text(
        encoding="utf-8"
    )
    assert "location ^~ /finance/" in routes
    assert "location ^~ /v1/finance/" in routes
    assert routes.count("proxy_pass http://127.0.0.1:8767;") == 2

    target = json.loads(
        (ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "hosted_runtime_target__europe_api.json").read_text(encoding="utf-8")
    )
    managed_units = {item["name"] for item in target.get("managed_systemd_units", [])}
    assert "wb-core-finance-liquidity.service" not in managed_units
    routes_manifest = json.loads(
        (ROOT / "artifacts" / "registry_upload_http_entrypoint" / "nginx" / "public_route_allowlist.json").read_text(encoding="utf-8")
    )
    published_paths = {item["path"] for item in routes_manifest.get("routes", [])}
    assert "/finance/" not in published_paths
    assert "/v1/finance/" not in published_paths


def main() -> None:
    _navigation_checks()
    _dormant_artifact_checks()
    print("finance_liquidity_integration_smoke: explicit navigation and dormant rollout OK")


if __name__ == "__main__":
    main()
