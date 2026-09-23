#!/usr/bin/env python3
"""Targeted A1-A3 readiness checks for the isolated Finance cash store."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from typing import Any, Callable
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.finance_liquidity_cash import (  # noqa: E402
    FinanceCashError,
    FinanceCashService,
    _canon,
    _digest,
    bootstrap_finance_cash_store,
)
from packages.contracts.finance_liquidity_cash import (  # noqa: E402
    FINANCE_CASH_SCHEMA_VERSION,
)
from packages.domain.finance_liquidity import money_to_api  # noqa: E402


ACTOR = "readiness-smoke"
OPENING_AT = "2026-09-21T08:00:00Z"


def identities() -> tuple[str, str]:
    return f"key-{uuid4().hex}", f"operation-{uuid4().hex}"


def expect_finance_error(code: str, action: Callable[[], Any]) -> None:
    try:
        action()
    except FinanceCashError as error:
        assert error.code == code, (code, error.code)
        return
    raise AssertionError(f"expected {code}")


def expect_sql_rejection(action: Callable[[], Any]) -> None:
    try:
        action()
    except sqlite3.DatabaseError:
        return
    raise AssertionError("SQL guard accepted an invalid transition")


def create_account(
    service: FinanceCashService, name: str, opening: str
) -> tuple[str, dict[str, Any]]:
    key, operation_id = identities()
    account = service.create_account(
        {
            "name": name,
            "account_type": "cash",
            "currency": "RUB",
            "responsible_name": "Synthetic cashier",
        },
        ACTOR,
        operation_id,
        key,
    )
    key, operation_id = identities()
    draft = service.create_document(
        {
            "document_type": "opening",
            "target_account_id": account["account_id"],
            "amount": opening,
            "occurred_at": OPENING_AT,
            "opening_evidence_type": "manual_confirmation",
            "opening_evidence_ref": "synthetic readiness fixture",
        },
        ACTOR,
        operation_id,
        key,
    )
    key, operation_id = identities()
    posted = service.post_document(
        str(draft["document_id"]),
        {"base_revision": draft["revision"]},
        ACTOR,
        operation_id,
        key,
    )
    return str(account["account_id"]), posted


def create_and_post(
    service: FinanceCashService, payload: dict[str, Any]
) -> tuple[dict[str, Any], str, str, dict[str, Any]]:
    key, operation_id = identities()
    draft = service.create_document(payload, ACTOR, operation_id, key)
    post_key, post_operation = identities()
    posted = service.post_document(
        str(draft["document_id"]),
        {"base_revision": draft["revision"]},
        ACTOR,
        post_operation,
        post_key,
    )
    return posted, post_key, post_operation, draft


def assert_effect_receipt(
    db_path: Path, response: dict[str, Any], expected_count: int
) -> None:
    with sqlite3.connect(db_path) as conn:
        seal = conn.execute(
            "SELECT root_document_id,transaction_count FROM "
            "finance_liquidity_effect_set_seals WHERE operation_id=?",
            (response["operation_id"],),
        ).fetchone()
        operation = conn.execute(
            "SELECT effect_root_document_id,result_json FROM finance_liquidity_operations "
            "WHERE operation_id=?",
            (response["operation_id"],),
        ).fetchone()
    assert seal == (response["document_id"], expected_count)
    assert operation is not None and operation[0] == response["document_id"]


def assert_operation_replay(
    service: FinanceCashService, response: dict[str, Any]
) -> None:
    assert (
        service.get_operation(str(response["operation_id"]), ACTOR, False)
        == response
    )


def test_cardinality_and_terminal_receipts(db_path: Path) -> dict[str, Any]:
    bootstrap_finance_cash_store(db_path)
    service = FinanceCashService(db_path)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT schema_version FROM finance_liquidity_schema_meta"
        ).fetchone()[0] == FINANCE_CASH_SCHEMA_VERSION == 2

    source, source_opening = create_account(service, "Readiness source", "500.00")
    target, target_opening = create_account(service, "Readiness target", "0.00")
    assert_effect_receipt(db_path, source_opening, 1)
    assert_effect_receipt(db_path, target_opening, 0)
    assert_operation_replay(service, source_opening)
    assert_operation_replay(service, target_opening)

    key, operation_id = identities()
    income_category = str(
        service.create_category(
            {"name": "Readiness income", "direction": "income"},
            ACTOR,
            operation_id,
            key,
        )["category_id"]
    )
    key, operation_id = identities()
    expense_category = str(
        service.create_category(
            {
                "name": "Readiness expense",
                "direction": "expense",
                "posting_class": "external_outflow",
            },
            ACTOR,
            operation_id,
            key,
        )["category_id"]
    )

    income, _, _, _ = create_and_post(
        service,
        {
            "document_type": "income",
            "target_account_id": source,
            "category_id": income_category,
            "amount": "10.00",
            "occurred_at": "2026-09-21T09:00:00Z",
            "purpose": "synthetic receipt cardinality",
        },
    )
    expense, _, _, _ = create_and_post(
        service,
        {
            "document_type": "expense",
            "source_account_id": source,
            "category_id": expense_category,
            "amount": "5.00",
            "occurred_at": "2026-09-21T09:10:00Z",
            "purpose": "synthetic receipt cardinality",
        },
    )
    assert_effect_receipt(db_path, income, 1)
    assert_effect_receipt(db_path, expense, 1)

    sent, _, _, _ = create_and_post(
        service,
        {
            "document_type": "transfer",
            "source_account_id": source,
            "target_account_id": target,
            "amount": "20.00",
            "occurred_at": "2026-09-21T10:00:00Z",
            "transfer_mode": "two_phase",
        },
    )
    assert_effect_receipt(db_path, sent, 1)
    before_complete = service.get_document(str(sent["document_id"]))["document"]
    complete_key, complete_operation = identities()
    completed = service.transfer_transition(
        str(sent["document_id"]),
        "complete",
        {"base_revision": before_complete["revision"], "occurred_at": "2026-09-21T10:10:00Z"},
        ACTOR,
        complete_operation,
        complete_key,
    )
    assert_effect_receipt(db_path, completed, 1)
    assert (
        service.transfer_transition(
            str(sent["document_id"]),
            "complete",
            {"base_revision": before_complete["revision"], "occurred_at": "2026-09-21T10:10:00Z"},
            ACTOR,
            complete_operation,
            complete_key,
        )
        == completed
    )
    after_complete = service.get_document(str(sent["document_id"]))["document"]
    mutable_on_complete = {"transfer_state", "revision", "updated_at"}
    for field, value in before_complete.items():
        if field not in mutable_on_complete:
            assert after_complete[field] == value, field
    with sqlite3.connect(db_path) as conn:
        receipt = conn.execute(
            "SELECT o.scope,s.transaction_count,t.phase,ts.entry_count "
            "FROM finance_liquidity_operations o "
            "JOIN finance_liquidity_effect_set_seals s ON s.operation_id=o.operation_id "
            "JOIN finance_liquidity_ledger_transactions t ON t.origin_operation_id=o.operation_id "
            "JOIN finance_liquidity_ledger_transaction_seals ts ON ts.transaction_id=t.transaction_id "
            "WHERE o.operation_id=?",
            (complete_operation,),
        ).fetchone()
    assert receipt == (
        f"transfer.complete:{sent['document_id']}",
        1,
        "transfer_complete",
        2,
    )

    reverse_key, reverse_operation = identities()
    reversed_transfer = service.reverse_document(
        str(sent["document_id"]),
        {
            "base_revision": after_complete["revision"],
            "reason": "synthetic exact two-phase reversal",
            "occurred_at": "2026-09-21T10:20:00Z",
        },
        ACTOR,
        reverse_operation,
        reverse_key,
    )
    assert_effect_receipt(db_path, reversed_transfer, 2)
    assert_operation_replay(service, reversed_transfer)

    cancelled_send, _, _, _ = create_and_post(
        service,
        {
            "document_type": "transfer",
            "source_account_id": source,
            "target_account_id": target,
            "amount": "5.00",
            "occurred_at": "2026-09-21T11:00:00Z",
            "transfer_mode": "two_phase",
        },
    )
    cancelled_doc = service.get_document(str(cancelled_send["document_id"]))["document"]
    cancel_key, cancel_operation = identities()
    cancelled = service.transfer_transition(
        str(cancelled_send["document_id"]),
        "cancel",
        {"base_revision": cancelled_doc["revision"], "occurred_at": "2026-09-21T11:10:00Z"},
        ACTOR,
        cancel_operation,
        cancel_key,
    )
    assert_effect_receipt(db_path, cancelled, 1)

    pending_send, _, _, _ = create_and_post(
        service,
        {
            "document_type": "transfer",
            "source_account_id": source,
            "target_account_id": target,
            "amount": "1.00",
            "occurred_at": "2026-09-21T11:20:00Z",
            "transfer_mode": "two_phase",
        },
    )
    pending_doc = service.get_document(str(pending_send["document_id"]))["document"]
    transition_at = (
        datetime.fromisoformat(str(pending_doc["updated_at"]).replace("Z", "+00:00"))
        + timedelta(seconds=1)
    ).isoformat(timespec="microseconds").replace("+00:00", "Z")
    assert transition_at > str(pending_doc["updated_at"])
    assert transition_at >= str(pending_doc["occurred_at"])
    with sqlite3.connect(db_path) as conn:
        conn.create_function("finance_internal_write", 0, lambda: 1)
        conn.create_function("finance_internal_operation_id", 0, lambda: None)
        transition_sql = (
            "UPDATE finance_liquidity_documents SET transfer_state='completed',"
            "revision=revision+1,updated_at=? WHERE document_id=?"
        )
        transition_parameters = (
            transition_at,
            pending_send["document_id"],
        )
        expect_sql_rejection(
            lambda: conn.execute(transition_sql, transition_parameters)
        )
        # The genuine send receipt is sealed, but it is not the required
        # completion receipt and cannot authorize the terminal transition.
        conn.create_function(
            "finance_internal_operation_id",
            0,
            lambda: pending_send["operation_id"],
        )
        expect_sql_rejection(
            lambda: conn.execute(transition_sql, transition_parameters)
        )
        assert conn.execute(
            "SELECT transfer_state,revision FROM finance_liquidity_documents WHERE document_id=?",
            (pending_send["document_id"],),
        ).fetchone() == ("in_transit", pending_doc["revision"])

        trigger_sql = str(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='finance_documents_posted_legal_transition'"
            ).fetchone()[0]
        )
        for field in (
            "document_id",
            "document_type",
            "transfer_mode",
            "source_account_id",
            "target_account_id",
            "category_id",
            "amount_minor",
            "occurred_at",
            "purpose",
            "reversal_of_document_id",
            "replaces_opening_document_id",
            "negative_balance_explanation",
            "opening_evidence_type",
            "opening_evidence_digest",
            "opening_evidence_ref",
            "created_at",
            "posted_at",
            "semantic_digest",
            "actor",
        ):
            assert f"NEW.{field} IS OLD.{field}" in trigger_sql, field
        assert "finance_liquidity_effect_set_seals" in trigger_sql
        assert "finance_internal_operation_id()" in trigger_sql
        assert "s.transaction_count=(" in trigger_sql
        assert "LEFT JOIN finance_liquidity_ledger_transaction_seals" in trigger_sql
        assert "s.transaction_count=1" in trigger_sql
        assert "COUNT(*) FROM finance_liquidity_ledger_transactions exact_t" in trigger_sql
        assert ") IS NOT TRUE" in trigger_sql

        # A fully sealed, current cancel receipt must not let SQL three-valued
        # logic admit a NULL terminal state. Keep the synthetic receipt and
        # attempted transition in one transaction, then discard both.
        null_operation = f"operation-{uuid4().hex}"
        null_transaction = f"transaction-{uuid4().hex}"
        now = transition_at
        conn.rollback()
        conn.execute("BEGIN IMMEDIATE")
        conn.create_function(
            "finance_internal_operation_id", 0, lambda: null_operation
        )
        conn.execute(
            "INSERT INTO finance_liquidity_operations("
            "operation_id,scope,idempotency_key,request_digest,actor,"
            "effect_root_document_id,result_json,created_at"
            ") VALUES(?,?,?,?,?,NULL,'{}',?)",
            (
                null_operation,
                f"transfer.cancel:{pending_send['document_id']}",
                f"key-{uuid4().hex}",
                "synthetic-null-transition",
                ACTOR,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO finance_liquidity_ledger_transactions("
            "transaction_id,document_id,origin_operation_id,phase,effective_at"
            ") VALUES(?,?,?,?,?)",
            (
                null_transaction,
                pending_send["document_id"],
                null_operation,
                "transfer_cancel",
                now,
            ),
        )
        amount = int(pending_doc["amount_minor"])
        entries = (
            (f"asset:{pending_doc['source_account_id']}", "debit", amount),
            (f"system:transit:{pending_doc['currency']}", "credit", amount),
        )
        for line_no, (account, side, entry_amount) in enumerate(entries, 1):
            conn.execute(
                "INSERT INTO finance_liquidity_ledger_entries("
                "entry_id,transaction_id,line_no,ledger_account_id,side,amount_minor"
                ") VALUES(?,?,?,?,?,?)",
                (
                    f"entry-{uuid4().hex}",
                    null_transaction,
                    line_no,
                    account,
                    side,
                    entry_amount,
                ),
            )
        canonical_entries = [
            dict(zip(("line_no", "ledger_account_id", "side", "amount_minor"), row))
            for row in conn.execute(
                "SELECT line_no,ledger_account_id,side,amount_minor "
                "FROM finance_liquidity_ledger_entries WHERE transaction_id=? "
                "ORDER BY line_no",
                (null_transaction,),
            )
        ]
        conn.execute(
            "INSERT INTO finance_liquidity_ledger_transaction_seals("
            "transaction_id,entry_count,entries_digest,sealed_at"
            ") VALUES(?,?,?,?)",
            (
                null_transaction,
                2,
                _digest(canonical_entries),
                now,
            ),
        )
        conn.row_factory = sqlite3.Row
        null_document = service._document(conn, str(pending_send["document_id"]))
        conn.row_factory = None
        null_result = service._posted_response(
            conn,
            null_document,
            str(pending_send["document_id"]),
            [null_transaction],
            "cancelled",
        )
        null_result.update(
            {"operation_id": null_operation, "receipt_id": f"receipt-{uuid4().hex}"}
        )
        conn.execute(
            "UPDATE finance_liquidity_operations SET effect_root_document_id=?,"
            "result_json=? WHERE operation_id=?",
            (pending_send["document_id"], _canon(null_result), null_operation),
        )
        conn.execute(
            "INSERT INTO finance_liquidity_effect_set_seals("
            "operation_id,root_document_id,transaction_count,transactions_digest,sealed_at"
            ") VALUES(?,?,?,?,?)",
            (
                null_operation,
                pending_send["document_id"],
                1,
                _digest(
                    [
                        {
                            "transaction_id": null_transaction,
                            "document_id": pending_send["document_id"],
                            "phase": "transfer_cancel",
                            "effective_at": now,
                        }
                    ]
                ),
                now,
            ),
        )
        assert conn.execute(
            "SELECT o.scope,s.root_document_id,s.transaction_count,t.phase,ts.entry_count "
            "FROM finance_liquidity_operations o "
            "JOIN finance_liquidity_effect_set_seals s ON s.operation_id=o.operation_id "
            "JOIN finance_liquidity_ledger_transactions t "
            "ON t.origin_operation_id=o.operation_id "
            "JOIN finance_liquidity_ledger_transaction_seals ts "
            "ON ts.transaction_id=t.transaction_id WHERE o.operation_id=?",
            (null_operation,),
        ).fetchone() == (
            f"transfer.cancel:{pending_send['document_id']}",
            pending_send["document_id"],
            1,
            "transfer_cancel",
            2,
        )
        conn.execute("SAVEPOINT valid_cancel_transition")
        valid = conn.execute(
            "UPDATE finance_liquidity_documents SET transfer_state='cancelled',"
            "revision=revision+1,updated_at=? WHERE document_id=?",
            (now, pending_send["document_id"]),
        )
        assert valid.rowcount == 1
        assert conn.execute(
            "SELECT transfer_state,revision FROM finance_liquidity_documents "
            "WHERE document_id=?",
            (pending_send["document_id"],),
        ).fetchone() == ("cancelled", pending_doc["revision"] + 1)
        conn.execute("ROLLBACK TO valid_cancel_transition")
        conn.execute("RELEASE valid_cancel_transition")
        expect_sql_rejection(
            lambda: conn.execute(
                "UPDATE finance_liquidity_documents SET transfer_state=NULL,"
                "revision=revision+1,updated_at=? WHERE document_id=?",
                (now, pending_send["document_id"]),
            )
        )
        assert conn.execute(
            "SELECT transfer_state,revision FROM finance_liquidity_documents "
            "WHERE document_id=?",
            (pending_send["document_id"],),
        ).fetchone() == ("in_transit", pending_doc["revision"])
        conn.rollback()

    for old_amount, new_amount, expected_count in (
        ("0.00", "0.00", 0),
        ("0.00", "7.00", 1),
        ("8.00", "0.00", 1),
        ("9.00", "11.00", 2),
    ):
        account_id, old_opening = create_account(
            service, f"Replacement {old_amount} to {new_amount}", old_amount
        )
        old_doc = service.get_document(str(old_opening["document_id"]))["document"]
        key, operation_id = identities()
        replacement = service.replace_opening(
            str(old_opening["document_id"]),
            {
                "base_revision": old_doc["revision"],
                "amount": new_amount,
                "occurred_at": "2026-09-21T12:00:00Z",
                "opening_evidence_type": "manual_confirmation",
                "reason": "synthetic replacement cardinality",
            },
            ACTOR,
            operation_id,
            key,
        )
        assert_effect_receipt(db_path, replacement, expected_count)
        assert_operation_replay(service, replacement)
        assert service.get_account(account_id)["balance_minor"] == int(
            new_amount.replace(".", "")
        )

    # These public reads traverse the completed validator, including the exact
    # posted-document-to-receipt ownership set.
    assert service.list_documents()
    assert service.movements(source)["balance_state"] == "current"
    return {
        "service": service,
        "source": source,
        "target": target,
        "pending_send": pending_send,
    }


def test_reconciliation_sql_guards(
    db_path: Path, service: FinanceCashService, source: str, target: str
) -> None:
    source_amount = money_to_api(service.get_account(source)["balance_minor"], "RUB")
    target_amount = money_to_api(service.get_account(target)["balance_minor"], "RUB")

    def record(account_id: str, actual: str, comment: str) -> dict[str, Any]:
        key, operation_id = identities()
        return service.record_reconciliation(
            {
                "account_id": account_id,
                "week_ending": "2026-09-21",
                "actual_amount": actual,
                "comment": comment,
            },
            ACTOR,
            operation_id,
            key,
        )

    earlier_match = record(source, source_amount, "earlier matched observation")
    discrepancy = record(source, "0.00", "synthetic discrepancy")
    cross_match = record(target, target_amount, "other account match")
    later_match = record(source, source_amount, "later matched observation")
    discrepancy_id = str(discrepancy["reconciliation_id"])

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        template = conn.execute(
            "SELECT * FROM finance_liquidity_cash_reconciliations "
            "WHERE reconciliation_id=?",
            (discrepancy_id,),
        ).fetchone()
    assert template is not None
    wrong_record_key, wrong_record_operation = identities()

    def wrong_record_owner(conn: sqlite3.Connection) -> dict[str, Any]:
        conn.execute(
            "INSERT INTO finance_liquidity_cash_reconciliations("
            "reconciliation_id,account_id,week_ending,balance_as_of,"
            "business_timezone,ledger_watermark,ledger_digest,expected_minor,"
            "actual_minor,difference_minor,status,checked_at,comment,actor,"
            "created_at,revision,record_operation_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"wrong-owner-{uuid4().hex}",
                template["account_id"],
                template["week_ending"],
                template["balance_as_of"],
                template["business_timezone"],
                template["ledger_watermark"],
                template["ledger_digest"],
                template["expected_minor"],
                template["actual_minor"],
                template["difference_minor"],
                "discrepancy",
                "2026-09-21T17:59:59.000000Z",
                "wrong receipt owner",
                ACTOR,
                "2026-09-21T17:59:59.000000Z",
                1,
                wrong_record_operation,
            ),
        )
        return {"reconciliation_id": "unreachable"}

    expect_sql_rejection(
        lambda: service._command(
            "cash.reconciliation.record:wrong-account",
            wrong_record_key,
            wrong_record_operation,
            ACTOR,
            {},
            wrong_record_owner,
        )
    )

    def invalid_resolution(
        *,
        kind: str | None,
        link: str | None,
        scope_target: str | None = None,
    ) -> None:
        key, operation_id = identities()

        def action(conn: sqlite3.Connection) -> dict[str, Any]:
            conn.execute(
                "UPDATE finance_liquidity_cash_reconciliations SET "
                "status='resolved',revision=revision+1,resolution_operation_id=?,"
                "resolution_kind=?,resolution_reconciliation_id=?,resolution_reason=?,"
                "resolved_at=?,resolved_by=? WHERE reconciliation_id=?",
                (
                    operation_id,
                    kind,
                    link,
                    "synthetic constraint attempt",
                    "2026-09-21T18:00:00.000000Z",
                    ACTOR,
                    discrepancy_id,
                ),
            )
            return {"reconciliation_id": discrepancy_id, "status": "resolved"}

        expect_sql_rejection(
            lambda: service._command(
                f"cash.reconciliation.resolve:{scope_target or discrepancy_id}",
                key,
                operation_id,
                ACTOR,
                {},
                action,
            )
        )
        current = next(
            item
            for item in service.list_reconciliations(source)
            if item["reconciliation_id"] == discrepancy_id
        )
        assert current["status"] == "discrepancy"

    invalid_resolution(kind=None, link=None)
    invalid_resolution(kind="matched_followup", link=discrepancy_id)
    invalid_resolution(
        kind="matched_followup", link=str(cross_match["reconciliation_id"])
    )
    invalid_resolution(
        kind="matched_followup", link=str(earlier_match["reconciliation_id"])
    )
    invalid_resolution(
        kind="matched_followup",
        link=str(later_match["reconciliation_id"]),
        scope_target="another-reconciliation",
    )

    key, operation_id = identities()
    resolved = service.resolve_reconciliation(
        discrepancy_id,
        {
            "base_revision": 1,
            "matched_reconciliation_id": later_match["reconciliation_id"],
            "override_reason": "synthetic later match accepted",
        },
        ACTOR,
        False,
        operation_id,
        key,
    )
    assert resolved["resolution_kind"] == "matched_followup"

    override_target = record(source, "0.00", "admin override target")
    key, override_operation = identities()
    override = service.resolve_reconciliation(
        str(override_target["reconciliation_id"]),
        {"base_revision": 1, "override_reason": "synthetic admin decision"},
        ACTOR,
        True,
        override_operation,
        key,
    )
    assert override["resolution_kind"] == "admin_override"

    with sqlite3.connect(db_path) as conn:
        owned = conn.execute(
            "SELECT r.record_operation_id,record_op.scope,r.resolution_operation_id,"
            "resolve_op.scope,record_op.effect_root_document_id,"
            "resolve_op.effect_root_document_id "
            "FROM finance_liquidity_cash_reconciliations r "
            "JOIN finance_liquidity_operations record_op "
            "ON record_op.operation_id=r.record_operation_id "
            "JOIN finance_liquidity_operations resolve_op "
            "ON resolve_op.operation_id=r.resolution_operation_id "
            "WHERE r.reconciliation_id=?",
            (discrepancy_id,),
        ).fetchone()
        assert owned is not None
        assert owned[1] == f"cash.reconciliation.record:{source}"
        assert owned[3] == f"cash.reconciliation.resolve:{discrepancy_id}"
        assert owned[4:] == (None, None)
        assert conn.execute(
            "SELECT COUNT(*) FROM finance_liquidity_effect_set_seals "
            "WHERE operation_id IN (?,?)",
            (owned[0], owned[2]),
        ).fetchone()[0] == 0
        trigger_sql = str(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='finance_reconciliation_legal_transition'"
            ).fetchone()[0]
        )
        for fragment in (
            "NEW.resolution_kind IS NULL",
            "NEW.resolution_reconciliation_id=OLD.reconciliation_id",
            "follow.account_id=OLD.account_id",
            "follow.status='matched'",
            "follow.checked_at>OLD.checked_at",
            "o.scope='cash.reconciliation.resolve:'||OLD.reconciliation_id",
        ):
            assert fragment in trigger_sql, fragment


def test_schema_version_refusal(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE finance_liquidity_schema_meta SET schema_version=1")
    expect_finance_error(
        "finance_schema_unavailable", FinanceCashService(db_path).list_accounts
    )


def run_readiness_checks() -> None:
    with TemporaryDirectory(prefix="finance-readiness-") as directory:
        db_path = Path(directory) / "cash.sqlite3"
        context = test_cardinality_and_terminal_receipts(db_path)
        test_reconciliation_sql_guards(
            db_path,
            context["service"],
            str(context["source"]),
            str(context["target"]),
        )
        test_schema_version_refusal(db_path)


def main() -> None:
    run_readiness_checks()
    print("finance_liquidity_readiness_smoke: A1-A3 constraints OK")


if __name__ == "__main__":
    main()
