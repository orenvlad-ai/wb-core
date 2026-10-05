"""Synthetic block-selection checks and an opt-in local UI fixture; no production data."""
from copy import deepcopy
from pathlib import Path
import json
import os
import re
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEMPLATE = ROOT / 'packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html'


def check_selection():
    source = TEMPLATE.read_text()
    def function(name, next_name):
        return source[source.index('    function ' + name):source.index('    function ' + next_name)]
    helpers = function('historyBlockGroups()', 'renderHistoryBlockFilters()')
    merge = function('historySnapshotMerged()', 'historyBlockIsCurrent(')
    loader = source[source.index('    async function loadSelectedHistoryBlocks('):source.index('    function renderSelectedHistoryBlocks()')]
    check_current = source[source.index('    function historyBlockIsCurrent('):source.index('    async function fetchHistoryBlock(')]
    script = '''const assert = require('node:assert/strict');
const state = {requestSequence: 7}; const HISTORY_CLIENT_BYTES=1000;
const groups=[{group_id:'group:clean',label:'Clean',sku_rows:5,total_available:true},
 {group_id:'group:nf',label:'NF',sku_rows:8,total_available:true},
 {group_id:'group:empty',label:'Empty',sku_rows:0,total_available:false}];
const historySnapshotState={requestId:7,viewId:3,summary:{history_snapshot:{reporting_groups:groups},table_surface:{rows:[]}},blocks:new Map()};
''' + helpers + merge + check_current + loader + '''
const h=historySnapshotState;
assert.deepEqual(normalizeHistoryBlockSelection(null,groups),{total:true,totals:[],skus:[]});
assert.deepEqual(normalizeHistoryBlockSelection({total:false,totals:['group:nf','unknown','group:empty'],skus:['group:clean','group:empty']},groups),{total:false,totals:['group:nf'],skus:['group:clean']});
let calls=[];
async function fetchHistoryBlock(scope,id){calls.push([scope,id]);return {rows:[{row_id:scope+id,row_kind:scope,group_id:id,section_id:'s'}],bytes:20};}
(async()=>{
 assert(await loadSelectedHistoryBlocks({total:true,totals:[],skus:[]},7,3));
 assert.deepEqual(calls,[['total','']]); // no group or SKU request by default
 calls=[];
 assert(await loadSelectedHistoryBlocks({total:true,totals:['group:nf'],skus:[]},7,3));
 assert.deepEqual(calls,[['group','group:nf']]); // existing total reused; group never fetches SKU
 calls=[];
 assert(await loadSelectedHistoryBlocks({total:true,totals:['group:nf'],skus:['group:clean','group:nf']},7,3));
 assert.deepEqual(calls,[['sku','group:clean'],['sku','group:nf']]);
 assert.deepEqual(historySnapshotMerged().table_surface.rows.map(r=>r.row_id),['total','skugroup:clean','groupgroup:nf','skugroup:nf']);
 const before=h.blocks;
 h.viewId=4;
 assert.equal(await loadSelectedHistoryBlocks({total:false,totals:[],skus:['group:clean']},7,3),false);
 assert.equal(h.blocks,before); // stale replies cannot publish selections
 h.viewId=3;calls=[];
 assert(await loadSelectedHistoryBlocks({total:false,totals:[],skus:[]},7,3));
 assert.equal(h.blocks.size,4);assert.equal(calls.length,0); // complete deselected data cached
 assert(await loadSelectedHistoryBlocks({total:true,totals:['group:nf'],skus:['group:clean','group:nf']},7,3));
 assert.equal(calls.length,0); // toggling complete groups does not fetch again
 console.log(JSON.stringify({status:'pass',default_total_only:true,group_without_sku:true,independent_selection:true,registry_order:true,stale_reply_guard:true}));
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    with tempfile.TemporaryDirectory(prefix='wbc-group-ui-') as temp:
        path = Path(temp) / 'selection.cjs'
        path.write_text(script)
        subprocess.run(['node', str(path)], check=True)
        for i, js in enumerate(re.findall(r'<script(?:\s[^>]*)?>(.*?)</script>', source, re.S)):
            path = Path(temp) / f'syntax-{i}.js'
            path.write_text(js)
            subprocess.run(['node', '--check', str(path)], check=True)
    assert 'role="dialog" aria-modal="true" aria-labelledby="block-filters-title"' in source
    assert 'data-block-master="all"' in source and 'data-block-master="sku"' in source


def check_complete_sku_pages():
    source = TEMPLATE.read_text()
    helpers = source[source.index('    function historyBlockGroups()'):source.index('    function renderHistoryBlockFilters()')]
    loader = source[source.index('    function historyBlockIsCurrent('):source.index('    function renderSelectedHistoryBlocks()')]
    page_loader = source[source.index('    async function historySnapshotPage('):source.index('    function historyBlockGroups()')]
    script = r'''const assert=require('node:assert/strict');
const state={requestSequence:7}; let HISTORY_CLIENT_BYTES=100000;
const groups=[{group_id:'group:clean',label:'Clean',sku_rows:710,total_available:true},
 {group_id:'group:matte',label:'Matte',sku_rows:568,total_available:true}];
const historySnapshotState={requestId:7,viewId:3,edition:'e',summary:{history_snapshot:{reporting_groups:groups},historyReplyBytes:20},blocks:new Map()};
function renderHistoryBlockFilters(){}
function skuClusterIdentity(row){return row.row_id.split('|')[0];}
''' + helpers + page_loader + loader + r'''
const h=historySnapshotState; let calls=[],cancel=false,broken=false,replyCeiling=512;
const dataset={};
for(const [group,n] of [['group:clean',10],['group:matte',8]]){
 dataset[group]=Array.from({length:n*71},(_,i)=>({row_id:'SKU:'+group+':'+Math.floor(i/71)+'|metric:'+i%71,row_kind:'sku',group_id:group,section_id:'s'}));
}
async function historySnapshotFetch(scope,period,edition,offset,limit,id){
 calls.push({scope,id,offset,limit,edition});
 if(scope==='sku'&&limit>replyCeiling){const error=new Error('history_reply_limit');error.code='history_reply_limit';throw error;}
 if(cancel && scope==='sku' && offset>0){h.viewId++;cancel=false;}
 const all=scope==='sku'?dataset[id]:[{row_id:scope+id,row_kind:scope,group_id:id,section_id:'s'}];
 const rows=all.slice(offset,offset+limit),next=offset+rows.length<all.length?offset+rows.length:null;
 return {table_surface:{rows},history_snapshot:{total_rows:all.length,next_offset:broken?null:next},historyReplyBytes:rows.length*10,historyReadMetrics:{}};
}
(async()=>{
 const all={total:true,totals:groups.map(g=>g.group_id),skus:groups.map(g=>g.group_id)};
 assert(await loadSelectedHistoryBlocks({total:true,totals:[],skus:[]},7,3));
 assert.equal(calls.filter(c=>c.scope==='sku').length,0);
 assert(await loadSelectedHistoryBlocks(all,7,3));
 for(const g of groups){
  const rows=h.blocks.get(historyBlockKey('sku',g.group_id)).rows;
  assert.deepEqual(rows.map(r=>r.row_id),dataset[g.group_id].map(r=>r.row_id));
  const identities=new Map();
  for(const row of rows){const sku=row.row_id.split('|')[0];identities.set(sku,(identities.get(sku)||0)+1);}
  assert.equal(identities.size,g.group_id==='group:clean'?10:8);
  assert([...identities.values()].every(count=>count===71));
  assert.deepEqual(calls.filter(c=>c.id===g.group_id&&c.scope==='sku').map(c=>c.offset),[0,512]);
 }
 assert(calls.every(c=>c.edition==='e'));assert(h.progress.includes('Matte'));assert.equal(h.progressRows,568);assert.equal(h.progressTotal,568);
 calls=[];assert(await loadSelectedHistoryBlocks({total:true,totals:[],skus:[]},7,3));
 assert(await loadSelectedHistoryBlocks(all,7,3));assert.equal(calls.length,0);
 // New period/edition clears cache before fetching: identical identities get fresh rows.
 h.blocks=new Map();h.edition='new';calls=[];
 assert(await loadSelectedHistoryBlocks(all,7,3));assert(calls.some(c=>c.scope==='sku'));assert(calls.every(c=>c.edition==='new'));
 // Cancellation after a transport page never publishes a partial block or starts another page.
 h.blocks=new Map();const before=h.blocks;cancel=true;calls=[];
 assert.equal(await loadSelectedHistoryBlocks({total:false,totals:[],skus:['group:clean']},7,3),false);assert.equal(h.blocks,before);assert.equal(calls.length,2);
 h.viewId=3;HISTORY_CLIENT_BYTES=1500;calls=[];
 await assert.rejects(loadSelectedHistoryBlocks(all,7,3),/Слишком большой объём/);assert.equal(h.blocks,before);
 assert(calls.filter(c=>c.scope==='sku').length<=2);
 HISTORY_CLIENT_BYTES=100000;
 assert(await loadSelectedHistoryBlocks({total:true,totals:[],skus:['group:clean']},7,3));
 const completeBefore=h.blocks, selectedBefore=h.selection, bytesBefore=h.loadedBytes;
 HISTORY_CLIENT_BYTES=3000;
 await assert.rejects(loadSelectedHistoryBlocks(all,7,3),/Слишком большой объём/);
 assert.equal(h.blocks,completeBefore);assert.equal(h.selection,selectedBefore);assert.equal(h.loadedBytes,bytesBefore);
 HISTORY_CLIENT_BYTES=100000;assert(await loadSelectedHistoryBlocks(all,7,3));
 HISTORY_CLIENT_BYTES=8000;
 assert(await loadSelectedHistoryBlocks({total:true,totals:[],skus:['group:clean']},7,3));
 assert(!h.blocks.has(historyBlockKey('sku','group:matte'))); // optional cache evicted before exceeding the budget
 assert.equal(h.loadedBytes,20+[...h.blocks.values()].reduce((sum,b)=>sum+b.bytes,0));
 assert(h.loadedBytes<=HISTORY_CLIENT_BYTES);
 // Use the actual adaptive page helper: a rejected 512 page halves at the
 // same pinned offset, then every subsequent page keeps the safe 128 limit.
 HISTORY_CLIENT_BYTES=100000;replyCeiling=128;calls=[];
 const fallback=await fetchHistoryBlock('sku','group:clean',7,3);
 assert.deepEqual(fallback.rows.map(r=>r.row_id),dataset['group:clean'].map(r=>r.row_id));
 assert.deepEqual(calls.slice(0,3).map(c=>[c.offset,c.limit]),[[0,512],[0,256],[0,128]]);
 assert(calls.slice(2).every(c=>c.limit===128&&c.edition==='new'));
 assert.deepEqual(calls.slice(2).map(c=>c.offset),[0,128,256,384,512,640]);
 assert.equal(fallback.requests,6);
 replyCeiling=0;calls=[];
 await assert.rejects(fetchHistoryBlock('sku','group:clean',7,3),e=>e.code==='history_reply_limit');
 assert.equal(calls.at(-1).limit,1);assert.equal(calls.length,10);
 replyCeiling=512;broken=true;
 await assert.rejects(fetchHistoryBlock('sku','group:clean',7,3),/не полностью/);
 console.log(JSON.stringify({status:'pass',complete_sku_identities:18,metric_rows_per_sku:71,initial_transport_limit:512,adaptive_reply_limit_fallback:true,transport_boundary_inside_sku:true,toggle_cache:true,period_reset:true,cancellation:true,bounded_atomic_failure:true}));
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    with tempfile.TemporaryDirectory(prefix='wbc-complete-sku-') as temp:
        path=Path(temp)/'complete.cjs';path.write_text(script)
        subprocess.run(['node',str(path)],check=True)


def serve():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlsplit, parse_qs
    from packages.application.web_vitrina_history_store import HistoryStore, import_finished_table
    from packages.application.web_vitrina_history_http_read import read_history_page
    from packages.adapters.registry_upload_http_entrypoint import _render_sheet_vitrina_web_vitrina_ui
    with tempfile.TemporaryDirectory(prefix='group-ui-fixture-') as directory:
        from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer
        from apps.web_vitrina_snapshot_pilot_smoke import _build
        fixture=LocalWebVitrinaFixtureServer(with_ready_snapshot=True,ready_days=3)
        with fixture:
            table=deepcopy(_build(fixture,'2026-04-19','2026-04-20',compact=True)['table_surface'])
        indices = {c['id']:i for i,c in enumerate(table['columns'])}
        def set_value(row, key, value):
            for cell in row['values']:
                if cell[0] == indices[key]: cell[1:3] = [value,str(value)]
        totals = [r for r in table['rows'] if r['row_kind']=='total']
        sku_rows = [r for r in table['rows'] if r['row_kind']=='sku']
        first_sku = sku_rows[0]['row_id'].split('|')[0]
        sku_metrics = [r for r in sku_rows if r['row_id'].split('|')[0] == first_sku]
        # Retained catalog caption is deliberately old; the numeric 11 ratio must still display 1100%.
        ctr=next((r for r in sku_metrics if any(v[0]==indices['metric_key'] and v[1]=='ctr' for v in r['values'])),None)
        if ctr is None:
            ctr=deepcopy(sku_metrics[0]);ctr['row_id']=first_sku+'|ctr';set_value(ctr,'metric_key','ctr');sku_metrics.append(ctr)
        set_value(ctr,'metric_label','CTR в воронке')
        from packages.application.web_vitrina_compact_table import CELL_DEFAULTS
        for cell in ctr['values']:
            if table['columns'][cell[0]]['id'].startswith('date:'):
                cell[:]=[cell[0],11,'1100%','percent','percent_default','renderer:percent:percent_default',*CELL_DEFAULTS[5:]]
        table['rows'] = list(totals)
        expected={}
        for group_index,(key,label) in enumerate([('clean','Clean'),('no_frame_clean','No Frame Clean'),('matte','Matte')]):
            for original in totals:
                row=deepcopy(original); row['row_id']='GROUP:'+key+'|'+original['row_id'];row['row_kind']='group';row['group_id']='group:'+key
                set_value(row,'group',label);set_value(row,'scope_key','GROUP:'+key);set_value(row,'scope_label',label)
                table['rows'].append(row)
            expected['group:'+key]=[]
            for n in range(34):
                nm=1000+group_index*100+n
                for original in sku_metrics:
                    row=deepcopy(original);row['row_id']='SKU:'+str(nm)+'|'+original['row_id'].split('|')[-1];row['group_id']='group:'+key
                    set_value(row,'group',label);set_value(row,'scope_key','SKU:'+str(nm));set_value(row,'scope_label',label+' SKU '+str(n));set_value(row,'nm_id',nm)
                    table['rows'].append(row);expected['group:'+key].append(row['row_id'])
        for n,row in enumerate(table['rows'],1):set_value(row,'row_order',n)
        table['groupings']=[]
        store=HistoryStore(Path(directory)/'history')
        import_finished_table(store,table,accepted_ready={d:True for d in ['2026-04-19','2026-04-20']})
        config={'status':'ok','revision':0,'config':{}}
        transport={'sku_reply_ceiling':512}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                path=urlsplit(self.path);status=200
                if path.path=='/sheet-vitrina-v1/vitrina':
                    body=_render_sheet_vitrina_web_vitrina_ui(read_path='/read',operator_path='/operator',refresh_path='/refresh',job_path='/job',history_snapshots_configured=True).encode()
                    mime='text/html; charset=utf-8'
                elif path.path=='/read':
                    q={k:v[0] for k,v in parse_qs(path.query).items()}
                    limit=int(q.get('limit',128));mime='application/json'
                    if q['scope']=='sku' and limit>transport['sku_reply_ceiling']:
                        status=503;body=b'{"error":"history_reply_limit"}'
                    else:
                        body=json.dumps(read_history_page(store,date_from=q['date_from'],date_to=q['date_to'],scope=q['scope'],edition_id=q.get('edition_id'),group_id=q.get('group_id'),offset=int(q.get('offset',0)),limit=limit),ensure_ascii=False).encode()
                elif path.path.endswith('.css'):
                    body=(ROOT/'packages/adapters/templates/sheet_vitrina_v1_ui_system.css').read_bytes();mime='text/css'
                else:body=json.dumps(config).encode();mime='application/json'
                self.send_response(status);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            def do_POST(self):
                body=self.rfile.read(int(self.headers.get('Content-Length','0')))
                if 'user-config' in self.path:
                    config['config']=json.loads(body).get('config',{});config['revision']+=1
                result=json.dumps(config).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(result)));self.end_headers();self.wfile.write(result)
        server=ThreadingHTTPServer(('127.0.0.1',int(os.environ.get('WBC_GROUP_UI_PORT','0'))),Handler)
        print('http://127.0.0.1:'+str(server.server_port)+'/sheet-vitrina-v1/vitrina?history_mode=explicit&date_from=2026-04-19&date_to=2026-04-20',flush=True)
        if '--browser' in sys.argv:
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                check_browser_complete_groups('http://127.0.0.1:'+str(server.server_port),expected,transport)
            finally:server.shutdown();thread.join();server.server_close()
        else:
            try:server.serve_forever()
            finally:server.server_close()


def check_browser_complete_groups(base, expected, transport):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True);page=browser.new_page();errors=[];requests=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.on('request',lambda request:requests.append(request.url) if '/read?' in request.url else None)
        page.goto(base+'/sheet-vitrina-v1/vitrina?history_mode=explicit&date_from=2026-04-19&date_to=2026-04-20')
        page.wait_for_selector('[data-history-summary-load-ms]')
        assert not any('scope=sku' in url for url in requests)
        assert page.evaluate("webVitrinaMetricExplanation('avg_addToCartConversion')")=='Среднее значение по SKU с доступными данными.'
        assert page.evaluate("webVitrinaMetricExplanation('avg_cartToOrderConversion')")=='Среднее значение по SKU с доступными данными.'
        assert page.evaluate("webVitrinaMetricExplanation('addToCartConversion')")==''
        assert page.evaluate("webVitrinaMetricCaption('total_ads_sum','Расходы на рекламу')")== 'Расходы на рекламу, ₽'
        assert page.evaluate("webVitrinaMetricCaption('total_ads_sum_price','Расходы, ₽')")== 'Расходы, ₽'
        page.locator('[data-filters-toggle]').click();page.locator('[data-block-master="all"]').check()
        page.locator('[data-filters-apply]').click()
        page.wait_for_function('historySnapshotState.busy === false && historySnapshotState.selection.skus.length === 3')
        for group,ids in expected.items():
            assert len(ids)>512, {'group':group,'metricrows':len(ids)}
            actual=page.evaluate("id => historySnapshotState.blocks.get(historyBlockKey('sku',id)).rows.map(row=>row.row_id)",group)
            assert actual==ids, {'group':group,'expected':len(ids),'actual':len(actual)}
            from urllib.parse import parse_qs, urlsplit
            pages=[parse_qs(urlsplit(url).query) for url in requests
                if 'scope=sku' in url and parse_qs(urlsplit(url).query).get('group_id')==[group]]
            assert [page['offset'][0] for page in pages]==['0','512'],pages
            assert all(page['limit']==['512'] for page in pages),pages
        scopes=page.locator('[data-table-body] tr[data-row-scope-key]').evaluate_all("rows=>[...new Set(rows.map(r=>r.dataset.rowScopeKey))]")
        wanted=['TOTAL']
        for group in page.evaluate('historyBlockGroups().map(g=>g.group_id)'):
            if group not in expected:continue
            wanted.append('GROUP:'+group.removeprefix('group:'));wanted.extend(dict.fromkeys(r.split('|')[0] for r in expected[group]))
        assert scopes==wanted, {'actual':scopes,'expected':wanted}
        ctr_caption=page.locator('[data-table-body] td[data-metric-key="ctr"][data-col-id="metric_label"] .metric-label-text').first
        assert ctr_caption.inner_text()=='Открытия / показы выдачи, %'
        assert 'может превышать 100%' in ctr_caption.get_attribute('title')
        ctr_label_cell=ctr_caption.locator('xpath=ancestor::td')
        explanation=page.evaluate("webVitrinaMetricExplanation('ctr')")
        assert explanation in ctr_label_cell.get_attribute('title')
        assert 'Открытия / показы выдачи, %' in ctr_label_cell.get_attribute('title')
        assert 'CTR в воронке' not in ctr_label_cell.get_attribute('title')
        assert ctr_label_cell.get_attribute('aria-label')==ctr_label_cell.get_attribute('title')
        assert page.evaluate("historySnapshotState.blocks.get(historyBlockKey('sku','group:clean')).rows.find(r=>r.values.metric_key.value==='ctr').values.metric_label.value")=='CTR в воронке'
        displayed_ctr=page.locator('[data-table-body] td[data-metric-key="ctr"][data-col-id^="date:"]').first.inner_text()
        assert '%' in displayed_ctr and float(re.sub(r'[\s\u00a0%]','',displayed_ctr).replace(',','.'))==1100,displayed_ctr
        assert page.evaluate("historySnapshotState.blocks.get(historyBlockKey('sku','group:clean')).rows.find(r=>r.values.metric_key.value==='ctr').values['date:2026-04-20'].value")==11
        page.locator('[data-metrics-settings-open]').click()
        ctr_setting=page.locator('[data-metric-config-row][data-sku-metric-key="ctr"] .metrics-config-label').first
        assert ctr_setting.inner_text()=='Открытия / показы выдачи, %'
        assert 'может превышать 100%' in ctr_setting.get_attribute('title')
        page.locator('[data-metrics-settings-close]').last.click()
        before=len(requests)
        page.locator('[data-filters-toggle]').click();page.locator('[data-block-master="sku"]').uncheck();page.locator('[data-filters-apply]').click()
        page.wait_for_function('historySnapshotState.busy === false')
        assert page.locator('[data-table-body] tr[data-row-kind="sku"]').count()==0
        page.locator('[data-filters-toggle]').click();page.locator('[data-block-master="sku"]').check();page.locator('[data-filters-apply]').click()
        page.wait_for_function('historySnapshotState.busy === false && historySnapshotState.selection.skus.length === 3')
        assert len(requests)==before
        # The real period-load entrypoint clears complete cached blocks and reads the new date range.
        before=len(requests);transport['sku_reply_ceiling']=128
        page.evaluate("async () => { history.replaceState(null,'','?history_mode=explicit&date_from=2026-04-20&date_to=2026-04-20'); state.requestSequence+=1; await loadHistorySnapshot(state.requestSequence); }")
        assert any('scope=sku' in url for url in requests[before:])
        assert all('date_from=2026-04-20' in url and 'date_to=2026-04-20' in url for url in requests[before:])
        assert page.locator('[data-table-head] th[data-col-id^="date:"]').count()==1
        for group,ids in expected.items():
            assert page.evaluate("id => historySnapshotState.blocks.get(historyBlockKey('sku',id)).rows.length",group)==len(ids)
            pages=[parse_qs(urlsplit(url).query) for url in requests[before:]
                if 'scope=sku' in url and parse_qs(urlsplit(url).query).get('group_id')==[group]]
            assert [(page['offset'][0],page['limit'][0]) for page in pages[:3]]==[('0','512'),('0','256'),('0','128')],pages
            assert all(page['limit']==['128'] for page in pages[2:]),pages
            assert [page['offset'][0] for page in pages[2:]]==[str(offset) for offset in range(0,len(ids),128)],pages
        assert not errors,errors
        evidence=os.environ.get('WBC_HISTORY_UI_EVIDENCE_DIR')
        if evidence:page.screenshot(path=str(Path(evidence)/'complete-all-sku-groups-fixture.png'))
        print(json.dumps({'status':'pass','fixture_only':True,'selected_groups':3,'sku_identities':102,'metric_rows_per_group':{k:len(v) for k,v in expected.items()},'all_metric_rows_complete':True,'initial_transport_limit':512,'http_reply_limit_fallback_512_256_128':True,'group_total_then_sku_order':True,'toggle_cache':True,'period_reset_real_loader':True,'retained_ctr_caption_tooltip_and_1100_preserved':True,'old_snapshot_ctr_label_td_title_and_aria_explained':True}))
        browser.close()



if __name__=='__main__':
    check_selection()
    check_complete_sku_pages()
    if '--serve' in sys.argv or '--browser' in sys.argv:serve()
