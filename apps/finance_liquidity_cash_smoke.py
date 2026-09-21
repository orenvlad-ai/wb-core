#!/usr/bin/env python3
"""Executable invariants for the isolated exact-money cash ledger."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from typing import Any, Callable
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.finance_liquidity_cash import (
    FinanceCashError,
    FinanceCashService,
    bootstrap_finance_cash_store,
)
from packages.domain.finance_liquidity import MoneyError, money_from_api, money_to_api


ACTOR = "cash-smoke"
OPENING_AT = "2026-09-21T10:00:00Z"


def command_id() -> tuple[str, str]:
    return (f"key-{uuid4().hex}", f"operation-{uuid4().hex}")


def expect_error(code: str, action: Callable[[], Any]) -> FinanceCashError:
    try:
        action()
    except FinanceCashError as error:
        if error.code != code:
            raise AssertionError((code, error.code)) from error
        return error
    raise AssertionError(f"expected {code}")


def create_account(
    service: FinanceCashService, name: str, currency: str = "RUB"
) -> str:
    key, operation_id = command_id()
    response = service.create_account(
        {
            "name": name,
            "account_type": "cash",
            "currency": currency,
            "responsible_name": "Fixture cashier",
        },
        ACTOR,
        operation_id,
        key,
    )
    return str(response["account_id"])


def create_and_post(
    service: FinanceCashService,
    payload: dict[str, Any],
    *,
    post_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    key, operation_id = command_id()
    draft = service.create_document(payload, ACTOR, operation_id, key)
    key, operation_id = command_id()
    return service.post_document(
        str(draft["document_id"]),
        {"base_revision": draft["revision"], **(post_payload or {})},
        ACTOR,
        operation_id,
        key,
    )


def open_account(
    service: FinanceCashService, account_id: str, amount: str
) -> dict[str, Any]:
    return create_and_post(
        service,
        {
            "document_type": "opening",
            "target_account_id": account_id,
            "amount": amount,
            "occurred_at": OPENING_AT,
            "opening_evidence_type": "manual_confirmation",
            "opening_evidence_ref": "fixture opening confirmation",
        },
    )


def test_money_boundaries() -> None:
    assert money_from_api("90000000000000.00", "RUB") == 9_000_000_000_000_000
    assert money_from_api("1", "JPY") == 1
    assert money_to_api(123, "JPY") == "123"
    for invalid in (
        1.0,
        "1.001",
        "1.000000000000000000000000000001",
        "1e2",
        "90000000000000.0000000000000000001",
        "90000000000000.01",
    ):
        try:
            money_from_api(invalid, "RUB")
        except MoneyError:
            pass
        else:
            raise AssertionError(invalid)


def test_cash_ledger(db_path: Path) -> None:
    unavailable = FinanceCashService(db_path)
    expect_error("finance_unavailable", unavailable.list_accounts)
    bootstrap_finance_cash_store(db_path)
    expect_error("store_already_exists", lambda: bootstrap_finance_cash_store(db_path))
    service = FinanceCashService(db_path)
    source = create_account(service, "Cash desk")
    target = create_account(service, "Reserve cash")
    yen = create_account(service, "JPY fixed scale", "JPY")
    assert service.get_account(yen)["currency_exponent"] == 0
    assert service.get_account(source)["balance_state"] == "uninitialized"

    opening = open_account(service, source, "100.00")
    opening_id = str(opening["document_id"])
    zero_opening = open_account(service, target, "0.00")
    assert (
        service.movements(source, "2026-09-21T09:59:59Z")["balance_state"]
        == "uninitialized"
    )

    category_key, category_operation = command_id()
    category = service.create_category(
        {"name": "Fixture expenses", "direction": "expense"},
        ACTOR,
        category_operation,
        category_key,
    )
    category_id = str(category["category_id"])
    income_key, income_operation = command_id()
    income_category_id = str(
        service.create_category(
            {"name": "Fixture income", "direction": "income"},
            ACTOR,
            income_operation,
            income_key,
        )["category_id"]
    )

    # A document remains a draft until post; idempotent replay returns its receipt.
    draft_key, draft_operation = command_id()
    draft_payload = {
        "document_type": "income",
        "target_account_id": source,
        "category_id": income_category_id,
        "amount": "20.00",
        "occurred_at": "2026-09-21T11:00:00Z",
        "purpose": "fixture income",
    }
    draft = service.create_document(draft_payload, ACTOR, draft_operation, draft_key)
    assert (
        service.create_document(draft_payload, ACTOR, draft_operation, draft_key)
        == draft
    )
    expect_error(
        "idempotency_conflict",
        lambda: service.create_document(
            {**draft_payload, "purpose": "changed"}, ACTOR, command_id()[1], draft_key
        ),
    )
    document_id = str(draft["document_id"])
    post_key, post_operation = command_id()
    posted = service.post_document(
        document_id,
        {"base_revision": draft["revision"]},
        ACTOR,
        post_operation,
        post_key,
    )
    assert (
        service.post_document(
            document_id,
            {"base_revision": draft["revision"]},
            ACTOR,
            post_operation,
            post_key,
        )
        == posted
    )

    # Two distinct commands racing on one draft have one winner only.
    race_key, race_operation = command_id()
    race_draft = service.create_document(
        {
            "document_type": "income",
            "target_account_id": source,
            "category_id": income_category_id,
            "amount": "1.00",
            "occurred_at": "2026-09-21T11:01:00Z",
            "purpose": "post race",
        },
        ACTOR,
        race_operation,
        race_key,
    )

    def post_race() -> str:
        key, operation_id = command_id()
        try:
            service.post_document(
                str(race_draft["document_id"]),
                {"base_revision": race_draft["revision"]},
                ACTOR,
                operation_id,
                key,
            )
            return "posted"
        except FinanceCashError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        race_results = list(pool.map(lambda _: post_race(), range(2)))
    assert race_results.count("posted") == 1, race_results

    # A trigger simulates a fault after transfer command admission. SQLite rolls
    # back both sides and leaves the draft available for a later retry.
    transfer_key, transfer_operation = command_id()
    transfer_draft = service.create_document(
        {
            "document_type": "transfer",
            "source_account_id": source,
            "target_account_id": target,
            "amount": "10.00",
            "occurred_at": "2026-09-21T12:00:00Z",
            "transfer_mode": "instant",
        },
        ACTOR,
        transfer_operation,
        transfer_key,
    )
    before = service.get_account(source)["balance_minor"]
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TRIGGER fixture_reject_transfer BEFORE INSERT ON "
            "finance_liquidity_ledger_entries WHEN NEW.ledger_account_id="
            f"'asset:{target}' BEGIN SELECT RAISE(ABORT, 'fixture transfer fault'); END"
        )
    try:
        key, operation_id = command_id()
        service.post_document(
            str(transfer_draft["document_id"]),
            {"base_revision": transfer_draft["revision"]},
            ACTOR,
            operation_id,
            key,
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("fixture fault did not fire")
    assert service.get_account(source)["balance_minor"] == before
    assert (
        service.get_document(str(transfer_draft["document_id"]))["document"]["status"]
        == "draft"
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TRIGGER fixture_reject_transfer")

    # A direct late entry cannot change a sealed transaction.
    transaction_id = str(posted["ledger_transaction_ids"][0])
    with sqlite3.connect(db_path) as conn:
        try:
            conn.execute(
                "INSERT INTO finance_liquidity_ledger_entries("
                "entry_id,transaction_id,line_no,ledger_account_id,side,amount_minor) "
                "VALUES(?,?,?,?,?,?)",
                ("fixture-late", transaction_id, 99, f"asset:{source}", "debit", 1),
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("sealed transaction accepted a late entry")

        immutable_sql = (
            ("UPDATE finance_liquidity_documents SET status='draft' WHERE document_id=?", (document_id,)),
            ("UPDATE finance_liquidity_ledger_transactions SET phase='tampered' WHERE transaction_id=?", (transaction_id,)),
            ("UPDATE finance_liquidity_ledger_transaction_seals SET entry_count=99 WHERE transaction_id=?", (transaction_id,)),
            ("DELETE FROM finance_liquidity_effect_set_seals WHERE operation_id=?", (post_operation,)),
            ("UPDATE finance_liquidity_operations SET actor='tampered' WHERE operation_id=?", (post_operation,)),
            ("DELETE FROM finance_liquidity_opening_anchors WHERE account_id=?", (source,)),
        )
        for statement, parameters in immutable_sql:
            try:
                conn.execute(statement, parameters)
            except sqlite3.DatabaseError:
                pass
            else:
                raise AssertionError(f"immutable fact accepted: {statement}")

    # Expenses that make cash negative require a human explanation and expose a warning.
    missing_reason_key, missing_reason_operation = command_id()
    missing_reason = service.create_document(
        {
            "document_type": "expense",
            "source_account_id": source,
            "category_id": category_id,
            "amount": "200.00",
            "occurred_at": "2026-09-21T13:00:00Z",
            "purpose": "personal advance requires review",
        },
        ACTOR,
        missing_reason_operation,
        missing_reason_key,
    )
    expect_error(
        "negative_balance_explanation_required",
        lambda: service.post_document(
            str(missing_reason["document_id"]),
            {"base_revision": missing_reason["revision"]},
            ACTOR,
            *reversed(command_id()),
        ),
    )
    negative = create_and_post(
        service,
        {
            "document_type": "expense",
            "source_account_id": source,
            "category_id": category_id,
            "amount": "200.00",
            "occurred_at": "2026-09-21T13:00:00Z",
            "purpose": "personal advance requires review",
            "negative_balance_explanation": "personal advance; manager review required",
        },
    )
    # The missing explanation attempt was rejected before any money changed.
    assert negative["balances"][source]["negative_balance_warning"] is True
    assert "reimbursement" not in str(negative)  # no second cash fact
    reverse_key, reverse_operation = command_id()
    service.reverse_document(
        str(negative["document_id"]),
        {
            "base_revision": service.get_document(str(negative["document_id"]))["document"]["revision"],
            "reason": "fixture correction",
            "occurred_at": "2026-09-21T14:00:00Z",
        },
        ACTOR,
        reverse_operation,
        reverse_key,
    )

    # Two-phase transit conserves money and only one concurrent terminal action wins.
    transit = create_and_post(
        service,
        {
            "document_type": "transfer",
            "source_account_id": source,
            "target_account_id": target,
            "amount": "5.00",
            "occurred_at": "2026-09-21T15:00:00Z",
            "transfer_mode": "two_phase",
        },
    )
    transit_document = str(transit["document_id"])
    revision = service.get_document(transit_document)["document"]["revision"]

    def transition(action: str) -> str:
        key, operation_id = command_id()
        try:
            service.transfer_transition(
                transit_document,
                action,
                {"base_revision": revision, "occurred_at": "2026-09-21T16:00:00Z"},
                ACTOR,
                operation_id,
                key,
            )
            return action
        except FinanceCashError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        transitions = list(pool.map(transition, ("complete", "cancel")))
    assert sum(result in {"complete", "cancel"} for result in transitions) == 1, (
        transitions
    )

    # Reconciliation is an immutable dated observation, never a hidden adjustment.
    current = service.get_account(source)["balance_minor"]
    reconciliation_key, reconciliation_operation = command_id()
    reconciliation = service.record_reconciliation(
        {
            "account_id": source,
            "week_ending": "2026-09-21",
            "actual_amount": "0.00",
            "comment": "fixture count discrepancy",
        },
        ACTOR,
        reconciliation_operation,
        reconciliation_key,
    )
    assert reconciliation["difference_minor"] != 0
    assert service.get_account(source)["balance_minor"] == current
    snapshot = service.list_reconciliations(source)[0]
    create_and_post(
        service,
        {
            "document_type": "income",
            "target_account_id": source,
            "category_id": income_category_id,
            "amount": "3.00",
            "occurred_at": "2026-09-22T10:00:00Z",
            "purpose": "after selected business day",
        },
    )
    preserved = service.list_reconciliations(source)[0]
    assert (
        preserved["expected_minor"],
        preserved["ledger_digest"],
        preserved["balance_as_of"],
    ) == (
        snapshot["expected_minor"],
        snapshot["ledger_digest"],
        snapshot["balance_as_of"],
    )

    # Replacement retains the original cutover, and no movement may precede it.
    old = service.get_document(opening_id)["document"]
    replacement_key, replacement_operation = command_id()
    source_replacement = service.replace_opening(
        opening_id,
        {
            "base_revision": old["revision"],
            "amount": "110.00",
            "occurred_at": "2026-09-21T18:00:00Z",
            "opening_evidence_type": "manual_confirmation",
            "reason": "fixture correction",
        },
        ACTOR,
        replacement_operation,
        replacement_key,
    )
    # A linked reversal is historical evidence, not a new opening candidate.
    # Replacing it must reject before any operation/effect can be committed.
    reversal_document_id = str(source_replacement["reversal_document_id"])
    reversal_doc = service.get_document(reversal_document_id)["document"]
    source_before_bad_replacement = service.get_account(source)["balance_minor"]
    with sqlite3.connect(db_path) as conn:
        tx_count_before_bad_replacement = conn.execute(
            "SELECT COUNT(*) FROM finance_liquidity_ledger_transactions"
        ).fetchone()[0]
    expect_error(
        "opening_not_replaceable",
        lambda: service.replace_opening(
            reversal_document_id,
            {
                "base_revision": reversal_doc["revision"],
                "amount": "1.00",
                "occurred_at": "2026-09-21T18:00:30Z",
                "opening_evidence_type": "manual_confirmation",
                "reason": "must reject correction document",
            },
            ACTOR,
            *reversed(command_id()),
        ),
    )
    assert service.get_account(source)["balance_minor"] == source_before_bad_replacement
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM finance_liquidity_ledger_transactions").fetchone()[0] == tx_count_before_bad_replacement
    # Both zero-opening corrections and a nonzero-to-zero correction retain a
    # durable root/effect receipt.  The former has an exact empty set; the
    # latter seals only the linked reversal transaction.
    zero_old = service.get_document(str(zero_opening["document_id"]))["document"]
    zero_replacement = service.replace_opening(
        str(zero_opening["document_id"]),
        {
            "base_revision": zero_old["revision"],
            "amount": "0.00",
            "occurred_at": "2026-09-21T18:01:00Z",
            "opening_evidence_type": "manual_confirmation",
            "reason": "zero opening confirmation correction",
        },
        ACTOR,
        *reversed(command_id()),
    )
    assert zero_replacement["ledger_transaction_ids"] == []
    with sqlite3.connect(db_path) as conn:
        zero_seal = conn.execute(
            "SELECT transaction_count,transactions_digest FROM finance_liquidity_effect_set_seals WHERE operation_id=?",
            (zero_replacement["operation_id"],),
        ).fetchone()
    assert zero_seal and zero_seal[0] == 0
    assert service.get_operation(str(zero_replacement["operation_id"]), ACTOR, False)["document_id"] == zero_replacement["document_id"]
    nonzero_old = service.get_document(str(source_replacement["document_id"]))["document"]
    nonzero_to_zero = service.replace_opening(
        str(source_replacement["document_id"]),
        {
            "base_revision": nonzero_old["revision"],
            "amount": "0.00",
            "occurred_at": "2026-09-21T18:02:00Z",
            "opening_evidence_type": "manual_confirmation",
            "reason": "zero corrected opening",
        },
        ACTOR,
        *reversed(command_id()),
    )
    assert len(nonzero_to_zero["ledger_transaction_ids"]) == 1
    with sqlite3.connect(db_path) as conn:
        cutover = conn.execute(
            "SELECT cutover_at FROM finance_liquidity_opening_anchors WHERE account_id=?",
            (source,),
        ).fetchone()[0]
    assert cutover == "2026-09-21T10:00:00.000000Z"
    late_key, late_operation = command_id()
    late = service.create_document(
        {
            "document_type": "income",
            "target_account_id": source,
            "category_id": income_category_id,
            "amount": "1.00",
            "occurred_at": "2026-09-21T09:00:00Z",
            "purpose": "too early",
        },
        ACTOR,
        late_operation,
        late_key,
    )
    expect_error(
        "backdated_before_cutover",
        lambda: service.post_document(
            str(late["document_id"]),
            {"base_revision": late["revision"]},
            ACTOR,
            *reversed(command_id()),
        ),
    )

    # Stored and read query instants use one fixed microsecond UTC form.  A
    # whole-second public as_of cannot sort after a later fractional opening.
    timestamp_account = create_account(service, "Timestamp boundary cash")
    timestamp_opening = create_and_post(
        service,
        {
            "document_type": "opening",
            "target_account_id": timestamp_account,
            "amount": "1.00",
            "occurred_at": "2026-09-23T00:00:00.900000Z",
            "opening_evidence_type": "manual_confirmation",
            "opening_evidence_ref": "timestamp fixture",
        },
    )
    assert timestamp_opening["balances"][timestamp_account]["balance_minor"] == 100
    assert service.movements(timestamp_account, "2026-09-23T00:00:00Z")["balance_state"] == "uninitialized"
    timestamp_draft = service.create_document(
        {
            "document_type": "income",
            "target_account_id": timestamp_account,
            "category_id": income_category_id,
            "amount": "1.00",
            "occurred_at": "2026-09-23T00:00:00Z",
            "purpose": "must not precede opening",
        },
        ACTOR,
        *reversed(command_id()),
    )
    expect_error(
        "backdated_before_cutover",
        lambda: service.post_document(
            str(timestamp_draft["document_id"]),
            {"base_revision": timestamp_draft["revision"]},
            ACTOR,
            *reversed(command_id()),
        ),
    )

    # Every monetary read recomputes its complete durable chain.  Simulate the
    # historical direct-SQL bypass: a balanced fake transaction with fake seals
    # and no durable Operation must make the projection fail closed, never add
    # money to a visible balance.
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO finance_liquidity_ledger_transactions(transaction_id,document_id,origin_operation_id,phase,effective_at) VALUES(?,?,?,?,?)",
            ("fixture-evil-tx", opening_id, "fixture-evil-op", "primary", "2026-09-22T12:00:00.000000Z"),
        )
        for line, side, account in (
            (1, "debit", f"asset:{source}"),
            (2, "credit", "system:fixture"),
        ):
            conn.execute(
                "INSERT INTO finance_liquidity_ledger_entries(entry_id,transaction_id,line_no,ledger_account_id,side,amount_minor) VALUES(?,?,?,?,?,?)",
                (f"fixture-evil-entry-{line}", "fixture-evil-tx", line, account, side, 500),
            )
        conn.execute(
            "INSERT INTO finance_liquidity_ledger_transaction_seals(transaction_id,entry_count,entries_digest,sealed_at) VALUES(?,?,?,?)",
            ("fixture-evil-tx", 99, "sha256:forged", "2026-09-22T12:00:00.000000Z"),
        )
        conn.execute(
            "INSERT INTO finance_liquidity_effect_set_seals(operation_id,root_document_id,transaction_count,transactions_digest,sealed_at) VALUES(?,?,?,?,?)",
            ("fixture-evil-op", opening_id, 77, "sha256:forged", "2026-09-22T12:00:00.000000Z"),
        )
    expect_error("ledger_integrity_unavailable", lambda: service.get_account(source))
    expect_error(
        "ledger_integrity_unavailable",
        lambda: service.get_operation(str(posted["operation_id"]), ACTOR, False),
    )
    expect_error(
        "ledger_integrity_unavailable",
        lambda: service.post_document(
            document_id,
            {"base_revision": draft["revision"]},
            ACTOR,
            post_operation,
            post_key,
        ),
    )


def main() -> None:
    test_money_boundaries()
    with TemporaryDirectory() as directory:
        test_cash_ledger(Path(directory) / "cash.sqlite3")
    print("finance_liquidity_cash_smoke: ok")


if __name__ == "__main__":
    main()
