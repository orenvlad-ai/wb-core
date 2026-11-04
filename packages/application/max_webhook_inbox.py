"""Durable, append-only MAX source inbox. No business processing or API calls."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Mapping

UPDATE_TYPES = frozenset({"message_created", "message_edited", "message_removed"})
MAX_BODY_BYTES = 1024 * 1024
DB_FILENAME = "max_inbox.sqlite3"


@dataclass(frozen=True)
class MaxWebhookConfig:
    secret: str = field(repr=False)
    bot_id: int
    chat_ids: frozenset[int]
    inbox_dir: Path


def load_max_webhook_config(
    *, business_runtime_dir: Path, environ: Mapping[str, str] | None = None,
) -> MaxWebhookConfig | None:
    """No configuration means no listener; reject partial/invalid opt-in."""
    env = os.environ if environ is None else environ
    enabled = env.get("WB_CORE_MAX_WEBHOOK_ENABLED", "0")
    if enabled == "0":
        return None
    if enabled != "1":
        raise ValueError("MAX webhook enabled flag must be 0 or 1")
    secret = env.get("WB_CORE_MAX_WEBHOOK_SECRET", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", secret):
        raise ValueError("MAX webhook requires a 32–256 character secret")
    raw_bot_id = env.get("WB_CORE_MAX_BOT_ID", "")
    if not re.fullmatch(r"[1-9][0-9]{0,18}", raw_bot_id) or int(raw_bot_id) > 2**63 - 1:
        raise ValueError("MAX webhook requires an explicit bot ID")
    raw_ids = env.get("WB_CORE_MAX_WEBHOOK_CHAT_IDS", "")
    parts = raw_ids.split(",")
    if not all(re.fullmatch(r"-[1-9][0-9]*", item.strip()) for item in parts):
        raise ValueError("MAX webhook requires explicit authorized group IDs")
    chat_ids = frozenset(int(item.strip()) for item in parts)
    if not chat_ids or len(chat_ids) > 100 or any(chat_id < -(2**63) for chat_id in chat_ids):
        raise ValueError("MAX webhook group allowlist is invalid")
    raw_dir = env.get("WB_CORE_MAX_INBOX_DIR", "")
    inbox_dir = Path(raw_dir)
    if not raw_dir or not inbox_dir.is_absolute():
        raise ValueError("MAX inbox requires an absolute dedicated directory")
    inbox_dir = inbox_dir.resolve()
    business_dir = business_runtime_dir.resolve()
    if inbox_dir == business_dir or business_dir in inbox_dir.parents:
        raise ValueError("MAX inbox must be outside the business runtime directory")
    return MaxWebhookConfig(secret=secret, bot_id=int(raw_bot_id), chat_ids=chat_ids, inbox_dir=inbox_dir)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("non-finite JSON value")


@dataclass(frozen=True)
class InboxEvent:
    update_type: str
    chat_id: int
    message_id: str
    event_timestamp: int
    canonical_payload: bytes = field(repr=False)
    raw_payload: bytes = field(repr=False)


def parse_inbox_event(raw: bytes, chat_ids: frozenset[int]) -> InboxEvent | None:
    """Return None for deliberately ignored event types or foreign/private chats."""
    payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                         parse_constant=_invalid_constant)
    if not isinstance(payload, dict):
        raise ValueError("Update must be an object")
    update_type = payload.get("update_type")
    if not isinstance(update_type, str):
        raise ValueError("Update type is required")
    if update_type not in UPDATE_TYPES:
        return None
    if update_type == "message_removed":
        chat_id = payload.get("chat_id")
        message_id = payload.get("message_id")
    else:
        message = payload.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("recipient"), dict):
            raise ValueError("Update message recipient is required")
        recipient = message["recipient"]
        # Inspect only the current recipient, never forwarded/replied-to chat IDs.
        if recipient.get("chat_type") != "chat":
            return None
        chat_id = recipient.get("chat_id")
        body = message.get("body")
        message_id = body.get("mid") if isinstance(body, dict) else None
    if type(chat_id) is not int:
        raise ValueError("Update chat ID must be an integer")
    if chat_id not in chat_ids:
        return None
    timestamp = payload.get("timestamp")
    if type(timestamp) is not int or not 0 <= timestamp <= 2**63 - 1:
        raise ValueError("Update timestamp must be an int64")
    if not isinstance(message_id, str) or not 1 <= len(message_id) <= 512:
        raise ValueError("Update message ID is required")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False).encode("utf-8")
    return InboxEvent(update_type, chat_id, message_id, timestamp, canonical, raw)


def check_inbox_storage(config: MaxWebhookConfig) -> Path:
    """Read-only readiness checks. They never bootstrap or open a business DB."""
    directory = config.inbox_dir
    directory_stat = directory.stat()
    if (not stat.S_ISDIR(directory_stat.st_mode) or directory_stat.st_mode & 0o077
            or directory_stat.st_uid != os.geteuid() or not os.access(directory, os.W_OK | os.X_OK)):
        raise OSError("MAX inbox directory must be private and writable by its owner")
    db_path = directory / DB_FILENAME
    if os.path.lexists(db_path):
        db_stat = db_path.lstat()
        if (not stat.S_ISREG(db_stat.st_mode) or db_stat.st_mode & 0o077
                or db_stat.st_uid != os.geteuid() or not os.access(db_path, os.R_OK | os.W_OK)):
            raise OSError("MAX inbox database must be a private writable regular file")
    return db_path


def persist_inbox_event(config: MaxWebhookConfig, event: InboxEvent) -> None:
    """Commit before ACK; any failure propagates to a retryable HTTP response.

    MAX supplies no global update ID. Canonical complete-Update hash deduplicates
    exact retries (including changed JSON formatting), while keeping event types,
    timestamps and different edit payloads separate. First raw receipt is retained.
    """
    db_path = check_inbox_storage(config)
    try:
        fd = os.open(db_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        check_inbox_storage(config)
    else:
        os.close(fd)
    connection = sqlite3.connect(str(db_path), timeout=2.0)
    try:
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA journal_mode=DELETE")
        with connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS max_inbox_events (
                event_key TEXT PRIMARY KEY,
                bot_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                update_type TEXT NOT NULL,
                message_id TEXT NOT NULL,
                event_timestamp_ms INTEGER NOT NULL,
                received_at TEXT NOT NULL,
                raw_payload BLOB NOT NULL
            )""")
            connection.execute("""INSERT INTO max_inbox_events (
                event_key, bot_id, chat_id, update_type, message_id,
                event_timestamp_ms, received_at, raw_payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_key) DO NOTHING""", (
                hashlib.sha256(str(config.bot_id).encode("ascii") + b":" + event.canonical_payload).hexdigest(), config.bot_id,
                event.chat_id, event.update_type, event.message_id,
                event.event_timestamp, datetime.now(timezone.utc).isoformat(),
                event.raw_payload,
            ))
    finally:
        connection.close()
