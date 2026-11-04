"""Read capability boundaries and canonical financial evidence fixtures."""
from __future__ import annotations
import hashlib
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from apps.webcore_cloud_reads_smoke import CloudReadTests
from apps.finance_liquidity_cash_smoke import open_account, create_and_post
from packages.application.finance_liquidity_cash import FinanceCashService
from packages.application.webcore_cloud_reads import WebCoreCloudReads, SCOPE_CASH_OPERATIONS_READ, SCOPE_MAX_HISTORY_READ
from packages.application.webcore_cloud_extended_reads import MaxHistoryReader
from packages.application.webcore_data_mcp import WebCoreDataMcpGateway, WebCoreDataMcpError
from apps.webcore_data_mcp_server import AuthIdentity

class ExtendedReadTests(CloudReadTests):
    def setUp(self):
        super().setUp()
        self.reads=WebCoreCloudReads.from_env({**self.env,'WEBCORE_DATA_MCP_CASH_OPERATIONS_READ_ENABLED':'1'})
        self.gateway=WebCoreDataMcpGateway(cloud_reads=self.reads)
        self.service=FinanceCashService(self.cash_path)

    def test_separate_grants_and_readonly_finance(self):
        old=AuthIdentity('owner',('webcore.cash.read',),'oauth')
        self.assertNotIn('cash_read',{x['name'] for x in self.rpc(old,'tools/list')['result']['tools']})
        self.assertEqual(self.rpc(old,'tools/call',name='cash_read',arguments={'resource':'accounts'})['error']['data']['code'],'insufficient_scope')
        full=AuthIdentity('owner',(SCOPE_CASH_OPERATIONS_READ,),'oauth')
        self.assertEqual([x['name'] for x in self.rpc(full,'tools/list')['result']['tools']],['cash_read'])
        accounts=self.gateway.call_tool('cash_read',{'resource':'accounts'})['rows']
        self.assertTrue(all(x['balance_minor'] is None for x in accounts))
        aid='cash_vladislav';open_account(self.service,aid,'1000.00')
        cat=next(x for x in self.service.list_categories() if x['direction']=='expense')
        create_and_post(self.service,{'document_type':'expense','source_account_id':aid,'category_id':cat['category_id'],'amount':'10.25','occurred_at':'2026-10-01T10:00:00Z','purpose':'fixture'})
        before=hashlib.sha256(self.cash_path.read_bytes()).digest()
        result=self.gateway.call_tool('cash_read',{'resource':'accounts','account_id':aid})
        self.assertEqual(result['rows'][0]['balance'],'989.75')
        first=self.gateway.call_tool('cash_read',{'resource':'documents','limit':1})
        second=self.gateway.call_tool('cash_read',{'resource':'documents','limit':1,'cursor':first['next_cursor']})
        self.assertNotEqual(first['rows'][0]['document_id'],second['rows'][0]['document_id'])
        detail=self.gateway.call_tool('cash_read',{'resource':'document','document_id':first['rows'][0]['document_id']})
        self.assertEqual(detail['rows'][0]['document']['status'],'posted')
        self.assertTrue(detail['rows'][0]['transactions'][0]['entries'])
        self.assertEqual(before,hashlib.sha256(self.cash_path.read_bytes()).digest())
        with self.assertRaises(WebCoreDataMcpError):
            self.gateway.call_tool('cash_read',{'resource':'accounts','cursor':first['next_cursor']})
        for resource in ['categories','counterparties']:
            self.assertEqual(self.gateway.call_tool('cash_read',{'resource':resource})['status'],'ok')

    def test_financial_chunk_redaction(self):
        from packages.application.webcore_cloud_extended_reads import _text_fields
        from packages.application.webcore_data_mcp import _redact
        result=_redact(_text_fields({'purpose':'a'*497+'token=VERY_PRIVATE_CREDENTIAL'+'z'*90}))
        self.assertEqual(result['purpose_chunks'],[])
        self.assertFalse(result['purpose_complete'])
        self.assertNotIn('VERY_PRIVATE_CREDENTIAL',str(result))

    def test_max_history_group_boundary_and_full_text(self):
        api=MaxHistoryReader(self.root/'secret',None,frozenset({-100}))
        self.reads.max_api=api
        message={'recipient':{'chat_id':-100,'chat_type':'chat'},'timestamp':1000,'body':{'mid':'mid.123','seq':2,'text':'оплата '*250,'attachments':[]}}
        with patch.object(api,'_get',return_value=[message]) as get:
            result=self.gateway.call_tool('max_messages',{'chat_id':-100,'before_ms':1500})
            self.assertTrue(result['rows'][0]['message_text_truncated'])
            self.assertEqual(get.call_args.args[0]['from'],1500)
            result=self.gateway.call_tool('max_message',{'chat_id':-100,'message_id':'mid.123'})
            self.assertEqual(''.join(result['rows'][0]['message_text_chunks']),message['body']['text'])
            with self.assertRaises(WebCoreDataMcpError):self.gateway.call_tool('max_message',{'chat_id':-200,'message_id':'mid.123'})
            message['link']={'type':'forward','message':{'mid':'old','text':'linked payment','attachments':[{'type':'file'}]}}
            linked=self.gateway.call_tool('max_message',{'chat_id':-100,'message_id':'mid.123'})['rows'][0]
            self.assertEqual(''.join(linked['linked_message']['message_text_chunks']),'linked payment')
            self.assertTrue(linked['linked_message']['attachments_omitted'])
            self.assertFalse(linked['message_text_complete'])
            message['recipient']['chat_id']=-200
            with self.assertRaises(WebCoreDataMcpError):self.gateway.call_tool('max_message',{'chat_id':-100,'message_id':'mid.123'})
        old=AuthIdentity('owner',('webcore.max_inbox.read',),'oauth')
        self.assertEqual(self.rpc(old,'tools/call',name='max_messages',arguments={'chat_id':-100})['error']['data']['code'],'insufficient_scope')

if __name__=='__main__':unittest.main()
