"""Durable fixed '+' acknowledgements; acceptance never implies delivery.

The gateway owns OAuth scope checks. This queue retains the exact authorized
chat/source/UI attestation and uses the existing reply journal as its dedup key.
Only a committed queued -> pending CAS permits POST. Pending/unknown (including
legacy rows) can only be reconciled by reads, even after a process crash.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import re
import time
import uuid

from packages.application.webcore_cloud_reads import CloudReadError, _arguments


class MaxAckQueue:
    WORK_SECONDS = 60.0
    RETRY_BASE_SECONDS = 5.0
    MAX_BACKOFF_SECONDS = 300.0
    RECONCILE_SECONDS = 60.0
    HISTORY_PAGES = 3
    HISTORY_PAGE_SIZE = 100

    def __init__(self, reply, bot_id, *, chat_id):
        if type(bot_id) is not int or bot_id <= 0:
            raise ValueError('Explicit MAX bot identity required')
        if type(chat_id) is not int or not -(2**63) <= chat_id < 0 or chat_id not in reply.api.chat_ids:
            raise ValueError('Explicit authorized MAX acknowledgement group required')
        self.chat_id = chat_id
        self.reply = reply
        self.api = reply.api
        self.bot_id = bot_id

    @contextmanager
    def _connect(self):
        with self.reply._connect() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS max_ack_queue (
                chat_id INTEGER NOT NULL, source_id TEXT NOT NULL,
                source_text TEXT, created_at REAL NOT NULL,
                next_attempt_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                lease_token TEXT, lease_until REAL NOT NULL DEFAULT 0,
                history_before_ms INTEGER,
                PRIMARY KEY(chat_id,source_id))''')
            conn.execute('CREATE INDEX IF NOT EXISTS max_ack_queue_due ON max_ack_queue(next_attempt_at,created_at)')
            # Schema creation must not leave a transaction open before BEGIN IMMEDIATE.
            conn.commit()
            yield conn

    @staticmethod
    def _digest(text):
        return hashlib.sha256(text.encode()).hexdigest()

    def _validate(self, args):
        _arguments(args, {'chat_id', 'message_id', 'source_text', 'ui_verification', 'all_operations_posted'})
        chat, mid = args.get('chat_id'), args.get('message_id')
        if type(chat) is not int or chat != self.chat_id:
            raise CloudReadError('MAX group is not authorized', 'group_not_authorized')
        if not isinstance(mid, str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,200}', mid):
            raise CloudReadError('Invalid source message', 'invalid_arguments')
        for key, limit in [('source_text', 16000), ('ui_verification', 1000)]:
            value = args.get(key)
            if not isinstance(value, str) or not value.strip() or len(value) > limit or '\x00' in value:
                raise CloudReadError('Exact source text and UI verification are required', 'invalid_arguments')
        if args.get('all_operations_posted') is not True:
            raise CloudReadError('All source operations must be verified posted', 'invalid_arguments')
        return chat, mid, self._digest(args['source_text'])

    @staticmethod
    def _receipt(row, digest=None, sent_now=False):
        state = row[1] if digest is None or row[0] == digest else 'source_changed'
        return dict(status=state, accepted=state != 'source_changed',
                    delivery_confirmed=state == 'sent', reply_message_id=row[2],
                    sent_now=sent_now and state == 'sent', retry_allowed=False,
                    financial_verification='operator_ui_attestation')

    def call(self, args, deadline=None, cancel_event=None):
        """Enqueue, or return the durable status for exactly the same source."""
        chat, mid, digest = self._validate(args)
        if ((cancel_event and cancel_event.is_set())
                or (deadline is not None and time.monotonic() >= deadline)):
            raise CloudReadError('Cancelled before queue acceptance', 'deadline_exceeded')
        now = time.time()
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('INSERT OR IGNORE INTO replies VALUES (?,?,?,?,?,NULL)',
                         (chat, mid, digest, args['ui_verification'], 'queued'))
            row = conn.execute('SELECT source_digest,state,reply_id FROM replies WHERE chat_id=? AND source_id=?', (chat, mid)).fetchone()
            if row[0] == digest and row[1] in {'queued', 'retry_wait', 'pending', 'unknown'}:
                conn.execute('''INSERT OR IGNORE INTO max_ack_queue
                    (chat_id,source_id,source_text,created_at,next_attempt_at) VALUES (?,?,?,?,?)''',
                             (chat, mid, args['source_text'], now, now))
                # Backfill source text on an adopted legacy record only if its
                # original attestation digest matches. Never replace evidence.
                conn.execute('UPDATE max_ack_queue SET source_text=COALESCE(source_text,?) WHERE chat_id=? AND source_id=?',
                             (args['source_text'], chat, mid))
            return self._receipt(row, digest)

    def _claim(self):
        now, token = time.time(), uuid.uuid4().hex
        chats = [self.chat_id]
        if not chats:
            return None
        marks = ','.join('?' for _ in chats)
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            # Legacy pending is an indeterminate submission, never a queued job.
            conn.execute(f'''INSERT OR IGNORE INTO max_ack_queue
                (chat_id,source_id,source_text,created_at,next_attempt_at)
                SELECT r.chat_id,r.source_id,NULL,?,? FROM replies r
                WHERE r.chat_id IN ({marks}) AND r.state IN ('pending','unknown')
                AND NOT EXISTS (SELECT 1 FROM max_ack_queue q
                    WHERE q.chat_id=r.chat_id AND q.source_id=r.source_id)
                ORDER BY r.rowid LIMIT 100''', (now, now, *chats))
            row = conn.execute(f'''SELECT q.chat_id,q.source_id,q.source_text,
                    r.source_digest,r.state,r.reply_id,q.attempts,q.history_before_ms
                FROM max_ack_queue q JOIN replies r USING(chat_id,source_id)
                WHERE q.chat_id IN ({marks}) AND q.next_attempt_at<=? AND q.lease_until<=?
                    AND r.state IN ('queued','retry_wait','pending','unknown')
                ORDER BY q.next_attempt_at,q.created_at,q.rowid LIMIT 1''', (*chats, now, now)).fetchone()
            if row is None:
                return None
            conn.execute('UPDATE max_ack_queue SET lease_token=?,lease_until=?,attempts=attempts+1 WHERE chat_id=? AND source_id=?',
                         (token, now + self.WORK_SECONDS + 30, row[0], row[1]))
            # A prior worker may have died at any point after its durable claim.
            if row[4] == 'pending':
                conn.execute("UPDATE replies SET state='unknown' WHERE chat_id=? AND source_id=? AND state='pending'", row[:2])
            return (*row, token)

    def _source(self, job, deadline):
        chat, mid, text, digest = job[:4]
        rows = self.api.call('max_message', {'chat_id': chat, 'message_id': mid}, deadline, None)['rows']
        if len(rows) != 1:
            raise CloudReadError('Source missing or changed', 'source_changed')
        row = rows[0]
        chunks = row.get('message_text_chunks')
        if (row.get('chat_id') != chat or row.get('message_id') != mid
                or row.get('message_text_complete') is not True
                or row.get('message_text_redacted') is True
                or row.get('sender', {}).get('is_bot') is not False
                or not isinstance(chunks, list) or not all(isinstance(x, str) for x in chunks)):
            raise CloudReadError('Source missing, changed or incomplete', 'source_changed')
        live = ''.join(chunks)
        if self._digest(live) != digest or (text is not None and live != text):
            raise CloudReadError('Source changed; recheck UI mapping', 'source_changed')
        return row

    def _matches(self, message, chat, mid, source):
        return (isinstance(message, dict)
                and type(message.get('sender', {}).get('user_id')) is int
                and message['sender']['user_id'] == self.bot_id
                and not message.get('body', {}).get('attachments')
                and self.reply._matches(message, chat, mid, source))

    @staticmethod
    def _valid_mid(mid):
        return isinstance(mid, str) and re.fullmatch(r'[A-Za-z0-9._:-]{1,200}', mid) is not None

    def _verified_id(self, reply_id, job, source, deadline):
        if not self._valid_mid(reply_id):
            return False
        messages = self.api._get({'message_ids': reply_id}, deadline)
        return (len(messages) == 1
                and messages[0].get('body', {}).get('mid') == reply_id
                and self._matches(messages[0], job[0], job[1], source))

    def _finish(self, job, state, *, reply_id=None, error=None, cursor=None):
        chat, mid, token = job[0], job[1], job[8]
        delay = (min(self.MAX_BACKOFF_SECONDS, self.RETRY_BASE_SECONDS * 2**min(job[6], 12))
                 if state == 'retry_wait' else self.RECONCILE_SECONDS)
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            owned = conn.execute('SELECT 1 FROM max_ack_queue WHERE chat_id=? AND source_id=? AND lease_token=?', (chat, mid, token)).fetchone()
            if owned:
                conn.execute("UPDATE replies SET state=?,reply_id=COALESCE(?,reply_id) WHERE chat_id=? AND source_id=? AND state!='sent'",
                             (state, reply_id, chat, mid))
                conn.execute('''UPDATE max_ack_queue SET lease_token=NULL,lease_until=0,
                    next_attempt_at=?,history_before_ms=? WHERE chat_id=? AND source_id=? AND lease_token=?''',
                             (time.time()+delay, cursor, chat, mid, token))
                code, status = self.reply._error_code(error) if error else ('', None)
                conn.execute('INSERT OR REPLACE INTO reply_diagnostics VALUES (?,?,?,?,?,?)',
                             (chat, mid, 'verified' if state == 'sent' else state, code, status, time.time()))
            row = conn.execute('SELECT source_digest,state,reply_id FROM replies WHERE chat_id=? AND source_id=?', (chat, mid)).fetchone()
        return self._receipt(row)

    def _reconcile(self, job, source, deadline):
        if job[5] and self._verified_id(job[5], job, source, deadline):
            return self._finish(job, 'sent', reply_id=job[5])
        cursor = job[7]
        for _ in range(self.HISTORY_PAGES):
            if time.monotonic() >= deadline:
                break
            params = {'chat_id': job[0], 'count': self.HISTORY_PAGE_SIZE}
            if cursor is not None:
                params['from'] = cursor
            messages = self.api._get(params, deadline)
            if len(messages) > self.HISTORY_PAGE_SIZE:
                raise CloudReadError('MAX history exceeded page bound', 'source_limit')
            for message in messages:
                if self._matches(message, job[0], job[1], source):
                    reply_id = message.get('body', {}).get('mid')
                    if self._verified_id(reply_id, job, source, deadline):
                        return self._finish(job, 'sent', reply_id=reply_id)
            stamps = [m.get('timestamp') for m in messages]
            valid = stamps and all(type(t) is int and 0 <= t < 2**63 for t in stamps)
            oldest = min(stamps) if valid else None
            source_stamp = source.get('timestamp_ms')
            if (len(messages) < self.HISTORY_PAGE_SIZE or oldest is None
                    or (type(source_stamp) is int and oldest < source_stamp)):
                cursor = None  # Restart from newest next time: absence is not proof.
                break
            if cursor is not None and oldest >= cursor:
                # An inclusive timestamp tie cannot prove complete coverage. Do
                # not skip it or infer absence; a later pass starts from newest.
                cursor = None
                break
            cursor = oldest
        return self._finish(job, 'unknown', cursor=cursor)

    def run_once(self):
        """Process at most one due job within a shared bounded network budget."""
        job = self._claim()
        if job is None:
            return None
        deadline = time.monotonic() + self.WORK_SECONDS
        submitted = job[4] in {'pending', 'unknown'}
        try:
            source = self._source(job, deadline)
            if submitted:
                return self._reconcile(job, source, deadline)
            # Pre-submit reads may safely retry; after this CAS every outcome,
            # including process death before POST, is readback-only forever.
            if deadline - time.monotonic() < 30:
                raise CloudReadError('Insufficient worker submission budget', 'deadline_exceeded')
            # A journal/commit failure during the submission claim is also
            # indeterminate. Never downgrade a possibly committed pending row.
            submitted = True
            with self._connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                claimed = conn.execute('''UPDATE replies SET state='pending'
                    WHERE chat_id=? AND source_id=? AND source_digest=?
                    AND state IN ('queued','retry_wait') AND EXISTS (
                        SELECT 1 FROM max_ack_queue q WHERE q.chat_id=replies.chat_id
                        AND q.source_id=replies.source_id AND q.lease_token=? AND q.lease_until>?)''',
                                       (job[0], job[1], job[3], job[8], time.time())).rowcount
            if not claimed:
                return self._finish(job, 'unknown')
            submitted = True
            result = self.reply._post(job[0], job[1], deadline, None)
            reply_id = result.get('message', {}).get('body', {}).get('mid')
            if not self._valid_mid(reply_id):
                raise CloudReadError('Missing acknowledgement identity')
            with self._connect() as conn:
                conn.execute("UPDATE replies SET reply_id=? WHERE chat_id=? AND source_id=? AND state='pending'", (reply_id, job[0], job[1]))
            if not self._verified_id(reply_id, job, source, deadline):
                raise CloudReadError('Acknowledgement readback mismatch')
            result = self._finish(job, 'sent', reply_id=reply_id)
            result['sent_now'] = result['delivery_confirmed']
            return result
        except Exception as exc:
            # Never persist private exception text or provider bodies.
            changed = isinstance(exc, CloudReadError) and exc.code in {'source_changed', 'message_not_found'}
            state = 'unknown' if submitted else ('source_changed' if changed else 'retry_wait')
            return self._finish(job, state, error=exc, cursor=job[7])
