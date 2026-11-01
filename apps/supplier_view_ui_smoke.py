"""Exercise the actual template's request coordination without a browser."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT / "packages/adapters/templates/sheet_vitrina_v1_supplier.html").read_text()
start = source.index("      const viewRequests = new Map();")
end = source.index("          async function fetchJsonNetwork", start)
wrapper = source[start:end]
script = r'''
const assert = require('node:assert/strict');
const window = {addEventListener() {}};
const config = {supplier_read_path: '/view'};
const tradeDocumentsPath = '/contracts';
const shipmentsPath = '/shipments';
const requests = [];
function fetchJsonNetwork(url, options = {}) {
  return new Promise(resolve => requests.push({url, method: options.method || 'GET', resolve}));
}
''' + wrapper + r'''
(async () => {
  const first = fetchJson('/shipments/sup_one/documents');
  const second = fetchJson('/shipments/sup_one/documents');
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, '/view/sup_one/documents');
  const mutation = fetchJson('/shipments/sup_one', {method: 'PATCH'});
  assert.equal(requests[1].url, '/shipments/sup_one');
  const duringWrite = fetchJson('/shipments/sup_one/documents');
  assert.equal(requests.length, 3);
  requests[1].resolve({saved: true}); await mutation;
  const afterWrite = fetchJson('/shipments/sup_one/documents');
  assert.equal(requests.length, 4, 'post-commit read must not reuse pre-commit request');
  requests[0].resolve({revision: 1});
  requests[2].resolve({revision: 1});
  const a = await first, b = await second; a.revision = 99;
  assert.equal(b.revision, 1, 'editable card responses must not share objects');
  await duringWrite;
  const coalescedAfterWrite = fetchJson('/shipments/sup_one/documents');
  assert.equal(requests.length, 4, 'old finally must not erase newer in-flight request');
  requests[3].resolve({revision: 2});
  assert.equal((await afterWrite).revision, 2);
  assert.equal((await coalescedAfterWrite).revision, 2);
  console.log('supplier_view_ui_smoke: PASS (dedup, mutation generations, response ownership)');
})().catch(error => {console.error(error);process.exitCode=1;});
'''
subprocess.run(["node", "-"], input=script, text=True, check=True)
