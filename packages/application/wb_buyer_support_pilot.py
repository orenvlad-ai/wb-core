"""Durable, explicitly confirmed buyer pilot; policy proposes, this service executes.

No automatic dispatch and no network at construction. Exactly one local write
attempt per semantic action; unknown outcomes only reconcile. SQLite transactions
never enclose model/WB calls. Source observation remains independently durable.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any
from uuid import uuid4

from packages.application.wb_buyer_support import BuyerSupportRepository, _utc
from packages.application.business_data_procedure_admission import admitted_write
from packages.adapters.wb_buyer_support_pilot import (
    OfficialPilotWbTransport, ResponsesProposalProvider, PilotTransportError)


_REQUEST_RESERVATION: ContextVar[dict | None] = ContextVar('buyer_pilot_request_reservation', default=None)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 512:
        raise BuyerSupportPilotError('invalid_identifier', 422)
    return value


def _unconfirmed_execution_assertion(text: str) -> bool:
    """Narrow assertion guard; does not classify the buyer's complaint or return grounds."""
    pattern = r'(?:заявк[а-я]*|возврат|мы)[^.!?]{0,80}\b(?P<result>одобрен[а-я]*|одобрили|выполнен[а-я]*|оформили)\b'
    for match in re.finditer(pattern, text, re.IGNORECASE):
        prefix = text[match.start():match.start('result')].lower()
        if re.search(r'\bне(?:\s+(?:была|был|были|было))?\s*$', prefix):
            continue
        if re.search(r'\b(?:будет|будут|если|когда)\b', prefix):
            continue
        return True
    return False


class BuyerSupportPilotError(RuntimeError):
    def __init__(self, code: str, http_status: int = 409):
        self.code, self.http_status = code, http_status
        super().__init__(code)


def _record_safe_failure(method):
    """Terminal recovery receipt only when no write/provider attempt was committed."""
    @wraps(method)
    def call(self, cabinet, *args, **kwargs):
        request_id = kwargs.get('request_id')
        owned = {'cabinet': cabinet, 'request_id': request_id, 'store_path': str(self.store.path), 'reserved': False}
        token = _REQUEST_RESERVATION.set(owned)
        try:
            return method(self, cabinet, *args, **kwargs)
        except BuyerSupportPilotError as exc:
            if request_id and owned['reserved'] and self.store.path.exists():
                with self.store.db() as db:
                    row = db.execute('SELECT kind,result FROM requests WHERE cabinet=? AND id=?', (cabinet, request_id)).fetchone()
                    paid = db.execute("SELECT 1 FROM internal_events WHERE cabinet=? AND type IN ('generation_started','provider_call_started') AND data LIKE ? LIMIT 1", (cabinet, '%"request_id":' + _json(request_id) + '%')).fetchone()
                if row and row['result'] is None and not paid:
                    receipt = {'kind': row['kind'], 'state': 'failed', 'write_attempted': False,
                               'error': exc.code, 'request_id': request_id}
                    with self.store.db(write=True) as db:
                        db.execute('UPDATE requests SET state=?,result=? WHERE cabinet=? AND id=? AND result IS NULL',
                                   ('completed', _json(receipt), cabinet, request_id))
            raise
        finally:
            _REQUEST_RESERVATION.reset(token)
    return call


@dataclass(frozen=True)
class PilotConfig:
    enabled: bool = False
    cabinet: str = ''
    chat_ids: frozenset[str] = frozenset()
    claim_ids: frozenset[str] = frozenset()
    token_rate_class: str = ''

    @classmethod
    def from_env(cls) -> 'PilotConfig':
        return cls(os.environ.get('WB_BUYER_SUPPORT_PILOT_ENABLED', '').lower() == 'true',
                   os.environ.get('WB_BUYER_SUPPORT_CABINET_ID', '').strip(),
                   frozenset(s.strip() for s in os.environ.get('WB_BUYER_SUPPORT_PILOT_CHAT_IDS', '').split(',') if s.strip()),
                   frozenset(s.strip() for s in os.environ.get('WB_BUYER_SUPPORT_PILOT_CLAIM_IDS', '').split(',') if s.strip()),
                   os.environ.get('WB_BUYER_SUPPORT_TOKEN_RATE_CLASS', '').strip())


class PilotStore:
    def __init__(self, runtime_dir: Path):
        self.path = Path(runtime_dir).resolve() / 'buyer-support' / 'pilot.sqlite3'

    @contextmanager
    def db(self, *, write: bool = False):
        if not write and not self.path.exists():
            yield None
            return
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.path.parent, 0o700)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600); os.close(fd)
            os.chmod(self.path, 0o600)
        conn = sqlite3.connect(str(self.path) if write else self.path.as_uri() + '?mode=ro', uri=not write, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            if write:
                conn.executescript('''
                CREATE TABLE IF NOT EXISTS requests(cabinet TEXT, id TEXT, kind TEXT, payload_hash TEXT,
                  state TEXT, result TEXT, created_at TEXT, PRIMARY KEY(cabinet,id));
                CREATE TABLE IF NOT EXISTS drafts(cabinet TEXT, id TEXT, kind TEXT, item_id TEXT,
                  semantic_key TEXT, data TEXT, status TEXT, PRIMARY KEY(cabinet,id), UNIQUE(cabinet,semantic_key));
                CREATE TABLE IF NOT EXISTS operations(cabinet TEXT, id TEXT, kind TEXT, item_id TEXT,
                  semantic_key TEXT, data TEXT, PRIMARY KEY(cabinet,id), UNIQUE(cabinet,semantic_key));
                CREATE TABLE IF NOT EXISTS internal_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                  cabinet TEXT, kind TEXT, item_id TEXT, type TEXT, data TEXT, created_at TEXT);
                CREATE TABLE IF NOT EXISTS source_snapshots(cabinet TEXT, kind TEXT, item_id TEXT,
                  version TEXT, data TEXT, created_at TEXT, PRIMARY KEY(cabinet,kind,item_id,version));
                ''')
                conn.execute('BEGIN IMMEDIATE')
            else:
                conn.execute('PRAGMA query_only=ON'); conn.execute('BEGIN')
            yield conn
            if write:
                conn.commit()
        except BaseException:
            conn.rollback(); raise
        finally:
            conn.close()

    @contextmanager
    def lock(self, cabinet: str, kind: str, item_id: str):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.path.parent / ('pilot-lock-' + _hash([cabinet, kind, item_id]))
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, 'w') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BuyerSupportPilotError('conversation_busy') from None
            yield

    def event(self, db, cabinet: str, kind: str, item_id: str, type_: str, data: dict):
        db.execute('INSERT INTO internal_events(cabinet,kind,item_id,type,data,created_at) VALUES(?,?,?,?,?,?)',
                   (cabinet, kind, item_id, type_, _json(data), _utc()))

    def reserve(self, cabinet: str, request_id: str, kind: str, payload: dict) -> dict | None:
        _identifier(request_id)
        with self.db(write=True) as db:
            row = db.execute('SELECT * FROM requests WHERE cabinet=? AND id=?', (cabinet, request_id)).fetchone()
            if row:
                if row['kind'] != kind or row['payload_hash'] != _hash(payload):
                    raise BuyerSupportPilotError('request_id_conflict')
                if row['result']:
                    return json.loads(row['result'])
                raise BuyerSupportPilotError('request_result_unknown')
            db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?)',
                       (cabinet, request_id, kind, _hash(payload), 'started', None, _utc()))
        owned = _REQUEST_RESERVATION.get()
        if owned and owned['cabinet'] == cabinet and owned['request_id'] == request_id and owned['store_path'] == str(self.path):
            owned['reserved'] = True
        return None

    def finish(self, db, cabinet: str, request_id: str, result: dict):
        db.execute('UPDATE requests SET state=?,result=? WHERE cabinet=? AND id=?',
                   ('completed', _json(result), cabinet, request_id))


class BuyerSupportPilotService:
    def __init__(self, runtime_dir: Path, observation_repository=None, provider=None, wb=None,
                 config: PilotConfig | None = None, verification=None):
        self.runtime_dir = Path(runtime_dir).resolve()
        self.repository = observation_repository or BuyerSupportRepository(self.runtime_dir)
        self.store = PilotStore(self.runtime_dir)
        self.provider = provider or ResponsesProposalProvider()
        self.wb = wb or OfficialPilotWbTransport(self.runtime_dir)
        self._config = config
        # Optional trusted boundary, receives snapshot; no model/buyer supplied verification.
        self.verification = verification
        self.policy_path = Path(__file__).resolve().parents[1] / 'contracts' / 'wb_buyer_support_policy_v5.md'

    @property
    def config(self) -> PilotConfig:
        return self._config or PilotConfig.from_env()

    def _allowed(self, cabinet: str, kind: str, item_id: str) -> bool:
        cfg = self.config
        return bool(cabinet and cabinet == cfg.cabinet and item_id in (cfg.chat_ids if kind == 'chat' else cfg.claim_ids))

    def _gate(self, cabinet: str, kind: str, item_id: str, *, provider: bool = False, recovery: bool = False):
        _identifier(cabinet); _identifier(item_id)
        if kind not in ('chat', 'claim'): raise BuyerSupportPilotError('invalid_item_kind', 422)
        if not self.config.enabled and not recovery:
            raise BuyerSupportPilotError('pilot_disabled', 403)
        if not self._allowed(cabinet, kind, item_id):
            raise BuyerSupportPilotError('pilot_target_not_allowlisted', 403)
        if self.config.token_rate_class != 'personal-service':
            raise BuyerSupportPilotError('pilot_rate_class_not_supported', 503)
        if not getattr(self.wb, 'configured', False):
            raise BuyerSupportPilotError('wb_not_configured', 503)
        if provider and not getattr(self.provider, 'configured', False):
            raise BuyerSupportPilotError('provider_not_configured', 503)

    def _actor(self, actor: str):
        _identifier(actor)

    def _can_reconcile(self, cabinet: str, op: dict) -> bool:
        # Saved explicit authorization survives OFF/allowlist removal. Account scope,
        # source capability and recorded quota class still constrain recovery reads.
        return bool(cabinet and cabinet == self.config.cabinet and op.get('write_attempted')
                    and getattr(self.wb, 'configured', False)
                    and op.get('token_rate_class', self.config.token_rate_class) == 'personal-service')

    def _public_operation(self, cabinet: str, op: dict) -> dict:
        public = dict(op)
        if public['state'] == 'write_started': public['state'] = 'unknown'
        public['can_reconcile'] = self._can_reconcile(cabinet, op)
        return public

    def _source(self, cabinet: str, kind: str, item_id: str) -> dict:
        try:
            item = self.repository.detail(cabinet, kind=kind, item_id=item_id)
        except KeyError:
            raise BuyerSupportPilotError('buyer_support_item_not_found', 404) from None
        # Explicit projection. Source timestamps of reads are freshness metadata, not content versions.
        messages, media_evidence, claim_extra = [], [], {}
        with self.repository._db() as db:
            if db and kind == 'chat':
                messages = [dict(r) for r in db.execute('SELECT id,timestamp,time,sender,text,rid,nm,media_count FROM events WHERE cabinet=? AND chat_id=? ORDER BY timestamp,id', (cabinet, item_id))]
                for r in db.execute('SELECT id,raw FROM events WHERE cabinet=? AND chat_id=?', (cabinet, item_id)):
                    raw = json.loads(r['raw']); attachments = raw.get('message', {}).get('attachments', {})
                    media = {k: v for k, v in attachments.items() if k != 'goodCard' and v}
                    if media: media_evidence.append({'source_id': r['id'], 'attachment_hash': _hash(media), 'state': 'unavailable'})
            if db:
                for c in item.get('claims', []):
                    r = db.execute('SELECT raw FROM claims WHERE cabinet=? AND id=?', (cabinet, c['id'])).fetchone()
                    raw = json.loads(r['raw'])
                    claim_extra[c['id']] = {k: raw.get(k) for k in ('claim_type', 'wb_comment', 'price', 'currency_code', 'order_dt', 'delivery_dt', 'origin_id_info')}
                    media = {k: raw[k] for k in ('photos', 'video_paths') if raw.get(k)}
                    if media: media_evidence.append({'source_id': c['id'], 'attachment_hash': _hash(media), 'state': 'unavailable'})
        claims = [{k: c.get(k) for k in ('id', 'srid', 'nm', 'status', 'status_ex', 'archive', 'comment',
                       'title', 'created_at', 'updated_at', 'actions', 'media_count', 'link_state', 'chat_ids')}
                  for c in item.get('claims', [])]
        for c in claims:
            c.update(claim_extra.get(c['id'], {}))
            c['claim_version'] = _hash(c)
            pair = (c['status'], c['status_ex'])
            c['observed_status'] = ('pending' if not c['archive'] and pair == ('0', '0') else
                                    'approved' if c['archive'] and pair in (('2', '5'), ('2', '10')) else 'unknown')
            c['return_method'] = 'keep_goods' if pair == ('2', '5') else 'return_goods' if pair == ('2', '10') else None
            c['deadline_at'] = None
            c['media_verification'] = 'unavailable' if c['media_count'] else 'not_present'
        source = {'case_id': kind + ':' + item_id, 'kind': kind, 'item_id': item_id,
                  'chat_id': item_id if kind == 'chat' else None,
                  'messages': messages, 'claims': claims, 'purchases': item.get('purchases', []),
                  'link_state': item.get('link_state', claims[0]['link_state'] if claims else 'unlinked'),
                  'source_freshness': 'available_local_history; fresh action preflight is separate',
                  'media_evidence': sorted(media_evidence, key=lambda m: (m['source_id'], m['attachment_hash'])),
                  'media_verification': {'state': 'unavailable', 'results': [],
                                         'note': 'Attachment presence/text is not proof of its contents.'}}
        if self.verification is not None:
            verified = self.verification.read_verified(cabinet=cabinet, kind=kind, item_id=item_id, snapshot=source)
            # Boundary must bind every result to current evidence, not silently promote arbitrary text.
            known = {(m['source_id'], m['attachment_hash']) for m in media_evidence}
            if not isinstance(verified, list) or any(not isinstance(v, dict) or (v.get('source_id'), v.get('attachment_hash')) not in known or not v.get('verified_feature') for v in verified):
                raise BuyerSupportPilotError('invalid_media_verification', 503)
            source['media_verification'] = {'state': 'verified' if verified else 'unavailable', 'results': verified}
        return source

    def _context(self, cabinet: str, kind: str, item_id: str) -> tuple[dict, str]:
        source = self._source(cabinet, kind, item_id)
        events = []
        with self.store.db() as db:
            if db:
                events = [json.loads(r['data']) | {'event_type': r['type']} for r in db.execute(
                    "SELECT type,data FROM internal_events WHERE cabinet=? AND kind=? AND item_id=? AND type IN ('operation_confirmed','operation_unknown','operation_blocked','claim_read_result','operation_read_result') ORDER BY seq",
                    (cabinet, kind, item_id))]
                source['operations'] = [json.loads(r['data']) for r in db.execute('SELECT data FROM operations WHERE cabinet=? AND item_id=? ORDER BY rowid', (cabinet, item_id))]
                # Do not put technical transport/raw errors or exact outgoing text into system facts.
                source['operations'] = [{k: o.get(k) for k in ('operation_id', 'kind', 'claim_id', 'action', 'state', 'write_attempted', 'readback')} for o in source['operations']]
                for op in source['operations']:
                    if op['state'] == 'write_started': op['state'] = 'unknown'
        source.setdefault('operations', [])
        source['internal_events'] = events
        return source, _hash(source)

    def detail(self, cabinet: str, kind: str, item_id: str) -> dict:
        source, version = self._context(cabinet, kind, item_id)
        item = self.repository.detail(cabinet, kind=kind, item_id=item_id)
        versions = {c['id']: c['claim_version'] for c in source['claims']}
        item['claims'] = [c | {'claim_version': versions[c['id']]} for c in item.get('claims', [])]
        drafts, operations, events = [], [], []
        with self.store.db() as db:
            if db:
                for r in db.execute('SELECT data,status FROM drafts WHERE cabinet=? AND kind=? AND item_id=? ORDER BY rowid DESC LIMIT 1', (cabinet, kind, item_id)):
                    d = json.loads(r['data']); d['status'] = r['status']
                    if d['context_version'] != version and d['status'] == 'ready': d['status'] = 'stale'
                    drafts.append(d)
                # operations.kind is action kind, item_id is target conversation; include chat/claim scope via data.
                operations = [json.loads(r['data']) for r in db.execute('SELECT data FROM operations WHERE cabinet=? AND item_id=? ORDER BY rowid', (cabinet, item_id))]
                operations = [self._public_operation(cabinet, op) for op in operations]
                events = [dict(r) | {'data': json.loads(r['data'])} for r in db.execute('SELECT seq,type,data,created_at FROM internal_events WHERE cabinet=? AND kind=? AND item_id=? ORDER BY seq', (cabinet, kind, item_id))]
        blocks = []
        fresh, refreshed_at = False, None
        generation_blocked = False
        with self.store.db() as db:
            row = db.execute("SELECT data,created_at FROM internal_events WHERE cabinet=? AND kind=? AND item_id=? AND type='source_refreshed' ORDER BY seq DESC LIMIT 1", (cabinet, kind, item_id)).fetchone() if db else None
            if row:
                refreshed_at = row['created_at']
                fresh = json.loads(row['data'])['context_version'] == version and (datetime.now(timezone.utc) - datetime.fromisoformat(refreshed_at)).total_seconds() <= 120
            if db:
                policy_hash = hashlib.sha256(self.policy_path.read_bytes()).hexdigest()
                semantic = _hash([kind, item_id, version, policy_hash, self.provider.profile])
                started = db.execute("SELECT 1 FROM internal_events WHERE cabinet=? AND kind=? AND item_id=? AND type='generation_started' AND data LIKE ? LIMIT 1", (cabinet, kind, item_id, '%"semantic_key":"' + semantic + '"%')).fetchone()
                finished = db.execute('SELECT 1 FROM drafts WHERE cabinet=? AND semantic_key=?', (cabinet, semantic)).fetchone()
                generation_blocked = bool(started and not finished)
        if not self.config.enabled: blocks.append('pilot_disabled')
        if not self._allowed(cabinet, kind, item_id): blocks.append('pilot_target_not_allowlisted')
        if self.config.token_rate_class != 'personal-service': blocks.append('pilot_rate_class_not_supported')
        if not getattr(self.provider, 'configured', False): blocks.append('provider_not_configured')
        if not getattr(self.wb, 'configured', False): blocks.append('wb_not_configured')
        if not fresh: blocks.append('source_refresh_required')
        if generation_blocked: blocks.append('generation_result_unknown')
        return item | {'context_version': version, 'pilot': {'enabled': self.config.enabled,
                'allowlisted': self._allowed(cabinet, kind, item_id), 'automatic_actions': False,
                'wb_configured': bool(getattr(self.wb, 'configured', False)), 'source_fresh': fresh,
                'generation_blocked': generation_blocked,
                'source_refreshed_at': refreshed_at, 'media_mode': 'text_first; media-dependent grounds need trusted verification',
                'provider_configured': bool(getattr(self.provider, 'configured', False)), 'blocked_codes': blocks},
                'workflow': {'state': operations[-1]['state'] if operations else 'observation',
                             'latest_draft': drafts[0] if drafts else None, 'operations': operations, 'internal_events': events}}

    def _save_snapshot(self, db, cabinet: str, source: dict, version: str):
        db.execute('INSERT OR IGNORE INTO source_snapshots VALUES(?,?,?,?,?,?)',
                   (cabinet, source['kind'], source['item_id'], version, _json(source), _utc()))

    def _validate_proposal(self, proposal: Any, source: dict):
        if not isinstance(proposal, dict) or set(proposal) != {'case_id', 'reply', 'operation'} or proposal.get('case_id') != source['case_id']:
            raise BuyerSupportPilotError('invalid_model_proposal', 503)
        if not isinstance(proposal['reply'], str) or len(proposal['reply']) > 1000:
            raise BuyerSupportPilotError('invalid_model_reply', 503)
        op = proposal['operation']
        if not isinstance(op, dict) or set(op) != {'kind', 'claim_id', 'operation_id', 'basis', 'basis_type'} or any(not isinstance(v, str) for v in op.values()):
            raise BuyerSupportPilotError('invalid_model_operation', 503)
        if op['basis_type'] not in ('text_sufficient', 'media_required', 'unknown'):
            raise BuyerSupportPilotError('invalid_model_basis_type', 503)
        if op['kind'] not in ('none', 'read_claim', 'read_operation', 'autorefund1', 'approve2') or len(op['basis']) > 2000:
            raise BuyerSupportPilotError('invalid_model_operation', 503)
        claim_ids = {c['id'] for c in source['claims']}
        if op['claim_id'] and op['claim_id'] not in claim_ids:
            raise BuyerSupportPilotError('fabricated_claim_id', 503)
        if op['kind'] == 'none' and (op['claim_id'] or op['operation_id']):
            raise BuyerSupportPilotError('unexpected_operation_identifiers', 503)
        if op['kind'] in ('read_claim', 'autorefund1', 'approve2') and not op['claim_id']:
            raise BuyerSupportPilotError('missing_claim_id', 503)
        if op['kind'] != 'read_operation' and op['operation_id']:
            raise BuyerSupportPilotError('unexpected_operation_id', 503)
        if op['kind'] == 'read_operation':
            if not op['operation_id'] or op['operation_id'] not in {e.get('operation_id') for e in source['operations']}:
                raise BuyerSupportPilotError('fabricated_operation_id', 503)
        if source['kind'] == 'claim' and proposal['reply']:
            raise BuyerSupportPilotError('orphan_claim_has_no_reply_channel', 503)

    @_record_safe_failure
    def refresh(self, cabinet: str, *, kind: str, item_id: str, request_id: str, actor: str) -> dict:
        """Explicit source refresh, displayed to operator before proposal confirmation."""
        self._actor(actor); _identifier(item_id); _identifier(request_id)
        self._gate(cabinet, kind, item_id)
        with admitted_write(self.runtime_dir), self.store.lock(cabinet, kind, item_id):
            replay = self.store.reserve(cabinet, request_id, 'refresh', {'kind': kind, 'item_id': item_id})
            if replay is not None:
                if replay.get('state') == 'failed': raise BuyerSupportPilotError(replay['error'], 503)
                return self.detail(cabinet, kind, item_id)
            try:
                self._refresh(cabinet, kind, item_id)
            except BuyerSupportPilotError as exc:
                receipt = {'kind': 'refresh', 'state': 'failed', 'write_attempted': False,
                           'error': exc.code, 'request_id': request_id, 'item_kind': kind, 'item_id': item_id}
                with self.store.db(write=True) as db:
                    self.store.finish(db, cabinet, request_id, receipt)
                    self.store.event(db, cabinet, kind, item_id, 'source_refresh_failed', receipt)
                raise
            _, version = self._context(cabinet, kind, item_id)
            with self.store.db(write=True) as db:
                self.store.event(db, cabinet, kind, item_id, 'source_refreshed', {'request_id': request_id, 'context_version': version})
            detail = self.detail(cabinet, kind, item_id)
            with self.store.db(write=True) as db:
                self.store.finish(db, cabinet, request_id, detail)
        return detail

    def _recent_refresh(self, cabinet: str, kind: str, item_id: str, version: str):
        with self.store.db() as db:
            row = db.execute("SELECT data,created_at FROM internal_events WHERE cabinet=? AND kind=? AND item_id=? AND type='source_refreshed' ORDER BY seq DESC LIMIT 1", (cabinet, kind, item_id)).fetchone() if db else None
        if not row or json.loads(row['data'])['context_version'] != version or (datetime.now(timezone.utc) - datetime.fromisoformat(row['created_at'])).total_seconds() > 120:
            raise BuyerSupportPilotError('source_refresh_required')

    @_record_safe_failure
    def propose(self, cabinet: str, *, chat_id: str = '', kind: str = 'chat', item_id: str = '',
                expected_context_version: str, request_id: str, actor: str) -> dict:
        return self._propose(cabinet, chat_id=chat_id, kind=kind, item_id=item_id,
                expected_context_version=expected_context_version, request_id=request_id, actor=actor)

    def _propose(self, cabinet: str, *, chat_id: str = '', kind: str = 'chat', item_id: str = '',
                 expected_context_version: str, request_id: str, actor: str, internal: bool = False) -> dict:
        item_id = item_id or chat_id
        _identifier(item_id); self._actor(actor)
        self._gate(cabinet, kind, item_id, provider=True)
        with admitted_write(self.runtime_dir), self.store.lock(cabinet, kind, item_id):
            source, version = self._context(cabinet, kind, item_id)
            if version != expected_context_version:
                raise BuyerSupportPilotError('stale_context')
            payload = {'kind': kind, 'item_id': item_id, 'context_version': version}
            replay = self.store.reserve(cabinet, request_id, 'propose', payload)
            if replay is not None: return replay
            if not internal: self._recent_refresh(cabinet, kind, item_id, version)
            policy = self.policy_path.read_text()
            policy_hash = hashlib.sha256(policy.encode()).hexdigest()
            profile = self.provider.profile
            semantic = _hash([kind, item_id, version, policy_hash, profile])
            with self.store.db(write=True) as db:
                prior = db.execute('SELECT data FROM drafts WHERE cabinet=? AND semantic_key=?', (cabinet, semantic)).fetchone()
                if prior:
                    d = json.loads(prior['data']); self.store.finish(db, cabinet, request_id, d); return d
                self._save_snapshot(db, cabinet, source, version)
                self.store.event(db, cabinet, kind, item_id, 'generation_started', {'request_id': request_id, 'context_version': version, 'policy_hash': policy_hash, 'model_profile': profile, 'semantic_key': semantic})
                # Any unfinished prior call for this same input is ambiguous, never paid twice.
                old = db.execute("SELECT COUNT(*) FROM internal_events WHERE cabinet=? AND kind=? AND item_id=? AND type='generation_started' AND data LIKE ?", (cabinet, kind, item_id, '%"semantic_key":"' + semantic + '"%')).fetchone()[0]
                if old > 1: raise BuyerSupportPilotError('generation_result_unknown')
            trace, read_limit = [], False
            try:
                # Bounded internal read continuation. These are real source events, not buyer utterances.
                for turn in range(2):
                    result = self.provider.generate(policy=policy, context=source)
                    call_receipt = {'response_id': result.get('response_id'), 'usage': result.get('usage'),
                                    'model_profile': result.get('model_profile', profile), 'context_version': version}
                    trace.append(call_receipt)
                    with self.store.db(write=True) as db:
                        self.store.event(db, cabinet, kind, item_id, 'provider_call_completed', {'request_id': request_id, 'continuation': turn} | call_receipt)
                    self._validate_proposal(result.get('proposal'), source)
                    suggested = result['proposal']['operation']
                    if suggested['kind'] not in ('read_claim', 'read_operation') or turn == 1:
                        read_limit = suggested['kind'] in ('read_claim', 'read_operation')
                        break
                    if suggested['kind'] == 'read_claim':
                        record = self._transport(lambda: self.wb.read_claim(self.repository, cabinet, suggested['claim_id']))
                        event = {'claim_id': suggested['claim_id'], 'found': record is not None,
                                 'status': str(record.get('status')) if record else None,
                                 'status_ex': str(record.get('status_ex')) if record else None}
                        event_type = 'claim_read_result'
                    else:
                        operation = self.operation(cabinet, suggested['operation_id'])
                        if operation['item_kind'] != kind or operation['item_id'] != item_id:
                            raise BuyerSupportPilotError('operation_context_mismatch')
                        operation = self._read_operation(cabinet, operation)
                        event = {'operation_id': operation['operation_id'], 'state': operation['state']}
                        event_type = 'operation_read_result'
                    with self.store.db(write=True) as db:
                        self.store.event(db, cabinet, kind, item_id, event_type, event)
                    source, version = self._context(cabinet, kind, item_id)
                    with self.store.db(write=True) as db:
                        self._save_snapshot(db, cabinet, source, version)
                        self.store.event(db, cabinet, kind, item_id, 'provider_call_started', {'request_id': request_id, 'continuation': turn + 1, 'context_version': version})
                else:
                    raise BuyerSupportPilotError('internal_read_limit', 503)
            except (PilotTransportError, BuyerSupportPilotError) as exc:
                code = exc.code
                receipt = {'kind': 'draft', 'request_id': request_id, 'status': 'generation_unknown', 'error': code, 'provider_trace': trace}
                with self.store.db(write=True) as db:
                    self.store.event(db, cabinet, kind, item_id, 'generation_unknown', receipt)
                    self.store.finish(db, cabinet, request_id, receipt)
                return receipt
            _, now_version = self._context(cabinet, kind, item_id)
            proposal = result['proposal']; op = proposal['operation']
            claim = next((c for c in source['claims'] if c['id'] == op['claim_id']), None)
            return_proposal = {'claim_id': claim['id'], 'action': op['kind'], 'claim_version': claim['claim_version'], 'basis': op['basis'], 'basis_type': op['basis_type']} if claim and op['kind'] in ('autorefund1', 'approve2') else None
            draft = {'kind': 'draft', 'draft_id': uuid4().hex, 'request_id': request_id,
                     'chat_id': item_id if kind == 'chat' else None, 'item_kind': kind, 'item_id': item_id,
                     'text': proposal['reply'], 'context_version': version, 'policy_hash': policy_hash,
                     'model_profile': result.get('model_profile', profile), 'status': ('read_limit' if read_limit else 'ready') if version == now_version else 'stale',
                     'operation': op, 'return_proposal': return_proposal, 'response_id': result.get('response_id'),
                     'usage': result.get('usage'), 'provider_trace': trace, 'created_at': _utc()}
            with self.store.db(write=True) as db:
                db.execute('INSERT INTO drafts VALUES(?,?,?,?,?,?,?)', (cabinet, draft['draft_id'], kind, item_id, semantic, _json(draft), draft['status']))
                self.store.event(db, cabinet, kind, item_id, 'draft_created', {'draft_id': draft['draft_id'], 'status': draft['status']})
                self.store.finish(db, cabinet, request_id, draft)
            return draft

    def _draft(self, cabinet: str, draft_id: str) -> dict:
        with self.store.db() as db:
            row = db.execute('SELECT data,status FROM drafts WHERE cabinet=? AND id=?', (cabinet, draft_id)).fetchone() if db else None
            if not row: raise BuyerSupportPilotError('draft_not_found', 404)
            return json.loads(row['data']) | {'status': row['status']}

    def _operation_save(self, db, cabinet: str, op: dict):
        db.execute('UPDATE operations SET data=? WHERE cabinet=? AND id=?', (_json(op), cabinet, op['operation_id']))

    def _transport(self, call):
        try:
            return call()
        except PilotTransportError as exc:
            raise BuyerSupportPilotError(exc.code, 503) from None

    def _refresh(self, cabinet: str, kind: str, item_id: str):
        result = self._transport(lambda: self.wb.refresh(self.repository, cabinet, kind=kind, item_id=item_id))
        if not isinstance(result, dict) or result.get('complete') is not True:
            raise BuyerSupportPilotError('source_refresh_incomplete', 503)
        source, version = self._context(cabinet, kind, item_id)
        with self.store.db(write=True) as db:
            self._save_snapshot(db, cabinet, source, version)
        return result

    @_record_safe_failure
    def send(self, cabinet: str, *, draft_id: str, expected_context_version: str,
             request_id: str, actor: str, confirmed: bool) -> dict:
        return self._dispatch(cabinet, draft_id=draft_id, expected_context_version=expected_context_version,
                              request_id=request_id, actor=actor, confirmed=confirmed, kind='chat_send')

    @_record_safe_failure
    def claim(self, cabinet: str, *, draft_id: str, claim_id: str, action: str,
              expected_context_version: str, expected_claim_version: str,
              request_id: str, actor: str, confirmed: bool, text_basis_confirmed: bool = False) -> dict:
        return self._dispatch(cabinet, draft_id=draft_id, expected_context_version=expected_context_version,
                request_id=request_id, actor=actor, confirmed=confirmed, kind='claim_decision',
                claim_id=claim_id, action=action, expected_claim_version=expected_claim_version,
                text_basis_confirmed=text_basis_confirmed)

    def _dispatch(self, cabinet: str, *, draft_id: str, expected_context_version: str,
                  request_id: str, actor: str, confirmed: bool, kind: str,
                  claim_id: str = '', action: str = '', expected_claim_version: str = '',
                  text_basis_confirmed: bool = False) -> dict:
        self._actor(actor)
        if confirmed is not True: raise BuyerSupportPilotError('explicit_confirmation_required', 422)
        draft = self._draft(cabinet, draft_id)
        item_kind, item_id = draft['item_kind'], draft['item_id']
        self._gate(cabinet, item_kind, item_id)
        payload = {'draft_id': draft_id, 'expected_context_version': expected_context_version,
                   'claim_id': claim_id, 'action': action, 'expected_claim_version': expected_claim_version,
                   'text_basis_confirmed': text_basis_confirmed}
        with admitted_write(self.runtime_dir), self.store.lock(cabinet, item_kind, item_id):
            # Request replay precedes staleness: already sent operation is recoverable.
            replay = self.store.reserve(cabinet, request_id, kind, payload)
            if replay is not None:
                if replay.get('state') == 'failed': raise BuyerSupportPilotError(replay['error'])
                return self.operation(cabinet, operation_id=replay['operation_id'])
            semantic = _hash([kind, draft_id]) if kind == 'chat_send' else _hash([kind, claim_id, expected_claim_version, action])
            with self.store.db(write=True) as db:
                old = db.execute('SELECT data FROM operations WHERE cabinet=? AND semantic_key=?', (cabinet, semantic)).fetchone()
                if old:
                    op = json.loads(old['data']); self.store.finish(db, cabinet, request_id, op); return op
            if draft['status'] != 'ready' or draft['context_version'] != expected_context_version:
                raise BuyerSupportPilotError('stale_draft')
            if hashlib.sha256(self.policy_path.read_bytes()).hexdigest() != draft['policy_hash']:
                raise BuyerSupportPilotError('stale_policy')
            if item_kind != 'chat': raise BuyerSupportPilotError('claim_chat_link_unconfirmed')
            before, _ = self._context(cabinet, item_kind, item_id)
            if any(o['state'] != 'confirmed' for o in before['operations']):
                raise BuyerSupportPilotError('previous_operation_requires_readback')
            if kind == 'chat_send' and not draft['text'].strip():
                raise BuyerSupportPilotError('empty_reply')
            if kind == 'chat_send' and draft['return_proposal']:
                # A mechanical execution-state guard, not a return/business classifier.
                if _unconfirmed_execution_assertion(draft['text']):
                    raise BuyerSupportPilotError('unconfirmed_execution_assertion')
            fresh = self._refresh(cabinet, item_kind, item_id)
            source, version = self._context(cabinet, item_kind, item_id)
            if version != expected_context_version: raise BuyerSupportPilotError('stale_context')
            if kind == 'claim_decision':
                proposal = draft['return_proposal']
                target = next((c for c in source['claims'] if c['id'] == claim_id), None)
                if not proposal or proposal['claim_id'] != claim_id or proposal['action'] != action or action not in ('autorefund1', 'approve2'):
                    raise BuyerSupportPilotError('claim_proposal_mismatch')
                if proposal['basis_type'] != 'text_sufficient':
                    raise BuyerSupportPilotError('media_capability_unavailable', 503)
                if text_basis_confirmed is not True:
                    raise BuyerSupportPilotError('text_basis_confirmation_required', 422)
                if target is None or target['link_state'] != 'linked' or target['chat_ids'] != [item_id] or target['srid'] not in {p['rid'] for p in source['purchases']}:
                    raise BuyerSupportPilotError('claim_chat_link_unconfirmed')
                if claim_id not in self.config.claim_ids:
                    raise BuyerSupportPilotError('pilot_claim_not_allowlisted', 403)
                current_claim = self._transport(lambda: self.wb.read_claim(self.repository, cabinet, claim_id))
                if current_claim is None: raise BuyerSupportPilotError('claim_current_state_unknown', 503)
                source, version = self._context(cabinet, item_kind, item_id)
                target = next(c for c in source['claims'] if c['id'] == claim_id)
                if version != expected_context_version or target['claim_version'] != expected_claim_version or proposal['claim_version'] != expected_claim_version:
                    raise BuyerSupportPilotError('stale_claim')
                if target['observed_status'] != 'pending': raise BuyerSupportPilotError('claim_not_pending')
                if action not in target['actions']: raise BuyerSupportPilotError('claim_action_unavailable')
            elif not fresh.get('reply_sign'):
                raise BuyerSupportPilotError('chat_signature_unavailable', 503)
            # Recheck gate and context immediately before durable attempt; no signatures persisted.
            self._gate(cabinet, item_kind, item_id)
            _, last_version = self._context(cabinet, item_kind, item_id)
            if last_version != expected_context_version: raise BuyerSupportPilotError('stale_context')
            op = {'operation_id': uuid4().hex, 'request_id': request_id, 'kind': kind, 'item_kind': item_kind,
                  'item_id': item_id, 'chat_id': item_id, 'draft_id': draft_id, 'claim_id': claim_id or None,
                  'action': action or None, 'state': 'write_started', 'write_attempted': True,
                  'exact_text': draft['text'] if kind == 'chat_send' else None,
                  'context_version': expected_context_version, 'claim_version': expected_claim_version or None,
                  'baseline_event_ids': [m['id'] for m in source['messages']], 'actor': actor,
                  'attempt_id': uuid4().hex, 'started_at': _utc(), 'transport': None}
            op['token_rate_class'] = self.config.token_rate_class
            op['text_basis_confirmed'] = text_basis_confirmed if kind == 'claim_decision' else None
            op['basis_type'] = draft['return_proposal']['basis_type'] if kind == 'claim_decision' else None
            with self.store.db(write=True) as db:
                self._save_snapshot(db, cabinet, source, version)
                db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?)', (cabinet, op['operation_id'], kind, item_id, semantic, _json(op)))
                self.store.event(db, cabinet, item_kind, item_id, 'write_attempt_started', {'operation_id': op['operation_id'], 'kind': kind})
                self.store.finish(db, cabinet, request_id, op)
                db.execute('UPDATE drafts SET status=? WHERE cabinet=? AND id=?', ('consumed', cabinet, draft_id))
            try:
                result = self.wb.send_message(reply_sign=fresh['reply_sign'], text=draft['text']) if kind == 'chat_send' else self.wb.decide_claim(claim_id=claim_id, action=action)
                receipt = result.get('payload', {}).get('result', {}) if kind == 'chat_send' else {}
                # Explicitly allowlist receipt fields, never persist arbitrary provider bodies.
                op['transport'] = {'http_status': result.get('http_status'), 'chat_id': receipt.get('chatID'), 'add_time': receipt.get('addTime')}
                op['state'] = 'pending_readback'
            except Exception as exc:
                op['transport'] = {'http_status': getattr(exc, 'http_status', None), 'code': getattr(exc, 'code', 'write_result_unknown')}
                op['state'] = 'unknown'
            with self.store.db(write=True) as db:
                self._operation_save(db, cabinet, op)
                self.store.event(db, cabinet, item_kind, item_id, 'operation_unknown' if op['state'] == 'unknown' else 'transport_received', {'operation_id': op['operation_id'], 'state': op['state']})
                self.store.finish(db, cabinet, request_id, op)
            return self._public_operation(cabinet, op)

    def operation(self, cabinet: str, operation_id: str = '', request_id: str = '') -> dict:
        with self.store.db() as db:
            if request_id:
                r = db.execute('SELECT * FROM requests WHERE cabinet=? AND id=?', (cabinet, request_id)).fetchone() if db else None
                if not r: raise BuyerSupportPilotError('request_not_found', 404)
                if not r['result']: return {'request_id': request_id, 'state': 'request_result_unknown', 'kind': r['kind'], 'write_attempted': False}
                receipt = json.loads(r['result'])
                if r['kind'] == 'refresh' and receipt.get('state') != 'failed': return self.detail(cabinet, receipt['kind'], receipt['id'])
                if not receipt.get('operation_id'): return receipt
                operation_id = receipt['operation_id']
            _identifier(operation_id)
            row = db.execute('SELECT data FROM operations WHERE cabinet=? AND id=?', (cabinet, operation_id)).fetchone() if db else None
            if not row: raise BuyerSupportPilotError('operation_not_found', 404)
            op = json.loads(row['data'])
            # Process may have died after attempt commit. It is never eligible for dispatch again.
            return self._public_operation(cabinet, op)

    def _read_operation(self, cabinet: str, op: dict) -> dict:
        try:
            self._refresh(cabinet, op['item_kind'], op['item_id'])
            source, _ = self._context(cabinet, op['item_kind'], op['item_id'])
            if op['kind'] == 'claim_decision':
                record = self._transport(lambda: self.wb.read_claim(self.repository, cabinet, op['claim_id']))
                pair = (str(record.get('status')), str(record.get('status_ex'))) if record else None
                expected = ('2', '5') if op['action'] == 'autorefund1' else ('2', '10')
                confirmed = bool(record and record.get('archive') is True and pair == expected)
                op['readback'] = {'status': pair[0] if pair else None, 'status_ex': pair[1] if pair else None, 'archive': record.get('archive') if record else None}
            else:
                receipt = op.get('transport') or {}
                matches = [m for m in source['messages'] if m['id'] not in op['baseline_event_ids'] and m['sender'] == 'seller' and m['text'] == op['exact_text']]
                confirmed = bool(len(matches) == 1 and receipt.get('chat_id') == op['chat_id'] and receipt.get('add_time') == matches[0]['timestamp'])
                op['readback'] = {'matching_event_ids': [m['id'] for m in matches], 'receipt_correlation': confirmed}
            op['state'] = 'confirmed' if confirmed else 'unknown'
        except BuyerSupportPilotError as exc:
            op['state'] = 'unknown'; op['readback_error'] = exc.code
        with self.store.db(write=True) as db:
            self._operation_save(db, cabinet, op)
            self.store.event(db, cabinet, op['item_kind'], op['item_id'], 'operation_confirmed' if op['state'] == 'confirmed' else 'operation_unknown',
                {'operation_id': op['operation_id'], 'kind': op['kind'], 'claim_id': op['claim_id'], 'action': op['action'], 'state': op['state']})
        return op

    def reconcile(self, cabinet: str, *, operation_id: str, request_id: str, actor: str) -> dict:
        self._actor(actor)
        op = self.operation(cabinet, operation_id=operation_id)
        if not self._can_reconcile(cabinet, op):
            raise BuyerSupportPilotError('operation_readback_not_permitted', 403)
        with admitted_write(self.runtime_dir), self.store.lock(cabinet, op['item_kind'], op['item_id']):
            replay = self.store.reserve(cabinet, request_id, 'reconcile', {'operation_id': operation_id})
            if replay is not None: op = self.operation(cabinet, operation_id=operation_id)
            if op['state'] != 'confirmed': op = self._read_operation(cabinet, op)
            with self.store.db(write=True) as db:
                self.store.finish(db, cabinet, request_id, op)
        if op['state'] == 'confirmed' and op['kind'] == 'claim_decision' and not op.get('refresh_draft_id'):
            # A real operation event is the trigger. Never fabricate a buyer utterance or auto-send.
            try:
                _, version = self._context(cabinet, op['item_kind'], op['item_id'])
                draft = self._propose(cabinet, kind=op['item_kind'], item_id=op['item_id'], internal=True,
                                     expected_context_version=version, request_id='result-' + operation_id, actor=actor)
                op['refresh_draft_id'] = draft.get('draft_id')
                op['response_draft_state'] = draft.get('status')
            except BuyerSupportPilotError as exc:
                op['response_draft_state'] = 'draft_pending'; op['draft_error'] = exc.code
            with self.store.db(write=True) as db:
                self._operation_save(db, cabinet, op); self.store.finish(db, cabinet, request_id, op)
        return self._public_operation(cabinet, op)

    def tick(self, cabinet: str, *, kind: str, item_id: str, request_id: str, actor: str) -> dict:
        """Future event ingestion entrypoint. Generates only; cannot dispatch any action."""
        self._actor(actor); self._gate(cabinet, kind, item_id)
        with admitted_write(self.runtime_dir), self.store.lock(cabinet, kind, item_id):
            self._refresh(cabinet, kind, item_id)
            _, version = self._context(cabinet, kind, item_id)
        return self._propose(cabinet, kind=kind, item_id=item_id, expected_context_version=version, request_id=request_id, actor=actor, internal=True)

    def set_automatic_actions(self, enabled: bool):
        if enabled: raise BuyerSupportPilotError('automatic_dispatch_forbidden_in_pilot', 403)
        return {'automatic_actions': False}
