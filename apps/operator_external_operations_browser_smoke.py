"""Actual Chromium, intercepted local receipts only; no seller requests."""
from pathlib import Path
import json
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ASSET = ROOT / 'packages/adapters/templates/sheet_vitrina_v1_operator_acceptance.js'


def main():
    receipt = dict(operation_id='registry-native',domain='wb_prices',durable_saved=True,
        accepted_at='2026-10-08T10:00:00Z',primary_effect='external_command',state='needs_attention',
        source_ref=dict(domain='wb_prices',native_idempotency_key='preview-native'),partial=True,
        children=[dict(nm_id=1,parameter_field='price',outcome='confirmed',external_confirmed=True),
                  dict(nm_id=2,parameter_field='price',outcome='ambiguous',external_confirmed=False)])
    requests=[]; errors=[]
    with sync_playwright() as p:
        browser=p.chromium.launch()
        try:
            page=browser.new_page()
            page.on('request',lambda r:requests.append((r.method,r.url)))
            page.on('pageerror',lambda e:errors.append(str(e)))
            def route(r):
                if '/operations/external?' in r.request.url:
                    r.fulfill(status=200,content_type='application/json',body=json.dumps({'operation':receipt}))
                else:r.fulfill(status=200,content_type='text/html',body='<div id="receipt"></div>')
            page.route('http://operator.test/**',route)
            page.goto('http://operator.test/fixture');page.add_script_tag(path=str(ASSET))
            result=page.evaluate('''async () => {
                window.sends=0;
                const submit=async()=>{sends++;throw Error('lost after native save');};
                const first=await OperatorAcceptance.onceExternal('wb_prices','preview-native',submit);
                const second=await OperatorAcceptance.onceExternal('wb_prices','preview-native',submit);
                OperatorAcceptance.renderReceipt(document.getElementById('receipt'),second.acceptance);
                return {first,second,sends};
            }''')
            assert result['sends']==1 and result['first']['status']=='native_readback',result
            assert result['second']['acceptance']['partial'] is True
            node=page.locator('#receipt');assert node.get_by_text('Команда сохранена.',exact=True).count()==1
            assert node.get_by_text('Требует внимания',exact=True).count()==1
            assert node.get_by_text('Подтверждено WB',exact=False).count()==1
            assert node.get_by_text('Результат неизвестен',exact=False).count()==1
            # A page reload preserves the one-submit fence. Only exact GET follows.
            page.reload();page.add_script_tag(path=str(ASSET))
            resumed=page.evaluate('''async () => {
                let sends=0;const result=await OperatorAcceptance.onceExternal('wb_prices','preview-native',async()=>{sends++;});
                return {sends,result};
            }''')
            assert resumed['sends']==0 and resumed['result']['status']=='native_readback',resumed
            assert page.evaluate("OperatorAcceptance.readExternalNative('wb_ads','preview-native')") is None
            assert page.evaluate("OperatorAcceptance.readExternalNative('wb_prices','foreign-preview')") is None
            assert all(method=='GET' for method,_ in requests),requests
            assert len([u for _,u in requests if '/operations/external?' in u])==5,requests
            assert not errors,errors
            rejection=page.evaluate("""async()=>{
              let sends=0;
              const submit=async()=>{sends++;throw OperatorAcceptance.externalHttpError({status:409},{error:'native prestate drift'},'fallback');};
              const messages=[];for(let n=0;n<2;n++){try{await OperatorAcceptance.onceExternal('wb_prices','rejected-native',submit);}catch(e){messages.push(e.message);}}
              return {sends,messages};
            }""")
            assert rejection['sends']==1 and len(rejection['messages'])==2 and all('native prestate drift' in m and 'новый preview' in m for m in rejection['messages']),rejection
            pre_submit=page.evaluate("""async()=>{try{await OperatorAcceptance.onceExternal('wb_prices','pre-submit-native',async()=>{throw OperatorAcceptance.externalHttpError({status:503},{error:'WB upload was not called',reason:'registry_fail_closed'},'fallback');});}catch(e){return e.message;}}""")
            assert 'WB upload was not called' in pre_submit
            # A direct POST with another receipt must reconcile only the original native source.
            mismatch=page.evaluate("""async()=>OperatorAcceptance.onceExternal('wb_prices','original-native',async()=>({acceptance:{operation_id:'foreign',domain:'wb_ads',durable_saved:true,accepted_at:'now',state:'completed',source_ref:{native_idempotency_key:'foreign'}}}))""")
            assert mismatch['status']=='unknown' and mismatch['acceptance'] is None,mismatch
        finally:browser.close()
    print('operator_external_operations_browser_smoke: PASS (one lost submit, exact GET/reload recovery, partial/unknown children, foreign identity rejected)')


if __name__=='__main__':main()
