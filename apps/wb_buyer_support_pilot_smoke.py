#!/usr/bin/env python3
"""Synthetic pilot E2E/transport contracts; never uses real network or credentials."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import threading
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from packages.application.wb_buyer_support import BuyerSupportRepository
from packages.application.wb_buyer_support_pilot import BuyerSupportPilotService, BuyerSupportPilotError, PilotConfig, _unconfirmed_execution_assertion
from packages.adapters.wb_buyer_support_pilot import OfficialPilotWbTransport, ResponsesProposalProvider, PilotTransportError

CABINET, CHAT, CLAIM = 'pilot-cabinet', 'owner-chat', '11111111-1111-4111-8111-111111111111'


def claim_row(**changes):
    return {'id': CLAIM, 'srid': 'purchase', 'nm_id': 123, 'status': 0, 'status_ex': 0,
            'user_comment': 'Пузырь не ушёл после совета.', 'imt_name': 'Glass', 'dt': '2026-10-06T10:00:00',
            'dt_update': '2026-10-06T10:00:00', 'actions': ['autorefund1', 'approve2'],
            'photos': [], 'video_paths': []} | changes


def append(repo, sender='client', text='Как установить?', eid=None, ts=None, media=None):
    cursor = repo.cursor(CABINET)
    next_ = (cursor or 0) + 1
    event = {'eventID': eid or 'event-' + str(next_), 'chatID': CHAT, 'addTimestamp': ts or 1791280800000 + next_,
             'addTime': '2026-10-06T10:00:00Z', 'sender': sender,
             'message': {'text': text, 'attachments': media or {}}}
    repo.save_event_page(CABINET, {'result': {'events': [event], 'next': next_, 'totalEvents': 1}}, expected_cursor=cursor)


class FakeProvider:
    configured = True
    profile = {'model': 'fake', 'reasoning': 'medium', 'store': False}
    def __init__(self):
        self.calls, self.action, self.hook = 0, 'none', None
        self.basis_type = 'text_sufficient'
        self.contexts = []
    def generate(self, *, policy, context):
        self.calls += 1; self.contexts.append(context)
        if self.hook: self.hook()
        action = self.action
        if any(e['event_type'] == 'operation_confirmed' for e in context['internal_events']): action = 'none'
        return {'proposal': {'case_id': context['case_id'],
                'reply': '' if action != 'none' or context['kind'] == 'claim' else 'Здравствуйте! Помогу с установкой.',
                'operation': {'kind': action, 'claim_id': CLAIM if action in ('autorefund1', 'approve2', 'read_claim') else '',
                              'operation_id': '', 'basis': 'Описание текущей проблемы', 'basis_type': self.basis_type}},
                'response_id': 'fake-response', 'usage': {'input_tokens': 1, 'output_tokens': 1}, 'model_profile': self.profile}


class FakeWb:
    configured = True
    def __init__(self):
        self.sends, self.decisions, self.reads = 0, 0, 0
        self.queue, self.hook, self.unknown, self.crash = [], None, False, False
        self.row = claim_row(); self.archive = False
    def refresh(self, repo, cabinet, *, kind, item_id):
        self.reads += 1
        if self.hook:
            hook, self.hook = self.hook, None; hook(repo)
        for sender, text, ts in self.queue: append(repo, sender=sender, text=text, ts=ts)
        self.queue.clear()
        repo.save_claim_page(cabinet, {'claims': [self.row], 'total': 1}, archive=self.archive)
        return {'complete': True, 'reply_sign': 'transient-not-stored', 'observed_claim_ids': [CLAIM]}
    def read_claim(self, repo, cabinet, claim_id):
        self.reads += 1
        repo.save_claim_page(cabinet, {'claims': [self.row], 'total': 1}, archive=self.archive)
        return self.row | {'archive': self.archive}
    def send_message(self, *, reply_sign, text):
        self.sends += 1
        ts = 1791280809000
        self.queue.append(('seller', text, ts))
        if self.crash: raise SystemExit('simulated process death')
        if self.unknown: raise PilotTransportError('wb_write_result_unknown')
        return {'http_status': 200, 'payload': {'result': {'chatID': CHAT, 'addTime': ts}, 'errors': []}}
    def decide_claim(self, *, claim_id, action):
        self.decisions += 1
        self.archive = True
        self.row = claim_row(status=2, status_ex=5 if action == 'autorefund1' else 10, actions=[],
                             dt_update='2026-10-06T10:01:00', wb_comment='Approved')
        if self.crash: raise SystemExit('simulated process death')
        if self.unknown: raise PilotTransportError('wb_write_result_unknown')
        return {'http_status': 200, 'payload': {}}


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name)
        self.repo = BuyerSupportRepository(self.runtime)
        self.repo.save_chats(CABINET, {'result': [{'chatID': CHAT, 'clientName': 'Owner',
            'replySign': 'do-not-store', 'goodCard': {'rid': 'purchase', 'nmID': 123, 'name': 'Glass'}}]})
        append(self.repo)
        self.repo.save_claim_page(CABINET, {'claims': [claim_row()], 'total': 1}, archive=False)
        self.provider, self.wb = FakeProvider(), FakeWb()
        self.cfg = PilotConfig(True, CABINET, frozenset([CHAT]), frozenset([CLAIM]), 'personal-service')
        self.service = BuyerSupportPilotService(self.runtime, self.repo, self.provider, self.wb, self.cfg)
        self.n = 0
    def request(self):
        self.n += 1; return 'request-' + str(self.n)
    def draft(self):
        detail = self.service.refresh(CABINET, kind='chat', item_id=CHAT, request_id=self.request(), actor='operator')
        return self.service.propose(CABINET, chat_id=CHAT, expected_context_version=detail['context_version'], request_id=self.request(), actor='operator')
    def send(self, d, **changes):
        return self.service.send(CABINET, draft_id=d['draft_id'], expected_context_version=d['context_version'],
                                 request_id=self.request(), actor='operator', confirmed=True, **changes)
    def decide(self, d, **changes):
        changes.setdefault('text_basis_confirmed', True)
        return self.service.claim(CABINET, draft_id=d['draft_id'], claim_id=CLAIM,
                action=d['return_proposal']['action'], expected_context_version=d['context_version'],
                expected_claim_version=d['return_proposal']['claim_version'], request_id=self.request(), actor='operator', confirmed=True, **changes)
    def assert_code(self, code, callback):
        with self.assertRaises(BuyerSupportPilotError) as result: callback()
        self.assertEqual(result.exception.code, code)

    def test_default_off_no_io_or_store_creation(self):
        with TemporaryDirectory() as directory:
            srv = BuyerSupportPilotService(Path(directory), self.repo, self.provider, self.wb, PilotConfig())
            detail = srv.detail(CABINET, 'chat', CHAT)
            self.assertFalse(detail['pilot']['enabled']); self.assertFalse(detail['pilot']['automatic_actions'])
            self.assertFalse(srv.store.path.exists())
            self.assert_code('pilot_disabled', lambda: srv.propose(CABINET, chat_id=CHAT,
                expected_context_version=detail['context_version'], request_id='off', actor='operator'))
            self.assertEqual((self.provider.calls, self.wb.reads), (0, 0))
    def test_not_allowlisted_no_calls(self):
        self.service._config = PilotConfig(True, CABINET, frozenset(), frozenset(), 'personal-service')
        self.assert_code('pilot_target_not_allowlisted', self.draft)
        self.assertEqual((self.provider.calls, self.wb.reads), (0, 0))
    def test_preview_then_send_then_readback(self):
        d = self.draft(); self.assertEqual(self.wb.sends, 0)
        op = self.send(d); self.assertEqual(op['state'], 'pending_readback'); self.assertEqual(self.wb.sends, 1)
        done = self.service.reconcile(CABINET, operation_id=op['operation_id'], request_id=self.request(), actor='operator')
        self.assertEqual(done['state'], 'confirmed')
        self.assertEqual(self.wb.sends, 1)
        detail = self.service.detail(CABINET, 'chat', CHAT)
        self.assertEqual(detail['messages'][-1]['sender'], 'seller')
        self.assertNotIn('replySign', self.service.store.path.read_bytes().decode('latin1'))
        self.assertNotIn('transient-not-stored', self.service.store.path.read_bytes().decode('latin1'))
    def test_same_and_alternative_request_never_send_twice(self):
        d = self.draft(); request_id = self.request()
        args = dict(draft_id=d['draft_id'], expected_context_version=d['context_version'], request_id=request_id, actor='operator', confirmed=True)
        op = self.service.send(CABINET, **args)
        same = self.service.send(CABINET, **args)
        other = self.send(d)
        self.assertEqual(op['operation_id'], same['operation_id']); self.assertEqual(op['operation_id'], other['operation_id'])
        self.assertEqual(self.wb.sends, 1)
        self.assertEqual(self.service.operation(CABINET, request_id=request_id)['operation_id'], op['operation_id'])
        args['expected_context_version'] = 'changed'
        self.assert_code('request_id_conflict', lambda: self.service.send(CABINET, **args))
    def test_restart_unknown_send_cannot_repeat(self):
        d = self.draft(); self.wb.crash = True
        with self.assertRaises(SystemExit): self.send(d)
        restart = BuyerSupportPilotService(self.runtime, self.repo, self.provider, self.wb, self.cfg)
        detail = restart.detail(CABINET, 'chat', CHAT); op = detail['workflow']['operations'][0]
        self.assertEqual(restart.operation(CABINET, op['operation_id'])['state'], 'unknown')
        old = self.send(d); self.assertEqual(self.wb.sends, 1)
        done = restart.reconcile(CABINET, operation_id=old['operation_id'], request_id=self.request(), actor='operator')
        self.assertEqual(done['state'], 'unknown')  # receipt correlation was lost, text alone insufficient
        d2 = self.draft()
        self.assert_code('previous_operation_requires_readback', lambda: self.send(d2))
    def test_unknown_claim_restart_confirms_and_drafts_without_buyer(self):
        self.provider.action = 'autorefund1'; d = self.draft(); self.wb.crash = True
        with self.assertRaises(SystemExit): self.decide(d)
        self.wb.crash = False
        restart = BuyerSupportPilotService(self.runtime, self.repo, self.provider, self.wb, self.cfg)
        op = restart.detail(CABINET, 'chat', CHAT)['workflow']['operations'][0]
        done = restart.reconcile(CABINET, operation_id=op['operation_id'], request_id=self.request(), actor='operator')
        self.assertEqual(done['state'], 'confirmed'); self.assertTrue(done['refresh_draft_id'])
        detail = restart.detail(CABINET, 'chat', CHAT)
        self.assertEqual(len(detail['messages']), 1)
        self.assertEqual(detail['workflow']['latest_draft']['status'], 'ready')
        self.assertEqual(self.wb.decisions, 1); self.assertEqual(self.wb.sends, 0)
        done2 = restart.reconcile(CABINET, operation_id=op['operation_id'], request_id=self.request(), actor='operator')
        self.assertEqual(self.provider.calls, 2)
    def test_return_separate_confirmation_and_both_mappings(self):
        for action in ('autorefund1', 'approve2'):
            with self.subTest(action=action):
                # independent source/workflow for each case
                self.setUp(); self.provider.action = action; d = self.draft()
                self.assert_code('empty_reply', lambda: self.send(d))
                op = self.decide(d)
                done = self.service.reconcile(CABINET, operation_id=op['operation_id'], request_id=self.request(), actor='operator')
                self.assertEqual(done['state'], 'confirmed'); self.assertTrue(done['refresh_draft_id'])
                self.assertEqual(self.wb.decisions, 1); self.assertEqual(self.wb.sends, 0)
    def test_new_buyer_during_generation_stales_draft(self):
        self.provider.hook = lambda: append(self.repo, text='Новый вопрос')
        d = self.draft(); self.assertEqual(d['status'], 'stale')
        self.assert_code('stale_draft', lambda: self.send(d))
        self.assertEqual(self.wb.sends, 0)
    def test_new_buyer_during_fresh_preflight_blocks_write(self):
        d = self.draft(); self.wb.hook = lambda repo: append(repo, text='Новый вопрос')
        self.assert_code('stale_context', lambda: self.send(d))
        self.assertEqual(self.wb.sends, 0)
    def test_changed_claim_actions_after_confirmation_blocks_write(self):
        self.provider.action = 'autorefund1'; d = self.draft()
        self.wb.row = claim_row(actions=['approve2'])
        self.assert_code('stale_context', lambda: self.decide(d)); self.assertEqual(self.wb.decisions, 0)
    def test_unknown_status_never_confirmed(self):
        self.provider.action = 'autorefund1'; d = self.draft(); op = self.decide(d)
        self.wb.row = claim_row(status=2, status_ex=999, actions=[])
        done = self.service.reconcile(CABINET, operation_id=op['operation_id'], request_id=self.request(), actor='operator')
        self.assertEqual(done['state'], 'unknown'); self.assertEqual(self.provider.calls, 1)
    def test_empty_poll_does_not_stale_and_request_reuses_generation(self):
        detail = self.service.refresh(CABINET, kind='chat', item_id=CHAT, request_id=self.request(), actor='operator')
        request_id = self.request(); args = dict(chat_id=CHAT, expected_context_version=detail['context_version'], request_id=request_id, actor='operator')
        d = self.service.propose(CABINET, **args)
        self.repo.save_claim_page(CABINET, {'claims': [claim_row()], 'total': 1}, archive=False)
        self.assertEqual(detail['context_version'], self.service.detail(CABINET, 'chat', CHAT)['context_version'])
        self.assertEqual(d, self.service.propose(CABINET, **args)); self.assertEqual(self.provider.calls, 1)
        self.assertEqual(self.service.operation(CABINET, request_id=request_id)['draft_id'], d['draft_id'])
    def test_claim_before_chat_is_readable_but_cannot_dispatch(self):
        self.repo.save_claim_page(CABINET, {'claims': [claim_row(srid='not-yet-chat')], 'total': 1}, archive=False)
        self.provider.action = 'autorefund1'
        self.wb.row = claim_row(srid='not-yet-chat')
        detail = self.service.refresh(CABINET, kind='claim', item_id=CLAIM, request_id=self.request(), actor='operator')
        self.assertEqual(detail['claims'][0]['link_state'], 'orphan')
        d = self.service.propose(CABINET, kind='claim', item_id=CLAIM,
                expected_context_version=detail['context_version'], request_id=self.request(), actor='operator')
        self.assertEqual(d['text'], '')
        self.assert_code('claim_chat_link_unconfirmed', lambda: self.decide(d))
        self.assertEqual(self.wb.decisions, 0)
    def test_unverified_media_never_promoted(self):
        append(self.repo, text='Вот фото', media={'images': [{'downloadID': 'image-1'}]})
        self.draft(); context = self.provider.contexts[-1]
        self.assertEqual(context['media_verification']['state'], 'unavailable')
        self.assertEqual(context['media_evidence'][0]['state'], 'unavailable')
        class BadVerifier:
            def read_verified(self, **kwargs): return [{'source_id': 'fabricated', 'attachment_hash': 'x', 'verified_feature': 'crack'}]
        self.service.verification = BadVerifier()
        self.assert_code('invalid_media_verification', lambda: self.service.detail(CABINET, 'chat', CHAT))
    def test_model_unknown_result_is_not_automatically_repaid(self):
        def fail(): raise PilotTransportError('provider_result_unknown')
        self.provider.hook = fail
        d = self.draft(); self.assertEqual(d['status'], 'generation_unknown')
        self.assert_code('generation_result_unknown', self.draft)
        self.assertEqual(self.provider.calls, 1)
    def test_conversation_lock_and_automatic_forbidden(self):
        with self.service.store.lock(CABINET, 'chat', CHAT):
            self.assert_code('conversation_busy', self.draft)
        self.assert_code('automatic_dispatch_forbidden_in_pilot', lambda: self.service.set_automatic_actions(True))
        self.assertEqual(self.service.set_automatic_actions(False), {'automatic_actions': False})
    def test_new_buyer_during_return_readback_included_in_followup(self):
        self.provider.action = 'approve2'; d = self.draft(); op = self.decide(d)
        self.wb.hook = lambda repo: append(repo, text='Когда надо сдавать?')
        done = self.service.reconcile(CABINET, operation_id=op['operation_id'], request_id=self.request(), actor='operator')
        self.assertEqual(done['state'], 'confirmed')
        self.assertEqual(self.provider.contexts[-1]['messages'][-1]['text'], 'Когда надо сдавать?')
        self.assertEqual(self.wb.sends, 0)

    def test_visible_refresh_required_before_model(self):
        detail = self.service.detail(CABINET, 'chat', CHAT)
        self.assertFalse(detail['pilot']['source_fresh'])
        self.assert_code('source_refresh_required', lambda: self.service.propose(CABINET, chat_id=CHAT,
            expected_context_version=detail['context_version'], request_id=self.request(), actor='operator'))
        self.assertEqual(self.provider.calls, 0)
        fresh = self.service.refresh(CABINET, kind='chat', item_id=CHAT, request_id=self.request(), actor='operator')
        self.assertTrue(fresh['pilot']['source_fresh']); self.assertTrue(fresh['pilot']['source_refreshed_at'])
        self.assertEqual((self.wb.sends, self.wb.decisions), (0, 0))

    def test_refresh_lost_response_request_lookup_and_no_repeated_io(self):
        request_id = self.request()
        detail = self.service.refresh(CABINET, kind='chat', item_id=CHAT, request_id=request_id, actor='operator')
        reads = self.wb.reads
        receipt = self.service.operation(CABINET, request_id=request_id)
        self.assertEqual(receipt['context_version'], detail['context_version'])
        self.assertIn('messages', receipt)
        self.service.refresh(CABINET, kind='chat', item_id=CHAT, request_id=request_id, actor='operator')
        self.assertEqual(self.wb.reads, reads)

    def test_failed_read_refresh_has_terminal_receipt_and_safe_new_request(self):
        request_id = self.request()
        original = self.wb.refresh
        def failed(*args, **kwargs): raise PilotTransportError('source_sync_busy')
        self.wb.refresh = failed
        self.assert_code('source_sync_busy', lambda: self.service.refresh(CABINET, kind='chat', item_id=CHAT, request_id=request_id, actor='operator'))
        receipt = self.service.operation(CABINET, request_id=request_id)
        self.assertEqual(receipt['kind'], 'refresh'); self.assertEqual(receipt['state'], 'failed')
        self.assertFalse(receipt['write_attempted']); self.assertEqual(receipt['error'], 'source_sync_busy')
        self.wb.refresh = original
        self.service.refresh(CABINET, kind='chat', item_id=CHAT, request_id=self.request(), actor='operator')
        self.assertEqual((self.wb.sends, self.wb.decisions, self.provider.calls), (0, 0, 0))

    def test_busy_second_caller_cannot_mark_first_running_request_failed(self):
        d = self.draft(); request_id = self.request()
        args = dict(draft_id=d['draft_id'], expected_context_version=d['context_version'], request_id=request_id, actor='operator', confirmed=True)
        entered, release = threading.Event(), threading.Event()
        def blocked(repo):
            entered.set()
            if not release.wait(3): raise RuntimeError('test timeout')
        self.wb.hook = blocked
        result = []
        worker = threading.Thread(target=lambda: result.append(self.service.send(CABINET, **args)))
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            self.assert_code('conversation_busy', lambda: self.service.send(CABINET, **args))
            receipt = self.service.operation(CABINET, request_id=request_id)
            self.assertEqual(receipt['state'], 'request_result_unknown')
            self.assertNotEqual(receipt.get('error'), 'conversation_busy')
        finally:
            release.set(); worker.join(3)
        self.assertEqual(result[0]['state'], 'pending_readback'); self.assertEqual(self.wb.sends, 1)

    def test_saved_operation_reconcile_after_off_and_allowlist_removed(self):
        d = self.draft(); op = self.send(d)
        self.service._config = PilotConfig(False, CABINET, frozenset(), frozenset(), '')
        self.provider.configured = False
        self.assertTrue(self.service.operation(CABINET, op['operation_id'])['can_reconcile'])
        done = self.service.reconcile(CABINET, operation_id=op['operation_id'], request_id=self.request(), actor='operator')
        self.assertEqual(done['state'], 'confirmed'); self.assertEqual(self.wb.sends, 1)

    def test_confirmed_claim_provider_unavailable_then_recover(self):
        self.provider.action = 'autorefund1'; d = self.draft(); op = self.decide(d)
        self.provider.configured = False
        done = self.service.reconcile(CABINET, operation_id=op['operation_id'], request_id=self.request(), actor='operator')
        self.assertEqual(done['state'], 'confirmed'); self.assertEqual(done['response_draft_state'], 'draft_pending')
        self.provider.configured = True
        restart = BuyerSupportPilotService(self.runtime, self.repo, self.provider, self.wb, self.cfg)
        done = restart.reconcile(CABINET, operation_id=op['operation_id'], request_id=self.request(), actor='operator')
        self.assertTrue(done['refresh_draft_id']); self.assertEqual(self.wb.decisions, 1)

    def test_read_claim_internal_continuation_without_buyer_or_write(self):
        original = self.provider.generate
        def generate(**kwargs):
            self.provider.action = 'read_claim' if self.provider.calls == 0 else 'none'
            return original(**kwargs)
        self.provider.generate = generate
        d = self.draft()
        self.assertEqual(d['status'], 'ready'); self.assertEqual(self.provider.calls, 2)
        self.assertEqual(len(d['provider_trace']), 2)
        self.assertEqual(len(self.service.detail(CABINET, 'chat', CHAT)['messages']), 1)
        self.assertTrue(any(e['event_type'] == 'claim_read_result' for e in self.provider.contexts[-1]['internal_events']))
        self.assertEqual((self.wb.sends, self.wb.decisions), (0, 0))

    def test_read_operation_internal_continuation(self):
        d = self.draft(); op = self.send(d)
        original = self.provider.generate
        step = [0]
        def generate(**kwargs):
            result = original(**kwargs)
            if step[0] == 0:
                result['proposal']['reply'] = ''
                result['proposal']['operation'] = {'kind': 'read_operation', 'claim_id': '', 'operation_id': op['operation_id'], 'basis': 'Read result', 'basis_type': 'unknown'}
            step[0] += 1
            return result
        self.provider.generate = generate
        d2 = self.draft()
        self.assertEqual(d2['status'], 'ready'); self.assertEqual(self.service.operation(CABINET, op['operation_id'])['state'], 'confirmed')
        self.assertEqual(self.wb.sends, 1); self.assertEqual(step[0], 2)

    def test_media_dependent_basis_blocks_but_attachment_does_not_block_text(self):
        append(self.repo, text='Фото', media={'images': [{'downloadID': 'image-1'}]})
        self.provider.action = 'autorefund1'; self.provider.basis_type = 'media_required'; d = self.draft()
        self.assert_code('media_capability_unavailable', lambda: self.decide(d))
        self.assertEqual(self.wb.decisions, 0)
        append(self.repo, text='По тексту ясно: совет уже пробовал, воздушные пузыри остались.')
        self.provider.basis_type = 'text_sufficient'; d2 = self.draft()
        self.assert_code('text_basis_confirmation_required', lambda: self.decide(d2, text_basis_confirmed=False))
        self.decide(d2); self.assertEqual(self.wb.decisions, 1)

    def test_preliminary_text_send_separate_from_claim_and_no_fake_execution(self):
        self.provider.action = 'approve2'
        original = self.provider.generate
        def generate(**kwargs):
            result = original(**kwargs); result['proposal']['reply'] = 'Спасибо, проверю заявку.'
            return result
        self.provider.generate = generate
        d = self.draft(); op = self.send(d)
        self.assertEqual(self.wb.sends, 1); self.assertEqual(self.wb.decisions, 0)
        self.assertEqual(op['kind'], 'chat_send')

    def test_unconfirmed_execution_assertion_blocked(self):
        self.provider.action = 'approve2'
        original = self.provider.generate
        def generate(**kwargs):
            result = original(**kwargs); result['proposal']['reply'] = 'Ваша заявка уже одобрена.'
            return result
        self.provider.generate = generate
        d = self.draft()
        self.assert_code('unconfirmed_execution_assertion', lambda: self.send(d))
        self.assertEqual((self.wb.sends, self.wb.decisions), (0, 0))


class TransportTests(unittest.TestCase):
    def test_execution_guard_respects_negation_and_condition(self):
        self.assertTrue(_unconfirmed_execution_assertion('Ваша заявка уже одобрена.'))
        self.assertFalse(_unconfirmed_execution_assertion('Заявка пока не одобрена.'))
        self.assertFalse(_unconfirmed_execution_assertion('Ваша заявка будет одобрена после проверки.'))
        self.assertTrue(_unconfirmed_execution_assertion('Заявка не одобрена. Мы одобрили другой возврат.'))
    def test_message_multipart_and_claim_json(self):
        with TemporaryDirectory() as directory:
            wb = OfficialPilotWbTransport(Path(directory))
            with patch.object(wb, '_write', return_value={'http_status': 200, 'payload': {}}) as write:
                wb.send_message(reply_sign='signature', text='Привет')
                args = write.call_args.kwargs
                self.assertEqual(args['method'], 'POST'); self.assertIn('multipart/form-data', args['content_type'])
                self.assertIn('name="replySign"', args['body'].decode()); self.assertIn('Привет', args['body'].decode())
                wb.decide_claim(claim_id=CLAIM, action='autorefund1')
                self.assertEqual(write.call_args.kwargs['method'], 'PATCH')
                self.assertEqual(json.loads(write.call_args.kwargs['body']), {'id': CLAIM, 'action': 'autorefund1'})
    def test_provider_payload_real_boundary_but_mocked_socket(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps({'status': 'completed', 'id': 'fake-response', 'usage': {}, 'output': [{'content': [{'type': 'output_text', 'text': json.dumps({'case_id': 'chat:owner-chat', 'reply': 'Привет', 'operation': {'kind': 'none', 'claim_id': '', 'operation_id': '', 'basis': '', 'basis_type': 'unknown'}})}]}]}).encode()
        class Opener:
            calls = []
            def open(self, req, timeout): self.calls.append(req); return Response()
        opener = Opener()
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'fake-test-key'}), patch('packages.adapters.wb_buyer_support_pilot.request.build_opener', return_value=opener):
            provider = ResponsesProposalProvider(model='fake-model')
            result = provider.generate(policy='policy', context={'case_id': 'chat:owner-chat'})
            payload = json.loads(opener.calls[0].data)
            self.assertFalse(payload['store']); self.assertEqual(payload['model'], 'fake-model')
            self.assertTrue(payload['text']['format']['strict']); self.assertNotIn('tools', payload)
            self.assertEqual(result['response_id'], 'fake-response')


if __name__ == '__main__':
    unittest.main()
