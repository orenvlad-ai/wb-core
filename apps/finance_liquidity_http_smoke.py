#!/usr/bin/env python3
"""Actual loopback HTTP fixture flow for the isolated cash sidecar."""

from __future__ import annotations

from pathlib import Path
import json
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.finance_liquidity_auth import FixtureFinanceAuth
from packages.adapters.finance_liquidity_http import (
    FinanceHttpApp,
    build_finance_http_server,
)
from packages.application.finance_liquidity_cash import (
    FinanceCashService,
    bootstrap_finance_cash_store,
)
from packages.application.business_data_write_barrier import STATE_FILENAME, SCHEMA_VERSION


def request(
    base: str,
    path: str,
    *,
    payload: dict[str, object] | None = None,
    csrf: str = "",
) -> tuple[int, dict[str, object]]:
    headers = {"Cookie": "finance_fixture_session=admin"}
    body = None
    method = "GET"
    if payload is not None:
        method = "POST"
        body = json.dumps(payload).encode()
        headers.update(
            {
                "Content-Type": "application/json",
                "Origin": base,
                "X-Finance-CSRF": csrf,
                "Idempotency-Key": f"fixture-{uuid4().hex}",
                "X-Operation-Id": f"fixture-{uuid4().hex}",
            }
        )
    try:
        with urlopen(
            Request(base + path, data=body, headers=headers, method=method)
        ) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


def main() -> None:
    with TemporaryDirectory() as directory:
        temporary = Path(directory)
        db_path = temporary / "finance.sqlite3"
        fixture_path = temporary / "auth.json"
        bootstrap_finance_cash_store(db_path)
        fixture_path.write_text(
            json.dumps(
                {
                    "actors": {
                        "admin": {
                            "username": "fixture-admin",
                            "role": "operator",
                            "capabilities": ["finance_admin"],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        app = FinanceHttpApp(
            FinanceCashService(db_path),
            FixtureFinanceAuth(fixture_path),
            read_enabled=True,
            write_enabled=True,
            csrf_secret="fixture-csrf",
            static_dir=ROOT / "packages/adapters/finance_liquidity_static",
            allowed_origin="",
            business_runtime_dir=temporary,
        )
        server = build_finance_http_server(
            "127.0.0.1",
            0,
            app,
        )
        base = f"http://127.0.0.1:{server.server_port}"
        app.allowed_origin = base
        server_thread = Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            status, capabilities = request(base, "/v1/finance/capabilities")
            assert status == 200 and capabilities["contract"] == "finance_cash_v1"
            csrf = str(capabilities["data"]["csrf_token"])  # type: ignore[index]
            status, account = request(
                base,
                "/v1/finance/accounts",
                payload={
                    "name": "HTTP fixture cash",
                    "account_type": "cash",
                    "currency": "RUB",
                    "responsible_name": "Fixture cashier",
                },
                csrf=csrf,
            )
            assert status == 201
            account_id = str(account["data"]["account_id"])  # type: ignore[index]
            status, draft = request(
                base,
                "/v1/finance/documents",
                payload={
                    "document_type": "opening",
                    "target_account_id": account_id,
                    "amount": "12.34",
                    "occurred_at": "2026-09-21T10:00:00Z",
                    "opening_evidence_type": "manual_confirmation",
                    "opening_evidence_ref": "HTTP fixture",
                },
                csrf=csrf,
            )
            document = draft["data"]  # type: ignore[index]
            status, posted = request(
                base,
                f"/v1/finance/documents/{document['document_id']}/post",  # type: ignore[index]
                payload={"base_revision": document["revision"]},  # type: ignore[index]
                csrf=csrf,
            )
            assert status == 200 and posted["data"]["financial_effect"] is True  # type: ignore[index]
            operation_id = posted["data"]["operation_id"]  # type: ignore[index]
            status, operation = request(base, f"/v1/finance/operations/{operation_id}")
            assert status == 200 and operation["data"]["operation_id"] == operation_id  # type: ignore[index]
            status, documents = request(base, "/v1/finance/documents")
            item = documents["data"]["documents"][0]  # type: ignore[index]
            assert (
                status == 200
                and item["amount"] == "12.34"
                and item["amount_minor"] == 1234
            ), item
            # The common maintenance barrier is independent of the Finance
            # feature flag: GET remains available while every mutation stops.
            barrier = temporary / STATE_FILENAME
            barrier.write_text(
                json.dumps({"schema_version": SCHEMA_VERSION, "phase": "held"}),
                encoding="utf-8",
            )
            barrier.chmod(0o600)
            status, blocked = request(
                base,
                "/v1/finance/categories",
                payload={"name": "blocked", "direction": "income"},
                csrf=csrf,
            )
            assert status == 423 and blocked["error"]["code"] == "business_data_maintenance", blocked
            status, readable = request(base, "/v1/finance/accounts")
            assert status == 200 and readable["data"]["accounts"], readable
        finally:
            server.shutdown()
            server.server_close()
    print("finance_liquidity_http_smoke: ok")


if __name__ == "__main__":
    main()
