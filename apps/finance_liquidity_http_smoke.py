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
    redact_vlad_balance,
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
    actor: str = "admin",
) -> tuple[int, dict[str, object]]:
    headers = {"Cookie": f"finance_fixture_session={actor}"}
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
    redacted_negative = redact_vlad_balance({
        "account_id": "cash_vladislav", "name": "Changed label", "balance": "-1.00",
        "balance_minor": -100, "balance_state": "current", "negative_balance_warning": True,
    }, permitted=False)
    assert redacted_negative["balance"] is None and redacted_negative["negative_balance_warning"] is None
    assert redacted_negative["name"] == "Changed label" and redacted_negative["balance_hidden"] is True
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
                        },
                        "viewer": {"username": "fixture-viewer", "role": "operator", "capabilities": ["finance"]},
                        "vlad_granted": {"username": "fixture-granted", "role": "operator", "capabilities": ["finance_admin", "finance_vlad_balance"]},
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
            instance_label="ТЕСТОВАЯ БАЗА · ИЗОЛИРОВАННЫЕ ДАННЫЕ",
            store_id="finance-liquidity-pilot",
            store_mode="isolated_test",
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
            assert capabilities["data"]["instance_label"] == "ТЕСТОВАЯ БАЗА · ИЗОЛИРОВАННЫЕ ДАННЫЕ"  # type: ignore[index]
            assert capabilities["data"]["store_id"] == "finance-liquidity-pilot"  # type: ignore[index]
            assert capabilities["data"]["store_mode"] == "isolated_test"  # type: ignore[index]
            with urlopen(base + "/finance/") as response:
                html = response.read().decode("utf-8")
            assert 'data-instance-label' in html
            assert "ТЕСТОВАЯ БАЗА · ИЗОЛИРОВАННЫЕ ДАННЫЕ" in html
            assert "{{FINANCE_INSTANCE_BANNER}}" not in html
            csrf = str(capabilities["data"]["csrf_token"])  # type: ignore[index]
            status, categories = request(base, "/v1/finance/categories")
            assert status == 200 and len(categories["data"]["categories"]) == 23  # type: ignore[index]
            status, groups = request(base, "/v1/finance/category-groups", actor="viewer")
            assert status == 200 and groups["data"]["enabled"] and len(groups["data"]["groups"]) == 6  # type: ignore[index]
            status, counterparties = request(base, "/v1/finance/counterparties")
            assert status == 200 and counterparties["data"]["counterparties"] == []  # type: ignore[index]
            status, audit = request(base, "/v1/finance/audit")
            assert status == 200 and len(audit["data"]["events"]) == 26  # type: ignore[index]
            status, directory_audit = request(base, "/v1/finance/audit?scope=directories")
            assert status == 200 and len(directory_audit["data"]["events"]) == 26  # type: ignore[index]
            status, viewer_caps = request(base, "/v1/finance/capabilities", actor="viewer")
            viewer_csrf = str(viewer_caps["data"]["csrf_token"])  # type: ignore[index]
            status, denied = request(base, "/v1/finance/audit", actor="viewer")
            assert status == 403 and denied["error"]["code"] == "finance_capability_denied"  # type: ignore[index]
            status, denied = request(base, "/v1/finance/counterparties", payload={"name": "Denied"}, csrf=viewer_csrf, actor="viewer")
            assert status == 403 and denied["error"]["code"] == "finance_capability_denied"  # type: ignore[index]
            status, denied = request(base, "/v1/finance/category-groups", payload={"name": "Denied"}, csrf=viewer_csrf, actor="viewer")
            assert status == 403 and denied["error"]["code"] == "finance_capability_denied"  # type: ignore[index]
            status, group = request(base, "/v1/finance/category-groups", payload={"name": "HTTP группа"}, csrf=csrf)
            assert status == 201 and group["data"]["group_id"]  # type: ignore[index]
            group_id = str(group["data"]["group_id"])  # type: ignore[index]
            status, assigned = request(base, "/v1/finance/directories/categories/category_goods_payment", payload={"action": "set_group", "group_id": group_id, "base_revision": 1}, csrf=csrf)
            assert status == 200 and assigned["data"]["group_id"] == group_id  # type: ignore[index]
            status, archived = request(base, f"/v1/finance/directories/category-groups/{group_id}", payload={"action": "archive", "base_revision": 1}, csrf=csrf)
            assert status == 200 and archived["data"]["revision"] == 2  # type: ignore[index]
            status, categories = request(base, "/v1/finance/categories")
            assert status == 200 and next(item for item in categories["data"]["categories"] if item["category_id"] == "category_goods_payment")["group_id"] == group_id  # type: ignore[index]
            status, counterparty = request(base, "/v1/finance/counterparties", payload={"name": "HTTP fixture counterparty"}, csrf=csrf)
            assert status == 201 and counterparty["data"]["counterparty_id"]  # type: ignore[index]
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
            status, vlad_draft = request(base, "/v1/finance/documents", payload={
                "document_type": "opening", "target_account_id": "cash_vladislav",
                "amount": "100.00", "occurred_at": "2026-09-21T10:00:00Z",
                "opening_evidence_type": "manual_confirmation",
            }, csrf=csrf)
            assert status == 201, vlad_draft
            vlad_doc = vlad_draft["data"]
            status, vlad_post = request(
                base, f"/v1/finance/documents/{vlad_doc['document_id']}/post",
                payload={"base_revision": vlad_doc["revision"]}, csrf=csrf,
            )
            assert status == 200 and vlad_post["data"]["balances"]["cash_vladislav"]["balance"] is None, vlad_post
            assert vlad_post["data"]["balances"]["cash_vladislav"]["balance_hidden"] is True
            status, vlad_operation = request(base, f"/v1/finance/operations/{vlad_post['data']['operation_id']}")
            assert status == 200 and vlad_operation["data"]["balances"]["cash_vladislav"]["balance_minor"] is None
            status, vlad_accounts = request(base, "/v1/finance/accounts", actor="admin")
            vlad = next(item for item in vlad_accounts["data"]["accounts"] if item["account_id"] == "cash_vladislav")
            assert status == 200 and vlad["balance"] is None and vlad["balance_hidden"] is True
            assert vlad["negative_balance_warning"] is None and vlad["balance_state"] == "current"
            status, account_detail = request(base, "/v1/finance/accounts/cash_vladislav", actor="viewer")
            assert status == 200 and account_detail["data"]["account"]["balance_minor"] is None
            status, movements = request(base, "/v1/finance/accounts/cash_vladislav/movements", actor="viewer")
            assert status == 200 and movements["data"]["balance"] is None and movements["data"]["movements"][0]["amount_minor"] == 10000
            status, visible = request(base, "/v1/finance/accounts", actor="vlad_granted")
            assert status == 200 and next(item for item in visible["data"]["accounts"] if item["account_id"] == "cash_vladislav")["balance"] == "100.00"
            status, reconciliation = request(base, "/v1/finance/cash-reconciliations", payload={
                "account_id": "cash_vladislav", "week_ending": "2026-09-27",
                "actual_amount": "99.00", "comment": "fixture discrepancy",
            }, csrf=csrf)
            assert status == 201, reconciliation
            assert all(reconciliation["data"][field] is None for field in ("expected_minor", "actual_minor", "difference_minor")), reconciliation
            status, rec_operation = request(base, f"/v1/finance/operations/{reconciliation['data']['operation_id']}")
            assert status == 200 and rec_operation["data"]["actual_minor"] is None and rec_operation["data"]["status"] == "hidden"
            status, rec_list = request(base, "/v1/finance/cash-reconciliations", actor="viewer")
            rec = next(item for item in rec_list["data"]["reconciliations"] if item["account_id"] == "cash_vladislav")
            assert status == 200 and rec["status"] == "hidden" and rec["difference_amount"] is None and rec["actual_minor"] is None
            status, rec_visible = request(base, "/v1/finance/cash-reconciliations", actor="vlad_granted")
            assert status == 200 and rec_visible["data"]["reconciliations"][0]["difference_amount"] == "-1.00"
            status, ordinary_rec = request(base, "/v1/finance/cash-reconciliations", payload={
                "account_id": account_id, "week_ending": "2026-09-27", "actual_amount": "12.34",
            }, csrf=csrf)
            assert status == 201 and ordinary_rec["data"]["actual_minor"] == 1234
            status, audit_hidden = request(base, "/v1/finance/audit", actor="admin")
            hidden_event = next(item for item in audit_hidden["data"]["events"] if item["object_id"] == reconciliation["data"]["reconciliation_id"])
            assert status == 200 and hidden_event["payload_json"] == "{}", hidden_event
            ordinary_event = next(item for item in audit_hidden["data"]["events"] if item["object_id"] == ordinary_rec["data"]["reconciliation_id"])
            assert json.loads(ordinary_event["payload_json"])["actual_minor"] == 1234
            status, audit_visible = request(base, "/v1/finance/audit", actor="vlad_granted")
            visible_event = next(item for item in audit_visible["data"]["events"] if item["object_id"] == reconciliation["data"]["reconciliation_id"])
            assert status == 200 and json.loads(visible_event["payload_json"]) == {"expected_minor": 10000, "actual_minor": 9900}
            status, documents = request(base, "/v1/finance/documents")
            item = next(item for item in documents["data"]["documents"] if item["target_account_id"] == account_id)  # type: ignore[index]
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
