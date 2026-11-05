"""Opt-in, source-bound MAX and TEST cash projections; never bootstrap or write."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
from typing import Any, Iterator, Mapping
from urllib.parse import quote

from packages.adapters.finance_liquidity_access import (
    FinanceBootstrapAccess,
    load_finance_bootstrap_access,
    validate_finance_bootstrap_store,
)
from packages.contracts.finance_liquidity_cash import FINANCE_CASH_SCHEMA_VERSION

SCOPE_MAX_MESSAGE_SEND = "webcore.max_messages.write"
SCOPE_MAX_POSTED_REPLY = "webcore.max_posted_reply.write"
SCOPE_MAX_INBOX_READ = "webcore.max_inbox.read"
SCOPE_CASH_READ = "webcore.cash.read"
SCOPE_CASH_OPERATIONS_READ = "webcore.cash.operations.read"
SCOPE_MAX_HISTORY_READ = "webcore.max_history.read"
SCOPE_TRANSIT_SEND = "webcore.max_transit.write"
SCOPE_ACCOUNTING_READ = "webcore.max_accounting.read"
SCOPE_ACCOUNTING_SEND = "webcore.max_accounting.write"
ACCOUNTING_GROUP_ID = -78110411849813
ACCOUNTING_READ_TOOLS = {"accounting_messages": "max_messages", "accounting_message": "max_message"}
SCOPE_TRANSIT_READ = "webcore.max_transit.read"
TRANSIT_GROUP_ID = -73424916294290
TRANSIT_READ_TOOLS = {"transit_messages": "max_messages", "transit_message": "max_message"}
CLOUD_READ_SCOPES = frozenset({SCOPE_MAX_INBOX_READ, SCOPE_CASH_READ, SCOPE_CASH_OPERATIONS_READ, SCOPE_MAX_HISTORY_READ, SCOPE_TRANSIT_READ, SCOPE_ACCOUNTING_READ})
CLOUD_OAUTH_SCOPES = CLOUD_READ_SCOPES | {SCOPE_MAX_POSTED_REPLY, SCOPE_MAX_MESSAGE_SEND, SCOPE_TRANSIT_SEND, SCOPE_ACCOUNTING_SEND}
CLOUD_READ_TOOLS = ("max_inbox_events", "max_inbox_event", "cash_accounts", "cash_read", "max_messages", "max_message", "max_ack_status", *TRANSIT_READ_TOOLS, *ACCOUNTING_READ_TOOLS)
CLOUD_WRITE_TOOLS = ("max_posted_reply", "max_send_message", "transit_send_message", "accounting_send_message")
CLOUD_TOOLS = (*CLOUD_READ_TOOLS, *CLOUD_WRITE_TOOLS)
MAX_PAGE_SIZE = 100
MAX_MESSAGE_TEXT_CHARS = 16_000
MESSAGE_TEXT_CHUNK_CHARS = 500


class CloudReadError(ValueError):
    def __init__(self, message: str, code: str = "source_unavailable") -> None:
        super().__init__(message)
        self.code = code


def _fixed_path(path: Path) -> Path:
    # URI, relative paths and aliases never become deployment source bindings.
    if (not path.is_absolute() or ".." in path.parts or "\x00" in str(path)
            or path.resolve() != path):
        raise ValueError("Cloud read source requires a canonical absolute file path")
    return path


def _enabled(env: Mapping[str, str], name: str) -> bool:
    value = env.get(name, "0")
    if value not in {"0", "1"}:
        raise ValueError(f"{name} must be 0 or 1")
    return value == "1"


@dataclass(frozen=True)
class MaxInboxReadConfig:
    db_path: Path
    chat_ids: frozenset[int]

    def __post_init__(self) -> None:
        _fixed_path(self.db_path)
        if self.db_path.name != "max_inbox.sqlite3":
            raise ValueError("MAX read source must be the dedicated inbox database")
        if (not self.chat_ids or len(self.chat_ids) > 100
                or any(type(item) is not int or not -(2**63) <= item < 0 for item in self.chat_ids)):
            raise ValueError("MAX read requires an explicit group allowlist")


@dataclass(frozen=True)
class CashReadConfig:
    access_config_path: Path
    binding: FinanceBootstrapAccess

    def __post_init__(self) -> None:
        _fixed_path(self.access_config_path)
        _fixed_path(self.binding.store_path)
        if self.binding.mode != "isolated_test":
            raise ValueError("Cloud cash reads currently require an isolated TEST source")


class WebCoreCloudReads:
    def __init__(self, *, max_inbox: MaxInboxReadConfig | None = None,
                 cash: CashReadConfig | None = None, financial_read: bool = False,
                 max_api: Any = None, max_reply: Any = None, max_send: Any = None, max_ack_queue: Any = None, transit_api: Any = None,
                 transit_send: Any = None, accounting_api: Any = None, accounting_send: Any = None) -> None:
        self.max_inbox = max_inbox
        self.cash = cash
        self.financial_read = financial_read
        self.accounting_api = accounting_api
        self.accounting_send = accounting_send
        self.transit_send = transit_send
        self.transit_api = transit_api
        self.max_api = max_api
        self.max_reply = max_reply
        self.max_send = max_send
        self.max_ack_queue = max_ack_queue

    @classmethod
    def from_env(cls, environment: Mapping[str, str] | None = None) -> WebCoreCloudReads:
        env = os.environ if environment is None else environment
        max_config = None
        cash_config = None
        if _enabled(env, "WEBCORE_DATA_MCP_MAX_INBOX_READ_ENABLED"):
            raw_ids = env.get("WEBCORE_DATA_MCP_MAX_INBOX_CHAT_IDS", "")
            parts = raw_ids.split(",")
            if not all(re.fullmatch(r"-[1-9][0-9]*", part.strip()) for part in parts):
                raise ValueError("MAX read requires its own explicit group allowlist")
            max_config = MaxInboxReadConfig(
                Path(env.get("WEBCORE_DATA_MCP_MAX_INBOX_DB_PATH", "")),
                frozenset(int(part.strip()) for part in parts),
            )
        if _enabled(env, "WEBCORE_DATA_MCP_CASH_READ_ENABLED"):
            binding = load_finance_bootstrap_access(env)
            if binding is None:
                raise ValueError("Cash read requires the existing explicit TEST store binding")
            cash_config = CashReadConfig(Path(env["FINANCE_LIQUIDITY_ACCESS_CONFIG"]), binding)
        from packages.application.webcore_cloud_extended_reads import MaxHistoryReader
        from packages.application.webcore_max_posted_reply import MaxPostedReply
        from packages.application.webcore_max_send_message import MaxSendMessage
        api = MaxHistoryReader.from_env(env, max_config)
        def group_access(prefix, group_id):
            reader = sender = None
            if _enabled(env, f"WEBCORE_DATA_MCP_{prefix}_HISTORY_ENABLED"):
                reader = MaxHistoryReader(env.get("WEBCORE_DATA_MCP_MAX_TOKEN_PATH", ""),
                                          env.get("WEBCORE_DATA_MCP_MAX_CA_PATH"), frozenset({group_id}))
            if _enabled(env, f"WEBCORE_DATA_MCP_{prefix}_SEND_ENABLED"):
                if reader is None:
                    raise ValueError(f"{prefix} sending requires configured history")
                sender = MaxSendMessage(reader, env.get("WEBCORE_DATA_MCP_MAX_REPLY_STORE_PATH", ""),
                                        int(env.get("WEBCORE_DATA_MCP_MAX_SEND_BOT_ID", "0")), group_id)
            return reader, sender

        transit_api, transit_send = group_access("TRANSIT", TRANSIT_GROUP_ID)
        accounting_api, accounting_send = group_access("ACCOUNTING", ACCOUNTING_GROUP_ID)
        reply = MaxPostedReply.from_env(env, api)
        ack_queue = None
        if _enabled(env, "WEBCORE_DATA_MCP_MAX_ACK_QUEUE_ENABLED"):
            from packages.application.webcore_max_ack_queue import MaxAckQueue
            if reply is None:
                raise ValueError("MAX acknowledgement queue requires the posted-reply binding")
            ack_queue = MaxAckQueue(reply, int(env.get("WEBCORE_DATA_MCP_MAX_SEND_BOT_ID", "0")),
                                    chat_id=int(env.get("WEBCORE_DATA_MCP_MAX_SEND_CHAT_ID", "0")))
        return cls(max_inbox=max_config, cash=cash_config,
                   financial_read=_enabled(env, "WEBCORE_DATA_MCP_CASH_OPERATIONS_READ_ENABLED"),
                   max_api=api, max_reply=reply, max_ack_queue=ack_queue,
                   max_send=MaxSendMessage.from_env(env, api), transit_api=transit_api, transit_send=transit_send,
                   accounting_api=accounting_api, accounting_send=accounting_send)

    def enabled(self, name: str) -> bool:
        return ((name in TRANSIT_READ_TOOLS and self.transit_api is not None)
                or (name in ACCOUNTING_READ_TOOLS and self.accounting_api is not None)
                or (name == "accounting_send_message" and self.accounting_send is not None)
                or (name == "transit_send_message" and self.transit_send is not None)
                or (name == "max_send_message" and self.max_send is not None)
                or (name == "max_posted_reply" and self.max_reply is not None)
                or (name == "max_ack_status" and self.max_ack_queue is not None)
                or (name in {"max_inbox_events", "max_inbox_event"} and self.max_inbox is not None)
                or (name == "cash_accounts" and self.cash is not None)
                or (name == "cash_read" and self.cash is not None and self.financial_read)
                or (name in {"max_messages", "max_message"} and self.max_api is not None))

    def call_tool(self, name: str, args: Mapping[str, Any], *,
                  deadline_at: float | None = None,
                  cancel_event: threading.Event | None = None) -> dict[str, Any]:
        if not self.enabled(name):
            raise CloudReadError("Cloud read source is not enabled", "source_not_enabled")
        try:
            if name == "accounting_send_message":
                _arguments(args, {"request_id", "text"})
                return self.accounting_send.call(dict(args, chat_id=ACCOUNTING_GROUP_ID), deadline_at, cancel_event)
            if name in ACCOUNTING_READ_TOOLS:
                _arguments(args, {"message_id"} if name == "accounting_message" else {"limit", "before_ms"})
                return self.accounting_api.call(ACCOUNTING_READ_TOOLS[name], dict(args, chat_id=ACCOUNTING_GROUP_ID),
                                                deadline_at, cancel_event)
            if name == "transit_send_message":
                _arguments(args, {"request_id", "text"})
                return self.transit_send.call(dict(args, chat_id=TRANSIT_GROUP_ID), deadline_at, cancel_event)
            if name in TRANSIT_READ_TOOLS:
                _arguments(args, {"message_id"} if name == "transit_message" else {"limit", "before_ms"})
                return self.transit_api.call(TRANSIT_READ_TOOLS[name], dict(args, chat_id=TRANSIT_GROUP_ID),
                                             deadline_at, cancel_event)
            if name == "max_ack_status":
                return self._max_ack_status(args, deadline_at, cancel_event)
            if name == "max_send_message":
                return self.max_send.call(args, deadline_at, cancel_event)
            if name == "max_posted_reply":
                handler = self.max_ack_queue or self.max_reply
                return handler.call(args, deadline_at, cancel_event)
            if name in {"max_messages", "max_message"}:
                return self.max_api.call(name, args, deadline_at, cancel_event)
            if name == "cash_read":
                from packages.application.webcore_cloud_extended_reads import cash_read
                return cash_read(self, args, deadline_at, cancel_event)
            if name == "max_inbox_events":
                return self._max_events(args, deadline_at, cancel_event)
            if name == "max_inbox_event":
                return self._max_event(args, deadline_at, cancel_event)
            return self._cash_accounts(args, deadline_at, cancel_event)
        except CloudReadError:
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            # Never include filesystem locations, payloads or SQLite errors in MCP output.
            raise CloudReadError("Configured read source is unavailable") from exc

    @contextmanager
    def _connect(self, path: Path, deadline_at: float | None,
                 cancel_event: threading.Event | None) -> Iterator[sqlite3.Connection]:
        _fixed_path(path)
        if not stat.S_ISREG(path.lstat().st_mode):
            raise CloudReadError("Configured read source is unavailable")
        conn = sqlite3.connect(f"file:{quote(str(path), safe='/')}?mode=ro", uri=True, timeout=1.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.set_progress_handler(
                lambda: int(bool((cancel_event and cancel_event.is_set())
                                 or (deadline_at is not None and time.monotonic() >= deadline_at))), 1000)
            conn.execute("BEGIN")
            yield conn
        finally:
            conn.close()

    def _max_ack_status(self, args: Mapping[str, Any], deadline_at: float | None,
                        cancel_event: threading.Event | None) -> dict[str, Any]:
        """Read durable receipts only; never call the queue's writing connection."""
        _arguments(args, {"chat_id", "message_ids"})
        chat = args.get("chat_id")
        if type(chat) is not int or chat != self.max_ack_queue.chat_id:
            raise CloudReadError("MAX group is not authorized", "group_not_authorized")
        mids = args.get("message_ids")
        if (not isinstance(mids, list) or not 1 <= len(mids) <= MAX_PAGE_SIZE
                or any(not isinstance(mid, str) or re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", mid) is None
                       for mid in mids)
                or len(set(mids)) != len(mids)):
            raise CloudReadError("message_ids must contain 1 to 100 unique source message IDs", "invalid_arguments")
        if ((cancel_event and cancel_event.is_set())
                or (deadline_at is not None and time.monotonic() >= deadline_at)):
            raise CloudReadError("Receipt read deadline exceeded", "deadline_exceeded")
        marks = ",".join("?" for _ in mids)
        with self._connect(self.max_ack_queue.reply.path, deadline_at, cancel_event) as conn:
            stored = conn.execute(
                f"SELECT source_id,state,reply_id FROM replies WHERE chat_id=? AND source_id IN ({marks})",
                (chat, *mids)).fetchall()
            # Older journals remain readable without any schema initialization.
            has_recovery = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='max_ack_recovery'").fetchone()
            recovery = ({row["source_id"]: row["resend_count"] for row in conn.execute(
                f"SELECT source_id,resend_count FROM max_ack_recovery WHERE chat_id=? AND source_id IN ({marks})",
                (chat, *mids)).fetchall()} if has_recovery else {})
        receipts = {row["source_id"]: row for row in stored}
        rows = []
        for mid in mids:
            row = receipts.get(mid)
            state, reply_id = (row["state"], row["reply_id"]) if row else ("not_found", None)
            if (state not in {"queued", "retry_wait", "pending", "unknown", "sent", "source_changed", "not_found"}
                    or (reply_id is not None and (not isinstance(reply_id, str)
                        or re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", reply_id) is None))
                    or (state == "sent" and reply_id is None)):
                raise CloudReadError("Acknowledgement receipt is unavailable")
            resends = recovery.get(mid)
            if resends is not None and (type(resends) is not int or resends < 0):
                raise CloudReadError("Acknowledgement receipt is unavailable")
            rows.append(dict(message_id=mid, status=state, reply_message_id=reply_id,
                             delivery_confirmed=state == "sent", automatic_resends=resends))
        return dict(status="ok", source="persisted_max_acknowledgement_receipts", chat_id=chat, rows=rows,
                    all_delivery_confirmed=all(row["delivery_confirmed"] for row in rows),
                    coverage="Durable acknowledgement receipts only; sent records prior verified delivery. "
                             "Missing is not sent and does not authorize resubmission. No live MAX read, "
                             "source-version check or financial verification is performed.",
                    upstream_calls=False, mutations=False)

    def _max_events(self, args: Mapping[str, Any], deadline_at: float | None,
                    cancel_event: threading.Event | None) -> dict[str, Any]:
        _arguments(args, {"chat_id", "limit", "cursor"})
        chat_id = args.get("chat_id")
        if type(chat_id) is not int or chat_id not in self.max_inbox.chat_ids:
            raise CloudReadError("MAX group is not authorized for reading", "group_not_authorized")
        limit = _limit(args)
        before = 2**63 - 1
        watermark = None
        if args.get("cursor") is not None:
            value = _cursor_decode(args["cursor"])
            if (set(value) != {"chat_id", "before", "watermark"} or value["chat_id"] != chat_id
                    or any(type(value[key]) is not int or not 0 < value[key] < 2**63
                           for key in ("before", "watermark"))
                    or value["before"] > value["watermark"]):
                raise CloudReadError("Invalid MAX page cursor", "invalid_arguments")
            before, watermark = value["before"], value["watermark"]
        with self._connect(self.max_inbox.db_path, deadline_at, cancel_event) as conn:
            if watermark is None:
                watermark = conn.execute(
                    "SELECT MAX(rowid) FROM max_inbox_events WHERE chat_id=?", (chat_id,)).fetchone()[0] or 0
            rows = conn.execute(
                "SELECT rowid AS ingestion_sequence,event_key,bot_id,chat_id,update_type,message_id,"
                "event_timestamp_ms,received_at,raw_payload FROM max_inbox_events "
                "WHERE chat_id=? AND rowid<? AND rowid<=? ORDER BY rowid DESC LIMIT ?",
                (chat_id, before, watermark, limit + 1),
            ).fetchall()
        items = [_event(row) for row in rows[:limit]]
        cursor = None
        if len(rows) > limit:
            cursor = _cursor_encode({"chat_id": chat_id, "before": rows[limit - 1]["ingestion_sequence"],
                                     "watermark": watermark})
        return dict(status="ok", source="persisted_max_webhook_inbox", chat_id=chat_id,
                    rows=items, next_cursor=cursor, limit=limit, snapshot_sequence=watermark,
                    order="ingestion_sequence_desc", event_types=["message_created", "message_edited", "message_removed"],
                    coverage="Only accepted, persisted webhook events; earlier chat history and missed events are unknown. "
                             "Edits and removals are separate events, not a reconstructed current conversation. "
                             "Continue with next_cursor; restart without cursor to see newly received events.",
                    raw_payloads_returned=False, upstream_calls=False, mutations=False)

    def _max_event(self, args: Mapping[str, Any], deadline_at: float | None,
                   cancel_event: threading.Event | None) -> dict[str, Any]:
        _arguments(args, {"chat_id", "event_key"})
        chat_id = args.get("chat_id")
        if type(chat_id) is not int or chat_id not in self.max_inbox.chat_ids:
            raise CloudReadError("MAX group is not authorized for reading", "group_not_authorized")
        event_key = args.get("event_key")
        if not isinstance(event_key, str) or not re.fullmatch(r"[0-9a-f]{64}", event_key):
            raise CloudReadError("An exact event_key from the MAX event list is required", "invalid_arguments")
        with self._connect(self.max_inbox.db_path, deadline_at, cancel_event) as conn:
            row = conn.execute(
                "SELECT rowid AS ingestion_sequence,event_key,bot_id,chat_id,update_type,message_id,"
                "event_timestamp_ms,received_at,raw_payload FROM max_inbox_events "
                "WHERE chat_id=? AND event_key=? LIMIT 1", (chat_id, event_key),
            ).fetchone()
        if row is None:
            # Same outcome for an unknown event and an event in a different group.
            raise CloudReadError("MAX event is not available in the authorized group", "event_not_found")
        event = _event(row, detail=True)
        return dict(status="ok", source="persisted_max_webhook_inbox", event=event,
                    coverage="One exact persisted event, not a reconstructed current message or full chat history. "
                             "Later edits and removals may exist. Join message_text_chunks in order only when "
                             "message_text_complete=true. Incomplete or redacted text must not be used to infer "
                             "a payment or other financial operation. Attachments are not included.",
                    raw_payloads_returned=False, upstream_calls=False, mutations=False)

    def _cash_accounts(self, args: Mapping[str, Any], deadline_at: float | None,
                       cancel_event: threading.Event | None) -> dict[str, Any]:
        _arguments(args, {"limit", "cursor"})
        limit = _limit(args)
        after = ""
        if args.get("cursor") is not None:
            value = _cursor_decode(args["cursor"])
            if (set(value) != {"after", "store_id"} or value["store_id"] != self.cash.binding.store_id
                    or not isinstance(value["after"], str) or not 1 <= len(value["after"]) <= 160):
                raise CloudReadError("Invalid cash page cursor", "invalid_arguments")
            after = value["after"]
        # Revalidate the owning sidecar's TEST identity for every call. This is a
        # source binding only; its web grant is not an OAuth principal mapping.
        binding = load_finance_bootstrap_access({
            "FINANCE_LIQUIDITY_ACCESS_CONFIG": str(self.cash.access_config_path)})
        if binding != self.cash.binding:
            raise CloudReadError("Cash TEST source binding changed; restart with reviewed configuration")
        validate_finance_bootstrap_store(binding.store_path, binding, require_existing=True)
        with self._connect(binding.store_path, deadline_at, cancel_event) as conn:
            meta = conn.execute(
                "SELECT schema_version FROM finance_liquidity_schema_meta WHERE singleton=1").fetchone()
            if meta is None or meta[0] != FINANCE_CASH_SCHEMA_VERSION:
                raise CloudReadError("Cash source schema is unavailable")
            # Deliberately no FinanceCashService.list_accounts(): that computes
            # balances, including cash_vladislav, requiring a distinct grant.
            rows = conn.execute(
                "SELECT account_id,name,account_type,currency,currency_exponent,is_active "
                "FROM finance_liquidity_accounts WHERE is_deleted=0 AND account_id>? "
                "ORDER BY account_id LIMIT ?", (after, limit + 1)).fetchall()
        cursor = (_cursor_encode({"after": rows[limit - 1]["account_id"], "store_id": binding.store_id})
                  if len(rows) > limit else None)
        return dict(status="ok", source="finance_liquidity_account_metadata", store_id=binding.store_id,
                    store_mode=binding.mode, store_class="TEST", instance_label=binding.instance_label,
                    rows=[dict(row) for row in rows[:limit]], limit=limit, next_cursor=cursor,
                    balances_returned=False, transactions_returned=False, mutations=False,
                    coverage="TEST account directory metadata only. No REAL cash store is connected; "
                             "balances, documents, ledger entries and writes require separate authorization.")


def _event(row: sqlite3.Row, *, detail: bool = False) -> dict[str, Any]:
    item = {key: row[key] for key in ("ingestion_sequence", "event_key", "bot_id", "chat_id",
                                    "update_type", "message_id", "event_timestamp_ms", "received_at")}
    if row["update_type"] not in {"message_created", "message_edited", "message_removed"}:
        raise CloudReadError("MAX inbox contains an unsupported event")
    raw = row["raw_payload"]
    if not isinstance(raw, (bytes, str)) or len(raw) > 1024 * 1024:
        raise CloudReadError("MAX inbox event is unavailable")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise CloudReadError("MAX inbox event is unavailable")
    message = payload.get("message") or {} if row["update_type"] != "message_removed" else {}
    if not isinstance(message, dict):
        raise CloudReadError("MAX inbox event is unavailable")
    body = message.get("body") or {}
    if not isinstance(body, dict):
        raise CloudReadError("MAX inbox event is unavailable")
    text = body.get("text") or ""
    if not isinstance(text, str):
        raise CloudReadError("MAX inbox event text is unavailable")
    attachments = body.get("attachments") or []
    item.update(attachment_count=len(attachments), message_content_is_untrusted=True)
    if detail:
        too_long = len(text) > MAX_MESSAGE_TEXT_CHARS
        item.update(
            message_text_chunks=([text[start:start + MESSAGE_TEXT_CHUNK_CHARS]
                                  for start in range(0, len(text), MESSAGE_TEXT_CHUNK_CHARS)] if not too_long else []),
            message_text_chars=len(text), message_text_max_chars=MAX_MESSAGE_TEXT_CHARS,
            message_text_complete=not too_long, message_text_too_long=too_long,
            message_text_redacted=False,
        )
    else:
        item.update(message_text=text[:550], message_text_truncated=len(text) > 550)
    return item


def _arguments(args: Mapping[str, Any], allowed: set[str]) -> None:
    if set(args) - allowed:
        raise CloudReadError("Unknown tool argument", "invalid_arguments")


def _limit(args: Mapping[str, Any]) -> int:
    value = args.get("limit", 25)
    if type(value) is not int or not 1 <= value <= MAX_PAGE_SIZE:
        raise CloudReadError("limit must be an integer from 1 to 100", "invalid_arguments")
    return value


def _cursor_encode(value: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


def _cursor_decode(value: Any) -> dict[str, Any]:
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 1024:
            raise ValueError()
        decoded = json.loads(base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True))
        if not isinstance(decoded, dict):
            raise ValueError()
        return decoded
    except (ValueError, UnicodeError) as exc:
        raise CloudReadError("Invalid page cursor", "invalid_arguments") from exc
