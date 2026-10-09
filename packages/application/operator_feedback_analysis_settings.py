"""Native prompt-file receipts. No analysis, provider call or writable GET."""
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from uuid import uuid4

DOMAIN = 'feedback_analysis_settings'
LABEL = 'Инструкция и модель разбора отзывов'
PATH = '/v1/sheet-vitrina-v1/feedbacks/ai-prompt'
FILENAME = 'sheet_vitrina_v1_feedbacks_ai_prompt.json'
LEDGER = 'operator_source_receipts'
GENERATION = 'operator_source_generation'
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_LEDGER_BYTES = 16 * 1024 * 1024
MAX_RECEIPTS = 512
IDENTITY = re.compile(r'analysis-settings:[a-zA-Z0-9_-]{16,80}\Z')


class SourceRejected(ValueError):
    """Only emitted before atomic replacement; safe negative acknowledgment."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def safe_path(path):
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError('analysis_settings_source_path_unsafe')


def raw(path):
    """Atomic native-file snapshot. No mkdir, lock creation, provider or owner init."""
    safe_path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    with os.fdopen(fd, 'rb') as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError('analysis_settings_source_size_exceeded')
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError('analysis_settings_source_shape_invalid')
    return value


class NativePromptLock:
    """Existing native write critical section, shared across threads/processes."""
    def __init__(self, path):
        self.path = path
        self.thread = threading.RLock()
        self.local = threading.local()

    def __enter__(self):
        self.thread.acquire()
        try:
            depth = getattr(self.local, 'depth', 0)
            if not depth:
                safe_path(self.path)
                self.path.parent.mkdir(parents=True, exist_ok=True)
                lock = self.path.with_suffix(self.path.suffix + '.lock')
                self.local.fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                fcntl.flock(self.local.fd, fcntl.LOCK_EX)
            self.local.depth = depth + 1
            return self
        except BaseException:
            if hasattr(self.local, 'fd'):
                os.close(self.local.fd)
                del self.local.fd
            self.thread.release()
            raise

    def __exit__(self, *exc):
        self.local.depth -= 1
        if not self.local.depth:
            fcntl.flock(self.local.fd, fcntl.LOCK_UN)
            os.close(self.local.fd)
            del self.local.fd
        self.thread.release()


def material(value):
    # Discovery catalog is presentation metadata, not a prompt operand. Native
    # generation changes on EVERY source save, including legacy writes (ABA).
    return dict(prompt=str(value.get('prompt') or ''), model=str(value.get('model') or ''),
                updated_at=value.get('updated_at'), **{GENERATION: value.get(GENERATION, 'legacy')})


def revision(value):
    return digest(material(value))


def command(payload, *, actor, account, account_scope):
    if set(payload) - {'operation_id', 'expected_source_revision', 'prompt', 'model'}:
        raise SourceRejected('analysis_settings_command_invalid')
    identity = payload.get('operation_id')
    expected = payload.get('expected_source_revision')
    if not isinstance(identity, str) or not IDENTITY.fullmatch(identity):
        raise SourceRejected('analysis_settings_identity_invalid')
    if not isinstance(expected, str) or not re.fullmatch('[a-f0-9]{64}', expected):
        raise SourceRejected('analysis_settings_revision_required')
    if not actor or not account or not account_scope:
        raise SourceRejected('analysis_settings_native_scope_required')
    if not isinstance(payload.get('prompt'), str) or not isinstance(payload.get('model'), str):
        raise SourceRejected('analysis_settings_command_invalid')
    if not payload['prompt'].strip() or len(payload['prompt'].strip()) > 16000 or not payload['model'].strip():
        raise SourceRejected('analysis_settings_command_invalid')
    request = {key: value for key, value in payload.items() if key != 'operation_id'}
    if len(encoded(request)) > 80 * 1024:
        raise SourceRejected('analysis_settings_command_size_exceeded')
    return dict(operation_id=identity, domain=DOMAIN, actor=actor, account=account,
                account_scope=account_scope, request=request, request_digest=digest(request))


def records(value):
    rows = value.get(LEDGER, [])
    if not isinstance(rows, list) or len(rows) > MAX_RECEIPTS or len(encoded(rows)) > MAX_LEDGER_BYTES:
        raise ValueError('analysis_settings_retained_proof_invalid')
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('analysis_settings_retained_proof_invalid')
        body = {key: item for key, item in row.items() if key != 'proof_digest'}
        if row.get('proof_digest') != digest(body):
            raise ValueError('analysis_settings_retained_proof_invalid')
        cmd = row.get('command', {})
        rebuilt = command(dict(cmd.get('request', {}), operation_id=cmd.get('operation_id')),
                          actor=cmd.get('actor'), account=cmd.get('account'), account_scope=cmd.get('account_scope'))
        if cmd != rebuilt or cmd['operation_id'] in seen:
            raise ValueError('analysis_settings_retained_proof_invalid')
        before, after = row.get('before'), row.get('after')
        if not isinstance(before, dict) or not isinstance(after, dict) or row.get('before_revision') != revision(before) or row.get('after_revision') != revision(after):
            raise ValueError('analysis_settings_retained_proof_invalid')
        if cmd['request']['expected_source_revision'] != revision(before) or after.get('prompt') != cmd['request']['prompt'].strip() or after.get('model') != cmd['request']['model'].strip():
            raise ValueError('analysis_settings_retained_proof_invalid')
        if not after.get(GENERATION) or after.get(GENERATION) == before.get(GENERATION) or not row.get('accepted_at') or row['accepted_at'] != after.get('updated_at'):
            raise ValueError('analysis_settings_retained_proof_invalid')
        seen.add(cmd['operation_id'])
    return rows


def retained(value, cmd):
    for row in records(value):
        if row['command']['operation_id'] == cmd['operation_id']:
            if row['command'] != cmd:
                raise SourceRejected('analysis_settings_identity_conflict')
            return row
    return None


def prepare(previous, new, cmd=None):
    rows = records(previous)
    updated = dict(new, **{GENERATION: uuid4().hex, LEDGER: rows})
    if cmd:
        if revision(previous) != cmd['request']['expected_source_revision']:
            raise SourceRejected('analysis_settings_revision_stale')
        record = dict(command=cmd, accepted_at=updated['updated_at'], before=material(previous),
                      after=material(updated), before_revision=revision(previous), after_revision=revision(updated))
        record['after'][GENERATION] = updated[GENERATION]
        record['proof_digest'] = digest(record)
        updated[LEDGER] = [*rows, record]
        if len(updated[LEDGER]) > MAX_RECEIPTS or len(encoded(updated[LEDGER])) > MAX_LEDGER_BYTES:
            raise SourceRejected('analysis_settings_capacity_exceeded')
        records(updated)  # Independent normalized operand/proof validation before commit.
    if len(encoded(updated)) > MAX_FILE_BYTES:
        raise SourceRejected('analysis_settings_capacity_exceeded')
    return updated


def atomic_write(path, payload):
    """Single fsynced native source+receipt commit; no sidecar receipt transaction."""
    safe_path(path)
    data = encoded(payload) + b'\n'
    if len(data) > MAX_FILE_BYTES:
        raise SourceRejected('analysis_settings_capacity_exceeded')
    temp = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


def public(row):
    cmd = row['command']
    return dict(contract_name='operator_operations_v1', operation_id=cmd['operation_id'], domain=DOMAIN,
                title_ru=LABEL, actor=cmd['actor'], accepted_at=row['accepted_at'], state='completed',
                native_state='source_saved',
                durable_saved=True, primary_effect='source_saved', execution_required=False,
                external_confirmed=False, calculation_completed=False, resubmit_allowed=False,
                reason_ru='Инструкция и модель сохранены. Разбор отзывов запускается отдельно.',
                request=cmd['request'], saved_source=row['after'],
                source_ref=dict(entity_id=cmd['operation_id'], filename=FILENAME, account=cmd['account'],
                                account_scope=cmd['account_scope'], before_revision=row['before_revision'],
                                after_revision=row['after_revision'], proof_digest=row['proof_digest']), fields=[])


@dataclass(frozen=True)
class PromptScope:
    runtime_dir: Path
    actor: str
    account: str
    account_scope: str = 'default'

    @classmethod
    def from_entrypoint(cls, app, *, actor):
        runtime = Path(app.runtime.runtime_dir).resolve()
        owner = app.feedbacks_ai_block.prompt_store
        surface = app.change_registry_read_surface
        if surface is None:
            raise SourceRejected('analysis_settings_native_scope_required')
        if owner.path.absolute() != (runtime / FILENAME).absolute() or Path(surface.runtime_dir).resolve() != runtime.resolve():
            raise ValueError('analysis_settings_native_source_binding_invalid')
        return cls(runtime, actor, str(surface.seller_id or ''), str(surface.account_scope or 'default'))


def items(*, selected, scope):
    # Domain/native grants are resolved BEFORE even reading the owner file.
    if DOMAIN not in selected or not isinstance(scope, PromptScope) or not scope.actor or not scope.account:
        return []
    value = raw(scope.runtime_dir.resolve() / FILENAME)
    rows = value.get(LEDGER, [])
    if not isinstance(rows, list):
        raise ValueError('analysis_settings_retained_proof_invalid')
    # Foreign entries do not participate in proof diagnostics, search or
    # pagination. The native writer separately verifies the entire ledger.
    own = [row for row in rows if isinstance(row, dict) and isinstance(row.get('command'), dict)
           and row['command'].get('actor') == scope.actor and row['command'].get('account') == scope.account
           and row['command'].get('account_scope') == scope.account_scope]
    return [public(row) for row in records({LEDGER: own})]
