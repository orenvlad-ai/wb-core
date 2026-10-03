#!/usr/bin/env python3
"""Exercise offline retirement, immutable facts, backup, replay and guards."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sqlite3
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.finance_liquidity_access import FinanceBootstrapAccess
from packages.adapters.finance_liquidity_http import FinanceHttpApp, build_finance_http_server
from packages.application.finance_liquidity_cash import FinanceCashError, FinanceCashService, bootstrap_finance_cash_store
from packages.application.finance_liquidity_test_retirement import (
    apply_test_account_retirement, preview_test_account_retirement, readback_test_account_retirement,
)


def run() -> None:
    with TemporaryDirectory() as directory:
        db = Path(directory).resolve() / "test.sqlite3"
        backup = Path(directory).resolve() / "before.sqlite3"
        bootstrap_finance_cash_store(db)
        service = FinanceCashService(db)
        access = FinanceBootstrapAccess("owner", "finance_admin", "TEST", "fixture", db, "isolated_test")

        def command(method, payload):
            return method(payload, "fixture", f"op-{uuid4().hex}", f"key-{uuid4().hex}")

        def account(name):
            return command(service.create_account, {
                "name": name, "account_type": "cash", "currency": "RUB", "responsible_name": "Fixture"
            })["account_id"]

        a, b = account("TEST A"), account("TEST B")
        real = account("Real cash")

        def posted(payload):
            draft = command(service.create_document, payload)
            return service.post_document(draft["document_id"], {"base_revision": draft["revision"]},
                                         "fixture", f"op-{uuid4().hex}", f"key-{uuid4().hex}")

        for ident in (a, b, real):
            posted({"document_type": "opening", "target_account_id": ident, "amount": "100.00",
                    "occurred_at": "2026-09-21T10:00:00Z", "opening_evidence_type": "manual_confirmation",
                    "opening_evidence_ref": "fixture"})
        retired_document = posted({"document_type": "transfer", "source_account_id": a, "target_account_id": b,
                "amount": "5.00", "occurred_at": "2026-09-21T11:00:00Z", "transfer_mode": "instant",
                "purpose": "fixture transfer"})
        retired_reconciliation = command(service.record_reconciliation, {"account_id": a, "week_ending": "2026-09-21",
                                                "actual_amount": "95.00"})

        def rows(table):
            with sqlite3.connect(db) as conn:
                return conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()

        facts = {name: rows(name) for name in (
            "finance_liquidity_documents", "finance_liquidity_opening_anchors",
            "finance_liquidity_cash_reconciliations", "finance_liquidity_ledger_transactions",
            "finance_liquidity_ledger_entries", "finance_liquidity_ledger_transaction_seals",
            "finance_liquidity_effect_set_seals",
        )}
        real_before = service.get_account(real)
        preview = preview_test_account_retirement(db, access, "fixture", (a, b))
        assert preview["document_count"] == 3 and preview["reconciliation_count"] == 1
        wrong_mode = FinanceBootstrapAccess("owner", "finance_admin", "TEST", "fixture", db, "live")
        try:
            preview_test_account_retirement(db, wrong_mode, "fixture", (a, b))
        except FinanceCashError as exc:
            assert exc.code == "invalid_retirement_target"
        else:
            raise AssertionError("non-TEST store accepted")
        occupied = Path(directory).resolve() / "occupied.sqlite3"
        occupied.write_bytes(b"occupied")
        for invalid_backup in (occupied, Path(directory).resolve() / "alias.sqlite3"):
            if invalid_backup != occupied:
                invalid_backup.symlink_to(occupied)
            try:
                apply_test_account_retirement(db, access, "fixture", (a, b), invalid_backup,
                                              preview["fingerprint"], "retire-invalid", "fixture",
                                              service_stopped=True)
            except FinanceCashError as exc:
                assert exc.code == "invalid_retirement_backup"
            else:
                raise AssertionError("occupied or symlink backup accepted")
        try:
            apply_test_account_retirement(db, access, "fixture", (a, b), backup,
                                          preview["fingerprint"], "retire-1", "fixture", service_stopped=False)
        except FinanceCashError as exc:
            assert exc.code == "retirement_precondition"
        else:
            raise AssertionError("missing stopped-service assertion accepted")
        assert not backup.exists()
        receipt = apply_test_account_retirement(db, access, "fixture", (a, b), backup,
                                                preview["fingerprint"], "retire-1", "fixture",
                                                service_stopped=True)
        assert receipt["retired_account_ids"] == [a, b] and backup.is_file()
        assert apply_test_account_retirement(db, access, "fixture", (a, b), backup,
                                             preview["fingerprint"], "retire-1", "fixture",
                                             service_stopped=True) == receipt
        assert readback_test_account_retirement(db, access, "fixture", (a, b),
                                                preview["fingerprint"], "retire-1", "fixture") == receipt
        try:
            readback_test_account_retirement(db, access, "fixture", (a, real),
                                             preview["fingerprint"], "retire-1", "fixture")
        except FinanceCashError as exc:
            assert exc.code == "retirement_receipt_not_found"
        else:
            raise AssertionError("wrong candidate readback accepted")
        assert a not in {item["account_id"] for item in service.list_accounts()}
        assert b not in {item["account_id"] for item in service.list_accounts()}
        assert len(service.list_documents()) == 1 and len(service.list_reconciliations()) == 0
        try:
            service.get_document(retired_document["document_id"])
        except FinanceCashError as exc:
            assert exc.status == 404
        else:
            raise AssertionError("retired document appeared in ordinary detail")
        assert service.get_document(retired_document["document_id"], include_retired=True)["document"]["amount"] == "5.00"
        assert service.get_reconciliation(retired_reconciliation["reconciliation_id"], include_retired=True)["account_id"] == a
        assert service.reconciliation_account_id(retired_reconciliation["reconciliation_id"]) == a

        class FixtureAuth:
            def authenticate(self, headers):
                admin = "finance_fixture_session=admin" in str(headers.get("Cookie") or "")
                return {"username": "fixture" if admin else "viewer", "role": "operator",
                        "capabilities": ["finance", "finance_operate", "finance_admin"] if admin else ["finance"]}

        app = FinanceHttpApp(service, FixtureAuth(), read_enabled=True, write_enabled=False,
                             csrf_secret="fixture", static_dir=ROOT / "packages/adapters/finance_liquidity_static",
                             allowed_origin="", business_runtime_dir=Path(directory))
        server = build_finance_http_server("127.0.0.1", 0, app)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"

            def get(path, *, admin=False):
                request = Request(base + path, headers={"Cookie": "finance_fixture_session=" + ("admin" if admin else "viewer")})
                try:
                    with urlopen(request) as response:
                        return response.status, json.loads(response.read())
                except HTTPError as error:
                    return error.code, json.loads(error.read())

            document_path = f"/v1/finance/documents/{retired_document['document_id']}"
            reconciliation_path = f"/v1/finance/cash-reconciliations/{retired_reconciliation['reconciliation_id']}"
            assert get(document_path)[0] == 404
            assert get(document_path + "?include_retired=1")[0] == 403
            code, body = get(document_path + "?include_retired=1", admin=True)
            assert code == 200 and body["data"]["document"]["amount"] == "5.00", (code, body)
            assert get(reconciliation_path)[0] == 404
            assert get(reconciliation_path + "?include_retired=1")[0] == 403
            assert get(reconciliation_path + "?include_retired=1", admin=True)[1]["data"]["reconciliation"]["account_id"] == a
            operation_path = f"/v1/finance/operations/{retired_reconciliation['operation_id']}"
            assert get(operation_path, admin=True)[1]["data"]["actual_minor"] == 9500
        finally:
            server.shutdown()
            server.server_close()
        assert service.get_account(real) == real_before
        for name, before in facts.items():
            assert rows(name) == before, name
        with sqlite3.connect(db) as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        try:
            preview_test_account_retirement(db, access, "fixture", ("cash_vladislav",))
        except FinanceCashError as exc:
            assert exc.code == "invalid_retirement_accounts"
        else:
            raise AssertionError("seed account accepted")

        cross_db = Path(directory).resolve() / "cross.sqlite3"
        shutil.copyfile(backup, cross_db)
        cross_access = FinanceBootstrapAccess("owner", "finance_admin", "TEST", "fixture", cross_db, "isolated_test")
        cross_service = FinanceCashService(cross_db)
        cross_service.create_document({
            "document_type": "transfer", "source_account_id": a, "target_account_id": real,
            "amount": "1.00", "occurred_at": "2026-09-21T12:00:00Z",
            "transfer_mode": "instant", "purpose": "cross graph fixture",
        }, "fixture", f"op-{uuid4().hex}", f"key-{uuid4().hex}")
        try:
            preview_test_account_retirement(cross_db, cross_access, "fixture", (a, b))
        except FinanceCashError as exc:
            assert exc.code == "retirement_cross_account_graph"
        else:
            raise AssertionError("cross-account document accepted")

        stale_db = Path(directory).resolve() / "stale.sqlite3"
        stale_backup = Path(directory).resolve() / "stale-before.sqlite3"
        shutil.copyfile(backup, stale_db)
        stale_access = FinanceBootstrapAccess("owner", "finance_admin", "TEST", "fixture", stale_db, "isolated_test")
        stale_service = FinanceCashService(stale_db)
        stale_preview = preview_test_account_retirement(stale_db, stale_access, "fixture", (a, b))
        stale_service.create_category({"name": "Changed after preview", "direction": "income"},
                                      "fixture", f"op-{uuid4().hex}", f"key-{uuid4().hex}")
        try:
            apply_test_account_retirement(stale_db, stale_access, "fixture", (a, b), stale_backup,
                                          stale_preview["fingerprint"], "retire-stale", "fixture",
                                          service_stopped=True)
        except FinanceCashError as exc:
            assert exc.code == "retirement_source_changed"
        else:
            raise AssertionError("stale preview accepted")
        assert all(item["is_deleted"] == 0 for item in stale_service.list_accounts() if item["account_id"] in {a, b})
        print(json.dumps({"ok": True, "retired": 2}))


if __name__ == "__main__":
    run()
