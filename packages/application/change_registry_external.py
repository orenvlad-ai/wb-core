"""Register portal evidence against canonical facts, without any WB transport.

Observed facts remain immutable. Revisions refine their provenance and time;
chains retain intermediate transitions without counting the net fact twice.
An unobserved transition must first be admitted by the ordinary observer.
"""
from __future__ import annotations

import base64
import hashlib
from io import BytesIO
import zipfile
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
import socket

from apps.production_apply_contract import AdapterError
from packages.application.change_registry import canonical_digest, canonical_json
from packages.application.storage_registry import StoreRegistry

EVIDENCE = 'change_registry_external_evidence'
BATCHES = 'change_registry_external_batches'


def require(condition, message):
    if not condition:
        raise AdapterError(message)


def utc(value):
    require(isinstance(value, str) and bool(re.search(r'(Z|[+-]\d\d:\d\d)$', value)), 'Время должно содержать часовой пояс.')
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
    except ValueError as exc:
        raise AdapterError('Некорректное время.') from exc


def exists(conn, table):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def migrate(conn):
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {BATCHES}(
        operation_id TEXT PRIMARY KEY, seller_id TEXT NOT NULL, account_scope TEXT NOT NULL,
        candidate_digest TEXT NOT NULL, receipt_json TEXT NOT NULL, registered_at TEXT NOT NULL)''')
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {EVIDENCE}(
        evidence_id TEXT PRIMARY KEY, fact_id TEXT NOT NULL REFERENCES change_registry_facts(fact_id),
        revision INTEGER NOT NULL CHECK(revision>0), payload_digest TEXT NOT NULL,
        payload_json TEXT NOT NULL, actor TEXT NOT NULL, registered_at TEXT NOT NULL,
        operation_id TEXT NOT NULL REFERENCES {BATCHES}(operation_id),
        UNIQUE(fact_id,revision), UNIQUE(fact_id,payload_digest))''')
    for table in (EVIDENCE, BATCHES):
        for action in ('UPDATE', 'DELETE'):
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'external registry is append-only'); END")


def latest(conn, fact_id):
    if not exists(conn, EVIDENCE):
        return None
    row = conn.execute(f'SELECT * FROM {EVIDENCE} WHERE fact_id=? ORDER BY revision DESC LIMIT 1', (fact_id,)).fetchone()
    return dict(row) if row else None


def _proof(step, fact, *, first_step):
    """Native bid exports prove seconds; submit/readback receipts prove windows."""
    proof = step.get('evidence')
    require(isinstance(proof, dict), 'Нужно подтверждение изменения.')
    require(isinstance(proof.get('reference'), str) and 0 < len(proof['reference']) <= 500, 'Нужна ссылка на подтверждение.')
    require(re.fullmatch(r'sha256:[0-9a-f]{64}', str(proof.get('artifact_digest', ''))), 'Нужен отпечаток исходного подтверждения.')
    try:
        artifact = base64.b64decode(proof.get('artifact_base64', ''), validate=True)
    except (ValueError, TypeError) as exc:
        raise AdapterError('Не удалось прочитать приложенное подтверждение.') from exc
    require(0 < len(artifact) <= 300_000 and 'sha256:' + hashlib.sha256(artifact).hexdigest() == proof['artifact_digest'], 'Приложение не совпадает с отпечатком подтверждения.')
    kind = proof.get('kind')
    if kind == 'wb_native_bid_history':
        row = proof.get('row', {})
        # Validate the selected row against the unchanged native workbook bytes.
        from openpyxl import load_workbook
        try:
            with zipfile.ZipFile(BytesIO(artifact)) as archive:
                require(sum(x.file_size for x in archive.infolist()) <= 8_000_000 and len(archive.infolist()) <= 100, 'Выгрузка истории слишком большая.')
            workbook = load_workbook(BytesIO(artifact), read_only=True, data_only=True)
            found = False
            for sheet in workbook:
                values = sheet.iter_rows(values_only=True)
                header = next(values, ())
                if not set(row).issubset(set(header)):
                    continue
                for index, value in enumerate(values):
                    require(index < 20000, 'Выгрузка истории слишком большая.')
                    source = dict(zip(header, value))
                    if all(str(source.get(k)) == str(v) for k, v in row.items()):
                        found = True
                        break
            workbook.close()
            require(found, 'Указанная строка отсутствует в приложенной истории WB.')
        except (zipfile.BadZipFile, KeyError, OSError) as exc:
            raise AdapterError('Не удалось прочитать историю WB.') from exc
        require(fact['target_kind'] == 'bid' and fact['parameter_field'] == 'bid_minor', 'История ставок не подтверждает цену.')
        require(str(row.get('ID кампании')) == str(fact['advert_id']) and str(row.get('ID товара')) == str(fact['nm_id']), 'История WB относится к другому товару или кампании.')
        require(row.get('Источник') == 'Личный кабинет', 'Источник истории WB должен быть личным кабинетом.')
        names = {'search': {'Ставка в поиске'}, 'recommendations': {'Ставка в рекомендациях', 'Единая ставка'}, 'combined': {'Единая ставка'}}
        require(row.get('Что изменилось') in names.get(fact['placement'], set()) and str(row.get('Кластер', '-')) == '-', 'Не совпадает размещение ставки.')
        for key, field in [('Было', 'before_value_integer'), ('Стало', 'after_value_integer')]:
            amount = Decimal(str(row.get(key)).replace(',', '.')) * 100
            native_creation = first_step and key == 'Было' and amount == 0 and row.get('Изменение') == 'Установлена ставка'
            require(amount.is_finite() and (amount == step[field] or native_creation), 'Значение не совпадает с историей WB.')
            if native_creation and amount != step[field]:
                step['before_value_source'] = 'canonical_observer_fact'
                step['native_before_value_integer'] = 0
                step['evidence_note'] = 'WB записал установку ставки с нуля; прежнее фактическое значение подтверждено наблюдателем отдельно.'
        native = utc(str(row.get('Дата и время (GMT +3)', '')).replace(' ', 'T') + '+03:00')
        require(step['observed_from'] == step['observed_to'] == native, 'Время не совпадает с историей WB.')
        return 'exact_second', 'native_audit'
    require(kind == 'portal_submit_readback', 'Неизвестный вид подтверждения.')
    require(step['observed_from'] < step['observed_to'], 'Подтверждение отправки и чтения задаёт интервал, а не точную секунду.')
    receipt = proof.get('receipt', {})
    try:
        evidence_document = json.loads(artifact)
    except (ValueError, UnicodeError) as exc:
        raise AdapterError('Не удалось прочитать журнал подтверждений.') from exc
    require(isinstance(evidence_document, dict) and receipt in evidence_document.get('receipts', []), 'Подтверждение отсутствует в приложенном журнале.')
    require(utc(receipt.get('submitted_from')) == step['observed_from'] and utc(receipt.get('verified_at')) == step['observed_to'], 'Интервал не совпадает с журналом.')
    require(receipt.get('nm_id') == fact['nm_id'] and receipt.get('advert_id', 0) == fact['advert_id'] and receipt.get('placement', '') == fact['placement'] and receipt.get('parameter_field') == fact['parameter_field'], 'Подтверждение относится к другому параметру.')
    require(receipt.get('before_value_integer') == step['before_value_integer'] and receipt.get('after_value_integer') == step['after_value_integer'], 'Подтверждение содержит другие значения.')
    return 'bounded_interval', 'wb_readback'


def normalize(record, fact):
    require(fact['target_kind'] in {'price', 'bid'} and fact['before_value_kind'] == fact['after_value_kind'] == 'integer', 'Поддерживаются подтверждённые числовые цены и ставки.')
    require(record.get('source') == 'wb_portal', 'Нужно явно указать источник wb_portal.')
    steps = record.get('steps')
    require(isinstance(steps, list) and 1 <= len(steps) <= 16, 'Нужна цепочка из 1–16 изменений.')
    normalized = []
    previous_value = fact['before_value_integer']
    previous_time = utc(fact['observed_from'])
    for raw in steps:
        step = {k: raw.get(k) for k in ('before_value_integer', 'after_value_integer', 'evidence')}
        require(type(step['before_value_integer']) is int and type(step['after_value_integer']) is int and 0 <= step['after_value_integer'] < 2**63, 'Значения задаются целым числом в единицах реестра.')
        if fact['parameter_field'] == 'discount_bps':
            require(step['after_value_integer'] <= 10000, 'Скидка не может превышать 100%.')
        require(step['before_value_integer'] == previous_value and step['after_value_integer'] != previous_value, 'Цепочка значений разорвана или содержит пустое изменение.')
        step['observed_from'] = utc(raw.get('observed_from'))
        step['observed_to'] = utc(raw.get('observed_to'))
        require(previous_time <= step['observed_from'] <= step['observed_to'] <= utc(fact['observed_to']), 'Время выходит за подтверждённое окно или шаги перекрываются.')
        step['time_precision'], step['proof_kind'] = _proof(step, fact, first_step=not normalized)
        normalized.append(step)
        previous_time, previous_value = step['observed_to'], step['after_value_integer']
    require(previous_value == fact['after_value_integer'], 'Итог цепочки не совпадает с подтверждённым WB значением.')
    reason = str(record.get('reason', '')).strip()
    require(0 < len(reason) <= 1000, 'Нужна причина изменения (до 1000 знаков).')
    return {'fact_id': fact['fact_id'], 'source': 'wb_portal', 'reason': reason, 'steps': normalized}


def decorate_facts(conn, facts):
    """Keep raw observations; expose additive registration and effective changes."""
    result = []
    for raw in facts:
        fact = dict(raw)
        registration = latest(conn, fact['fact_id'])
        if registration:
            fact['external_registration'] = {**json.loads(registration['payload_json']),
                'evidence_id': registration['evidence_id'], 'payload_digest': registration['payload_digest'],
                'registered_at': registration['registered_at'], 'registered_by': registration['actor'],
                'revision': registration['revision']}
        result.append(fact)
    return result


def effective_changes(facts):
    """Same atomic field/value contract; each net fact replaced by its steps once."""
    result = []
    for fact in facts:
        registration = fact.get('external_registration')
        if not registration:
            result.append({**fact, 'time_precision': 'observation_window'})
            continue
        for i, step in enumerate(registration['steps']):
            result.append({**fact, **{k: v for k, v in step.items() if k != 'evidence'},
                'change_id': registration['evidence_id'] + ':' + str(i + 1),
                'source': 'wb_portal', 'source_label': 'Портал WB',
                'observation_window': {'from': fact['observed_from'], 'to': fact['observed_to']},
                'native_evidence': step['evidence'], 'reason': registration['reason']})
    return result


class ExternalRegistryAdapter:
    """Single SQLite transaction with CAS; reusable safe-launcher adapter."""
    def context(self, request):
        require(request.get('hostname') == socket.gethostname(), 'Не совпадает целевой сервер.')
        root = Path(request['runtime_dir']).resolve()
        require(root.is_dir(), 'Рабочая папка не существует.')
        for field in ('seller_id', 'account_scope', 'actor'):
            require(isinstance(request.get(field), str) and 0 < len(request[field]) <= 160, 'Не задан владелец или автор регистрации.')
        require(request['account_scope'] == 'seller-portal-primary', 'Неизвестная область аккаунта.')
        require(len(canonical_json(request).encode()) <= 2_000_000, 'Пакет подтверждений слишком большой.')
        return StoreRegistry(root)

    def candidate(self, conn, request):
        records = request.get('records')
        require(isinstance(records, list) and 1 <= len(records) <= 200, 'Пакет должен содержать 1–200 записей.')
        candidates, prestate, seen = [], [], set()
        for record in records:
            fid = str(record.get('fact_id', ''))
            require(fid not in seen, 'Одна запись реестра указана дважды.')
            seen.add(fid)
            row = conn.execute('SELECT * FROM change_registry_facts WHERE fact_id=? AND seller_id=? AND account_scope=?', (fid, request['seller_id'], request['account_scope'])).fetchone()
            require(row is not None, 'Изменение ещё не подтверждено наблюдателем или принадлежит другому аккаунту. Сначала выполните проверку реестра.')
            fact = dict(row)
            require(not conn.execute("SELECT 1 FROM change_registry_fact_links WHERE fact_id=? AND link_kind='change_item'", (fid,)).fetchone(), 'Событие уже связано с операцией сервиса; нельзя заменить его источник порталом.')
            # Do not refine across a lost/ambiguous intermediate observation. Such
            # histories need reconciliation before their timeline can be changed.
            gaps = conn.execute('''SELECT cp.checkpoint_id FROM change_registry_checkpoints cp
                LEFT JOIN change_registry_observation_values ob ON ob.checkpoint_id=cp.checkpoint_id
                AND ob.target_kind=? AND ob.nm_id=? AND ob.advert_id=? AND ob.placement=? AND ob.parameter_field=?
                WHERE cp.seller_id=? AND cp.account_scope=? AND cp.completeness_status='complete'
                AND cp.source_surface='wb_prices_ads_joint'
                AND julianday(cp.completed_at)>julianday(?) AND julianday(cp.completed_at)<julianday(?)
                AND (ob.observation_value_id IS NULL OR ob.observation_status NOT IN ('exact','exact_zero') OR ob.value_kind<>'integer')''',
                (fact['target_kind'], fact['nm_id'], fact['advert_id'], fact['placement'], fact['parameter_field'], request['seller_id'], request['account_scope'], fact['observed_from'], fact['observed_to'])).fetchall()
            require(not gaps, 'Внутри окна есть пропуск наблюдений. Сначала требуется сверка истории; время не уточнено.')
            payload = normalize(record, fact)
            digest = canonical_digest(payload)
            prior = latest(conn, fid)
            duplicate = bool(prior and prior['payload_digest'] == digest)
            require(not prior or duplicate or record.get('supersedes') == prior['payload_digest'], 'У записи уже есть другое подтверждение. Для уточнения нужен отпечаток предыдущей версии.')
            candidates.append({'payload': payload, 'digest': digest, 'duplicate': duplicate, 'revision': prior['revision'] + 1 if prior and not duplicate else prior['revision'] if prior else 1})
            prestate.append({'fact': fact, 'previous_evidence': prior['payload_digest'] if prior else None})
        candidates.sort(key=lambda x: x['payload']['fact_id'])
        prestate.sort(key=lambda x: x['fact']['fact_id'])
        return candidates, canonical_digest(prestate)

    def preview(self, request, operation_id):
        store = self.context(request)
        with store.session('operational', mode='ro', operation='external_registry_preview') as conn:
            candidates, state = self.candidate(conn, request)
        # Candidate identity excludes duplicate/revision: repeated evidence has stable bytes.
        digest = canonical_digest({'seller_id': request['seller_id'], 'account_scope': request['account_scope'], 'actor': request['actor'], 'records': [x['payload'] for x in candidates]})
        return {'operation_id': operation_id, 'target': request['hostname'] + ':' + str(store.resolve('operational')),
            'scope': {'seller_id': request['seller_id'], 'account_scope': request['account_scope'], 'fact_ids': [x['payload']['fact_id'] for x in candidates]},
            'prestate_sha256': state, 'candidate_sha256': digest,
            'recovery': {'method': 'readback_same_operation', 'operation_id': operation_id, 'wb_calls': 0, 'immutable_original_facts': True},
            'summary': {'facts': len(candidates), 'new_or_refined': sum(not x['duplicate'] for x in candidates), 'duplicates': sum(x['duplicate'] for x in candidates),
                'exact_steps': sum(s['time_precision'] == 'exact_second' for x in candidates for s in x['payload']['steps']),
                'interval_steps': sum(s['time_precision'] != 'exact_second' for x in candidates for s in x['payload']['steps'])}}

    def apply(self, request, operation_id, preview):
        store = self.context(request)
        with store.session('operational', mode='rw', operation='external_registry_apply') as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                candidates, state = self.candidate(conn, request)
                require(state == preview['prestate_sha256'], 'Подтверждения изменились после предварительной сверки.')
                digest = canonical_digest({'seller_id': request['seller_id'], 'account_scope': request['account_scope'], 'actor': request['actor'], 'records': [x['payload'] for x in candidates]})
                require(digest == preview['candidate_sha256'], 'Изменился пакет подтверждений.')
                migrate(conn)
                prior_batch = conn.execute(f'SELECT * FROM {BATCHES} WHERE operation_id=?', (operation_id,)).fetchone()
                if prior_batch:
                    require(prior_batch['candidate_digest'] == digest, 'Номер операции уже занят другим пакетом.')
                    conn.rollback()
                    return {'operation_id': operation_id, 'disposition': 'already_applied'}
                now = utc(datetime.now(timezone.utc).isoformat())
                ids = ['crex_' + canonical_digest([x['payload']['fact_id'], x['digest']])[7:39] for x in candidates]
                receipt = {'operation_id': operation_id, 'state': 'applied', 'candidate_sha256': digest, 'evidence_ids': ids, 'facts': len(candidates), 'created': sum(not x['duplicate'] for x in candidates), 'wb_calls': 0, 'registered_at': now}
                conn.execute(f'INSERT INTO {BATCHES} VALUES(?,?,?,?,?,?)', (operation_id, request['seller_id'], request['account_scope'], digest, canonical_json(receipt), now))
                for item, evidence_id in zip(candidates, ids):
                    if item['duplicate']:
                        continue
                    conn.execute(f'INSERT INTO {EVIDENCE} VALUES(?,?,?,?,?,?,?,?)', (evidence_id, item['payload']['fact_id'], item['revision'], item['digest'], canonical_json(item['payload']), request['actor'], now, operation_id))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return {'operation_id': operation_id, 'disposition': 'submitted'}

    def readback(self, request, operation_id):
        store = self.context(request)
        with store.session('operational', mode='ro', operation='external_registry_readback') as conn:
            if not exists(conn, BATCHES):
                return {'operation_id': operation_id, 'state': 'not_submitted'}
            row = conn.execute(f'SELECT * FROM {BATCHES} WHERE operation_id=?', (operation_id,)).fetchone()
            if not row:
                return {'operation_id': operation_id, 'state': 'not_submitted'}
            require(row['seller_id'] == request['seller_id'] and row['account_scope'] == request['account_scope'], 'Операция принадлежит другому аккаунту.')
            records = request.get('records')
            require(isinstance(records, list) and 1 <= len(records) <= 200, 'Некорректный пакет подтверждений.')
            payloads = []
            for record in records:
                fact = conn.execute('SELECT * FROM change_registry_facts WHERE fact_id=? AND seller_id=? AND account_scope=?', (record.get('fact_id'), request['seller_id'], request['account_scope'])).fetchone()
                require(fact is not None, 'Не найдена исходная запись операции.')
                payloads.append(normalize(record, dict(fact)))
            payloads.sort(key=lambda x: x['fact_id'])
            digest = canonical_digest({'seller_id': request['seller_id'], 'account_scope': request['account_scope'], 'actor': request['actor'], 'records': payloads})
            require(digest == row['candidate_digest'], 'Номер операции уже занят другим пакетом.')
            receipt = json.loads(row['receipt_json'])
            for evidence_id in receipt['evidence_ids']:
                require(conn.execute(f'SELECT 1 FROM {EVIDENCE} WHERE evidence_id=?', (evidence_id,)).fetchone(), 'Подтверждение операции неполно.')
            return receipt
