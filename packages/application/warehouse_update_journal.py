"""Durable automatic/manual warehouse update run and phase journal."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping
from uuid import uuid4


PHASES = (
    "wb_supply_registry",
    "transit_enrichment",
    "ff_ledger_reservations",
    "official_complete_wb_stocks",
    "cost_materialization",
    "functional_publication",
    "dependent_replay_economics",
)

PHASE_LABELS_RU = {
    "wb_supply_registry": "Реестр поставок WB",
    "transit_enrichment": "Транзитная себестоимость",
    "ff_ledger_reservations": "FF ledger и резервы",
    "official_complete_wb_stocks": "Полные официальные остатки WB",
    "cost_materialization": "Материализация себестоимости",
    "functional_publication": "Публикация складов",
    "dependent_replay_economics": "Зависимый пересчёт и экономика",
}


class WarehouseUpdateJournal:
    def __init__(self, *, db_path: Path, timestamp_factory: Any | None = None) -> None:
        self.db_path = Path(db_path)
        self.timestamp_factory = timestamp_factory or _now
        # Schema ownership belongs to service/runner construction.  Read-side
        # status requests below remain strict query-only operations.
        with _connect(self.db_path) as conn:
            ensure_warehouse_update_journal_schema(conn)
            conn.commit()

    def start(self, *, trigger_source: str, scheduled_for: str = "") -> str:
        started_at = self.timestamp_factory()
        run_id = "whur_" + hashlib.sha256(
            f"{trigger_source}:{started_at}:{uuid4().hex}".encode("utf-8")
        ).hexdigest()[:24]
        with _connect(self.db_path) as conn:
            ensure_warehouse_update_journal_schema(conn)
            interrupted = conn.execute(
                "SELECT run_id,started_at FROM sheet_vitrina_v1_warehouse_update_runs "
                "WHERE status='running'"
            ).fetchall()
            for row in interrupted:
                prior_run_id = str(row["run_id"])
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_warehouse_update_runs
                    SET status='interrupted',finished_at=?,duration_ms=?,active_phase='',
                        last_error='Прошлый запуск прерван до завершения; сохранён last-good',
                        updated_at=? WHERE run_id=?
                    """,
                    (
                        started_at,
                        _duration_ms(str(row["started_at"] or ""), started_at),
                        started_at,
                        prior_run_id,
                    ),
                )
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_warehouse_update_phases
                    SET status='failed',finished_at=?,
                        last_error='Запуск прерван до завершения'
                    WHERE run_id=? AND status='running'
                    """,
                    (started_at, prior_run_id),
                )
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_warehouse_update_runs(
                    run_id,trigger_source,status,scheduled_for,started_at,
                    finished_at,duration_ms,active_phase,last_error,result_json,
                    functional_version_id,business_date,created_at,updated_at
                ) VALUES(?,?,'running',?,?,NULL,NULL,'','', '{}','','',?,?)
                """,
                (run_id, trigger_source, scheduled_for, started_at, started_at, started_at),
            )
            for phase in PHASES:
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_warehouse_update_phases(
                        run_id,phase_key,status,started_at,finished_at,duration_ms,
                        item_count,last_good_at,last_error,details_json
                    ) VALUES(?,?,'pending',NULL,NULL,NULL,0,NULL,'','{}')
                    """,
                    (run_id, phase),
                )
            conn.commit()
        return run_id

    def phase_started(self, run_id: str, phase_key: str) -> None:
        _require_phase(phase_key)
        now = self.timestamp_factory()
        with _connect(self.db_path) as conn:
            ensure_warehouse_update_journal_schema(conn)
            conn.execute(
                "UPDATE sheet_vitrina_v1_warehouse_update_runs SET active_phase=?,updated_at=? WHERE run_id=?",
                (phase_key, now, run_id),
            )
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_warehouse_update_phases
                SET status='running',started_at=?,finished_at=NULL,duration_ms=NULL,
                    last_error='',details_json='{}' WHERE run_id=? AND phase_key=?
                """,
                (now, run_id, phase_key),
            )
            conn.commit()

    def phase_finished(
        self,
        run_id: str,
        phase_key: str,
        *,
        status: str = "success",
        item_count: int = 0,
        details: Mapping[str, Any] | None = None,
        error: str = "",
    ) -> None:
        _require_phase(phase_key)
        now = self.timestamp_factory()
        with _connect(self.db_path) as conn:
            ensure_warehouse_update_journal_schema(conn)
            row = conn.execute(
                "SELECT started_at FROM sheet_vitrina_v1_warehouse_update_phases WHERE run_id=? AND phase_key=?",
                (run_id, phase_key),
            ).fetchone()
            duration_ms = _duration_ms(str(row["started_at"] or ""), now) if row else 0
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_warehouse_update_phases
                SET status=?,finished_at=?,duration_ms=?,item_count=?,
                    last_good_at=CASE WHEN ?='success' THEN ? ELSE last_good_at END,
                    last_error=?,details_json=?
                WHERE run_id=? AND phase_key=?
                """,
                (
                    status,
                    now,
                    duration_ms,
                    max(0, int(item_count)),
                    status,
                    now,
                    str(error or "")[:2000],
                    _json(_bounded_details(details or {})),
                    run_id,
                    phase_key,
                ),
            )
            conn.execute(
                "UPDATE sheet_vitrina_v1_warehouse_update_runs SET updated_at=? WHERE run_id=?",
                (now, run_id),
            )
            conn.commit()

    def finish(
        self,
        run_id: str,
        *,
        status: str,
        result: Mapping[str, Any] | None = None,
        error: str = "",
    ) -> None:
        now = self.timestamp_factory()
        payload = dict(result or {})
        active_version = dict(payload.get("active_version") or {})
        with _connect(self.db_path) as conn:
            ensure_warehouse_update_journal_schema(conn)
            row = conn.execute(
                "SELECT started_at FROM sheet_vitrina_v1_warehouse_update_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            duration_ms = _duration_ms(str(row["started_at"] or ""), now) if row else 0
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_warehouse_update_runs
                SET status=?,finished_at=?,duration_ms=?,active_phase='',last_error=?,
                    result_json=?,functional_version_id=?,business_date=?,updated_at=?
                WHERE run_id=?
                """,
                (
                    status,
                    now,
                    duration_ms,
                    str(error or "")[:2000],
                    _json(_bounded_details(payload)),
                    str(active_version.get("version_id") or payload.get("functional_version_id") or ""),
                    str(active_version.get("business_effective_date") or payload.get("business_date") or "")[:10],
                    now,
                    run_id,
                ),
            )
            conn.commit()

    def public_status(self) -> dict[str, Any]:
        with _connect(self.db_path, query_only=True) as conn:
            runs = [dict(row) for row in conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_update_runs ORDER BY started_at DESC,run_id DESC LIMIT 50"
            ).fetchall()]
            latest_automatic = next(
                (row for row in runs if str(row["trigger_source"]) in {"hourly", "automatic", "timer"}),
                None,
            )
            latest_manual = next(
                (row for row in runs if str(row["trigger_source"]) in {"manual", "operator"}),
                None,
            )
            latest_automatic_success = next(
                (
                    row
                    for row in runs
                    if str(row["trigger_source"]) in {"hourly", "automatic", "timer"}
                    and str(row["status"]) == "success"
                ),
                None,
            )
            latest_manual_success = next(
                (
                    row
                    for row in runs
                    if str(row["trigger_source"]) in {"manual", "operator"}
                    and str(row["status"]) == "success"
                ),
                None,
            )
            active = next((row for row in runs if str(row["status"]) == "running"), None)
            selected = active or (runs[0] if runs else None)
            phases = [dict(row) for row in conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_update_phases WHERE run_id=? ORDER BY rowid",
                (str((selected or {}).get("run_id") or ""),),
            ).fetchall()] if selected else []
            last_good_by_phase = {
                str(row["phase_key"]): str(row["last_good_at"] or "")
                for row in conn.execute(
                    """
                    SELECT phase_key,MAX(last_good_at) AS last_good_at
                    FROM sheet_vitrina_v1_warehouse_update_phases
                    WHERE last_good_at IS NOT NULL AND last_good_at<>''
                    GROUP BY phase_key
                    """
                ).fetchall()
            }
            version = conn.execute(
                """
                SELECT version.version_id,version.business_effective_date,
                       version.effective_at,version.published_at,version.plan_fingerprint
                FROM sheet_vitrina_v1_warehouse_functional_active active
                JOIN sheet_vitrina_v1_warehouse_functional_versions version
                  ON version.version_id=active.version_id WHERE active.slot=1
                """
            ).fetchone()
        automatic = _run_public(
            latest_automatic,
            last_success_at=str((latest_automatic_success or {}).get("finished_at") or ""),
        )
        manual = _run_public(
            latest_manual,
            last_success_at=str((latest_manual_success or {}).get("finished_at") or ""),
        )
        if latest_automatic:
            next_run = _add_hour(str(latest_automatic.get("started_at") or ""))
        else:
            next_run = ""
        return {
            "contract_name": "warehouse_update_journal_v1",
            "automatic_updates": automatic,
            "manual_updates": manual,
            "active_run": _run_public(active),
            "phases": [
                {
                    "phase_key": str(row["phase_key"]),
                    "label_ru": PHASE_LABELS_RU.get(str(row["phase_key"]), str(row["phase_key"])),
                    "status": str(row["status"]),
                    "started_at": str(row["started_at"] or ""),
                    "finished_at": str(row["finished_at"] or ""),
                    "duration_ms": row["duration_ms"],
                    "item_count": int(row["item_count"] or 0),
                    "last_good_at": str(
                        row["last_good_at"]
                        or last_good_by_phase.get(str(row["phase_key"]))
                        or ""
                    ),
                    "last_error": str(row["last_error"] or ""),
                    "details": json.loads(str(row["details_json"] or "{}")),
                }
                for row in phases
            ],
            "next_scheduled_run": next_run,
            "active_version": dict(version) if version is not None else {},
            "freshness": (
                "degraded"
                if (latest_automatic and str(latest_automatic.get("status")) == "failed")
                else "current" if version is not None else "unavailable"
            ),
            "last_good_retained": bool(
                version is not None
                and latest_automatic
                and str(latest_automatic.get("status")) == "failed"
            ),
        }


def ensure_warehouse_update_journal_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_update_runs(
            run_id TEXT PRIMARY KEY,trigger_source TEXT NOT NULL,status TEXT NOT NULL,
            scheduled_for TEXT NOT NULL,started_at TEXT NOT NULL,finished_at TEXT,
            duration_ms INTEGER,active_phase TEXT NOT NULL,last_error TEXT NOT NULL,
            result_json TEXT NOT NULL,functional_version_id TEXT NOT NULL,
            business_date TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS warehouse_update_runs_by_started
        ON sheet_vitrina_v1_warehouse_update_runs(started_at DESC,run_id DESC);
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_update_phases(
            run_id TEXT NOT NULL,phase_key TEXT NOT NULL,status TEXT NOT NULL,
            started_at TEXT,finished_at TEXT,duration_ms INTEGER,item_count INTEGER NOT NULL,
            last_good_at TEXT,last_error TEXT NOT NULL,details_json TEXT NOT NULL,
            PRIMARY KEY(run_id,phase_key)
        );
        """
    )


def _run_public(
    row: Mapping[str, Any] | None,
    *,
    last_success_at: str = "",
) -> dict[str, Any]:
    if not row:
        return {
            "status": "never",
            "run_id": "",
            "last_attempt_at": "",
            "last_success_at": "",
            "last_error": "",
        }
    return {
        "run_id": str(row.get("run_id") or ""),
        "trigger_source": str(row.get("trigger_source") or ""),
        "status": str(row.get("status") or ""),
        "scheduled_for": str(row.get("scheduled_for") or ""),
        "last_attempt_at": str(row.get("started_at") or ""),
        "last_success_at": last_success_at or (
            str(row.get("finished_at") or "") if str(row.get("status")) == "success" else ""
        ),
        "finished_at": str(row.get("finished_at") or ""),
        "duration_ms": row.get("duration_ms"),
        "active_phase": str(row.get("active_phase") or ""),
        "last_error": str(row.get("last_error") or ""),
        "functional_version_id": str(row.get("functional_version_id") or ""),
        "business_date": str(row.get("business_date") or ""),
    }


def _require_phase(value: str) -> None:
    if value not in PHASES:
        raise ValueError(f"unknown warehouse update phase: {value}")


def _duration_ms(start: str, finish: str) -> int:
    try:
        first = datetime.fromisoformat(start.replace("Z", "+00:00"))
        last = datetime.fromisoformat(finish.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return max(0, int((last - first).total_seconds() * 1000))


def _add_hour(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return (parsed + timedelta(hours=1)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _connect(path: Path, *, query_only: bool = False) -> sqlite3.Connection:
    if query_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only=ON")
    else:
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=120000")
    return conn


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _bounded_details(value: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the durable journal observable without copying producer payloads."""

    compact: dict[str, Any] = {}
    for key, item in sorted(dict(value).items()):
        name = str(key)[:120]
        if item is None or isinstance(item, (bool, int, float)):
            compact[name] = item
        elif isinstance(item, str):
            compact[name] = item[:2000]
        elif isinstance(item, Mapping):
            compact[name] = _bounded_details(dict(item))
        elif isinstance(item, (list, tuple, set)):
            values = list(item)
            if len(values) <= 20 and all(
                element is None or isinstance(element, (bool, int, float, str))
                for element in values
            ):
                compact[name] = [
                    element[:500] if isinstance(element, str) else element
                    for element in values
                ]
            else:
                compact[name] = {"item_count": len(values), "details_omitted": True}
        else:
            compact[name] = str(item)[:2000]
        if len(_json(compact).encode("utf-8")) > 32768:
            compact.pop(name, None)
            compact["details_truncated"] = True
            break
    return compact


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
