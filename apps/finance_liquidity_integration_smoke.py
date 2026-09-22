#!/usr/bin/env python3
"""Verify Finance navigation and dormant release artifacts stay isolated."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
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
    _authenticated_web_user,
    _build_session_cookie,
    _finance_navigation_is_available,
    _render_sheet_vitrina_web_vitrina_ui,
    _web_auth_config,
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

        access_config = temporary / "pilot-access.json"
        access_payload = {
            "contract_version": "finance_liquidity_bootstrap_access_v1",
            "enabled": True,
            "username": "owner",
            "capability": "finance_admin",
            "instance_label": "TEST DATABASE",
            "store_id": "fixture-pilot",
            "store_path": str(temporary / "pilot" / "pilot.sqlite3"),
            "mode": "isolated_test",
        }
        access_config.write_text(json.dumps(access_payload), encoding="utf-8")
        wrong_store = temporary / "wrong" / "wrong.sqlite3"
        with patch.object(
            sys,
            "argv",
            ["finance_liquidity_http.py", "--db", str(wrong_store), "--bootstrap"],
        ), patch.dict(
            os.environ,
            {"FINANCE_LIQUIDITY_ACCESS_CONFIG": str(access_config)},
            clear=False,
        ):
            try:
                launcher.main()
            except SystemExit as error:
                assert str(error) == "Finance database does not match access config store binding"
            else:
                raise AssertionError("pilot access config allowed a different bootstrap store")
        assert not wrong_store.exists()

        real_parent = temporary / "real-pilot"
        real_parent.mkdir()
        alias_parent = temporary / "alias-pilot"
        alias_parent.symlink_to(real_parent, target_is_directory=True)
        alias_store = alias_parent / "pilot.sqlite3"
        access_payload["store_path"] = str(alias_store)
        access_config.write_text(json.dumps(access_payload), encoding="utf-8")
        with patch.object(
            sys,
            "argv",
            ["finance_liquidity_http.py", "--db", str(alias_store), "--bootstrap"],
        ), patch.dict(
            os.environ,
            {"FINANCE_LIQUIDITY_ACCESS_CONFIG": str(access_config)},
            clear=False,
        ):
            try:
                launcher.main()
            except SystemExit as error:
                assert str(error) == "Finance access-configured database must not use an alias"
            else:
                raise AssertionError("pilot bootstrap accepted an aliased store path")
        assert not (real_parent / "pilot.sqlite3").exists()

        canonical_store = temporary.resolve() / "canonical-pilot" / "pilot.sqlite3"
        access_payload["store_path"] = str(canonical_store)
        access_config.write_text(json.dumps(access_payload), encoding="utf-8")
        with patch.object(
            sys,
            "argv",
            ["finance_liquidity_http.py", "--db", str(canonical_store), "--bootstrap"],
        ), patch.dict(
            os.environ,
            {"FINANCE_LIQUIDITY_ACCESS_CONFIG": str(access_config)},
            clear=False,
        ):
            launcher.main()
        assert canonical_store.is_file()
        with sqlite3.connect(canonical_store) as connection:
            assert connection.execute(
                "SELECT schema_version FROM finance_liquidity_schema_meta WHERE singleton=1"
            ).fetchone()[0] == 2
            assert connection.execute(
                "SELECT COUNT(*) FROM finance_liquidity_accounts"
            ).fetchone()[0] == 0

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

    with TemporaryDirectory(prefix="finance-bootstrap-main-") as directory:
        access_path = Path(directory) / "access.json"
        payload = {
            "contract_version": "finance_liquidity_bootstrap_access_v1",
            "enabled": True,
            "username": "owner",
            "capability": "finance_admin",
            "instance_label": "TEST DATABASE",
            "store_id": "fixture-pilot",
            "store_path": str(Path(directory) / "pilot.sqlite3"),
            "mode": "isolated_test",
        }
        access_path.write_text(json.dumps(payload), encoding="utf-8")
        environment = {
            "WB_CORE_WEB_AUTH_USERNAME": "owner",
            "WB_CORE_WEB_AUTH_PASSWORD_HASH": "fixture-not-used",
            "WB_CORE_WEB_AUTH_SESSION_SECRET": "fixture-session-secret",
            "FINANCE_LIQUIDITY_ACCESS_CONFIG": str(access_path),
            "FINANCE_LIQUIDITY_ENABLED": "1",
            "FINANCE_LIQUIDITY_READ_ENABLED": "1",
        }
        with patch.dict(os.environ, environment, clear=False):
            config = _web_auth_config()
            granted = list(config["operator"]["allowed_sections"])
            assert granted[-3:] == ["finance", "finance_operate", "finance_admin"]
            class OwnerHandler:
                headers: dict[str, str] = {"Host": "api.selleros.pro"}

            owner_handler = OwnerHandler()
            owner_handler.headers["Cookie"] = _build_session_cookie(
                owner_handler,  # type: ignore[arg-type]
                "owner",
                config,
                role=WEB_AUTH_ROLE_ADMIN,
                display_name="Owner",
            ).split(";", 1)[0]
            signed_owner = _authenticated_web_user(owner_handler, config)  # type: ignore[arg-type]
            assert signed_owner is not None
            assert list(signed_owner["allowed_sections"])[-3:] == [
                "finance",
                "finance_operate",
                "finance_admin",
            ]
            assert 'href="/finance/">Финансы</a>' in _render(
                role=WEB_AUTH_ROLE_ADMIN,
                grants=granted,
                enabled=True,
                read_enabled=True,
            )
            payload["enabled"] = False
            access_path.write_text(json.dumps(payload), encoding="utf-8")
            revoked_config = _web_auth_config()
            revoked = list(revoked_config["operator"]["allowed_sections"])
            assert not {"finance", "finance_operate", "finance_admin"}.intersection(revoked)
            revoked_owner = _authenticated_web_user(owner_handler, revoked_config)  # type: ignore[arg-type]
            assert revoked_owner is not None
            assert not {"finance", "finance_operate", "finance_admin"}.intersection(
                revoked_owner["allowed_sections"]
            )
            assert 'href="/finance/">Финансы</a>' not in _render(
                role=WEB_AUTH_ROLE_ADMIN,
                grants=revoked,
                enabled=True,
                read_enabled=True,
            )
            access_path.write_text("{broken", encoding="utf-8")
            malformed = list(_web_auth_config()["operator"]["allowed_sections"])
            assert not {"finance", "finance_operate", "finance_admin"}.intersection(malformed)


def _pilot_artifact_checks() -> None:
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

    pilot_dir = ROOT / "artifacts" / "finance_liquidity_cash" / "pilot"
    access = json.loads((pilot_dir / "finance-liquidity-pilot-access.json").read_text(encoding="utf-8"))
    pilot_store = "/opt/wb-core-runtime/state/finance-liquidity-pilot/finance-liquidity-pilot.sqlite3"
    assert access == {
        "contract_version": "finance_liquidity_bootstrap_access_v1",
        "enabled": True,
        "username": "owner",
        "capability": "finance_admin",
        "instance_label": "ТЕСТОВАЯ БАЗА · ИЗОЛИРОВАННЫЕ ДАННЫЕ",
        "store_id": "finance-liquidity-pilot",
        "store_path": pilot_store,
        "mode": "isolated_test",
    }
    assert access["store_path"] != "/opt/wb-core-runtime/state/finance-liquidity/finance-liquidity.sqlite3"
    flags = (pilot_dir / "finance-liquidity-pilot.env").read_text(encoding="utf-8")
    assert "FINANCE_LIQUIDITY_ENABLED=1" in flags
    assert "FINANCE_LIQUIDITY_READ_ENABLED=1" in flags
    assert "FINANCE_LIQUIDITY_WRITE_ENABLED=1" in flags
    assert "FINANCE_LIQUIDITY_ACCESS_CONFIG=/opt/wb-core-runtime/app/artifacts/finance_liquidity_cash/pilot/finance-liquidity-pilot-access.json" in flags
    assert "PASSWORD" not in flags and "SECRET" not in flags

    pilot_unit = (ROOT / "artifacts" / "registry_upload_http_entrypoint" / "systemd" / "wb-core-finance-liquidity-pilot.service").read_text(encoding="utf-8")
    assert f"ConditionPathExists={pilot_store}" in pilot_unit
    assert f"--db {pilot_store}" in pilot_unit
    assert "--bootstrap" not in pilot_unit
    assert "--host 127.0.0.1 --port 8767" in pilot_unit
    assert pilot_unit.index("EnvironmentFile=/opt/wb-ai/.env") < pilot_unit.index("EnvironmentFile=/opt/wb-core-runtime/app/artifacts/finance_liquidity_cash/pilot/finance-liquidity-pilot.env")
    main_unit = (ROOT / "artifacts" / "registry_upload_http_entrypoint" / "systemd" / "wb-core-registry-http.service").read_text(encoding="utf-8")
    assert main_unit.index("EnvironmentFile=/opt/wb-ai/.env") < main_unit.index("EnvironmentFile=/opt/wb-core-runtime/app/artifacts/finance_liquidity_cash/pilot/finance-liquidity-pilot.env")

    target = json.loads(
        (ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "hosted_runtime_target__europe_api.json").read_text(encoding="utf-8")
    )
    managed_units = {item["name"] for item in target.get("managed_systemd_units", [])}
    assert "wb-core-finance-liquidity.service" not in managed_units
    assert "wb-core-finance-liquidity-pilot.service" in managed_units
    routes_manifest = json.loads(
        (ROOT / "artifacts" / "registry_upload_http_entrypoint" / "nginx" / "public_route_allowlist.json").read_text(encoding="utf-8")
    )
    finance_routes = {
        item["path"]: item
        for item in routes_manifest.get("routes", [])
        if item["path"] in {"/finance/", "/v1/finance/"}
    }
    assert finance_routes["/finance/"]["methods"] == ["GET"]
    assert finance_routes["/v1/finance/"]["methods"] == ["GET", "POST", "PATCH"]
    assert {item["proxy_pass_url"] for item in finance_routes.values()} == {"http://127.0.0.1:8767"}


def main() -> None:
    _dormant_lifecycle_checks()
    _navigation_checks()
    _pilot_artifact_checks()
    print("finance_liquidity_integration_smoke: owner grant and isolated TEST pilot config OK")


if __name__ == "__main__":
    main()
