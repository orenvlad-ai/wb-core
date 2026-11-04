"""Fixed acknowledgement authorization, deduplication and uncertain-send checks."""
import sys
from pathlib import Path
from unittest.mock import Mock, patch
import json
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from tempfile import TemporaryDirectory
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from apps.webcore_cloud_reads_smoke import CloudReadTests
from apps.webcore_data_mcp_server import AuthIdentity
from packages.application.webcore_cloud_reads import SCOPE_MAX_POSTED_REPLY
from packages.application.webcore_max_posted_reply import MaxPostedReply

class ReplyTests(CloudReadTests):
    def setUp(self):
        super().setUp()
        self.api = Mock(chat_ids={-100})
        self.api.call.return_value = {'rows': [{'message_text_complete': True, 'message_text_chunks':['payment'], 'sender':{'is_bot':False}}]}
        self.reply = MaxPostedReply(self.api, self.root/'replies'/'max_posted_replies.sqlite3')
        self.reads.max_reply = self.reply
        self.args = dict(chat_id=-100,message_id='mid.source',source_text='payment',ui_verification='Cash UI verified all operations posted',all_operations_posted=True)
        message={'recipient':{'chat_id':-100},'sender':{'is_bot':True},'body':{'mid':'mid.reply','text':'+'},'link':{'type':'reply','message':{'mid':'mid.source'}}}
        self.reply._post=Mock(return_value={'message':message})
        self.api._get.return_value=[message]

    def test_scope_and_annotations(self):
        for identity in [AuthIdentity('owner',('webcore.max_history.read',),'oauth'), AuthIdentity('legacy',(SCOPE_MAX_POSTED_REPLY,),'bearer')]:
            self.assertNotIn('max_posted_reply',[x['name'] for x in self.rpc(identity,'tools/list')['result']['tools']])
            self.assertEqual(self.rpc(identity,'tools/call',name='max_posted_reply',arguments=self.args)['error']['data']['code'],'insufficient_scope')
        tool=next(x for x in self.gateway.list_tools() if x['name']=='max_posted_reply')
        self.assertFalse(tool['annotations']['readOnlyHint'])
        self.assertTrue(tool['annotations']['openWorldHint'])
        self.assertEqual(tool['inputSchema']['properties']['chat_id']['enum'],[-100])

    def test_positive_oauth_and_concurrent_claims(self):
        identity=AuthIdentity('owner',(SCOPE_MAX_POSTED_REPLY,),'oauth')
        result=self.rpc(identity,'tools/call',name='max_posted_reply',arguments=self.args)
        self.assertNotIn('error', result)
        self.assertEqual(self.reply._post.call_count,1)
        self.args['message_id']='mid.concurrent'
        self.api._get.return_value[0]['link']['message']['mid']='mid.concurrent'
        with ThreadPoolExecutor(4) as pool:
            list(pool.map(lambda _: self.reply.call(self.args), range(4)))
        self.assertEqual(self.reply._post.call_count,2)

    def test_real_transport_fixed_payload_and_cancel(self):
        token=self.root/'token';token.write_text('fixture-token');token.chmod(0o600)
        self.api.token_path=token;self.api.ca_path=None
        event=threading.Event()
        response=Mock();response.read.return_value=b'{"message":{}}'
        opener=Mock();opener.open.return_value.__enter__=Mock(return_value=response)
        opener.open.return_value.__exit__=Mock(return_value=False)
        with patch('packages.application.webcore_max_posted_reply.build_opener',return_value=opener):
            MaxPostedReply._post(self.reply,-100,'mid.source',None,event)
            req=opener.open.call_args.args[0]
            self.assertEqual(req.full_url,'https://platform-api2.max.ru/messages?chat_id=-100')
            self.assertEqual(json.loads(req.data),{'text':'+','link':{'type':'reply','mid':'mid.source'},'notify':False})
            event.set()
            with self.assertRaises(ValueError):MaxPostedReply._post(self.reply,-100,'mid.source',None,event)
            self.assertEqual(opener.open.call_count,1)

    def test_verified_send_and_durable_duplicate(self):
        self.assertEqual(self.reply.call(self.args)['status'],'sent')
        again=MaxPostedReply(self.api,self.reply.path)
        again._post=Mock(side_effect=AssertionError('duplicate send'))
        self.assertEqual(again.call(self.args)['status'],'sent')
        self.assertEqual(self.reply._post.call_count,1)
        self.assertEqual(again.call({**self.args,'source_text':'edited'})['status'],'source_changed')

    def test_unknown_never_retries(self):
        self.reply._post.side_effect=TimeoutError()
        self.assertEqual(self.reply.call(self.args)['status'],'unknown')
        self.assertEqual(self.reply.call(self.args)['status'],'unknown')
        self.assertEqual(self.reply._post.call_count,1)

    def test_refuse_partial_changed_and_foreign(self):
        for change in [dict(all_operations_posted=False),dict(chat_id=-200),dict(source_text='changed'),dict(text='arbitrary')]:
            with self.assertRaises(ValueError):self.reply.call({**self.args,**change})
        self.reply._post.assert_not_called()

    def test_failed_readback_and_pending_claim(self):
        self.api._get.return_value=[]
        self.assertEqual(self.reply.call(self.args)['status'],'unknown')
        with self.reply._connect() as conn:
            conn.execute("UPDATE replies SET state='pending'")
        self.assertEqual(self.reply.call(self.args)['status'],'pending')
        self.assertEqual(self.reply._post.call_count,1)

if __name__=='__main__': unittest.main()
