#!/usr/bin/env python3
"""Synthetic directory, funding and explicit migration checks for Finance v3."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.finance_liquidity_cash import (
    FinanceCashError,
    FinanceCashService,
    _SCHEMA,
    _canon,
    _digest,
    bootstrap_finance_cash_store,
    migrate_finance_cash_store_category_groups,
    migrate_finance_cash_store_v2,
)
from packages.application.finance_liquidity_directories import SEED_CATEGORY_GROUPS, install_v3_extension, seed_directories


WHEN = "2026-09-21T05:00:00.000000Z"


def run_checks() -> None:
    with TemporaryDirectory(prefix="finance-directories-") as directory:
        common_path = Path(directory) / "common.sqlite3"
        bootstrap_finance_cash_store(common_path)
        common = FinanceCashService(common_path)
        common_payload = {"occurred_at": WHEN, "amounts": {
            "cash_vladislav": "0.00", "cash_victoria": "1.00", "cash_carolina": "2.00"
        }}
        prepared = common.prepare_common_openings(common_payload, "fixture", "common-op", "common-key")
        assert prepared == common.prepare_common_openings(common_payload, "fixture", "common-op", "common-key")
        assert len(prepared["documents"]) == 3
        assert {item["occurred_at"] for item in common.list_documents()} == {WHEN}
        assert {item["status"] for item in common.list_documents()} == {"draft"}
        assert all(item["balance"] is None for item in common.list_accounts())
        path = Path(directory) / "cash.sqlite3"
        bootstrap_finance_cash_store(path)
        service = FinanceCashService(path)
        assert {a["account_id"] for a in service.list_accounts()} == {
            "cash_vladislav", "cash_victoria", "cash_carolina"
        }
        assert len(service.list_categories()) == 23
        groups = service.list_category_groups()
        assert groups["enabled"] and len(groups["groups"]) == 6
        seeded = {category["code"]: category for category in service.list_categories()}
        for group_id, _, codes in SEED_CATEGORY_GROUPS:
            assert all(seeded[code]["group_id"] == group_id for code in codes)
        assert service.list_counterparties() == []
        sequence = 0

        def op() -> tuple[str, str]:
            nonlocal sequence
            sequence += 1
            return f"directories-op-{sequence}", f"directories-key-{sequence}"

        def opening(account_id: str, amount: str) -> None:
            doc = service.create_document({
                "document_type": "opening", "target_account_id": account_id,
                "occurred_at": WHEN, "amount": amount,
                "opening_evidence_type": "manual_confirmation",
                "opening_evidence_ref": "Синтетическая проверка",
            }, "fixture", *op())
            service.post_document(doc["document_id"], {"base_revision": 1}, "fixture", *op())

        for account_id, amount in (("cash_vladislav", "100.00"), ("cash_victoria", "0.00"), ("cash_carolina", "0.00")):
            opening(account_id, amount)
        funding = service.create_document({
            "document_type": "income", "funding_kind": "owner_funding",
            "target_account_id": "cash_vladislav", "occurred_at": WHEN,
            "amount": "50.00",
        }, "fixture", *op())
        service.post_document(funding["document_id"], {"base_revision": 1}, "fixture", *op())
        funding_record = service.get_document(funding["document_id"])
        funding_entries = funding_record["transactions"][0]["entries"]
        assert {entry["ledger_account_id"] for entry in funding_entries} == {
            "asset:cash_vladislav", "system:owner_funding:RUB"
        }
        assert funding_record["document"]["analytic_class_snapshot"] == "owner_funding"

        transfer = service.create_document({
            "document_type": "transfer", "source_account_id": "cash_vladislav",
            "target_account_id": "cash_victoria", "amount": "20.00",
            "occurred_at": WHEN,
        }, "fixture", *op())
        service.post_document(transfer["document_id"], {"base_revision": 1}, "fixture", *op())
        transfer_record = service.get_document(transfer["document_id"])
        assert len(transfer_record["transactions"]) == 1
        assert {entry["ledger_account_id"] for entry in transfer_record["transactions"][0]["entries"]} == {
            "asset:cash_vladislav", "asset:cash_victoria"
        }
        assert transfer_record["document"]["category_id"] is None
        assert service.get_account("cash_vladislav")["balance"] == "130.00"
        assert service.get_account("cash_victoria")["balance"] == "20.00"

        counterparty = service.create_counterparty({"name": "Тестовый поставщик"}, "fixture", *op())
        repeated = service.create_counterparty({"name": "Тестовый поставщик"}, "fixture", "same-operation", "same-key")
        assert repeated == service.create_counterparty({"name": "Тестовый поставщик"}, "fixture", "same-operation", "same-key")
        assert repeated["counterparty_id"] == counterparty["counterparty_id"]
        misc = "category_miscellaneous"
        invalid = {
            "document_type": "expense", "source_account_id": "cash_vladislav",
            "category_id": misc, "counterparty_id": counterparty["counterparty_id"],
            "amount": "1.00", "occurred_at": WHEN, "purpose": "   ",
        }
        try:
            service.create_document(invalid, "fixture", *op())
        except FinanceCashError as error:
            assert error.code == "comment_required"
        else:
            raise AssertionError("Whitespace-only miscellaneous comment was accepted")
        draft = service.create_document({**invalid, "purpose": "Стекло для проверки"}, "fixture", *op())
        draft_only_account = service.create_account({"name": "Черновая касса", "account_type": "cash", "currency": "RUB", "responsible_name": "Fixture"}, "fixture", *op())
        service.create_document({"document_type": "transfer", "source_account_id": draft_only_account["account_id"], "target_account_id": "cash_victoria", "amount": "1.00", "occurred_at": WHEN}, "fixture", *op())
        try:
            service.update_directory("accounts", draft_only_account["account_id"], {"action": "delete", "base_revision": 1}, "fixture", *op())
        except FinanceCashError as error:
            assert error.code == "directory_in_use"
        else:
            raise AssertionError("Cashbox used only by a draft was deleted")
        for kind, ident in (("categories", misc), ("counterparties", counterparty["counterparty_id"]), ("accounts", "cash_vladislav")):
            entry = next(item for item in {"categories": service.list_categories(), "counterparties": service.list_counterparties(), "accounts": service.list_accounts()}[kind] if item[f"{kind[:-1] if kind != 'categories' else 'category'}_id" if kind != "counterparties" else "counterparty_id"] == ident)
            try:
                service.update_directory(kind, ident, {"action": "delete", "base_revision": entry["revision"]}, "fixture", *op())
            except FinanceCashError as error:
                assert error.code == "directory_in_use"
            else:
                raise AssertionError(f"Referenced {kind} entry was deleted")
        service.post_document(draft["document_id"], {"base_revision": 1}, "fixture", *op())
        before = service.get_document(draft["document_id"])["document"]
        group_operation = op()
        group = service.create_category_group({"name": "Особые закупки"}, "fixture", *group_operation)
        assert group == service.create_category_group({"name": "Особые закупки"}, "fixture", *group_operation)
        group_id = group["group_id"]
        service.update_directory("categories", misc, {"action": "set_group", "group_id": group_id, "base_revision": 1}, "fixture", *op())
        assert next(item for item in service.list_categories() if item["category_id"] == misc)["group_id"] == group_id
        try:
            service.update_directory("categories", misc, {"action": "set_group", "group_id": None, "base_revision": 1}, "fixture", *op())
        except FinanceCashError as error:
            assert error.code == "version_conflict"
        else:
            raise AssertionError("Stale group assignment was accepted")
        group = service.update_category_group(group_id, {"action": "rename", "name": "Особые закупки и доставка", "base_revision": 1}, "fixture", *op())
        group = service.update_category_group(group_id, {"action": "archive", "base_revision": group["revision"]}, "fixture", *op())
        assert not next(item for item in service.list_category_groups()["groups"] if item["group_id"] == group_id)["is_active"]
        assert next(item for item in service.list_categories() if item["category_id"] == misc)["group_id"] == group_id
        try:
            service.update_directory("categories", "category_goods_payment", {"action": "set_group", "group_id": group_id, "base_revision": 1}, "fixture", *op())
        except FinanceCashError as error:
            assert error.code == "category_group_unavailable"
        else:
            raise AssertionError("Archived group accepted a new article")
        service.update_directory("categories", misc, {"action": "set_group", "group_id": None, "base_revision": 2}, "fixture", *op())
        group = service.update_category_group(group_id, {"action": "restore", "base_revision": group["revision"]}, "fixture", *op())
        assert group["revision"] == 4
        service.update_directory("categories", misc, {"action": "rename", "name": "Иные расходы", "base_revision": 3}, "fixture", *op())
        service.update_directory("counterparties", counterparty["counterparty_id"], {"action": "rename", "name": "Новое имя", "base_revision": 1}, "fixture", *op())
        try:
            service.update_directory("counterparties", counterparty["counterparty_id"], {"action": "rename", "name": "Устаревшее изменение", "base_revision": 1}, "fixture", *op())
        except FinanceCashError as error:
            assert error.code == "version_conflict"
        else:
            raise AssertionError("Stale directory edit was accepted")
        after = service.get_document(draft["document_id"])["document"]
        assert before["category_name_snapshot"] == after["category_name_snapshot"] == "Прочие расходы"
        assert before["counterparty_name_snapshot"] == after["counterparty_name_snapshot"] == "Тестовый поставщик"
        assert before["analytic_class_snapshot"] == after["analytic_class_snapshot"] == "operating_expense"
        snapshot_insert = (
            "INSERT INTO finance_liquidity_v2_directory_snapshots"
            "(document_id,category_name_at_migration,captured_at) VALUES(?,?,?)"
        )
        with sqlite3.connect(path) as conn:
            try:
                conn.execute(snapshot_insert, (draft["document_id"], "Подмена имени", WHEN))
            except sqlite3.IntegrityError as error:
                assert "migration snapshot immutable" in str(error)
            else:
                raise AssertionError("Fresh v3 store accepted a late migration snapshot")
        forged_path = Path(directory) / "forged-v3.sqlite3"
        with sqlite3.connect(path) as source, sqlite3.connect(forged_path) as forged:
            source.backup(forged)
            forged.execute("DROP TRIGGER finance_v2_snapshot_immutable_insert")
            forged.execute(snapshot_insert, (draft["document_id"], "Подмена имени", WHEN))
        assert FinanceCashService(forged_path).get_document(draft["document_id"])["document"]["category_name_snapshot"] == "Прочие расходы"
        assert service.get_account("cash_vladislav")["balance"] == "129.00"
        service.update_directory("accounts", "cash_vladislav", {"action": "rename", "name": "Касса Владислав (новое имя)", "base_revision": 1}, "fixture", *op())
        assert service.get_account("cash_vladislav")["balance"] == "129.00"
        categories = {item["category_id"]: item for item in service.list_categories()}
        assert categories["category_owner_draw"]["analytic_class"] == "owner_draw"
        assert categories["category_profit_distribution"]["analytic_class"] == "profit_distribution"
        debt = service.create_document({"document_type": "expense", "source_account_id": "cash_vladislav", "category_id": "category_debt_payment", "amount": "2.00", "occurred_at": WHEN}, "fixture", *op())
        service.post_document(debt["document_id"], {"base_revision": 1}, "fixture", *op())
        assert service.get_document(debt["document_id"])["document"]["analytic_class_snapshot"] == "debt_service_unallocated"
        assert service.get_account("cash_vladislav")["balance"] == "127.00"
        assert any(event["event_type"] == "categories.rename" and '"old_name":"Прочие расходы"' in event["payload_json"] and '"new_name":"Иные расходы"' in event["payload_json"] for event in service.list_audit_events())
        assert all(event["event_type"].split(".")[0] in {"account", "accounts", "category", "categories", "category_group", "counterparty", "counterparties"} for event in service.list_audit_events(directory_only=True))
        service.update_directory("categories", misc, {"action": "archive", "base_revision": 4}, "fixture", *op())
        service.update_directory("categories", misc, {"action": "restore", "base_revision": 5}, "fixture", *op())
        service.update_directory("categories", "category_rent", {"action": "delete", "base_revision": 1}, "fixture", *op())
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            seed_directories(conn, WHEN)
            assert conn.execute("SELECT name FROM finance_liquidity_categories WHERE category_id=?", (misc,)).fetchone()[0] == "Иные расходы"
            assert conn.execute("SELECT name FROM finance_liquidity_accounts WHERE account_id='cash_vladislav'").fetchone()[0] == "Касса Владислав (новое имя)"
            assert conn.execute("SELECT is_deleted FROM finance_liquidity_categories WHERE category_id='category_rent'").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM finance_liquidity_audit_events WHERE event_type LIKE '%rename' OR event_type='document.posted'").fetchone()[0] >= 3
        unused = service.create_counterparty({"name": "Удаляемый"}, "fixture", *op())
        service.update_directory("counterparties", unused["counterparty_id"], {"action": "delete", "base_revision": 1}, "fixture", *op())
        assert unused["counterparty_id"] not in {item["counterparty_id"] for item in service.list_counterparties()}

        # An existing v3 store stays usable before the explicitly invoked,
        # additive group install. Documents and balances cannot be rewritten.
        plain_v3 = Path(directory) / "plain-v3.sqlite3"
        with sqlite3.connect(plain_v3) as conn:
            conn.create_function("finance_internal_write", 0, lambda: 1)
            conn.executescript(_SCHEMA)
            install_v3_extension(conn, WHEN)
            conn.execute("INSERT INTO finance_liquidity_schema_meta VALUES(1,3,?)", (WHEN,))
        plain = FinanceCashService(plain_v3)
        assert plain.list_category_groups() == {"enabled": False, "groups": []}
        assert all(item["group_id"] is None for item in plain.list_categories())
        plain_opening = plain.create_document({"document_type": "opening", "target_account_id": "cash_vladislav", "amount": "20.00", "occurred_at": WHEN, "opening_evidence_type": "manual_confirmation"}, "fixture", *op())
        plain.post_document(plain_opening["document_id"], {"base_revision": 1}, "fixture", *op())
        plain_expense = plain.create_document({"document_type": "expense", "source_account_id": "cash_vladislav", "category_id": "category_goods_payment", "amount": "2.00", "occurred_at": WHEN}, "fixture", *op())
        plain.post_document(plain_expense["document_id"], {"base_revision": 1}, "fixture", *op())
        plain_draft = plain.create_document({"document_type": "expense", "source_account_id": "cash_vladislav", "category_id": "category_marketing", "amount": "1.00", "occurred_at": WHEN}, "fixture", *op())
        original_documents = plain.list_documents()
        original_balance = plain.get_account("cash_vladislav")["balance"]
        with sqlite3.connect(plain_v3) as conn:
            original_ledger = list(conn.execute("SELECT * FROM finance_liquidity_ledger_entries ORDER BY entry_id"))
            original_version = conn.execute("SELECT schema_version FROM finance_liquidity_schema_meta").fetchone()[0]
        group_backup = Path(directory) / "plain-v3-backup.sqlite3"
        migrate_finance_cash_store_category_groups(plain_v3, group_backup)
        assert sqlite3.connect(group_backup).execute("SELECT schema_version FROM finance_liquidity_schema_meta").fetchone()[0] == 3
        assert plain.list_category_groups()["enabled"]
        assert plain.get_account("cash_vladislav")["balance"] == original_balance == "18.00"
        assert plain.list_documents() == original_documents
        assert plain.get_document(plain_draft["document_id"])["document"]["status"] == "draft"
        with sqlite3.connect(plain_v3) as conn:
            assert conn.execute("SELECT schema_version FROM finance_liquidity_schema_meta").fetchone()[0] == original_version == 3
            assert list(conn.execute("SELECT * FROM finance_liquidity_ledger_entries ORDER BY entry_id")) == original_ledger
            assert conn.execute("SELECT extension_version FROM finance_liquidity_extensions WHERE extension_name='category_groups'").fetchone()[0] == 1
        assert next(item for item in plain.list_categories() if item["category_id"] == "category_marketing")["group_id"] == "category_group_marketing_customers"
        migrate_finance_cash_store_category_groups(plain_v3, Path(directory) / "repeat-backup-must-not-exist.sqlite3")
        assert not (Path(directory) / "repeat-backup-must-not-exist.sqlite3").exists()
        partial = Path(directory) / "partial-v3.sqlite3"
        with sqlite3.connect(group_backup) as source, sqlite3.connect(partial) as target:
            source.backup(target)
            target.execute("ALTER TABLE finance_liquidity_categories ADD COLUMN group_id TEXT")
        try:
            FinanceCashService(partial).list_categories()
        except FinanceCashError as error:
            assert error.code == "finance_schema_unavailable"
        else:
            raise AssertionError("Partial group extension was accepted")
        try:
            migrate_finance_cash_store_category_groups(partial, Path(directory) / "partial-backup.sqlite3")
        except FinanceCashError as error:
            assert error.code == "finance_schema_unavailable"
        else:
            raise AssertionError("Partial group extension migrated instead of failing closed")

        old = Path(directory) / "old-v2.sqlite3"
        with sqlite3.connect(old) as conn:
            conn.create_function("finance_internal_write", 0, lambda: 1)
            conn.executescript(_SCHEMA)
            conn.execute("INSERT INTO finance_liquidity_schema_meta VALUES(1,2,?)", (WHEN,))
            conn.execute("INSERT INTO finance_liquidity_accounts(account_id,name,account_type,currency,currency_exponent,responsible_name,is_active,revision,created_at,updated_at) VALUES('legacy-cash','Legacy cash','cash','RUB',2,'Fixture',1,1,?,?)", (WHEN, WHEN))
            conn.execute("INSERT INTO finance_liquidity_documents(document_id,document_type,status,target_account_id,amount_minor,occurred_at,opening_evidence_type,opening_evidence_digest,revision,created_at,updated_at,posted_at,semantic_digest,actor) VALUES('legacy-opening','opening','posted','legacy-cash',1000,?,'manual_confirmation',?,2,?,?,?,?,?)", (WHEN, "sha256:" + "a" * 64, WHEN, WHEN, WHEN, "legacy-semantic", "fixture"))
            receipt = {"document_id": "legacy-opening", "ledger_transaction_ids": ["legacy-tx"], "operation_id": "legacy-post", "receipt_id": "legacy-receipt"}
            conn.execute("INSERT INTO finance_liquidity_operations(operation_id,scope,idempotency_key,request_digest,actor,effect_root_document_id,result_json,created_at) VALUES('legacy-post','document.post:legacy-opening','legacy-key','legacy-request','fixture','legacy-opening',?,?)", (_canon(receipt), WHEN))
            conn.execute("INSERT INTO finance_liquidity_ledger_transactions(transaction_id,document_id,origin_operation_id,phase,effective_at) VALUES('legacy-tx','legacy-opening','legacy-post','primary',?)", (WHEN,))
            entries = [
                {"line_no": 1, "ledger_account_id": "asset:legacy-cash", "side": "debit", "amount_minor": 1000},
                {"line_no": 2, "ledger_account_id": "system:opening:RUB", "side": "credit", "amount_minor": 1000},
            ]
            for entry in entries:
                conn.execute("INSERT INTO finance_liquidity_ledger_entries(entry_id,transaction_id,line_no,ledger_account_id,side,amount_minor) VALUES(?,?,?,?,?,?)", (f"legacy-entry-{entry['line_no']}", "legacy-tx", entry["line_no"], entry["ledger_account_id"], entry["side"], entry["amount_minor"]))
            conn.execute("INSERT INTO finance_liquidity_ledger_transaction_seals(transaction_id,entry_count,entries_digest,sealed_at) VALUES('legacy-tx',2,?,?)", (_digest(entries), WHEN))
            transactions = [{"transaction_id": "legacy-tx", "document_id": "legacy-opening", "phase": "primary", "effective_at": WHEN}]
            conn.execute("INSERT INTO finance_liquidity_effect_set_seals(operation_id,root_document_id,transaction_count,transactions_digest,sealed_at) VALUES('legacy-post','legacy-opening',1,?,?)", (_digest(transactions), WHEN))
            conn.execute("INSERT INTO finance_liquidity_opening_anchors(anchor_id,account_id,opening_document_id,is_active,cutover_at,created_at) VALUES('legacy-anchor','legacy-cash','legacy-opening',1,?,?)", (WHEN, WHEN))
            conn.execute("INSERT INTO finance_liquidity_categories(category_id,name,direction,posting_class,is_active,created_at) VALUES('legacy-category','Старое имя','expense','external_outflow',1,?)", (WHEN,))
            conn.execute("INSERT INTO finance_liquidity_categories(category_id,name,direction,posting_class,is_active,created_at) VALUES('legacy-income-category','Старое поступление','income',NULL,1,?)", (WHEN,))
            conn.execute("INSERT INTO finance_liquidity_documents(document_id,document_type,status,source_account_id,category_id,amount_minor,occurred_at,purpose,revision,created_at,updated_at,posted_at,semantic_digest,actor) VALUES('legacy-expense','expense','posted','legacy-cash','legacy-category',100,?,'Legacy',2,?,?,?,?,?)", (WHEN, WHEN, WHEN, WHEN, "legacy-expense-semantic", "fixture"))
            expense_receipt = {"document_id": "legacy-expense", "ledger_transaction_ids": ["legacy-expense-tx"], "operation_id": "legacy-expense-post", "receipt_id": "legacy-expense-receipt"}
            conn.execute("INSERT INTO finance_liquidity_operations(operation_id,scope,idempotency_key,request_digest,actor,effect_root_document_id,result_json,created_at) VALUES('legacy-expense-post','document.post:legacy-expense','legacy-expense-key','legacy-expense-request','fixture','legacy-expense',?,?)", (_canon(expense_receipt), WHEN))
            conn.execute("INSERT INTO finance_liquidity_ledger_transactions(transaction_id,document_id,origin_operation_id,phase,effective_at) VALUES('legacy-expense-tx','legacy-expense','legacy-expense-post','primary',?)", (WHEN,))
            expense_entries = [
                {"line_no": 1, "ledger_account_id": "system:external_outflow:RUB", "side": "debit", "amount_minor": 100},
                {"line_no": 2, "ledger_account_id": "asset:legacy-cash", "side": "credit", "amount_minor": 100},
            ]
            for entry in expense_entries:
                conn.execute("INSERT INTO finance_liquidity_ledger_entries(entry_id,transaction_id,line_no,ledger_account_id,side,amount_minor) VALUES(?,?,?,?,?,?)", (f"legacy-expense-entry-{entry['line_no']}", "legacy-expense-tx", entry["line_no"], entry["ledger_account_id"], entry["side"], entry["amount_minor"]))
            conn.execute("INSERT INTO finance_liquidity_ledger_transaction_seals(transaction_id,entry_count,entries_digest,sealed_at) VALUES('legacy-expense-tx',2,?,?)", (_digest(expense_entries), WHEN))
            expense_transactions = [{"transaction_id": "legacy-expense-tx", "document_id": "legacy-expense", "phase": "primary", "effective_at": WHEN}]
            conn.execute("INSERT INTO finance_liquidity_effect_set_seals(operation_id,root_document_id,transaction_count,transactions_digest,sealed_at) VALUES('legacy-expense-post','legacy-expense',1,?,?)", (_digest(expense_transactions), WHEN))
        backup = Path(directory) / "backup-v2.sqlite3"
        migrate_finance_cash_store_v2(old, backup)
        assert sqlite3.connect(backup).execute("SELECT schema_version FROM finance_liquidity_schema_meta").fetchone()[0] == 2
        migrated = FinanceCashService(old)
        assert len(migrated.list_accounts()) == 4
        assert len(migrated.list_categories()) == 25
        legacy_categories = {item["category_id"]: item for item in migrated.list_categories()}
        assert legacy_categories["legacy-category"]["analytic_class"] == "legacy_expense_unclassified"
        assert legacy_categories["legacy-income-category"]["analytic_class"] == "external_inflow_unclassified"
        assert migrated.get_account("legacy-cash")["balance"] == "9.00"
        assert migrated.get_document("legacy-opening")["document"]["status"] == "posted"
        legacy_before = migrated.get_document("legacy-expense")["document"]
        assert legacy_before["category_name_snapshot"] == "Старое имя"
        assert legacy_before["analytic_class_snapshot"] is None
        assert legacy_before["directory_snapshot_origin"] == "v2_migration"
        migrated.update_directory("categories", "legacy-category", {"action": "rename", "name": "Новое имя", "base_revision": 1}, "fixture", "legacy-rename", "legacy-rename-key")
        legacy_after = migrated.get_document("legacy-expense")["document"]
        assert legacy_after["category_name_snapshot"] == "Старое имя"
        assert migrated.get_account("legacy-cash")["balance"] == "9.00"
        with sqlite3.connect(old) as conn:
            assert conn.execute("SELECT category_name_snapshot FROM finance_liquidity_documents WHERE document_id='legacy-expense'").fetchone()[0] is None
            assert conn.execute("SELECT category_name_at_migration FROM finance_liquidity_v2_directory_snapshots WHERE document_id='legacy-expense'").fetchone()[0] == "Старое имя"
            try:
                conn.execute(snapshot_insert, ("legacy-opening", "Поздняя вставка", WHEN))
            except sqlite3.IntegrityError as error:
                assert "migration snapshot immutable" in str(error)
            else:
                raise AssertionError("Migrated v2 store accepted a late migration snapshot")
        legacy_income = migrated.create_document({"document_type": "income", "target_account_id": "legacy-cash", "category_id": "legacy-income-category", "amount": "3.00", "occurred_at": WHEN}, "fixture", *op())
        migrated.post_document(legacy_income["document_id"], {"base_revision": 1}, "fixture", *op())
        assert migrated.get_document(legacy_income["document_id"])["document"]["analytic_class_snapshot"] == "external_inflow_unclassified"
        assert migrated.get_account("legacy-cash")["balance"] == "12.00"
        try:
            migrate_finance_cash_store_v2(old, Path(directory) / "second-backup.sqlite3")
        except FinanceCashError as error:
            assert error.code == "invalid_migration_source"
        else:
            raise AssertionError("Migration unexpectedly repeated")


if __name__ == "__main__":
    run_checks()
    print("finance_liquidity_directories_smoke: ok")
