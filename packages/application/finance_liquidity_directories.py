"""Stable, explicit cash directory seed and v2-to-v3 schema extension.

No function in this module opens a store.  The caller owns the write transaction.
"""

from __future__ import annotations

import sqlite3
import hashlib
import json


SEED_ACCOUNTS = (
    ("cash_vladislav", "Касса Владислав", "Владислав"),
    ("cash_victoria", "Касса Виктория", "Виктория"),
    ("cash_carolina", "Касса Каролина", "Каролина"),
)

# The code is an immutable semantic identity.  Names are editable labels.
SEED_CATEGORIES = (
    ("goods_payment", "Оплата товара", "operating_expense", 0),
    ("china_delivery", "Доставка из Китая", "operating_expense", 0),
    ("russia_delivery", "Доставка по России", "operating_expense", 0),
    ("packaging", "Упаковка", "operating_expense", 0),
    ("fulfillment", "Фулфилмент", "operating_expense", 0),
    ("salary_advances", "Зарплата и авансы", "operating_expense", 0),
    ("rent", "Аренда", "operating_expense", 0),
    ("office", "Расходы офиса", "operating_expense", 0),
    ("software", "Сервисы и ПО", "operating_expense", 0),
    ("accounting", "Услуги бухгалтерии", "operating_expense", 0),
    ("travel", "Командировки", "operating_expense", 0),
    ("marketing", "Маркетинг", "operating_expense", 0),
    ("listing_content", "Контент для карточек", "operating_expense", 0),
    ("customer_replacement", "Перезаказ", "operating_expense", 0),
    ("giveaways", "Раздачи", "operating_expense", 0),
    ("bonuses_100", "Бонусы 100 ₽", "operating_expense", 0),
    ("cash_delivery", "Доставка наличных", "operating_expense", 0),
    ("taxes", "Налоги и взносы", "operating_expense", 0),
    ("bank_fees", "Банковские комиссии", "operating_expense", 0),
    ("miscellaneous", "Прочие расходы", "operating_expense", 1),
    ("owner_draw", "Личные изъятия", "owner_draw", 0),
    ("profit_distribution", "Распределение прибыли", "profit_distribution", 0),
    ("debt_payment", "Платежи по кредитам и займам", "debt_service_unallocated", 0),
)


def seed_directories(conn: sqlite3.Connection, now: str) -> None:
    """Insert only absent stable IDs.  A deleted row is a lasting tombstone."""
    def audit(kind: str, ident: str, name: str) -> None:
        payload = json.dumps({"name": name}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = "sha256:" + hashlib.sha256(payload.encode()).hexdigest()
        conn.execute(
            "INSERT INTO finance_liquidity_audit_events(event_id,actor,event_type,object_id,payload_digest,payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (f"audit_seed_{kind}_{ident}", "system", f"{kind}.seeded", ident, digest, payload, now),
        )
    for ident, name, responsible in SEED_ACCOUNTS:
        inserted = conn.execute(
            "INSERT OR IGNORE INTO finance_liquidity_accounts"
            "(account_id,name,account_type,currency,currency_exponent,responsible_name,"
            "is_active,revision,created_at,updated_at,code,is_deleted)"
            " VALUES(?,?,'cash','RUB',2,?,1,1,?,?,?,0)",
            (ident, name, responsible, now, now, ident),
        )
        if inserted.rowcount:
            audit("account", ident, name)
    for code, name, analytic_class, requires_comment in SEED_CATEGORIES:
        inserted = conn.execute(
            "INSERT OR IGNORE INTO finance_liquidity_categories"
            "(category_id,name,direction,posting_class,is_active,created_at,"
            "code,analytic_class,requires_comment,revision,updated_at,is_deleted)"
            " VALUES(?,?,'expense',?,1,?,?,?,?,1,?,0)",
            (f"category_{code}", name, "fee" if code == "bank_fees" else "external_outflow", now, code, analytic_class, requires_comment, now),
        )
        if inserted.rowcount:
            audit("category", f"category_{code}", name)


V3_ALTERS = (
    "ALTER TABLE finance_liquidity_accounts ADD COLUMN code TEXT",
    "ALTER TABLE finance_liquidity_accounts ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0 CHECK(is_deleted IN(0,1))",
    "ALTER TABLE finance_liquidity_categories ADD COLUMN code TEXT",
    "ALTER TABLE finance_liquidity_categories ADD COLUMN analytic_class TEXT NOT NULL DEFAULT 'operating_expense'",
    "ALTER TABLE finance_liquidity_categories ADD COLUMN requires_comment INTEGER NOT NULL DEFAULT 0 CHECK(requires_comment IN(0,1))",
    "ALTER TABLE finance_liquidity_categories ADD COLUMN revision INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE finance_liquidity_categories ADD COLUMN updated_at TEXT",
    "ALTER TABLE finance_liquidity_categories ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0 CHECK(is_deleted IN(0,1))",
    "ALTER TABLE finance_liquidity_documents ADD COLUMN counterparty_id TEXT REFERENCES finance_liquidity_counterparties(counterparty_id)",
    "ALTER TABLE finance_liquidity_documents ADD COLUMN counterparty_name_snapshot TEXT",
    "ALTER TABLE finance_liquidity_documents ADD COLUMN category_name_snapshot TEXT",
    "ALTER TABLE finance_liquidity_documents ADD COLUMN analytic_class_snapshot TEXT",
    "ALTER TABLE finance_liquidity_documents ADD COLUMN requires_comment_snapshot INTEGER",
    "ALTER TABLE finance_liquidity_documents ADD COLUMN funding_kind TEXT",
    "ALTER TABLE finance_liquidity_audit_events ADD COLUMN payload_json TEXT",
)

COUNTERPARTY_SCHEMA = "CREATE TABLE finance_liquidity_counterparties(counterparty_id TEXT PRIMARY KEY,name TEXT NOT NULL CHECK(length(trim(name))>0),is_active INTEGER NOT NULL CHECK(is_active IN(0,1)),is_deleted INTEGER NOT NULL CHECK(is_deleted IN(0,1)),revision INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)"

V3_EXTRA_SCHEMA = """
CREATE UNIQUE INDEX finance_accounts_code ON finance_liquidity_accounts(code) WHERE code IS NOT NULL;
CREATE UNIQUE INDEX finance_categories_code ON finance_liquidity_categories(code) WHERE code IS NOT NULL;
CREATE TRIGGER finance_counterparty_delete_guard BEFORE DELETE ON finance_liquidity_counterparties WHEN EXISTS(SELECT 1 FROM finance_liquidity_documents WHERE counterparty_id=OLD.counterparty_id) BEGIN SELECT RAISE(ABORT,'counterparty in use'); END;
CREATE TRIGGER finance_category_delete_guard BEFORE DELETE ON finance_liquidity_categories WHEN EXISTS(SELECT 1 FROM finance_liquidity_documents WHERE category_id=OLD.category_id) BEGIN SELECT RAISE(ABORT,'category in use'); END;
CREATE TRIGGER finance_account_delete_guard BEFORE DELETE ON finance_liquidity_accounts WHEN EXISTS(SELECT 1 FROM finance_liquidity_documents WHERE source_account_id=OLD.account_id OR target_account_id=OLD.account_id) OR EXISTS(SELECT 1 FROM finance_liquidity_cash_reconciliations WHERE account_id=OLD.account_id) BEGIN SELECT RAISE(ABORT,'account in use'); END;
CREATE TRIGGER finance_category_semantics_guard BEFORE UPDATE OF analytic_class,requires_comment,code ON finance_liquidity_categories WHEN NEW.analytic_class IS NOT OLD.analytic_class OR NEW.requires_comment IS NOT OLD.requires_comment OR NEW.code IS NOT OLD.code BEGIN SELECT RAISE(ABORT,'category classification immutable'); END;
CREATE TRIGGER finance_account_identity_guard BEFORE UPDATE OF account_type,currency,currency_exponent,responsible_name,code ON finance_liquidity_accounts WHEN NEW.account_type IS NOT OLD.account_type OR NEW.currency IS NOT OLD.currency OR NEW.currency_exponent IS NOT OLD.currency_exponent OR NEW.responsible_name IS NOT OLD.responsible_name OR NEW.code IS NOT OLD.code BEGIN SELECT RAISE(ABORT,'account identity immutable'); END;
CREATE TRIGGER finance_counterparty_document_guard BEFORE UPDATE OF counterparty_id,category_name_snapshot,analytic_class_snapshot,requires_comment_snapshot,counterparty_name_snapshot ON finance_liquidity_documents WHEN OLD.status IN('posted','reversed') BEGIN SELECT RAISE(ABORT,'posted document immutable'); END;
CREATE TRIGGER finance_funding_document_guard BEFORE UPDATE OF funding_kind ON finance_liquidity_documents WHEN OLD.status IN('posted','reversed') BEGIN SELECT RAISE(ABORT,'posted document immutable'); END;
"""


def install_v3_extension(conn: sqlite3.Connection, now: str) -> None:
    conn.execute(COUNTERPARTY_SCHEMA)
    for statement in V3_ALTERS:
        conn.execute(statement)
    conn.execute("DROP TRIGGER finance_account_immutable_update")
    conn.execute("DROP TRIGGER finance_account_immutable_delete")
    for statement in V3_EXTRA_SCHEMA.splitlines():
        if statement.strip():
            conn.execute(statement)
    seed_directories(conn, now)
