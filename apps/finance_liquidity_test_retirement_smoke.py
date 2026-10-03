#!/usr/bin/env python3
"""Exercise offline retirement, immutable facts, backup, replay and guards."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sqlite3
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.finance_liquidity_access import FinanceBootstrapAccess
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
        posted({"document_type": "transfer", "source_account_id": a, "target_account_id": b,
                "amount": "5.00", "occurred_at": "2026-09-21T11:00:00Z", "transfer_mode": "instant",
                "purpose": "fixture transfer"})
        command(service.record_reconciliation, {"account_id": a, "week_ending": "2026-09-21",
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
