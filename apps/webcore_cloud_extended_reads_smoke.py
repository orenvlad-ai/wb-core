"""Read capability boundaries and canonical financial evidence fixtures."""
from __future__ import annotations
import hashlib
import io
import json
import ssl
import threading
from tempfile import TemporaryDirectory
from urllib.error import HTTPError, URLError
import time
import sys
from pathlib import Path
import unittest
from unittest.mock import MagicMock, Mock, patch
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from apps.webcore_cloud_reads_smoke import CloudReadTests
from apps.finance_liquidity_cash_smoke import open_account, create_and_post
from packages.application.finance_liquidity_cash import FinanceCashService
from packages.application.webcore_cloud_reads import CloudReadError, WebCoreCloudReads, SCOPE_CASH_OPERATIONS_READ, SCOPE_MAX_HISTORY_READ
from packages.application.webcore_cloud_extended_reads import MaxHistoryReader
from packages.application.webcore_max_posted_reply import MaxPostedReply
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

    def test_history_has_separate_bounded_deadline(self):
        self.reads.max_api=MaxHistoryReader(self.root/'secret',None,frozenset({-100}))
        identity=AuthIdentity('owner',(SCOPE_MAX_HISTORY_READ,),'oauth')
        self.config.tool_deadline_seconds=0.05
        self.config.max_history_deadline_seconds=0.4
        def delayed_get(params, deadline, cancel_event=None):
            self.assertGreater(deadline-time.monotonic(),0.2)
            time.sleep(0.1)
            return []
        with patch.object(self.reads.max_api,'_get',side_effect=delayed_get):
            for name,args in [('max_messages',{'chat_id':-100}),('max_message',{'chat_id':-100,'message_id':'mid.123'})]:
                result=self.rpc(identity,'tools/call',name=name,arguments=args)
                self.assertNotIn('error',result)
        self.config.max_history_deadline_seconds=0.05
        with patch.object(self.reads.max_api,'_get',side_effect=lambda *args: (time.sleep(0.15) or [])):
            result=self.rpc(identity,'tools/call',name='max_messages',arguments={'chat_id':-100})
            self.assertEqual(result['error']['data']['code'],'tool_timeout')
            time.sleep(0.2)

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

class MaxGetResilienceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        token = Path(self.temp.name).resolve()/'token'
        token.write_text('private-fixture-token')
        token.chmod(0o600)
        self.api = MaxHistoryReader(token, None, {-100})
        self.clock = 100.0
        self.opener = MagicMock()
        self.response = Mock()
        self.response.read.return_value = b'{"messages":[]}'
        self.opener.open.return_value.__enter__.return_value = self.response
        prefix = 'packages.application.webcore_cloud_extended_reads.'
        for name, kwargs in [('build_opener', {'return_value': self.opener}),
                             ('time.monotonic', {'side_effect': lambda: self.clock}),
                             ('time.sleep', {'side_effect': self.advance})]:
            patcher = patch(prefix+name, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def advance(self, seconds):
        self.clock += seconds

    def success(self):
        response = MagicMock()
        response.__enter__.return_value = self.response
        return response

    def test_timeout_retry_is_get_only_and_shares_deadline(self):
        def first(*args, **kwargs):
            self.advance(20)
            raise URLError(TimeoutError('private fixture detail'))
        def request(*args, **kwargs):
            if self.opener.open.call_count == 1:
                return first(*args, **kwargs)
            return self.success()
        self.opener.open.side_effect = request
        self.assertEqual(self.api._get({'chat_id':-100}, 140), [])
        self.assertEqual(self.opener.open.call_count, 2)
        calls = self.opener.open.call_args_list
        self.assertEqual([c.args[0].get_method() for c in calls], ['GET', 'GET'])
        self.assertEqual([c.kwargs['timeout'] for c in calls], [20, 19.75])
        self.assertLess(self.clock, 140)

    def test_transient_errors_retry_at_most_once_and_preserve_cause(self):
        for exc in [TimeoutError('private-fixture-token'), URLError('private-fixture-token'),
                    HTTPError('https://private', 429, 'secret', {}, io.BytesIO(b'secret')),
                    HTTPError('https://private', 503, 'secret', {}, io.BytesIO(b'secret'))]:
            with self.subTest(type=type(exc), status=getattr(exc,'code',None)):
                self.clock = 100
                self.opener.open.reset_mock()
                self.opener.open.side_effect = exc
                with self.assertLogs('packages.application.webcore_cloud_extended_reads', level='WARNING') as logs:
                    with self.assertRaises(CloudReadError) as error:
                        self.api._get({'chat_id':-100}, 140)
                self.assertEqual(self.opener.open.call_count, 2)
                self.assertIs(error.exception.__cause__, exc)
                self.assertIn('max_get_open_', error.exception.code)
                self.assertNotIn('private', str(error.exception))
                self.assertNotIn('secret', str(error.exception))
                self.assertNotIn('private', str(logs.output))
                self.assertNotIn('secret', str(logs.output))

    def test_nontransient_http_and_tls_never_retry(self):
        for exc in [HTTPError('https://private', code, 'secret', {}, None) for code in (301, 400, 401, 403, 404)]+[
                    URLError(ssl.SSLCertVerificationError('private detail')),
                    ssl.SSLError('private detail')]:
            with self.subTest(type=type(exc), status=getattr(exc,'code',None)):
                self.opener.open.reset_mock()
                self.opener.open.side_effect = exc
                with self.assertRaises(CloudReadError):
                    self.api._get({}, 140)
                self.assertEqual(self.opener.open.call_count, 1)

    def test_invalid_json_and_shape_never_retry(self):
        for body in [b'private-fixture-token', b'{"messages":{}}']:
            self.opener.open.reset_mock()
            self.response.read.return_value = body
            with self.assertRaises(CloudReadError) as error:
                self.api._get({}, 140)
            self.assertEqual(error.exception.code, 'max_get_decode_invalid_response')
            self.assertEqual(self.opener.open.call_count, 1)
            self.assertNotIn('private-fixture-token', str(error.exception))

    def test_read_timeout_retries_and_classifies_read_phase(self):
        self.response.read.side_effect = TimeoutError('private detail')
        with self.assertRaises(CloudReadError) as error:
            self.api._get({}, 140)
        self.assertEqual(error.exception.code, 'max_get_read_timeout')
        self.assertEqual(self.opener.open.call_count, 2)

    def test_no_request_after_cancel_or_expiry(self):
        event = threading.Event()
        event.set()
        for deadline, cancel in [(140,event), (100,None), (99,None)]:
            with self.assertRaises(CloudReadError) as error:
                self.api._get({}, deadline, cancel)
            self.assertEqual(error.exception.code, 'deadline_exceeded')
        self.opener.open.assert_not_called()

    def test_cancel_during_backoff_prevents_second_request(self):
        event = Mock()
        event.is_set.return_value = False
        event.wait.side_effect = lambda delay: setattr(event.is_set, 'return_value', True)
        self.opener.open.side_effect = TimeoutError()
        with self.assertRaises(CloudReadError):
            self.api.call('max_messages', {'chat_id':-100}, 140, event)
        self.assertEqual(self.opener.open.call_count, 1)
        event.wait.assert_called_once_with(0.25)

    def test_retry_after_is_respected_within_bound(self):
        self.opener.open.side_effect = [HTTPError('https://private', 429, 'secret', {'Retry-After':'1'}, None), self.success()]
        self.api._get({}, 140)
        self.assertEqual(self.clock, 101)
        self.assertEqual(self.opener.open.call_count, 2)

    def test_recovered_source_read_cannot_spend_post_reserve(self):
        row = {'recipient':{'chat_id':-100,'chat_type':'chat'},
               'body':{'mid':'mid.source','text':'payment'}, 'sender':{'is_bot':False}}
        self.response.read.return_value = json.dumps({'messages':[row]}).encode()
        def request(*args, **kwargs):
            if self.opener.open.call_count == 1:
                self.advance(11)
                raise TimeoutError()
            return self.success()
        self.opener.open.side_effect = request
        reply = MaxPostedReply(self.api, Path(self.temp.name).resolve()/'replies'/'max_posted_replies.sqlite3')
        reply._post = Mock()
        with self.assertRaises(CloudReadError) as error:
            reply.call(dict(chat_id=-100, message_id='mid.source', source_text='payment',
                            ui_verification='UI verified', all_operations_posted=True), deadline=140)
        self.assertEqual(error.exception.code, 'deadline_exceeded')
        self.assertEqual(self.opener.open.call_count, 2)
        reply._post.assert_not_called()
        with reply._connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM replies').fetchone()[0], 0)

    def test_no_retry_without_backoff_budget_or_with_long_retry_after(self):
        def near_deadline(*args, **kwargs):
            self.advance(0.9)
            raise TimeoutError()
        self.opener.open.side_effect = near_deadline
        with self.assertRaises(CloudReadError):
            self.api._get({}, 101)
        self.assertEqual(self.opener.open.call_count, 1)
        self.clock = 100
        self.opener.open.reset_mock()
        self.opener.open.side_effect = HTTPError('https://private', 429, 'secret', {'Retry-After':'30'}, None)
        with self.assertRaises(CloudReadError):
            self.api._get({}, 140)
        self.assertEqual(self.opener.open.call_count, 1)

if __name__=='__main__':unittest.main()
