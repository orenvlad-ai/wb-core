"""Metric-settings draft, row allowlist and persistence checks on synthetic localhost data."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import os
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.sheet_vitrina_v1_web_vitrina_user_config_browser_smoke import (
    FixtureServer, _build_composition, _wait_for_server_save_count,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
)
from packages.application.registry_upload_http_entrypoint import _sanitize_web_vitrina_metric_presentation_config


def check_sanitizer():
    source = {"version": 5, "presentation": {"order": ["same::a"], "display": {"same::a": "hidden"}, "manual": True},
              "row_metric_allowlists": {"group": ["same::a", "same::a", "", "x" * 401], "sku": "bad", "total": ["same::a"]}}
    result = _sanitize_web_vitrina_metric_presentation_config(source)
    assert result["row_metric_allowlists"] == {"group": ["same::a"], "sku": []}
    assert result["presentation"] == source["presentation"]
    source.pop("row_metric_allowlists")
    assert _sanitize_web_vitrina_metric_presentation_config(source)["row_metric_allowlists"] == {"group": [], "sku": []}
    source["row_metric_allowlists"] = {"sku": [f"same::{i}" for i in range(600)]}
    assert len(_sanitize_web_vitrina_metric_presentation_config(source)["row_metric_allowlists"]["sku"]) == 500


def composition_with_groups(directory):
    composition = _build_composition(directory)
    composition["shell_format"] = "metadata_v2"
    composition["response_schema_version"] = 2
    table = composition["table_surface"]
    group_rows = []
    for source in table["rows"]:
        if source["row_kind"] != "total":
            continue
        row = deepcopy(source)
        row.update(row_id="GROUP:fixture|" + source["row_id"], row_kind="group", group_id="group:fixture")
        for key, value in {"scope_kind": "GROUP", "scope_key": "GROUP:fixture", "scope_label": "Группа", "group": "Тестовая группа"}.items():
            row["values"][key].update(value=value, display_text=value)
        row["filter_tokens"].update(row_kind=["group"], scope_kind=["GROUP"], group=["Тестовая группа"])
        group_rows.append(row)
    table["rows"].extend(group_rows)
    return composition


def run_browser(server, evidence):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/*.css", lambda route: route.fulfill(path=str(ROOT / "packages/adapters/templates/sheet_vitrina_v1_ui_system.css"), content_type="text/css"))
        page.goto(server.base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH)
        page.wait_for_selector('[data-table-shell]:not(.is-hidden)')
        page.wait_for_function('state.metricPresentation.serverConfig.saveInFlight === false && state.metricPresentation.serverConfig.savePending === false')
        baseline = page.evaluate('buildMetricPresentationPersistedPayload()')
        assert baseline["row_metric_allowlists"] == {"group": [], "sku": []}
        ids = page.evaluate("state.metricPresentation.logicalMetrics.filter(r=>r.totalKey && r.skuKey && unifiedMetricDisplayStatus(r.logicalId)==='shown').slice(0,3).map(r=>({id:r.logicalId,total:r.totalKey,sku:r.skuKey}))")
        assert len(ids) == 3
        first, second, third = ids
        def open_modal():
            page.locator('[data-metrics-settings-open]').click()
            assert page.locator('[data-metric-list-filter]').input_value() == 'except_hidden'
        def row(item):
            return page.locator('[data-metric-config-row]').filter(has=page.locator(f'[data-metric-display-select][data-metric-config-key="{item["id"]}"]'))
        def checkbox(item, level):
            return row(item).locator(f'[data-metric-level-checkbox="{level}"]')
        def applied():
            return page.evaluate('buildMetricPresentationPersistedPayload()')
        def save():
            count = server.save_count
            page.locator('[data-metrics-settings-save]').click()
            _wait_for_server_save_count(server, count + 1)
        def visible_keys(level):
            return page.evaluate("kind=>[...new Set(applyMetricPresentationToRows(state.composition.table_surface.rows).filter(r=>r.row_kind===kind).map(r=>cellValue(r,'metric_key')))]", level)
        before_keys = {kind: visible_keys(kind) for kind in ['total','group','sku']}
        assert all(before_keys.values()), before_keys
        open_modal()
        assert page.locator('[data-metric-level-clear]').count() == 2
        assert not page.locator('[data-metric-level-checkbox]:checked').count()
        assert not page.locator('[data-metric-config-row][data-metric-display-status="hidden"]').count()
        count = server.save_count
        checkbox(first, 'group').check()
        assert 'несохранённые изменения' in page.locator('[data-metrics-presentation-summary]').inner_text()
        checkbox(second, 'sku').check()
        row(third).locator('[data-metric-display-select]').select_option('hidden')
        page.locator('[data-metric-list-filter]').select_option('only_hidden')
        assert page.locator('[data-metric-config-row]:not([data-metric-display-status="hidden"])').count() == 0
        assert applied() == baseline and server.save_count == count
        # Cancel, Escape, backdrop and close all discard the draft.
        page.locator('[data-metrics-settings-close]').last.click()
        for close in ['close', 'escape', 'backdrop']:
            open_modal();checkbox(first, 'group').check()
            if close == 'close': page.locator('[data-metrics-settings-close]').first.click()
            elif close == 'escape': page.keyboard.press('Escape')
            else: page.locator('[data-metrics-presentation]').click(position={"x":2,"y":2})
            assert applied() == baseline and server.save_count == count
        open_modal();checkbox(first, 'group').check();checkbox(second, 'sku').check()
        # While modal is filtered, column clear affects the entire level.
        page.locator('[data-metric-list-filter]').select_option('only_hidden')
        page.locator('[data-metric-level-clear="group"]').click()
        assert page.evaluate('state.metricPresentation.draft.rowMetricAllowlists') == {'group': [], 'sku': [second['sku']]}
        page.locator('[data-metric-list-filter]').select_option('all')
        checkbox(first, 'group').check()
        row(third).locator('[data-metric-display-select]').select_option('hidden')
        # Selecting hidden metrics does not override the separate global hidden state.
        checkbox(third, 'group').check();checkbox(third, 'sku').check()
        assert visible_keys('group') == before_keys['group'] and visible_keys('sku') == before_keys['sku']
        save()
        assert visible_keys('total') == [key for key in before_keys['total'] if key != third['total']]
        assert visible_keys('group') == [first['total']]
        assert visible_keys('sku') == [second['sku']]
        saved = deepcopy(server.user_config['config'])
        assert saved['row_metric_allowlists'] == {'group': [first['total'],third['total']], 'sku': [second['sku'],third['sku']]}
        assert saved['sku_metric_selection'] == baseline['sku_metric_selection']
        assert saved['sku_presets'] == baseline['sku_presets']
        assert saved['presentation']['display'][third['id']] == 'hidden'
        page.reload();page.wait_for_selector('[data-table-shell]:not(.is-hidden)');open_modal()
        assert checkbox(first,'group').is_checked() and checkbox(second,'sku').is_checked()
        page.locator('[data-metric-list-filter]').select_option('all')
        assert checkbox(third,'group').is_checked() and checkbox(third,'sku').is_checked()
        # Header icons restore all available rows for only their own level.
        page.locator('[data-metric-list-filter]').select_option('only_hidden')
        page.locator('[data-metric-level-clear="group"]').click();save()
        assert visible_keys('group') == [key for key in before_keys['group'] if key != third['total']]
        assert visible_keys('sku') == [second['sku']]
        open_modal();page.locator('[data-metric-level-clear="sku"]').click();save()
        assert visible_keys('sku') == [key for key in before_keys['sku'] if key != third['sku']]
        assert applied()['presentation']['display'][third['id']] == 'hidden'
        # Uncheck the final member = all available, including members not in the list view.
        open_modal();checkbox(first,'group').check();checkbox(first,'group').uncheck();save()
        assert applied()['row_metric_allowlists']['group'] == []
        # Reset requires confirmation. Dismissal leaves draft/applied/server unchanged.
        open_modal();checkbox(first,'group').check()
        draft_before = page.evaluate('JSON.stringify(state.metricPresentation.draft)')
        applied_before = applied();count = server.save_count
        page.once('dialog',lambda dialog: dialog.dismiss())
        page.locator('[data-metrics-settings-reset]').click()
        assert page.evaluate('JSON.stringify(state.metricPresentation.draft)') == draft_before
        assert applied() == applied_before and server.save_count == count
        page.once('dialog',lambda dialog: dialog.accept())
        page.locator('[data-metrics-settings-reset]').click()
        assert page.evaluate('state.metricPresentation.draft.rowMetricAllowlists') == {'group': [], 'sku': []}
        assert applied() == applied_before and server.save_count == count
        page.locator('[data-metrics-settings-close]').last.click()
        assert applied() == applied_before
        open_modal();page.once('dialog',lambda dialog: dialog.accept());page.locator('[data-metrics-settings-reset]').click();save()
        assert applied()['row_metric_allowlists'] == {'group': [], 'sku': []}
        assert applied()['presentation'] == baseline['presentation']
        assert applied()['sku_metric_selection'] == applied_before['sku_metric_selection']
        assert applied()['sku_presets'] == baseline['sku_presets']
        # A shown, B shown, C collapsed; only A+C selected. The group/SKU
        # disclosure must anchor C to A, while the overall total anchors to B.
        open_modal();checkbox(first,'group').check();checkbox(third,'group').check()
        checkbox(first,'sku').check();checkbox(third,'sku').check()
        row(third).locator('[data-metric-display-select]').select_option('collapsed')
        page.evaluate("ids=>{const d=state.metricPresentation.draft;d.unifiedOrder=ids.concat(d.unifiedOrder.filter(id=>!ids.includes(id)));}",[item['id'] for item in ids])
        save()
        assert visible_keys('group') == [first['total']]
        assert visible_keys('sku') == [first['sku']]
        for level,key in [('group',first['total']),('sku',first['sku'])]:
            anchor=page.locator(f'[data-metric-anchor-toggle][data-metric-anchor-row-kind="{level}"][data-metric-anchor-key="{key}"]').first
            assert anchor.count() == 1
            anchor.click()
            assert third['total' if level=='group' else 'sku'] in visible_keys(level)
            # Expanded logical anchors remain shared by existing mode semantics.
            if level=='group':
                anchor.click()
        count=server.save_count
        page.evaluate('pruneExpandedMetricPresentationAnchors();persistMetricPresentation()')
        _wait_for_server_save_count(server,count+1)
        assert first['id'] in applied()['expanded_anchors']
        # Temporarily absent SKU blocks / pair-to-scope-only evolution retain the
        # stable allowlists. Restoring the full catalog keeps the same selection.
        expected_lists=applied()['row_metric_allowlists']
        page.evaluate("() => {const p=JSON.parse(JSON.stringify(state.composition));p.table_surface.rows=p.table_surface.rows.filter(r=>r.row_kind!=='sku');for(const c of p.filter_surface.controls || []){if(c.control_id==='metric'){c.options=c.options.filter(o=>o.scope_group_id!=='sku');}}initializeMetricPresentation(p,{preserveExistingPresentation:true});}")
        assert applied()['row_metric_allowlists']==expected_lists
        page.evaluate('initializeMetricPresentation(state.composition,{preserveExistingPresentation:true})')
        assert applied()['row_metric_allowlists']==expected_lists
        # An absent member is still a nonempty selection, never unrestricted.
        assert page.evaluate("metricAllowedForRowLevel('group','total','absent_metric')") is False
        conflict=page.evaluate("""() => {const base=buildMetricPresentationPersistedPayload();const local=JSON.parse(JSON.stringify(base));const remote=JSON.parse(JSON.stringify(base));local.row_metric_allowlists.group=[];remote.row_metric_allowlists.sku=['remote_metric'];return rebaseMetricPresentationConfig(base,local,remote).row_metric_allowlists;}""")
        assert conflict=={'group':[],'sku':['remote_metric']}
        open_modal();page.once('dialog',lambda dialog:dialog.accept());page.locator('[data-metrics-settings-reset]').click();save()
        # Filter never changes table selection, reset defaults are transient until Save.
        open_modal();page.locator('[data-metric-list-filter]').select_option('all')
        for unavailable in ['total','sku']:
            for node in page.locator(f'[data-metric-availability="{unavailable}"]').all():
                assert node.locator(f'[data-metric-level-checkbox="{"sku" if unavailable == "total" else "group"}"]').is_disabled()
        if evidence:
            evidence.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(evidence / 'metric-settings-desktop.png'))
        page.set_viewport_size({'width':390,'height':844})
        geometry=page.evaluate("() => {const dialog=document.querySelector('#metrics-presentation-dialog').getBoundingClientRect();const body=document.querySelector('[data-metrics-presentation-body]');return {left:dialog.left,right:dialog.right,top:dialog.top,bottom:dialog.bottom,scroll:body.scrollWidth>body.clientWidth};}")
        assert geometry['left'] >= 0 and geometry['right'] <= 391 and geometry['top'] >= 0 and geometry['bottom'] <= 845 and geometry['scroll'],geometry
        assert page.locator('[data-metric-level-clear]').count() == 2
        assert page.locator('[data-metrics-settings-save]').bounding_box()['x'] + page.locator('[data-metrics-settings-save]').bounding_box()['width'] <= 390
        if evidence: page.screenshot(path=str(evidence / 'metric-settings-mobile.png'))
        assert not errors,errors
        browser.close()
    print(json.dumps({'status':'pass','synthetic_localhost_only':True,'draft_cancel_escape_backdrop':True,'filter_only_modal':True,'independent_group_sku_allowlists':True,'overall_total_preserved':True,'empty_means_all':True,'hidden_not_unchecked':True,'column_clear_including_filtered_rows':True,'server_save_reload':True,'reset_confirmation_cancel_accept_save':True,'sku_picker_preferences_preserved':True,'desktop_mobile_geometry':True,'filtered_collapse_toggle_prune':True,'catalog_evolution_absent_keys':True,'concurrent_per_level_rebase':True}))


def main():
    check_sanitizer()
    source=(ROOT/'packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html').read_text()
    with TemporaryDirectory(prefix='wbc0146-metric-settings-') as temp:
        directory=Path(temp)
        for i, script in enumerate(re.findall(r'<script(?:\s[^>]*)?>(.*?)</script>',source,re.S)):
            path=directory/f'script-{i}.js';path.write_text(script);subprocess.run(['node','--check',str(path)],check=True)
        with FixtureServer(composition_with_groups(directory/'runtime')) as server:
            if '--serve' in sys.argv:
                import time
                print(server.base_url+DEFAULT_SHEET_WEB_VITRINA_UI_PATH,flush=True)
                while True: time.sleep(1)
            run_browser(server,Path(os.environ['WBC_METRIC_SETTINGS_EVIDENCE_DIR']) if os.environ.get('WBC_METRIC_SETTINGS_EVIDENCE_DIR') else None)


if __name__ == '__main__': main()
