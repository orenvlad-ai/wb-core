"""Fixed MAX acknowledgement; UI evidence is supplied by the authorized operator.

No cash database access. Durable claim before network submission deliberately
prefers a missing acknowledgement over duplicate sends after an unknown result.
"""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import ssl
import stat
import time
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPSHandler

from packages.application.webcore_cloud_reads import CloudReadError, _arguments, _enabled, _fixed_path
from packages.application.webcore_cloud_extended_reads import _NoRedirect


class MaxPostedReply:
    def __init__(self, api, path):
        self.api = api
        self.path = _fixed_path(Path(path))
        if self.path.name != 'max_posted_replies.sqlite3':
            raise ValueError('Dedicated MAX reply journal required')

    @classmethod
    def from_env(cls, env, api):
        if not _enabled(env, 'WEBCORE_DATA_MCP_MAX_POSTED_REPLY_ENABLED'):
            return None
        if api is None:
            raise ValueError('MAX reply requires configured MAX history access')
        return cls(api, env.get('WEBCORE_DATA_MCP_MAX_REPLY_STORE_PATH', ''))

    @contextmanager
    def _connect(self):
        parent = self.path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _fixed_path(parent)
        if parent.stat().st_mode & 0o077:
            raise CloudReadError('Reply journal directory must be private')
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        _fixed_path(self.path)
        info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise CloudReadError('Reply journal must be private')
        conn = sqlite3.connect(self.path, timeout=2)
        conn.execute('CREATE TABLE IF NOT EXISTS replies (chat_id INTEGER, source_id TEXT, source_digest TEXT, evidence TEXT, state TEXT, reply_id TEXT, PRIMARY KEY(chat_id,source_id))')
        conn.execute('CREATE TABLE IF NOT EXISTS reply_diagnostics (chat_id INTEGER, source_id TEXT, phase TEXT, error_code TEXT, http_status INTEGER, updated_at REAL, PRIMARY KEY(chat_id,source_id))')
        conn.commit()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _record(self, chat, mid, phase, error_code='', http_status=None):
        # Persist only controlled metadata: never exception text, tokens or response bodies.
        with self._connect() as conn:
            conn.execute('INSERT OR REPLACE INTO reply_diagnostics VALUES (?,?,?,?,?,?)',
                         (chat, mid, phase, error_code, http_status, time.time()))

    @staticmethod
    def _error_code(exc):
        for _ in range(3):
            if isinstance(exc, CloudReadError) and exc.__cause__ is not None:
                exc = exc.__cause__
            else:
                break
        if isinstance(exc, HTTPError):
            return 'upstream_http_error', exc.code
        cause = exc.reason if isinstance(exc, URLError) else exc
        if isinstance(cause, TimeoutError):
            return 'upstream_timeout', None
        if isinstance(cause, ssl.SSLError):
            return 'upstream_tls_error', None
        if isinstance(exc, URLError):
            return 'upstream_transport_error', None
        return 'reply_verification_failed', None

    def _post(self, chat, mid, deadline, cancel_event=None):
        return self._send_payload(chat, {'text': '+', 'link': {'type': 'reply', 'mid': mid}, 'notify': False}, deadline, cancel_event)

    def _send_payload(self, chat, payload, deadline, cancel_event=None):
        info = self.api.token_path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 8192:
            raise CloudReadError('MAX credential unavailable')
        token = self.api.token_path.read_text().strip()
        if not token or '\n' in token or '\r' in token:
            raise CloudReadError('MAX credential unavailable')
        context = ssl.create_default_context()
        if self.api.ca_path:
            _fixed_path(self.api.ca_path)
            context.load_verify_locations(cafile=str(self.api.ca_path))
        req = Request('https://platform-api2.max.ru/messages?' + urlencode({'chat_id': chat}),
                      data=json.dumps(payload).encode(),
                      headers={'Authorization': token, 'Content-Type': 'application/json'}, method='POST')
        timeout = min(20.0, max(0.1, deadline-time.monotonic()-10.0)) if deadline else 20.0
        if (cancel_event and cancel_event.is_set()) or (deadline is not None and time.monotonic() >= deadline):
            raise CloudReadError('Cancelled before network submission', 'deadline_exceeded')
        with build_opener(_NoRedirect(), HTTPSHandler(context=context)).open(req, timeout=timeout) as response:
            raw = response.read(1024*1024+1)
        if len(raw) > 1024*1024:
            raise CloudReadError('MAX reply response too large')
        return json.loads(raw)

    @staticmethod
    def _matches(message, chat, source, source_row=None):
        if not (isinstance(message, dict)
                and message.get('recipient', {}).get('chat_id') == chat
                and message.get('sender', {}).get('is_bot') is True
                and message.get('body', {}).get('text') == '+'
                and message.get('link', {}).get('type') == 'reply'):
            return False
        link = message['link']
        body = link.get('message', {})
        if body.get('mid') == source:
            return True
        # MAX reply links can encode the positive group magnitude whereas GET
        # returns its signed group ID. Do not compare suffixes alone: corroborate
        # both complete IDs with the live source's group, seq, sender and text.
        if not source_row or source_row.get('message_id') != source or source_row.get('chat_id') != chat:
            return False
        seq = source_row.get('sequence')
        sender = source_row.get('sender', {}).get('user_id')
        return (type(seq) is int and 0 <= seq < 2**64
                and type(sender) is int
                and source == f'mid.{chat & ((1 << 64)-1):016x}{seq:016x}'
                and body.get('mid') == f'mid.{abs(chat):016x}{seq:016x}'
                and link.get('chat_id') == abs(chat)
                and body.get('seq') == seq
                and link.get('sender', {}).get('user_id') == sender
                and source_row.get('message_text_complete') is True
                and body.get('text') == ''.join(source_row.get('message_text_chunks', [])))

    def call(self, args, deadline=None, cancel_event=None):
        _arguments(args, {'chat_id', 'message_id', 'source_text', 'ui_verification', 'all_operations_posted'})
        chat, mid = args.get('chat_id'), args.get('message_id')
        if type(chat) is not int or chat not in self.api.chat_ids:
            raise CloudReadError('MAX group is not authorized', 'group_not_authorized')
        if not isinstance(mid, str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,200}', mid):
            raise CloudReadError('Invalid source message', 'invalid_arguments')
        for key, limit in [('source_text', 16000), ('ui_verification', 1000)]:
            if not isinstance(args.get(key), str) or not 1 <= len(args[key].strip()) <= limit:
                raise CloudReadError('Source text and UI verification are required', 'invalid_arguments')
        if args.get('all_operations_posted') is not True:
            raise CloudReadError('All source operations must be verified posted', 'invalid_arguments')
        digest = hashlib.sha256(args['source_text'].encode()).hexdigest()
        with self._connect() as conn:
            existing = conn.execute('SELECT source_digest,state,reply_id FROM replies WHERE chat_id=? AND source_id=?', (chat,mid)).fetchone()
        if existing:
            state = existing[1] if existing[0] == digest else 'source_changed'
            # A repeated call may verify a known reply, but must never repeat POST.
            if state in {'unknown', 'pending'} and existing[2]:
                try:
                    messages = self.api._get({'message_ids': existing[2]}, deadline)
                    source_rows = self.api.call('max_message', {'chat_id': chat, 'message_id': mid}, deadline, cancel_event)['rows']
                    source_row = source_rows[0] if len(source_rows) == 1 else None
                    if (not source_row or not source_row.get('message_text_complete')
                            or source_row.get('sender', {}).get('is_bot')
                            or ''.join(source_row.get('message_text_chunks', [])) != args['source_text']):
                        state = 'source_changed'
                    elif (len(messages) == 1 and messages[0].get('body', {}).get('mid') == existing[2]
                            and self._matches(messages[0], chat, mid, source_row)):
                        with self._connect() as conn:
                            conn.execute("UPDATE replies SET state='sent' WHERE chat_id=? AND source_id=? AND state IN ('unknown','pending') AND reply_id=?",
                                         (chat, mid, existing[2]))
                        state = 'sent'
                        self._record(chat, mid, 'verified')
                except Exception as exc:
                    self._record(chat, mid, 'readback', *self._error_code(exc))
            return {'status': state, 'reply_message_id': existing[2], 'sent_now': False,
                    'retry_allowed': False, 'financial_verification': 'operator_ui_attestation'}
        source = self.api.call('max_message', {'chat_id': chat, 'message_id': mid}, deadline, cancel_event)['rows']
        if (len(source) != 1 or not source[0].get('message_text_complete')
                or source[0].get('sender', {}).get('is_bot')
                or ''.join(source[0].get('message_text_chunks', [])) != args['source_text']):
            raise CloudReadError('Source missing, changed or incomplete; recheck UI mapping', 'source_changed')
        if (cancel_event and cancel_event.is_set()) or (deadline is not None and time.monotonic() >= deadline):
            raise CloudReadError('Cancelled before submission', 'deadline_exceeded')
        if deadline is not None and deadline - time.monotonic() < 30.0:
            raise CloudReadError('Insufficient time for send and readback; nothing submitted', 'deadline_exceeded')
        with self._connect() as conn:
            claimed = conn.execute('INSERT OR IGNORE INTO replies VALUES (?,?,?,?,?,NULL)',
                                   (chat,mid,digest,args['ui_verification'],'pending')).rowcount
        if not claimed:
            return {'status': 'pending', 'sent_now': False, 'retry_allowed': False}
        # No exception after this durable claim may cause an automatic resend.
        reply_id = None
        phase = 'submit'
        try:
            self._record(chat, mid, phase)
            result = self._post(chat, mid, deadline, cancel_event)
            message = result.get('message', {})
            reply_id = message.get('body', {}).get('mid')
            if not isinstance(reply_id, str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,200}', reply_id):
                raise ValueError('Missing reply identity')
            with self._connect() as conn:
                conn.execute('UPDATE replies SET reply_id=? WHERE chat_id=? AND source_id=?', (reply_id,chat,mid))
            phase = 'readback'
            self._record(chat, mid, phase)
            messages = self.api._get({'message_ids': reply_id}, deadline)
            if len(messages) != 1 or messages[0].get('body', {}).get('mid') != reply_id or not self._matches(messages[0],chat,mid,source[0]):
                raise ValueError('Reply readback not verified')
            state = 'sent'
            self._record(chat, mid, 'verified')
        except Exception as exc:
            state = 'unknown'
            self._record(chat, mid, phase, *self._error_code(exc))
        with self._connect() as conn:
            conn.execute('UPDATE replies SET state=? WHERE chat_id=? AND source_id=?', (state,chat,mid))
        return {'status': state, 'reply_message_id': reply_id, 'sent_now': state == 'sent',
                'retry_allowed': False, 'financial_verification': 'operator_ui_attestation'}
