"""Retired manual FF input must not bypass the native facility/pool authority."""
from pathlib import Path
import sqlite3


class ModernFfWorkflowRequired(ValueError):
    code = "modern_workflow_required"

    def __init__(self):
        super().__init__("Для прихода или списания откройте современные документы и выберите склад и вид документа.")


class ManualFfAuthorityUnavailable(ModernFfWorkflowRequired):
    code = "manual_ff_authority_unavailable"

    def __init__(self):
        ValueError.__init__(self, "Состояние остатков склада не подтверждено. Документ не сохранён.")


def require_legacy_manual_authority(conn: sqlite3.Connection) -> None:
    # This is the same cutover predicate as the native functional projection.
    table = "sheet_vitrina_v1_ff_pool_cutover_manifests"
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
            raise ModernFfWorkflowRequired()
    balances = "sheet_vitrina_v1_ff_pool_balances"
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (balances,)).fetchone():
        if conn.execute(f"SELECT 1 FROM {balances} LIMIT 1").fetchone():
            raise ManualFfAuthorityUnavailable()


def check_legacy_manual_authority(db_path: Path) -> None:
    if not db_path.exists():
        return
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        require_legacy_manual_authority(conn)
    finally:
        conn.close()
