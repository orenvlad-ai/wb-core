"""Offline retirement of isolated TEST cash accounts with an exact snapshot guard.

Financial facts stay in the store. Only account directory tombstones and an
operation/audit receipt are appended. The sidecar must be stopped by the caller.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any

from packages.adapters.finance_liquidity_access import (
    FinanceBootstrapAccess,
    validate_finance_bootstrap_store,
)
from packages.application.finance_liquidity_cash import (
    FinanceCashError,
    FinanceCashService,
    _canon,
    _digest,
    _now,
)
from packages.application.finance_liquidity_directories import SEED_ACCOUNTS


SCOPE = "accounts.retire_isolated_test"
PROTECTED_IDS = {item[0] for item in SEED_ACCOUNTS}


def _request_digest(store_id: str, account_ids: tuple[str, ...], fingerprint: str) -> str:
    return _digest({"store_id": store_id, "account_ids": sorted(account_ids), "fingerprint": fingerprint})


def _guard_binding(db_path: Path, access: FinanceBootstrapAccess, store_id: str) -> None:
    if access.mode != "isolated_test" or access.store_id != store_id:
        raise FinanceCashError("invalid_retirement_target", "Isolated TEST store binding required", 409)
    try:
        validate_finance_bootstrap_store(db_path, access, require_existing=True)
    except ValueError as exc:
        raise FinanceCashError("invalid_retirement_target", str(exc), 409) from exc


def _snapshot(conn: sqlite3.Connection) -> str:
    """Hash every persisted table row, independent of SQLite page layout."""
    digest = hashlib.sha256()
    for kind, name, sql in conn.execute(
        "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
    ):
        digest.update(_canon([kind, name, sql]).encode() + b"\n")
    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )]
    for table in tables:
        quoted = '"' + table.replace('"', '""') + '"'
        rows = [dict(row) for row in conn.execute(f"SELECT * FROM {quoted}")]
        for payload in sorted(_canon(row) for row in rows):
            digest.update((table + "\0" + payload + "\n").encode())
    return "sha256:" + digest.hexdigest()


def _validate(conn: sqlite3.Connection, account_ids: tuple[str, ...]) -> dict[str, Any]:
    if not account_ids or len(set(account_ids)) != len(account_ids) or set(account_ids) & PROTECTED_IDS:
        raise FinanceCashError("invalid_retirement_accounts", "Explicit unique non-seed accounts required", 409)
    accounts = []
    for ident in account_ids:
        row = conn.execute("SELECT * FROM finance_liquidity_accounts WHERE account_id=?", (ident,)).fetchone()
        if (row is None or row["is_deleted"] or row["account_type"] != "cash"
                or row["code"] is not None or not str(row["name"]).strip().upper().startswith("TEST")):
            raise FinanceCashError("invalid_retirement_accounts", "Active TEST cash accounts required", 409)
        accounts.append({"account_id": ident, "name": row["name"], "revision": row["revision"]})
    selected = set(account_ids)
    documents = list(conn.execute(
        "SELECT document_id,source_account_id,target_account_id,reversal_of_document_id,"
        "replaces_opening_document_id FROM finance_liquidity_documents"
    ))
    selected_docs = {
        row["document_id"] for row in documents
        if row["source_account_id"] in selected or row["target_account_id"] in selected
    }
    for row in documents:
        refs = {row["source_account_id"], row["target_account_id"]} - {None}
        linked = {row["reversal_of_document_id"], row["replaces_opening_document_id"]} - {None}
        if (row["document_id"] in selected_docs and not refs <= selected) or any(
            (linked_id in selected_docs) != (row["document_id"] in selected_docs)
            for linked_id in linked
        ):
            raise FinanceCashError("retirement_cross_account_graph", "TEST documents link outside selected accounts", 409)
    for row in conn.execute(
        "SELECT t.document_id,e.ledger_account_id FROM finance_liquidity_ledger_entries e "
        "JOIN finance_liquidity_ledger_transactions t ON t.transaction_id=e.transaction_id"
    ):
        ledger_id = str(row["ledger_account_id"])
        if ledger_id.startswith("asset:") and (
            (ledger_id.removeprefix("asset:") in selected) != (row["document_id"] in selected_docs)
        ):
            raise FinanceCashError("retirement_cross_account_graph", "TEST ledger links outside selected documents", 409)
    return {
        "accounts": accounts,
        "document_count": len(selected_docs),
        "reconciliation_count": conn.execute(
            f"SELECT count(*) FROM finance_liquidity_cash_reconciliations WHERE account_id IN ({','.join('?' for _ in account_ids)})",
            account_ids,
        ).fetchone()[0],
        "anchor_count": conn.execute(
            f"SELECT count(*) FROM finance_liquidity_opening_anchors WHERE account_id IN ({','.join('?' for _ in account_ids)})",
            account_ids,
        ).fetchone()[0],
    }


def preview_test_account_retirement(
    db_path: Path, access: FinanceBootstrapAccess, store_id: str, account_ids: tuple[str, ...]
) -> dict[str, Any]:
    _guard_binding(db_path, access, store_id)
    service = FinanceCashService(db_path)
    with service._connect() as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or conn.execute("PRAGMA foreign_key_check").fetchone():
            raise FinanceCashError("retirement_source_invalid", "Store integrity failed", 409)
        service._assert_ledger_integrity(conn)
        result = _validate(conn, account_ids)
        return {**result, "store_id": store_id, "fingerprint": _snapshot(conn)}


def apply_test_account_retirement(
    db_path: Path, access: FinanceBootstrapAccess, store_id: str,
    account_ids: tuple[str, ...], backup_path: Path, fingerprint: str,
    operation_id: str, actor: str, *, service_stopped: bool,
) -> dict[str, Any]:
    """Single submit. A repeated operation ID returns its durable receipt."""
    _guard_binding(db_path, access, store_id)
    source, backup = Path(db_path), Path(backup_path)
    if not service_stopped or not actor.strip() or not operation_id.strip():
        raise FinanceCashError("retirement_precondition", "Stopped service and operation identity required", 409)
    if not backup.is_absolute() or backup.is_symlink() or backup.resolve() == source:
        raise FinanceCashError("invalid_retirement_backup", "Separate absolute backup path required", 409)
    request_digest = _request_digest(store_id, account_ids, fingerprint)
    service = FinanceCashService(source)
    with service._connect() as conn:
        existing = conn.execute("SELECT * FROM finance_liquidity_operations WHERE operation_id=?", (operation_id,)).fetchone()
        if existing is not None:
            if existing["scope"] != SCOPE or existing["actor"] != actor or existing["request_digest"] != request_digest:
                raise FinanceCashError("retirement_operation_conflict", "Operation identity already used", 409)
            return json.loads(existing["result_json"])
    if backup.exists() or not fingerprint.startswith("sha256:"):
        raise FinanceCashError("invalid_retirement_backup", "Absent backup and preview fingerprint required", 409)
    # The operator stops the sidecar; an exclusive transaction will reject a
    # concurrent writer. Backup is made and checked before any source mutation.
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    try:
        with service._connect() as source_conn:
            if _snapshot(source_conn) != fingerprint:
                raise FinanceCashError("retirement_source_changed", "Preview is stale", 409)
            _validate(source_conn, account_ids)
            with sqlite3.connect(backup) as backup_conn:
                source_conn.backup(backup_conn)
        with sqlite3.connect(f"file:{backup}?mode=ro", uri=True) as backup_conn:
            backup_conn.row_factory = sqlite3.Row
            if (_snapshot(backup_conn) != fingerprint or
                    backup_conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or
                    backup_conn.execute("PRAGMA foreign_key_check").fetchone()):
                raise FinanceCashError("retirement_backup_invalid", "Backup validation failed", 500)
        with backup.open("rb") as backup_file:
            os.fsync(backup_file.fileno())
        os.chmod(backup, 0o400)
        with service._connect(write=True) as conn:
            conn.execute("ROLLBACK")
            conn.execute("BEGIN EXCLUSIVE")
            if _snapshot(conn) != fingerprint:
                raise FinanceCashError("retirement_source_changed", "Source changed during backup", 409)
            service._assert_ledger_integrity(conn)
            result = _validate(conn, account_ids)
            now = _now()
            for item in result["accounts"]:
                changed = conn.execute(
                    "UPDATE finance_liquidity_accounts SET is_deleted=1,is_active=0,revision=revision+1,updated_at=? "
                    "WHERE account_id=? AND revision=? AND is_deleted=0",
                    (now, item["account_id"], item["revision"]),
                )
                if changed.rowcount != 1:
                    raise FinanceCashError("retirement_source_changed", "Account changed", 409)
            receipt = {"operation_id": operation_id, "store_id": store_id,
                       "retired_account_ids": list(account_ids), "source_fingerprint": fingerprint,
                       "backup_path": str(backup), "retired_at": now}
            conn.execute(
                "INSERT INTO finance_liquidity_operations(operation_id,scope,idempotency_key,request_digest,actor,result_json,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (operation_id, SCOPE, operation_id, request_digest, actor, _canon(receipt), now),
            )
            for ident in account_ids:
                service._audit(conn, actor, "accounts.retired_isolated_test", ident,
                               {"operation_id": operation_id, "source_fingerprint": fingerprint})
            service._assert_ledger_integrity(conn)
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or conn.execute("PRAGMA foreign_key_check").fetchone():
                raise FinanceCashError("retirement_result_invalid", "Post-write integrity failed", 500)
        return receipt
    except Exception:
        # Keep the backup for recovery or investigation if the source CAS fails.
        raise


def readback_test_account_retirement(
    db_path: Path, access: FinanceBootstrapAccess, store_id: str,
    account_ids: tuple[str, ...], fingerprint: str, operation_id: str, actor: str,
) -> dict[str, Any]:
    """Read the same durable receipt after an ambiguous apply response."""
    _guard_binding(db_path, access, store_id)
    if not account_ids or len(set(account_ids)) != len(account_ids):
        raise FinanceCashError("invalid_retirement_accounts", "Unique account IDs required", 409)
    service = FinanceCashService(db_path)
    with service._connect() as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or conn.execute("PRAGMA foreign_key_check").fetchone():
            raise FinanceCashError("retirement_result_invalid", "Store integrity failed", 503)
        service._assert_ledger_integrity(conn)
        record = conn.execute(
            "SELECT scope,actor,request_digest,result_json FROM finance_liquidity_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if (record is None or record["scope"] != SCOPE or record["actor"] != actor or
                record["request_digest"] != _request_digest(store_id, account_ids, fingerprint)):
            raise FinanceCashError("retirement_receipt_not_found", "Matching retirement receipt not found", 404)
        try:
            receipt = json.loads(record["result_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise FinanceCashError("retirement_result_invalid", "Retirement receipt is invalid", 503) from exc
        if (not isinstance(receipt, dict) or receipt.get("operation_id") != operation_id or
                receipt.get("store_id") != store_id or
                set(receipt.get("retired_account_ids", [])) != set(account_ids) or
                receipt.get("source_fingerprint") != fingerprint):
            raise FinanceCashError("retirement_result_invalid", "Retirement receipt does not match", 503)
        for ident in account_ids:
            row = conn.execute(
                "SELECT is_deleted,is_active FROM finance_liquidity_accounts WHERE account_id=?", (ident,)
            ).fetchone()
            if row is None or row["is_deleted"] != 1 or row["is_active"] != 0:
                raise FinanceCashError("retirement_result_invalid", "Retirement tombstone missing", 503)
            if not conn.execute(
                "SELECT 1 FROM finance_liquidity_audit_events WHERE event_type='accounts.retired_isolated_test' "
                "AND object_id=? AND actor=? AND payload_json=?",
                (ident, actor, _canon({"operation_id": operation_id, "source_fingerprint": fingerprint})),
            ).fetchone():
                raise FinanceCashError("retirement_result_invalid", "Retirement audit missing", 503)
        return receipt
