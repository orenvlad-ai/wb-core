"""Local observation model for buyer chats/claims, with strictly scoped evidence.

No publication, LLM or scheduling code lives here. External data is never a bot
judgement. Chat purchase evidence is retained per event, not copied to all events.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterator


class BuyerSupportValidationError(ValueError):
    pass


def _id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 512:
        raise BuyerSupportValidationError("invalid identifier")
    return value


def _integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BuyerSupportValidationError("invalid non-negative integer")
    return value


def _object(value: Any) -> dict:
    if not isinstance(value, dict):
        raise BuyerSupportValidationError("invalid object")
    return value


def _rows(value: Any) -> list[dict]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise BuyerSupportValidationError("invalid rows")
    return value


def _payload(value: Any) -> dict:
    value = _object(value)
    if value.get("error") or value.get("errors"):
        raise BuyerSupportValidationError("upstream error payload")
    return value


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _private_evidence(value: Any) -> Any:
    """Keep source evidence privately, excluding credentials even in nested fields."""
    if isinstance(value, dict):
        return {k: _private_evidence(v) for k, v in value.items()
                if not any(s in k.lower().replace('_', '') for s in
                           ('replysign', 'authorization', 'token', 'password', 'secret', 'cookie', 'signature'))}
    if isinstance(value, list):
        return [_private_evidence(v) for v in value]
    return value


def _json(value: Any) -> str:
    return json.dumps(_private_evidence(value), ensure_ascii=False, sort_keys=True)


def _card(value: Any) -> tuple[str | None, str | None, str]:
    if value is None:
        return None, None, ""
    value = _object(value)
    rid = _id(value['rid']) if value.get('rid') else None
    nm = value.get('nmID')
    if nm is not None:
        nm = str(_integer(nm))
    return rid, nm, _text(value.get('name'))


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class BuyerSupportRepository:
    def __init__(self, runtime_dir: Path):
        self.path = Path(runtime_dir).resolve() / 'buyer-support' / 'observation.sqlite3'

    @contextmanager
    def _db(self, *, write: bool = False) -> Iterator[sqlite3.Connection | None]:
        if not write and not self.path.exists():
            yield None
            return
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.path.parent, 0o700)
            # Creation mode is private from the first open, not only after schema creation.
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            os.chmod(self.path, 0o600)
        db = sqlite3.connect(str(self.path) if write else self.path.as_uri() + '?mode=ro', uri=not write, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute('PRAGMA foreign_keys=ON')
                db.executescript('''
                CREATE TABLE IF NOT EXISTS sync_state(cabinet TEXT, source TEXT, cursor INTEGER,
                  state TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(cabinet,source));
                CREATE TABLE IF NOT EXISTS chats(cabinet TEXT, id TEXT, name TEXT NOT NULL, raw TEXT NOT NULL,
                  seen_at TEXT NOT NULL, PRIMARY KEY(cabinet,id));
                CREATE TABLE IF NOT EXISTS purchases(cabinet TEXT, rid TEXT, PRIMARY KEY(cabinet,rid));
                CREATE TABLE IF NOT EXISTS purchase_evidence(cabinet TEXT, rid TEXT, chat_id TEXT, source_id TEXT,
                  nm TEXT, name TEXT NOT NULL, PRIMARY KEY(cabinet,rid,chat_id,source_id),
                  FOREIGN KEY(cabinet,rid) REFERENCES purchases(cabinet,rid));
                CREATE TABLE IF NOT EXISTS events(cabinet TEXT, id TEXT, chat_id TEXT NOT NULL, timestamp INTEGER,
                  time TEXT NOT NULL, sender TEXT NOT NULL, text TEXT NOT NULL, rid TEXT, nm TEXT,
                  media_count INTEGER NOT NULL, raw TEXT NOT NULL, seen_at TEXT NOT NULL, PRIMARY KEY(cabinet,id));
                CREATE INDEX IF NOT EXISTS event_chat ON events(cabinet,chat_id,timestamp,id);
                CREATE TABLE IF NOT EXISTS claims(cabinet TEXT, id TEXT, srid TEXT, nm TEXT, archive INTEGER NOT NULL,
                  status TEXT, status_ex TEXT, comment TEXT NOT NULL, title TEXT NOT NULL,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, actions TEXT NOT NULL,
                  media_count INTEGER NOT NULL, raw TEXT NOT NULL, seen_at TEXT NOT NULL, PRIMARY KEY(cabinet,id));
                ''')
                db.execute('BEGIN IMMEDIATE')
            else:
                db.execute('PRAGMA query_only=ON')
                db.execute('BEGIN')
            yield db
            if write:
                db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def cursor(self, cabinet: str) -> int | None:
        with self._db() as db:
            row = db.execute("SELECT cursor FROM sync_state WHERE cabinet=? AND source='events'", (cabinet,)).fetchone() if db else None
            return row['cursor'] if row else None

    def _evidence(self, db: sqlite3.Connection, cabinet: str, chat: str, source: str,
                  card: tuple[str | None, str | None, str]) -> None:
        rid, nm, name = card
        if rid:
            db.execute('INSERT OR IGNORE INTO purchases VALUES(?,?)', (cabinet, rid))
            # Keep every observed product identity for a source. A later snapshot can
            # enrich a missing nm or reveal a conflict; neither may be discarded.
            source_key = source + ':nm:' + (nm or 'unknown')
            db.execute('INSERT INTO purchase_evidence VALUES(?,?,?,?,?,?) ON CONFLICT(cabinet,rid,chat_id,source_id) DO UPDATE SET name=excluded.name',
                       (cabinet, rid, chat, source_key, nm, name))

    def save_chats(self, cabinet: str, payload: dict) -> int:
        cabinet = _id(cabinet)
        chats = _rows(_payload(payload).get('result'))
        validated = [(_id(c.get('chatID')), _text(c.get('clientName')), _card(c.get('goodCard')), _json(c)) for c in chats]
        with self._db(write=True) as db:
            for chat, name, card, raw in validated:
                db.execute('INSERT INTO chats VALUES(?,?,?,?,?) ON CONFLICT(cabinet,id) DO UPDATE SET name=excluded.name,raw=excluded.raw,seen_at=excluded.seen_at', (cabinet, chat, name, raw, _utc()))
                self._evidence(db, cabinet, chat, 'chat:' + chat, card)
            self._state(db, cabinet, 'chats', 'complete')
        return len(validated)

    def save_event_page(self, cabinet: str, payload: dict, *, expected_cursor: int | None) -> tuple[int, int]:
        cabinet = _id(cabinet)
        result = _object(_payload(payload).get('result'))
        events = _rows(result.get('events'))
        total, next_cursor = _integer(result.get('totalEvents')), _integer(result.get('next'))
        if total != len(events) or (events and expected_cursor is not None and next_cursor <= expected_cursor):
            raise BuyerSupportValidationError('inconsistent event page/cursor')
        validated = []
        for event in events:
            eid, chat = _id(event.get('eventID')), _id(event.get('chatID'))
            message = _object(event.get('message', {}))
            attachments = _object(message.get('attachments', {}))
            card = _card(attachments.get('goodCard'))
            ts = _integer(event.get('addTimestamp'))
            media_count = sum(len(v) if isinstance(v, list) else 1 for k, v in attachments.items() if k != 'goodCard' and v)
            validated.append((eid, chat, ts, _text(event.get('addTime')), _text(event.get('sender')),
                              _text(message.get('text')), card, media_count, _json(event), _text(event.get('clientName'))))
        # Whole-page validation precedes transaction; event writes and cursor CAS share one commit.
        with self._db(write=True) as db:
            row = db.execute("SELECT cursor FROM sync_state WHERE cabinet=? AND source='events'", (cabinet,)).fetchone()
            if (row['cursor'] if row else None) != expected_cursor:
                raise BuyerSupportValidationError('event cursor changed concurrently')
            inserted = 0
            for eid, chat, ts, time, sender, text, card, count, raw, name in validated:
                old = db.execute('SELECT raw FROM events WHERE cabinet=? AND id=?', (cabinet, eid)).fetchone()
                if old and old['raw'] != raw:
                    raise BuyerSupportValidationError('conflicting event identifier')
                if old:
                    continue
                db.execute('INSERT OR IGNORE INTO chats VALUES(?,?,?,?,?)', (cabinet, chat, name, '{}', _utc()))
                db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (cabinet, eid, chat, ts, time, sender, text, card[0], card[1], count, raw, _utc()))
                self._evidence(db, cabinet, chat, 'event:' + eid, card)
                inserted += 1
            # Empty end-page can echo or advance next; it must never regress the durable cursor.
            durable = max(next_cursor, expected_cursor or 0)
            self._state(db, cabinet, 'events', 'complete' if not events else 'partial', durable)
        return inserted, total

    @staticmethod
    def _state(db: sqlite3.Connection, cabinet: str, source: str, state: str, cursor: int | None = None) -> None:
        db.execute('INSERT INTO sync_state VALUES(?,?,?,?,?) ON CONFLICT(cabinet,source) DO UPDATE SET cursor=COALESCE(excluded.cursor,sync_state.cursor),state=excluded.state,updated_at=excluded.updated_at', (cabinet, source, cursor, state, _utc()))

    def state(self, cabinet: str, source: str, state: str) -> None:
        with self._db(write=True) as db:
            self._state(db, _id(cabinet), source, state)

    def save_claim_page(self, cabinet: str, payload: dict, *, archive: bool) -> tuple[int, int]:
        cabinet = _id(cabinet)
        payload = _payload(payload)
        claims, total = _rows(payload.get('claims')), _integer(payload.get('total'))
        if len(claims) > total:
            raise BuyerSupportValidationError('inconsistent claim total')
        validated = []
        for claim in claims:
            cid = _id(claim.get('id'))
            srid = _id(claim['srid']) if claim.get('srid') else None
            nm = str(_integer(claim['nm_id'])) if claim.get('nm_id') is not None else None
            actions = claim.get('actions', [])
            if not isinstance(actions, list) or any(not isinstance(v, str) for v in actions):
                raise BuyerSupportValidationError('invalid claim actions')
            photos, videos = claim.get('photos', []), claim.get('video_paths', [])
            if not isinstance(photos, list) or not isinstance(videos, list):
                raise BuyerSupportValidationError('invalid claim media')
            validated.append((cabinet, cid, srid, nm, int(archive), str(claim.get('status')) if claim.get('status') is not None else None,
                              str(claim.get('status_ex')) if claim.get('status_ex') is not None else None,
                              _text(claim.get('user_comment')), _text(claim.get('imt_name')), _text(claim.get('dt')),
                              _text(claim.get('dt_update')), _json(actions), len(photos) + len(videos), _json(claim), _utc()))
        with self._db(write=True) as db:
            for row in validated:
                if row[2]:
                    db.execute('INSERT OR IGNORE INTO purchases VALUES(?,?)', (cabinet, row[2]))
                db.execute('''INSERT INTO claims VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(cabinet,id)
                  DO UPDATE SET srid=excluded.srid,nm=excluded.nm,archive=excluded.archive,status=excluded.status,
                  status_ex=excluded.status_ex,comment=excluded.comment,title=excluded.title,created_at=excluded.created_at,
                  updated_at=excluded.updated_at,actions=excluded.actions,media_count=excluded.media_count,
                  raw=excluded.raw,seen_at=excluded.seen_at''', row)
            self._state(db, cabinet, 'claims_archive' if archive else 'claims_active', 'partial')
        # Absence is never closure: external scans are a limited, moving window.
        return len(validated), total

    def _claim_public(self, db: sqlite3.Connection, row: sqlite3.Row) -> dict:
        evidence = list(db.execute('SELECT * FROM purchase_evidence WHERE cabinet=? AND rid=?', (row['cabinet'], row['srid']))) if row['srid'] else []
        matching = {e['chat_id'] for e in evidence}
        conflicting = len({e['nm'] for e in evidence if e['nm']}) > 1 or any(e['nm'] and row['nm'] and e['nm'] != row['nm'] for e in evidence)
        link = ('orphan' if row['srid'] else 'unlinked') if not matching else 'ambiguous' if len(matching) > 1 or conflicting else 'linked'
        return {k: row[k] for k in ('id', 'srid', 'nm', 'status', 'status_ex', 'comment', 'title', 'created_at', 'updated_at', 'seen_at', 'media_count')} | {
            'archive': bool(row['archive']), 'actions': json.loads(row['actions']), 'link_state': link,
            'chat_ids': sorted(matching), 'deadline_at': None, 'deadline_state': 'unknown',
            'time_zone': 'unknown', 'decision': None,
            'link_reason': 'cabinet + exact rid=srid; product conflicts and multiple chats block unique association' if evidence else 'no exact chat purchase evidence in this cabinet' if row['srid'] else 'claim purchase identifier is absent'}

    def _chat_public(self, db: sqlite3.Connection, row: sqlite3.Row) -> dict:
        latest = db.execute('SELECT text,time FROM events WHERE cabinet=? AND chat_id=? ORDER BY timestamp DESC,id DESC LIMIT 1', (row['cabinet'], row['id'])).fetchone()
        purchases = [dict(p) for p in db.execute('SELECT DISTINCT rid,nm,name FROM purchase_evidence WHERE cabinet=? AND chat_id=? ORDER BY rid', (row['cabinet'], row['id']))]
        purchase_conflict = any(len({p['nm'] for p in purchases if p['rid'] == rid and p['nm']}) > 1
                                for rid in {p['rid'] for p in purchases})
        claims = [self._claim_public(db, c) for c in db.execute('SELECT * FROM claims WHERE cabinet=? AND srid IN (SELECT rid FROM purchase_evidence WHERE cabinet=? AND chat_id=?) ORDER BY seen_at DESC,id', (row['cabinet'], row['cabinet'], row['id']))]
        return {'kind': 'chat', 'id': row['id'], 'name': row['name'] or 'Покупатель',
                'preview': latest['text'] if latest else '', 'time': latest['time'] if latest else '',
                'purchases': purchases, 'claims': claims, 'seen_at': row['seen_at'], 'decision': None,
                'link_state': 'ambiguous' if purchase_conflict or len({p['rid'] for p in purchases}) > 1 or any(c['link_state'] == 'ambiguous' for c in claims) else 'linked' if purchases else 'unlinked'}

    def list_items(self, cabinet: str, *, query: str = '', filter_state: str = 'all', offset: int = 0, limit: int = 50) -> dict:
        if filter_state not in ('all', 'active', 'archive', 'orphan', 'unlinked', 'ambiguous') or offset < 0 or not 1 <= limit <= 100 or len(query) > 256:
            raise BuyerSupportValidationError('invalid list query')
        base = {'mode': 'observation', 'automatic_actions': False, 'configured': bool(cabinet), 'items': [], 'total': 0,
                'offset': offset, 'limit': limit, 'sync': []}
        if not cabinet:
            return base
        with self._db() as db:
            if db is None:
                return base
            items = [self._chat_public(db, row) for row in db.execute('SELECT * FROM chats WHERE cabinet=?', (cabinet,))]
            for row in db.execute('SELECT * FROM claims WHERE cabinet=?', (cabinet,)):
                claim = self._claim_public(db, row)
                # Ambiguous claims stay independent; no chat is chosen arbitrarily.
                if claim['link_state'] != 'linked':
                    items.append({'kind': 'claim', 'id': row['id'], 'name': 'Заявка без однозначного чата',
                                  'preview': row['comment'], 'time': row['created_at'], 'seen_at': row['seen_at'],
                                  'purchases': [], 'claims': [claim], 'link_state': claim['link_state'], 'decision': None})
            text = query.casefold()
            def matches(item: dict) -> bool:
                if text and text not in json.dumps(item, ensure_ascii=False).casefold():
                    return False
                if filter_state == 'all': return True
                if filter_state in ('orphan', 'unlinked', 'ambiguous'): return item['link_state'] == filter_state
                return any(c['archive'] == (filter_state == 'archive') for c in item['claims'])
            items = sorted((i for i in items if matches(i)), key=lambda i: (i['time'], i['id']), reverse=True)
            base.update(items=items[offset:offset + limit], total=len(items), sync=[dict(r) for r in db.execute('SELECT source,state,updated_at FROM sync_state WHERE cabinet=? ORDER BY source', (cabinet,))])
        return base

    def detail(self, cabinet: str, *, kind: str, item_id: str) -> dict:
        if kind not in ('chat', 'claim'):
            raise BuyerSupportValidationError('invalid item kind')
        _id(item_id)
        with self._db() as db:
            row = db.execute('SELECT * FROM ' + ('chats' if kind == 'chat' else 'claims') + ' WHERE cabinet=? AND id=?', (cabinet, item_id)).fetchone() if db else None
            if row is None:
                raise KeyError('buyer_support_item_not_found')
            if kind == 'claim':
                return {'kind': kind, 'id': item_id, 'name': 'Заявка на возврат', 'claims': [self._claim_public(db, row)], 'messages': [], 'decision': None}
            item = self._chat_public(db, row)
            item['messages'] = [dict(e) for e in db.execute('SELECT id,time,sender,text,rid,nm,media_count FROM events WHERE cabinet=? AND chat_id=? ORDER BY timestamp,id', (cabinet, item_id))]
            rids = {p['rid'] for p in item['purchases']}
            for message in item['messages']:
                message['purchase_link_state'] = ('inline' if message['rid'] else
                    'observed_single_purchase' if len(rids) == 1 else 'ambiguous' if len(rids) > 1 else 'unlinked')
                message['candidate_rid'] = next(iter(rids)) if not message['rid'] and len(rids) == 1 else None
            return item


class BuyerSupportSync:
    def __init__(self, repository: BuyerSupportRepository, adapter: Any, *, page_pause_seconds: float = 3):
        self.repository, self.adapter = repository, adapter
        self.page_pause_seconds = page_pause_seconds

    def run(self, cabinet: str, *, max_pages: int = 1000) -> dict:
        import time
        cabinet = _id(cabinet)
        if max_pages < 1 or self.page_pause_seconds < 0:
            raise BuyerSupportValidationError('invalid sync limits')
        self.repository.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.repository.path.parent / 'sync.lock'
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, 'w') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BuyerSupportValidationError('sync already running') from None
            counts = {'chats': 0, 'events': 0, 'claims_active': 0, 'claims_archive': 0}
            source = 'chats'
            try:
                self.repository.state(cabinet, source, 'running')
                counts[source] = self.repository.save_chats(cabinet, self.adapter.fetch_chats())
                time.sleep(self.page_pause_seconds)
                source = 'events'
                self.repository.state(cabinet, source, 'running')
                for _ in range(max_pages):
                    cursor = self.repository.cursor(cabinet)
                    inserted, total = self.repository.save_event_page(cabinet, self.adapter.fetch_events(cursor), expected_cursor=cursor)
                    counts[source] += inserted
                    if total == 0: break
                    time.sleep(self.page_pause_seconds)
                else:
                    raise BuyerSupportValidationError('event page limit reached')
                for archive in (False, True):
                    source = 'claims_archive' if archive else 'claims_active'
                    self.repository.state(cabinet, source, 'running')
                    offset = 0
                    seen = set()
                    for _ in range(max_pages):
                        time.sleep(self.page_pause_seconds)
                        payload = self.adapter.fetch_claims(archive=archive, offset=offset, limit=200)
                        rows = _rows(_payload(payload).get('claims'))
                        ids = [_id(c.get('id')) for c in rows]
                        if len(set(ids)) != len(ids) or seen.intersection(ids):
                            raise BuyerSupportValidationError('claim pagination repeated identifiers')
                        count, total = self.repository.save_claim_page(cabinet, payload, archive=archive)
                        seen.update(ids)
                        offset += count
                        counts[source] += count
                        if offset >= total:
                            self.repository.state(cabinet, source, 'complete')
                            break
                        if count == 0:
                            raise BuyerSupportValidationError('claim pagination incomplete')
                    else:
                        raise BuyerSupportValidationError('claim page limit reached')
            except Exception:
                self.repository.state(cabinet, source, 'error')
                raise
            return {'mode': 'observation', 'automatic_actions': False, 'counts': counts}
