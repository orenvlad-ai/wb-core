"""Immutable automatic parameters and Decimal formula semantics for Proxy 4."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import sqlite3
from typing import Any, Callable, Mapping

from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
)
from packages.application.sheet_vitrina_v1_buyout_percent import (
    BUYOUT_PERCENT_AGGREGATION_RULE,
    aggregate_buyout_percent,
    build_three_closed_week_buyout_reference,
    three_closed_week_keys,
)
from packages.application.sqlite_contention import connect_sqlite
from packages.application.wb_finance_weekly import (
    CLASSIFIER_VERSION as WB_FINANCE_CLASSIFIER_VERSION,
)
from packages.application.warehouse_sync_lock import (
    WarehouseSyncBusyError,
    warehouse_sync_lock,
)
from packages.business_time import (
    CANONICAL_BUSINESS_TIMEZONE_NAME,
    business_date_from_timestamp,
    current_business_date_iso,
)


PROXY_V4_BLOCK_KEY = "proxy_profit_margin_v4"
PROXY_V4_FIXED_BOUNDARY = "2026-08-01"
PROXY_V4_CONTRACT_VERSION = "sheet_vitrina_v1_proxy_v4_parameters_v3_no_transit"
PROXY_V4_LEGACY_FORMULA_VERSION = "proxy_profit_4_v1"
PROXY_V4_FORMULA_VERSION = "proxy_profit_4_v2_no_transit"
PROXY_V4_LATEST_WEEK_SELECTION_CONTRACT_VERSION = (
    "sheet_vitrina_v1_proxy_v4_latest_confirmed_week_v2_no_transit"
)
PROXY_V4_INITIAL_EFFECTIVE_DATES = ("2026-08-01", "2026-08-08")

AUTOMATIC_RATE_FIELDS: tuple[str, ...] = (
    "buyout_rate",
    "agent_remuneration_rate",
    "acquiring_rate",
    "wb_logistics_rate",
    "wb_storage_rate",
    "penalties_adjustments_rate",
    "other_expense_rate",
)
EXPENSE_RATE_FIELDS: tuple[str, ...] = (
    "tax_rate",
    "agent_remuneration_rate",
    "acquiring_rate",
    "wb_logistics_rate",
    "wb_storage_rate",
    "penalties_adjustments_rate",
    "other_expense_rate",
)

RATE_LABELS_RU = {
    "buyout_rate": "Расчётный выкуп (подтверждённый)",
    "tax_rate": "Налог",
    "agent_remuneration_rate": "Агентское вознаграждение WB",
    "acquiring_rate": "Эквайринг",
    "wb_logistics_rate": "Логистика WB до покупателя",
    "wb_storage_rate": "Хранение WB",
    "penalties_adjustments_rate": "Штрафы и корректировки расходов",
    "other_expense_rate": "Другие расходы",
}

_FINANCE_REQUIRED_DIRECT_FIELDS = (
    "net_revenue",
    "acquiring",
    "logistics",
    "storage",
    "penalties",
    "corrections",
    "subscriptions",
    "paid_services",
    "review_points",
    "other_deductions",
    "acceptance",
    "capitalized_acceptance",
    "transit_logistics",
    "capitalized_transit_logistics",
)


@dataclass(frozen=True)
class ProxyV4Parameters:
    effective_date: str
    buyout_rate: Decimal
    tax_rate: Decimal
    agent_remuneration_rate: Decimal
    acquiring_rate: Decimal
    wb_logistics_rate: Decimal
    wb_storage_rate: Decimal
    penalties_adjustments_rate: Decimal
    other_expense_rate: Decimal
    source_window_from: str
    source_window_to: str
    source_window_fingerprint: str
    source_week_ranges: tuple[tuple[str, str], ...]
    source_slot_from: str
    source_slot_to: str
    buyout_order_count_weight: Decimal
    finance_net_revenue_weight: Decimal
    formula_version: str
    version_id: str = ""
    revision: int = 0
    version_kind: str = ""
    created_at: str = ""
    created_by: str = ""
    fingerprint: str = ""

    @property
    def included_expense_rate(self) -> Decimal:
        return sum(
            (getattr(self, field) for field in EXPENSE_RATE_FIELDS),
            Decimal("0"),
        )

    @property
    def retained_share(self) -> Decimal:
        return Decimal("1") - self.included_expense_rate

    def public(self) -> dict[str, Any]:
        rates = {
            field: _text(getattr(self, field))
            for field in ("buyout_rate", *EXPENSE_RATE_FIELDS)
        }
        rate_percentages = {
            f"{field}_pct": _text(getattr(self, field) * Decimal("100"))
            for field in ("buyout_rate", *EXPENSE_RATE_FIELDS)
        }
        latest_week_mode = len(self.source_week_ranges) == 1
        source_selection_mode = (
            "latest_confirmed_week"
            if latest_week_mode
            else "frozen_legacy_multi_week"
        )
        coverage_text = (
            "последняя подтверждённая неделя: "
            f"{self.source_week_ranges[0][0]} — {self.source_week_ranges[0][1]}"
            if latest_week_mode
            else (
                "историческая frozen version: расчёт по "
                f"{len(self.source_week_ranges)} подтверждённым неделям"
            )
        )
        return {
            "version_id": self.version_id,
            "revision": self.revision,
            "effective_date": self.effective_date,
            **rates,
            **rate_percentages,
            "included_expense_rate": _text(self.included_expense_rate),
            "included_expense_rate_pct": _text(
                self.included_expense_rate * Decimal("100")
            ),
            "retained_share": _text(self.retained_share),
            "retained_share_pct": _text(self.retained_share * Decimal("100")),
            "source_window_from": self.source_window_from,
            "source_window_to": self.source_window_to,
            "source_window_fingerprint": self.source_window_fingerprint,
            "source_week_ranges": [list(item) for item in self.source_week_ranges],
            "source_week_count": len(self.source_week_ranges),
            "source_selection_mode": source_selection_mode,
            "selected_week_range": (
                list(self.source_week_ranges[0]) if latest_week_mode else None
            ),
            "source_slot_from": self.source_slot_from,
            "source_slot_to": self.source_slot_to,
            "buyout_order_count_weight": _text(self.buyout_order_count_weight),
            "finance_net_revenue_weight": _text(self.finance_net_revenue_weight),
            "coverage_text": coverage_text,
            "version_kind": self.version_kind,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "fingerprint": self.fingerprint,
            "formula_version": self.formula_version,
        }


def calculate_proxy_4(
    *,
    order_sum: Any,
    order_count: Any,
    canonical_wb_wac: Any,
    ads_sum: Any,
    parameters: ProxyV4Parameters | None,
    business_date: str,
) -> dict[str, Decimal | None]:
    """Calculate one SKU while preserving the fixed boundary and missing values."""

    if str(business_date)[:10] < PROXY_V4_FIXED_BOUNDARY or parameters is None:
        return _blank_proxy_4()
    operands = {
        "order_sum": _optional_decimal(order_sum),
        "order_count": _optional_decimal(order_count),
        "canonical_wb_wac": _optional_decimal(canonical_wb_wac),
        "ads_sum": _optional_decimal(ads_sum),
    }
    if any(value is None for value in operands.values()):
        return {
            **_blank_proxy_4(),
            "included_expense_rate": parameters.included_expense_rate,
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
        "proxy_profit_4": profit,
        "proxy_margin_4": None if expected_revenue == 0 else profit / expected_revenue,
    }


def aggregate_proxy_4(rows: list[Mapping[str, Any]]) -> dict[str, Decimal | None]:
    """TOTAL sums only eligible SKU results and divides by their revenue."""

    eligible: list[tuple[Decimal, Decimal]] = []
    for row in rows:
        profit = _optional_decimal(row.get("proxy_profit_4"))
        revenue = _optional_decimal(row.get("expected_buyout_revenue"))
        if profit is not None and revenue is not None:
            eligible.append((profit, revenue))
    if not eligible:
        return {
            "proxy_profit_4": None,
            "expected_buyout_revenue": None,
            "proxy_margin_4": None,
        }
    profit = sum((item[0] for item in eligible), Decimal("0"))
    revenue = sum((item[1] for item in eligible), Decimal("0"))
    return {
        "proxy_profit_4": profit,
        "expected_buyout_revenue": revenue,
        "proxy_margin_4": None if revenue == 0 else profit / revenue,
    }


def build_confirmed_aligned_window(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    today: date,
    finance_available_by: str | None = None,
) -> dict[str, Any]:
    """Build rates from the exact READY Buyout/Finance week intersection."""

    week_keys = three_closed_week_keys(today)
    buyout = build_three_closed_week_buyout_reference(runtime=runtime, today=today)
    finance = _build_finance_window(
        runtime=runtime,
        week_keys=week_keys,
        available_by=finance_available_by,
    )
    buyout_ranges = [
        (str(item.get("week_start") or ""), str(item.get("week_end") or ""))
        for item in buyout.get("weeks") or []
    ]
    aligned = buyout_ranges == week_keys and list(finance["week_keys"]) == week_keys
    buyout_by_range = {
        (str(item.get("week_start") or ""), str(item.get("week_end") or "")): item
        for item in buyout.get("weeks") or []
    }
    finance_by_range = {
        (str(item.get("week_start") or ""), str(item.get("week_end") or "")): item
        for item in finance.get("weeks") or []
    }
    contributing_week_ranges = [
        key
        for key in week_keys
        if aligned
        and str(buyout_by_range.get(key, {}).get("status") or "") == "ready"
        and str(finance_by_range.get(key, {}).get("status") or "") == "ready"
    ]
    buyout_combined = _combine_buyout_week_results(
        buyout_by_range,
        contributing_week_ranges,
    )
    finance_combined = _combine_finance_week_results(
        finance_by_range,
        contributing_week_ranges,
    )
    ready = (
        aligned
        and bool(contributing_week_ranges)
        and buyout_combined["value"] is not None
        and finance_combined["status"] == "ready"
    )
    automatic_rates: dict[str, str] = {}
    blockers: list[str] = []
    if not aligned:
        blockers.append("Buyout and Finance week ranges are not aligned.")
    for key in week_keys:
        buyout_status = str(buyout_by_range.get(key, {}).get("status") or "missing")
        finance_week = finance_by_range.get(key, {})
        finance_status = str(finance_week.get("status") or "missing")
        if buyout_status != "ready" or finance_status != "ready":
            reasons = [
                f"Buyout={buyout_status}",
                f"Finance={finance_status}",
                *[str(item) for item in finance_week.get("reasons") or []],
            ]
            blockers.append(f"{key[0]}..{key[1]} excluded ({', '.join(reasons)}).")
    if ready:
        buyout_rate = _decimal(buyout_combined["value"])
        finance_rates = dict(finance_combined["rates"])
        automatic_rates = {
            "buyout_rate": _text(buyout_rate),
            **{field: _text(_decimal(finance_rates[field])) for field in AUTOMATIC_RATE_FIELDS if field != "buyout_rate"},
        }
    fingerprint_payload = {
        "contract": PROXY_V4_CONTRACT_VERSION,
        "source_week_ranges": contributing_week_ranges,
        "buyout": {
            "value": buyout_combined["value"],
            "order_count_weight": buyout_combined["order_count_weight"],
            "included_sku_day_count": buyout_combined["included_sku_day_count"],
            "source_payload_digests": [
                {
                    "week_start": week_start,
                    "week_end": week_end,
                    "digest": _buyout_source_payload_digest(
                        runtime,
                        date_from=week_start,
                        date_to=week_end,
                    ),
                }
                for week_start, week_end in contributing_week_ranges
            ],
        },
        "finance": finance_combined["fingerprint_payload"],
        "automatic_rates": automatic_rates,
    }
    coverage_text = (
        f"расчёт по {len(contributing_week_ranges)} из {len(week_keys)} подтверждённых недель"
    )
    return {
        "status": "ready" if ready else "unavailable",
        "status_message": (
            "Buyout и Finance используют одно точное пересечение READY COMPLETE недель; "
            + coverage_text
            + "."
            if ready
            else "Новая V4 version не создаётся: нет общего READY COMPLETE периода. "
            + " ".join(blockers)
        ),
        "business_timezone": CANONICAL_BUSINESS_TIMEZONE_NAME,
        "business_date": today.isoformat(),
        "source_window_from": (
            contributing_week_ranges[0][0] if contributing_week_ranges else ""
        ),
        "source_window_to": (
            contributing_week_ranges[-1][1] if contributing_week_ranges else ""
        ),
        "source_slot_from": week_keys[0][0],
        "source_slot_to": week_keys[-1][1],
        "week_keys": [list(item) for item in week_keys],
        "source_week_ranges": [list(item) for item in contributing_week_ranges],
        "ready_week_count": len(contributing_week_ranges),
        "required_week_count": len(week_keys),
        "coverage_text": coverage_text,
        "buyout": buyout,
        "aligned_buyout": {
            "value": buyout_combined["value"],
            "weighted_average_pct": (
                None
                if buyout_combined["value"] is None
                else _text(_decimal(buyout_combined["value"]) * Decimal("100"))
            ),
            "order_count_weight": buyout_combined["order_count_weight"],
            "included_sku_day_count": buyout_combined["included_sku_day_count"],
        },
        "finance": {key: value for key, value in finance.items() if key != "fingerprint_payload"},
        "aligned_finance": {
            key: value
            for key, value in finance_combined.items()
            if key != "fingerprint_payload"
        },
        "automatic_rates": automatic_rates,
        "source_window_fingerprint": _digest(fingerprint_payload),
        "buyout_aggregation_rule": BUYOUT_PERCENT_AGGREGATION_RULE,
        "finance_aggregation_rule": "SUM(signed amount) / SUM(net_revenue)",
        "blockers": blockers,
    }


def build_latest_confirmed_week_window(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    today: date,
    finance_available_by: str | None = None,
) -> dict[str, Any]:
    """Select one freshest week that is READY COMPLETE in Buyout and Finance."""

    reference = build_confirmed_aligned_window(
        runtime=runtime,
        today=today,
        finance_available_by=finance_available_by,
    )
    week_keys = [
        (str(item[0]), str(item[1])) for item in reference.get("week_keys") or []
    ]
    common_ready_ranges = [
        (str(item[0]), str(item[1]))
        for item in reference.get("source_week_ranges") or []
    ]
    selected_ranges = common_ready_ranges[-1:]
    buyout_by_range = {
        (str(item.get("week_start") or ""), str(item.get("week_end") or "")): item
        for item in reference.get("buyout", {}).get("weeks") or []
    }
    finance_by_range = {
        (str(item.get("week_start") or ""), str(item.get("week_end") or "")): item
        for item in reference.get("finance", {}).get("weeks") or []
    }
    buyout_selected = _combine_buyout_week_results(
        buyout_by_range,
        selected_ranges,
    )
    finance_selected = _combine_finance_week_results(
        finance_by_range,
        selected_ranges,
    )
    ready = (
        bool(selected_ranges)
        and buyout_selected["value"] is not None
        and finance_selected["status"] == "ready"
    )
    automatic_rates: dict[str, str] = {}
    if ready:
        automatic_rates = {
            "buyout_rate": _text(_decimal(buyout_selected["value"])),
            **{
                field: _text(_decimal(finance_selected["rates"][field]))
                for field in AUTOMATIC_RATE_FIELDS
                if field != "buyout_rate"
            },
        }
    selected = selected_ranges[0] if selected_ranges else ("", "")
    fingerprint_payload = {
        "contract": PROXY_V4_LATEST_WEEK_SELECTION_CONTRACT_VERSION,
        "selection_policy": "latest_confirmed_common_week",
        "selected_week_range": selected_ranges,
        "buyout": {
            "value": buyout_selected["value"],
            "order_count_weight": buyout_selected["order_count_weight"],
            "included_sku_day_count": buyout_selected["included_sku_day_count"],
            "source_payload_digest": (
                _buyout_source_payload_digest(
                    runtime,
                    date_from=selected[0],
                    date_to=selected[1],
                )
                if selected_ranges
                else ""
            ),
        },
        "finance": finance_selected["fingerprint_payload"],
        "automatic_rates": automatic_rates,
    }
    common_coverage = (
        f"общих READY COMPLETE недель: {len(common_ready_ranges)} из {len(week_keys)}"
    )
    coverage_text = (
        f"последняя подтверждённая неделя: {selected[0]} — {selected[1]}; "
        + common_coverage
        if ready
        else common_coverage
    )
    return {
        "status": "ready" if ready else "unavailable",
        "status_message": (
            "Формула V4 использует ровно одну самую свежую общую READY COMPLETE неделю: "
            f"{selected[0]} — {selected[1]}."
            if ready
            else (
                "Новая V4 version не создаётся: среди трёх последних закрытых недель "
                "нет общей READY COMPLETE недели Buyout и Finance. "
                + " ".join(str(item) for item in reference.get("blockers") or [])
            )
        ),
        "business_timezone": CANONICAL_BUSINESS_TIMEZONE_NAME,
        "business_date": today.isoformat(),
        "selection_policy": "latest_confirmed_common_week",
        "selection_contract_version": PROXY_V4_LATEST_WEEK_SELECTION_CONTRACT_VERSION,
        "source_window_from": selected[0],
        "source_window_to": selected[1],
        "source_slot_from": str(reference.get("source_slot_from") or ""),
        "source_slot_to": str(reference.get("source_slot_to") or ""),
        "week_keys": [list(item) for item in week_keys],
        "common_ready_week_ranges": [list(item) for item in common_ready_ranges],
        "common_ready_week_count": len(common_ready_ranges),
        "source_week_ranges": [list(item) for item in selected_ranges],
        "selected_week_range": list(selected) if selected_ranges else None,
        "selected_week_count": 1 if ready else 0,
        "ready_week_count": len(common_ready_ranges),
        "required_week_count": len(week_keys),
        "coverage_text": coverage_text,
        "buyout": reference.get("buyout") or {},
        "aligned_buyout": {
            "value": buyout_selected["value"],
            "weighted_average_pct": (
                None
                if buyout_selected["value"] is None
                else _text(_decimal(buyout_selected["value"]) * Decimal("100"))
            ),
            "order_count_weight": buyout_selected["order_count_weight"],
            "included_sku_day_count": buyout_selected["included_sku_day_count"],
        },
        "finance": reference.get("finance") or {},
        "aligned_finance": {
            key: value
            for key, value in finance_selected.items()
            if key != "fingerprint_payload"
        },
        "automatic_rates": automatic_rates,
        "source_window_fingerprint": _digest(fingerprint_payload),
        "buyout_aggregation_rule": BUYOUT_PERCENT_AGGREGATION_RULE,
        "finance_aggregation_rule": "SUM(signed amount) / SUM(net_revenue)",
        "blockers": list(reference.get("blockers") or []),
    }


class ProxyV4ParametersBlock:
    def __init__(
        self,
        *,
        runtime: RegistryUploadDbBackedRuntime,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.runtime = runtime
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self.runtime.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.runtime.db_path) as conn:
            ensure_proxy_v4_schema(conn)
            conn.commit()

    def parameters_for_date(self, effective_date: str) -> ProxyV4Parameters | None:
        target = date.fromisoformat(str(effective_date)[:10]).isoformat()
        if target < PROXY_V4_FIXED_BOUNDARY:
            return None
        with _connect(self.runtime.db_path) as conn:
            row = conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_proxy_v4_parameter_versions
                WHERE block_key=? AND effective_date<=?
                ORDER BY effective_date DESC,revision DESC,created_at DESC LIMIT 1
                """,
                (PROXY_V4_BLOCK_KEY, target),
            ).fetchone()
        return None if row is None else _parameters_from_row(row)

    def materialize_latest_confirmed_window(
        self,
        *,
        business_date: str | None = None,
        created_by: str = "sheet_vitrina_v1_refresh",
    ) -> dict[str, Any]:
        effective_date = date.fromisoformat(
            str(business_date or current_business_date_iso(self.now_factory()))[:10]
        ).isoformat()
        if effective_date < PROXY_V4_FIXED_BOUNDARY:
            return {
                "status": "pre_boundary",
                "created": False,
                "effective_date": effective_date,
                "detail": f"Proxy V4 starts on {PROXY_V4_FIXED_BOUNDARY}.",
            }
        history = self._history_rows()
        if not history:
            return {
                "status": "initialization_required",
                "created": False,
                "effective_date": effective_date,
                "detail": "Guarded historical initialization must create the 2026-08-01 as-of version first.",
            }
        window = build_latest_confirmed_week_window(
            runtime=self.runtime,
            today=date.fromisoformat(effective_date),
        )
        current = _parameters_from_row(history[0])
        if window["status"] != "ready":
            return {
                "status": "stale",
                "created": False,
                "effective_date": effective_date,
                "current_version_id": current.version_id,
                "detail": window["status_message"],
                "window": window,
            }
        candidate_fingerprint = str(window["source_window_fingerprint"])
        candidate_weeks = tuple(
            (str(item[0]), str(item[1])) for item in window["source_week_ranges"]
        )
        current_weeks = current.source_week_ranges
        if candidate_fingerprint == current.source_window_fingerprint:
            return {
                "status": "already_materialized",
                "created": False,
                "effective_date": effective_date,
                "current_version_id": current.version_id,
                "window": window,
            }
        if candidate_weeks == current_weeks:
            return {
                "status": "historical_repair_required",
                "created": False,
                "effective_date": effective_date,
                "current_version_id": current.version_id,
                "detail": "Те же contributing weeks изменили payload; обычный rollover не переписывает frozen V4 history.",
                "window": window,
            }
        legacy_to_latest_transition = (
            len(current_weeks) > 1
            and len(candidate_weeks) == 1
            and candidate_weeks[0] == current_weeks[-1]
        )
        if (
            not legacy_to_latest_transition
            and candidate_weeks
            and candidate_weeks[-1][1] <= current.source_window_to
        ):
            return {
                "status": "stale",
                "created": False,
                "effective_date": effective_date,
                "current_version_id": current.version_id,
                "detail": (
                    "Самая свежая общая READY COMPLETE неделя не продвинулась; "
                    "последняя immutable V4 version сохранена."
                ),
                "window": window,
            }
        try:
            with warehouse_sync_lock(self.runtime.runtime_dir, blocking=False):
                latest = self.parameters_for_date(effective_date)
                if latest is None:
                    raise ValueError("Proxy V4 historical initialization is not complete")
                return self._insert_window_version_locked(
                    window=window,
                    effective_date=effective_date,
                    tax_rate=latest.tax_rate,
                    version_kind="automatic_latest_week",
                    created_by=created_by,
                )
        except WarehouseSyncBusyError:
            return {
                "status": "pending_lock_busy",
                "created": False,
                "effective_date": effective_date,
                "current_version_id": current.version_id,
                "detail": (
                    "Новое подтверждённое окно готово, но общий warehouse writer занят; "
                    "последняя immutable V4 version продолжает действовать до следующего refresh."
                ),
                "window": window,
            }

    def preview_tax_version(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        effective_date = current_business_date_iso(self.now_factory())
        current = self.parameters_for_date(effective_date)
        if current is None:
            raise ValueError("Proxy V4 historical initialization is not complete")
        if current.formula_version != PROXY_V4_FORMULA_VERSION:
            raise ValueError(
                "Proxy V4 transit-exclusion historical correction is required before a tax revision"
            )
        tax_rate = _bounded_rate(payload.get("tax_rate"), "tax_rate")
        fingerprint = _digest(
            {
                "contract": PROXY_V4_CONTRACT_VERSION,
                "action": "operator_tax",
                "effective_date": effective_date,
                "current_version_id": current.version_id,
                "tax_rate": _text(tax_rate),
            }
        )
        return {
            "status": "preview_ready",
            "effective_date": effective_date,
            "before_tax_rate_pct": _text(current.tax_rate * Decimal("100")),
            "after_tax_rate_pct": _text(tax_rate * Decimal("100")),
            "changed": tax_rate != current.tax_rate,
            "preview_fingerprint": fingerprint,
        }

    def create_tax_version(
        self,
        payload: Mapping[str, Any],
        *,
        preview_fingerprint: str,
        created_by: str,
    ) -> dict[str, Any]:
        with warehouse_sync_lock(self.runtime.runtime_dir, blocking=False):
            preview = self.preview_tax_version(payload)
            if str(preview_fingerprint or "") != str(preview["preview_fingerprint"]):
                raise ValueError("Proxy V4 tax or current version changed after preview")
            if not preview["changed"]:
                return {**self.get_payload(), "created_version_id": "", "idempotent": True}
            effective_date = str(preview["effective_date"])
            current = self.parameters_for_date(effective_date)
            if current is None:
                raise ValueError("Proxy V4 historical initialization is not complete")
            created = self._insert_parameters_locked(
                effective_date=effective_date,
                tax_rate=_bounded_rate(payload.get("tax_rate"), "tax_rate"),
                automatic_rates={field: getattr(current, field) for field in AUTOMATIC_RATE_FIELDS},
                source_window_from=current.source_window_from,
                source_window_to=current.source_window_to,
                source_window_fingerprint=current.source_window_fingerprint,
                source_week_ranges=current.source_week_ranges,
                source_slot_from=current.source_slot_from,
                source_slot_to=current.source_slot_to,
                buyout_order_count_weight=current.buyout_order_count_weight,
                finance_net_revenue_weight=current.finance_net_revenue_weight,
                version_kind="operator_tax",
                created_by=created_by,
            )
        return {
            **self.get_payload(),
            "created_version_id": created["version_id"],
            "idempotent": False,
        }

    def get_payload(self) -> dict[str, Any]:
        today = date.fromisoformat(current_business_date_iso(self.now_factory()))
        rows = self._history_rows()
        history = [_version_row(row) for row in rows]
        current = next(
            (item for item in history if str(item["effective_date"]) <= today.isoformat()),
            None,
        )
        window = build_latest_confirmed_week_window(runtime=self.runtime, today=today)
        status = "initialization_required"
        message = "V4 ожидает guarded historical initialization с 2026-08-01."
        if current is not None:
            current_parameters = dict(current["parameters"])
            if window["status"] != "ready":
                status = "stale"
                message = (
                    "Используется последняя подтверждённая V4 version; "
                    "среди трёх slot-недель нет общей READY COMPLETE недели."
                )
            elif str(window.get("source_window_fingerprint") or "") == str(
                current_parameters.get("source_window_fingerprint") or ""
            ):
                status = "ready"
                message = (
                    "Действует последняя подтверждённая immutable V4 version; "
                    + str(window.get("coverage_text") or "")
                    + "."
                )
            elif (
                window.get("source_week_ranges")
                == current_parameters.get("source_week_ranges")
                and window.get("source_slot_from")
                == current_parameters.get("source_slot_from")
                and window.get("source_slot_to")
                == current_parameters.get("source_slot_to")
            ):
                status = "historical_repair_required"
                message = "Frozen contributing weeks изменили source payload; требуется guarded reconciliation."
            elif (
                current_parameters.get("source_selection_mode")
                == "latest_confirmed_week"
                and str(window.get("source_window_to") or "")
                <= str(current_parameters.get("source_window_to") or "")
            ):
                status = "stale"
                message = (
                    "Самая свежая общая READY COMPLETE неделя не продвинулась; "
                    "продолжает действовать последняя immutable V4 version. "
                    + str(window.get("coverage_text") or "")
                    + "."
                )
            else:
                status = "pending_new_window"
                message = (
                    "Новая последняя общая READY COMPLETE неделя ожидает ближайший "
                    "штатный Vitrina refresh; "
                    + str(window.get("coverage_text") or "")
                    + "."
                )
        return {
            "contract_name": "sheet_vitrina_v1_proxy_v4_parameters",
            "contract_version": PROXY_V4_CONTRACT_VERSION,
            "selection_contract_version": PROXY_V4_LATEST_WEEK_SELECTION_CONTRACT_VERSION,
            "formula_version": PROXY_V4_FORMULA_VERSION,
            "fixed_boundary": PROXY_V4_FIXED_BOUNDARY,
            "business_timezone": CANONICAL_BUSINESS_TIMEZONE_NAME,
            "status": status,
            "status_message": message,
            "current_business_date": today.isoformat(),
            "current": current,
            "history": history,
            "formula_input_policy": "latest_confirmed_common_week",
            "latest_confirmed_week": window,
            "aligned_window": window,
            "automatic_rate_fields": list(AUTOMATIC_RATE_FIELDS),
            "manual_rate_fields": ["tax_rate"],
            "formula": (
                "expected_buyout_revenue * (1 - included_expense_rate) - "
                "expected_buyout_qty * canonical_WB_WAC - ads_sum"
            ),
        }

    def _history_rows(self) -> list[sqlite3.Row]:
        with _connect(self.runtime.db_path) as conn:
            return conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_proxy_v4_parameter_versions
                WHERE block_key=? ORDER BY effective_date DESC,revision DESC,created_at DESC
                """,
                (PROXY_V4_BLOCK_KEY,),
            ).fetchall()

    def _insert_window_version_locked(
        self,
        *,
        window: Mapping[str, Any],
        effective_date: str,
        tax_rate: Decimal,
        version_kind: str,
        created_by: str,
    ) -> dict[str, Any]:
        with _connect(self.runtime.db_path) as conn:
            existing = conn.execute(
                """SELECT * FROM sheet_vitrina_v1_proxy_v4_parameter_versions
                   WHERE block_key=? AND source_window_fingerprint=?
                   ORDER BY revision DESC LIMIT 1""",
                (PROXY_V4_BLOCK_KEY, str(window["source_window_fingerprint"])),
            ).fetchone()
        if existing is not None:
            return {
                "status": "already_materialized",
                "created": False,
                "version_id": str(existing["version_id"]),
                "window": dict(window),
            }
        result = self._insert_parameters_locked(
            effective_date=effective_date,
            tax_rate=tax_rate,
            automatic_rates={
                field: _decimal(window["automatic_rates"][field])
                for field in AUTOMATIC_RATE_FIELDS
            },
            source_window_from=str(window["source_window_from"]),
            source_window_to=str(window["source_window_to"]),
            source_window_fingerprint=str(window["source_window_fingerprint"]),
            source_week_ranges=tuple(
                (str(item[0]), str(item[1])) for item in window["source_week_ranges"]
            ),
            source_slot_from=str(window["source_slot_from"]),
            source_slot_to=str(window["source_slot_to"]),
            buyout_order_count_weight=_decimal(
                window["aligned_buyout"]["order_count_weight"]
            ),
            finance_net_revenue_weight=_decimal(
                window["aligned_finance"]["net_revenue"]
            ),
            version_kind=version_kind,
            created_by=created_by,
        )
        return {
            "status": "materialized",
            "created": True,
            "version_id": result["version_id"],
            "effective_date": effective_date,
            "window": dict(window),
        }

    def _insert_parameters_locked(
        self,
        *,
        effective_date: str,
        tax_rate: Decimal,
        automatic_rates: Mapping[str, Decimal],
        source_window_from: str,
        source_window_to: str,
        source_window_fingerprint: str,
        source_week_ranges: tuple[tuple[str, str], ...],
        source_slot_from: str,
        source_slot_to: str,
        buyout_order_count_weight: Decimal,
        finance_net_revenue_weight: Decimal,
        version_kind: str,
        created_by: str,
    ) -> dict[str, Any]:
        now = _timestamp(self.now_factory())
        with _connect(self.runtime.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            revision = int(
                conn.execute(
                    "SELECT COALESCE(MAX(revision),0)+1 FROM sheet_vitrina_v1_proxy_v4_parameter_versions WHERE block_key=?",
                    (PROXY_V4_BLOCK_KEY,),
                ).fetchone()[0]
            )
            version_id = f"proxy_v4_v{revision}_{effective_date.replace('-', '')}"
            parameters = _parameters_from_values(
                effective_date=effective_date,
                tax_rate=tax_rate,
                automatic_rates=automatic_rates,
                source_window_from=source_window_from,
                source_window_to=source_window_to,
                source_window_fingerprint=source_window_fingerprint,
                source_week_ranges=source_week_ranges,
                source_slot_from=source_slot_from,
                source_slot_to=source_slot_to,
                buyout_order_count_weight=buyout_order_count_weight,
                finance_net_revenue_weight=finance_net_revenue_weight,
                version_id=version_id,
                revision=revision,
                version_kind=version_kind,
                created_at=now,
                created_by=created_by,
            )
            fingerprint = _parameter_fingerprint(parameters)
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_proxy_v4_parameter_versions(
                    version_id,block_key,revision,effective_date,source_window_from,
                    source_window_to,source_window_fingerprint,parameters_json,
                    fingerprint,version_kind,created_by,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    version_id,
                    PROXY_V4_BLOCK_KEY,
                    revision,
                    effective_date,
                    source_window_from,
                    source_window_to,
                    source_window_fingerprint,
                    _json(parameters.public()),
                    fingerprint,
                    version_kind,
                    created_by,
                    now,
                ),
            )
            conn.commit()
        return {"version_id": version_id, "revision": revision, "fingerprint": fingerprint}


def plan_initial_historical_versions(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    tax_rate_resolver: Callable[[str], Decimal],
    effective_dates: tuple[str, ...] = PROXY_V4_INITIAL_EFFECTIVE_DATES,
) -> list[ProxyV4Parameters]:
    """Reconstruct only the owner-approved initial as-of V4 versions."""

    versions: list[ProxyV4Parameters] = []
    current_tax: Decimal | None = None
    for revision, effective_date in enumerate(effective_dates, start=1):
        normalized_date = date.fromisoformat(effective_date).isoformat()
        window = build_confirmed_aligned_window(
            runtime=runtime,
            today=date.fromisoformat(normalized_date),
            finance_available_by=normalized_date,
        )
        if window["status"] != "ready" or int(window["ready_week_count"]) != 3:
            raise ValueError(
                f"Proxy V4 historical window {normalized_date} requires exact 3-of-3 as-of proof: {window['status_message']}"
            )
        if current_tax is None:
            current_tax = _bounded_rate(tax_rate_resolver(normalized_date), "tax_rate")
        versions.append(
            _parameters_from_values(
                effective_date=normalized_date,
                tax_rate=current_tax,
                automatic_rates={
                    field: _decimal(window["automatic_rates"][field])
                    for field in AUTOMATIC_RATE_FIELDS
                },
                source_window_from=str(window["source_window_from"]),
                source_window_to=str(window["source_window_to"]),
                source_window_fingerprint=str(window["source_window_fingerprint"]),
                source_week_ranges=tuple(
                    (str(item[0]), str(item[1]))
                    for item in window["source_week_ranges"]
                ),
                source_slot_from=str(window["source_slot_from"]),
                source_slot_to=str(window["source_slot_to"]),
                buyout_order_count_weight=_decimal(
                    window["aligned_buyout"]["order_count_weight"]
                ),
                finance_net_revenue_weight=_decimal(
                    window["aligned_finance"]["net_revenue"]
                ),
                version_id=f"proxy_v4_v{revision}_{normalized_date.replace('-', '')}",
                revision=revision,
                version_kind="historical_initialization",
                created_at="",
                created_by="production_mutation",
            )
        )
    return versions


def ensure_proxy_v4_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_proxy_v4_parameter_versions(
            version_id TEXT PRIMARY KEY,
            block_key TEXT NOT NULL,
            revision INTEGER NOT NULL,
            effective_date TEXT NOT NULL,
            source_window_from TEXT NOT NULL,
            source_window_to TEXT NOT NULL,
            source_window_fingerprint TEXT NOT NULL,
            parameters_json TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            version_kind TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(block_key,revision)
        );
        CREATE INDEX IF NOT EXISTS proxy_v4_parameters_by_effective_date
        ON sheet_vitrina_v1_proxy_v4_parameter_versions(
            block_key,effective_date DESC,revision DESC
        );
        CREATE INDEX IF NOT EXISTS proxy_v4_parameters_by_source_window
        ON sheet_vitrina_v1_proxy_v4_parameter_versions(
            block_key,source_window_to DESC,source_window_fingerprint
        );
        """
    )


def _build_finance_window(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    week_keys: list[tuple[str, str]],
    available_by: str | None,
) -> dict[str, Any]:
    with _connect(runtime.db_path) as conn:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        required_tables = {"wb_finance_weekly_aggregates", "wb_finance_weekly_sync"}
        if not required_tables.issubset(tables):
            return _unavailable_finance_window(week_keys, "Finance weekly tables are missing.")
        aggregate_rows = conn.execute(
            """
            SELECT seller_id,week_start,week_end,classifier_version,metrics_json,calculated_at
            FROM wb_finance_weekly_aggregates
            WHERE (week_start=? AND week_end=?)
               OR (week_start=? AND week_end=?)
               OR (week_start=? AND week_end=?)
            ORDER BY week_start,seller_id
            """,
            tuple(value for key in week_keys for value in key),
        ).fetchall()
        sync_rows = conn.execute(
            """
            SELECT seller_id,week_start,week_end,status,first_loaded_at,content_hash
            FROM wb_finance_weekly_sync
            WHERE (week_start=? AND week_end=?)
               OR (week_start=? AND week_end=?)
               OR (week_start=? AND week_end=?)
            ORDER BY week_start,seller_id
            """,
            tuple(value for key in week_keys for value in key),
        ).fetchall()
        requested_sellers = {str(row["seller_id"]) for row in aggregate_rows}
        expected_sellers = sorted(
            requested_sellers
            or {
                str(row[0])
                for row in conn.execute(
                    "SELECT DISTINCT seller_id FROM wb_finance_weekly_aggregates ORDER BY seller_id"
                ).fetchall()
            }
        )
    aggregates_by_week: dict[tuple[str, str], list[sqlite3.Row]] = {
        key: [] for key in week_keys
    }
    sync_by_week: dict[tuple[str, str], list[sqlite3.Row]] = {key: [] for key in week_keys}
    for row in aggregate_rows:
        aggregates_by_week[(str(row["week_start"]), str(row["week_end"]))].append(row)
    for row in sync_rows:
        sync_by_week[(str(row["week_start"]), str(row["week_end"]))].append(row)

    week_results: list[dict[str, Any]] = []
    for week_start, week_end in week_keys:
        aggregates = aggregates_by_week[(week_start, week_end)]
        sync = sync_by_week[(week_start, week_end)]
        aggregate_sellers = sorted({str(row["seller_id"]) for row in aggregates})
        sync_sellers = sorted({str(row["seller_id"]) for row in sync})
        reasons: list[str] = []
        if aggregate_sellers != expected_sellers or sync_sellers != expected_sellers:
            reasons.append("seller coverage is incomplete")
        if any(str(row["classifier_version"] or "") != WB_FINANCE_CLASSIFIER_VERSION for row in aggregates):
            reasons.append("classifier version is stale")
        if any(str(row["status"] or "") != "completed" for row in sync):
            reasons.append("weekly sync is not completed")
        if available_by:
            for row in sync:
                first_loaded_at = str(row["first_loaded_at"] or "")
                try:
                    first_loaded_date = business_date_from_timestamp(first_loaded_at)
                except ValueError:
                    reasons.append("first-loaded provenance is missing")
                    continue
                if first_loaded_date > available_by:
                    reasons.append("Finance week was not available by the historical effective date")
        week_sources: list[dict[str, Any]] = []
        for row in aggregates:
            metrics = json.loads(str(row["metrics_json"] or "{}"))
            missing = [field for field in _FINANCE_REQUIRED_DIRECT_FIELDS if metrics.get(field) in (None, "")]
            if metrics.get("agent_remuneration") in (None, "") and metrics.get("commission") in (None, ""):
                missing.append("agent_remuneration|commission")
            if missing:
                reasons.append("required Finance metrics are missing")
            else:
                week_sources.append(metrics)
        status = (
            "ready"
            if expected_sellers
            and not reasons
            and len(week_sources) == len(expected_sellers)
            else "unavailable"
        )
        net_revenue: Decimal | None = None
        amounts: dict[str, Decimal] = {}
        excluded_amounts: dict[str, Decimal] = {}
        if status == "ready":
            net_revenue = sum(
                (_metric(source, "net_revenue") for source in week_sources),
                Decimal("0"),
            )
            if net_revenue <= 0:
                reasons.append("SUM(net_revenue) must be positive")
                status = "unavailable"
                net_revenue = None
            else:
                amounts = _finance_amounts(week_sources)
                excluded_amounts = _finance_excluded_amounts(week_sources)
        week_results.append(
            {
                "week_start": week_start,
                "week_end": week_end,
                "status": status,
                "seller_count": len(aggregate_sellers),
                "expected_seller_count": len(expected_sellers),
                "reasons": sorted(set(reasons)),
                "first_loaded_at": min(
                    (str(row["first_loaded_at"] or "") for row in sync),
                    default="",
                ),
                "net_revenue": None if net_revenue is None else _text(net_revenue),
                "amounts": {key: _text(value) for key, value in amounts.items()},
                "excluded_amounts": {
                    key: _text(value) for key, value in excluded_amounts.items()
                },
                "rates": (
                    {
                        key: _text(value / net_revenue)
                        for key, value in amounts.items()
                    }
                    if net_revenue is not None
                    else {}
                ),
                "source_metrics_digest": _digest(week_sources),
                "source_sync_digest": _digest(
                    [
                        {
                            "seller_id": str(row["seller_id"]),
                            "status": str(row["status"] or ""),
                            "first_loaded_at": str(row["first_loaded_at"] or ""),
                            "content_hash": str(row["content_hash"] or ""),
                        }
                        for row in sync
                    ]
                ),
            }
        )
    week_by_range = {
        (str(item["week_start"]), str(item["week_end"])): item
        for item in week_results
    }
    ready_week_ranges = [
        key for key in week_keys if week_by_range[key]["status"] == "ready"
    ]
    combined = _combine_finance_week_results(week_by_range, ready_week_ranges)
    status = (
        "ready"
        if len(ready_week_ranges) == len(week_keys) and combined["status"] == "ready"
        else "partial"
        if ready_week_ranges and combined["status"] == "ready"
        else "unavailable"
    )
    return {
        "status": status,
        "status_message": (
            f"Finance: расчёт по {len(ready_week_ranges)} из {len(week_keys)} READY COMPLETE недель прямым SUM/SUM."
            if ready_week_ranges
            else "Finance: среди трёх закрытых slot-недель нет READY COMPLETE недели."
        ),
        "week_keys": list(week_keys),
        "weeks": week_results,
        "ready_week_count": len(ready_week_ranges),
        "required_week_count": len(week_keys),
        "contributing_week_ranges": [list(item) for item in ready_week_ranges],
        "expected_seller_count": len(expected_sellers),
        "classifier_version": WB_FINANCE_CLASSIFIER_VERSION,
        "aggregation_rule": "SUM(signed amount) / SUM(net_revenue)",
        "net_revenue": combined["net_revenue"],
        "amounts": combined["amounts"],
        "rates": combined["rates"],
        "composition": _finance_composition(),
        "fingerprint_payload": combined["fingerprint_payload"],
    }


def _finance_amounts(sources: list[Mapping[str, Any]]) -> dict[str, Decimal]:
    return {
        "agent_remuneration_rate": sum(
            (_metric_alias(source, "agent_remuneration", "commission") for source in sources),
            Decimal("0"),
        ),
        "acquiring_rate": sum((_metric(source, "acquiring") for source in sources), Decimal("0")),
        "wb_logistics_rate": sum((_metric(source, "logistics") for source in sources), Decimal("0")),
        "wb_storage_rate": sum((_metric(source, "storage") for source in sources), Decimal("0")),
        "penalties_adjustments_rate": sum(
            (_metric(source, "penalties") + _metric(source, "corrections") for source in sources),
            Decimal("0"),
        ),
        "other_expense_rate": sum(
            (
                _metric(source, "subscriptions")
                + _metric(source, "paid_services")
                + _metric(source, "review_points")
                + _metric(source, "other_deductions")
                + _metric(source, "acceptance")
                - _metric(source, "capitalized_acceptance")
                for source in sources
            ),
            Decimal("0"),
        ),
    }


def _finance_excluded_amounts(
    sources: list[Mapping[str, Any]],
) -> dict[str, Decimal]:
    return {
        "transit_logistics": sum(
            (_metric(source, "transit_logistics") for source in sources),
            Decimal("0"),
        ),
        "capitalized_transit_logistics": sum(
            (_metric(source, "capitalized_transit_logistics") for source in sources),
            Decimal("0"),
        ),
    }


def _finance_composition() -> dict[str, Any]:
    return {
        "agent_remuneration": "agent_remuneration|commission; acquiring excluded",
        "penalties_adjustments": "penalties + corrections",
        "other_expense": (
            "subscriptions + paid_services + review_points + other_deductions + "
            "acceptance - capitalized_acceptance"
        ),
        "excluded": [
            "marketing",
            "positive_adjustments",
            "wb_remuneration_adjustment",
            "capitalized_acceptance",
            "transit_logistics",
            "capitalized_transit_logistics",
        ],
    }


def _combine_finance_week_results(
    week_by_range: Mapping[tuple[str, str], Mapping[str, Any]],
    included_ranges: list[tuple[str, str]],
) -> dict[str, Any]:
    included = [week_by_range[key] for key in included_ranges]
    if not included or any(str(item.get("status") or "") != "ready" for item in included):
        return {
            "status": "unavailable",
            "net_revenue": None,
            "amounts": {},
            "rates": {},
            "fingerprint_payload": {"status": "unavailable", "week_ranges": included_ranges},
        }
    net_revenue = sum((_decimal(item.get("net_revenue")) for item in included), Decimal("0"))
    if net_revenue <= 0:
        return {
            "status": "unavailable",
            "net_revenue": None,
            "amounts": {},
            "rates": {},
            "fingerprint_payload": {"status": "unavailable", "week_ranges": included_ranges},
        }
    amounts = {
        field: sum(
            (_decimal(dict(item.get("amounts") or {}).get(field)) for item in included),
            Decimal("0"),
        )
        for field in AUTOMATIC_RATE_FIELDS
        if field != "buyout_rate"
    }
    excluded_amounts = {
        field: sum(
            (
                _decimal(dict(item.get("excluded_amounts") or {}).get(field))
                for item in included
            ),
            Decimal("0"),
        )
        for field in ("transit_logistics", "capitalized_transit_logistics")
    }
    rates = {field: _text(value / net_revenue) for field, value in amounts.items()}
    fingerprint_payload = {
        "classifier_version": WB_FINANCE_CLASSIFIER_VERSION,
        "week_ranges": included_ranges,
        "net_revenue": _text(net_revenue),
        "amounts": {field: _text(value) for field, value in amounts.items()},
        "excluded_amounts": {
            field: _text(value) for field, value in excluded_amounts.items()
        },
        "rates": rates,
        "source_weeks": [
            {
                "week_start": str(item.get("week_start") or ""),
                "week_end": str(item.get("week_end") or ""),
                "source_metrics_digest": str(item.get("source_metrics_digest") or ""),
                "source_sync_digest": str(item.get("source_sync_digest") or ""),
            }
            for item in included
        ],
        "composition": _finance_composition(),
    }
    return {
        "status": "ready",
        "net_revenue": _text(net_revenue),
        "amounts": {field: _text(value) for field, value in amounts.items()},
        "excluded_amounts": {
            field: _text(value) for field, value in excluded_amounts.items()
        },
        "rates": rates,
        "fingerprint_payload": fingerprint_payload,
    }


def _combine_buyout_week_results(
    week_by_range: Mapping[tuple[str, str], Mapping[str, Any]],
    included_ranges: list[tuple[str, str]],
) -> dict[str, Any]:
    included = [week_by_range[key] for key in included_ranges]
    aggregation = aggregate_buyout_percent(
        (
            _decimal(item.get("weighted_average_pct")) / Decimal("100"),
            _decimal(item.get("order_count_weight")),
        )
        for item in included
        if str(item.get("status") or "") == "ready"
    )
    return {
        "value": None if aggregation.value is None else _text(aggregation.value),
        "order_count_weight": _text(aggregation.order_count_weight),
        "included_sku_day_count": sum(
            int(item.get("included_sku_day_count") or 0) for item in included
        ),
    }


def _unavailable_finance_window(
    week_keys: list[tuple[str, str]],
    message: str,
) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "status_message": message,
        "week_keys": list(week_keys),
        "weeks": [],
        "expected_seller_count": 0,
        "classifier_version": WB_FINANCE_CLASSIFIER_VERSION,
        "aggregation_rule": "SUM(signed amount) / SUM(net_revenue)",
        "net_revenue": None,
        "rates": {},
        "composition": {},
        "fingerprint_payload": {"status": "unavailable", "message": message},
    }


def _buyout_source_payload_digest(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    date_from: str,
    date_to: str,
) -> str:
    with _connect(runtime.db_path) as conn:
        rows = conn.execute(
            """
            SELECT snapshot_date,payload_json
            FROM temporal_source_snapshots
            WHERE source_key=? AND snapshot_date>=? AND snapshot_date<=?
            ORDER BY snapshot_date
            """,
            ("sales_funnel_history", date_from, date_to),
        ).fetchall()
    return _digest(
        [
            [str(row["snapshot_date"]), json.loads(str(row["payload_json"]))]
            for row in rows
        ]
    )


def _parameters_from_values(
    *,
    effective_date: str,
    tax_rate: Decimal,
    automatic_rates: Mapping[str, Decimal],
    source_window_from: str,
    source_window_to: str,
    source_window_fingerprint: str,
    source_week_ranges: tuple[tuple[str, str], ...],
    source_slot_from: str,
    source_slot_to: str,
    buyout_order_count_weight: Decimal,
    finance_net_revenue_weight: Decimal,
    version_id: str,
    revision: int,
    version_kind: str,
    created_at: str,
    created_by: str,
    formula_version: str = PROXY_V4_FORMULA_VERSION,
) -> ProxyV4Parameters:
    result = ProxyV4Parameters(
        effective_date=date.fromisoformat(effective_date).isoformat(),
        tax_rate=_bounded_rate(tax_rate, "tax_rate"),
        source_window_from=date.fromisoformat(source_window_from).isoformat(),
        source_window_to=date.fromisoformat(source_window_to).isoformat(),
        source_window_fingerprint=str(source_window_fingerprint),
        source_week_ranges=tuple(
            (
                date.fromisoformat(str(week_start)).isoformat(),
                date.fromisoformat(str(week_end)).isoformat(),
            )
            for week_start, week_end in source_week_ranges
        ),
        source_slot_from=date.fromisoformat(source_slot_from).isoformat(),
        source_slot_to=date.fromisoformat(source_slot_to).isoformat(),
        buyout_order_count_weight=_decimal(buyout_order_count_weight),
        finance_net_revenue_weight=_decimal(finance_net_revenue_weight),
        formula_version=str(formula_version),
        version_id=version_id,
        revision=revision,
        version_kind=version_kind,
        created_at=created_at,
        created_by=created_by,
        **{field: _decimal(automatic_rates[field]) for field in AUTOMATIC_RATE_FIELDS},
    )
    if result.buyout_rate < 0 or result.buyout_rate > 1:
        raise ValueError("buyout_rate must be between 0 and 1")
    if result.included_expense_rate >= Decimal("1"):
        raise ValueError("Proxy V4 total included expenses must be below 100%")
    if result.formula_version not in {
        PROXY_V4_LEGACY_FORMULA_VERSION,
        PROXY_V4_FORMULA_VERSION,
    }:
        raise ValueError(f"unsupported Proxy V4 formula version: {result.formula_version}")
    return result


def _parameters_from_row(row: sqlite3.Row) -> ProxyV4Parameters:
    raw = json.loads(str(row["parameters_json"] or "{}"))
    raw_ranges = raw.get("source_week_ranges")
    source_week_ranges = (
        tuple((str(item[0]), str(item[1])) for item in raw_ranges)
        if isinstance(raw_ranges, list) and raw_ranges
        else ((str(row["source_window_from"]), str(row["source_window_to"])),)
    )
    return _parameters_from_values(
        effective_date=str(row["effective_date"]),
        tax_rate=_decimal(raw.get("tax_rate")),
        automatic_rates={field: _decimal(raw.get(field)) for field in AUTOMATIC_RATE_FIELDS},
        source_window_from=str(row["source_window_from"]),
        source_window_to=str(row["source_window_to"]),
        source_window_fingerprint=str(row["source_window_fingerprint"]),
        source_week_ranges=source_week_ranges,
        source_slot_from=str(raw.get("source_slot_from") or row["source_window_from"]),
        source_slot_to=str(raw.get("source_slot_to") or row["source_window_to"]),
        buyout_order_count_weight=_decimal(raw.get("buyout_order_count_weight")),
        finance_net_revenue_weight=_decimal(raw.get("finance_net_revenue_weight")),
        version_id=str(row["version_id"]),
        revision=int(row["revision"]),
        version_kind=str(row["version_kind"]),
        created_at=str(row["created_at"]),
        created_by=str(row["created_by"]),
        formula_version=str(
            raw.get("formula_version") or PROXY_V4_LEGACY_FORMULA_VERSION
        ),
    )


def _version_row(row: sqlite3.Row) -> dict[str, Any]:
    parameters = _parameters_from_row(row)
    return {
        "version_id": parameters.version_id,
        "revision": parameters.revision,
        "effective_date": parameters.effective_date,
        "source_window_from": parameters.source_window_from,
        "source_window_to": parameters.source_window_to,
        "source_window_fingerprint": parameters.source_window_fingerprint,
        "version_kind": parameters.version_kind,
        "created_by": parameters.created_by,
        "created_at": parameters.created_at,
        "fingerprint": str(row["fingerprint"]),
        "parameters": {**parameters.public(), "fingerprint": str(row["fingerprint"])},
    }


def _parameter_fingerprint(parameters: ProxyV4Parameters) -> str:
    return _digest(
        {
            "contract": PROXY_V4_CONTRACT_VERSION,
            "formula_version": parameters.formula_version,
            "effective_date": parameters.effective_date,
            "source_window_fingerprint": parameters.source_window_fingerprint,
            "rates": {
                field: _text(getattr(parameters, field))
                for field in ("buyout_rate", *EXPENSE_RATE_FIELDS)
            },
        }
    )


def _blank_proxy_4() -> dict[str, Decimal | None]:
    return {
        "expected_buyout_revenue": None,
        "expected_buyout_qty": None,
        "included_expense_rate": None,
        "proxy_profit_4": None,
        "proxy_margin_4": None,
    }


def _metric(source: Mapping[str, Any], field: str) -> Decimal:
    if field not in source or source.get(field) in (None, ""):
        raise ValueError(f"required Finance metric is missing: {field}")
    return _decimal(source.get(field))


def _metric_alias(source: Mapping[str, Any], primary: str, alias: str) -> Decimal:
    if source.get(primary) not in (None, ""):
        return _decimal(source.get(primary))
    return _metric(source, alias)


def _bounded_rate(value: Any, field: str) -> Decimal:
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
    return None if value in (None, "") else _decimal(value)


def _text(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f") if normalized != 0 else "0"


def _digest(value: Any) -> str:
    body = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Proxy V4 timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _connect(path: Any) -> sqlite3.Connection:
    conn = connect_sqlite(path)
    conn.row_factory = sqlite3.Row
    return conn
