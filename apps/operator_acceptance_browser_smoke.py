"""Browser contract for durable FF overhead acceptance and read-only journal.

All data and HTTP responses are isolated local fixtures. The browser deliberately
loses confirmation/read responses after the simulated durable source commit.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402
from apps.ff_pool_surfaces_browser_smoke import Clock, _seed, _reserve_free_port  # noqa: E402
from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_FF_POOL_PATH, DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH, DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH, DEFAULT_UPLOAD_PATH, build_registry_upload_http_server,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402

OPERATIONS = "/v1/sheet-vitrina-v1/operations"


def main() -> None:
    with TemporaryDirectory(prefix="operator-acceptance-browser-") as directory:
        runtime_dir = Path(directory) / "runtime"
        runtime_dir.mkdir()
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        runtime.list_ff_stock_operations(limit=1)
        clock = Clock()
        _seed(runtime, clock)
        config = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1", port=_reserve_free_port(), upload_path=DEFAULT_UPLOAD_PATH,
            sheet_plan_path=DEFAULT_SHEET_PLAN_PATH, sheet_refresh_path="/v1/sheet-vitrina-v1/refresh",
            sheet_status_path=DEFAULT_SHEET_STATUS_PATH, sheet_operator_ui_path=DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=runtime_dir,
        )
        server = build_registry_upload_http_server(config, entrypoint=RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir, runtime=runtime, activated_at_factory=clock,
        ))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                try:
                    run(browser, f"http://127.0.0.1:{config.port}", Path(directory))
                finally:
                    browser.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)
    print("operator_acceptance_browser_smoke: OK (manual/PDF, lost/malformed response, reload, journal states, safe text, focus/mobile)")


def run(browser, base: str, directory: Path) -> None:
    context = browser.new_context(viewport={"width":1280,"height":900})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    previews = {}
    accepted = {}
    confirmations = []
    reads = []
    journal_reads = []
    held_reads = []
    controls = {"lose_confirm":False, "malformed_confirm":False, "lose_read":False, "reject_confirm":False, "reject_html":False, "commit_proxy403":False, "commit_proxy409":False, "malformed_read":False, "renewed_pdf":False, "hold_read_id":"", "mismatch_identity":"", "receipt_domain":None, "receipt_source_domain":None}

    def send(route, payload):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload, ensure_ascii=False))

    def preview(route):
        request = route.request
        pdf = request.all_headers().get("content-type", "").startswith("multipart/")
        if pdf:
            # The fixture parser response proves that file parsing alone is not acceptance.
            identity = "renewed-pdf-browser" if controls["renewed_pdf"] else "pdf-browser"
        else:
            identity = request.post_data_json["request_id"]
        summary = {"facility_id":"browser-facility", "facility_name":"FF Москва",
            "scope":"FBS", "category":"other", "category_label_ru":"Прочие",
            "comment":"<img src=x onerror=window.__acceptanceXss=1>", "amount_rub":"61.25",
            "source_mode":"payment_order_pdf" if pdf else "manual", "business_date":"2026-08-12"}
        payload = {"contract_name":"ff_facility_pool_surfaces_v1", "workflow_contract":"ff_document_workflow_v1", "status":"ready",
            "request_id":identity,"document_kind":"pool_overhead", "state":"ready",
            "state_label_ru":"Готово к проведению", "confirm_allowed":True, "steps":[],
            "preview":{"available":True,"summary":summary}}
        if pdf and controls["renewed_pdf"]:
            summary["allocation_label"] = "Распределение будет выполнено после подтверждения в плановом цикле"
            payload["preview_renewal"] = {"predecessor_request_id":"old-pdf-browser","previous_business_date":"2026-08-11"}
            old = dict(payload)
            old.update(request_id="old-pdf-browser",state="blocked",state_label_ru="Проведение заблокировано",confirm_allowed=False,
                superseded_by={"request_id":identity,"business_date":"2026-08-12"},
                confirmation_block_reason_code="overhead_preview_superseded",
                confirmation_block_reason_ru="Этот предпросмотр заменён после повторной загрузки платежа. Откройте новый предпросмотр и проверьте дату учёта перед подтверждением.",
                error={"code":"overhead_preview_superseded"})
            old.pop("preview_renewal")
            old["preview"]={"available":True,"summary":{**summary,"business_date":"2026-08-11"}}
            previews["old-pdf-browser"]=old
        previews[identity] = payload
        send(route, payload)

    def request_status(route):
        path = urlparse(route.request.url).path
        identity = path.split("/requests/")[1].split("/")[0]
        selected_alias = identity == "browser-client-alias"
        if selected_alias: identity = manual
        if route.request.method == "POST":
            confirmations.append(identity)
            if controls["reject_html"]:
                route.fulfill(status=403, content_type="text/html", body="<html>Access denied</html>"); return
            if controls["reject_confirm"]:
                route.fulfill(status=409, content_type="application/json", body=json.dumps({"reason_ru":"Дата документа изменилась. Проверьте документ снова."})); return
            data = previews[identity]
            accepted[identity] = {"durable_saved":True,"document_kind":previews[identity]["document_kind"],"operation_id":identity,"request_id":identity,
                "accepted_at":"2026-08-12T07:00:00Z", "state":"accepted", "label_ru":"Принято",
                "reason_ru":"", "business_date":"2026-08-12", "summary":data["preview"]["summary"],
                "source_document":{"request_id":identity,"filename":"payment.pdf" if identity == "pdf-browser" else "",
                    "source_sha256":"sha256:fixture","source_revision":"revision:fixture"},
                "document":None,"publication":None}
            if controls["commit_proxy409"]:
                route.fulfill(status=409, content_type="application/json", body="{}"); return
            if controls["commit_proxy403"]:
                route.fulfill(status=403, content_type="text/html", body="<html>Session expired</html>"); return
            if controls["lose_confirm"]:
                route.abort(); return
            if controls["malformed_confirm"]:
                route.fulfill(status=200, content_type="text/html", body="<html>unexpected proxy response</html>"); return
        else:
            reads.append(identity)
            if controls["hold_read_id"] == identity:
                held_reads.append(route); return
            if controls["lose_read"]:
                route.abort(); return
            if controls["malformed_read"]:
                send(route, {"request_id":identity}); return
        if controls["mismatch_identity"]: identity = controls["mismatch_identity"]
        payload = dict(previews[identity])
        if selected_alias: payload["client_request_id"] = "browser-client-alias"
        if identity in accepted:
            operation=dict(accepted[identity])
            if controls['receipt_domain'] is not None: operation['domain']=controls['receipt_domain']
            if controls['receipt_source_domain'] is not None: operation['source_ref']={'domain':controls['receipt_source_domain']}
            payload.update(confirm_allowed=False, acceptance=operation)
        send(route, payload)

    def journal(route):
        assert route.request.method == "GET", "journal must never submit a write"
        journal_reads.append(route.request.url)
        path = urlparse(route.request.url).path
        if path == OPERATIONS:
            query = parse_qs(urlparse(route.request.url).query)
            number = int(query.get("page", ["1"])[0])
            send(route, {"contract_name":"operator_operations_v1","status":"ready", "items":list(accepted.values()),
                "page":number,"limit":25,"total":len(accepted),"has_more":False})
        else:
            identity = path.rsplit("/",1)[1]
            send(route, {"contract_name":"operator_operations_v1","status":"ready","operation":accepted[identity]})

    page.route(f"**{DEFAULT_FF_POOL_PATH}/documents/overhead/preview", preview)
    page.route(f"**{DEFAULT_FF_POOL_PATH}/requests/**", request_status)
    page.route(f"**{OPERATIONS}**", journal)
    page.goto(f"{base}{DEFAULT_SHEET_WEB_VITRINA_UI_PATH}?tab=warehouses&warehouse=ff", wait_until="domcontentloaded")
    page.locator('[data-unified-tab-button="warehouses"]').click()
    page.locator('[data-warehouse-key="ff"]').click()
    page.locator('[data-ff-operations-open]').click()
    page.get_by_text("Принятых операций пока нет.", exact=True).wait_for()
    assert not accepted and not confirmations
    assert page.locator('[data-ff-operation-receipt]').count() == 0

    page.locator('[data-ff-pool-tab="create"]').click()
    page.locator('[data-ff-pool-action-kind]').select_option("pool_overhead")
    # Client validation cannot be painted green or call the acceptance endpoint.
    page.locator('[data-ff-pool-facility]').select_option("")
    page.locator('[data-ff-pool-action-form]').dispatch_event("submit")
    page.get_by_text("Выберите склад FF.", exact=True).wait_for()
    assert not previews and not confirmations
    page.locator('[data-ff-pool-facility]').select_option(index=1)
    page.locator('[data-ff-pool-scope]').select_option("FBS")
    page.locator('[data-ff-pool-overhead-category]').select_option("other")
    page.locator('[data-ff-pool-overhead-comment]').fill("Синтетический расход")
    page.locator('[data-ff-pool-amount]').fill("61.25")
    page.locator('[data-ff-pool-preview]').click()
    page.get_by_role("button", name="Подтвердить проведение", exact=True).wait_for()
    manual = page.locator('[data-ff-pool-request-id]').input_value()
    assert not accepted
    assert page.locator('[data-ff-operation-receipt]').count() == 0
    assert page.locator('[data-ff-pool-preview-detail]').get_attribute("data-tone") == "neutral"
    # Repeated activation while POST is unresolved sends exactly one command.
    page.get_by_role("button", name="Подтвердить проведение", exact=True).evaluate("node => { node.click(); node.click(); }")
    receipt = page.locator('[data-ff-operation-receipt]')
    receipt.wait_for()
    assert confirmations == [manual]
    assert receipt.locator("h3").inner_text() == "Принято"
    assert receipt.get_by_text("Документ сохранён.", exact=True).is_visible()
    assert "Ваша работа выполнена" not in receipt.inner_text()
    assert "61,25" in receipt.inner_text() and "12.08.2026" in receipt.inner_text()
    assert "sha256" not in receipt.inner_text() and "request_id" not in receipt.inner_text()
    assert receipt.locator("img").count() == 0 and page.evaluate("window.__acceptanceXss") is None
    # Security assertion above uses malicious text; visual artifacts use ordinary business text.
    accepted[manual]["summary"]["comment"] = "Складские расходные материалы"
    page.locator('[data-ff-pool-tab="workflow"]').click()
    receipt.get_by_text("Складские расходные материалы", exact=True).wait_for()
    if os.environ.get("OPERATOR_ACCEPTANCE_SCREENSHOT_PREFIX"):
        page.screenshot(path=os.environ["OPERATOR_ACCEPTANCE_SCREENSHOT_PREFIX"] + "-desktop-receipt.png", full_page=False)
    receipt.get_by_role("link", name="Журнал операций", exact=True).click()
    detail = page.locator('[data-ff-operations-detail]')
    detail.get_by_text("Ожидает обработки", exact=True).wait_for()
    assert "при задержке" in detail.inner_text()
    if os.environ.get("OPERATOR_ACCEPTANCE_SCREENSHOT_PREFIX"):
        page.screenshot(path=os.environ["OPERATOR_ACCEPTANCE_SCREENSHOT_PREFIX"] + "-desktop-journal.png", full_page=False)
    detail.get_by_role("button", name="Открыть подтверждение").click()
    receipt.wait_for()

    # Reload/reopen reads the accepted source, without reusing the POST.
    page.reload(wait_until="domcontentloaded")
    page.locator('[data-unified-tab-button="warehouses"]').click()
    page.locator('[data-warehouse-key="ff"]').click()
    page.locator('[data-ff-pool-open]').click()
    receipt.wait_for()
    assert confirmations == [manual]
    assert page.evaluate("document.activeElement.matches('[data-ff-operation-receipt]')")
    page.keyboard.press("Escape")
    page.locator('[data-ff-pool-modal]').wait_for(state="hidden")
    assert page.evaluate("document.activeElement.matches('[data-ff-pool-open]')")
    page.locator('[data-ff-pool-open]').click()
    receipt.wait_for()
    page.locator('[data-ff-pool-close]').focus()
    page.keyboard.press("Shift+Tab")
    assert page.evaluate("document.querySelector('[data-ff-pool-modal]').contains(document.activeElement)")

    # Accepted stays green even when downstream is delayed/failed; journal owns processing state.
    for state, label in (("processing","Обрабатывается"),("delayed","Обработка задерживается"),
                         ("needs_attention","Требует внимания"),("completed","Обработано")):
        accepted[manual]["state"] = state
        accepted[manual]["reason_ru"] = "<svg onload=window.__acceptanceXss=1>"
        page.locator('[data-ff-pool-tab="workflow"]').click()
        receipt.wait_for()
        assert receipt.get_by_text("Принято", exact=True).is_visible()
        page.locator('[data-ff-pool-tab="journal"]').click()
        page.locator('[data-ff-operations-list]').get_by_text(label, exact=True).wait_for()
        page.locator('[data-ff-operations-list]').get_by_role("button", name="Подробнее").click()
        detail.get_by_text(label, exact=True).wait_for()
        assert detail.locator("svg").count() == 0 and page.evaluate("window.__acceptanceXss") is None
    accepted[manual]["document"] = {"document_id":"browser-posted-doc"}
    page.route(f"**{DEFAULT_FF_POOL_PATH}/documents/browser-posted-doc", lambda route: send(route, {
        "documents":[{"document_id":"browser-posted-doc","document_label_ru":"Накладные расходы ФФ"}]}))
    page.locator('[data-ff-operations-refresh]').click()
    detail.get_by_role("button", name="Открыть документ").wait_for()
    detail.get_by_role("button", name="Открыть документ").click()
    page.locator('[data-ff-pool-document-detail]').get_by_role("heading", name="Накладные расходы ФФ · browser-posted-doc").wait_for()

    # PDF parse remains a preview. Lose both confirmation and first read response.
    page.locator('[data-ff-pool-tab="create"]').click()
    page.locator('[data-ff-pool-action-kind]').select_option("pool_overhead")
    page.locator('[data-ff-pool-facility]').select_option(index=1)
    page.locator('[data-ff-pool-scope]').select_option("FBS")
    page.locator('[data-ff-pool-overhead-category]').select_option("other")
    page.locator('[data-ff-pool-overhead-comment]').fill("Платёж по PDF")
    pdf = directory / "synthetic.pdf"; pdf.write_bytes(b"%PDF-synthetic-fixture")
    page.locator('[data-ff-pool-overhead-file]').set_input_files(str(pdf))
    page.locator('[data-ff-pool-preview]').click()
    page.get_by_role("button", name="Подтвердить проведение", exact=True).wait_for()
    assert page.locator('[data-ff-operation-receipt]').count() == 0
    controls.update(lose_confirm=True, lose_read=True)
    page.get_by_role("button", name="Подтвердить проведение", exact=True).click()
    page.get_by_text("Проверяем сохранение", exact=True).wait_for()
    assert confirmations == [manual, "pdf-browser"]
    assert page.locator('[data-ff-operation-receipt]').count() == 0
    assert page.get_by_role("button", name="Подтвердить проведение", exact=True).count() == 0
    # The unresolved intent also blocks a blind new overhead submission from the create tab.
    preview_count = len(previews)
    page.locator('[data-ff-pool-tab="create"]').click()
    page.locator('[data-ff-pool-action-form]').dispatch_event("submit")
    page.get_by_text("Проверяем сохранение", exact=True).wait_for()
    assert len(previews) == preview_count and confirmations == [manual, "pdf-browser"]
    controls["lose_read"] = False
    page.reload(wait_until="domcontentloaded")
    page.locator('[data-unified-tab-button="warehouses"]').click()
    page.locator('[data-warehouse-key="ff"]').click()
    page.locator('[data-ff-pool-open]').click()
    receipt.wait_for()
    assert confirmations == [manual, "pdf-browser"]
    assert "Платёжное поручение PDF" in receipt.inner_text()

    # Malformed successful HTTP response also recovers by read only.
    controls.update(lose_confirm=False, malformed_confirm=True)
    page.locator('[data-ff-pool-tab="create"]').click()
    page.locator('[data-ff-pool-action-kind]').select_option("pool_overhead")
    if page.locator('[data-ff-pool-overhead-file-remove]').is_visible():
        page.locator('[data-ff-pool-overhead-file-remove]').click()
    page.locator('[data-ff-pool-facility]').select_option(index=1)
    page.locator('[data-ff-pool-scope]').select_option("FBS")
    page.locator('[data-ff-pool-overhead-category]').select_option("other")
    page.locator('[data-ff-pool-overhead-comment]').fill("Ещё один синтетический расход")
    page.locator('[data-ff-pool-amount]').fill("61.25")
    page.locator('[data-ff-pool-preview]').click()
    page.get_by_role("button", name="Подтвердить проведение", exact=True).click()
    receipt.wait_for()
    assert len(confirmations) == 3 and len(set(confirmations)) == 3
    assert reads and journal_reads
    page.set_viewport_size({"width":390,"height":844})
    assert page.locator('.ff-pool-dialog').evaluate("node => node.scrollWidth <= node.clientWidth + 1")
    # The long receipt stays scrollable and both lower actions remain reachable by touch/keyboard.
    mobile_close = receipt.get_by_role("button", name="Закрыть", exact=True)
    mobile_close.scroll_into_view_if_needed()
    assert mobile_close.is_visible() and mobile_close.bounding_box()["y"] < 844
    receipt.get_by_role("link", name="Журнал операций", exact=True).click()
    detail.get_by_text("Ожидает обработки", exact=True).wait_for()
    detail.get_by_role("button", name="Открыть подтверждение").click()
    receipt.wait_for()
    assert page.locator('.ff-pool-dialog').evaluate("node => node.scrollHeight > node.clientHeight")
    page.locator('.ff-pool-dialog').evaluate("node => { node.scrollTop = 0; }")
    if os.environ.get("OPERATOR_ACCEPTANCE_SCREENSHOT_PATH"):
        page.screenshot(path=os.environ["OPERATOR_ACCEPTANCE_SCREENSHOT_PATH"], full_page=False)
    if os.environ.get("OPERATOR_ACCEPTANCE_SCREENSHOT_PREFIX"):
        page.screenshot(path=os.environ["OPERATOR_ACCEPTANCE_SCREENSHOT_PREFIX"] + "-mobile-receipt.png", full_page=False)
    page.locator('[data-ff-pool-tab="journal"]').click()
    page.locator('[data-ff-operations-list] .ff-operation-row').first.wait_for()
    assert page.locator('.ff-pool-dialog').evaluate("node => node.scrollWidth <= node.clientWidth + 1")
    # A proved server rejection is a real error, never green and never a permanent uncertain marker.
    controls.update(malformed_confirm=False, reject_confirm=True)
    page.locator('[data-ff-pool-tab="create"]').click()
    page.locator('[data-ff-pool-amount]').fill("61.25")
    page.locator('[data-ff-pool-preview]').click()
    page.get_by_role("button", name="Подтвердить проведение", exact=True).click()
    page.get_by_text("Дата документа изменилась. Проверьте документ снова.", exact=True).wait_for()
    assert page.locator('[data-ff-operation-receipt]').count() == 0
    assert page.evaluate("localStorage.getItem('wb.ffPool.overheadConfirmation.v1')") is None
    assert len(confirmations) == 4 and len(accepted) == 3
    controls.update(reject_confirm=False, reject_html=True)
    page.locator('[data-ff-pool-tab="create"]').click()
    page.locator('[data-ff-pool-preview]').click()
    page.get_by_role("button", name="Подтвердить проведение", exact=True).click()
    page.get_by_text("Документ не принят. Проверьте данные и выполните проверку снова.", exact=True).wait_for()
    assert page.locator('[data-ff-operation-receipt]').count() == 0
    assert page.evaluate("localStorage.getItem('wb.ffPool.overheadConfirmation.v1')") is None
    assert len(confirmations) == 5 and len(accepted) == 3
    # A proxy/session 403 after commit cannot prove rejection when same-ID read is unavailable.
    controls.update(reject_html=False, commit_proxy403=True, lose_read=False)
    page.locator('[data-ff-pool-tab="create"]').click()
    page.locator('[data-ff-pool-preview]').click()
    page.get_by_role("button", name="Подтвердить проведение", exact=True).wait_for()
    controls["lose_read"] = True
    page.get_by_role("button", name="Подтвердить проведение", exact=True).click()
    page.get_by_text("Проверяем сохранение", exact=True).wait_for()
    uncertain = confirmations[-1]
    assert uncertain in accepted
    assert page.evaluate("localStorage.getItem('wb.ffPool.overheadConfirmation.v1')") == uncertain
    assert page.get_by_role("button", name="Подтвердить проведение", exact=True).count() == 0
    controls["lose_read"] = False
    page.reload(wait_until="domcontentloaded")
    page.locator('[data-unified-tab-button="warehouses"]').click()
    page.locator('[data-warehouse-key="ff"]').click()
    page.locator('[data-ff-pool-open]').click()
    receipt.wait_for()
    assert receipt.get_attribute("data-ff-operation-receipt") == uncertain
    assert len(confirmations) == 6 and len(accepted) == 4
    # Generic JSON409 after commit is equally uncertain; failed/malformed same-ID GET never proves refusal.
    for malformed in (False, True):
        controls.update(commit_proxy403=False, commit_proxy409=True, lose_read=False, malformed_read=False)
        page.locator('[data-ff-pool-tab="create"]').click()
        page.locator('[data-ff-pool-action-kind]').select_option("pool_overhead")
        page.locator('[data-ff-pool-facility]').select_option(index=1)
        page.locator('[data-ff-pool-scope]').select_option("FBS")
        page.locator('[data-ff-pool-overhead-category]').select_option("other")
        page.locator('[data-ff-pool-overhead-comment]').fill("Расход для проверки ответа")
        page.locator('[data-ff-pool-amount]').fill("61.25")
        page.locator('[data-ff-pool-preview]').click()
        page.get_by_role("button", name="Подтвердить проведение", exact=True).wait_for()
        controls.update(lose_read=not malformed, malformed_read=malformed)
        page.get_by_role("button", name="Подтвердить проведение", exact=True).click()
        page.get_by_text("Проверяем сохранение", exact=True).wait_for()
        uncertain = confirmations[-1]
        assert uncertain in accepted
        assert page.evaluate("localStorage.getItem('wb.ffPool.overheadConfirmation.v1')") == uncertain
        assert page.get_by_text("Документ не принят. Проверьте данные и выполните проверку снова.", exact=True).count() == 0
        assert page.get_by_role("button", name="Подтвердить проведение", exact=True).count() == 0
        preview_count, confirm_count = len(previews), len(confirmations)
        page.locator('[data-ff-pool-tab="create"]').click()
        page.locator('[data-ff-pool-action-form]').dispatch_event("submit")
        page.get_by_text("Проверяем сохранение", exact=True).wait_for()
        assert len(previews) == preview_count and len(confirmations) == confirm_count
        controls.update(lose_read=False, malformed_read=False)
        page.reload(wait_until="domcontentloaded")
        page.locator('[data-unified-tab-button="warehouses"]').click()
        page.locator('[data-warehouse-key="ff"]').click()
        page.locator('[data-ff-pool-open]').click()
        receipt.wait_for()
        assert receipt.get_attribute("data-ff-operation-receipt") == uncertain
        assert len(confirmations) == confirm_count
    assert len(confirmations) == 8 and len(accepted) == 6
    # Ordinary reupload opens today's active preview. An old saved ID remains
    # unconfirmed and provides a safe, explicit navigation to its successor.
    controls.update(commit_proxy409=False, renewed_pdf=True)
    page.locator('[data-ff-pool-tab="create"]').click()
    page.locator('[data-ff-pool-action-kind]').select_option("pool_overhead")
    page.locator('[data-ff-pool-facility]').select_option(index=1)
    page.locator('[data-ff-pool-scope]').select_option("FBS")
    page.locator('[data-ff-pool-overhead-category]').select_option("other")
    page.locator('[data-ff-pool-overhead-comment]').fill("Ранее загруженный платёж")
    page.locator('[data-ff-pool-overhead-file]').set_input_files(str(pdf))
    page.locator('[data-ff-pool-preview]').click()
    page.get_by_text("Дата учёта обновлена после повторной загрузки. Проверьте новый предпросмотр перед подтверждением.",exact=True).wait_for()
    page.get_by_text("Распределение будет выполнено после подтверждения в плановом цикле",exact=True).wait_for()
    assert page.locator('[data-ff-pool-request-id]').input_value() == "renewed-pdf-browser"
    assert page.get_by_role("button", name="Подтвердить проведение", exact=True).is_enabled()
    assert page.locator('[data-ff-operation-receipt]').count() == 0
    page.locator('[data-ff-pool-request-id]').fill("old-pdf-browser")
    page.locator('[data-ff-pool-request-load]').click()
    blocked_reason = page.locator('[data-ff-confirmation-block-reason]')
    blocked_reason.wait_for()
    assert "Этот предпросмотр заменён" in blocked_reason.inner_text()
    assert page.get_by_role("button", name="Подтвердить проведение", exact=True).is_disabled()
    assert page.locator('[data-ff-operation-receipt]').count() == 0
    page.get_by_role("button",name="Открыть новый предпросмотр",exact=True).click()
    page.wait_for_function("document.querySelector('[data-ff-pool-request-id]').value === 'renewed-pdf-browser' && !document.querySelector('[data-ff-confirmation-block-reason]')")
    assert page.get_by_role("button", name="Подтвердить проведение", exact=True).is_enabled()
    assert page.locator('[data-ff-operation-receipt]').count() == 0
    assert "old-pdf-browser" not in accepted and "renewed-pdf-browser" not in accepted and len(confirmations) == 8
    # Hold accepted A's actual GET response while the operator previews a new unconfirmed B.
    controls.update(renewed_pdf=False, hold_read_id=manual)
    page.locator('[data-ff-pool-request-id]').fill(manual)
    page.locator('[data-ff-pool-request-load]').click()
    for _ in range(50):
        if held_reads: break
        page.wait_for_timeout(10)
    assert held_reads, "accepted A GET must really be held before creating B"
    # Closing the modal invalidates that generation even if the same ID is still in the input.
    page.keyboard.press("Escape")
    closed_payload = dict(previews[manual], acceptance=accepted[manual], confirm_allowed=False)
    with page.expect_response(lambda response: response.url.endswith("/requests/" + manual)):
        send(held_reads.pop(), closed_payload)
    page.evaluate("async () => { await new Promise(requestAnimationFrame); await new Promise(requestAnimationFrame); }")
    assert page.locator('[data-ff-operation-receipt]').count() == 0
    page.locator('[data-ff-pool-open]').click()
    page.locator('[data-ff-pool-request-load]').click()
    for _ in range(50):
        if held_reads: break
        page.wait_for_timeout(10)
    assert held_reads
    page.locator('[data-ff-pool-tab="create"]').click()
    page.locator('[data-ff-pool-overhead-file-remove]').click()
    page.locator('[data-ff-pool-amount]').fill("61.25")
    page.locator('[data-ff-pool-preview]').click()
    page.get_by_role("button", name="Подтвердить проведение", exact=True).wait_for()
    current_b = page.locator('[data-ff-pool-request-id]').input_value()
    assert current_b != manual and current_b not in accepted
    delayed_payload = dict(previews[manual], acceptance=accepted[manual], confirm_allowed=False)
    with page.expect_response(lambda response: response.url.endswith("/requests/" + manual)):
        send(held_reads.pop(), delayed_payload)
    controls["hold_read_id"] = ""
    page.evaluate("async () => { await new Promise(requestAnimationFrame); await new Promise(requestAnimationFrame); }")
    assert page.locator('[data-ff-pool-request-id]').input_value() == current_b
    assert page.locator('[data-ff-operation-receipt]').count() == 0
    assert page.get_by_role("button", name="Подтвердить проведение", exact=True).is_enabled()
    assert len(confirmations) == 8
    # Even successful HTTP responses must prove the currently selected identity on GET and confirm recovery.
    controls["mismatch_identity"] = manual
    page.locator('[data-ff-pool-request-load]').click()
    page.get_by_text("Не удалось проверить сведения этого документа. Обновите статус.", exact=True).wait_for()
    assert page.locator('[data-ff-operation-receipt]').count() == 0
    assert page.locator('[data-ff-pool-request-id]').input_value() == current_b
    controls["mismatch_identity"] = ""
    page.locator('[data-ff-pool-request-load]').click()
    page.get_by_role("button", name="Подтвердить проведение", exact=True).wait_for()
    controls["mismatch_identity"] = manual
    page.get_by_role("button", name="Подтвердить проведение", exact=True).click()
    page.get_by_text("Проверяем сохранение", exact=True).wait_for()
    assert page.locator('[data-ff-operation-receipt]').count() == 0
    assert page.evaluate("localStorage.getItem('wb.ffPool.overheadConfirmation.v1')") == current_b
    controls["mismatch_identity"] = ""
    page.get_by_role("button", name="Проверить статус", exact=True).click()
    receipt.wait_for()
    assert receipt.get_attribute("data-ff-operation-receipt") == current_b
    assert len(confirmations) == 9
    # Original client aliases can resolve a canonical accepted identity, without trusting unrelated IDs.
    page.locator('[data-ff-pool-tab="create"]').click()
    page.locator('[data-ff-pool-request-id]').evaluate("node => { node.value = 'browser-client-alias'; }")
    page.locator('[data-ff-pool-tab="workflow"]').click()
    page.wait_for_function("identity => document.querySelector('[data-ff-operation-receipt]')?.dataset.ffOperationReceipt === identity", arg=manual)
    assert receipt.get_attribute("data-ff-operation-receipt") == manual
    assert page.locator('[data-ff-pool-request-id]').input_value() == manual
    assert len(confirmations) == 9
    # The real FF host binds domain as well as same-ID readback. W1 cannot use
    # the narrow domain-less legacy pool_overhead compatibility above.
    page.locator('[data-ff-pool-tab="create"]').click()
    page.locator('[data-ff-pool-amount]').fill("73.50")
    page.locator('[data-ff-pool-preview]').click()
    page.get_by_role("button", name="Подтвердить проведение", exact=True).wait_for()
    native_id=page.locator('[data-ff-pool-request-id]').input_value()
    previews[native_id]['document_kind']='pool_reallocation'
    controls['receipt_domain']='foreign_domain'
    before_count=len(confirmations)
    page.get_by_role("button", name="Подтвердить проведение", exact=True).click()
    page.get_by_text("Проверяем сохранение", exact=True).wait_for()
    assert page.locator('[data-ff-operation-receipt]').count()==0
    assert page.evaluate("localStorage.getItem('wb.ffPool.overheadConfirmation.v1')")==native_id
    controls['receipt_domain']=None # domain missing is invalid for W1, same operation ID
    page.get_by_role("button", name="Проверить статус", exact=True).click()
    page.get_by_text("Проверяем сохранение", exact=True).wait_for()
    assert page.locator('[data-ff-operation-receipt]').count()==0
    controls['receipt_domain']='ff_pool_document';controls['receipt_source_domain']='foreign_domain'
    page.get_by_role("button", name="Проверить статус", exact=True).click()
    page.get_by_text("Проверяем сохранение", exact=True).wait_for()
    assert page.locator('[data-ff-operation-receipt]').count()==0
    controls['receipt_source_domain']='ff_pool_document'
    page.get_by_role("button", name="Проверить статус", exact=True).click()
    receipt.wait_for();assert receipt.get_attribute('data-ff-operation-receipt')==native_id
    assert len(confirmations)==before_count+1
    assert reads[-1]==native_id
    # A same-ID journal detail also rejects explicit foreign domain.
    accepted[native_id]['domain']='ff_pool_document'
    page.locator('[data-ff-pool-tab="journal"]').click()
    row=page.locator('[data-ff-operations-list] [data-ff-operation-id="'+native_id+'"]')
    row.wait_for();accepted[native_id]['domain']='foreign_domain'
    row.get_by_role('button',name='Подробнее').click()
    detail.get_by_text('Сведения временно недоступны. Документ не нужно отправлять повторно.',exact=True).wait_for()
    assert detail.locator('.ff-operation-accepted').count()==0
    accepted[native_id]['domain']='ff_pool_document'
    row.get_by_role('button',name='Подробнее').click()
    detail.locator('.ff-operation-accepted').wait_for()
    assert len(confirmations)==before_count+1
    # Each W2 family uses the real host's same-ID read-only recovery after a
    # lost confirmation response. Green certifies durable source, not movement.
    for kind in ('pool_inventory','correction','storno','late_expense'):
        page.locator('[data-ff-pool-tab="create"]').click()
        page.locator('[data-ff-pool-action-kind]').select_option('pool_overhead')
        page.locator('[data-ff-pool-amount]').fill('87.25')
        page.locator('[data-ff-pool-preview]').click()
        page.get_by_role('button',name='Подтвердить проведение',exact=True).wait_for()
        identity=page.locator('[data-ff-pool-request-id]').input_value()
        previews[identity]['document_kind']=kind
        controls['receipt_domain']='ff_pool_document';controls['receipt_source_domain']=None
        controls['lose_confirm']=True
        before_count=len(confirmations)
        page.get_by_role('button',name='Подтвердить проведение',exact=True).click()
        receipt.wait_for()
        assert receipt.get_attribute('data-ff-operation-receipt')==identity
        assert len(confirmations)==before_count+1 and reads[-1]==identity
        assert accepted[identity]['document'] is None
        controls['lose_confirm']=False
    assert not errors, errors
    context.close()


if __name__ == "__main__":
    main()
