"""Bot text scope isolation, durable receipts and no ambiguous retries."""
import sys
from pathlib import Path
from unittest.mock import Mock
from concurrent.futures import ThreadPoolExecutor
import copy
import time
import threading
import unittest
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from apps.webcore_cloud_reads_smoke import CloudReadTests
from apps.webcore_data_mcp_server import AuthIdentity
from packages.application.webcore_cloud_reads import SCOPE_MAX_MESSAGE_SEND, SCOPE_TRANSIT_READ, SCOPE_TRANSIT_SEND, TRANSIT_GROUP_ID, CloudReadError
from packages.application.webcore_max_send_message import MaxSendMessage

class SendTests(CloudReadTests):
    def setUp(self):
        super().setUp()
        self.api=Mock(chat_ids={-100})
        self.sender=MaxSendMessage(self.api,self.root/'replies'/'max_posted_replies.sqlite3',99,-100)
        self.reads.max_send=self.sender
        self.args={'chat_id':-100,'request_id':'reconciliation-1','text':'Сверка касс\nКаролина — 10 ₽\nВиктория — 20 ₽'}
        self.message={'recipient':{'chat_id':-100},'sender':{'user_id':99,'is_bot':True},'body':{'mid':'mid.sent','text':self.args['text']}}
        self.sender._send_payload=Mock(return_value={'message':self.message})
        self.api._get.return_value=[self.message]

    def test_oauth_scope_and_annotations(self):
        for identity in [AuthIdentity('owner',('webcore.max_history.read',),'oauth'),AuthIdentity('owner',('webcore.max_posted_reply.write',),'oauth'),AuthIdentity('legacy',(SCOPE_MAX_MESSAGE_SEND,),'bearer')]:
            self.assertNotIn('max_send_message',[x['name'] for x in self.rpc(identity,'tools/list')['result']['tools']])
            self.assertEqual(self.rpc(identity,'tools/call',name='max_send_message',arguments=self.args)['error']['data']['code'],'insufficient_scope')
        tool=next(x for x in self.gateway.list_tools() if x['name']=='max_send_message')
        self.assertFalse(tool['annotations']['readOnlyHint'])
        self.assertTrue(tool['annotations']['openWorldHint'])
        self.assertEqual(tool['inputSchema']['properties']['chat_id']['enum'],[-100])
        result=self.rpc(AuthIdentity('owner',(SCOPE_MAX_MESSAGE_SEND,),'oauth'),'tools/call',name='max_send_message',arguments=self.args)
        self.assertEqual(result['result']['structuredContent']['status'],'sent')

    def test_transit_sender_is_bound_to_group_and_separate_write_grant(self):
        self.api.chat_ids = {TRANSIT_GROUP_ID}
        self.message['recipient']['chat_id'] = TRANSIT_GROUP_ID
        sender = MaxSendMessage(self.api, self.sender.path, 99, TRANSIT_GROUP_ID)
        sender._send_payload = self.sender._send_payload
        self.reads.transit_send = sender
        args = {'request_id': 'transit-1', 'text': self.args['text']}
        for scope in (SCOPE_MAX_MESSAGE_SEND, SCOPE_TRANSIT_READ):
            result = self.rpc(AuthIdentity('owner', (scope,), 'oauth'), 'tools/call',
                              name='transit_send_message', arguments=args)
            self.assertEqual(result['error']['data']['code'], 'insufficient_scope')
        with self.assertRaises(CloudReadError):
            self.reads.call_tool('transit_send_message', dict(args, chat_id=-100))
        identity = AuthIdentity('owner', (SCOPE_TRANSIT_SEND,), 'oauth')
        tools = self.rpc(identity, 'tools/list')['result']['tools']
        self.assertEqual([t['name'] for t in tools], ['transit_send_message'])
        self.assertFalse(tools[0]['annotations']['readOnlyHint'])
        result = self.rpc(identity, 'tools/call', name='transit_send_message', arguments=args)
        self.assertEqual(result['result']['structuredContent']['status'], 'sent')
        self.assertEqual(sender._send_payload.call_args.args[0], TRANSIT_GROUP_ID)

    def test_default_off_and_explicit_bot(self):
        self.assertIsNone(MaxSendMessage.from_env({},self.api))
        with self.assertRaises(ValueError):MaxSendMessage(self.api,self.sender.path,0,-100)
        with self.assertRaises(ValueError):MaxSendMessage.from_env({'WEBCORE_DATA_MCP_MAX_MESSAGE_SEND_ENABLED':'1'},None)

    def test_once_concurrent_and_restart_and_conflict(self):
        with ThreadPoolExecutor(4) as pool:
            list(pool.map(lambda _: self.sender.call(self.args),range(4)))
        self.assertEqual(self.sender._send_payload.call_count,1)
        other=MaxSendMessage(self.api,self.sender.path,99,-100)
        other._send_payload=Mock(side_effect=AssertionError('resend'))
        result=other.call(self.args)
        self.assertEqual(result['status'],'sent');self.assertFalse(result['sent_now'])
        self.assertEqual(other.call({**self.args,'text':'different'})['status'],'request_conflict')
        other._send_payload.assert_not_called()

    def test_read_scope_expansion_does_not_expand_write(self):
        self.api.chat_ids.add(-200)
        with self.assertRaises(CloudReadError):self.sender.call({**self.args,'chat_id':-200})
        self.sender._send_payload.assert_not_called()

    def test_reconciled_sent_is_not_downgraded_by_original_timeout(self):
        entered=threading.Event();reconciled=threading.Event()
        def get(*args):
            if not entered.is_set():
                entered.set()
                self.assertTrue(reconciled.wait(3))
                raise TimeoutError()
            return [self.message]
        self.api._get.side_effect=get
        with ThreadPoolExecutor(2) as pool:
            first=pool.submit(self.sender.call,self.args)
            self.assertTrue(entered.wait(3))
            result=self.sender.call(self.args)
            self.assertEqual(result['status'],'sent')
            reconciled.set()
            self.assertEqual(first.result(3)['status'],'sent')
        self.assertEqual(self.sender.call(self.args)['status'],'sent')
        self.assertEqual(self.sender._send_payload.call_count,1)

    def test_unknown_without_id_never_resends(self):
        self.sender._send_payload.side_effect=TimeoutError()
        self.assertEqual(self.sender.call(self.args)['status'],'unknown')
        self.assertEqual(self.sender.call(self.args)['status'],'unknown')
        self.assertEqual(self.sender._send_payload.call_count,1)

    def test_readback_failure_can_reconcile_without_post(self):
        self.api._get.side_effect=TimeoutError()
        self.assertEqual(self.sender.call(self.args)['status'],'unknown')
        self.api._get.side_effect=None
        self.assertEqual(self.sender.call(self.args)['status'],'sent')
        self.assertEqual(self.sender._send_payload.call_count,1)

    def test_wrong_bot_group_text_are_not_success(self):
        for field,value in [('sender',{'is_bot':True,'user_id':88}),('recipient',{'chat_id':-200}),('body',{'mid':'mid.sent','text':'other'})]:
            bad=copy.deepcopy(self.message);bad[field]=value;self.api._get.return_value=[bad]
            result=self.sender.call({**self.args,'request_id':field})
            self.assertEqual(result['status'],'unknown')

    def test_validation_and_low_budget_do_not_submit(self):
        for changes in [{'chat_id':-200},{'request_id':'bad key'},{'text':''},{'text':'x'*4001},{'text':'a\x00b'},{'attachments':[]}]:
            with self.assertRaises(CloudReadError):self.sender.call({**self.args,**changes})
        with self.assertRaises(CloudReadError):self.sender.call(self.args,time.monotonic()+5)
        self.sender._send_payload.assert_not_called()

if __name__=='__main__':unittest.main()
