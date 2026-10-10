"""Actual browser checks for the shared, presentation-only receipt component.

The page, callbacks and responses are synthetic. No application DB, mutation or
network request is used; the browser's fixture navigation is intercepted locally.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse
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
        mode:'inline',
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

    for processing, label in [({"kind":"source_only"}, "Сохранено"), ({"kind":"cost_history", "complete":True}, "Обработано")]:
        page.evaluate("processing => render('first', {...receipt,state:'completed',primary_effect:'source_saved',processing})", processing)
        assert first.locator(".ff-operation-status").inner_text() == label

    for effect in ("external_command", "external_job"):
        page.evaluate("""effect => render('first', {...receipt,primary_effect:effect,state:'processing',children:[
          {nm_id:101,parameter_field:'price',external_confirmed:true,outcome:'ambiguous'},
          {nm_id:102,parameter_field:'bid',external_confirmed:false,outcome:'submitted'},
          {nm_id:103,parameter_field:'bid',external_confirmed:false,outcome:'ambiguous'},
          {nm_id:104,parameter_field:'price',external_confirmed:false,outcome:'failed'},
          {nm_id:'<img src=x>',parameter_field:'price',external_confirmed:false,outcome:'rejected'},null
        ]})""", effect)
        assert first.locator('.ff-operation-children li').all_inner_texts() == [
            'SKU 101: price — Подтверждено WB', 'SKU 102: bid — Ожидает WB',
            'SKU 103: bid — Результат неизвестен', 'SKU 104: price — Ошибка',
            'SKU <img src=x>: price — Отклонено'], effect
        assert first.locator('img,script').count() == 0

    for override in [{"state": "draft"}, {"status": "draft"}, {"primary_effect": "preview"},
                     {"source_state": "staged"}, {"durable_saved": False}, {"operation_id": ""},
                     {"operation_id": " native-1"}, {"accepted_at": ""}, {"state": "toString"}]:
        page.evaluate("override => render('first', {...receipt,...override})", override)
        assert first.locator(".ff-operation-check").count() == 0
        assert first.locator("[data-ff-operation-receipt]").count() == 0
    assert page.evaluate("OperatorAcceptance.acceptedOperation(null)") is False
    assert page.evaluate("OperatorAcceptance.acceptedOperation([])") is False

    # Financial batch children are source receipts, not WB commands. External
    # command/job children may show provider confirmation only when proven.
    children = [{"label_ru": "Операция 1", "external_confirmed": True},
                {"label_ru": "Операция 2", "outcome": "ambiguous"}]
    for effect in ["source_saved", "external_command", "external_job"]:
        page.evaluate("o => render('first', {...receipt,...o})",
                      {"primary_effect": effect, "children": children})
        if effect == "source_saved":
            assert first.locator('.ff-operation-children').count() == 0
            assert first.get_by_text('Подтверждено WB', exact=False).count() == 0
        else:
            assert first.locator('.ff-operation-children li').all_text_contents() == [
                'Операция 1 — Подтверждено WB', 'Операция 2 — Результат неизвестен']

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


def modal_checks(browser) -> None:
    page = browser.new_page(viewport={"width": 1000, "height": 800})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    shell = '<body style="height:6000px"><button id="outside">Outside</button><iframe id="middle" src="/middle" style="height:5000px;width:900px"></iframe></body>'
    middle = '<body style="height:5000px"><iframe id="supplier" src="/supplier" style="height:4000px;width:850px"></iframe></body>'
    source = '<body style="height:4000px"><button id="confirm" style="margin-top:1600px">Confirm</button><div id="receipt"></div></body>'
    page.route("http://operator.test/**", lambda route: route.fulfill(status=200,content_type="text/html",body={"/shell":shell,"/middle":middle,"/supplier":source}[urlparse(route.request.url).path]))
    page.goto("http://operator.test/shell")
    page.add_style_tag(path=str(ROOT / "packages/adapters/templates/sheet_vitrina_v1_ui_system.css"))
    child = page.frame(url="http://operator.test/supplier")
    assert child is not None
    child.add_script_tag(path=str(ASSET))
    child.evaluate("""() => {window.saved={durable_saved:true,operation_id:'modal-native',accepted_at:'2026-10-10T10:00:00Z',state:'accepted',title_ru:'Document',journal_path:'/journal'};
      window.modalClosed=[];window.journals=[];window.show=value=>OperatorAcceptance.renderReceipt(document.getElementById('receipt'),value,{onClose:r=>modalClosed.push(r.operation_id),onJournal:r=>journals.push(r.operation_id)});
      document.getElementById('confirm').onclick=()=>{document.getElementById('confirm').disabled=true;show(saved);document.getElementById('confirm').disabled=false;};
      window.scrollTo(0,1400);document.getElementById('confirm').focus({preventScroll:true});}""")
    page.evaluate("window.scrollTo(0,2200)")
    scroll = page.evaluate("scrollY")
    child_scroll = child.evaluate("scrollY")
    child.evaluate("document.getElementById('confirm').click()")
    dialog=page.locator('dialog.ff-operation-popup[open]')
    assert dialog.count()==1 and child.locator('dialog').count()==0
    assert child.evaluate("document.getElementById('receipt').parentNode.tagName")=='BODY'
    assert child.evaluate("document.getElementById('receipt').ownerDocument===document") is True
    box=dialog.bounding_box();assert box and abs(box['x']+box['width']/2-500)<2 and abs(box['y']+box['height']/2-400)<2,box
    child.evaluate("""() => {const node=document.getElementById('receipt');const list=document.createElement('ul');list.textContent='child-document: saved';node.append(list);const link=document.createElement('a');link.href='/next';link.textContent='Следующая операция';node.append(link);node.querySelector('.ff-operation-status').textContent='Source complete';}""")
    from playwright.sync_api import expect
    expect(dialog).to_contain_text('child-document: saved')
    expect(dialog).to_contain_text('Source complete')
    expect(dialog.get_by_role('link',name='Следующая операция')).to_have_attribute('href','/next')
    assert page.evaluate("scrollY")==scroll and child.evaluate("scrollY")==child_scroll
    # Source lookup and exact-operation refresh remain possible while the host
    # dialog is open; one active popup, no extra submit/read is introduced.
    dialog.get_by_role('link',name='Журнал операций').focus()
    child.evaluate("show({...saved,state:'processing'})")
    expect(dialog).to_contain_text('Обрабатывается')
    assert page.evaluate("document.activeElement.textContent")=='Журнал операций'
    dialog.get_by_role('button',name='Закрыть').focus()
    child.evaluate("show({...saved,state:'delayed'})")
    expect(dialog).to_contain_text('Обработка задерживается')
    assert page.evaluate("document.activeElement.textContent")=='Закрыть'
    child.evaluate("const link=document.createElement('a');link.href='/next';link.textContent='Следующая операция';document.getElementById('receipt').append(link)")
    dialog.get_by_role('link',name='Следующая операция').focus()
    child.evaluate("document.getElementById('receipt').querySelector('.ff-operation-status').textContent='Refreshed status'")
    expect(dialog).to_contain_text('Refreshed status')
    assert page.evaluate("document.activeElement.getAttribute('href')")=='/next'
    child.evaluate("show({...saved,state:'processing'});const link=document.createElement('a');link.href='/next';link.textContent='Следующая операция';document.getElementById('receipt').append(link)")
    expect(dialog.get_by_role('link',name='Следующая операция')).to_be_focused()
    dialog.locator('.ff-operation-receipt').focus()
    assert dialog.count()==1
    page.keyboard.press('Tab')
    assert page.evaluate("document.activeElement.textContent")=='Закрыть'
    page.keyboard.press('Tab')
    assert page.evaluate("document.activeElement.textContent")=='Журнал операций'
    page.keyboard.press('Tab')
    assert page.evaluate("document.activeElement.closest('dialog')!==null") is True
    page.keyboard.press('Escape')
    expect(dialog).to_have_count(0)
    assert child.evaluate("modalClosed")==['modal-native']
    assert child.evaluate("document.activeElement.id")=='confirm'
    assert page.evaluate("scrollY")==scroll and child.evaluate("scrollY")==child_scroll
    child.evaluate("show({...saved,state:'completed'})")
    assert dialog.count()==0  # polling the dismissed operation cannot reopen it
    child.evaluate("show({...saved,operation_id:'journal-native'})")
    dialog.get_by_role('link',name='Журнал операций').click()
    assert dialog.count()==0 and child.evaluate("journals")==['journal-native']
    assert child.evaluate("modalClosed")==['modal-native']
    child.evaluate("show({...saved,operation_id:'journal-native',state:'completed'})")
    assert dialog.count()==0 and child.locator('#receipt').is_hidden()
    child.evaluate("show({...saved,operation_id:'dismissed-B'})")
    dialog.get_by_role('button',name='Закрыть').click()
    child.evaluate("show({...saved,operation_id:'modal-native',state:'completed'})")
    assert dialog.count()==0
    child.evaluate("show({...saved,operation_id:'invalid-native'})")
    child.evaluate("show({...saved,durable_saved:false})")
    assert dialog.count()==0 and page.locator('.ff-operation-check').count()==0
    child.evaluate("show({...saved,operation_id:'removed-frame-native'})")
    page.evaluate("document.getElementById('middle').remove()")
    expect(dialog).to_have_count(0)
    # Existing native modal remains its owner; the common renderer never nests
    # a second dialog or steals its caller's .showModal()/.close() lifecycle.
    page.add_script_tag(path=str(ASSET))
    page.evaluate("""() => {const d=document.createElement('dialog');d.id='native';document.body.append(d);OperatorAcceptance.renderReceipt(d,{durable_saved:true,operation_id:'native-dialog',accepted_at:'now',state:'accepted'},{onClose:()=>d.close()});d.showModal();}""")
    assert page.locator('dialog[open]').count()==1 and page.locator('.ff-operation-popup').count()==0
    page.locator('#native').get_by_role('button',name='Закрыть').click()
    assert page.locator('dialog[open]').count()==0 and not errors,errors
    # Recovery can finish while its native owner is hidden. The visible popup
    # must acknowledge the exact operation without opening/moving that owner.
    for hidden_style in ('hidden', 'style="display:none"'):
        page.set_content('<button id="next">Next</button><div id="owner" '+hidden_style+'><div id="receipt"></div></div>')
        page.add_script_tag(path=str(ASSET))
        page.evaluate("""() => {window.hiddenOwnerCloses=[];window.saved={durable_saved:true,operation_id:'hidden-native',accepted_at:'now',state:'accepted'};
          window.show=value=>OperatorAcceptance.renderReceipt(document.getElementById('receipt'),value,{onClose:r=>hiddenOwnerCloses.push(r.operation_id)});show(saved);}""")
        dialog=page.get_by_role('dialog',name='Принято',exact=True)
        expect(dialog.locator('.ff-operation-check')).to_be_visible()
        expect(dialog.locator('[data-ff-operation-receipt]')).to_have_attribute('data-ff-operation-receipt','hidden-native')
        assert page.evaluate("document.getElementById('receipt').parentElement.id")=='owner'
        page.evaluate("show({...saved,state:'processing'})")
        expect(dialog).to_contain_text('Обрабатывается')
        assert page.locator('dialog[open]').count()==1
        dialog.get_by_role('button',name='Закрыть',exact=True).click()
        expect(dialog).to_have_count(0)
        assert page.evaluate('hiddenOwnerCloses')==['hidden-native']
        page.get_by_role('button',name='Next',exact=True).click()
        page.evaluate("show({...saved,state:'completed'})")
        expect(dialog).to_have_count(0)
        page.evaluate("show({...saved,operation_id:'removed-owner'});document.getElementById('owner').remove()")
        expect(dialog).to_have_count(0)
    assert not errors,errors
    template=(ROOT/'packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html').read_text()
    actions=template.split('<div class="shell-actions">',1)[1].split('</div>',1)[0]
    page.set_content('<div class="shell-actions">'+actions+'</div>')
    link=page.locator('[data-operator-journal-link]')
    expect(link).to_be_visible()
    assert link.inner_text()=='Журнал операций' and link.get_attribute('href')=='/sheet-vitrina-v1/operations'
    assert page.locator('.ff-operation-check').count()==0
    page.close()


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
            modal_checks(browser)
            assert not errors, errors
            assert requests == [("GET", "http://operator.test/forms")], requests
        finally:
            browser.close()
    print("operator_acceptance_component_smoke: OK (actual Chromium; durable pending/processed, "
          "unknown/exact identity/domain, draft, safe text/link, keyboard callbacks, independent forms; no submits)")


if __name__ == "__main__":
    main()
