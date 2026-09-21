#!/usr/bin/env python3
"""Executable invariants for the isolated exact-money cash ledger."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from threading import Barrier
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
from apps.finance_liquidity_readiness_smoke import run_readiness_checks


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
        " 1.00",
        "1.00 ",
        "\t1.00",
        "1.00\n",
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


def test_duplicate_and_reconciliation_regressions(db_path: Path) -> None:
    """Service-level D03 §11.5 and reconciliation contract regressions."""
    bootstrap_finance_cash_store(db_path)
    service = FinanceCashService(db_path)
    source = create_account(service, "Regression cash A")
    target = create_account(service, "Regression cash B")
    limit_ok = create_account(service, "Regression limit accepted")
    limit_negative = create_account(service, "Regression limit negative accepted")
    limit_over = create_account(service, "Regression limit rejected")
    open_account(service, source, "100.00")
    open_account(service, target, "0.00")
    open_account(service, limit_ok, "-90000000000000.00")
    open_account(service, limit_negative, "90000000000000.00")
    open_account(service, limit_over, "-90000000000000.00")
    key, operation_id = command_id()
    income_category = str(
        service.create_category(
            {"name": "Regression income", "direction": "income"},
            ACTOR,
            operation_id,
            key,
        )["category_id"]
    )
    key, operation_id = command_id()
    alternate_income_category = str(
        service.create_category(
            {"name": "Regression income alternate", "direction": "income"},
            ACTOR,
            operation_id,
            key,
        )["category_id"]
    )

    income_payload = {
        "document_type": "income",
        "target_account_id": source,
        "category_id": income_category,
        "amount": "10.00",
        "occurred_at": "2026-09-21T11:00:00Z",
        "purpose": "first manual income",
    }
    original = create_and_post(service, income_payload)
    duplicate_payload = {
        **income_payload,
        "category_id": alternate_income_category,
        "purpose": "same payment, explicit decision",
    }
    key, operation_id = command_id()
    duplicate = service.create_document(duplicate_payload, ACTOR, operation_id, key)
    warning_document = service.get_document(str(duplicate["document_id"]))["document"]
    warning_balance = service.get_account(source)["balance_minor"]
    post_key, post_operation = command_id()
    first_required = expect_error(
        "duplicate_confirmation_required",
        lambda: service.post_document(
            str(duplicate["document_id"]),
            {"base_revision": duplicate["revision"]},
            ACTOR,
            post_operation,
            post_key,
        ),
    )
    replay_required = expect_error(
        "duplicate_confirmation_required",
        lambda: service.post_document(
            str(duplicate["document_id"]),
            {"base_revision": duplicate["revision"]},
            ACTOR,
            post_operation,
            post_key,
        ),
    )
    token = str(first_required.data["duplicate_confirmation_token"])
    assert token == replay_required.data["duplicate_confirmation_token"]
    assert first_required.data["candidates"] == replay_required.data["candidates"]
    action_required = service.get_operation(post_operation, ACTOR, False)
    assert action_required["action_required"] == "duplicate_confirmation"
    assert action_required["duplicate_confirmation_token"] == token
    assert action_required["candidates"] == first_required.data["candidates"]
    assert service.get_document(str(duplicate["document_id"]))["document"] == warning_document
    assert service.get_account(source)["balance_minor"] == warning_balance

    # A similar, explicitly confirmed manual posting changes both the ordered
    # candidate set and the sealed ledger watermark.  Its successful exact
    # replay is an idempotent receipt, while the earlier warning token is stale.
    key, operation_id = command_id()
    changed_candidate = service.create_document(
        {**income_payload, "purpose": "candidate set drift"}, ACTOR, operation_id, key
    )
    change_key, change_operation = command_id()
    changed_required = expect_error(
        "duplicate_confirmation_required",
        lambda: service.post_document(
            str(changed_candidate["document_id"]),
            {"base_revision": changed_candidate["revision"]},
            ACTOR,
            change_operation,
            change_key,
        ),
    )
    confirmation_key, confirmation_operation = command_id()
    confirmed = service.post_document(
        str(changed_candidate["document_id"]),
        {
            "base_revision": changed_candidate["revision"],
            "duplicate_confirmation_token": changed_required.data["duplicate_confirmation_token"],
        },
        ACTOR,
        confirmation_operation,
        confirmation_key,
    )
    confirmed_replay = service.post_document(
        str(changed_candidate["document_id"]),
        {
            "base_revision": changed_candidate["revision"],
            "duplicate_confirmation_token": changed_required.data["duplicate_confirmation_token"],
        },
        ACTOR,
        confirmation_operation,
        confirmation_key,
    )
    assert confirmed_replay == confirmed
    candidate_stale_balance = service.get_account(source)["balance_minor"]
    stale_candidate = expect_error(
        "duplicate_confirmation_stale",
        lambda: service.post_document(
            str(duplicate["document_id"]),
            {"base_revision": duplicate["revision"], "duplicate_confirmation_token": token},
            ACTOR,
            *reversed(command_id()),
        ),
    )
    assert stale_candidate.data == {}
    assert service.get_document(str(duplicate["document_id"]))["document"] == warning_document
    assert service.get_account(source)["balance_minor"] == candidate_stale_balance

    # A nonmatching ordinary posting leaves the candidate set unchanged but
    # advances the global ledger watermark, so it independently stales a token.
    key, operation_id = command_id()
    watermark_draft = service.create_document(
        {**income_payload, "purpose": "watermark only confirmation"}, ACTOR, operation_id, key
    )
    watermark_key, watermark_operation = command_id()
    watermark_required = expect_error(
        "duplicate_confirmation_required",
        lambda: service.post_document(
            str(watermark_draft["document_id"]),
            {"base_revision": watermark_draft["revision"]},
            ACTOR,
            watermark_operation,
            watermark_key,
        ),
    )
    assert any(
        item["document_id"] == changed_candidate["document_id"]
        for item in watermark_required.data["candidates"]
    )
    assert watermark_required.data["candidates"] != first_required.data["candidates"]
    create_and_post(
        service, {**income_payload, "amount": "7.00", "purpose": "watermark-only drift"}
    )
    watermark_document = service.get_document(str(watermark_draft["document_id"]))["document"]
    watermark_balance = service.get_account(source)["balance_minor"]
    expect_error(
        "duplicate_confirmation_stale",
        lambda: service.post_document(
            str(watermark_draft["document_id"]),
            {
                "base_revision": watermark_draft["revision"],
                "duplicate_confirmation_token": watermark_required.data["duplicate_confirmation_token"],
            },
            ACTOR,
            *reversed(command_id()),
        ),
    )
    assert service.get_document(str(watermark_draft["document_id"]))["document"] == watermark_document
    assert service.get_account(source)["balance_minor"] == watermark_balance

    original_doc = service.get_document(str(original["document_id"]))["document"]
    reversed_income = service.reverse_document(
        str(original["document_id"]),
        {
            "base_revision": original_doc["revision"],
            "reason": "regression reversal",
            "occurred_at": "2026-09-21T11:10:00Z",
        },
        ACTOR,
        *reversed(command_id()),
    )
    key, operation_id = command_id()
    reversed_candidate_draft = service.create_document(
        {**income_payload, "purpose": "manual candidate after reversal"}, ACTOR, operation_id, key
    )
    reversed_candidate_error = expect_error(
        "duplicate_confirmation_required",
        lambda: service.post_document(
            str(reversed_candidate_draft["document_id"]),
            {"base_revision": reversed_candidate_draft["revision"]},
            ACTOR,
            *reversed(command_id()),
        ),
    )
    candidates = reversed_candidate_error.data["candidates"]
    assert any(item["document_id"] == original["document_id"] and item["status"] == "reversed" for item in candidates)
    assert all(item["document_id"] != reversed_income["document_id"] for item in candidates)

    transfer_payload = {
        "document_type": "transfer",
        "source_account_id": source,
        "target_account_id": target,
        "amount": "5.00",
        "occurred_at": "2026-09-21T12:00:00Z",
        "transfer_mode": "instant",
        "purpose": "first manual transfer",
    }
    transfer = create_and_post(service, transfer_payload)
    key, operation_id = command_id()
    transfer_duplicate = service.create_document(
        {**transfer_payload, "purpose": "same transfer explicit decision"}, ACTOR, operation_id, key
    )
    transfer_required = expect_error(
        "duplicate_confirmation_required",
        lambda: service.post_document(
            str(transfer_duplicate["document_id"]),
            {"base_revision": transfer_duplicate["revision"]},
            ACTOR,
            *reversed(command_id()),
        ),
    )
    assert any(item["document_id"] == transfer["document_id"] for item in transfer_required.data["candidates"])

    # EKT business-day bounds are UTC 19:00 to 19:00.  This pair crosses a UTC
    # date but is the same EKT day and therefore warns; the following midnight
    # boundary is a new business day and posts without a warning.
    ekt_edge_payload = {
        **income_payload,
        "amount": "13.00",
        "occurred_at": "2026-09-21T19:00:00Z",
        "purpose": "EKT day start",
    }
    ekt_edge = create_and_post(service, ekt_edge_payload)
    key, operation_id = command_id()
    ekt_same_day = service.create_document(
        {**ekt_edge_payload, "occurred_at": "2026-09-22T18:59:59Z", "purpose": "EKT day end"},
        ACTOR,
        operation_id,
        key,
    )
    ekt_same_day_required = expect_error(
        "duplicate_confirmation_required",
        lambda: service.post_document(
            str(ekt_same_day["document_id"]),
            {"base_revision": ekt_same_day["revision"]},
            ACTOR,
            *reversed(command_id()),
        ),
    )
    assert any(
        item["document_id"] == ekt_edge["document_id"]
        for item in ekt_same_day_required.data["candidates"]
    )
    create_and_post(
        service,
        {**ekt_edge_payload, "occurred_at": "2026-09-22T19:00:00Z", "purpose": "next EKT day"},
    )

    # The candidate query itself is capped and ordered: only the twenty most
    # recent ledger effects are returned when there are twenty-one matches.
    capped_payload = {
        **income_payload,
        "amount": "17.00",
        "occurred_at": "2026-09-21T13:00:00Z",
        "purpose": "cap original",
    }
    create_and_post(service, capped_payload)
    confirmed_candidate_ids: list[str] = []
    for index in range(20):
        key, operation_id = command_id()
        candidate = service.create_document(
            {**capped_payload, "purpose": f"cap candidate {index}"},
            ACTOR,
            operation_id,
            key,
        )
        key, operation_id = command_id()
        required = expect_error(
            "duplicate_confirmation_required",
            lambda candidate=candidate, key=key, operation_id=operation_id: service.post_document(
                str(candidate["document_id"]),
                {"base_revision": candidate["revision"]},
                ACTOR,
                operation_id,
                key,
            ),
        )
        confirmed_candidate_ids.append(
            str(
                service.post_document(
                    str(candidate["document_id"]),
                    {
                        "base_revision": candidate["revision"],
                        "duplicate_confirmation_token": required.data["duplicate_confirmation_token"],
                    },
                    ACTOR,
                    *reversed(command_id()),
                )["document_id"]
            )
        )
    key, operation_id = command_id()
    capped_draft = service.create_document(
        {**capped_payload, "purpose": "cap assertion"}, ACTOR, operation_id, key
    )
    capped_required = expect_error(
        "duplicate_confirmation_required",
        lambda: service.post_document(
            str(capped_draft["document_id"]),
            {"base_revision": capped_draft["revision"]},
            ACTOR,
            *reversed(command_id()),
        ),
    )
    assert [item["document_id"] for item in capped_required.data["candidates"]] == list(
        reversed(confirmed_candidate_ids)
    )

    def record(account_id: str, actual: str, *, comment: str | None = None, checked_at: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "account_id": account_id,
            "week_ending": "2026-09-21",
            "actual_amount": actual,
        }
        if comment:
            payload["comment"] = comment
        if checked_at:
            payload["checked_at"] = checked_at
        return service.record_reconciliation(payload, ACTOR, *reversed(command_id()))

    expect_error(
        "reconciliation_explanation_required",
        lambda: record(source, "0.00"),
    )
    checked_before = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )
    first_reconciliation = record(
        source,
        "0.00",
        comment="source discrepancy",
        checked_at="2000-01-01T00:00:00Z",
    )
    checked_after = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )
    assert (
        checked_before
        <= first_reconciliation["checked_at"]
        <= checked_after
        and first_reconciliation["checked_at"] != "2000-01-01T00:00:00Z"
    )
    second_reconciliation = record(target, "1.00", comment="target discrepancy")
    first_revision = service.list_reconciliations(source)[0]["revision"]
    second_revision = service.list_reconciliations(target)[0]["revision"]
    # Same key is safe across distinct aggregate routes; both target records
    # resolve rather than replaying the first receipt.
    shared_key = "reconciliation-cross-target-key"
    first_resolve = service.resolve_reconciliation(
        str(first_reconciliation["reconciliation_id"]),
        {"base_revision": first_revision, "override_reason": "count corrected"},
        ACTOR,
        True,
        "reconciliation-cross-target-op-a",
        shared_key,
    )
    second_resolve = service.resolve_reconciliation(
        str(second_reconciliation["reconciliation_id"]),
        {"base_revision": second_revision, "override_reason": "count corrected"},
        ACTOR,
        True,
        "reconciliation-cross-target-op-b",
        shared_key,
    )
    assert first_resolve["reconciliation_id"] != second_resolve["reconciliation_id"]
    source_resolved = next(
        item
        for item in service.list_reconciliations(source)
        if item["reconciliation_id"] == first_reconciliation["reconciliation_id"]
    )
    target_resolved = next(
        item
        for item in service.list_reconciliations(target)
        if item["reconciliation_id"] == second_reconciliation["reconciliation_id"]
    )
    assert source_resolved["status"] == target_resolved["status"] == "resolved"

    raced = record(source, "0.00", comment="concurrent resolution")
    raced_revision = service.list_reconciliations(source)[0]["revision"]
    expect_error(
        "version_conflict",
        lambda: service.resolve_reconciliation(
            str(raced["reconciliation_id"]),
            {"base_revision": raced_revision - 1, "override_reason": "stale CAS"},
            ACTOR,
            True,
            "reconciliation-stale-op",
            "reconciliation-stale-key",
        ),
    )
    expect_error(
        "invalid_reconciliation_resolution",
        lambda: service.resolve_reconciliation(
            str(raced["reconciliation_id"]),
            {"base_revision": raced_revision},
            ACTOR,
            True,
            "reconciliation-missing-reason-op",
            "reconciliation-missing-reason-key",
        ),
    )

    race_start = Barrier(2)

    def resolve_race(index: int) -> str:
        try:
            race_start.wait()
            service.resolve_reconciliation(
                str(raced["reconciliation_id"]),
                {"base_revision": raced_revision, "override_reason": f"race {index}"},
                ACTOR,
                True,
                f"reconciliation-race-op-{index}",
                f"reconciliation-race-key-{index}",
            )
            return "resolved"
        except FinanceCashError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        race_results = list(pool.map(resolve_race, range(2)))
    assert race_results.count("resolved") == 1 and any(
        result in {"version_conflict", "reconciliation_not_resolvable"}
        for result in race_results
    ), race_results
    raced_after = next(
        item
        for item in service.list_reconciliations(source)
        if item["reconciliation_id"] == raced["reconciliation_id"]
    )
    assert raced_after["status"] == "resolved"
    assert raced_after["revision"] == raced_revision + 1

    matched_followup = record(source, "0.00", comment="follow-up discrepancy")
    matched_observation = record(
        source,
        money_to_api(int(matched_followup["expected_minor"]), "RUB"),
    )
    matched_rows = service.list_reconciliations(source)
    matched_revision = next(
        item["revision"]
        for item in matched_rows
        if item["reconciliation_id"] == matched_followup["reconciliation_id"]
    )
    assert next(
        item["status"]
        for item in matched_rows
        if item["reconciliation_id"] == matched_observation["reconciliation_id"]
    ) == "matched"
    expect_error(
        "invalid_reconciliation_resolution",
        lambda: service.resolve_reconciliation(
            str(matched_followup["reconciliation_id"]),
            {
                "base_revision": matched_revision,
                "matched_reconciliation_id": matched_observation["reconciliation_id"],
            },
            ACTOR,
            False,
            "reconciliation-followup-no-reason-op",
            "reconciliation-followup-no-reason-key",
        ),
    )
    matched_resolve = service.resolve_reconciliation(
        str(matched_followup["reconciliation_id"]),
        {
            "base_revision": matched_revision,
            "matched_reconciliation_id": matched_observation["reconciliation_id"],
            "override_reason": "cash count reconciled",
        },
        ACTOR,
        False,
        "reconciliation-followup-reason-op",
        "reconciliation-followup-reason-key",
    )
    assert (
        matched_resolve["status"] == "resolved"
        and matched_resolve["resolution_kind"] == "matched_followup"
        and matched_resolve["resolution_reconciliation_id"]
        == matched_observation["reconciliation_id"]
    )

    accepted_bound = record(limit_ok, "0.00", comment="exact maximum difference")
    assert accepted_bound["difference_minor"] == 9_000_000_000_000_000
    accepted_negative_bound = record(
        limit_negative, "0.00", comment="exact negative maximum difference"
    )
    assert accepted_negative_bound["difference_minor"] == -9_000_000_000_000_000
    expect_error(
        "reconciliation_difference_out_of_range",
        lambda: record(limit_over, "90000000000000.00", comment="must exceed range"),
    )


def test_review_007_regressions(db_path: Path) -> None:
    """Ordinary service regressions for the four backend review-007 findings."""
    bootstrap_finance_cash_store(db_path)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"

    service = FinanceCashService(db_path)
    source = create_account(service, "Review source")
    target = create_account(service, "Review target")
    yen = create_account(service, "Review JPY", "JPY")
    open_account(service, source, "100.00")
    open_account(service, target, "0.00")
    open_account(service, yen, "0")

    income_category = str(
        service.create_category(
            {"name": "Review income", "direction": "income"},
            ACTOR,
            *reversed(command_id()),
        )["category_id"]
    )
    expense_category = str(
        service.create_category(
            {
                "name": "Review expense",
                "direction": "expense",
                "posting_class": "external_outflow",
            },
            ACTOR,
            *reversed(command_id()),
        )["category_id"]
    )
    unused_expense_category = str(
        service.create_category(
            {"name": "Unused review expense", "direction": "expense"},
            ACTOR,
            *reversed(command_id()),
        )["category_id"]
    )
    expect_error(
        "invalid_category",
        lambda: service.create_category(
            {
                "name": "Invalid review expense",
                "direction": "expense",
                "posting_class": "other",
            },
            ACTOR,
            *reversed(command_id()),
        ),
    )

    # A real writer commits in WAL mode after the reader has calculated its
    # final balance but before it selects movement rows.  Both parts of the
    # public response must still come from the reader's one snapshot.
    pending = service.create_document(
        {
            "document_type": "income",
            "target_account_id": source,
            "category_id": income_category,
            "amount": "10.00",
            "occurred_at": "2026-09-21T11:00:00Z",
            "purpose": "concurrent ordinary posting",
        },
        ACTOR,
        *reversed(command_id()),
    )
    balance_ready = Barrier(2)
    writer_done = Barrier(2)

    class PausedReader(FinanceCashService):
        balance_calls = 0

        def _balance(
            self,
            conn: sqlite3.Connection,
            account_id: str,
            as_of: str | None = None,
        ) -> int:
            result = super()._balance(conn, account_id, as_of)
            self.balance_calls += 1
            if self.balance_calls == 2:
                balance_ready.wait(timeout=5)
                writer_done.wait(timeout=5)
            return result

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(PausedReader(db_path).movements, source)
        balance_ready.wait(timeout=5)
        service.post_document(
            str(pending["document_id"]),
            {"base_revision": pending["revision"]},
            ACTOR,
            *reversed(command_id()),
        )
        writer_done.wait(timeout=5)
        snapshot = future.result(timeout=5)
    movement_total = sum(
        int(entry["amount_minor"]) * (1 if entry["side"] == "debit" else -1)
        for entry in snapshot["movements"]
    )
    assert snapshot["balance_minor"] == movement_total == 10_000
    assert service.get_account(source)["balance_minor"] == 11_000

    # An omitted amount is a true partial PATCH.  Changing to an account with a
    # different currency requires an explicit replacement amount so stored
    # minor units are never silently reinterpreted.
    draft = service.create_document(
        {
            "document_type": "income",
            "target_account_id": source,
            "category_id": income_category,
            "amount": "12.34",
            "occurred_at": "2026-09-21T12:00:00Z",
            "purpose": "before patch",
        },
        ACTOR,
        *reversed(command_id()),
    )
    patched = service.patch_document(
        str(draft["document_id"]),
        {"base_revision": 1, "purpose": "after patch"},
        ACTOR,
        *reversed(command_id()),
    )
    patched_view = service.get_document(str(draft["document_id"]))["document"]
    assert patched_view["amount_minor"] == 1_234
    assert patched_view["amount"] == "12.34"
    assert patched_view["purpose"] == "after patch"
    posted_partial = service.post_document(
        str(draft["document_id"]),
        {"base_revision": patched["revision"]},
        ACTOR,
        *reversed(command_id()),
    )
    assert posted_partial["financial_effect"] is True

    currency_draft = service.create_document(
        {
            "document_type": "income",
            "target_account_id": source,
            "category_id": income_category,
            "amount": "5.00",
            "occurred_at": "2026-09-21T12:10:00Z",
            "purpose": "currency patch",
        },
        ACTOR,
        *reversed(command_id()),
    )
    expect_error(
        "invalid_document",
        lambda: service.patch_document(
            str(currency_draft["document_id"]),
            {"base_revision": 1, "target_account_id": yen},
            ACTOR,
            *reversed(command_id()),
        ),
    )
    unchanged_currency_draft = service.get_document(
        str(currency_draft["document_id"])
    )["document"]
    assert unchanged_currency_draft["revision"] == 1
    assert unchanged_currency_draft["target_account_id"] == source
    assert unchanged_currency_draft["amount"] == "5.00"
    explicit_currency_patch = service.patch_document(
        str(currency_draft["document_id"]),
        {"base_revision": 1, "target_account_id": yen, "amount": "5"},
        ACTOR,
        *reversed(command_id()),
    )
    assert service.get_document(str(currency_draft["document_id"]))["document"][
        "amount"
    ] == "5"
    service.post_document(
        str(currency_draft["document_id"]),
        {"base_revision": explicit_currency_patch["revision"]},
        ACTOR,
        *reversed(command_id()),
    )
    assert service.get_account(yen)["balance_minor"] == 5

    # The reversal date is bounded by the latest exact original phase.  A
    # rejected between-phase request leaves no document, operation or effect.
    balances_before_transfer = (
        service.get_account(source)["balance_minor"],
        service.get_account(target)["balance_minor"],
    )
    transfer = create_and_post(
        service,
        {
            "document_type": "transfer",
            "source_account_id": source,
            "target_account_id": target,
            "amount": "10.00",
            "occurred_at": "2026-09-21T13:00:00Z",
            "transfer_mode": "two_phase",
        },
    )
    transfer_id = str(transfer["document_id"])
    completed = service.transfer_transition(
        transfer_id,
        "complete",
        {"base_revision": 2, "occurred_at": "2026-09-23T13:00:00Z"},
        ACTOR,
        *reversed(command_id()),
    )
    assert completed["effect_kind"] == "completed"
    completed_revision = service.get_document(transfer_id)["document"]["revision"]
    rejected_key, rejected_operation = command_id()
    with sqlite3.connect(db_path) as conn:
        counts_before = conn.execute(
            "SELECT (SELECT COUNT(*) FROM finance_liquidity_documents),"
            "(SELECT COUNT(*) FROM finance_liquidity_ledger_transactions)"
        ).fetchone()
    expect_error(
        "invalid_reversal",
        lambda: service.reverse_document(
            transfer_id,
            {
                "base_revision": completed_revision,
                "reason": "between phases must fail",
                "occurred_at": "2026-09-22T13:00:00Z",
            },
            ACTOR,
            rejected_operation,
            rejected_key,
        ),
    )
    with sqlite3.connect(db_path) as conn:
        counts_after = conn.execute(
            "SELECT (SELECT COUNT(*) FROM finance_liquidity_documents),"
            "(SELECT COUNT(*) FROM finance_liquidity_ledger_transactions)"
        ).fetchone()
        rejected_receipt = conn.execute(
            "SELECT 1 FROM finance_liquidity_operations WHERE operation_id=?",
            (rejected_operation,),
        ).fetchone()
    assert counts_after == counts_before
    assert rejected_receipt is None
    assert service.movements(target, "2026-09-22T13:30:00Z")["balance_minor"] == 0
    reversed_transfer = service.reverse_document(
        transfer_id,
        {
            "base_revision": completed_revision,
            "reason": "at latest phase succeeds",
            "occurred_at": "2026-09-23T13:00:00Z",
        },
        ACTOR,
        *reversed(command_id()),
    )
    assert len(reversed_transfer["ledger_transaction_ids"]) == 2
    assert (
        service.get_account(source)["balance_minor"],
        service.get_account(target)["balance_minor"],
    ) == balances_before_transfer

    # Category matrix is enforced at storage and classification becomes
    # immutable only after first posting; names/activity remain outside this
    # bounded classification guard.
    create_and_post(
        service,
        {
            "document_type": "expense",
            "source_account_id": source,
            "category_id": expense_category,
            "amount": "1.00",
            "occurred_at": "2026-09-24T10:00:00Z",
            "purpose": "use expense classification",
        },
    )
    with sqlite3.connect(db_path) as conn:
        table_sql = str(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='finance_liquidity_categories'"
            ).fetchone()[0]
        )
        trigger_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='finance_category_classification_after_use'"
        ).fetchone()
        assert "posting_class IN('external_outflow','fee')" in table_sql
        assert trigger_sql is not None

        def rejected(statement: str, parameters: tuple[Any, ...]) -> None:
            try:
                conn.execute(statement, parameters)
            except sqlite3.IntegrityError:
                return
            raise AssertionError(f"category guard accepted: {statement}")

        rejected(
            "INSERT INTO finance_liquidity_categories VALUES(?,?,?,?,?,?)",
            ("invalid-income", "Invalid", "income", "external_outflow", 1, OPENING_AT),
        )
        rejected(
            "INSERT INTO finance_liquidity_categories VALUES(?,?,?,?,?,?)",
            ("invalid-expense", "Invalid", "expense", None, 1, OPENING_AT),
        )
        rejected(
            "UPDATE finance_liquidity_categories SET direction='expense',posting_class='external_outflow' WHERE category_id=?",
            (income_category,),
        )
        rejected(
            "UPDATE finance_liquidity_categories SET posting_class='fee' WHERE category_id=?",
            (expense_category,),
        )
        conn.execute(
            "UPDATE finance_liquidity_categories SET posting_class='fee' WHERE category_id=?",
            (unused_expense_category,),
        )
        assert conn.execute(
            "SELECT posting_class FROM finance_liquidity_categories WHERE category_id=?",
            (unused_expense_category,),
        ).fetchone()[0] == "fee"


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
    run_readiness_checks()
    with TemporaryDirectory() as directory:
        test_review_007_regressions(
            Path(directory) / "cash-review-007-regressions.sqlite3"
        )
    with TemporaryDirectory() as directory:
        test_cash_ledger(Path(directory) / "cash.sqlite3")
    with TemporaryDirectory() as directory:
        test_duplicate_and_reconciliation_regressions(
            Path(directory) / "cash-regressions.sqlite3"
        )
    print("finance_liquidity_cash_smoke: ok")


if __name__ == "__main__":
    main()
