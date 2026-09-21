"""Append-only, exact-money cash ledger service.

This is intentionally a separate SQLite owner.  Importing it opens no database;
only the explicit bootstrap command creates a store.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from pathlib import Path
import os
import sqlite3
from typing import Any, Iterator, Mapping
from uuid import uuid4

from packages.domain.finance_liquidity import (
    CURRENCY_MINOR_UNIT_EXPONENTS,
    MAX_MONEY_MINOR,
    MoneyError,
    money_from_api,
    money_to_api,
)
from packages.business_time import (
    CANONICAL_BUSINESS_TIMEZONE,
    CANONICAL_BUSINESS_TIMEZONE_NAME,
)
from packages.contracts.finance_liquidity_cash import FINANCE_CASH_SCHEMA_VERSION


class FinanceCashError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        status: int = 422,
        *,
        data: Mapping[str, Any] | None = None,
        commit: bool = False,
    ) -> None:
        super().__init__(message)
        self.code, self.status, self.data, self.commit = (
            code,
            status,
            dict(data or {}),
            commit,
        )


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _utc_timestamp(value: object, field: str) -> str:
    """Accept one explicit UTC instant, never a locale-dependent date."""

    text = str(value or "").strip()
    if not text.endswith("Z"):
        raise FinanceCashError(
            "invalid_timestamp", f"{field} must be an RFC3339 UTC timestamp"
        )
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise FinanceCashError(
            "invalid_timestamp", f"{field} must be an RFC3339 UTC timestamp"
        ) from exc
    if parsed.tzinfo != timezone.utc:
        raise FinanceCashError("invalid_timestamp", f"{field} must be UTC")
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _business_day_end(value: str) -> str:
    """Final representable instant of the selected Yekaterinburg business day."""

    try:
        business_day = date.fromisoformat(value)
    except ValueError as exc:
        raise FinanceCashError(
            "invalid_reconciliation", "week_ending must be an ISO business-day label"
        ) from exc
    next_day = datetime.combine(
        business_day + timedelta(days=1), time.min, tzinfo=CANONICAL_BUSINESS_TIMEZONE
    )
    return (
        (next_day - timedelta(microseconds=1))
        .astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _canon(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canon(value).encode()).hexdigest()


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def bootstrap_finance_cash_store(path: Path) -> None:
    """Create the isolated store once; callers must explicitly request this."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise FinanceCashError(
            "store_already_exists", "Finance store already exists", 409
        ) from exc
    else:
        os.close(descriptor)
    conn = sqlite3.connect(target)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT INTO finance_liquidity_schema_meta(singleton, schema_version, created_at) VALUES(1, ?, ?)",
            (FINANCE_CASH_SCHEMA_VERSION, _now()),
        )
        conn.commit()
    finally:
        conn.close()


class FinanceCashService:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    @contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if not self.db_path.is_file():
            raise FinanceCashError(
                "finance_unavailable", "Finance store is not bootstrapped", 503
            )
        uri = f"file:{self.db_path.resolve()}?mode={'rw' if write else 'ro'}"
        conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=5.0)
        conn.row_factory = sqlite3.Row
        # Schema guards use this connection-local capability for the few legal
        # state transitions.  A plain sqlite connection cannot manufacture a
        # posted transition or an immutable-fact mutation by merely issuing SQL.
        conn.create_function("finance_internal_write", 0, lambda: 1)
        conn.create_function("finance_internal_operation_id", 0, lambda: None)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            if not write:
                conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            meta = conn.execute(
                "SELECT schema_version FROM finance_liquidity_schema_meta WHERE singleton=1"
            ).fetchone()
            if meta is None or int(meta[0]) != FINANCE_CASH_SCHEMA_VERSION:
                raise FinanceCashError(
                    "finance_schema_unavailable", "Finance schema is unavailable", 503
                )
            yield conn
            conn.commit()
        except sqlite3.OperationalError as exc:
            if conn.in_transaction:
                conn.rollback()
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise FinanceCashError(
                    "finance_storage_busy", "Finance storage is busy", 503
                ) from exc
            raise
        except FinanceCashError as exc:
            if conn.in_transaction:
                (conn.commit() if write and exc.commit else conn.rollback())
            raise
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def _account(
        self, conn: sqlite3.Connection, account_id: str, *, active: bool = True
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM finance_liquidity_accounts WHERE account_id=?", (account_id,)
        ).fetchone()
        if row is None:
            raise FinanceCashError("account_not_found", "Account not found", 404)
        if active and not row["is_active"]:
            raise FinanceCashError("account_archived", "Account is archived", 422)
        return row

    def _balance(
        self, conn: sqlite3.Connection, account_id: str, as_of: str | None = None
    ) -> int:
        if as_of is not None:
            as_of = _utc_timestamp(as_of, "as_of")
        self._assert_ledger_integrity(conn)
        sql = """
          SELECT e.side,e.amount_minor FROM finance_liquidity_ledger_entries e
          JOIN finance_liquidity_ledger_transactions t ON t.transaction_id=e.transaction_id
          JOIN finance_liquidity_ledger_transaction_seals s ON s.transaction_id=t.transaction_id
          JOIN finance_liquidity_effect_set_seals os ON os.operation_id=t.origin_operation_id
          WHERE e.ledger_account_id=?"""
        args: list[Any] = [f"asset:{account_id}"]
        if as_of:
            sql += " AND t.effective_at<=?"
            args.append(as_of)
        balance = 0
        for entry in conn.execute(sql, args):
            balance += (
                int(entry["amount_minor"])
                if entry["side"] == "debit"
                else -int(entry["amount_minor"])
            )
        if abs(balance) > MAX_MONEY_MINOR:
            raise FinanceCashError(
                "balance_projection_unavailable",
                "Balance is out of supported range",
                503,
            )
        return balance

    def _assert_ledger_integrity(self, conn: sqlite3.Connection) -> None:
        """Fail closed unless every visible ledger fact has its full receipt chain.

        SQLite triggers make sealed facts append-only, but they cannot prove a
        hand-written *new* chain has the right membership.  Reads therefore
        recompute every seal and require a durable operation for every effect.
        This deliberately validates globally: an unrelated forged transaction
        must not be silently ignored just because a query filters one account.
        """
        def invalid() -> None:
            raise FinanceCashError(
                "ledger_integrity_unavailable", "Ledger receipt chain is invalid", 503
            )

        operations = {
            str(row["operation_id"]): row
            for row in conn.execute("SELECT * FROM finance_liquidity_operations")
        }
        documents = {
            str(row["document_id"]): row
            for row in conn.execute(
                "SELECT d.*,COALESCE(a.currency,b.currency) AS currency "
                "FROM finance_liquidity_documents d "
                "LEFT JOIN finance_liquidity_accounts a ON a.account_id=d.source_account_id "
                "LEFT JOIN finance_liquidity_accounts b ON b.account_id=d.target_account_id"
            )
        }
        accounts = {
            str(row["account_id"]): row
            for row in conn.execute("SELECT account_id,currency,currency_exponent FROM finance_liquidity_accounts")
        }
        seals = {
            str(row["operation_id"]): row
            for row in conn.execute("SELECT * FROM finance_liquidity_effect_set_seals")
        }
        all_transactions = list(
            conn.execute("SELECT * FROM finance_liquidity_ledger_transactions ORDER BY sequence_no")
        )
        transactions: dict[str, list[sqlite3.Row]] = {}
        document_transactions: dict[str, list[sqlite3.Row]] = {}
        for row in all_transactions:
            transactions.setdefault(str(row["origin_operation_id"]), []).append(row)
            document_transactions.setdefault(str(row["document_id"]), []).append(row)
        transaction_ids = {str(row["transaction_id"]) for row in all_transactions}
        entry_transaction_ids = {
            str(row[0])
            for row in conn.execute("SELECT DISTINCT transaction_id FROM finance_liquidity_ledger_entries")
        }
        transaction_seal_ids = {
            str(row[0])
            for row in conn.execute("SELECT transaction_id FROM finance_liquidity_ledger_transaction_seals")
        }
        # All three membership sets are exact in both directions.  This catches
        # foreign-key-off orphan entries/seals as well as missing receipts.
        effect_operations = {
            operation_id
            for operation_id, operation in operations.items()
            if operation["effect_root_document_id"] is not None
        }
        transaction_effect_operations = {
            operation_id
            for operation_id, seal in seals.items()
            if int(seal["transaction_count"]) > 0
        }
        if (
            effect_operations != set(seals)
            or set(transactions) != transaction_effect_operations
            or entry_transaction_ids != transaction_ids
            or transaction_seal_ids != transaction_ids
        ):
            invalid()
        receipt_documents: set[str] = set()
        for operation_id, seal in seals.items():
            operation = operations.get(operation_id)
            txs = transactions.get(operation_id, [])
            if operation is None or not operation["effect_root_document_id"]:
                invalid()
            root = str(operation["effect_root_document_id"])
            root_document = documents.get(root)
            if root_document is None or not root_document["currency"]:
                invalid()
            canonical_transactions = [
                {
                    "transaction_id": str(tx["transaction_id"]),
                    "document_id": str(tx["document_id"]),
                    "phase": str(tx["phase"]),
                    "effective_at": str(tx["effective_at"]),
                }
                for tx in txs
            ]
            if (
                int(seal["transaction_count"]) != len(canonical_transactions)
                or seal["transactions_digest"] != _digest(canonical_transactions)
                or seal["root_document_id"] != root
            ):
                invalid()
            try:
                result = json.loads(operation["result_json"])
            except (TypeError, json.JSONDecodeError):
                invalid()
            if (
                not isinstance(result, dict)
                or result.get("operation_id") != operation_id
                or not str(result.get("receipt_id") or "")
                or result.get("document_id") != root
                or result.get("ledger_transaction_ids") != [
                    str(tx["transaction_id"]) for tx in txs
                ]
            ):
                invalid()
            scope = str(operation["scope"])
            if scope.startswith("document.post:"):
                expected_phase = (
                    "transfer_send"
                    if root_document["document_type"] == "transfer"
                    and root_document["transfer_mode"] == "two_phase"
                    else "primary"
                )
                expected_count = (
                    0
                    if root_document["document_type"] == "opening"
                    and int(root_document["amount_minor"]) == 0
                    else 1
                )
                if (
                    scope.removeprefix("document.post:") != root
                    or root_document["status"] not in {"posted", "reversed"}
                    or len(txs) != expected_count
                    or any(
                        tx["document_id"] != root or tx["phase"] != expected_phase
                        for tx in txs
                    )
                ):
                    invalid()
                receipt_documents.add(root)
            elif scope.startswith("transfer."):
                action, _, target = scope.removeprefix("transfer.").partition(":")
                expected_phase = {"complete": "transfer_complete", "cancel": "transfer_cancel"}.get(action)
                if (
                    target != root
                    or expected_phase is None
                    or root_document["document_type"] != "transfer"
                    or root_document["transfer_mode"] != "two_phase"
                    or root_document["transfer_state"]
                    != {"complete": "completed", "cancel": "cancelled"}[action]
                    or len(txs) != 1
                    or txs[0]["document_id"] != root
                    or txs[0]["phase"] != expected_phase
                ):
                    invalid()
                receipt_documents.add(root)
            elif scope.startswith("document.reverse:"):
                target = scope.removeprefix("document.reverse:")
                target_document = documents.get(target)
                original_txs = document_transactions.get(target, [])
                expected = [
                    (root, f"reversal:{tx['phase']}") for tx in reversed(original_txs)
                ]
                if (
                    target_document is None
                    or target_document["status"] != "reversed"
                    or root_document["reversal_of_document_id"] != target
                    or result.get("reversal_of_document_id") != target
                    or not original_txs
                    or [(str(tx["document_id"]), str(tx["phase"])) for tx in txs]
                    != expected
                ):
                    invalid()
                receipt_documents.update({target, root})
            elif scope.startswith("opening.replace:"):
                target = scope.removeprefix("opening.replace:")
                target_document = documents.get(target)
                reversal_id = str(result.get("reversal_document_id") or "")
                reversal_document = documents.get(reversal_id)
                expected = []
                if target_document is not None and int(target_document["amount_minor"]) != 0:
                    expected.append((reversal_id, "reversal"))
                if int(root_document["amount_minor"]) != 0:
                    expected.append((root, "replacement_opening"))
                if (
                    target_document is None
                    or target_document["document_type"] != "opening"
                    or target_document["status"] != "reversed"
                    or root_document["replaces_opening_document_id"] != target
                    or root_document["document_type"] != "opening"
                    or reversal_document is None
                    or reversal_document["document_type"] != "opening"
                    or reversal_document["reversal_of_document_id"] != target
                    or [(str(tx["document_id"]), str(tx["phase"])) for tx in txs]
                    != expected
                ):
                    invalid()
                receipt_documents.update({target, reversal_id, root})
            else:
                invalid()
            for tx in txs:
                document = documents.get(str(tx["document_id"]))
                if document is None or document["currency"] != root_document["currency"]:
                    invalid()
                try:
                    if _utc_timestamp(tx["effective_at"], "stored_effective_at") != tx["effective_at"]:
                        invalid()
                except FinanceCashError:
                    invalid()
                tx_seal = conn.execute(
                    "SELECT * FROM finance_liquidity_ledger_transaction_seals WHERE transaction_id=?",
                    (tx["transaction_id"],),
                ).fetchone()
                entries = [
                    _row(row)
                    for row in conn.execute(
                        "SELECT line_no,ledger_account_id,side,amount_minor "
                        "FROM finance_liquidity_ledger_entries WHERE transaction_id=? ORDER BY line_no",
                        (tx["transaction_id"],),
                    )
                ]
                if (
                    tx_seal is None
                    or int(tx_seal["entry_count"]) != len(entries)
                    or tx_seal["entries_digest"] != _digest(entries)
                    or len(entries) < 2
                    or sum(int(x["amount_minor"]) for x in entries if x["side"] == "debit")
                    != sum(int(x["amount_minor"]) for x in entries if x["side"] == "credit")
                ):
                    invalid()
                for entry in entries:
                    account_name = str(entry["ledger_account_id"])
                    if account_name.startswith("asset:"):
                        account = accounts.get(account_name.removeprefix("asset:"))
                        if (
                            account is None
                            or account["currency"] != document["currency"]
                            or int(account["currency_exponent"])
                            != CURRENCY_MINOR_UNIT_EXPONENTS[str(document["currency"])]
                        ):
                            invalid()
                    elif not account_name.startswith("system:") or not account_name.endswith(
                        f":{document['currency']}"
                    ):
                        invalid()
        if receipt_documents != {
            document_id
            for document_id, document in documents.items()
            if document["status"] in {"posted", "reversed"}
        }:
            invalid()

    def _account_view(
        self, conn: sqlite3.Connection, account: sqlite3.Row, as_of: str | None = None
    ) -> dict[str, Any]:
        if as_of is not None:
            as_of = _utc_timestamp(as_of, "as_of")
        initialized = (
            conn.execute(
                "SELECT 1 FROM finance_liquidity_opening_anchors "
                "WHERE account_id=? AND is_active=1"
                + (" AND cutover_at<=?" if as_of else ""),
                (account["account_id"], as_of) if as_of else (account["account_id"],),
            ).fetchone()
            is not None
        )
        if not initialized:
            return {
                **_row(account),
                "balance_state": "uninitialized",
                "balance_minor": None,
                "balance": None,
            }
        amount = self._balance(conn, account["account_id"], as_of)
        return {
            **_row(account),
            "balance_state": "current",
            "balance_minor": amount,
            "balance": money_to_api(amount, account["currency"]),
            "negative_balance_warning": amount < 0,
        }

    def list_accounts(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            self._assert_ledger_integrity(conn)
            return [
                self._account_view(conn, account)
                for account in conn.execute(
                    "SELECT * FROM finance_liquidity_accounts ORDER BY name COLLATE NOCASE, account_id"
                )
            ]

    def get_account(self, account_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            self._assert_ledger_integrity(conn)
            return self._account_view(
                conn, self._account(conn, account_id, active=False)
            )

    def create_account(
        self, payload: Mapping[str, Any], actor: str, operation_id: str, key: str
    ) -> dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        account_type = str(payload.get("account_type") or "").strip()
        currency = str(payload.get("currency") or "").upper().strip()
        responsible = str(payload.get("responsible_name") or "").strip() or None
        if (
            not name
            or len(name) > 160
            or account_type not in {"cash", "bank"}
            or currency not in CURRENCY_MINOR_UNIT_EXPONENTS
        ):
            raise FinanceCashError(
                "invalid_account",
                "Account name, type and a supported fixed currency are required",
            )
        if account_type == "cash" and not responsible:
            raise FinanceCashError(
                "invalid_account", "Cash account requires responsible_name"
            )
        return self._command(
            "account.create",
            key,
            operation_id,
            actor,
            payload,
            lambda conn: self._create_account_tx(
                conn, name, account_type, currency, responsible, actor
            ),
        )

    def _create_account_tx(
        self,
        conn: sqlite3.Connection,
        name: str,
        account_type: str,
        currency: str,
        responsible: str | None,
        actor: str,
    ) -> dict[str, Any]:
        account_id = _id("fla")
        now = _now()
        conn.execute(
            "INSERT INTO finance_liquidity_accounts("
            "account_id,name,account_type,currency,currency_exponent,responsible_name,"
            "is_active,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,1,1,?,?)",
            (
                account_id,
                name,
                account_type,
                currency,
                CURRENCY_MINOR_UNIT_EXPONENTS[currency],
                responsible,
                now,
                now,
            ),
        )
        self._audit(conn, actor, "account.created", account_id, {"name": name})
        return {
            "account_id": account_id,
            "revision": 1,
            "balance_state": "uninitialized",
        }

    def create_category(
        self, payload: Mapping[str, Any], actor: str, operation_id: str, key: str
    ) -> dict[str, Any]:
        name, direction = (
            str(payload.get("name") or "").strip(),
            str(payload.get("direction") or "").strip(),
        )
        posting_class = str(payload.get("posting_class") or "external_outflow").strip()
        if (
            not name
            or direction not in {"income", "expense"}
            or (
                direction == "expense"
                and posting_class not in {"external_outflow", "fee"}
            )
        ):
            raise FinanceCashError("invalid_category", "Category is invalid")
        return self._command(
            "category.create",
            key,
            operation_id,
            actor,
            payload,
            lambda conn: self._create_category_tx(
                conn, name, direction, posting_class, actor
            ),
        )

    def _create_category_tx(
        self,
        conn: sqlite3.Connection,
        name: str,
        direction: str,
        posting_class: str,
        actor: str,
    ) -> dict[str, Any]:
        category_id, now = _id("flc"), _now()
        conn.execute(
            "INSERT INTO finance_liquidity_categories(category_id,name,direction,posting_class,is_active,created_at) VALUES(?,?,?,?,1,?)",
            (
                category_id,
                name,
                direction,
                posting_class if direction == "expense" else None,
                now,
            ),
        )
        self._audit(conn, actor, "category.created", category_id, {"name": name})
        return {"category_id": category_id}

    def list_categories(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [
                _row(row)
                for row in conn.execute(
                    "SELECT * FROM finance_liquidity_categories WHERE is_active=1 ORDER BY name COLLATE NOCASE"
                )
            ]

    def create_document(
        self, payload: Mapping[str, Any], actor: str, operation_id: str, key: str
    ) -> dict[str, Any]:
        document_type = str(payload.get("document_type") or "").strip()
        if document_type not in {"opening", "income", "expense", "transfer"}:
            raise FinanceCashError("invalid_document", "Unsupported document_type")
        return self._command(
            "document.create",
            key,
            operation_id,
            actor,
            payload,
            lambda conn: self._create_document_tx(conn, document_type, payload, actor),
        )

    def _create_document_tx(
        self,
        conn: sqlite3.Connection,
        document_type: str,
        payload: Mapping[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        document_id, now = _id("fld"), _now()
        stored = self._document_fields(
            document_type,
            payload,
            draft=True,
            currency=self._document_currency(conn, document_type, payload),
        )
        if (
            document_type == "opening"
            and stored["opening_evidence_type"] == "manual_confirmation"
        ):
            stored["opening_evidence_digest"] = _digest(
                {
                    "kind": "manual_opening_confirmation",
                    "actor": actor,
                    "account_id": stored["target_account_id"],
                    "amount_minor": stored["amount_minor"],
                    "occurred_at": stored["occurred_at"],
                    "comment": stored["opening_evidence_ref"],
                    "document_id": document_id,
                }
            )
        conn.execute(
            """INSERT INTO finance_liquidity_documents(document_id,document_type,status,transfer_mode,transfer_state,source_account_id,target_account_id,category_id,amount_minor,occurred_at,purpose,negative_balance_explanation,opening_evidence_type,opening_evidence_digest,opening_evidence_ref,revision,created_at,updated_at,actor) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                document_id,
                document_type,
                "draft",
                stored["transfer_mode"],
                None,
                stored["source_account_id"],
                stored["target_account_id"],
                stored["category_id"],
                stored["amount_minor"],
                stored["occurred_at"],
                stored["purpose"],
                stored["negative_balance_explanation"],
                stored["opening_evidence_type"],
                stored["opening_evidence_digest"],
                stored["opening_evidence_ref"],
                1,
                now,
                now,
                actor,
            ),
        )
        self._audit(
            conn,
            actor,
            "document.created",
            document_id,
            {"document_type": document_type},
        )
        return {"document_id": document_id, "status": "draft", "revision": 1}

    def patch_document(
        self,
        document_id: str,
        payload: Mapping[str, Any],
        actor: str,
        operation_id: str,
        key: str,
    ) -> dict[str, Any]:
        return self._command(
            f"document.patch:{document_id}",
            key,
            operation_id,
            actor,
            payload,
            lambda conn: self._patch_document_tx(conn, document_id, payload, actor),
        )

    def _patch_document_tx(
        self,
        conn: sqlite3.Connection,
        document_id: str,
        payload: Mapping[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        doc = self._document(conn, document_id)
        if doc["status"] != "draft":
            raise FinanceCashError(
                "document_immutable", "Posted document is immutable", 409
            )
        if int(payload.get("base_revision", -1)) != int(doc["revision"]):
            raise FinanceCashError(
                "version_conflict",
                "Draft revision changed",
                409,
                data={"current_revision": doc["revision"]},
            )
        merged = {
            **_row(doc),
            **{key: value for key, value in payload.items() if key != "base_revision"},
        }
        currency = self._document_currency(conn, doc["document_type"], merged)
        if "amount" not in payload and doc["amount_minor"] is not None:
            if currency != doc["currency"]:
                raise FinanceCashError(
                    "invalid_document",
                    "Amount is required when document currency changes",
                )
            merged["amount"] = money_to_api(
                int(doc["amount_minor"]), str(doc["currency"])
            )
        stored = self._document_fields(
            doc["document_type"],
            merged,
            draft=True,
            currency=currency,
        )
        if (
            doc["document_type"] == "opening"
            and stored["opening_evidence_type"] == "manual_confirmation"
        ):
            stored["opening_evidence_digest"] = _digest(
                {
                    "kind": "manual_opening_confirmation",
                    "actor": actor,
                    "account_id": stored["target_account_id"],
                    "amount_minor": stored["amount_minor"],
                    "occurred_at": stored["occurred_at"],
                    "comment": stored["opening_evidence_ref"],
                    "document_id": document_id,
                }
            )
        cursor = conn.execute(
            """UPDATE finance_liquidity_documents SET transfer_mode=?,source_account_id=?,target_account_id=?,category_id=?,amount_minor=?,occurred_at=?,purpose=?,negative_balance_explanation=?,opening_evidence_type=?,opening_evidence_digest=?,opening_evidence_ref=?,revision=revision+1,updated_at=? WHERE document_id=? AND revision=? AND status='draft'""",
            (
                stored["transfer_mode"],
                stored["source_account_id"],
                stored["target_account_id"],
                stored["category_id"],
                stored["amount_minor"],
                stored["occurred_at"],
                stored["purpose"],
                stored["negative_balance_explanation"],
                stored["opening_evidence_type"],
                stored["opening_evidence_digest"],
                stored["opening_evidence_ref"],
                _now(),
                document_id,
                doc["revision"],
            ),
        )
        if cursor.rowcount != 1:
            raise FinanceCashError("version_conflict", "Draft revision changed", 409)
        self._audit(conn, actor, "document.patched", document_id, {})
        return {
            "document_id": document_id,
            "status": "draft",
            "revision": int(doc["revision"]) + 1,
        }

    def post_document(
        self,
        document_id: str,
        payload: Mapping[str, Any],
        actor: str,
        operation_id: str,
        key: str,
    ) -> dict[str, Any]:
        result = self._command(
            f"document.post:{document_id}",
            key,
            operation_id,
            actor,
            payload,
            lambda conn: self._post_document_tx(conn, document_id, payload, actor, operation_id),
        )
        if result.get("action_required") == "duplicate_confirmation":
            raise FinanceCashError(
                "duplicate_confirmation_required",
                "Similar posted operation requires confirmation",
                409,
                data={
                    "duplicate_confirmation_token": result["duplicate_confirmation_token"],
                    "candidates": result["candidates"],
                    "operation_id": result["operation_id"],
                },
            )
        return result

    def _post_document_tx(
        self,
        conn: sqlite3.Connection,
        document_id: str,
        payload: Mapping[str, Any],
        actor: str,
        operation_id: str,
    ) -> dict[str, Any]:
        doc = self._document(conn, document_id)
        if doc["status"] != "draft":
            raise FinanceCashError(
                "document_immutable", "Document is already posted", 409
            )
        if int(payload.get("base_revision", -1)) != int(doc["revision"]):
            raise FinanceCashError(
                "version_conflict",
                "Draft revision changed",
                409,
                data={"current_revision": doc["revision"]},
            )
        self._validate_posting(conn, doc)
        candidates = self._duplicate_candidates(conn, doc)
        token = str(payload.get("duplicate_confirmation_token") or "")
        semantic = self._semantic_doc(doc)
        watermark = self._global_ledger_watermark(conn)
        if token:
            self._consume_duplicate_token(
                conn, token, actor, document_id, doc["revision"], semantic, candidates, watermark
            )
        elif candidates:
            duplicate_token = _id("fldc")
            conn.execute(
                "INSERT INTO finance_liquidity_duplicate_tokens(token,actor,document_id,document_revision,semantic_digest,candidate_digest,ledger_watermark,created_at,consumed_at) VALUES(?,?,?,?,?,?,?,?,NULL)",
                (
                    duplicate_token,
                    actor,
                    document_id,
                    doc["revision"],
                    semantic,
                    _digest(candidates),
                    watermark,
                    _now(),
                ),
            )
            return {
                "action_required": "duplicate_confirmation",
                "duplicate_confirmation_token": duplicate_token,
                "candidates": candidates,
            }
        if doc["document_type"] == "opening":
            active = conn.execute(
                "SELECT 1 FROM finance_liquidity_opening_anchors WHERE account_id=? AND is_active=1",
                (doc["target_account_id"],),
            ).fetchone()
            if active:
                raise FinanceCashError(
                    "opening_already_exists",
                    "Use replace-opening to correct an opening",
                    409,
                )
            conn.execute(
                "INSERT INTO finance_liquidity_opening_anchors(anchor_id,account_id,opening_document_id,is_active,cutover_at,created_at) VALUES(?,?,?,?,?,?)",
                (
                    _id("floa"),
                    doc["target_account_id"],
                    document_id,
                    1,
                    doc["occurred_at"],
                    _now(),
                ),
            )
        effects, phase, transfer_state = self._posting_effects(doc)
        tx_ids = self._seal_effect(
            conn,
            operation_id=operation_id,
            document_id=document_id,
            effective_at=doc["occurred_at"],
            phase=phase,
            effects=effects,
        )
        conn.execute(
            "UPDATE finance_liquidity_documents SET status='posted',transfer_state=?,revision=revision+1,posted_at=?,semantic_digest=? WHERE document_id=? AND revision=?",
            (transfer_state, _now(), semantic, document_id, doc["revision"]),
        )
        self._audit(
            conn, actor, "document.posted", document_id, {"transaction_ids": tx_ids}
        )
        response = self._posted_response(
            conn,
            doc,
            document_id,
            tx_ids,
            "initialization_only" if not effects else "posted",
        )
        return response

    def transfer_transition(
        self,
        document_id: str,
        action: str,
        payload: Mapping[str, Any],
        actor: str,
        operation_id: str,
        key: str,
    ) -> dict[str, Any]:
        return self._command(
            f"transfer.{action}:{document_id}",
            key,
            operation_id,
            actor,
            payload,
            lambda conn: self._transfer_transition_tx(
                conn, document_id, action, payload, actor, operation_id
            ),
        )

    def _transfer_transition_tx(
        self,
        conn: sqlite3.Connection,
        document_id: str,
        action: str,
        payload: Mapping[str, Any],
        actor: str,
        operation_id: str,
    ) -> dict[str, Any]:
        doc = self._document(conn, document_id)
        if (
            doc["document_type"] != "transfer"
            or doc["transfer_mode"] != "two_phase"
            or doc["transfer_state"] != "in_transit"
        ):
            raise FinanceCashError(
                "transfer_not_in_transit", "Transfer is not in transit", 409
            )
        if int(payload.get("base_revision", -1)) != int(doc["revision"]):
            raise FinanceCashError("version_conflict", "Transfer revision changed", 409)
        amount = int(doc["amount_minor"])
        occurred_at = _utc_timestamp(payload.get("occurred_at"), "occurred_at")
        if not occurred_at or occurred_at < doc["occurred_at"]:
            raise FinanceCashError(
                "invalid_transfer_date",
                "Completion/cancel date must be no earlier than send",
                422,
            )
        if action == "complete":
            effects, state, phase = (
                [
                    (f"asset:{doc['target_account_id']}", "debit", amount),
                    (f"system:transit:{doc['currency']}", "credit", amount),
                ],
                "completed",
                "transfer_complete",
            )
        elif action == "cancel":
            effects, state, phase = (
                [
                    (f"asset:{doc['source_account_id']}", "debit", amount),
                    (f"system:transit:{doc['currency']}", "credit", amount),
                ],
                "cancelled",
                "transfer_cancel",
            )
        else:
            raise FinanceCashError(
                "invalid_transfer_action", "Unsupported transfer action"
            )
        tx_ids = self._seal_effect(
            conn,
            operation_id=operation_id,
            document_id=document_id,
            effective_at=occurred_at,
            phase=phase,
            effects=effects,
        )
        self._seal_operation_effect(conn, operation_id, document_id)
        cursor = conn.execute(
            "UPDATE finance_liquidity_documents SET transfer_state=?,revision=revision+1,updated_at=? WHERE document_id=? AND revision=? AND transfer_state='in_transit'",
            (state, _now(), document_id, doc["revision"]),
        )
        if cursor.rowcount != 1:
            raise FinanceCashError("version_conflict", "Transfer state changed", 409)
        self._audit(
            conn, actor, f"transfer.{action}", document_id, {"transaction_ids": tx_ids}
        )
        return self._posted_response(conn, doc, document_id, tx_ids, state)

    def reverse_document(
        self,
        document_id: str,
        payload: Mapping[str, Any],
        actor: str,
        operation_id: str,
        key: str,
    ) -> dict[str, Any]:
        return self._command(
            f"document.reverse:{document_id}",
            key,
            operation_id,
            actor,
            payload,
            lambda conn: self._reverse_document_tx(conn, document_id, payload, actor, operation_id),
        )

    def _reverse_document_tx(
        self,
        conn: sqlite3.Connection,
        document_id: str,
        payload: Mapping[str, Any],
        actor: str,
        operation_id: str,
    ) -> dict[str, Any]:
        original = self._document(conn, document_id)
        reason = str(payload.get("reason") or "").strip()
        occurred_at = _utc_timestamp(payload.get("occurred_at"), "occurred_at")
        if (
            original["status"] != "posted"
            or original["document_type"] == "opening"
            or original["transfer_state"] in {"in_transit", "cancelled"}
            or original["reversal_of_document_id"] is not None
        ):
            raise FinanceCashError(
                "document_not_reversible",
                "Only an unchanged completed non-opening document can be reversed",
                409,
            )
        if int(payload.get("base_revision", -1)) != int(original["revision"]):
            raise FinanceCashError("version_conflict", "Document revision changed", 409)
        self._assert_ledger_integrity(conn)
        original_transactions = list(
            conn.execute(
                "SELECT transaction_id,phase,effective_at "
                "FROM finance_liquidity_ledger_transactions "
                "WHERE document_id=? ORDER BY sequence_no DESC",
                (document_id,),
            )
        )
        if not original_transactions:
            raise FinanceCashError("document_not_reversible", "Document has no sealed effects", 409)
        latest_original_effect = max(
            str(transaction["effective_at"]) for transaction in original_transactions
        )
        if not reason or not occurred_at or occurred_at < latest_original_effect:
            raise FinanceCashError(
                "invalid_reversal",
                "Reason and a date no earlier than the latest original phase are required",
            )
        reversal_id, now = _id("fld"), _now()
        conn.execute(
            """INSERT INTO finance_liquidity_documents(document_id,document_type,status,source_account_id,target_account_id,category_id,amount_minor,occurred_at,purpose,reversal_of_document_id,revision,created_at,updated_at,posted_at,semantic_digest,actor) VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?)""",
            (
                reversal_id,
                original["document_type"],
                "posted",
                original["source_account_id"],
                original["target_account_id"],
                original["category_id"],
                original["amount_minor"],
                occurred_at,
                f"Reversal: {reason}",
                document_id,
                now,
                now,
                now,
                _digest({"reversal_of": document_id, "reason": reason}),
                actor,
            ),
        )
        tx_ids: list[str] = []
        # A completed two-phase transfer is reversed in the inverse phase
        # order.  Each original transaction gets its own linked transaction;
        # collapsing them loses the transit history and breaks reconciliation.
        for original_tx in original_transactions:
            entries = conn.execute(
                "SELECT ledger_account_id,side,amount_minor FROM finance_liquidity_ledger_entries "
                "WHERE transaction_id=? ORDER BY line_no",
                (original_tx["transaction_id"],),
            ).fetchall()
            tx_ids += self._seal_effect(
                conn,
                operation_id=operation_id,
                document_id=reversal_id,
                effective_at=occurred_at,
                phase=f"reversal:{original_tx['phase']}",
                effects=[
                    (
                        entry["ledger_account_id"],
                        "credit" if entry["side"] == "debit" else "debit",
                        int(entry["amount_minor"]),
                    )
                    for entry in entries
                ],
            )
        self._seal_operation_effect(conn, operation_id, reversal_id)
        conn.execute(
            "UPDATE finance_liquidity_documents SET status='reversed',revision=revision+1,updated_at=? WHERE document_id=? AND revision=?",
            (_now(), document_id, original["revision"]),
        )
        self._audit(
            conn,
            actor,
            "document.reversed",
            reversal_id,
            {"reversal_of": document_id, "reason": reason},
        )
        return self._posted_response(
            conn,
            self._document(conn, reversal_id),
            reversal_id,
            tx_ids,
            "reversal",
            extra={"reversal_of_document_id": document_id},
        )

    def replace_opening(
        self,
        document_id: str,
        payload: Mapping[str, Any],
        actor: str,
        operation_id: str,
        key: str,
    ) -> dict[str, Any]:
        return self._command(
            f"opening.replace:{document_id}",
            key,
            operation_id,
            actor,
            payload,
            lambda conn: self._replace_opening_tx(conn, document_id, payload, actor, operation_id),
        )

    def _replace_opening_tx(
        self,
        conn: sqlite3.Connection,
        document_id: str,
        payload: Mapping[str, Any],
        actor: str,
        operation_id: str,
    ) -> dict[str, Any]:
        old = self._document(conn, document_id)
        if (
            old["document_type"] != "opening"
            or old["status"] != "posted"
            or old["reversal_of_document_id"] is not None
        ):
            raise FinanceCashError(
                "opening_not_replaceable", "Posted opening required", 409
            )
        if int(payload.get("base_revision", -1)) != int(old["revision"]):
            raise FinanceCashError("version_conflict", "Opening revision changed", 409)
        anchor = conn.execute(
            "SELECT * FROM finance_liquidity_opening_anchors WHERE account_id=? AND is_active=1",
            (old["target_account_id"],),
        ).fetchone()
        if anchor is None:
            raise FinanceCashError(
                "opening_not_replaceable", "Opening anchor is unavailable", 409
            )
        amount = money_from_api(
            payload.get("amount"),
            self._account(conn, old["target_account_id"])["currency"],
        )
        occurred = _utc_timestamp(payload.get("occurred_at"), "occurred_at")
        evidence_type = str(payload.get("opening_evidence_type") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        if evidence_type != "manual_confirmation" or not reason:
            raise FinanceCashError(
                "invalid_opening",
                "Manual confirmation and a replacement explanation are required",
            )
        if occurred < anchor["cutover_at"]:
            raise FinanceCashError(
                "backdated_before_cutover", "Replacement is before the opening boundary", 422
            )
        reversal_id, replacement_id, now = _id("fld"), _id("fld"), _now()
        evidence_digest = _digest(
            {
                "kind": "manual_opening_replacement",
                "actor": actor,
                "account_id": old["target_account_id"],
                "amount_minor": amount,
                "occurred_at": occurred,
                "reason": reason,
                "comment": payload.get("opening_evidence_ref"),
                "document_id": replacement_id,
                "replaces": document_id,
            }
        )
        conn.execute(
            """INSERT INTO finance_liquidity_documents(document_id,document_type,status,target_account_id,amount_minor,occurred_at,purpose,reversal_of_document_id,revision,created_at,updated_at,posted_at,semantic_digest,actor) VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?,?)""",
            (
                reversal_id,
                "opening",
                "posted",
                old["target_account_id"],
                old["amount_minor"],
                occurred,
                f"Opening reversal: {reason}",
                document_id,
                now,
                now,
                now,
                _digest({"reversal_of": document_id, "reason": reason}),
                actor,
            ),
        )
        conn.execute(
            """INSERT INTO finance_liquidity_documents(document_id,document_type,status,target_account_id,amount_minor,occurred_at,replaces_opening_document_id,opening_evidence_type,opening_evidence_digest,opening_evidence_ref,revision,created_at,updated_at,posted_at,semantic_digest,actor) VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?)""",
            (
                replacement_id,
                "opening",
                "posted",
                old["target_account_id"],
                amount,
                occurred,
                document_id,
                evidence_type,
                evidence_digest,
                payload.get("opening_evidence_ref"),
                now,
                now,
                now,
                _digest({"replacement_of": document_id, "reason": reason}),
                actor,
            ),
        )
        old_effects, _, _ = self._posting_effects(old)
        reversal = [
            (account, "credit" if side == "debit" else "debit", value)
            for account, side, value in old_effects
        ]
        new_doc = self._document(conn, replacement_id)
        new_effects, _, _ = self._posting_effects(new_doc)
        tx_ids = self._seal_effect(
            conn,
            operation_id=operation_id,
            document_id=reversal_id,
            effective_at=occurred,
            phase="reversal",
            effects=reversal,
        ) + self._seal_effect(
            conn,
            operation_id=operation_id,
            document_id=replacement_id,
            effective_at=occurred,
            phase="replacement_opening",
            effects=new_effects,
        )
        self._seal_operation_effect(conn, operation_id, replacement_id)
        conn.execute(
            "UPDATE finance_liquidity_documents SET status='reversed',revision=revision+1,updated_at=? WHERE document_id=? AND revision=?",
            (_now(), document_id, old["revision"]),
        )
        # The anchor is lifelong evidence of the first known cutover.  The
        # linked reversal and replacement carry corrections; they never rewrite
        # the historical boundary or the anchor's original document identity.
        self._audit(
            conn,
            actor,
            "opening.replaced",
            replacement_id,
            {"reversal_of": document_id, "transaction_ids": tx_ids},
        )
        return self._posted_response(
            conn,
            new_doc,
            replacement_id,
            tx_ids,
            "replaced",
            extra={
                "reversal_document_id": reversal_id,
                "replaces_opening_document_id": document_id,
                "anchor_id": anchor["anchor_id"],
            },
        )

    def record_reconciliation(
        self, payload: Mapping[str, Any], actor: str, operation_id: str, key: str
    ) -> dict[str, Any]:
        return self._command(
            f"cash.reconciliation.record:{str(payload.get('account_id') or '')}",
            key,
            operation_id,
            actor,
            payload,
            lambda conn: self._record_reconciliation_tx(conn, payload, actor, operation_id),
        )

    def _record_reconciliation_tx(
        self, conn: sqlite3.Connection, payload: Mapping[str, Any], actor: str, operation_id: str
    ) -> dict[str, Any]:
        account = self._account(conn, str(payload.get("account_id") or ""))
        if account["account_type"] != "cash":
            raise FinanceCashError(
                "not_cash_account", "Reconciliation is for cash accounts", 422
            )
        # checked_at is the server audit instant, never an operator assertion.
        checked_at = _now()
        week = str(payload.get("week_ending") or "").strip()
        balance_as_of = _business_day_end(week)
        initialized = self._account_view(conn, account, balance_as_of)
        if initialized["balance_state"] == "uninitialized":
            raise FinanceCashError(
                "account_uninitialized",
                "Account needs an opening by the selected business day",
                422,
            )
        actual = money_from_api(payload.get("actual_amount"), account["currency"])
        expected = self._balance(conn, account["account_id"], balance_as_of)
        difference = actual - expected
        if abs(difference) > MAX_MONEY_MINOR:
            raise FinanceCashError(
                "reconciliation_difference_out_of_range",
                "Reconciliation difference exceeds the supported exact range",
                422,
            )
        comment = str(payload.get("comment") or "").strip() or None
        if difference != 0 and not comment:
            raise FinanceCashError(
                "reconciliation_explanation_required",
                "A discrepancy explanation is required",
                422,
            )
        ledger_watermark, ledger_digest = self._ledger_snapshot(
            conn, account["account_id"], balance_as_of
        )
        reconciliation_id, now = _id("flr"), _now()
        conn.execute(
            "INSERT INTO finance_liquidity_cash_reconciliations(reconciliation_id,account_id,week_ending,balance_as_of,business_timezone,ledger_watermark,ledger_digest,expected_minor,actual_minor,difference_minor,status,checked_at,comment,actor,created_at,revision,record_operation_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                reconciliation_id,
                account["account_id"],
                week,
                balance_as_of,
                CANONICAL_BUSINESS_TIMEZONE_NAME,
                ledger_watermark,
                ledger_digest,
                expected,
                actual,
                difference,
                "matched" if difference == 0 else "discrepancy",
                checked_at,
                comment,
                actor,
                now,
                1,
                operation_id,
            ),
        )
        self._audit(
            conn,
            actor,
            "cash.reconciliation.recorded",
            reconciliation_id,
            {"expected_minor": expected, "actual_minor": actual},
        )
        return {
            "reconciliation_id": reconciliation_id,
            "week_ending": week,
            "balance_as_of": balance_as_of,
            "business_timezone": CANONICAL_BUSINESS_TIMEZONE_NAME,
            "ledger_watermark": ledger_watermark,
            "ledger_digest": ledger_digest,
            "checked_at": checked_at,
            "expected_minor": expected,
            "actual_minor": actual,
            "difference_minor": difference,
            "status": "matched" if difference == 0 else "discrepancy",
        }

    def resolve_reconciliation(
        self,
        reconciliation_id: str,
        payload: Mapping[str, Any],
        actor: str,
        is_admin: bool,
        operation_id: str,
        key: str,
    ) -> dict[str, Any]:
        return self._command(
            f"cash.reconciliation.resolve:{reconciliation_id}",
            key,
            operation_id,
            actor,
            payload,
            lambda conn: self._resolve_reconciliation_tx(
                conn, reconciliation_id, payload, actor, is_admin, operation_id
            ),
        )

    def _resolve_reconciliation_tx(
        self,
        conn: sqlite3.Connection,
        reconciliation_id: str,
        payload: Mapping[str, Any],
        actor: str,
        is_admin: bool,
        operation_id: str,
    ) -> dict[str, Any]:
        original = conn.execute(
            "SELECT * FROM finance_liquidity_cash_reconciliations WHERE reconciliation_id=?",
            (reconciliation_id,),
        ).fetchone()
        if original is None:
            raise FinanceCashError(
                "reconciliation_not_found", "Reconciliation not found", 404
            )
        if original["status"] != "discrepancy":
            raise FinanceCashError(
                "reconciliation_not_resolvable",
                "Only a discrepancy can be resolved",
                409,
            )
        if int(payload.get("base_revision", -1)) != int(original["revision"]):
            raise FinanceCashError("version_conflict", "Reconciliation revision changed", 409)
        matched_id, reason = (
            str(payload.get("matched_reconciliation_id") or ""),
            str(payload.get("override_reason") or "").strip(),
        )
        if matched_id:
            follow = conn.execute(
                "SELECT * FROM finance_liquidity_cash_reconciliations WHERE reconciliation_id=?",
                (matched_id,),
            ).fetchone()
            if (
                follow is None
                or follow["account_id"] != original["account_id"]
                or follow["status"] != "matched"
                or follow["checked_at"] <= original["checked_at"]
                or not reason
            ):
                raise FinanceCashError(
                    "invalid_reconciliation_resolution",
                    "A later matched reconciliation and explanation are required",
                )
            kind, link = "matched_followup", matched_id
        elif is_admin and reason:
            kind, link = "admin_override", None
        else:
            raise FinanceCashError(
                "invalid_reconciliation_resolution",
                "A later match or admin override reason is required",
                422,
            )
        conn.execute(
            "UPDATE finance_liquidity_cash_reconciliations SET status='resolved',revision=revision+1,resolution_operation_id=?,resolution_kind=?,resolution_reconciliation_id=?,resolution_reason=?,resolved_at=?,resolved_by=? WHERE reconciliation_id=? AND status='discrepancy' AND revision=?",
            (operation_id, kind, link, reason or None, _now(), actor, reconciliation_id, original["revision"]),
        )
        self._audit(
            conn,
            actor,
            "cash.reconciliation.resolved",
            reconciliation_id,
            {"kind": kind, "linked": link},
        )
        return {
            "reconciliation_id": reconciliation_id,
            "status": "resolved",
            "resolution_kind": kind,
            "resolution_reconciliation_id": link,
        }

    def list_reconciliations(
        self, account_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            self._assert_ledger_integrity(conn)
            sql = (
                "SELECT r.*,a.currency FROM finance_liquidity_cash_reconciliations r "
                "JOIN finance_liquidity_accounts a ON a.account_id=r.account_id"
            )
            args: list[Any] = []
            if account_id:
                sql += " WHERE r.account_id=?"
                args.append(account_id)
            return [
                self._reconciliation_view(item)
                for item in conn.execute(
                    sql + " ORDER BY r.checked_at DESC,r.reconciliation_id DESC", args
                )
            ]

    def get_document(self, document_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            self._assert_ledger_integrity(conn)
            doc = self._document(conn, document_id)
            transactions = [
                _row(row)
                for row in conn.execute(
                    "SELECT * FROM finance_liquidity_ledger_transactions WHERE document_id=? ORDER BY sequence_no",
                    (document_id,),
                )
            ]
            for item in transactions:
                item["entries"] = [
                    self._entry_view(_row(row), str(doc["currency"]))
                    for row in conn.execute(
                        "SELECT * FROM finance_liquidity_ledger_entries WHERE transaction_id=? ORDER BY line_no",
                        (item["transaction_id"],),
                    )
                ]
            return {
                "document": self._document_view(doc),
                "transactions": transactions,
            }

    def list_documents(self, *, account_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            self._assert_ledger_integrity(conn)
            sql = (
                "SELECT d.*, COALESCE(a.currency,b.currency,'RUB') AS currency "
                "FROM finance_liquidity_documents d "
                "LEFT JOIN finance_liquidity_accounts a ON a.account_id=d.source_account_id "
                "LEFT JOIN finance_liquidity_accounts b ON b.account_id=d.target_account_id"
            )
            args: list[Any] = []
            if account_id:
                sql += " WHERE source_account_id=? OR target_account_id=?"
                args = [account_id, account_id]
            return [
                self._document_view(row)
                for row in conn.execute(
                    sql
                    + " ORDER BY COALESCE(d.posted_at,d.created_at) DESC,d.document_id DESC",
                    args,
                )
            ]

    def movements(self, account_id: str, as_of: str | None = None) -> dict[str, Any]:
        with self._connect() as conn:
            if as_of is not None:
                as_of = _utc_timestamp(as_of, "as_of")
            self._assert_ledger_integrity(conn)
            account = self._account(conn, account_id, active=False)
            view = self._account_view(conn, account, as_of)
            if view["balance_state"] == "uninitialized":
                return {
                    "account_id": account_id,
                    "as_of": as_of,
                    "balance_state": "uninitialized",
                    "balance_minor": None,
                    "balance": None,
                    "movements": [],
                }
            balance = self._balance(conn, account_id, as_of)
            sql = """SELECT t.transaction_id,t.document_id,t.phase,t.effective_at,e.side,e.amount_minor FROM finance_liquidity_ledger_entries e JOIN finance_liquidity_ledger_transactions t ON t.transaction_id=e.transaction_id JOIN finance_liquidity_ledger_transaction_seals s ON s.transaction_id=t.transaction_id JOIN finance_liquidity_effect_set_seals os ON os.operation_id=t.origin_operation_id WHERE e.ledger_account_id=?"""
            args = [f"asset:{account_id}"]
            if as_of:
                sql += " AND t.effective_at<=?"
                args.append(as_of)
            sql += " ORDER BY t.effective_at,t.sequence_no,e.line_no"
            return {
                "account_id": account_id,
                "as_of": as_of,
                "balance_state": "current",
                "balance_minor": balance,
                "balance": money_to_api(balance, account["currency"]),
                "movements": [_row(item) for item in conn.execute(sql, args)],
            }

    def get_operation(
        self, operation_id: str, actor: str, is_admin: bool
    ) -> dict[str, Any]:
        with self._connect() as conn:
            record = conn.execute(
                "SELECT * FROM finance_liquidity_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if record is None:
                raise FinanceCashError(
                    "operation_not_found", "Operation not found", 404
                )
            if record["actor"] != actor and not is_admin:
                raise FinanceCashError(
                    "operation_forbidden", "Operation belongs to another actor", 403
                )
            if record["effect_root_document_id"] is not None:
                self._assert_ledger_integrity(conn)
            return json.loads(record["result_json"])

    def _seal_operation_effect(
        self,
        conn: sqlite3.Connection,
        operation_id: str,
        root_document_id: str,
    ) -> list[str]:
        canonical_transactions = [
            _row(row)
            for row in conn.execute(
                "SELECT transaction_id,document_id,phase,effective_at "
                "FROM finance_liquidity_ledger_transactions "
                "WHERE origin_operation_id=? ORDER BY sequence_no",
                (operation_id,),
            )
        ]
        transaction_ids = [
            str(transaction["transaction_id"]) for transaction in canonical_transactions
        ]
        operation = conn.execute(
            "SELECT effect_root_document_id FROM finance_liquidity_operations "
            "WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if operation is None:
            raise FinanceCashError(
                "ledger_integrity_unavailable", "Money operation is unavailable", 500
            )
        existing = conn.execute(
            "SELECT root_document_id,transaction_count,transactions_digest "
            "FROM finance_liquidity_effect_set_seals WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        digest = _digest(canonical_transactions)
        if existing is None:
            if operation["effect_root_document_id"] is not None:
                raise FinanceCashError(
                    "ledger_integrity_unavailable",
                    "Money operation receipt is incomplete",
                    500,
                )
            conn.execute(
                "UPDATE finance_liquidity_operations SET effect_root_document_id=? "
                "WHERE operation_id=? AND effect_root_document_id IS NULL",
                (root_document_id, operation_id),
            )
            conn.execute(
                "INSERT INTO finance_liquidity_effect_set_seals("
                "operation_id,root_document_id,transaction_count,transactions_digest,sealed_at"
                ") VALUES(?,?,?,?,?)",
                (
                    operation_id,
                    root_document_id,
                    len(transaction_ids),
                    digest,
                    _now(),
                ),
            )
        elif (
            operation["effect_root_document_id"] != root_document_id
            or existing["root_document_id"] != root_document_id
            or int(existing["transaction_count"]) != len(transaction_ids)
            or existing["transactions_digest"] != digest
        ):
            raise FinanceCashError(
                "ledger_integrity_unavailable", "Money operation receipt changed", 500
            )
        return transaction_ids

    def _command(
        self,
        scope: str,
        key: str,
        operation_id: str,
        actor: str,
        payload: Mapping[str, Any],
        action: Any,
    ) -> dict[str, Any]:
        if not key or len(key) > 256 or not operation_id or len(operation_id) > 256:
            raise FinanceCashError(
                "invalid_idempotency_key",
                "Operation and idempotency identities are required",
            )
        request_digest = _digest({"actor": actor, "scope": scope, "payload": payload})
        with self._connect(write=True) as conn:
            existing = conn.execute(
                "SELECT request_digest,result_json,effect_root_document_id FROM finance_liquidity_operations WHERE actor=? AND scope=? AND idempotency_key=?",
                (actor, scope, key),
            ).fetchone()
            by_operation = conn.execute(
                "SELECT scope,request_digest,result_json,effect_root_document_id FROM finance_liquidity_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if existing:
                if existing["request_digest"] != request_digest:
                    raise FinanceCashError(
                        "idempotency_conflict",
                        "Idempotency key was used with another body",
                        409,
                    )
                if existing["effect_root_document_id"] is not None:
                    self._assert_ledger_integrity(conn)
                return json.loads(existing["result_json"])
            if by_operation:
                if (
                    by_operation["scope"] != scope
                    or by_operation["request_digest"] != request_digest
                ):
                    raise FinanceCashError(
                        "operation_conflict",
                        "Operation identity was used with another command",
                        409,
                    )
                if by_operation["effect_root_document_id"] is not None:
                    self._assert_ledger_integrity(conn)
                return json.loads(by_operation["result_json"])
            # The durable operation exists before any ledger row.  This removes
            # the old pending rebinding window and gives every sealed effect a
            # concrete, immutable owner from its first insert.
            conn.execute(
                "INSERT INTO finance_liquidity_operations(operation_id,scope,idempotency_key,request_digest,actor,effect_root_document_id,result_json,created_at) VALUES(?,?,?,?,?,NULL,?,?)",
                (operation_id, scope, key, request_digest, actor, "{}", _now()),
            )
            conn.create_function(
                "finance_internal_operation_id", 0, lambda: operation_id
            )
            try:
                result = dict(action(conn))
            finally:
                conn.create_function(
                    "finance_internal_operation_id", 0, lambda: None
                )
            result.update({"operation_id": operation_id, "receipt_id": _id("flop")})
            txids = [
                str(row[0])
                for row in conn.execute(
                    "SELECT transaction_id FROM finance_liquidity_ledger_transactions WHERE origin_operation_id=? ORDER BY sequence_no",
                    (operation_id,),
                )
            ]
            is_effect_operation = bool(txids) or (
                scope.startswith("document.post:")
                and result.get("effect_kind") == "initialization_only"
            ) or scope.startswith("opening.replace:")
            if is_effect_operation:
                root_document_id = str(result.get("document_id") or "")
                if not root_document_id:
                    raise FinanceCashError(
                        "ledger_integrity_unavailable", "Money operation lacks a root document", 500
                    )
                if self._seal_operation_effect(
                    conn, operation_id, root_document_id
                ) != txids:
                    raise FinanceCashError(
                        "ledger_integrity_unavailable",
                        "Money operation transaction order changed",
                        500,
                    )
                # The read-side integrity check also runs while assembling
                # balances.  Persist the immutable receipt skeleton first so
                # it can validate this just-sealed command rather than seeing
                # the temporary `{}` admission placeholder.
                conn.execute(
                    "UPDATE finance_liquidity_operations SET result_json=? WHERE operation_id=?",
                    (_canon(result), operation_id),
                )
                if isinstance(result.get("balance_account_ids"), list):
                    result["balances"] = {
                        account_id: self._account_view(
                            conn, self._account(conn, account_id)
                        )
                        for account_id in result.pop("balance_account_ids")
                    }
            conn.execute(
                "UPDATE finance_liquidity_operations SET result_json=? WHERE operation_id=?",
                (_canon(result), operation_id),
            )
            return result

    def _document(self, conn: sqlite3.Connection, document_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT d.*, COALESCE(a.currency,b.currency,'RUB') AS currency FROM finance_liquidity_documents d LEFT JOIN finance_liquidity_accounts a ON a.account_id=d.source_account_id LEFT JOIN finance_liquidity_accounts b ON b.account_id=d.target_account_id WHERE d.document_id=?",
            (document_id,),
        ).fetchone()
        if row is None:
            raise FinanceCashError("document_not_found", "Document not found", 404)
        return row

    def _document_view(self, document: sqlite3.Row) -> dict[str, Any]:
        result = _row(document) or {}
        currency = str(document["currency"])
        result["currency"] = currency
        amount_minor = document["amount_minor"]
        result["amount"] = (
            money_to_api(int(amount_minor), currency)
            if amount_minor is not None
            else None
        )
        return result

    def _entry_view(
        self, entry: dict[str, Any] | None, currency: str
    ) -> dict[str, Any]:
        result = entry or {}
        result["currency"] = currency
        result["amount"] = money_to_api(int(result["amount_minor"]), currency)
        return result

    def _reconciliation_view(self, reconciliation: sqlite3.Row) -> dict[str, Any]:
        result = _row(reconciliation) or {}
        currency = str(reconciliation["currency"])
        result["currency"] = currency
        for name in ("expected", "actual", "difference"):
            result[f"{name}_amount"] = money_to_api(
                int(reconciliation[f"{name}_minor"]), currency
            )
        return result

    def _ledger_snapshot(
        self, conn: sqlite3.Connection, account_id: str, balance_as_of: str
    ) -> tuple[int, str]:
        balance_as_of = _utc_timestamp(balance_as_of, "balance_as_of")
        self._assert_ledger_integrity(conn)
        rows = [
            _row(row)
            for row in conn.execute(
                "SELECT t.sequence_no,t.transaction_id,t.document_id,t.phase,"
                "t.effective_at,e.line_no,e.ledger_account_id,e.side,e.amount_minor "
                "FROM finance_liquidity_ledger_entries e "
                "JOIN finance_liquidity_ledger_transactions t ON t.transaction_id=e.transaction_id "
                "JOIN finance_liquidity_ledger_transaction_seals s ON s.transaction_id=t.transaction_id "
                "JOIN finance_liquidity_effect_set_seals os ON os.operation_id=t.origin_operation_id "
                "WHERE e.ledger_account_id=? AND t.effective_at<=? "
                "ORDER BY t.sequence_no,e.line_no",
                (f"asset:{account_id}", balance_as_of),
            )
        ]
        return max((int(row["sequence_no"]) for row in rows), default=0), _digest(rows)

    def _document_currency(
        self,
        conn: sqlite3.Connection,
        kind: str,
        payload: Mapping[str, Any],
    ) -> str:
        account_ids = (
            [payload.get("target_account_id")]
            if kind in {"opening", "income"}
            else [payload.get("source_account_id")]
            if kind == "expense"
            else [payload.get("source_account_id"), payload.get("target_account_id")]
        )
        currencies = [
            str(row[0])
            for account_id in account_ids
            if account_id
            if (
                row := conn.execute(
                    "SELECT currency FROM finance_liquidity_accounts WHERE account_id=?",
                    (str(account_id),),
                ).fetchone()
            )
            is not None
        ]
        return currencies[0] if currencies else str(payload.get("currency") or "RUB")

    def _document_fields(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        draft: bool,
        currency: str,
    ) -> dict[str, Any]:
        source = str(payload.get("source_account_id") or "").strip() or None
        target = str(payload.get("target_account_id") or "").strip() or None
        category = str(payload.get("category_id") or "").strip() or None
        amount = payload.get("amount")
        minor = None
        occurred = str(payload.get("occurred_at") or "").strip()
        if amount not in (None, ""):
            minor = money_from_api(amount, currency)
        return {
            "source_account_id": source,
            "target_account_id": target,
            "category_id": category,
            "amount_minor": minor,
            "occurred_at": _utc_timestamp(occurred, "occurred_at")
            if occurred
            else None,
            "purpose": str(payload.get("purpose") or "").strip() or None,
            "negative_balance_explanation": str(
                payload.get("negative_balance_explanation") or ""
            ).strip()
            or None,
            "opening_evidence_type": str(
                payload.get("opening_evidence_type") or ""
            ).strip()
            or None,
            "opening_evidence_digest": str(
                payload.get("opening_evidence_digest") or ""
            ).strip()
            or None,
            "opening_evidence_ref": str(
                payload.get("opening_evidence_ref") or ""
            ).strip()
            or None,
            "transfer_mode": str(payload.get("transfer_mode") or "instant").strip(),
        }

    def _validate_posting(self, conn: sqlite3.Connection, doc: sqlite3.Row) -> None:
        kind, amount, occurred = (
            doc["document_type"],
            doc["amount_minor"],
            doc["occurred_at"],
        )
        if amount is None or not occurred:
            raise FinanceCashError(
                "invalid_document", "Posting requires amount and occurred_at"
            )
        if kind == "opening":
            account = self._account(conn, doc["target_account_id"] or "")
            digest = str(doc["opening_evidence_digest"] or "")
            if (
                doc["source_account_id"]
                or doc["opening_evidence_type"] != "manual_confirmation"
                or len(digest) != 71
                or not digest.startswith("sha256:")
                or any(char not in "0123456789abcdef" for char in digest[7:])
            ):
                raise FinanceCashError(
                    "invalid_opening", "Opening target and evidence are required"
                )
        elif kind in {"income", "expense"}:
            account_id = (
                doc["target_account_id"]
                if kind == "income"
                else doc["source_account_id"]
            )
            account = self._account(conn, account_id or "")
            if int(amount) <= 0 or not doc["purpose"]:
                raise FinanceCashError(
                    "invalid_document", "Amount and purpose are required"
                )
            category = (
                conn.execute(
                    "SELECT * FROM finance_liquidity_categories WHERE category_id=? AND is_active=1",
                    (doc["category_id"],),
                ).fetchone()
                if doc["category_id"]
                else None
            )
            if category is None or category["direction"] != kind:
                raise FinanceCashError(
                    "invalid_category", "Active category direction is required"
                )
            anchor = conn.execute(
                "SELECT cutover_at FROM finance_liquidity_opening_anchors WHERE account_id=? AND is_active=1",
                (account_id,),
            ).fetchone()
            if anchor is None:
                raise FinanceCashError(
                    "account_uninitialized", "Account needs an opening", 422
                )
            if occurred < anchor["cutover_at"]:
                raise FinanceCashError(
                    "backdated_before_cutover", "Movement is before opening", 422
                )
            if (
                kind == "expense"
                and self._balance(conn, account_id, occurred) - int(amount) < 0
                and not doc["negative_balance_explanation"]
            ):
                raise FinanceCashError(
                    "negative_balance_explanation_required",
                    "Negative cash requires an explanation",
                    422,
                )
        elif kind == "transfer":
            source = self._account(conn, doc["source_account_id"] or "")
            target = self._account(conn, doc["target_account_id"] or "")
            if (
                int(amount) <= 0
                or source["account_id"] == target["account_id"]
                or source["currency"] != target["currency"]
                or doc["transfer_mode"] not in {"instant", "two_phase"}
            ):
                raise FinanceCashError(
                    "invalid_transfer",
                    "Valid same-currency source, target, positive amount and mode are required",
                )
            source_anchor = conn.execute(
                    "SELECT cutover_at FROM finance_liquidity_opening_anchors WHERE account_id=? AND is_active=1",
                    (source["account_id"],),
                ).fetchone()
            target_anchor = conn.execute(
                    "SELECT cutover_at FROM finance_liquidity_opening_anchors WHERE account_id=? AND is_active=1",
                    (target["account_id"],),
                ).fetchone()
            if not source_anchor or not target_anchor:
                raise FinanceCashError(
                    "account_uninitialized", "Transfer accounts need openings", 422
                )
            if occurred < source_anchor["cutover_at"] or occurred < target_anchor["cutover_at"]:
                raise FinanceCashError("backdated_before_cutover", "Transfer is before an opening", 422)
            if self._balance(conn, source["account_id"], occurred) - int(amount) < 0 and not doc["negative_balance_explanation"]:
                raise FinanceCashError("negative_balance_explanation_required", "Negative cash requires an explanation", 422)
        else:
            raise FinanceCashError("invalid_document", "Unsupported document type")

    def _posting_effects(
        self, doc: sqlite3.Row
    ) -> tuple[list[tuple[str, str, int]], str, str | None]:
        amount = abs(int(doc["amount_minor"]))
        kind = doc["document_type"]
        currency = doc["currency"]
        if kind == "opening":
            return (
                (
                    []
                    if amount == 0
                    else [
                        (
                            f"asset:{doc['target_account_id']}",
                            "debit" if int(doc["amount_minor"]) > 0 else "credit",
                            amount,
                        ),
                        (
                            f"system:opening:{currency}",
                            "credit" if int(doc["amount_minor"]) > 0 else "debit",
                            amount,
                        ),
                    ]
                ),
                "primary",
                None,
            )
        if kind == "income":
            return (
                [
                    (f"asset:{doc['target_account_id']}", "debit", amount),
                    (f"system:external_inflow:{currency}", "credit", amount),
                ],
                "primary",
                None,
            )
        if kind == "expense":
            return (
                [
                    (f"system:external_outflow:{currency}", "debit", amount),
                    (f"asset:{doc['source_account_id']}", "credit", amount),
                ],
                "primary",
                None,
            )
        if doc["transfer_mode"] == "instant":
            return (
                [
                    (f"asset:{doc['target_account_id']}", "debit", amount),
                    (f"asset:{doc['source_account_id']}", "credit", amount),
                ],
                "primary",
                "completed",
            )
        return (
            [
                (f"system:transit:{currency}", "debit", amount),
                (f"asset:{doc['source_account_id']}", "credit", amount),
            ],
            "transfer_send",
            "in_transit",
        )

    def _seal_effect(
        self,
        conn: sqlite3.Connection,
        *,
        operation_id: str,
        document_id: str,
        effective_at: str,
        phase: str,
        effects: list[tuple[str, str, int]],
    ) -> list[str]:
        if not effects:
            return []
        transaction_id = _id("fltx")
        conn.execute(
            "INSERT INTO finance_liquidity_ledger_transactions(transaction_id,document_id,origin_operation_id,phase,effective_at) VALUES(?,?,?,?,?)",
            (transaction_id, document_id, operation_id, phase, effective_at),
        )
        for index, (account, side, amount) in enumerate(effects, 1):
            conn.execute(
                "INSERT INTO finance_liquidity_ledger_entries(entry_id,transaction_id,line_no,ledger_account_id,side,amount_minor) VALUES(?,?,?,?,?,?)",
                (_id("fle"), transaction_id, index, account, side, amount),
            )
        canonical = [
            _row(item)
            for item in conn.execute(
                "SELECT line_no,ledger_account_id,side,amount_minor FROM finance_liquidity_ledger_entries WHERE transaction_id=? ORDER BY line_no",
                (transaction_id,),
            )
        ]
        if sum(
            item["amount_minor"] for item in canonical if item["side"] == "debit"
        ) != sum(
            item["amount_minor"] for item in canonical if item["side"] == "credit"
        ):
            raise FinanceCashError(
                "ledger_unbalanced", "Ledger transaction is unbalanced", 500
            )
        conn.execute(
            "INSERT INTO finance_liquidity_ledger_transaction_seals(transaction_id,entry_count,entries_digest,sealed_at) VALUES(?,?,?,?)",
            (transaction_id, len(canonical), _digest(canonical), _now()),
        )
        return [transaction_id]

    def _duplicate_candidates(
        self, conn: sqlite3.Connection, doc: sqlite3.Row
    ) -> list[dict[str, Any]]:
        if doc["document_type"] not in {"income", "expense", "transfer"}:
            return []
        business_day_start, business_day_end = self._duplicate_business_day_bounds(
            str(doc["occurred_at"])
        )
        rows = conn.execute(
            """SELECT d.document_id,d.status,d.semantic_digest,d.occurred_at,
                   COALESCE(MAX(t.sequence_no),0) AS latest_sequence_no
               FROM finance_liquidity_documents d
               LEFT JOIN finance_liquidity_accounts a ON a.account_id=d.source_account_id
               LEFT JOIN finance_liquidity_accounts b ON b.account_id=d.target_account_id
               LEFT JOIN finance_liquidity_ledger_transactions t ON t.document_id=d.document_id
               WHERE d.status IN ('posted','reversed')
                 AND d.reversal_of_document_id IS NULL
                 AND d.document_type=? AND d.amount_minor=?
                 AND COALESCE(d.source_account_id,'')=COALESCE(?, '')
                 AND COALESCE(d.target_account_id,'')=COALESCE(?, '')
                 AND COALESCE(a.currency,b.currency)=?
                 AND d.occurred_at>=? AND d.occurred_at<?
               GROUP BY d.document_id
               ORDER BY latest_sequence_no DESC,d.document_id ASC
               LIMIT 20""",
            (
                doc["document_type"],
                doc["amount_minor"],
                doc["source_account_id"],
                doc["target_account_id"],
                doc["currency"],
                business_day_start,
                business_day_end,
            ),
        ).fetchall()
        return [
            {
                "document_id": row["document_id"],
                "status": row["status"],
                "semantic_digest": row["semantic_digest"],
            }
            for row in rows
        ]

    def _duplicate_business_day_bounds(self, occurred_at: str) -> tuple[str, str]:
        instant = datetime.fromisoformat(
            _utc_timestamp(occurred_at, "occurred_at")[:-1] + "+00:00"
        )
        local_start = datetime.combine(
            instant.astimezone(CANONICAL_BUSINESS_TIMEZONE).date(),
            time.min,
            tzinfo=CANONICAL_BUSINESS_TIMEZONE,
        )
        return (
            local_start.astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
            (local_start + timedelta(days=1))
            .astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
        )

    def _consume_duplicate_token(
        self,
        conn: sqlite3.Connection,
        token: str,
        actor: str,
        doc_id: str,
        revision: int,
        semantic: str,
        candidates: list[dict[str, Any]],
        watermark: int,
    ) -> bool:
        if not token:
            return False
        row = conn.execute(
            "SELECT * FROM finance_liquidity_duplicate_tokens WHERE token=?", (token,)
        ).fetchone()
        if (
            row is None
            or row["consumed_at"]
            or row["actor"] != actor
            or row["document_id"] != doc_id
            or row["document_revision"] != revision
            or row["semantic_digest"] != semantic
            or row["candidate_digest"] != _digest(candidates)
            or int(row["ledger_watermark"]) != watermark
        ):
            raise FinanceCashError(
                "duplicate_confirmation_stale", "Duplicate confirmation is stale", 409
            )
        conn.execute(
            "UPDATE finance_liquidity_duplicate_tokens SET consumed_at=? WHERE token=? AND consumed_at IS NULL",
            (_now(), token),
        )
        return True

    def _global_ledger_watermark(self, conn: sqlite3.Connection) -> int:
        self._assert_ledger_integrity(conn)
        return int(
            conn.execute(
                "SELECT COALESCE(MAX(sequence_no),0) FROM finance_liquidity_ledger_transactions"
            ).fetchone()[0]
        )

    def _semantic_doc(self, doc: sqlite3.Row) -> str:
        return _digest(
            {
                key: doc[key]
                for key in (
                    "document_type",
                    "source_account_id",
                    "target_account_id",
                    "category_id",
                    "amount_minor",
                    "occurred_at",
                    "purpose",
                    "transfer_mode",
                )
            }
        )

    def _audit(
        self,
        conn: sqlite3.Connection,
        actor: str,
        event: str,
        object_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        conn.execute(
            "INSERT INTO finance_liquidity_audit_events(event_id,actor,event_type,object_id,payload_digest,created_at) VALUES(?,?,?,?,?,?)",
            (_id("flae"), actor, event, object_id, _digest(payload), _now()),
        )

    def _posted_response(
        self,
        conn: sqlite3.Connection,
        doc: sqlite3.Row,
        document_id: str,
        txids: list[str],
        kind: str,
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        affected = [
            item
            for item in (doc["source_account_id"], doc["target_account_id"])
            if item
        ]
        result = {
            "document_id": document_id,
            "financial_effect": bool(txids),
            "effect_kind": kind,
            "ledger_transaction_ids": txids,
            "balance_account_ids": list(dict.fromkeys(affected)),
        }
        result.update(extra or {})
        return result


_SCHEMA = """
CREATE TABLE finance_liquidity_schema_meta(singleton INTEGER PRIMARY KEY CHECK(singleton=1),schema_version INTEGER NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE finance_liquidity_accounts(account_id TEXT PRIMARY KEY,name TEXT NOT NULL,account_type TEXT NOT NULL CHECK(account_type IN ('cash','bank')),currency TEXT NOT NULL CHECK(currency GLOB '[A-Z][A-Z][A-Z]'),currency_exponent INTEGER NOT NULL CHECK(currency_exponent BETWEEN 0 AND 9),responsible_name TEXT,is_active INTEGER NOT NULL CHECK(is_active IN(0,1)),revision INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL, CHECK(account_type!='cash' OR responsible_name IS NOT NULL));
CREATE TABLE finance_liquidity_categories(category_id TEXT PRIMARY KEY,name TEXT NOT NULL,direction TEXT NOT NULL CHECK(direction IN('income','expense')),posting_class TEXT,is_active INTEGER NOT NULL,created_at TEXT NOT NULL,CHECK((direction='income' AND posting_class IS NULL) OR (direction='expense' AND posting_class IS NOT NULL AND posting_class IN('external_outflow','fee'))));
CREATE TABLE finance_liquidity_documents(document_id TEXT PRIMARY KEY,document_type TEXT NOT NULL CHECK(document_type IN('opening','income','expense','transfer')),status TEXT NOT NULL CHECK(status IN('draft','posted','reversed')),transfer_mode TEXT,transfer_state TEXT,source_account_id TEXT REFERENCES finance_liquidity_accounts(account_id),target_account_id TEXT REFERENCES finance_liquidity_accounts(account_id),category_id TEXT REFERENCES finance_liquidity_categories(category_id),amount_minor INTEGER CHECK(amount_minor BETWEEN -9000000000000000 AND 9000000000000000),occurred_at TEXT CHECK(occurred_at IS NULL OR occurred_at GLOB '????-??-??T??:??:??.??????Z'),purpose TEXT,reversal_of_document_id TEXT UNIQUE REFERENCES finance_liquidity_documents(document_id),replaces_opening_document_id TEXT UNIQUE REFERENCES finance_liquidity_documents(document_id),negative_balance_explanation TEXT,opening_evidence_type TEXT,opening_evidence_digest TEXT,opening_evidence_ref TEXT,revision INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,posted_at TEXT,semantic_digest TEXT,actor TEXT NOT NULL);
CREATE TABLE finance_liquidity_opening_anchors(anchor_id TEXT PRIMARY KEY,account_id TEXT NOT NULL UNIQUE REFERENCES finance_liquidity_accounts(account_id),opening_document_id TEXT NOT NULL REFERENCES finance_liquidity_documents(document_id),is_active INTEGER NOT NULL,cutover_at TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE finance_liquidity_ledger_transactions(sequence_no INTEGER PRIMARY KEY AUTOINCREMENT,transaction_id TEXT NOT NULL UNIQUE,document_id TEXT NOT NULL REFERENCES finance_liquidity_documents(document_id),origin_operation_id TEXT NOT NULL,phase TEXT NOT NULL,effective_at TEXT NOT NULL);
CREATE TABLE finance_liquidity_ledger_entries(entry_id TEXT PRIMARY KEY,transaction_id TEXT NOT NULL REFERENCES finance_liquidity_ledger_transactions(transaction_id),line_no INTEGER NOT NULL,ledger_account_id TEXT NOT NULL,side TEXT NOT NULL CHECK(side IN('debit','credit')),amount_minor INTEGER NOT NULL CHECK(amount_minor>0 AND amount_minor<=9000000000000000),UNIQUE(transaction_id,line_no));
CREATE TABLE finance_liquidity_ledger_transaction_seals(transaction_id TEXT PRIMARY KEY REFERENCES finance_liquidity_ledger_transactions(transaction_id),entry_count INTEGER NOT NULL CHECK(entry_count>=2),entries_digest TEXT NOT NULL,sealed_at TEXT NOT NULL);
CREATE TABLE finance_liquidity_effect_set_seals(operation_id TEXT PRIMARY KEY REFERENCES finance_liquidity_operations(operation_id),root_document_id TEXT NOT NULL REFERENCES finance_liquidity_documents(document_id),transaction_count INTEGER NOT NULL,transactions_digest TEXT NOT NULL,sealed_at TEXT NOT NULL);
CREATE TABLE finance_liquidity_operations(operation_id TEXT PRIMARY KEY,scope TEXT NOT NULL,idempotency_key TEXT NOT NULL,request_digest TEXT NOT NULL,actor TEXT NOT NULL,effect_root_document_id TEXT REFERENCES finance_liquidity_documents(document_id),result_json TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(actor,scope,idempotency_key));
CREATE TABLE finance_liquidity_duplicate_tokens(token TEXT PRIMARY KEY,actor TEXT NOT NULL,document_id TEXT NOT NULL,document_revision INTEGER NOT NULL,semantic_digest TEXT NOT NULL,candidate_digest TEXT NOT NULL,ledger_watermark INTEGER NOT NULL,created_at TEXT NOT NULL,consumed_at TEXT);
CREATE TABLE finance_liquidity_audit_events(event_id TEXT PRIMARY KEY,actor TEXT NOT NULL,event_type TEXT NOT NULL,object_id TEXT NOT NULL,payload_digest TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE finance_liquidity_cash_reconciliations(reconciliation_id TEXT PRIMARY KEY,account_id TEXT NOT NULL REFERENCES finance_liquidity_accounts(account_id),week_ending TEXT NOT NULL,balance_as_of TEXT NOT NULL,business_timezone TEXT NOT NULL,ledger_watermark INTEGER NOT NULL,ledger_digest TEXT NOT NULL,expected_minor INTEGER NOT NULL,actual_minor INTEGER NOT NULL,difference_minor INTEGER NOT NULL CHECK(difference_minor BETWEEN -9000000000000000 AND 9000000000000000),status TEXT NOT NULL CHECK(status IN('matched','discrepancy','resolved')),checked_at TEXT NOT NULL,comment TEXT,actor TEXT NOT NULL,created_at TEXT NOT NULL,revision INTEGER NOT NULL,record_operation_id TEXT NOT NULL REFERENCES finance_liquidity_operations(operation_id),resolution_operation_id TEXT REFERENCES finance_liquidity_operations(operation_id),resolution_kind TEXT,resolution_reconciliation_id TEXT REFERENCES finance_liquidity_cash_reconciliations(reconciliation_id),resolution_reason TEXT,resolved_at TEXT,resolved_by TEXT);
CREATE TRIGGER finance_account_immutable_update BEFORE UPDATE ON finance_liquidity_accounts BEGIN SELECT RAISE(ABORT,'account immutable'); END;
CREATE TRIGGER finance_account_immutable_delete BEFORE DELETE ON finance_liquidity_accounts BEGIN SELECT RAISE(ABORT,'account immutable'); END;
CREATE TRIGGER finance_category_classification_after_use BEFORE UPDATE OF direction,posting_class ON finance_liquidity_categories WHEN (NEW.direction!=OLD.direction OR NEW.posting_class IS NOT OLD.posting_class) AND EXISTS(SELECT 1 FROM finance_liquidity_documents WHERE category_id=OLD.category_id AND status IN('posted','reversed')) BEGIN SELECT RAISE(ABORT,'used category classification immutable'); END;
CREATE TRIGGER finance_entries_append_only_update BEFORE UPDATE ON finance_liquidity_ledger_entries BEGIN SELECT RAISE(ABORT,'ledger entries immutable'); END;
CREATE TRIGGER finance_entries_append_only_delete BEFORE DELETE ON finance_liquidity_ledger_entries BEGIN SELECT RAISE(ABORT,'ledger entries immutable'); END;
CREATE TRIGGER finance_entry_after_seal BEFORE INSERT ON finance_liquidity_ledger_entries WHEN EXISTS(SELECT 1 FROM finance_liquidity_ledger_transaction_seals WHERE transaction_id=NEW.transaction_id) BEGIN SELECT RAISE(ABORT,'sealed transaction rejects entries'); END;
CREATE TRIGGER finance_transaction_after_effect_seal BEFORE INSERT ON finance_liquidity_ledger_transactions WHEN EXISTS(SELECT 1 FROM finance_liquidity_effect_set_seals WHERE operation_id=NEW.origin_operation_id) BEGIN SELECT RAISE(ABORT,'sealed operation rejects transactions'); END;
CREATE TRIGGER finance_documents_posted_any_update BEFORE UPDATE ON finance_liquidity_documents WHEN OLD.status IN ('posted','reversed') AND finance_internal_write()!=1 BEGIN SELECT RAISE(ABORT,'posted document immutable'); END;
CREATE TRIGGER finance_documents_posted_legal_transition BEFORE UPDATE ON finance_liquidity_documents
WHEN OLD.status IN ('posted','reversed') AND finance_internal_write()=1 AND (
  (
    OLD.status='posted' AND NEW.status='reversed'
    AND NEW.revision=OLD.revision+1 AND NEW.updated_at>OLD.updated_at
    AND NEW.document_id IS OLD.document_id
    AND NEW.document_type IS OLD.document_type
    AND NEW.transfer_mode IS OLD.transfer_mode
    AND NEW.transfer_state IS OLD.transfer_state
    AND NEW.source_account_id IS OLD.source_account_id
    AND NEW.target_account_id IS OLD.target_account_id
    AND NEW.category_id IS OLD.category_id
    AND NEW.amount_minor IS OLD.amount_minor
    AND NEW.occurred_at IS OLD.occurred_at
    AND NEW.purpose IS OLD.purpose
    AND NEW.reversal_of_document_id IS OLD.reversal_of_document_id
    AND NEW.replaces_opening_document_id IS OLD.replaces_opening_document_id
    AND NEW.negative_balance_explanation IS OLD.negative_balance_explanation
    AND NEW.opening_evidence_type IS OLD.opening_evidence_type
    AND NEW.opening_evidence_digest IS OLD.opening_evidence_digest
    AND NEW.opening_evidence_ref IS OLD.opening_evidence_ref
    AND NEW.created_at IS OLD.created_at
    AND NEW.posted_at IS OLD.posted_at
    AND NEW.semantic_digest IS OLD.semantic_digest
    AND NEW.actor IS OLD.actor
    AND finance_internal_operation_id() IS NOT NULL
    AND EXISTS(
      SELECT 1
      FROM finance_liquidity_operations o
      JOIN finance_liquidity_effect_set_seals s ON s.operation_id=o.operation_id
      WHERE o.operation_id=finance_internal_operation_id()
        AND s.transaction_count=(
          SELECT COUNT(*) FROM finance_liquidity_ledger_transactions t
          WHERE t.origin_operation_id=o.operation_id
        )
        AND NOT EXISTS(
          SELECT 1
          FROM finance_liquidity_ledger_transactions t
          LEFT JOIN finance_liquidity_ledger_transaction_seals ts
            ON ts.transaction_id=t.transaction_id
          WHERE t.origin_operation_id=o.operation_id
            AND ts.transaction_id IS NULL
        )
        AND (
          (
            o.scope='document.reverse:'||OLD.document_id
            AND EXISTS(
              SELECT 1 FROM finance_liquidity_documents r
              WHERE r.document_id=s.root_document_id
                AND r.reversal_of_document_id=OLD.document_id
            )
          )
          OR
          (
            o.scope='opening.replace:'||OLD.document_id
            AND EXISTS(
              SELECT 1 FROM finance_liquidity_documents replacement
              WHERE replacement.document_id=s.root_document_id
                AND replacement.replaces_opening_document_id=OLD.document_id
            )
            AND EXISTS(
              SELECT 1 FROM finance_liquidity_documents reversal
              WHERE reversal.reversal_of_document_id=OLD.document_id
            )
          )
        )
    )
  )
  OR
  (
    OLD.status='posted' AND NEW.status='posted'
    AND OLD.document_type='transfer'
    AND OLD.transfer_state='in_transit'
    AND NEW.transfer_state IN ('completed','cancelled')
    AND NEW.revision=OLD.revision+1 AND NEW.updated_at>OLD.updated_at
    AND NEW.document_id IS OLD.document_id
    AND NEW.document_type IS OLD.document_type
    AND NEW.transfer_mode IS OLD.transfer_mode
    AND NEW.source_account_id IS OLD.source_account_id
    AND NEW.target_account_id IS OLD.target_account_id
    AND NEW.category_id IS OLD.category_id
    AND NEW.amount_minor IS OLD.amount_minor
    AND NEW.occurred_at IS OLD.occurred_at
    AND NEW.purpose IS OLD.purpose
    AND NEW.reversal_of_document_id IS OLD.reversal_of_document_id
    AND NEW.replaces_opening_document_id IS OLD.replaces_opening_document_id
    AND NEW.negative_balance_explanation IS OLD.negative_balance_explanation
    AND NEW.opening_evidence_type IS OLD.opening_evidence_type
    AND NEW.opening_evidence_digest IS OLD.opening_evidence_digest
    AND NEW.opening_evidence_ref IS OLD.opening_evidence_ref
    AND NEW.created_at IS OLD.created_at
    AND NEW.posted_at IS OLD.posted_at
    AND NEW.semantic_digest IS OLD.semantic_digest
    AND NEW.actor IS OLD.actor
    AND finance_internal_operation_id() IS NOT NULL
    AND EXISTS(
      SELECT 1
      FROM finance_liquidity_operations o
      JOIN finance_liquidity_effect_set_seals s ON s.operation_id=o.operation_id
      JOIN finance_liquidity_ledger_transactions t
        ON t.origin_operation_id=o.operation_id
       AND t.document_id=OLD.document_id
      JOIN finance_liquidity_ledger_transaction_seals ts
        ON ts.transaction_id=t.transaction_id
      WHERE o.operation_id=finance_internal_operation_id()
        AND o.scope=(
          CASE NEW.transfer_state
            WHEN 'completed' THEN 'transfer.complete:'||OLD.document_id
            ELSE 'transfer.cancel:'||OLD.document_id
          END
        )
        AND s.root_document_id=OLD.document_id
        AND s.transaction_count=1
        AND (
          SELECT COUNT(*) FROM finance_liquidity_ledger_transactions exact_t
          WHERE exact_t.origin_operation_id=o.operation_id
        )=1
        AND t.phase=(
          CASE NEW.transfer_state
            WHEN 'completed' THEN 'transfer_complete'
            ELSE 'transfer_cancel'
          END
        )
    )
  )
) IS NOT TRUE BEGIN SELECT RAISE(ABORT,'posted document immutable'); END;
CREATE TRIGGER finance_documents_posted_no_delete BEFORE DELETE ON finance_liquidity_documents WHEN OLD.status IN ('posted','reversed') BEGIN SELECT RAISE(ABORT,'posted document immutable'); END;
CREATE TRIGGER finance_transactions_immutable_update BEFORE UPDATE ON finance_liquidity_ledger_transactions BEGIN SELECT RAISE(ABORT,'ledger transactions immutable'); END;
CREATE TRIGGER finance_transactions_immutable_delete BEFORE DELETE ON finance_liquidity_ledger_transactions BEGIN SELECT RAISE(ABORT,'ledger transactions immutable'); END;
CREATE TRIGGER finance_transaction_seals_immutable_update BEFORE UPDATE ON finance_liquidity_ledger_transaction_seals BEGIN SELECT RAISE(ABORT,'ledger seals immutable'); END;
CREATE TRIGGER finance_transaction_seals_immutable_delete BEFORE DELETE ON finance_liquidity_ledger_transaction_seals BEGIN SELECT RAISE(ABORT,'ledger seals immutable'); END;
CREATE TRIGGER finance_effect_seals_immutable_update BEFORE UPDATE ON finance_liquidity_effect_set_seals BEGIN SELECT RAISE(ABORT,'effect seals immutable'); END;
CREATE TRIGGER finance_effect_seals_immutable_delete BEFORE DELETE ON finance_liquidity_effect_set_seals BEGIN SELECT RAISE(ABORT,'effect seals immutable'); END;
CREATE TRIGGER finance_operations_internal_insert BEFORE INSERT ON finance_liquidity_operations WHEN finance_internal_write()!=1 BEGIN SELECT RAISE(ABORT,'operations are service-owned'); END;
CREATE TRIGGER finance_operations_immutable_update BEFORE UPDATE ON finance_liquidity_operations WHEN finance_internal_write()!=1 BEGIN SELECT RAISE(ABORT,'operations immutable'); END;
CREATE TRIGGER finance_operations_immutable_delete BEFORE DELETE ON finance_liquidity_operations BEGIN SELECT RAISE(ABORT,'operations immutable'); END;
CREATE TRIGGER finance_audit_immutable_update BEFORE UPDATE ON finance_liquidity_audit_events BEGIN SELECT RAISE(ABORT,'audit immutable'); END;
CREATE TRIGGER finance_audit_immutable_delete BEFORE DELETE ON finance_liquidity_audit_events BEGIN SELECT RAISE(ABORT,'audit immutable'); END;
CREATE TRIGGER finance_anchor_immutable_delete BEFORE DELETE ON finance_liquidity_opening_anchors BEGIN SELECT RAISE(ABORT,'opening anchor immutable'); END;
CREATE TRIGGER finance_anchor_immutable_update BEFORE UPDATE ON finance_liquidity_opening_anchors BEGIN SELECT RAISE(ABORT,'opening anchor immutable'); END;
CREATE TRIGGER finance_reconciliation_immutable_delete BEFORE DELETE ON finance_liquidity_cash_reconciliations BEGIN SELECT RAISE(ABORT,'reconciliation immutable'); END;
CREATE TRIGGER finance_reconciliation_internal_insert BEFORE INSERT ON finance_liquidity_cash_reconciliations WHEN finance_internal_write()!=1 OR finance_internal_operation_id() IS NULL OR NEW.record_operation_id IS NOT finance_internal_operation_id() OR NEW.revision!=1 OR NEW.status NOT IN('matched','discrepancy') OR NEW.resolution_operation_id IS NOT NULL OR NEW.resolution_kind IS NOT NULL OR NEW.resolution_reconciliation_id IS NOT NULL OR NEW.resolution_reason IS NOT NULL OR NEW.resolved_at IS NOT NULL OR NEW.resolved_by IS NOT NULL OR NOT EXISTS(SELECT 1 FROM finance_liquidity_operations o WHERE o.operation_id=NEW.record_operation_id AND o.scope='cash.reconciliation.record:'||NEW.account_id AND o.actor=NEW.actor AND o.effect_root_document_id IS NULL) BEGIN SELECT RAISE(ABORT,'invalid reconciliation receipt'); END;
CREATE TRIGGER finance_reconciliation_any_update BEFORE UPDATE ON finance_liquidity_cash_reconciliations WHEN finance_internal_write()!=1 BEGIN SELECT RAISE(ABORT,'reconciliation immutable'); END;
CREATE TRIGGER finance_reconciliation_legal_transition BEFORE UPDATE ON finance_liquidity_cash_reconciliations WHEN finance_internal_write()=1 AND (OLD.status!='discrepancy' OR NEW.status!='resolved' OR NEW.revision!=OLD.revision+1 OR NEW.reconciliation_id IS NOT OLD.reconciliation_id OR NEW.account_id IS NOT OLD.account_id OR NEW.week_ending IS NOT OLD.week_ending OR NEW.balance_as_of IS NOT OLD.balance_as_of OR NEW.business_timezone IS NOT OLD.business_timezone OR NEW.ledger_watermark IS NOT OLD.ledger_watermark OR NEW.ledger_digest IS NOT OLD.ledger_digest OR NEW.expected_minor IS NOT OLD.expected_minor OR NEW.actual_minor IS NOT OLD.actual_minor OR NEW.difference_minor IS NOT OLD.difference_minor OR NEW.checked_at IS NOT OLD.checked_at OR NEW.comment IS NOT OLD.comment OR NEW.actor IS NOT OLD.actor OR NEW.created_at IS NOT OLD.created_at OR NEW.record_operation_id IS NOT OLD.record_operation_id OR NEW.resolution_operation_id IS NULL OR NEW.resolution_operation_id IS NOT finance_internal_operation_id() OR NEW.resolution_kind IS NULL OR NEW.resolved_at IS NULL OR NEW.resolved_by IS NULL OR NEW.resolution_reason IS NULL OR length(trim(NEW.resolution_reason))=0 OR NOT EXISTS(SELECT 1 FROM finance_liquidity_operations o WHERE o.operation_id=NEW.resolution_operation_id AND o.scope='cash.reconciliation.resolve:'||OLD.reconciliation_id AND o.actor=NEW.resolved_by AND o.effect_root_document_id IS NULL) OR (NEW.resolution_kind='matched_followup' AND (NEW.resolution_reconciliation_id IS NULL OR NEW.resolution_reconciliation_id=OLD.reconciliation_id OR NOT EXISTS(SELECT 1 FROM finance_liquidity_cash_reconciliations follow WHERE follow.reconciliation_id=NEW.resolution_reconciliation_id AND follow.account_id=OLD.account_id AND follow.status='matched' AND follow.checked_at>OLD.checked_at))) OR (NEW.resolution_kind='admin_override' AND NEW.resolution_reconciliation_id IS NOT NULL) OR NEW.resolution_kind NOT IN ('matched_followup','admin_override')) BEGIN SELECT RAISE(ABORT,'invalid reconciliation transition'); END;
"""
