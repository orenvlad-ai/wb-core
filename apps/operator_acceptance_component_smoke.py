"""Actual browser checks for the shared, presentation-only receipt component.

The page, callbacks and responses are synthetic. No application DB, mutation or
network request is used; the browser's fixture navigation is intercepted locally.
"""
from __future__ import annotations

from pathlib import Path
import sys

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ASSET = ROOT / "packages/adapters/templates/sheet_vitrina_v1_operator_acceptance.js"
FIXTURE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<style>
body { font: 16px sans-serif; }
.ff-operation-check { color: green; }
.ff-operation-actions { display: flex; gap: 16px; }
.ff-operation-summary dd { margin: 0; }
</style></head><body><div id="first"></div><div id="second"></div></body></html>"""


def checks(page) -> None:
    page.evaluate("""() => {
      window.receipt = {durable_saved:true, operation_id:'native-1', request_id:'client-alias',
        accepted_at:'2026-10-08T10:00:00Z', state:'accepted', physical_applied:false,
        document:null, title_ru:'Приёмка поставки', fields:[{label:'Поставка',value:'26 GN 999'}]};
      window.calls = [];
      window.render = (id, value) => OperatorAcceptance.renderReceipt(document.getElementById(id), value, {
        onClose:r => calls.push(['close',id,r.operation_id]),
        onJournal:r => calls.push(['journal',id,r.operation_id])
      });
      render('first', receipt);
    }""")
    first = page.locator("#first")
    assert first.locator(".ff-operation-check").inner_text() == "✓"
    assert first.locator("h3").inner_text() == "Принято"
    assert first.get_by_text("Документ сохранён.", exact=True).count() == 1
    assert first.locator(".ff-operation-title").inner_text() == "Приёмка поставки"
    assert first.locator("dd").inner_text() == "26 GN 999"
    assert page.evaluate("document.activeElement.dataset.ffOperationReceipt") == "native-1"
    assert page.evaluate("OperatorAcceptance.acceptedOperation(receipt)") is True
    # Acceptance remains true even though physical posting has not happened.
    assert page.evaluate("receipt.physical_applied === false && receipt.document === null") is True

    labels = {"accepted": "Ожидает обработки", "processing": "Обрабатывается", "completed": "Обработано",
              "delayed": "Обработка задерживается", "needs_attention": "Требует внимания"}
    for state, label in labels.items():
        page.evaluate("state => render('first', {...receipt,state})", state)
        assert first.locator("h3").inner_text() == "Принято"
        assert first.locator(".ff-operation-status").inner_text() == label
        assert first.locator(".ff-operation-status").get_attribute("data-state") == (
            "processed" if state == "completed" else state)
        page.evaluate("state => OperatorAcceptance.renderState(document.getElementById('second'), {...receipt,state})", state)
        assert page.locator("#second .ff-operation-status").inner_text() == label
        assert page.locator("#second .ff-operation-check").count() == 0

    for override in [{"state": "draft"}, {"status": "draft"}, {"primary_effect": "preview"},
                     {"source_state": "staged"}, {"durable_saved": False}, {"operation_id": ""},
                     {"operation_id": " native-1"}, {"accepted_at": ""}, {"state": "toString"}]:
        page.evaluate("override => render('first', {...receipt,...override})", override)
        assert first.locator(".ff-operation-check").count() == 0
        assert first.locator("[data-ff-operation-receipt]").count() == 0
    assert page.evaluate("OperatorAcceptance.acceptedOperation(null)") is False
    assert page.evaluate("OperatorAcceptance.acceptedOperation([])") is False

    readback = page.evaluate("""async () => {
      const api = OperatorAcceptance;
      let reads = 0;
      const read = value => async () => { reads++; return value; };
      const ref = {operation_id:'native-1',domain:'ff_pool_document'};
      const bound = {...receipt,domain:'ff_pool_document'};
      const results = [];
      results.push(await api.readSameOperation(ref, read(bound)));
      results.push(await api.readSameOperation(ref, read({acceptance:bound})));
      results.push(await api.readSameOperation(ref, read({operation:bound})));
      results.push(await api.readSameOperation(ref, read({...receipt,source_ref:{domain:'ff_pool_document'}})));
      results.push(await api.readSameOperation('native-1', read(receipt)));
      results.push(await api.readSameOperation(ref, read({...bound,operation_id:'foreign'})));
      results.push(await api.readSameOperation(ref, read({...bound,domain:'cash'})));
      results.push(await api.readSameOperation(ref, read(receipt)));
      results.push(await api.readSameOperation(ref, read({...bound,source_ref:{domain:'cash'}})));
      results.push(await api.readSameOperation(ref, read({operation:bound,acceptance:bound})));
      results.push(await api.readSameOperation(ref, read({operation:bound,operation_id:'native-1'})));
      results.push(await api.readSameOperation(ref, read({...bound,status:'draft'})));
      results.push(await api.readSameOperation(ref, read('not JSON')));
      results.push(await api.readSameOperation(ref, async () => { reads++; throw Error('lost response'); }));
      results.push(await api.readSameOperation({operation_id:''}, read(bound)));
      results.push(await api.readSameOperation(ref, null));
      return {results,reads};
    }""")
    assert readback["reads"] == 14, readback
    assert all(row["status"] == "accepted" and row["operation"]["operation_id"] == "native-1"
               for row in readback["results"][:5])
    assert all(row["status"] == "unknown" and row["operation"] is None
               for row in readback["results"][5:])
    mutation = page.evaluate("""async () => {
      const ref={operation_id:'native-1',domain:'ff_pool_document'};
      return OperatorAcceptance.readSameOperation(ref, async () => {
        await Promise.resolve(); ref.operation_id='foreign';
        return {...receipt,operation_id:'foreign',domain:'ff_pool_document'};
      });
    }""")
    assert mutation['status']=='unknown' and mutation['reason_code']=='operation_mismatch', mutation
    # A malformed or lost readback gives an unknown display, never green.
    page.evaluate("""async () => {
      const value = await OperatorAcceptance.readSameOperation('native-1', async () => ({operation:{...receipt,operation_id:'wrong'}}));
      OperatorAcceptance.renderReceipt(document.getElementById('first'), value.operation);
    }""")
    assert first.get_by_text("Проверяем сохранение", exact=True).count() == 1
    assert first.locator(".ff-operation-check").count() == 0

    hostile = '<img src=x onerror="window.hostileRan=true"><script>window.hostileRan=true</script>'
    page.evaluate("""hostile => {
      render('first', {...receipt,title_ru:hostile,reason_ru:hostile,
        fields:[{label:hostile,value:hostile},{label:'Ноль',value:0},{label:'Нет',value:false}]});
    }""", hostile)
    assert first.locator(".ff-operation-title").inner_text() == hostile
    assert first.locator("dt").first.inner_text() == hostile
    assert first.locator("dd").first.inner_text() == hostile
    assert first.locator("dd").nth(1).inner_text() == "0"
    assert first.locator("dd").nth(2).inner_text() == "false"
    assert first.locator("img,script").count() == 0
    assert page.evaluate("window.hostileRan === undefined") is True
    unsafe = ["javascript:alert(1)", "https://evil.invalid/", "//evil.invalid/", "/\\evil.invalid",
              "/%5cevil.invalid", "/path\nnext", " /path", "/path with space", "data:text/html,bad"]
    for value in unsafe:
        node = page.evaluate("""value => {
          const link = OperatorAcceptance.journalLink({...receipt,detail_path:value});
          return {tag:link.tagName,href:link.getAttribute('href'),disabled:link.getAttribute('aria-disabled')};
        }""", value)
        assert node == {"tag": "SPAN", "href": None, "disabled": "true"}, (value, node)
    assert page.evaluate("""() => OperatorAcceptance.journalLink({...receipt,
      detail_path:'/sheet-vitrina-v1/vitrina?operation_id=native-1'}).getAttribute('href')""") == (
        "/sheet-vitrina-v1/vitrina?operation_id=native-1")

    # Two independent form containers: focus, keyboard activation and callbacks
    # retain their own native identity without module-wide active operation state.
    page.evaluate("""() => {
      calls.length = 0;
      render('first', {...receipt,operation_id:'first-native'});
      render('second', {...receipt,operation_id:'second-native'});
    }""")
    assert page.evaluate("document.activeElement.dataset.ffOperationReceipt") == "second-native"
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.textContent") == "Закрыть"
    page.keyboard.press("Enter")
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.textContent") == "Журнал операций"
    page.keyboard.press("Enter")
    assert page.evaluate("calls") == [["close", "second", "second-native"], ["journal", "second", "second-native"]]
    assert page.url == "http://operator.test/forms"
    first.get_by_role("button", name="Закрыть").click()
    first.get_by_role("link", name="Журнал операций").click()
    assert page.evaluate("calls")[-2:] == [["close", "first", "first-native"], ["journal", "first", "first-native"]]
    assert first.locator("[data-ff-operation-receipt]").get_attribute("data-ff-operation-receipt") == "first-native"
    assert page.locator("#second [data-ff-operation-receipt]").get_attribute("data-ff-operation-receipt") == "second-native"


def main() -> None:
    errors = []
    requests = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1000, "height": 800})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: requests.append((request.method, request.url)))
            page.route("http://operator.test/**", lambda route: route.fulfill(
                status=200, content_type="text/html", body=FIXTURE))
            page.goto("http://operator.test/forms")
            page.add_script_tag(path=str(ASSET))
            checks(page)
            assert not errors, errors
            assert requests == [("GET", "http://operator.test/forms")], requests
        finally:
            browser.close()
    print("operator_acceptance_component_smoke: OK (actual Chromium; durable pending/processed, "
          "unknown/exact identity/domain, draft, safe text/link, keyboard callbacks, independent forms; no submits)")


if __name__ == "__main__":
    main()
