"""Explicit pilot transports. No network during construction; no automatic retries."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any
from urllib import error, parse, request
from uuid import uuid4

from packages.adapters.wb_buyer_support import BuyerSupportApiError, _NoRedirect


class PilotTransportError(RuntimeError):
    def __init__(self, code: str, http_status: int | None = None):
        self.code, self.http_status = code, http_status
        super().__init__(code)


PROPOSAL_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['case_id', 'reply', 'operation'],
    'properties': {
        'case_id': {'type': 'string'}, 'reply': {'type': 'string'},
        'operation': {'type': 'object', 'additionalProperties': False,
            'required': ['kind', 'claim_id', 'operation_id', 'basis', 'basis_type'],
            'properties': {
                'kind': {'type': 'string', 'enum': ['none', 'read_claim', 'read_operation', 'autorefund1', 'approve2']},
                'claim_id': {'type': 'string'}, 'operation_id': {'type': 'string'}, 'basis': {'type': 'string'},
                'basis_type': {'type': 'string', 'enum': ['text_sufficient', 'media_required', 'unknown']}}}}}


class ResponsesProposalProvider:
    """Provider-only boundary; carries no WB capabilities or review-specific routing."""
    def __init__(self, *, model: str | None = None, reasoning: str | None = None,
                 timeout_seconds: float = 30, api_key_env_var: str = 'OPENAI_API_KEY'):
        self.model = model or os.environ.get('WB_BUYER_SUPPORT_MODEL', 'gpt-5.6-terra')
        self.reasoning = reasoning or os.environ.get('WB_BUYER_SUPPORT_REASONING', 'medium')
        self.timeout_seconds = min(45, max(1, timeout_seconds))
        self.api_key_env_var = api_key_env_var

    @property
    def configured(self) -> bool:
        return bool(os.environ.get(self.api_key_env_var, '').strip())

    @property
    def profile(self) -> dict:
        return {'model': self.model, 'reasoning': self.reasoning, 'store': False}

    def generate(self, *, policy: str, context: dict) -> dict:
        key = os.environ.get(self.api_key_env_var, '').strip()
        if not key:
            raise PilotTransportError('provider_not_configured')
        body = {'model': self.model, 'reasoning': {'effort': self.reasoning}, 'store': False,
                'instructions': policy, 'input': json.dumps(context, ensure_ascii=False),
                'max_output_tokens': 3500, 'safety_identifier': 'wbc_buyer_' + hashlib.sha256(context['case_id'].encode()).hexdigest()[:32],
                'text': {'format': {'type': 'json_schema', 'name': 'wbc_buyer_proposal_v5',
                                    'strict': True, 'schema': PROPOSAL_SCHEMA}}}
        req = request.Request('https://api.openai.com/v1/responses', method='POST',
                headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'},
                data=json.dumps(body, ensure_ascii=False).encode())
        try:
            with request.build_opener(_NoRedirect()).open(req, timeout=self.timeout_seconds) as response:
                result = json.loads(response.read().decode())
        except error.HTTPError as exc:
            raise PilotTransportError('provider_http_error', exc.code) from None
        except (error.URLError, OSError, TimeoutError):
            raise PilotTransportError('provider_result_unknown') from None
        except (ValueError, UnicodeError, BuyerSupportApiError):
            raise PilotTransportError('provider_invalid_response') from None
        if not isinstance(result, dict) or result.get('status') != 'completed' or result.get('error'):
            raise PilotTransportError('provider_incomplete_response')
        texts = [c.get('text', '') for item in result.get('output', []) if isinstance(item, dict)
                 for c in item.get('content', []) if isinstance(c, dict) and c.get('type') == 'output_text']
        try:
            proposal = json.loads(''.join(texts))
        except (ValueError, TypeError):
            raise PilotTransportError('provider_invalid_proposal') from None
        return {'proposal': proposal, 'response_id': result.get('id'), 'usage': result.get('usage'),
                'model_profile': self.profile}


class OfficialPilotWbTransport:
    """Fixed Russian hosts, source ingestion, one write per invocation, readback only."""
    def __init__(self, runtime_dir: Path, *, token_env_var: str = 'WB_API_TOKEN', timeout_seconds: float = 20):
        self.runtime_dir = Path(runtime_dir)
        self.token_env_var = token_env_var
        self.timeout_seconds = min(30, max(1, timeout_seconds))

    @property
    def configured(self) -> bool:
        return bool(os.environ.get(self.token_env_var, '').strip())

    def _rate(self, category: str, interval: float) -> None:
        # Cross-process pacing shared by this pilot. No request retry is implied.
        folder = self.runtime_dir / 'buyer-support'
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = folder / ('pilot-rate-' + category + '.json')
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, 'r+') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                last = json.load(stream).get('last', 0)
            except ValueError:
                last = 0
            delay = last + interval - time.time()
            if delay > 0:
                time.sleep(min(delay, interval))
            stream.seek(0); stream.truncate()
            json.dump({'last': time.time()}, stream); stream.flush(); os.fsync(stream.fileno())

    def _get(self, host: str, path: str, params: dict, category: str) -> dict:
        self._rate(category, 3.1 if category == 'claims' else 1.1)
        token = os.environ.get(self.token_env_var, '').strip()
        if not token: raise PilotTransportError('wb_token_not_configured')
        req = request.Request(host + path + ('?' + parse.urlencode(params) if params else ''),
                              method='GET', headers={'Authorization': token, 'Accept': 'application/json'})
        try:
            with request.build_opener(_NoRedirect()).open(req, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode())
        except error.HTTPError as exc:
            raise PilotTransportError('upstream_http_error', exc.code) from None
        except (error.URLError, TimeoutError, OSError):
            raise PilotTransportError('upstream_transport_error') from None
        except (ValueError, UnicodeError, BuyerSupportApiError):
            raise PilotTransportError('upstream_invalid_response') from None
        if not isinstance(payload, dict) or payload.get('error') or payload.get('errors'):
            raise PilotTransportError('upstream_error_payload')
        return payload

    def refresh(self, repository: Any, cabinet: str, *, kind: str, item_id: str) -> dict:
        """Full bounded current observation scan, sharing the original sync lock/CAS."""
        folder = repository.path.parent
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(folder / 'sync.lock', 'a+') as lock:
            os.chmod(folder / 'sync.lock', 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PilotTransportError('source_sync_busy') from None
            deadline = time.monotonic() + 65
            chats = self._get('https://buyer-chat-api.wildberries.ru', '/api/v1/seller/chats', {}, 'chat')
            repository.save_chats(cabinet, chats)
            signatures = {c.get('chatID'): c.get('replySign') for c in chats.get('result', [])}
            for _ in range(20):
                if time.monotonic() > deadline:
                    raise PilotTransportError('source_refresh_incomplete')
                cursor = repository.cursor(cabinet)
                page = self._get('https://buyer-chat-api.wildberries.ru', '/api/v1/seller/events',
                                 {} if cursor is None else {'next': cursor}, 'chat')
                _, total = repository.save_event_page(cabinet, page, expected_cursor=cursor)
                if total == 0:
                    break
            else:
                raise PilotTransportError('source_refresh_incomplete')
            observed_claims = []
            for archive in (False, True):
                seen, offset = set(), 0
                for _ in range(10):
                    if time.monotonic() > deadline:
                        raise PilotTransportError('source_refresh_incomplete')
                    page = self._get('https://returns-api.wildberries.ru', '/api/v1/claims',
                            {'is_archive': str(archive).lower(), 'limit': 200, 'offset': offset}, 'claims')
                    ids = [c.get('id') for c in page.get('claims', [])]
                    if len(ids) != len(set(ids)) or seen.intersection(ids):
                        raise PilotTransportError('source_claim_pagination_conflict')
                    count, total = repository.save_claim_page(cabinet, page, archive=archive)
                    observed_claims.extend(ids); seen.update(ids); offset += count
                    if offset >= total:
                        repository.state(cabinet, 'claims_archive' if archive else 'claims_active', 'complete')
                        break
                    if not count:
                        raise PilotTransportError('source_refresh_incomplete')
                else:
                    raise PilotTransportError('source_refresh_incomplete')
            return {'reply_sign': signatures.get(item_id) if kind == 'chat' else None,
                    'observed_claim_ids': observed_claims, 'complete': True}

    def read_claim(self, repository: Any, cabinet: str, claim_id: str) -> dict | None:
        found = []
        for archive in (False, True):
            page = self._get('https://returns-api.wildberries.ru', '/api/v1/claims',
                    {'is_archive': str(archive).lower(), 'id': claim_id, 'limit': 200, 'offset': 0}, 'claims')
            rows = page.get('claims', [])
            if any(c.get('id') != claim_id for c in rows) or len(rows) > 1:
                raise PilotTransportError('claim_read_mismatched_id')
            repository.save_claim_page(cabinet, page, archive=archive)
            found.extend(dict(c, archive=archive) for c in rows)
        if len(found) > 1:
            raise PilotTransportError('claim_read_conflict')
        return found[0] if found else None

    def _write(self, *, host: str, path: str, method: str, body: bytes, content_type: str, category: str) -> dict:
        token = os.environ.get(self.token_env_var, '').strip()
        if not token:
            raise PilotTransportError('wb_token_not_configured')
        self._rate(category, 3.1 if category == 'claims' else 1.1)
        req = request.Request(host + path, method=method,
                headers={'Authorization': token, 'Content-Type': content_type}, data=body)
        try:
            with request.build_opener(_NoRedirect()).open(req, timeout=self.timeout_seconds) as response:
                raw = response.read()
                payload = json.loads(raw.decode()) if raw else {}
                if not isinstance(payload, dict) or payload.get('error') or payload.get('errors'):
                    raise PilotTransportError('wb_write_result_unknown', response.status)
                return {'http_status': response.status, 'payload': payload}
        except error.HTTPError as exc:
            raise PilotTransportError('wb_write_http_error', exc.code) from None
        except (error.URLError, TimeoutError, OSError, ValueError, UnicodeError, BuyerSupportApiError):
            raise PilotTransportError('wb_write_result_unknown') from None

    def send_message(self, *, reply_sign: str, text: str) -> dict:
        if not isinstance(reply_sign, str) or not 1 <= len(reply_sign) <= 255 or not 1 <= len(text) <= 1000:
            raise PilotTransportError('invalid_chat_write')
        boundary = 'wbc-' + uuid4().hex
        parts = []
        for key, value in (('replySign', reply_sign), ('message', text)):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n')
        body = (''.join(parts) + f'--{boundary}--\r\n').encode()
        return self._write(host='https://buyer-chat-api.wildberries.ru', path='/api/v1/seller/message',
                method='POST', body=body, content_type='multipart/form-data; boundary=' + boundary, category='chat')

    def decide_claim(self, *, claim_id: str, action: str) -> dict:
        return self._write(host='https://returns-api.wildberries.ru', path='/api/v1/claim', method='PATCH',
                body=json.dumps({'id': claim_id, 'action': action}).encode(),
                content_type='application/json', category='claims')
