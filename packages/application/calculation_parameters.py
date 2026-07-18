"""Versioned operator calculation parameters and Decimal Proxy 3 semantics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import sqlite3
from typing import Any, Mapping

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.business_time import current_business_date_iso


PROXY_BLOCK_KEY = "proxy_profit_margin"
INITIAL_EFFECTIVE_DATE = "2026-07-01"
INITIAL_VERSION_ID = "calculation_parameters_proxy_v1_20260701"

RATE_FIELDS: tuple[str, ...] = (
    "tax_rate",
    "wb_agent_and_other_rate",
    "acquiring_rate",
    "wb_logistics_rate",
    "wb_storage_rate",
    "penalties_adjustments_rate",
    "other_expense_rate",
)

RATE_LABELS_RU = {
    "tax_rate": "Налог",
    "wb_agent_and_other_rate": "Агентское вознаграждение WB и прочие расходы",
    "acquiring_rate": "Эквайринг",
    "wb_logistics_rate": "Логистика WB до покупателя",
    "wb_storage_rate": "Хранение WB",
    "penalties_adjustments_rate": "Штрафы/корректировки",
    "other_expense_rate": "Другие расходы",
}


@dataclass(frozen=True)
class ProxyParameters:
    effective_date: str
    buyout_rate: Decimal
    tax_rate: Decimal
    wb_agent_and_other_rate: Decimal
    acquiring_rate: Decimal
    wb_logistics_rate: Decimal
    wb_storage_rate: Decimal
    penalties_adjustments_rate: Decimal
    other_expense_rate: Decimal
    version_id: str = ""
    fingerprint: str = ""

    @property
    def included_expense_rate(self) -> Decimal:
        return sum((getattr(self, field) for field in RATE_FIELDS), Decimal("0"))

    @property
    def retained_share(self) -> Decimal:
        return Decimal("1") - self.included_expense_rate

    def public(self) -> dict[str, Any]:
        rates = {field: _text(getattr(self, field)) for field in RATE_FIELDS}
        return {
            "version_id": self.version_id,
            "effective_date": self.effective_date,
            "buyout_rate": _text(self.buyout_rate),
            **rates,
            "included_expense_rate": _text(self.included_expense_rate),
            "retained_share": _text(self.retained_share),
            "buyout_rate_pct": _text(self.buyout_rate * Decimal("100")),
            "included_expense_rate_pct": _text(self.included_expense_rate * Decimal("100")),
            "retained_share_pct": _text(self.retained_share * Decimal("100")),
            "fingerprint": self.fingerprint,
        }


DEFAULT_PROXY_PARAMETERS = ProxyParameters(
    effective_date=INITIAL_EFFECTIVE_DATE,
    buyout_rate=Decimal("0.91"),
    tax_rate=Decimal("0.06"),
    wb_agent_and_other_rate=Decimal("0.38"),
    acquiring_rate=Decimal("0"),
    wb_logistics_rate=Decimal("0"),
    wb_storage_rate=Decimal("0"),
    penalties_adjustments_rate=Decimal("0"),
    other_expense_rate=Decimal("0"),
    version_id=INITIAL_VERSION_ID,
)


def calculate_proxy_3(
    *,
    order_sum: Any,
    order_count: Any,
    canonical_wb_wac: Any,
    ads_sum: Any,
    parameters: ProxyParameters,
) -> dict[str, Decimal | None]:
    """Calculate one SKU without converting a missing operand into zero."""

    operands = {
        "order_sum": _optional_decimal(order_sum),
        "order_count": _optional_decimal(order_count),
        "canonical_wb_wac": _optional_decimal(canonical_wb_wac),
        "ads_sum": _optional_decimal(ads_sum),
    }
    if any(value is None for value in operands.values()):
        return {
            "expected_buyout_revenue": None,
            "expected_buyout_qty": None,
            "included_expense_rate": parameters.included_expense_rate,
            "proxy_profit_3": None,
            "proxy_margin_3": None,
        }
    expected_revenue = operands["order_sum"] * parameters.buyout_rate  # type: ignore[operator]
    expected_qty = operands["order_count"] * parameters.buyout_rate  # type: ignore[operator]
    profit = (
        expected_revenue * parameters.retained_share
        - expected_qty * operands["canonical_wb_wac"]  # type: ignore[operator]
        - operands["ads_sum"]  # type: ignore[operator]
    )
    return {
        "expected_buyout_revenue": expected_revenue,
        "expected_buyout_qty": expected_qty,
        "included_expense_rate": parameters.included_expense_rate,
        "proxy_profit_3": profit,
        "proxy_margin_3": None if expected_revenue == 0 else profit / expected_revenue,
    }


def aggregate_proxy_3(rows: list[Mapping[str, Any]]) -> dict[str, Decimal | None]:
    """TOTAL is a sum of SKU profits divided by summed expected revenue."""

    profits = [_optional_decimal(row.get("proxy_profit_3")) for row in rows]
    revenues = [_optional_decimal(row.get("expected_buyout_revenue")) for row in rows]
    if not rows or any(value is None for value in profits + revenues):
        return {"proxy_profit_3": None, "expected_buyout_revenue": None, "proxy_margin_3": None}
    profit = sum((value for value in profits if value is not None), Decimal("0"))
    revenue = sum((value for value in revenues if value is not None), Decimal("0"))
    return {
        "proxy_profit_3": profit,
        "expected_buyout_revenue": revenue,
        "proxy_margin_3": None if revenue == 0 else profit / revenue,
    }


class CalculationParametersBlock:
    def __init__(self, *, runtime: RegistryUploadDbBackedRuntime) -> None:
        self.runtime = runtime
        self.runtime.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.runtime.db_path) as conn:
            ensure_calculation_parameters_schema(conn)
            conn.commit()

    def ensure_initial_version(
        self,
        *,
        connection: sqlite3.Connection | None = None,
        created_by: str = "warehouse_functional_cutover",
        created_at: str | None = None,
    ) -> dict[str, Any]:
        own = connection is None
        conn = connection or _connect(self.runtime.db_path)
        try:
            # A caller-supplied connection may already be inside the guarded
            # functional cutover transaction.  ``executescript`` implicitly
            # commits in sqlite3, so schema DDL is permitted only on the
            # independently owned connection.  Constructors establish the
            # schema before any apply transaction begins.
            if own:
                ensure_calculation_parameters_schema(conn)
            existing = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_calculation_parameter_versions WHERE version_id=?",
                (INITIAL_VERSION_ID,),
            ).fetchone()
            if existing is not None:
                return {**_version_row(existing), "idempotent": True}
            now = created_at or _now()
            payload = DEFAULT_PROXY_PARAMETERS.public()
            fingerprint = _settings_fingerprint(payload)
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_calculation_parameter_versions(
                    version_id,block_key,revision,effective_date,rates_json,fingerprint,
                    source,created_by,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    INITIAL_VERSION_ID,
                    PROXY_BLOCK_KEY,
                    1,
                    INITIAL_EFFECTIVE_DATE,
                    _json(payload),
                    fingerprint,
                    "functional_cutover_initial_version",
                    created_by,
                    now,
                ),
            )
            if own:
                conn.commit()
            row = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_calculation_parameter_versions WHERE version_id=?",
                (INITIAL_VERSION_ID,),
            ).fetchone()
            return {**_version_row(row), "idempotent": False}
        finally:
            if own:
                conn.close()

    def parameters_for_date(self, effective_date: str) -> ProxyParameters:
        target = date.fromisoformat(str(effective_date)[:10]).isoformat()
        with _connect(self.runtime.db_path) as conn:
            row = conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_calculation_parameter_versions
                WHERE block_key=? AND effective_date<=?
                ORDER BY effective_date DESC,revision DESC,created_at DESC LIMIT 1
                """,
                (PROXY_BLOCK_KEY, target),
            ).fetchone()
        if row is None:
            return DEFAULT_PROXY_PARAMETERS
        return _parameters_from_row(row)

    def preview_version(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        parameters = _parameters_from_payload(payload)
        current = self.parameters_for_date(parameters.effective_date)
        normalized = parameters.public()
        fingerprint = _settings_fingerprint(normalized)
        diff = []
        for field in ("buyout_rate", *RATE_FIELDS):
            before = getattr(current, field)
            after = getattr(parameters, field)
            if before != after:
                diff.append(
                    {
                        "field": field,
                        "label": "Коэффициент выкупа" if field == "buyout_rate" else RATE_LABELS_RU[field],
                        "before_pct": _text(before * Decimal("100")),
                        "after_pct": _text(after * Decimal("100")),
                    }
                )
        return {
            "status": "preview_ready",
            "parameters": normalized,
            "diff": diff,
            "preview_fingerprint": fingerprint,
            "formula_preview": (
                "orderSum × buyout_rate × retained_share − "
                "orderCount × buyout_rate × canonical_WB_WAC − ads_sum"
            ),
        }

    def create_version(
        self,
        payload: Mapping[str, Any],
        *,
        preview_fingerprint: str,
        created_by: str,
    ) -> dict[str, Any]:
        with _connect(self.runtime.db_path) as preflight_conn:
            initial = preflight_conn.execute(
                "SELECT 1 FROM sheet_vitrina_v1_calculation_parameter_versions WHERE version_id=?",
                (INITIAL_VERSION_ID,),
            ).fetchone()
        if initial is None:
            raise ValueError(
                "calculation parameters cannot be saved before the functional cutover initial version"
            )
        preview = self.preview_version(payload)
        if preview["preview_fingerprint"] != str(preview_fingerprint or ""):
            raise ValueError("calculation parameters changed after preview")
        parameters = _parameters_from_payload(payload)
        now = _now()
        with _connect(self.runtime.db_path) as conn:
            ensure_calculation_parameters_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            revision = int(
                conn.execute(
                    "SELECT COALESCE(MAX(revision),0)+1 FROM sheet_vitrina_v1_calculation_parameter_versions WHERE block_key=?",
                    (PROXY_BLOCK_KEY,),
                ).fetchone()[0]
            )
            version_id = f"calculation_parameters_proxy_v{revision}_{parameters.effective_date.replace('-', '')}"
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_calculation_parameter_versions(
                    version_id,block_key,revision,effective_date,rates_json,fingerprint,
                    source,created_by,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    version_id,
                    PROXY_BLOCK_KEY,
                    revision,
                    parameters.effective_date,
                    _json(parameters.public()),
                    preview["preview_fingerprint"],
                    "operator_version",
                    created_by,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_proxy_targeted_recalc_queue(
                    request_id,effective_date,settings_version_id,status,created_at
                ) VALUES(?,?,?,?,?)
                """,
                (f"proxy_recalc:{version_id}", parameters.effective_date, version_id, "pending", now),
            )
            conn.commit()
        recalculation = self.process_pending_targeted_recalculations()
        return {
            **self.get_payload(),
            "created_version_id": version_id,
            "diff": preview["diff"],
            "targeted_recalculation": recalculation,
        }

    def process_pending_targeted_recalculations(self) -> dict[str, Any]:
        with _connect(self.runtime.db_path) as conn:
            ensure_calculation_parameters_schema(conn)
            pending = [dict(row) for row in conn.execute(
                """SELECT * FROM sheet_vitrina_v1_proxy_targeted_recalc_queue
                   WHERE status IN ('pending','failed') ORDER BY effective_date,created_at,request_id"""
            ).fetchall()]
        if not pending:
            return {"status": "idle", "request_count": 0}
        request_ids = [str(item["request_id"]) for item in pending]
        try:
            result = self.publish_current_functional_economics()
        except Exception as exc:
            with _connect(self.runtime.db_path) as conn:
                placeholders = ",".join("?" for _ in request_ids)
                conn.execute(
                    f"""UPDATE sheet_vitrina_v1_proxy_targeted_recalc_queue
                        SET status='failed',error=? WHERE request_id IN ({placeholders})""",
                    (str(exc), *request_ids),
                )
                conn.commit()
            return {"status": "failed", "request_count": len(request_ids), "error": str(exc)}
        completed_at = _now()
        with _connect(self.runtime.db_path) as conn:
            placeholders = ",".join("?" for _ in request_ids)
            conn.execute(
                f"""UPDATE sheet_vitrina_v1_proxy_targeted_recalc_queue
                    SET status='complete',completed_at=?,error=NULL
                    WHERE request_id IN ({placeholders})""",
                (completed_at, *request_ids),
            )
            conn.commit()
        return {
            "status": "complete",
            "request_count": len(request_ids),
            "plan_fingerprint": result["plan_fingerprint"],
            "changed_snapshot_count": int(result.get("changed_snapshot_count") or 0),
            "database_written": bool(result.get("database_written")),
        }

    def publish_current_functional_economics(self) -> dict[str, Any]:
        """Publish only WB cost/Proxy target cells from the active functional state."""

        from packages.application.warehouse_functional_economics_backfill import (
            apply_functional_economics_backfill_plan,
            build_functional_economics_backfill_plan,
        )

        plan = build_functional_economics_backfill_plan(self.runtime)
        return apply_functional_economics_backfill_plan(
            self.runtime,
            plan,
            confirm_fingerprint=str(plan["plan_fingerprint"]),
            backup_dir=(self.runtime.runtime_dir / "backups" / "calculation-parameters").resolve(),
        )

    def get_payload(self) -> dict[str, Any]:
        with _connect(self.runtime.db_path) as conn:
            rows = conn.execute(
                """SELECT * FROM sheet_vitrina_v1_calculation_parameter_versions
                   WHERE block_key=? ORDER BY effective_date DESC,revision DESC""",
                (PROXY_BLOCK_KEY,),
            ).fetchall()
            recalc_rows = conn.execute(
                """SELECT * FROM sheet_vitrina_v1_proxy_targeted_recalc_queue
                   ORDER BY created_at DESC,request_id DESC LIMIT 20"""
            ).fetchall()
        history = [_version_row(row) for row in rows]
        today = current_business_date_iso()
        current = next((item for item in history if str(item.get("effective_date") or "") <= today), None)
        current = current or ({
            "version_id": "planned_default",
            "effective_date": INITIAL_EFFECTIVE_DATE,
            "parameters": DEFAULT_PROXY_PARAMETERS.public(),
            "status": "awaiting_functional_cutover",
        })
        return {
            "contract_name": "sheet_vitrina_v1_calculation_parameters",
            "contract_version": "v1",
            "status": "ready" if history else "awaiting_functional_cutover",
            "current": current,
            "history": history,
            "targeted_recalculation_history": [dict(row) for row in recalc_rows],
            "reference": self._three_closed_week_reference(),
        }

    def _three_closed_week_reference(self) -> dict[str, Any]:
        today = date.fromisoformat(current_business_date_iso())
        last_closed_sunday = today - timedelta(days=today.weekday() + 1)
        with _connect(self.runtime.db_path) as conn:
            if not _table_exists(conn, "wb_finance_weekly_aggregates"):
                return {"status": "unavailable", "weeks": [], "rows": []}
            closed_weeks = conn.execute(
                """
                SELECT DISTINCT week_start,week_end FROM wb_finance_weekly_aggregates
                WHERE week_end<=? ORDER BY week_end DESC LIMIT 3
                """,
                (last_closed_sunday.isoformat(),),
            ).fetchall()
            week_keys = [(str(row["week_start"]), str(row["week_end"])) for row in reversed(closed_weeks)]
            source_rows = []
            for week_start, week_end in week_keys:
                source_rows.extend(
                    conn.execute(
                        """SELECT week_start,week_end,metrics_json FROM wb_finance_weekly_aggregates
                           WHERE week_start=? AND week_end=? ORDER BY seller_id""",
                        (week_start, week_end),
                    ).fetchall()
                )
        metric_keys = (
            "net_revenue",
            "commission",
            "acquiring",
            "logistics",
            "storage",
            "acceptance",
            "penalties",
        )
        metrics_by_week: dict[tuple[str, str], dict[str, Decimal]] = {
            key: {metric: Decimal("0") for metric in metric_keys} for key in week_keys
        }
        for row in source_rows:
            target = metrics_by_week[(str(row["week_start"]), str(row["week_end"]))]
            source = _json_loads(row["metrics_json"])
            for metric in metric_keys:
                target[metric] += _decimal(source.get(metric))
        weeks = [{"week_start": start, "week_end": end} for start, end in week_keys]
        metrics = [metrics_by_week[key] for key in week_keys]
        base_key = "net_revenue"
        specs = (
            ("wb_agent_and_other", "Агентское вознаграждение", lambda item: _decimal(item.get("commission")) - _decimal(item.get("acquiring"))),
            ("acquiring", "Эквайринг", lambda item: _decimal(item.get("acquiring"))),
            ("logistics", "Логистика WB", lambda item: _decimal(item.get("logistics"))),
            ("storage", "Хранение WB", lambda item: _decimal(item.get("storage"))),
            ("acceptance", "Платная приёмка", lambda item: _decimal(item.get("acceptance"))),
            ("penalties", "Штрафы/корректировки", lambda item: _decimal(item.get("penalties"))),
        )
        result_rows = []
        total_base = sum((_decimal(item.get(base_key)) for item in metrics), Decimal("0"))
        for key, label, expense_fn in specs:
            expenses = [expense_fn(item) for item in metrics]
            weekly = [
                None if _decimal(item.get(base_key)) == 0 else expense / _decimal(item.get(base_key))
                for item, expense in zip(metrics, expenses)
            ]
            result_rows.append(
                {
                    "key": key,
                    "label": label,
                    "weekly_rate_pct": [None if value is None else _text(value * Decimal("100")) for value in weekly],
                    "weighted_average_pct": None if total_base == 0 else _text(sum(expenses, Decimal("0")) / total_base * Decimal("100")),
                    "included_in_proxy_by_default": key == "wb_agent_and_other",
                    "note": "excluded_from_proxy_cost_already_capitalized" if key == "acceptance" else ("shown_separately_from_commission" if key == "acquiring" else ""),
                }
            )
        return {
            "status": "ready" if len(weeks) == 3 else "partial",
            "gross_buyout_revenue_field": base_key,
            "weeks": weeks,
            "rows": result_rows,
        }


def ensure_calculation_parameters_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_calculation_parameter_versions(
            version_id TEXT PRIMARY KEY,
            block_key TEXT NOT NULL,
            revision INTEGER NOT NULL,
            effective_date TEXT NOT NULL,
            rates_json TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            source TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(block_key,revision)
        );
        CREATE INDEX IF NOT EXISTS calculation_parameters_by_effective_date
        ON sheet_vitrina_v1_calculation_parameter_versions(block_key,effective_date DESC,revision DESC);
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_proxy_targeted_recalc_queue(
            request_id TEXT PRIMARY KEY,
            effective_date TEXT NOT NULL,
            settings_version_id TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            error TEXT
        );
        """
    )


def _parameters_from_payload(payload: Mapping[str, Any]) -> ProxyParameters:
    effective_date = date.fromisoformat(str(payload.get("effective_date") or "")[:10]).isoformat()
    if effective_date < INITIAL_EFFECTIVE_DATE:
        raise ValueError(f"effective_date must be on or after {INITIAL_EFFECTIVE_DATE}")
    values = {"buyout_rate": _rate(payload.get("buyout_rate"), "buyout_rate")}
    values.update({field: _rate(payload.get(field, "0"), field) for field in RATE_FIELDS})
    result = ProxyParameters(effective_date=effective_date, **values)
    if result.included_expense_rate >= Decimal("1"):
        raise ValueError("total included expenses must be below 100%")
    return result


def _parameters_from_row(row: sqlite3.Row) -> ProxyParameters:
    raw = _json_loads(row["rates_json"])
    return ProxyParameters(
        effective_date=str(row["effective_date"]),
        buyout_rate=_rate(raw.get("buyout_rate"), "buyout_rate"),
        **{field: _rate(raw.get(field, "0"), field) for field in RATE_FIELDS},
        version_id=str(row["version_id"]),
        fingerprint=str(row["fingerprint"]),
    )


def _version_row(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        raise ValueError("calculation parameter version disappeared")
    parameters = _parameters_from_row(row)
    return {
        "version_id": str(row["version_id"]),
        "revision": int(row["revision"]),
        "effective_date": str(row["effective_date"]),
        "parameters": parameters.public(),
        "fingerprint": str(row["fingerprint"]),
        "source": str(row["source"]),
        "created_by": str(row["created_by"]),
        "created_at": str(row["created_at"]),
    }


def _rate(value: Any, field: str) -> Decimal:
    result = _decimal(value)
    if result < 0 or result > 1:
        raise ValueError(f"{field} must be between 0 and 1")
    return result


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value if value not in (None, "") else "0"))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid decimal: {value!r}") from exc
    if not result.is_finite():
        raise ValueError("decimal must be finite")
    return result


def _optional_decimal(value: Any) -> Decimal | None:
    return None if value is None or value == "" else _decimal(value)


def _text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_loads(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        loaded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(loaded) if isinstance(loaded, Mapping) else {}


def _settings_fingerprint(value: Mapping[str, Any]) -> str:
    semantic = {key: item for key, item in value.items() if key not in {"version_id", "fingerprint"}}
    return "sha256:" + hashlib.sha256(_json(semantic).encode("utf-8")).hexdigest()


def _connect(db_path: Any) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
