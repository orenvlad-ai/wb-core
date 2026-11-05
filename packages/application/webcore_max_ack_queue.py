"""Durable fixed '+' acknowledgements; acceptance never implies delivery.

The gateway owns OAuth scope checks. This queue retains the exact authorized
chat/source/UI attestation and uses the existing reply journal as its dedup key.
Only a committed submission CAS permits POST. An unknown fixed acknowledgement
may be retried only after two complete fresh absence scans and a settling delay;
legacy records without a durable attempt boundary remain readback-only.
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
    RESEND_SETTLE_SECONDS = 300.0
    ABSENCE_CONFIRM_SECONDS = 60.0
    MAX_RESENDS = 2

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
            conn.execute('''CREATE TABLE IF NOT EXISTS max_ack_recovery (
                chat_id INTEGER NOT NULL, source_id TEXT NOT NULL,
                attempt_started_at REAL, resend_count INTEGER NOT NULL DEFAULT 0,
                absence_proved_at REAL, source_boundary INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(chat_id,source_id))''')
            # Migration cannot prove queue.created_at preceded an old POST. Such
            # rows also require coverage back to the live source timestamp.
            # Newly adopted legacy rows have no durable attempt boundary.
            conn.execute('''INSERT OR IGNORE INTO max_ack_recovery
                (chat_id,source_id,attempt_started_at,source_boundary)
                SELECT chat_id,source_id,created_at,1 FROM max_ack_queue''')
            conn.execute('''CREATE TABLE IF NOT EXISTS max_ack_original_failure (
                chat_id INTEGER, source_id TEXT, phase TEXT, error_code TEXT,
                http_status INTEGER, updated_at REAL, PRIMARY KEY(chat_id,source_id))''')
            conn.execute('''INSERT OR IGNORE INTO max_ack_original_failure
                SELECT * FROM reply_diagnostics WHERE length(error_code)>0''')
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
            conn.execute('''INSERT OR IGNORE INTO max_ack_recovery
                (chat_id,source_id,attempt_started_at) VALUES (?,?,NULL)''', (chat, mid))
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
            conn.execute('''INSERT OR IGNORE INTO max_ack_recovery
                (chat_id,source_id,attempt_started_at)
                SELECT chat_id,source_id,NULL FROM max_ack_queue''')
            row = conn.execute(f'''SELECT q.chat_id,q.source_id,q.source_text,
                    r.source_digest,r.state,r.reply_id,q.attempts,q.history_before_ms,
                    a.attempt_started_at,a.resend_count,a.absence_proved_at,a.source_boundary
                FROM max_ack_queue q JOIN replies r USING(chat_id,source_id)
                JOIN max_ack_recovery a USING(chat_id,source_id)
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
            return (*row[:8], token, *row[8:])

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
                diagnostic = (chat, mid, 'verified' if state == 'sent' else state, code, status, time.time())
                if error:
                    conn.execute('INSERT OR IGNORE INTO max_ack_original_failure VALUES (?,?,?,?,?,?)', diagnostic)
                # A successful empty read is not a replacement for the original
                # transport/verification failure that caused unknown delivery.
                verb = 'REPLACE' if error or state == 'sent' else 'IGNORE'
                conn.execute(f'INSERT OR {verb} INTO reply_diagnostics VALUES (?,?,?,?,?,?)', diagnostic)
            row = conn.execute('SELECT source_digest,state,reply_id FROM replies WHERE chat_id=? AND source_id=?', (chat, mid)).fetchone()
        return self._receipt(row)

    def _absence_ready(self, job):
        """Two independent, fresh complete scans; durable lease owns the proof."""
        now = time.time()
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('''SELECT a.attempt_started_at,a.resend_count,a.absence_proved_at
                FROM max_ack_recovery a JOIN max_ack_queue q USING(chat_id,source_id)
                WHERE a.chat_id=? AND a.source_id=? AND q.lease_token=? AND q.lease_until>?''',
                               (job[0], job[1], job[8], now)).fetchone()
            if (not row or row[0] is None or row[1] >= self.MAX_RESENDS
                    or now-row[0] < self.RESEND_SETTLE_SECONDS * 2**row[1]):
                return False
            if row[2] is None:
                conn.execute('UPDATE max_ack_recovery SET absence_proved_at=? WHERE chat_id=? AND source_id=?',
                             (now, job[0], job[1]))
                return False
            return now-row[2] >= self.ABSENCE_CONFIRM_SECONDS

    def _reconcile(self, job, source, deadline):
        if job[5] and self._verified_id(job[5], job, source, deadline):
            return self._finish(job, 'sent', reply_id=job[5])
        cursor = job[7]
        # A resumed older scan can discover delivery, but cannot prove that
        # newer messages have stayed absent since its previous worker run.
        complete = cursor is None and job[9] is not None and not job[5]
        boundary = int(job[9]*1000) if job[9] is not None else None
        if job[12]:
            source_stamp = source.get('timestamp_ms')
            if type(source_stamp) is not int or not 0 <= source_stamp < 2**63:
                complete = False
            elif boundary is not None:
                boundary = min(boundary, source_stamp)
        absent = False
        for _ in range(self.HISTORY_PAGES):
            if time.monotonic() >= deadline:
                break
            params = {'chat_id': job[0], 'count': self.HISTORY_PAGE_SIZE}
            if cursor is not None:
                params['from'] = cursor
            messages = self.api._get(params, deadline)
            if not isinstance(messages, list) or len(messages) > self.HISTORY_PAGE_SIZE:
                raise CloudReadError('MAX history exceeded page bound', 'source_limit')
            valid = all(isinstance(m, dict)
                        and m.get('recipient', {}).get('chat_id') == job[0]
                        and m.get('recipient', {}).get('chat_type') == 'chat'
                        and type(m.get('timestamp')) is int and 0 <= m['timestamp'] < 2**63
                        and self._valid_mid(m.get('body', {}).get('mid'))
                        and isinstance(m.get('body', {}).get('text'), str)
                        and type(m.get('sender', {}).get('user_id')) is int
                        and type(m.get('sender', {}).get('is_bot')) is bool
                        for m in messages)
            if not valid:
                complete = False
            for message in messages:
                # An own-bot '+' with an incomplete reply link may be the
                # original delivery. Its absence cannot be positively proven.
                if (isinstance(message, dict)
                        and message.get('sender', {}).get('user_id') == self.bot_id
                        and message.get('body', {}).get('text') == '+'):
                    link = message.get('link') or {}
                    linked_id = (link.get('message') or {}).get('mid')
                    if (link.get('type') != 'reply' or not self._valid_mid(linked_id)
                            or linked_id == job[1]
                            or (linked_id != job[1] and len(linked_id) == len(job[1]) == 36
                                and linked_id[-16:] == job[1][-16:])):
                        complete = False
                if self._matches(message, job[0], job[1], source):
                    reply_id = message.get('body', {}).get('mid')
                    if self._verified_id(reply_id, job, source, deadline):
                        return self._finish(job, 'sent', reply_id=reply_id)
                    complete = False  # A plausible delivery must not be retried.
            stamps = [m['timestamp'] for m in messages] if valid else []
            if stamps != sorted(stamps, reverse=True) or (cursor is not None and any(t > cursor for t in stamps)):
                complete = False
            oldest = min(stamps) if stamps else None
            if cursor is not None and oldest is not None and oldest >= cursor:
                cursor = None  # Inclusive boundary is saturated/ambiguous.
                break
            if valid and oldest is not None and boundary is not None and oldest < boundary:
                absent = complete
                cursor = None
                break
            if len(messages) < self.HISTORY_PAGE_SIZE:
                cursor = None  # Short/empty is not proof of the older interval.
                break
            if oldest is None:
                cursor = None
                break
            cursor = oldest
        if absent and self._absence_ready(job):
            return True
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
            recovering = submitted
            if submitted:
                reconciled = self._reconcile(job, source, deadline)
                if reconciled is not True:
                    return reconciled
            # A recovery submission requires a fresh complete absence proof and
            # consumes its bounded retry before any POST, including on crashes.
            if deadline - time.monotonic() < 30:
                raise CloudReadError('Insufficient worker submission budget', 'deadline_exceeded')
            # A journal/commit failure during the submission claim is also
            # indeterminate. Never downgrade a possibly committed pending row.
            submitted = True
            with self._connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                now = time.time()
                eligible = ('unknown', 'pending') if recovering else ('queued', 'retry_wait')
                claimed = conn.execute('''UPDATE replies SET state='pending'
                    WHERE chat_id=? AND source_id=? AND source_digest=?
                    AND state IN (?,?) AND (?=0 OR reply_id IS NULL) AND EXISTS (
                        SELECT 1 FROM max_ack_queue q WHERE q.chat_id=replies.chat_id
                        AND q.source_id=replies.source_id AND q.lease_token=? AND q.lease_until>?)
                    AND EXISTS (SELECT 1 FROM max_ack_recovery a
                        WHERE a.chat_id=replies.chat_id AND a.source_id=replies.source_id
                        AND (?=0 OR (a.resend_count<? AND a.attempt_started_at IS NOT NULL
                            AND ?-a.attempt_started_at>=? * (1 << a.resend_count)
                            AND a.absence_proved_at IS NOT NULL AND ?-a.absence_proved_at>=?)))''',
                                       (job[0], job[1], job[3], *eligible, int(recovering), job[8], now,
                                        int(recovering), self.MAX_RESENDS, now, self.RESEND_SETTLE_SECONDS,
                                        now, self.ABSENCE_CONFIRM_SECONDS)).rowcount
                if claimed:
                    conn.execute('''UPDATE max_ack_recovery SET attempt_started_at=?,
                        resend_count=resend_count+?,absence_proved_at=NULL,source_boundary=0 WHERE chat_id=? AND source_id=?''',
                                 (now, int(recovering), job[0], job[1]))
                    conn.execute('UPDATE max_ack_queue SET history_before_ms=NULL WHERE chat_id=? AND source_id=?', job[:2])
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
