"""Read-only acknowledgement receipts and existing OAuth/group boundaries."""
from __future__ import annotations

import hashlib
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import webcore_cloud_reads_smoke
from apps.webcore_data_mcp_server import AuthIdentity
from packages.application.webcore_cloud_reads import SCOPE_MAX_HISTORY_READ
from packages.application.webcore_data_mcp import WebCoreDataMcpError
from packages.application.webcore_max_ack_queue import MaxAckQueue
from packages.application.webcore_max_posted_reply import MaxPostedReply


class AckStatusTests(unittest.TestCase):
    tearDown = webcore_cloud_reads_smoke.CloudReadTests.tearDown
    rpc = webcore_cloud_reads_smoke.CloudReadTests.rpc

    def setUp(self):
        webcore_cloud_reads_smoke.CloudReadTests.setUp(self)
        self.api = Mock(chat_ids={-100, -200})
        self.reply = MaxPostedReply(self.api, self.root / 'replies' / 'max_posted_replies.sqlite3')
        self.queue = MaxAckQueue(self.reply, 777, chat_id=-100)
        self.reads.max_api = self.api
        self.reads.max_reply = self.reply
        self.reads.max_ack_queue = self.queue
        self.identity = AuthIdentity('owner', (SCOPE_MAX_HISTORY_READ,), 'oauth')
        self.args = dict(chat_id=-100, message_ids=['mid.sent'])

    def insert(self, mid, state, reply_id=None, chat=-100):
        with self.reply._connect() as conn:
            conn.execute('INSERT INTO replies VALUES (?,?,?,?,?,?)',
                         (chat, mid, 'private-source-digest', 'private-UI-evidence', state, reply_id))

    def test_existing_history_scope_and_single_queue_group(self):
        tool = next(t for t in self.rpc(self.identity, 'tools/list')['result']['tools']
                    if t['name'] == 'max_ack_status')
        self.assertEqual(tool['securitySchemes'], [{'type': 'oauth2', 'scopes': [SCOPE_MAX_HISTORY_READ]}])
        self.assertEqual(tool['inputSchema']['properties']['chat_id']['enum'], [-100])
        self.assertEqual(tool['inputSchema']['properties']['message_ids']['maxItems'], 100)
        self.assertTrue(tool['annotations']['readOnlyHint'])
        self.assertFalse(tool['annotations']['openWorldHint'])
        self.assertFalse(tool['annotations']['destructiveHint'])
        self.assertTrue(tool['annotations']['idempotentHint'])
        for identity in [AuthIdentity('owner', ('webcore.max_inbox.read',), 'oauth'),
                         AuthIdentity('owner', ('webcore.max_posted_reply.write',), 'oauth'),
                         AuthIdentity('legacy', (SCOPE_MAX_HISTORY_READ,), 'bearer')]:
            self.assertNotIn('max_ack_status', [t['name'] for t in self.rpc(identity, 'tools/list')['result']['tools']])
            result = self.rpc(identity, 'tools/call', name='max_ack_status', arguments=self.args)
            self.assertEqual(result['error']['data']['code'], 'insufficient_scope')
        for chat in [-200, True, '-100']:
            result = self.rpc(self.identity, 'tools/call', name='max_ack_status',
                              arguments={**self.args, 'chat_id': chat})
            self.assertEqual(result['error']['data']['code'], 'group_not_authorized')
        self.reads.max_ack_queue = None
        self.assertNotIn('max_ack_status', [t['name'] for t in self.rpc(self.identity, 'tools/list')['result']['tools']])
        result = self.rpc(self.identity, 'tools/call', name='max_ack_status', arguments=self.args)
        self.assertEqual(result['error']['data']['code'], 'source_not_enabled')

    def test_batch_states_missing_and_foreign_receipts(self):
        states = ['queued', 'retry_wait', 'pending', 'unknown', 'sent', 'source_changed']
        for state in states:
            self.insert('mid.' + state, state, 'reply.' + state if state in {'unknown', 'sent'} else None)
        self.insert('mid.foreign', 'sent', 'reply.foreign', chat=-200)
        mids = ['mid.' + s for s in states] + ['mid.missing', 'mid.foreign']
        result = self.gateway.call_tool('max_ack_status', dict(chat_id=-100, message_ids=mids))
        self.assertEqual([r['message_id'] for r in result['rows']], mids)
        self.assertEqual([r['status'] for r in result['rows']], states + ['not_found', 'not_found'])
        self.assertEqual([r['delivery_confirmed'] for r in result['rows']],
                         [False, False, False, False, True, False, False, False])
        self.assertFalse(result['all_delivery_confirmed'])
        self.assertIsNone(result['rows'][-1]['reply_message_id'])
        self.assertTrue(all(r['automatic_resends'] is None for r in result['rows']))
        self.assertNotIn('private-', str(result))
        self.assertNotIn('reply.foreign', str(result))
        self.assertFalse(result['upstream_calls'])
        self.assertFalse(result['mutations'])
        result = self.rpc(self.identity, 'tools/call', name='max_ack_status', arguments=self.args)
        self.assertTrue(result['result']['structuredContent']['all_delivery_confirmed'])
        self.assertNotIn('cash_read', [t['name'] for t in self.rpc(self.identity, 'tools/list')['result']['tools']])

    def test_new_recovery_count_preserves_unknown_and_old_receipts(self):
        self.insert('mid.unknown', 'unknown')
        self.insert('mid.sent', 'sent', 'reply.sent')
        with self.queue._connect() as conn:
            conn.execute('''INSERT INTO max_ack_recovery
                (chat_id,source_id,attempt_started_at,resend_count,absence_proved_at)
                VALUES (?,?,?,?,?)''', (-100, 'mid.unknown', 1, 2, 2))
        result = self.gateway.call_tool('max_ack_status',
            dict(chat_id=-100, message_ids=['mid.unknown', 'mid.sent']))
        self.assertEqual(result['rows'][0]['automatic_resends'], 2)
        self.assertEqual(result['rows'][0]['status'], 'unknown')
        self.assertFalse(result['rows'][0]['delivery_confirmed'])
        self.assertIsNone(result['rows'][1]['automatic_resends'])

    def test_receipt_read_cannot_write_reconcile_or_send(self):
        self.insert('mid.sent', 'sent', 'reply.sent')
        before = hashlib.sha256(self.reply.path.read_bytes()).digest()
        original_connect = sqlite3.connect
        opened = []
        def connect(database, **kwargs):
            self.assertTrue(database.startswith('file:'))
            self.assertTrue(database.endswith('?mode=ro'))
            self.assertTrue(kwargs['uri'])
            conn = original_connect(database, **kwargs)
            def authorize(action, *rest):
                if action == sqlite3.SQLITE_SELECT:
                    self.assertEqual(conn.execute('PRAGMA query_only').fetchone()[0], 1)
                return sqlite3.SQLITE_OK
            conn.set_authorizer(authorize)
            opened.append(conn)
            return conn
        forbidden = AssertionError('Status read must not mutate, reconcile or call MAX')
        with patch.object(self.queue, '_connect', side_effect=forbidden), \
                patch.object(self.reply, '_connect', side_effect=forbidden), \
                patch.object(self.queue, 'call', side_effect=forbidden), \
                patch.object(self.queue, 'run_once', side_effect=forbidden), \
                patch.object(self.reply, '_post', side_effect=forbidden), \
                patch('packages.application.webcore_cloud_reads.sqlite3.connect', side_effect=connect):
            result = self.gateway.call_tool('max_ack_status', self.args)
        self.assertTrue(result['all_delivery_confirmed'])
        self.assertEqual(len(opened), 1)
        self.api.call.assert_not_called()
        self.api._get.assert_not_called()
        self.assertEqual(before, hashlib.sha256(self.reply.path.read_bytes()).digest())

    def test_boundaries_cancellation_and_unavailable_journal_never_bootstrap(self):
        for mids in [[], ['x'] * 101, ['x', 'x'], [True], ['bad/id'], ['x' * 201], 'mid.sent']:
            with self.subTest(mids=mids), self.assertRaises(WebCoreDataMcpError) as error:
                self.gateway.call_tool('max_ack_status', {**self.args, 'message_ids': mids})
            self.assertEqual(error.exception.code, 'invalid_arguments')
        with self.assertRaises(WebCoreDataMcpError) as error:
            self.gateway.call_tool('max_ack_status', {**self.args, 'source_text': 'payment'})
        self.assertEqual(error.exception.code, 'invalid_arguments')
        cancelled = threading.Event()
        cancelled.set()
        for kwargs in [dict(cancel_event=cancelled), dict(deadline_at=time.monotonic() - 1)]:
            with self.assertRaises(WebCoreDataMcpError) as error:
                self.gateway.call_tool('max_ack_status', self.args, **kwargs)
            self.assertEqual(error.exception.code, 'deadline_exceeded')
        with self.assertRaises(WebCoreDataMcpError) as error:
            self.gateway.call_tool('max_ack_status', self.args)
        self.assertEqual(error.exception.code, 'source_unavailable')
        self.assertFalse(self.reply.path.exists())
        self.reply.path.parent.mkdir(mode=0o700)
        sqlite3.connect(self.reply.path).close()
        before = self.reply.path.read_bytes()
        with self.assertRaises(WebCoreDataMcpError):
            self.gateway.call_tool('max_ack_status', self.args)
        self.assertEqual(self.reply.path.read_bytes(), before)
        with closing(sqlite3.connect(self.reply.path)) as conn:
            self.assertEqual(conn.execute('SELECT name FROM sqlite_master').fetchall(), [])


if __name__ == '__main__':
    unittest.main()
