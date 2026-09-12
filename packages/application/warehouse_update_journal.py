"""Durable automatic/manual warehouse update run and phase journal."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping
from uuid import uuid4


from packages.application.warehouse_functional_lock import (
    require_warehouse_job_owner, WarehouseJobOwnershipError,
)


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


class WarehouseRequestConflict(ValueError):
    """The same scoped request key refers to a different request body."""


def validate_warehouse_request(payload: Mapping[str, Any] | None, scope: str) -> tuple[str, str, str]:
    body = dict(payload or {})
    key = body.pop("request_key", "")
    if not isinstance(key, str) or (key and not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key)):
        raise ValueError("request_key must contain 16..128 letters, digits, '_' or '-'")
    if not scope or len(scope) > 200:
        raise ValueError("warehouse request scope is required")
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > 16384:
        raise ValueError("warehouse request payload is too large")
    return key, hashlib.sha256(encoded.encode("utf-8")).hexdigest(), encoded


class WarehouseUpdateJournal:
    def __init__(self, *, db_path: Path, runtime_dir: Path | None = None, timestamp_factory: Any | None = None) -> None:
        self.db_path = Path(db_path)
        self.runtime_dir = Path(runtime_dir) if runtime_dir is not None else self.db_path.parent
        self.timestamp_factory = timestamp_factory or _now
        # Schema ownership belongs to service/runner construction.  Read-side
        # status requests below remain strict query-only operations.
        with _connect(self.db_path) as conn:
            ensure_warehouse_update_journal_schema(conn)
            conn.commit()

    def accept(self, *, request_key: str, request_scope: str, payload_fingerprint: str,
               request_payload_json: str) -> tuple[dict[str, Any], bool]:
        """Commit intent and its public alias together, independently of claim."""
        now = self.timestamp_factory()
        with _connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if request_key:
                prior = conn.execute(
                    "SELECT * FROM sheet_vitrina_v1_warehouse_update_runs WHERE request_scope=? AND request_key=?",
                    (request_scope, request_key),
                ).fetchone()
                if prior is not None:
                    if prior["payload_fingerprint"] != payload_fingerprint:
                        raise WarehouseRequestConflict("request_key already accepted with a different payload")
                    return self._job(conn, prior), False
            # Preserve the existing one-flight manual button contract. A busy
            # request is not accepted and never acquires someone else's key.
            active = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_update_runs "
                "WHERE public_job_id<>'' AND status IN ('accepted','running') ORDER BY created_at LIMIT 1"
            ).fetchone()
            if active is not None:
                return self._job(conn, active), False
            run_id, public_id = "whur_" + uuid4().hex[:24], uuid4().hex
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_warehouse_update_runs(
                    run_id,trigger_source,status,scheduled_for,started_at,active_phase,last_error,result_json,
                    functional_version_id,business_date,created_at,updated_at,owner_scope,
                    public_job_id,request_key,request_scope,payload_fingerprint,request_payload_json
                ) VALUES(?,'manual','accepted','','','','','{}','','',?,?,?,?,?,?,?,?)""",
                (run_id, now, now, str(self.runtime_dir.resolve()), public_id, request_key,
                 request_scope, payload_fingerprint, request_payload_json),
            )
            for phase in PHASES:
                conn.execute(
                    """INSERT INTO sheet_vitrina_v1_warehouse_update_phases
                    (run_id,phase_key,status,item_count,last_error,details_json)
                    VALUES(?,?,'pending',0,'','{}')""", (run_id, phase),
                )
            row = conn.execute("SELECT * FROM sheet_vitrina_v1_warehouse_update_runs WHERE run_id=?", (run_id,)).fetchone()
            conn.commit()
            return self._job(conn, row), True

    def lookup(self, *, public_id: str = "", request_key: str = "", request_scope: str) -> dict[str, Any] | None:
        """Exact lookup only. No schema/bootstrap, latest scan, or status writes."""
        if bool(public_id) == bool(request_key):
            raise ValueError("supply exactly one run_id or request_key")
        with _connect(self.db_path, query_only=True) as conn:
            if public_id:
                row = conn.execute(
                    "SELECT * FROM sheet_vitrina_v1_warehouse_update_runs "
                    "WHERE (public_job_id=? OR (public_job_id='' AND run_id=?)) "
                    "AND (request_scope=? OR request_scope='')",
                    (public_id, public_id, request_scope),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM sheet_vitrina_v1_warehouse_update_runs WHERE request_scope=? AND request_key=?",
                    (request_scope, request_key),
                ).fetchone()
            return self._job(conn, row) if row is not None else None

    def latest_job(self, *, request_scope: str) -> dict[str, Any] | None:
        with _connect(self.db_path, query_only=True) as conn:
            row = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_update_runs "
                "WHERE trigger_source IN ('manual','operator') AND (request_scope=? OR request_scope='') "
                "ORDER BY created_at DESC,run_id DESC LIMIT 1", (request_scope,),
            ).fetchone()
            return self._job(conn, row) if row is not None else None

    def needs_pickup(self) -> bool:
        with _connect(self.db_path, query_only=True) as conn:
            return conn.execute(
                "SELECT 1 FROM sheet_vitrina_v1_warehouse_update_runs "
                "WHERE public_job_id<>'' AND status IN ('accepted','running') LIMIT 1"
            ).fetchone() is not None

    def recover_and_pick(self) -> dict[str, Any] | None:
        """Only a new live admission may classify an orphan; never a GET."""
        owner = require_warehouse_job_owner(self.runtime_dir)
        with _connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._interrupt_orphans(conn, owner, self.timestamp_factory())
            row = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_update_runs "
                "WHERE public_job_id<>'' AND status='accepted' ORDER BY created_at,run_id LIMIT 1"
            ).fetchone()
            conn.commit()
            return self._job(conn, row) if row is not None else None

    def claim(self, run_id: str) -> bool:
        owner = require_warehouse_job_owner(self.runtime_dir)
        now = self.timestamp_factory()
        with _connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "UPDATE sheet_vitrina_v1_warehouse_update_runs SET status='running',owner_token=?,"
                "attempt_id=?,started_at=?,updated_at=? WHERE run_id=? AND status='accepted' AND owner_scope=?",
                (owner, uuid4().hex, now, now, run_id, str(self.runtime_dir.resolve())),
            ).rowcount
            conn.commit()
            return changed == 1

    def _job(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        phases = [dict(phase) for phase in conn.execute(
            "SELECT * FROM sheet_vitrina_v1_warehouse_update_phases WHERE run_id=? ORDER BY rowid", (row["run_id"],)
        )]
        for phase in phases:
            phase["details"] = json.loads(phase.pop("details_json") or "{}")
            phase["label_ru"] = PHASE_LABELS_RU[phase["phase_key"]]
        return {"job_id": row["public_job_id"] or row["run_id"], "durable_run_id": row["run_id"],
                "request_scope": row["request_scope"], "request_key": row["request_key"],
                "payload_fingerprint": row["payload_fingerprint"], "attempt_id": row["attempt_id"],
                "operation": "warehouse_current_source_sync", "status": row["status"],
                "started_at": row["started_at"] or row["created_at"], "finished_at": row["finished_at"],
                "result": json.loads(row["result_json"] or "{}"), "error": row["last_error"],
                "phases": phases, "log_lines": []}

    def _interrupt_orphans(self, conn: sqlite3.Connection, owner: str, now: str) -> None:
        rows = conn.execute(
            "SELECT run_id,started_at,owner_token,owner_scope FROM sheet_vitrina_v1_warehouse_update_runs WHERE status='running'"
        ).fetchall()
        scope = str(self.runtime_dir.resolve())
        if any(row["owner_scope"] and row["owner_scope"] != scope for row in rows):
            raise WarehouseJobOwnershipError("running warehouse run belongs to a different admission scope")
        if any(row["owner_token"] == owner for row in rows):
            raise WarehouseJobOwnershipError("this admission already has a running warehouse run")
        for row in rows:
            conn.execute(
                "UPDATE sheet_vitrina_v1_warehouse_update_runs SET status='interrupted',finished_at=?,"
                "duration_ms=?,last_error='Прошлый запуск прерван до завершения; сохранён last-good',updated_at=? WHERE run_id=?",
                (now, _duration_ms(row["started_at"], now), now, row["run_id"]),
            )
            # Preserve confirmed phases, receipt details and the last active phase.
            conn.execute(
                "UPDATE sheet_vitrina_v1_warehouse_update_phases SET status='failed',finished_at=?,"
                "last_error='Запуск прерван до завершения' WHERE run_id=? AND status='running'",
                (now, row["run_id"]),
            )

    def start(self, *, trigger_source: str, scheduled_for: str = "", owner_token: str | None = None) -> str:
        owner = require_warehouse_job_owner(self.runtime_dir, owner_token)
        started_at = self.timestamp_factory()
        run_id = "whur_" + hashlib.sha256(
            f"{trigger_source}:{started_at}:{uuid4().hex}".encode("utf-8")
        ).hexdigest()[:24]
        with _connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            scope = str(self.runtime_dir.resolve())
            self._interrupt_orphans(conn, owner, started_at)
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_warehouse_update_runs(
                    run_id,trigger_source,status,scheduled_for,started_at,
                    finished_at,duration_ms,active_phase,last_error,result_json,
                    functional_version_id,business_date,created_at,updated_at,owner_token,owner_scope
                ) VALUES(?,?,'running',?,?,NULL,NULL,'','', '{}','','',?,?,?,?)
                """,
                (run_id, trigger_source, scheduled_for, started_at, started_at, started_at, owner, scope),
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

    def phase_started(self, run_id: str, phase_key: str, *, owner_token: str | None = None) -> None:
        _require_phase(phase_key)
        owner = require_warehouse_job_owner(self.runtime_dir, owner_token)
        now = self.timestamp_factory()
        with _connect(self.db_path) as conn:
            self._fence(conn, run_id, owner)
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
        owner_token: str | None = None,
    ) -> None:
        _require_phase(phase_key)
        owner = require_warehouse_job_owner(self.runtime_dir, owner_token)
        now = self.timestamp_factory()
        with _connect(self.db_path) as conn:
            self._fence(conn, run_id, owner)
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
                    _json(dict(details or {}) if conn.execute(
                        "SELECT public_job_id FROM sheet_vitrina_v1_warehouse_update_runs WHERE run_id=?", (run_id,)
                    ).fetchone()[0] else _bounded_details(details or {})),
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
        owner_token: str | None = None,
    ) -> None:
        owner = require_warehouse_job_owner(self.runtime_dir, owner_token)
        now = self.timestamp_factory()
        payload = dict(result or {})
        active_version = dict(payload.get("active_version") or {})
        with _connect(self.db_path) as conn:
            self._fence(conn, run_id, owner)
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
                    _json(payload if conn.execute(
                        "SELECT public_job_id FROM sheet_vitrina_v1_warehouse_update_runs WHERE run_id=?", (run_id,)
                    ).fetchone()[0] else _bounded_details(payload)),
                    str(active_version.get("version_id") or payload.get("functional_version_id") or ""),
                    str(active_version.get("business_effective_date") or payload.get("business_date") or "")[:10],
                    now,
                    run_id,
                ),
            )
            conn.commit()

    def _fence(self, conn: sqlite3.Connection, run_id: str, owner: str) -> None:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT owner_token,status FROM sheet_vitrina_v1_warehouse_update_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None or row["owner_token"] != owner or row["status"] != "running":
            raise WarehouseJobOwnershipError("warehouse run is not running under this admission owner")

    def public_status(self, *, request_scope: str | None = None) -> dict[str, Any]:
        with _connect(self.db_path, query_only=True) as conn:
            runs = [dict(row) for row in conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_update_runs "
                "WHERE (? IS NULL OR request_scope=? OR request_scope='') "
                "ORDER BY started_at DESC,run_id DESC LIMIT 50", (request_scope, request_scope),
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
                    FROM sheet_vitrina_v1_warehouse_update_phases phase
                    JOIN sheet_vitrina_v1_warehouse_update_runs run ON run.run_id=phase.run_id
                    WHERE last_good_at IS NOT NULL AND last_good_at<>''
                      AND (? IS NULL OR run.request_scope=? OR run.request_scope='')
                    GROUP BY phase_key
                    """, (request_scope, request_scope),
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
    columns = {str(row[1]) for row in conn.execute(
        "PRAGMA table_info(sheet_vitrina_v1_warehouse_update_runs)"
    )}
    for name in ("owner_token", "owner_scope", "public_job_id", "request_key", "request_scope",
                 "payload_fingerprint", "request_payload_json", "attempt_id"):
        if name not in columns:
            conn.execute(f"ALTER TABLE sheet_vitrina_v1_warehouse_update_runs ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")

    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS warehouse_update_public_id "
                 "ON sheet_vitrina_v1_warehouse_update_runs(public_job_id) WHERE public_job_id<>''")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS warehouse_update_request_key "
                 "ON sheet_vitrina_v1_warehouse_update_runs(request_scope,request_key) WHERE request_key<>''")


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
