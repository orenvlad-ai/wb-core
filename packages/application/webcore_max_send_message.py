"""Explicitly granted bot text; durable single-submit journal, no financial access."""
from __future__ import annotations
import hashlib
import re
import time
from packages.application.webcore_cloud_reads import CloudReadError, _arguments, _enabled
from packages.application.webcore_max_posted_reply import MaxPostedReply


class MaxSendMessage(MaxPostedReply):
    def __init__(self, api, path, bot_id, chat_id):
        super().__init__(api, path)
        if type(bot_id) is not int or bot_id <= 0:
            raise ValueError('Explicit MAX bot identity required')
        if type(chat_id) is not int or chat_id not in api.chat_ids:
            raise ValueError('Explicit MAX write group must be readable')
        self.chat_id = chat_id
        self.bot_id = bot_id

    @classmethod
    def from_env(cls, env, api):
        if not _enabled(env, 'WEBCORE_DATA_MCP_MAX_MESSAGE_SEND_ENABLED'):
            return None
        if api is None:
            raise ValueError('MAX text sending requires configured history access')
        return cls(api, env.get('WEBCORE_DATA_MCP_MAX_REPLY_STORE_PATH', ''),
                   int(env.get('WEBCORE_DATA_MCP_MAX_SEND_BOT_ID', '0')),
                   int(env.get('WEBCORE_DATA_MCP_MAX_SEND_CHAT_ID', '0')))

    def _receipt(self, row, digest):
        return {'status': row[1] if row[0] == digest else 'request_conflict',
                'message_id': row[2], 'sent_now': False, 'retry_allowed': False}

    def _verify(self, mid, chat, text, deadline):
        messages = self.api._get({'message_ids': mid}, deadline)
        return (len(messages) == 1
                and messages[0].get('recipient', {}).get('chat_id') == chat
                and messages[0].get('sender', {}).get('user_id') == self.bot_id
                and messages[0].get('sender', {}).get('is_bot') is True
                and messages[0].get('body', {}).get('mid') == mid
                and messages[0].get('body', {}).get('text') == text
                and not messages[0].get('body', {}).get('attachments')
                and not messages[0].get('link'))

    def call(self, args, deadline=None, cancel_event=None):
        _arguments(args, {'chat_id', 'request_id', 'text'})
        chat, key, text = args.get('chat_id'), args.get('request_id'), args.get('text')
        if type(chat) is not int or chat != self.chat_id:
            raise CloudReadError('MAX group is not authorized', 'group_not_authorized')
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,120}', key):
            raise CloudReadError('Stable request_id required', 'invalid_arguments')
        if not isinstance(text, str) or not text.strip() or len(text) > 4000 or '\x00' in text:
            raise CloudReadError('Plain text of 1 to 4000 characters required', 'invalid_arguments')
        digest = hashlib.sha256(text.encode()).hexdigest()
        with self._connect() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS bot_messages (chat_id INTEGER, request_id TEXT, text_digest TEXT, state TEXT, message_id TEXT, error_code TEXT, PRIMARY KEY(chat_id,request_id))')
            row = conn.execute('SELECT text_digest,state,message_id FROM bot_messages WHERE chat_id=? AND request_id=?', (chat,key)).fetchone()
        if row:
            # Reconcile a known message by GET only; a missing receipt never permits POST.
            if row[0] == digest and row[1] in {'pending', 'unknown'} and row[2]:
                try:
                    if self._verify(row[2], chat, text, deadline):
                        with self._connect() as conn:
                            conn.execute("UPDATE bot_messages SET state='sent',error_code=NULL WHERE chat_id=? AND request_id=? AND message_id=?", (chat,key,row[2]))
                        row = (digest, 'sent', row[2])
                except Exception:
                    pass
            return self._receipt(row, digest)
        if ((cancel_event and cancel_event.is_set())
                or (deadline is not None and deadline-time.monotonic() < 30)):
            raise CloudReadError('Insufficient time; nothing submitted', 'deadline_exceeded')
        with self._connect() as conn:
            claimed = conn.execute('INSERT OR IGNORE INTO bot_messages VALUES (?,?,?,?,NULL,NULL)', (chat,key,digest,'pending')).rowcount
            if not claimed:
                row = conn.execute('SELECT text_digest,state,message_id FROM bot_messages WHERE chat_id=? AND request_id=?', (chat,key)).fetchone()
                return self._receipt(row, digest)
        mid = None
        state, error = 'unknown', None
        try:
            result = self._send_payload(chat, {'text': text, 'notify': False}, deadline, cancel_event)
            mid = result.get('message', {}).get('body', {}).get('mid')
            if not isinstance(mid, str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,200}', mid):
                mid = None
                raise ValueError('Missing message identity')
            with self._connect() as conn:
                conn.execute('UPDATE bot_messages SET message_id=? WHERE chat_id=? AND request_id=?', (mid,chat,key))
            if not self._verify(mid, chat, text, deadline):
                raise ValueError('Readback mismatch')
            state = 'sent'
        except Exception as exc:
            error = self._error_code(exc)[0]
        with self._connect() as conn:
            conn.execute("UPDATE bot_messages SET state=?,error_code=? WHERE chat_id=? AND request_id=? AND state!='sent'", (state,error,chat,key))
            state = conn.execute('SELECT state FROM bot_messages WHERE chat_id=? AND request_id=?', (chat,key)).fetchone()[0]
        return {'status': state, 'message_id': mid, 'sent_now': state == 'sent', 'retry_allowed': False}
