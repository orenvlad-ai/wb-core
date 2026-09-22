#!/usr/bin/env python3
"""Playwright E2E for the isolated Finance cash UI using only synthetic data."""

from __future__ import annotations

import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
import time

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.finance_liquidity_auth import FixtureFinanceAuth  # noqa: E402
from packages.adapters.finance_liquidity_http import (  # noqa: E402
    FinanceHttpApp,
    build_finance_http_server,
)
from packages.application.finance_liquidity_cash import (  # noqa: E402
    FinanceCashService,
    bootstrap_finance_cash_store,
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _operation(index: int) -> tuple[str, str]:
    return (f"browser-operation-{index}", f"browser-key-{index}")


def _field(page: object, name: str) -> object:
    return page.locator(f'[data-dialog-content] [name="{name}"]')


def _submit(page: object) -> None:
    page.locator("[data-dialog-submit]").click()
    try:
        expect(page.locator("[data-dialog]")).to_be_hidden()
    except AssertionError as caught:
        raise AssertionError(
            f"dialog submit failed: {page.locator('[data-error]').inner_text()}"
        ) from caught


def _open(page: object, action: str) -> None:
    page.locator(f'[data-action="{action}"]').click()
    expect(page.locator("[data-dialog]")).to_be_visible()
    expect(page.locator("[data-dialog] [data-instance-label]")).to_have_text(
        "ТЕСТОВАЯ БАЗА · ИЗОЛИРОВАННЫЕ ДАННЫЕ"
    )


def _create_cash(page: object, name: str, responsible: str) -> None:
    _open(page, "new-cash")
    _field(page, "name").fill(name)
    _field(page, "responsible_name").fill(responsible)
    _submit(page)
    expect(page.locator("[data-accounts]", has_text=name)).to_be_visible()


def _opening(page: object, account_name: str, amount: str, comment: str) -> None:
    _open(page, "opening")
    _field(page, "target_account_id").select_option(label=account_name)
    _field(page, "occurred_at").fill("2026-09-21T10:00")
    _field(page, "amount").fill(amount)
    _field(page, "opening_evidence_ref").fill(comment)
    _submit(page)


def main() -> None:
    with TemporaryDirectory(prefix="finance-liquidity-browser-") as temporary:
        evidence_dir = Path(
            os.environ.get("FINANCE_LIQUIDITY_EVIDENCE_DIR", temporary)
        ).resolve()
        evidence_dir.mkdir(parents=True, exist_ok=True)
        database = Path(temporary) / "finance.sqlite"
        bootstrap_finance_cash_store(database)
        service = FinanceCashService(database)
        category_operation, category_key = _operation(1)
        service.create_category(
            {"name": "Тестовая статья расхода", "direction": "expense"},
            "fixture-admin",
            category_operation,
            category_key,
        )
        income_operation, income_key = _operation(2)
        service.create_category(
            {"name": "Тестовая статья поступления", "direction": "income"},
            "fixture-admin",
            income_operation,
            income_key,
        )
        actor = {
            "fixture-admin": {
                "username": "fixture-admin",
                "role": "admin",
                "capabilities": ["finance_admin", "finance_operate", "finance"],
            }
        }
        auth = FixtureFinanceAuth.__new__(FixtureFinanceAuth)
        auth.actors = actor
        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        app = FinanceHttpApp(
            service,
            auth,
            read_enabled=True,
            write_enabled=True,
            csrf_secret="browser-fixture-csrf",
            static_dir=ROOT / "packages/adapters/finance_liquidity_static",
            allowed_origin=base_url,
            business_runtime_dir=Path(temporary),
            instance_label="ТЕСТОВАЯ БАЗА · ИЗОЛИРОВАННЫЕ ДАННЫЕ",
            store_id="finance-liquidity-pilot",
            store_mode="isolated_test",
        )
        server = build_finance_http_server("127.0.0.1", port, app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(viewport={"width": 1440, "height": 1080}, color_scheme="dark", timezone_id="Asia/Yekaterinburg")
                page = context.new_page()
                console_errors: list[str] = []
                lost_response_seen = {"value": False}
                expected_validation_seen = {"value": False}
                submitted_posts: list[tuple[str, str]] = []
                operation_readbacks: list[str] = []
                operation_responses: list[tuple[str, int]] = []

                def observe_request(request: object) -> None:
                    if request.method == "POST" and request.url.endswith("/post"):
                        submitted_posts.append((request.url, request.header_value("x-operation-id") or ""))
                    if request.method == "GET" and "/v1/finance/operations/" in request.url:
                        operation_readbacks.append(request.url)

                page.on("request", observe_request)

                def observe_response(response: object) -> None:
                    if (
                        response.request.method == "GET"
                        and "/v1/finance/operations/" in response.url
                    ):
                        operation_responses.append((response.url, response.status))

                page.on("response", observe_response)
                page.on(
                    "console",
                    lambda message: console_errors.append(message.text)
                    if message.type == "error" and not lost_response_seen["value"] and not expected_validation_seen["value"]
                    else None,
                )
                page.context.add_cookies(
                    [{"name": "finance_fixture_session", "value": "fixture-admin", "url": base_url}]
                )
                page.goto(f"{base_url}/finance/", wait_until="networkidle")
                expect(page.locator("main > [data-instance-label]")).to_have_text(
                    "ТЕСТОВАЯ БАЗА · ИЗОЛИРОВАННЫЕ ДАННЫЕ"
                )
                expect(page.locator("[data-session-state]")).to_contain_text("операциям")
                expect(page.get_by_text("Пока нет счетов")).to_be_visible()

                _create_cash(page, "Тестовая касса A", "Тестовый оператор")
                _opening(page, "Тестовая касса A · Тестовый оператор", "1000,00", "Пересчитано с ответственным")
                try:
                    expect(page.locator("[data-accounts]", has_text="1\u202f000,00")).to_be_visible()
                except AssertionError as caught:
                    raise AssertionError(
                        f"opening balance did not render: accounts={page.locator('[data-accounts]').inner_text()!r}; error={page.locator('[data-error]').inner_text()!r}; session={page.locator('[data-session-state]').inner_text()!r}"
                    ) from caught
                _create_cash(page, "Тестовая касса B", "Тестовый оператор")
                _opening(page, "Тестовая касса B · Тестовый оператор", "0,00", "Подтверждено ответственным")

                _open(page, "expense")
                _field(page, "source_account_id").select_option(label="Тестовая касса A · Тестовый оператор")
                _field(page, "category_id").select_option(label="Тестовая статья расхода")
                _field(page, "occurred_at").fill("2026-09-21T11:00")
                _field(page, "amount").fill("1200,00")
                _field(page, "purpose").fill("Личная оплата сотрудника")
                page.locator("[data-dialog-save-draft]").click()
                expect(page.locator("[data-dialog]")).to_be_hidden()
                failed_expense = page.locator(".history-row", has_text="Личная оплата сотрудника")
                expected_validation_seen["value"] = True
                failed_expense.locator("[data-post-draft]").click()
                expect(page.locator("[data-error]")).to_contain_text("добавьте пояснение")
                expect(failed_expense).to_contain_text("Черновик")
                failed_expense.locator("[data-edit-draft]").click()
                _field(page, "negative_balance_explanation").fill("Личное авансирование, требуется разбор")
                _submit(page)
                failed_expense.locator("[data-post-draft]").click()
                expect(failed_expense).to_contain_text("Проведено")
                expect(page.locator("[data-attention]")).to_contain_text("Отрицательный остаток")
                expect(page.locator("[data-accounts]", has_text="требуется разбор")).to_be_visible()

                _open(page, "income")
                _field(page, "target_account_id").select_option(label="Тестовая касса A · Тестовый оператор")
                _field(page, "category_id").select_option(label="Тестовая статья поступления")
                _field(page, "occurred_at").fill("2026-09-21T11:10")
                _field(page, "amount").fill("1500,00")
                _field(page, "purpose").fill("Тестовый приход")
                page.locator("[data-dialog-save-draft]").click()
                expect(page.locator("[data-dialog]")).to_be_hidden()
                draft = page.locator(".history-row", has_text="Тестовый приход")
                expect(draft).to_contain_text("Черновик")
                draft.locator("[data-post-draft]").click()
                page.wait_for_timeout(200)
                if "Черновик" in draft.inner_text():
                    raise AssertionError(f"income draft did not post: {page.locator('[data-error]').inner_text()}")
                expect(draft).to_contain_text("Проведено")
                expect(draft).to_contain_text("+1\u202f500,00")
                expect(failed_expense).to_contain_text("−1\u202f200,00")

                _open(page, "transfer")
                _field(page, "source_account_id").select_option(label="Тестовая касса A · Тестовый оператор")
                _field(page, "target_account_id").select_option(label="Тестовая касса B · Тестовый оператор")
                _field(page, "occurred_at").fill("2026-09-21T11:20")
                _field(page, "amount").fill("1500,00")
                _field(page, "purpose").fill("Перевод без подтверждения получателя")
                _field(page, "transfer_mode").select_option("instant")
                page.locator("[data-dialog-save-draft]").click()
                expect(page.locator("[data-dialog]")).to_be_hidden()
                lost_transfer = page.locator(".history-row", has_text="Перевод без подтверждения получателя")
                expect(lost_transfer).to_contain_text("Черновик")
                lost_transfer.locator("[data-post-draft]").click()
                expect(page.locator("[data-error]")).to_contain_text("Для этой операции добавьте пояснение")
                expect(lost_transfer).to_contain_text("Черновик")
                lost_transfer.locator("[data-edit-draft]").click()
                _field(page, "negative_balance_explanation").fill("Личное авансирование, требуется разбор")
                _submit(page)
                lost_response_status: list[int] = []
                lost_response_body: list[str] = []
                lost_response_operation_ids: list[str] = []

                def lose_post_response(route: object) -> None:
                    response = route.fetch()
                    lost_response_status.append(response.status)
                    lost_response_body.append(response.text())
                    lost_response_operation_ids.append(route.request.header_value("x-operation-id") or "")
                    lost_response_seen["value"] = True
                    route.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        body="<html><body>incomplete response</body></html>",
                    )

                page.route("**/v1/finance/documents/*/post", lose_post_response, times=1)
                posts_before_lost_response = len(submitted_posts)
                try:
                    with page.expect_request(
                        lambda request: request.method == "POST" and request.url.endswith("/post")
                    ) as lost_post_request, page.expect_response(
                        lambda response: response.request.method == "GET"
                        and "/v1/finance/operations/" in response.url
                        and response.status == 200
                    ) as lost_readback_response:
                        lost_transfer.locator("[data-post-draft]").click()
                except PlaywrightTimeoutError as caught:
                    pending_transfers = [
                        item
                        for item in service.list_documents()
                        if item["document_type"] == "transfer"
                    ]
                    raise AssertionError(
                        "lost-response readback timed out: "
                        f"post_status={lost_response_status!r}; "
                        f"post_body={lost_response_body!r}; "
                        f"post_operation_ids={lost_response_operation_ids!r}; "
                        f"submitted_posts={submitted_posts[posts_before_lost_response:]!r}; "
                        f"readback_requests={operation_readbacks!r}; "
                        f"readback_responses={operation_responses!r}; "
                        f"notice={page.locator('[data-notice]').inner_text()!r}; "
                        f"error={page.locator('[data-error]').inner_text()!r}; "
                        f"transfers={pending_transfers!r}"
                    ) from caught
                expect(page.locator("[data-notice]")).to_contain_text("Результат операции подтверждён")
                if not lost_response_seen["value"]:
                    raise AssertionError(f"invalid-response route did not run: {page.locator('[data-error]').inner_text()}")
                if lost_post_request.value.header_value("x-operation-id") != lost_response_operation_ids[0]:
                    raise AssertionError("lost-response route intercepted a different operation")
                if not lost_readback_response.value.url.endswith(
                    "/" + lost_response_operation_ids[0]
                ):
                    raise AssertionError("lost-response readback used a different operation")
                pending_transfers = [item for item in service.list_documents() if item["document_type"] == "transfer"]
                if not pending_transfers or pending_transfers[-1]["status"] != "posted":
                    raise AssertionError(f"invalid-response post did not become durable: {lost_response_status} {lost_response_body} {pending_transfers}")
                if lost_response_status != [200] or not lost_response_operation_ids[0]:
                    raise AssertionError(f"invalid-response post was not forwarded successfully: {lost_response_status}")
                if len(submitted_posts) != posts_before_lost_response + 1:
                    raise AssertionError(f"ambiguous response retried the write: {submitted_posts}")
                expected_readback = f"/v1/finance/operations/{lost_response_operation_ids[0]}"
                if not any(expected_readback in url for url in operation_readbacks):
                    raise AssertionError(f"same-operation readback was not requested: {operation_readbacks}")
                transfers = [item for item in service.list_documents() if item["document_type"] == "transfer"]
                if len(transfers) != 1 or transfers[0]["status"] != "posted":
                    raise AssertionError(f"invalid response created duplicate or missed transfer: {transfers}")

                _open(page, "income")
                _field(page, "target_account_id").select_option(label="Тестовая касса A · Тестовый оператор")
                _field(page, "category_id").select_option(label="Тестовая статья поступления")
                _field(page, "occurred_at").fill("2026-09-21T11:21")
                _field(page, "amount").fill("1500,00")
                _field(page, "purpose").fill("Похожее поступление требует решения")
                page.locator("[data-dialog-save-draft]").click()
                expect(page.locator("[data-dialog]")).to_be_hidden()
                duplicate_draft = page.locator(".history-row", has_text="Похожее поступление требует решения")
                duplicate_response_status: list[int] = []
                duplicate_operation_ids: list[str] = []

                def lose_duplicate_response(route: object) -> None:
                    response = route.fetch()
                    duplicate_response_status.append(response.status)
                    duplicate_operation_ids.append(route.request.header_value("x-operation-id") or "")
                    route.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        body="<html><body>incomplete duplicate response</body></html>",
                    )

                page.route("**/v1/finance/documents/*/post", lose_duplicate_response, times=1)
                stalled_readbacks: list[str] = []

                def stall_first_operation_readback(route: object) -> None:
                    stalled_readbacks.append(route.request.url)
                    # Keep the route pending beyond the UI deadline. Chromium's
                    # AbortController fires independently of this test callback.
                    time.sleep(6)
                    try:
                        route.continue_()
                    except PlaywrightError:
                        # The expected abort can dispose the pending route first.
                        pass

                page.route(
                    "**/v1/finance/operations/*",
                    stall_first_operation_readback,
                    times=1,
                )
                posts_before_duplicate_response = len(submitted_posts)
                readbacks_before_duplicate_response = len(operation_readbacks)
                with page.expect_response(
                    lambda response: response.request.method == "GET"
                    and "/v1/finance/operations/" in response.url
                    and response.status == 200
                ) as duplicate_operation_readback:
                    with page.expect_request(
                        lambda request: request.method == "POST"
                        and request.url.endswith("/post")
                    ) as duplicate_post_request:
                        duplicate_draft.locator("[data-post-draft]").click()
                duplicate_readback_payload = duplicate_operation_readback.value.json()
                expect(page.locator("[data-notice]")).to_contain_text("требует вашего решения")
                if duplicate_post_request.value.header_value("x-operation-id") != duplicate_operation_ids[0]:
                    raise AssertionError("duplicate-response route intercepted a different operation")
                expect(page.locator("[data-notice]")).to_contain_text("Деньги пока не изменены")
                expect(duplicate_draft).to_contain_text("Черновик")
                if duplicate_response_status != [409] or not duplicate_operation_ids[0]:
                    raise AssertionError(f"duplicate action was not durably recorded: {duplicate_response_status}")
                if len(submitted_posts) != posts_before_duplicate_response + 1:
                    raise AssertionError(f"duplicate action retried the write: {submitted_posts}")
                duplicate_readback = f"/v1/finance/operations/{duplicate_operation_ids[0]}"
                duplicate_readback_requests = operation_readbacks[
                    readbacks_before_duplicate_response:
                ]
                if (
                    duplicate_operation_readback.value.url != f"{base_url}{duplicate_readback}"
                    or duplicate_readback_payload.get("data", {}).get("operation_id")
                    != duplicate_operation_ids[0]
                    or duplicate_readback_payload.get("data", {}).get("action_required")
                    != "duplicate_confirmation"
                ):
                    raise AssertionError(
                        f"duplicate action readback was not the durable decision: "
                        f"{duplicate_operation_readback.value.url} {duplicate_readback_payload}"
                    )
                if stalled_readbacks != [f"{base_url}{duplicate_readback}"]:
                    raise AssertionError(
                        f"first same-operation readback was not stalled: {stalled_readbacks}"
                    )
                if duplicate_readback_requests != [
                    f"{base_url}{duplicate_readback}",
                    f"{base_url}{duplicate_readback}",
                ]:
                    raise AssertionError(
                        "readback timeout did not retry the exact same operation: "
                        f"{duplicate_readback_requests}"
                    )
                if not any(duplicate_readback in url for url in operation_readbacks):
                    raise AssertionError(f"duplicate action did not read back its operation: {operation_readbacks}")

                lost_transfer.locator("[data-reverse]").click()
                _field(page, "occurred_at").fill("2026-09-21T11:22")
                _field(page, "reason").fill("Тестовая отмена перевода")
                _submit(page)
                transfer_correction = page.locator(".history-row", has_text="Тестовая отмена перевода")
                expect(transfer_correction).to_contain_text("Отмена перевода")
                expect(transfer_correction).to_contain_text("Тестовая касса B → Тестовая касса A")
                expect(transfer_correction).to_contain_text("Связано с исходной операцией")
                expect(transfer_correction.locator("[data-reverse]")).to_have_count(0)

                _open(page, "transfer")
                _field(page, "source_account_id").select_option(label="Тестовая касса A · Тестовый оператор")
                _field(page, "target_account_id").select_option(label="Тестовая касса B · Тестовый оператор")
                _field(page, "occurred_at").fill("2026-09-21T11:30")
                _field(page, "amount").fill("50,00")
                _field(page, "purpose").fill("Тестовый перевод в пути")
                _field(page, "transfer_mode").select_option("two_phase")
                _field(page, "negative_balance_explanation").fill("Личное авансирование, требуется разбор")
                _submit(page)
                transit = page.locator(".history-row", has_text="Тестовый перевод в пути")
                expect(transit).to_contain_text("В пути")
                transit.locator('[data-transfer-transition$=":complete"]').click()
                _field(page, "occurred_at").fill("2026-09-21T11:40")
                _submit(page)
                expect(transit).to_contain_text("Проведено")

                page.locator('[data-history-filters] [name="type"]').select_option("opening")
                page.locator('[data-history-filters]').get_by_role("button", name="Показать").click()
                opening = page.locator(
                    ".history-row", has_text="Тестовая касса A"
                ).locator("[data-replace-opening]")
                expect(opening).to_be_visible()
                opening.click()
                _field(page, "occurred_at").fill("2026-09-21T10:00")
                _field(page, "amount").fill("1100,00")
                _field(page, "reason").fill("Уточнили после пересчёта")
                _submit(page)
                expect(page.locator("[data-notice]")).to_contain_text("заменён")
                expect(page.locator(".history-row", has_text="Заменяет прежний начальный остаток")).to_be_visible()
                opening_correction = page.locator(
                    ".history-row", has_text="Исправление начального остатка: Уточнили после пересчёта"
                )
                expect(opening_correction).to_be_visible()
                expect(opening_correction).to_contain_text("−1\u202f000,00")
                expect(opening_correction.locator("[data-replace-opening]")).to_have_count(0)
                expect(page.locator("[data-history]")).not_to_contain_text("Opening reversal:")

                _create_cash(page, "Тестовая касса C", "Тестовый оператор")
                _opening(
                    page,
                    "Тестовая касса C · Тестовый оператор",
                    "-100,00",
                    "Отрицательный остаток подтверждён",
                )
                negative_opening = page.locator(
                    ".history-row", has_text="Тестовая касса C"
                )
                expect(negative_opening).to_contain_text("−100,00")
                negative_opening.locator("[data-replace-opening]").click()
                _field(page, "occurred_at").fill("2026-09-21T10:01")
                _field(page, "amount").fill("-90,00")
                _field(page, "reason").fill("Уточнили отрицательный остаток")
                _submit(page)
                negative_opening_correction = page.locator(
                    ".history-row",
                    has_text="Исправление начального остатка: Уточнили отрицательный остаток",
                )
                expect(negative_opening_correction).to_be_visible()
                expect(negative_opening_correction).to_contain_text("+100,00")

                page.locator('[data-history-filters] [name="type"]').select_option("")
                page.locator('[data-history-filters]').get_by_role("button", name="Показать").click()
                expense_reverse = failed_expense.locator("[data-reverse]")
                expect(expense_reverse).to_be_visible()
                expense_reverse.click()
                _field(page, "occurred_at").fill("2026-09-21T12:00")
                _field(page, "reason").fill("Тестовое исправление")
                _submit(page)
                expect(page.locator("[data-notice]")).to_contain_text("Исправление создано")
                expense_correction = page.locator(".history-row", has_text="Тестовое исправление")
                expect(expense_correction).to_contain_text("+1\u202f200,00")
                expect(expense_correction).to_contain_text("Связано с исходной операцией")
                expect(expense_correction.locator("[data-reverse]")).to_have_count(0)
                draft.locator("[data-reverse]").click()
                _field(page, "occurred_at").fill("2026-09-21T12:01")
                _field(page, "reason").fill("Тестовое исправление прихода")
                _submit(page)
                income_correction = page.locator(".history-row", has_text="Тестовое исправление прихода")
                expect(income_correction).to_contain_text("−1\u202f500,00")
                expect(income_correction.locator("[data-reverse]")).to_have_count(0)

                before_reconciliation = service.get_account(transfers[0]["source_account_id"])["balance_minor"]
                _open(page, "reconcile")
                _field(page, "account_id").select_option(label="Тестовая касса A · Тестовый оператор")
                _field(page, "week_ending").fill("2026-09-21")
                _field(page, "actual_amount").fill("1,00")
                page.locator("[data-dialog-submit]").click()
                expect(page.locator("[data-error]")).to_contain_text("Опишите причину расхождения")
                expect(page.locator("[data-dialog]")).to_be_visible()
                _field(page, "comment").fill("Нужна проверка наличных по смене")
                _submit(page)
                expect(page.locator("[data-attention]")).to_contain_text("Расхождение при сверке")
                expect(page.locator("[data-reconciliations]")).to_contain_text("Расчётный:")
                expect(page.locator("[data-reconciliations]")).to_contain_text("Фактический: 1,00")
                expect(page.locator("[data-reconciliations]")).to_contain_text("Зафиксировано:")
                expect(page.locator("[data-reconciliations]")).to_contain_text("ЕКТ")
                after_reconciliation = service.get_account(transfers[0]["source_account_id"])["balance_minor"]
                if before_reconciliation != after_reconciliation:
                    raise AssertionError("reconciliation changed money")

                expect(page.locator("[data-history]")).not_to_contain_text("Нет данных")
                _open(page, "transfer")
                expect(_field(page, "transfer_mode")).to_have_value("instant")
                page.screenshot(path=evidence_dir / "cash-transfer-dialog.png", full_page=True)
                page.get_by_role("button", name="Отмена").click()
                expect(page.locator("[data-dialog]")).to_be_hidden()
                page.screenshot(path=evidence_dir / "cash-desktop.png", full_page=True)
                mobile_context = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark", timezone_id="Pacific/Honolulu")
                mobile = mobile_context.new_page()
                mobile.context.add_cookies(
                    [{"name": "finance_fixture_session", "value": "fixture-admin", "url": base_url}]
                )
                mobile.goto(f"{base_url}/finance/", wait_until="networkidle")
                expect(mobile.locator("[data-accounts]")).to_contain_text("Тестовая касса A")
                mobile.screenshot(path=evidence_dir / "cash-mobile.png", full_page=True)
                mobile.close()
                mobile_context.close()
                context.close()
                browser.close()
                if console_errors:
                    raise AssertionError(f"browser console errors: {console_errors}")
        finally:
            server.shutdown()
            server.server_close()
    print("finance_liquidity_browser_smoke: ok")


if __name__ == "__main__":
    main()
