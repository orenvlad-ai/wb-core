#!/usr/bin/env python3
"""Verify Finance navigation and dormant release artifacts stay isolated."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    WEB_AUTH_ROLE_ADMIN,
    WEB_AUTH_ROLE_SUPPLIER,
    _finance_navigation_is_available,
    _render_sheet_vitrina_web_vitrina_ui,
)


def _dormant_lifecycle_checks() -> None:
    # Importing the active main runtime must not import the optional sidecar's
    # auth, HTTP adapter, cash service, or launcher.
    for module_name in (
        "packages.adapters.finance_liquidity_auth",
        "packages.adapters.finance_liquidity_http",
        "packages.application.finance_liquidity_cash",
        "apps.finance_liquidity_http",
    ):
        assert module_name not in sys.modules, module_name

    # Import and construction of the isolated sidecar objects are also inert:
    # an absent store and absent auth runtime stay absent.
    from apps import finance_liquidity_http as launcher
    from packages.adapters.finance_liquidity_auth import FinanceOperationalAuth
    from packages.adapters.finance_liquidity_http import (
        FinanceHttpApp,
        build_finance_http_server,
    )
    from packages.application.finance_liquidity_cash import FinanceCashService

    with TemporaryDirectory(prefix="finance-liquidity-dormant-") as directory:
        temporary = Path(directory)
        absent_store = temporary / "finance-state" / "finance.sqlite3"
        absent_auth_runtime = temporary / "absent-auth-runtime"
        FinanceCashService(absent_store)
        FinanceOperationalAuth(absent_auth_runtime, session_secret="fixture-only")
        assert not absent_store.exists()
        assert not absent_store.parent.exists()
        assert not absent_auth_runtime.exists()

        # The ordinary launcher path fails at the master feature flag before
        # constructing auth/service/server state. Explicit --bootstrap is a
        # separate manual command and is deliberately not invoked here.
        def unexpected_launcher_construction(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("disabled launcher constructed Finance runtime")

        with patch.object(
            sys,
            "argv",
            [
                "finance_liquidity_http.py",
                "--db",
                str(absent_store),
                "--runtime-dir",
                str(absent_auth_runtime),
            ],
        ), patch.dict(
            os.environ,
            {
                "FINANCE_LIQUIDITY_ENABLED": "0",
                "FINANCE_LIQUIDITY_READ_ENABLED": "0",
                "FINANCE_LIQUIDITY_WRITE_ENABLED": "0",
            },
            clear=False,
        ), patch.object(
            launcher, "FinanceOperationalAuth", unexpected_launcher_construction
        ), patch.object(
            launcher, "FinanceCashService", unexpected_launcher_construction
        ), patch.object(
            launcher, "FinanceHttpApp", unexpected_launcher_construction
        ), patch.object(
            launcher, "build_finance_http_server", unexpected_launcher_construction
        ):
            try:
                launcher.main()
            except SystemExit as error:
                assert str(error) == "FINANCE_LIQUIDITY_ENABLED=1 is required"
            else:
                raise AssertionError("disabled launcher unexpectedly started")
        assert not absent_store.exists()
        assert not absent_store.parent.exists()
        assert not absent_auth_runtime.exists()

        class NeverCalledAuth:
            calls = 0

            def authenticate(self, _headers: object) -> dict[str, object]:
                self.calls += 1
                raise AssertionError("disabled API called Finance auth")

        class NeverCalledService:
            calls = 0

            def __getattr__(self, _name: str) -> object:
                self.calls += 1
                raise AssertionError("disabled API called Finance service")

        auth, service = NeverCalledAuth(), NeverCalledService()
        app = FinanceHttpApp(
            service,  # type: ignore[arg-type]
            auth,
            read_enabled=False,
            write_enabled=False,
            csrf_secret="fixture-only",
            static_dir=ROOT / "packages/adapters/finance_liquidity_static",
            allowed_origin="http://127.0.0.1",
        )
        server = build_finance_http_server("127.0.0.1", 0, app)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            try:
                urlopen(
                    f"http://127.0.0.1:{server.server_port}/v1/finance/accounts",
                    timeout=5,
                )
            except HTTPError as error:
                payload = json.loads(error.read())
                assert error.code == 503
                assert payload["error"]["code"] == "finance_read_disabled"
            else:
                raise AssertionError("disabled Finance API unexpectedly answered")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        assert auth.calls == 0
        assert service.calls == 0
        assert not absent_store.exists()
        assert not absent_store.parent.exists()


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
    assert "--bootstrap" not in unit
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
    _dormant_lifecycle_checks()
    _navigation_checks()
    _dormant_artifact_checks()
    print("finance_liquidity_integration_smoke: explicit navigation and dormant rollout OK")


if __name__ == "__main__":
    main()
