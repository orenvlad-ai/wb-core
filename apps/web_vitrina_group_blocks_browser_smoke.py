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
 assert.equal(h.blocks.size,0);assert.equal(calls.length,0); // deselected data discarded
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


def serve():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlsplit, parse_qs
    from apps.sheet_vitrina_v1_web_vitrina_gravity_table_adapter_smoke import _build_view_model_payload
    from packages.application.web_vitrina_gravity_table_adapter import build_web_vitrina_gravity_table_adapter
    from packages.application.web_vitrina_compact_table import compact_adapter_payload
    from packages.application.web_vitrina_history_store import HistoryStore, import_finished_table
    from packages.application.web_vitrina_history_http_read import read_history_page
    from packages.adapters.registry_upload_http_entrypoint import _render_sheet_vitrina_web_vitrina_ui
    with tempfile.TemporaryDirectory(prefix='group-ui-fixture-') as directory:
        table=compact_adapter_payload(build_web_vitrina_gravity_table_adapter(_build_view_model_payload()))
        indices = {c['id']:i for i,c in enumerate(table['columns'])}
        def set_value(row, key, value):
            for cell in row['values']:
                if cell[0] == indices[key]: cell[1:3] = [value,str(value)]
        totals = [r for r in table['rows'] if r['row_kind']=='total']
        sku = next(r for r in table['rows'] if r['row_kind']=='sku')
        table['rows'] = list(totals)
        for key,label in [('clean','Clean'),('no_frame_clean','No Frame Clean'),('matte','Matte')]:
            for original in totals:
                row=deepcopy(original); row['row_id']='GROUP:'+key+'|'+original['row_id'];row['row_kind']='group';row['group_id']='group:'+key
                set_value(row,'group',label);set_value(row,'scope_key','GROUP:'+key);set_value(row,'scope_label',label)
                table['rows'].append(row)
            for n in range(140):
                row=deepcopy(sku);row['row_id']='SKU:'+key+':'+str(n);row['group_id']='group:'+key
                set_value(row,'group',label);set_value(row,'scope_key','SKU:'+str(1000+n));set_value(row,'scope_label',label+' SKU '+str(n));set_value(row,'nm_id',1000+n)
                table['rows'].append(row)
        for n,row in enumerate(table['rows'],1):set_value(row,'row_order',n)
        table['groupings']=[]
        store=HistoryStore(Path(directory)/'history')
        import_finished_table(store,table,accepted_ready={d:True for d in ['2026-04-19','2026-04-20']})
        config={'status':'ok','revision':0,'config':{}}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                path=urlsplit(self.path)
                if path.path=='/sheet-vitrina-v1/vitrina':
                    body=_render_sheet_vitrina_web_vitrina_ui(read_path='/read',operator_path='/operator',refresh_path='/refresh',job_path='/job',history_snapshots_configured=True).encode()
                    mime='text/html; charset=utf-8'
                elif path.path=='/read':
                    q={k:v[0] for k,v in parse_qs(path.query).items()}
                    body=json.dumps(read_history_page(store,date_from=q['date_from'],date_to=q['date_to'],scope=q['scope'],edition_id=q.get('edition_id'),group_id=q.get('group_id'),offset=int(q.get('offset',0)),limit=int(q.get('limit',128))),ensure_ascii=False).encode();mime='application/json'
                elif path.path.endswith('.css'):
                    body=(ROOT/'packages/adapters/templates/sheet_vitrina_v1_ui_system.css').read_bytes();mime='text/css'
                else:body=json.dumps(config).encode();mime='application/json'
                self.send_response(200);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            def do_POST(self):
                body=self.rfile.read(int(self.headers.get('Content-Length','0')))
                if 'user-config' in self.path:
                    config['config']=json.loads(body).get('config',{});config['revision']+=1
                result=json.dumps(config).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(result)));self.end_headers();self.wfile.write(result)
        server=ThreadingHTTPServer(('127.0.0.1',int(os.environ.get('WBC_GROUP_UI_PORT','0'))),Handler)
        print('http://127.0.0.1:'+str(server.server_port)+'/sheet-vitrina-v1/vitrina?history_mode=explicit&date_from=2026-04-19&date_to=2026-04-20',flush=True)
        try:server.serve_forever()
        finally:server.server_close()



if __name__=='__main__':
    check_selection()
    if '--serve' in sys.argv:serve()
