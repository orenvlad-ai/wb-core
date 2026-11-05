"""Durable queue acceptance, crash boundaries and own-bot reconciliation."""
import copy
from contextlib import contextmanager
import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.webcore_cloud_reads import CloudReadError
from packages.application.webcore_max_posted_reply import MaxPostedReply
from packages.application.webcore_max_ack_queue import MaxAckQueue


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.api = Mock(chat_ids={-100})
        self.sources = {}
        self.messages = {}
        self.history = []
        self.api.call.side_effect = lambda name, args, *rest: {'rows': [self.sources[args['message_id']]]}
        self.api._get.side_effect = self.get
        self.reply = MaxPostedReply(self.api, Path(self.temp.name).resolve()/'private'/'max_posted_replies.sqlite3')
        self.reply._post = Mock(side_effect=self.post)
        self.queue = MaxAckQueue(self.reply, 777, chat_id=-100)
        self.args = self.args_for('mid.source')

    def args_for(self, mid, text='payment'):
        self.sources[mid] = dict(chat_id=-100, message_id=mid, timestamp_ms=1000,
                                 message_text_complete=True, message_text_chunks=[text],
                                 sender={'user_id': 99, 'is_bot': False})
        return dict(chat_id=-100, message_id=mid, source_text=text,
                    ui_verification='Cash UI: both operations posted', all_operations_posted=True)

    @staticmethod
    def ack(source, mid='mid.reply', bot=777):
        return dict(recipient={'chat_id': -100, 'chat_type': 'chat'},
                    sender={'user_id': bot, 'is_bot': True}, timestamp=2000,
                    body={'mid': mid, 'text': '+'},
                    link={'type': 'reply', 'message': {'mid': source}})

    def post(self, chat, mid, deadline, cancel):
        ack = self.ack(mid, 'reply.' + mid)
        self.messages[ack['body']['mid']] = ack
        return {'message': ack}

    def get(self, params, *args):
        if 'message_ids' in params:
            return [self.messages[params['message_ids']]] if params['message_ids'] in self.messages else []
        return [m for m in self.history if params.get('from') is None or m['timestamp'] <= params['from']][:params['count']]

    def due(self):
        with self.queue._connect() as conn:
            conn.execute('UPDATE max_ack_queue SET next_attempt_at=0,lease_until=0')

    def legacy(self, state='unknown', reply_id=None, digest=None):
        with self.reply._connect() as conn:
            conn.execute('INSERT INTO replies VALUES (?,?,?,?,?,?)',
                         (-100, self.args['message_id'], digest or hashlib.sha256(b'payment').hexdigest(), 'original UI evidence', state, reply_id))

    def test_acceptance_is_durable_without_network_or_delivery(self):
        result = self.queue.call(self.args, deadline=time.monotonic()+.1)
        self.assertEqual(result['status'], 'queued')
        self.assertTrue(result['accepted'])
        self.assertFalse(result['delivery_confirmed'])
        self.assertFalse(result['sent_now'])
        self.api.call.assert_not_called()
        self.reply._post.assert_not_called()
        queue = MaxAckQueue(MaxPostedReply(self.api, self.reply.path), 777, chat_id=-100)
        self.assertEqual(queue.call(self.args)['status'], 'queued')
        with queue._connect() as conn:
            self.assertEqual(conn.execute('SELECT source_text FROM max_ack_queue').fetchone()[0], 'payment')
            self.assertEqual(conn.execute('SELECT evidence FROM replies').fetchone()[0], self.args['ui_verification'])
        self.assertEqual(self.queue.run_once()['status'], 'sent')
        self.assertIsNone(self.queue.run_once())
        self.assertTrue(queue.call(self.args)['delivery_confirmed'])

    def test_exact_authorization_attestation_and_cancellation(self):
        for changes in [dict(chat_id=True), dict(chat_id=-101), dict(all_operations_posted=1),
                        dict(ui_verification=' '), dict(source_text=' '*16001+'x'),
                        dict(message_id='bad/identity'), dict(text='arbitrary')]:
            with self.assertRaises(CloudReadError):
                self.queue.call({**self.args, **changes})
        cancelled = threading.Event(); cancelled.set()
        with self.assertRaises(CloudReadError):
            self.queue.call(self.args, cancel_event=cancelled)
        with self.assertRaises(CloudReadError):
            self.queue.call(self.args, deadline=time.monotonic()-1)
        with self.assertRaises(ValueError):
            MaxAckQueue(self.reply, True, chat_id=-100)
        self.reply._post.assert_not_called()

    def test_changed_request_preserves_original_payload_and_evidence(self):
        self.queue.call(self.args)
        changed = {**self.args, 'source_text': 'edited', 'ui_verification': 'other'}
        result = self.queue.call(changed)
        self.assertEqual(result['status'], 'source_changed')
        self.assertFalse(result['accepted'])
        with self.queue._connect() as conn:
            self.assertEqual(conn.execute('SELECT source_text FROM max_ack_queue').fetchone()[0], 'payment')
            self.assertEqual(conn.execute('SELECT evidence FROM replies').fetchone()[0], self.args['ui_verification'])

    def test_concurrent_enqueues_and_workers_submit_once(self):
        with ThreadPoolExecutor(6) as pool:
            list(pool.map(lambda _: self.queue.call(self.args), range(6)))
            results = list(pool.map(lambda _: self.queue.run_once(), range(6)))
        self.assertEqual(self.reply._post.call_count, 1)
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(self.queue.call(self.args)['status'], 'sent')

    def test_pre_submit_read_retries_with_backoff_and_fair_due_order(self):
        self.queue.call(self.args)
        second = self.args_for('mid.second')
        self.queue.call(second)
        original = self.api.call.side_effect
        self.api.call.side_effect = TimeoutError('private token must not persist')
        result = self.queue.run_once()
        self.assertEqual(result['status'], 'retry_wait')
        self.reply._post.assert_not_called()
        with self.queue._connect() as conn:
            self.assertGreater(conn.execute('SELECT next_attempt_at FROM max_ack_queue WHERE source_id=?', (self.args['message_id'],)).fetchone()[0], time.time())
            self.assertEqual(conn.execute('SELECT error_code FROM reply_diagnostics').fetchone()[0], 'upstream_timeout')
        self.api.call.side_effect = original
        self.assertEqual(self.queue.run_once()['status'], 'sent')
        self.assertEqual(self.reply._post.call_args.args[1], 'mid.second')
        self.due()
        self.assertEqual(self.queue.run_once()['status'], 'sent')
        self.assertEqual(self.reply._post.call_count, 2)

    def test_pilot_group_is_narrower_than_history_allowlist(self):
        self.api.chat_ids = {-100, -200}
        with self.assertRaises(CloudReadError):
            self.queue.call({**self.args, 'chat_id': -200})
        for chat in (True, 100, -300):
            with self.assertRaises(ValueError):
                MaxAckQueue(self.reply, 777, chat_id=chat)
        with self.reply._connect() as conn:
            conn.execute('INSERT INTO replies VALUES (?,?,?,?,?,?)', (-200, 'mid.foreign', 'x', 'e', 'unknown', None))
        self.assertIsNone(self.queue.run_once())

    def test_missing_source_is_terminal_before_submit(self):
        self.queue.call(self.args)
        self.api.call.side_effect = CloudReadError('not visible', 'message_not_found')
        self.assertEqual(self.queue.run_once()['status'], 'source_changed')
        self.due()
        self.assertIsNone(self.queue.run_once())
        self.reply._post.assert_not_called()

    def test_crash_during_presubmit_read_can_retry_safely(self):
        self.queue.call(self.args)
        original = self.api.call.side_effect
        self.api.call.side_effect = SystemExit()
        with self.assertRaises(SystemExit):
            self.queue.run_once()
        self.reply._post.assert_not_called()
        self.api.call.side_effect = original
        self.due()
        self.assertEqual(self.queue.run_once()['status'], 'sent')
        self.assertEqual(self.reply._post.call_count, 1)

    def test_source_edit_and_identity_mismatch_never_submit(self):
        for field, value in [('message_text_chunks', ['edited']), ('message_text_complete', False),
                             ('chat_id', -101), ('message_id', 'mid.other'),
                             ('sender', {'is_bot': True}), ('message_text_redacted', True)]:
            with self.subTest(field=field):
                mid = 'mid.'+field
                args = self.args_for(mid)
                self.sources[mid][field] = value
                self.queue.call(args)
                self.assertEqual(self.queue.run_once()['status'], 'source_changed')
        self.reply._post.assert_not_called()

    def test_unknown_without_reply_id_reconciles_history_without_repost(self):
        self.queue.call(self.args)
        self.reply._post.side_effect = TimeoutError()
        self.assertEqual(self.queue.run_once()['status'], 'unknown')
        ack = self.ack(self.args['message_id'])
        self.history = [ack]; self.messages['mid.reply'] = ack
        self.due()
        result = self.queue.run_once()
        self.assertEqual(result['status'], 'sent')
        self.assertFalse(result['sent_now'])
        self.assertEqual(self.reply._post.call_count, 1)

    def test_unknown_absence_never_becomes_retry_or_post(self):
        self.legacy()
        for _ in range(3):
            self.due()
            self.assertEqual(self.queue.run_once()['status'], 'unknown')
        self.reply._post.assert_not_called()

    def test_uncertain_claim_commit_never_reenters_submit_queue(self):
        self.queue.call(self.args)
        original = self.queue._connect
        calls = 0

        @contextmanager
        def uncertain_commit():
            nonlocal calls
            calls += 1
            number = calls
            with original() as conn:
                yield conn
            if number == 2:  # Claim transaction committed, receipt was lost.
                raise OSError('simulated journal commit uncertainty')

        self.queue._connect = uncertain_commit
        self.assertEqual(self.queue.run_once()['status'], 'unknown')
        self.reply._post.assert_not_called()
        self.queue._connect = original
        self.due()
        self.assertEqual(self.queue.run_once()['status'], 'unknown')
        self.reply._post.assert_not_called()

    def test_crash_after_claim_recovers_read_only(self):
        self.queue.call(self.args)
        self.reply._post.side_effect = SystemExit('simulated process death')
        with self.assertRaises(SystemExit):
            self.queue.run_once()
        with self.reply._connect() as conn:
            self.assertEqual(conn.execute('SELECT state FROM replies').fetchone()[0], 'pending')
        self.due()
        self.assertEqual(self.queue.run_once()['status'], 'unknown')
        self.assertEqual(self.reply._post.call_count, 1)

    def test_legacy_digest_and_own_bot_readback_are_required(self):
        self.legacy(reply_id='mid.reply')
        ack = self.ack(self.args['message_id'], bot=888)
        self.messages['mid.reply'] = ack; self.history = [ack]
        self.assertEqual(self.queue.run_once()['status'], 'unknown')
        self.due()
        ack['sender']['user_id'] = 777
        self.sources[self.args['message_id']]['message_text_chunks'] = ['edited']
        self.assertEqual(self.queue.run_once()['status'], 'unknown')
        self.due()
        self.sources[self.args['message_id']]['message_text_chunks'] = ['payment']
        self.assertEqual(self.queue.run_once()['status'], 'sent')
        self.reply._post.assert_not_called()
        with self.queue._connect() as conn:
            self.assertIsNone(conn.execute('SELECT source_text FROM max_ack_queue').fetchone()[0])
            self.assertEqual(conn.execute('SELECT evidence FROM replies').fetchone()[0], 'original UI evidence')

    def test_legacy_sent_and_foreign_unknown_are_not_adopted(self):
        self.legacy(state='sent', reply_id='mid.old')
        with self.reply._connect() as conn:
            conn.execute('INSERT INTO replies VALUES (?,?,?,?,?,?)', (-200, 'mid.foreign', 'x', 'e', 'unknown', None))
        self.assertIsNone(self.queue.run_once())
        self.assertEqual(self.queue.call(self.args)['status'], 'sent')
        self.reply._post.assert_not_called()

    def test_post_readback_failure_preserves_id_and_reconciles(self):
        self.queue.call(self.args)
        original = self.api._get.side_effect
        self.api._get.side_effect = TimeoutError()
        result = self.queue.run_once()
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['reply_message_id'], 'reply.mid.source')
        self.api._get.side_effect = original
        self.due()
        self.assertEqual(self.queue.run_once()['status'], 'sent')
        self.assertEqual(self.reply._post.call_count, 1)

    def test_canonical_signed_group_matching_remains_corroborated(self):
        seq = 117399484548871240
        mid = f'mid.{(-100) & ((1 << 64)-1):016x}{seq:016x}'
        args = self.args_for(mid)
        self.sources[mid]['sequence'] = seq
        self.queue.call(args)
        self.reply._post.side_effect = TimeoutError()
        self.queue.run_once()
        ack = self.ack(mid)
        ack['link'] = dict(type='reply', chat_id=100, sender={'user_id': 99},
                           message={'mid': f'mid.{100:016x}{seq:016x}', 'seq': seq, 'text': 'payment'})
        self.history = [ack]; self.messages['mid.reply'] = ack
        self.due()
        bad = copy.deepcopy(ack); bad['link']['sender']['user_id'] = 98
        self.history = [bad]
        self.assertEqual(self.queue.run_once()['status'], 'unknown')
        self.history = [ack]; self.due()
        self.assertEqual(self.queue.run_once()['status'], 'sent')
        self.assertEqual(self.reply._post.call_count, 1)

    def test_persistent_bounded_history_scan_reaches_old_ack(self):
        self.legacy()
        self.queue.HISTORY_PAGES = 1
        self.queue.HISTORY_PAGE_SIZE = 2
        ack = self.ack(self.args['message_id'])
        self.messages['mid.reply'] = ack
        self.history = [self.ack('mid.other', str(stamp)) for stamp in (5000, 4000, 3000)] + [ack]
        for message, stamp in zip(self.history, (5000, 4000, 3000, 2000)):
            message['timestamp'] = stamp
        self.assertEqual(self.queue.run_once()['status'], 'unknown')
        self.due()
        self.assertEqual(self.queue.run_once()['status'], 'unknown')
        self.due()
        self.assertEqual(self.queue.run_once()['status'], 'sent')
        self.reply._post.assert_not_called()


if __name__ == '__main__':
    unittest.main()
