"""Weekly Wildberries Finance report storage, aggregation and synchronization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Callable, Iterable, Mapping
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

from packages.application.ads_snapshot_payload import resolve_ads_snapshot_payload
from packages.application.finance_raw_storage import (
    FinanceOutboxConsumer,
    FinanceRawIngestor,
    ensure_raw_schema,
    storage_health,
)
from packages.application.storage_registry import GenerationManifest, StoreRegistry
from packages.application.canonical_wb_cost_resolver import (
    CANONICAL_COST_FORMULA_VERSION,
    CANONICAL_COST_POLICY_DATE,
    CHANNEL_LOCATION_COST_FORMULA_VERSION,
    FBS_OBSERVATIONS_TABLE,
    FUNCTIONAL_CUTOVER_ID,
    FUNCTIONAL_DAILY_TABLE,
    CanonicalChannelCostSnapshot,
    canonical_cost_source_date,
    classify_finance_channel,
    pooled_fbs_state_as_of,
    resolve_channel_location_cost,
)
from packages.application.warehouse_archival_estimate import (
    archival_estimate_for_nm_id,
)
from packages.business_time import business_date_from_timestamp


FINANCE_URL = "https://finance-api.wildberries.ru/api/finance/v1/sales-reports/detailed"
CLASSIFIER_VERSION = "wb_finance_weekly_classifier_v3_signed_review_points"
SKU_AGGREGATE_FORMULA_VERSION = "wb_finance_weekly_sku_aggregate_v5"
CALCULATION_REFERENCE_CONTRACT_VERSION = "wb_finance_calculation_reference_v3"
MOSCOW = ZoneInfo("Europe/Moscow")
ZERO = Decimal("0")
MONEY_QUANT = Decimal("0.0001")
FIRST_INCLUDED_DATE = date(2026, 1, 1)
OUR_WB_COST_OPENING_DATE = CANONICAL_COST_POLICY_DATE.isoformat()
OUR_WB_COST_CUTOVER_DATE = CANONICAL_COST_POLICY_DATE
OUR_WB_COST_CUTOVER_WEEK_START = OUR_WB_COST_CUTOVER_DATE - timedelta(
    days=OUR_WB_COST_CUTOVER_DATE.weekday()
)
FINANCE_SHADOW_INGEST_STATE_FILENAME = ".finance-storage-shadow-ingest.json"
FINANCE_SHADOW_INGEST_STATE_CONTRACT = (
    "wb_core_finance_shadow_ingest_state_v1"
)
RETRO_COST_PERIOD_START = date(2026, 5, 1)
RETRO_COST_PERIOD_END = date(2026, 6, 30)
RETRO_COST_REFERENCE_DATE = date(2026, 7, 1)
RETRO_COST_FIRST_WEEK_START = date(2026, 4, 27)
RETRO_COST_FORMULA_VERSION = CANONICAL_COST_FORMULA_VERSION
FBS_FINANCE_HISTORICAL_CUTOFF = date(2026, 8, 23)
FBS_FINANCE_FORWARD_INGRESS_DATE = FBS_FINANCE_HISTORICAL_CUTOFF + timedelta(
    days=1
)
PROFIT_METHOD_VERSION = "wb_finance_profit_covered_revenue_v4_signed_deductions"
COST_METHOD_VERSION = CHANNEL_LOCATION_COST_FORMULA_VERSION


# The calculation-parameters reference is a read projection of these canonical
# weekly aggregate fields.  Keeping the row audit beside the aggregate contract
# prevents Settings from inventing a second expense classifier or silently
# changing field composition.
CALCULATION_REFERENCE_ROWS: tuple[dict[str, Any], ...] = (
    {
        "key": "agent_remuneration",
        "label": "Агентское вознаграждение WB",
        "group": "Ориентиры для процентных параметров Proxy 3",
        "source_fields": ("agent_remuneration", "commission"),
        "source_mode": "first_available",
        "proxy_parameter_key": "wb_agent_and_other_rate",
        "sign_rule": "Продажа +, возврат −; эквайринг не входит",
        "proxy_treatment": "Ориентир для wb_agent_and_other_rate; значение не сохраняется автоматически",
        "note": "Канонический agent_remuneration (commission — совместимый alias); эквайринг показан отдельно и не вычитается повторно.",
    },
    {
        "key": "acquiring",
        "label": "Эквайринг",
        "group": "Ориентиры для процентных параметров Proxy 3",
        "source_fields": ("acquiring",),
        "source_mode": "direct",
        "proxy_parameter_key": "acquiring_rate",
        "sign_rule": "Продажа +, возврат −",
        "proxy_treatment": "Ориентир для acquiring_rate; учитывается отдельно от агентского вознаграждения",
        "note": "Отдельный канонический acquiring; agent_remuneration + acquiring = combined_commission_control.",
    },
    {
        "key": "logistics",
        "label": "Логистика WB до покупателя",
        "group": "Ориентиры для процентных параметров Proxy 3",
        "source_fields": ("logistics",),
        "source_mode": "direct",
        "proxy_parameter_key": "wb_logistics_rate",
        "sign_rule": "Канонический signed expense; сторно уменьшает расход",
        "proxy_treatment": "Ориентир для wb_logistics_rate",
        "note": "Периодная логистика до покупателя; транзит FF → WB раскрыт отдельно.",
    },
    {
        "key": "storage",
        "label": "Хранение WB",
        "group": "Ориентиры для процентных параметров Proxy 3",
        "source_fields": ("storage",),
        "source_mode": "direct",
        "proxy_parameter_key": "wb_storage_rate",
        "sign_rule": "Канонический signed expense; сторно уменьшает расход",
        "proxy_treatment": "Ориентир для wb_storage_rate",
        "note": "Периодное хранение WB без складской себестоимости и транзита.",
    },
    {
        "key": "penalties",
        "label": "Штрафы",
        "group": "Компоненты объединённых параметров Proxy 3",
        "source_fields": ("penalties",),
        "source_mode": "direct",
        "proxy_parameter_key": "penalties_adjustments_rate",
        "sign_rule": "Канонический signed expense; сторно уменьшает расход",
        "proxy_treatment": "Компонент penalties_adjustments_rate",
        "note": "Только штрафы; корректировки расходов показаны следующей отдельной строкой.",
    },
    {
        "key": "corrections",
        "label": "Корректировки (расходы)",
        "group": "Компоненты объединённых параметров Proxy 3",
        "source_fields": ("corrections",),
        "source_mode": "direct",
        "proxy_parameter_key": "penalties_adjustments_rate",
        "sign_rule": "Standalone отрицательная additionalPayment нормализуется как положительный расход",
        "proxy_treatment": "Компонент penalties_adjustments_rate",
        "note": "Расходные корректировки не смешиваются со штрафами и не теряются в total_wb_expenses.",
    },
    {
        "key": "subscriptions",
        "label": "Подписки",
        "group": "Компоненты параметра «Другие расходы»",
        "source_fields": ("subscriptions",),
        "source_mode": "direct",
        "proxy_parameter_key": "other_expense_rate",
        "sign_rule": "Канонический signed deduction; сторно уменьшает расход",
        "proxy_treatment": "Компонент other_expense_rate",
        "note": "Подписки/Jamm из канонического классификатора Finance.",
    },
    {
        "key": "paid_services",
        "label": "Платные сервисы",
        "group": "Компоненты параметра «Другие расходы»",
        "source_fields": ("paid_services",),
        "source_mode": "direct",
        "proxy_parameter_key": "other_expense_rate",
        "sign_rule": "Канонический signed deduction; сторно уменьшает расход",
        "proxy_treatment": "Компонент other_expense_rate",
        "note": "Платные сервисы WB, классифицированные отдельно от подписок.",
    },
    {
        "key": "review_points",
        "label": "Баллы за отзывы",
        "group": "Компоненты параметра «Другие расходы»",
        "source_fields": ("review_points",),
        "source_mode": "direct",
        "proxy_parameter_key": "other_expense_rate",
        "sign_rule": "Канонический signed deduction; сторно уменьшает расход",
        "proxy_treatment": "Компонент other_expense_rate",
        "note": "Только операции «Баллы за отзывы»/«Списание за отзыв».",
    },
    {
        "key": "other_deductions",
        "label": "Прочие удержания",
        "group": "Компоненты параметра «Другие расходы»",
        "source_fields": ("other_deductions",),
        "source_mode": "direct",
        "proxy_parameter_key": "other_expense_rate",
        "sign_rule": "Канонический signed deduction; сторно уменьшает расход",
        "proxy_treatment": "Компонент other_expense_rate",
        "note": "Только остаток канонического классификатора, не балансирующая разница.",
    },
    {
        "key": "marketing",
        "label": "Маркетинг и продвижение",
        "group": "Справочные строки с отдельным учётом",
        "source_fields": ("marketing",),
        "source_mode": "direct",
        "sign_rule": "Канонический signed deduction; сторно уменьшает расход",
        "proxy_treatment": "Справочно; Proxy 3 вычитает canonical ads_sum отдельно, не добавляет процент повторно",
        "note": "Finance marketing не копируется в versioned rate и не смешивается с другими удержаниями.",
    },
    {
        "key": "acceptance",
        "label": "Платная приёмка — начислено",
        "group": "Справочные строки с отдельным учётом",
        "source_fields": ("acceptance",),
        "source_mode": "direct",
        "sign_rule": "Канонический signed expense; отрицательная сумма не капитализируется",
        "proxy_treatment": "Справочно; доказанная часть капитализируется, недоказанный остаток остаётся расходом периода",
        "note": "Полное начисление до canonical Finance↔supply cost-layer addback.",
    },
    {
        "key": "capitalized_acceptance",
        "label": "Платная приёмка — капитализировано",
        "group": "Справочные строки с отдельным учётом",
        "source_fields": ("capitalized_acceptance",),
        "source_mode": "direct",
        "sign_rule": "Положительная доказанная часть; вычитается из расходов периода",
        "proxy_treatment": "Не входит в процент повторно: уже находится в canonical WB cost",
        "note": "Только exact matched-and-capped cost-layer lineage; разница с начислением не исчезает.",
    },
    {
        "key": "transit_logistics",
        "label": "Транзитная логистика — начислено",
        "group": "Справочные строки с отдельным учётом",
        "source_fields": ("transit_logistics",),
        "source_mode": "direct",
        "sign_rule": "Канонический signed deduction; отрицательная сумма не капитализируется",
        "proxy_treatment": "Справочно; доказанная часть капитализируется, недоказанный остаток остаётся расходом периода",
        "note": "Полное начисление транзита до canonical Finance↔supply cost-layer addback.",
    },
    {
        "key": "capitalized_transit_logistics",
        "label": "Транзитная логистика — капитализировано",
        "group": "Справочные строки с отдельным учётом",
        "source_fields": ("capitalized_transit_logistics",),
        "source_mode": "direct",
        "sign_rule": "Положительная доказанная часть; вычитается из расходов периода",
        "proxy_treatment": "Не входит в процент повторно: уже находится в canonical WB cost",
        "note": "Только exact matched-and-capped cost-layer lineage; разница с начислением не исчезает.",
    },
    {
        "key": "positive_adjustments",
        "label": "Корректировки и дополнительные выплаты (+)",
        "group": "Контроль корректировок",
        "source_fields": ("positive_adjustments",),
        "source_mode": "direct",
        "sign_rule": "Standalone положительная additionalPayment увеличивает финансовый результат",
        "proxy_treatment": "Не является расходной ставкой Proxy 3",
        "note": "Положительная корректировка раскрывается отдельно и не вычитается как расход.",
    },
    {
        "key": "wb_remuneration_adjustment",
        "label": "Корректировка вознаграждения WB — контроль",
        "group": "Контроль корректировок",
        "source_fields": ("wb_remuneration_adjustment",),
        "source_mode": "direct",
        "sign_rule": "Официальный signed control field",
        "proxy_treatment": "Только контроль; не складывается повторно с agent_remuneration/positive_adjustments/corrections",
        "note": "Sale/return уже отражены через forPay; standalone строки попадают ровно в одну экономическую категорию.",
    },
)


class _StreamingJsonArrayDigest:
    """Digest a deterministic JSON array without retaining every element."""

    def __init__(self) -> None:
        self._hash = hashlib.sha256()
        self._hash.update(b"[")
        self._count = 0
        self._finished = False

    def add(self, value: Any) -> None:
        if self._finished:
            raise RuntimeError("streaming JSON digest is already finalized")
        if self._count:
            self._hash.update(b",")
        self._hash.update(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )
        self._count += 1

    @property
    def count(self) -> int:
        return self._count

    def finish(self) -> str:
        if not self._finished:
            self._hash.update(b"]")
            self._finished = True
        return "sha256:" + self._hash.hexdigest()


class _StreamingCostDependencyDigest:
    """Preserve the canonical cost-state JSON hash without a per-row list."""

    def __init__(self) -> None:
        self._hash = hashlib.sha256()
        self._hash.update(b'{"dependencies":[')
        self._count = 0

    def add(self, value: Mapping[str, Any]) -> None:
        if self._count:
            self._hash.update(b",")
        self._hash.update(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        self._count += 1

    def finish(self) -> str:
        self._hash.update(b'],"formula_version":')
        self._hash.update(
            json.dumps(COST_METHOD_VERSION, ensure_ascii=False).encode("utf-8")
        )
        self._hash.update(b"}")
        return self._hash.hexdigest()


def _decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return ZERO
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return ZERO


def _money_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.quantize(MONEY_QUANT), "f")


def _finance_nm_id_sort_key(value: Any) -> tuple[bool, int | str]:
    text = str(value)
    return (not text.isdigit(), int(text) if text.isdigit() else text)


def _ratio(numerator: Decimal | None, denominator: Decimal) -> Decimal | None:
    if numerator is None or denominator <= ZERO:
        return None
    return numerator / denominator * Decimal("100")


def _operation_date(row: Mapping[str, Any], fallback: date) -> tuple[date, str]:
    for field in ("rrDate", "saleDt", "orderDt"):
        raw_date = str(row.get(field) or "")[:10]
        try:
            if raw_date:
                return date.fromisoformat(raw_date), field
        except ValueError:
            continue
    return fallback, "week_start_fallback"


def _finance_sale_identity(row: Mapping[str, Any]) -> str:
    """Return one non-PII stable order identity for coverage counts."""

    for field in ("srid", "rid", "orderUid", "order_uid"):
        token = str(row.get(field) or "").strip()
        if token:
            return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()
    material = {
        "report_id": str(row.get("reportId") or ""),
        "rrd_id": str(row.get("rrdId") or ""),
        "nm_id": str(row.get("nmId") or ""),
        "operation_date": next(
            (
                str(row.get(field) or "")[:10]
                for field in ("rrDate", "saleDt", "orderDt")
                if str(row.get(field) or "")
            ),
            "",
        ),
    }
    return "finance-row:" + hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _nomenclature_identity_index(
    conn: sqlite3.Connection,
) -> tuple[dict[str, str], set[str], dict[str, str], dict[str, dict[str, Any]]]:
    """Build deterministic active + historical identity without cross-SKU guessing."""

    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sheet_vitrina_v1_nomenclature_items'"
    ).fetchone()
    if table_exists is None:
        return {}, set(), {}, {}
    columns = {
        str(row["name"])
        for row in conn.execute(
            "PRAGMA table_info(sheet_vitrina_v1_nomenclature_items)"
        ).fetchall()
    }
    rows = conn.execute("SELECT * FROM sheet_vitrina_v1_nomenclature_items").fetchall()
    aliases: dict[str, set[str]] = {}
    groups: dict[str, str] = {}
    items: dict[str, dict[str, Any]] = {}
    product_type_groups = {
        "clear": "Clean",
        "anti_spy": "Anti-Spy",
        "matte": "Matte",
    }
    scalar_fields = (
        "nm_id",
        "vendor_code",
        "barcode",
        "our_sku",
        "match_key",
    )
    json_fields = ("barcodes_json", "aliases_json")
    for row in rows:
        nm = str(row["nm_id"] or "") if "nm_id" in columns else ""
        if not nm or nm == "0":
            continue
        item = {column: row[column] for column in columns}
        items.setdefault(nm, item)
        product_type = (
            str(row["product_type"] or "").casefold()
            if "product_type" in columns
            else ""
        )
        canonical_group = product_type_groups.get(product_type)
        if canonical_group:
            groups[nm] = canonical_group
        values: list[str] = []
        for field in scalar_fields:
            if field in columns and row[field] not in (None, ""):
                values.append(str(row[field]))
        for field in json_fields:
            if field not in columns:
                continue
            try:
                parsed = json.loads(row[field] or "[]")
            except (json.JSONDecodeError, TypeError):
                parsed = []
            if isinstance(parsed, list):
                values.extend(str(value) for value in parsed if value not in (None, ""))
        for value in values:
            aliases.setdefault(value.strip().casefold(), set()).add(nm)
    ambiguous = {alias for alias, owners in aliases.items() if len(owners) != 1}
    deterministic = {
        alias: next(iter(owners))
        for alias, owners in aliases.items()
        if len(owners) == 1
    }
    return deterministic, ambiguous, groups, items


def _resolve_finance_nm_id(
    row: Mapping[str, Any],
    *,
    alias_to_nm: Mapping[str, str],
    ambiguous_aliases: set[str],
) -> tuple[str, str, str]:
    raw_nm = str(row.get("nmId") or "").strip()
    if raw_nm and raw_nm != "0":
        return raw_nm, "direct_nm_id", ""
    for field in ("vendorCode", "sku"):
        raw = str(row.get(field) or "").strip()
        if not raw:
            continue
        key = raw.casefold()
        if key in ambiguous_aliases:
            return "", "ambiguous_alias", f"{field}:{raw}"
        resolved = alias_to_nm.get(key, "")
        if resolved:
            return resolved, f"canonical_{field}", ""
    return "", "unresolved", "nmId/vendorCode/sku"


def _functional_wb_cost_state(
    conn: sqlite3.Connection,
    *,
    as_of_date: str,
    nm_id: str,
) -> tuple[dict[str, Any] | None, bool]:
    """Return the active functional cost row and whether legacy fallback is forbidden."""

    required = {
        "sheet_vitrina_v1_warehouse_functional_cutovers",
        "sheet_vitrina_v1_warehouse_wb_daily_cost",
    }
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'sheet_vitrina_v1_warehouse_%'"
        ).fetchall()
    }
    if not required.issubset(tables) or as_of_date < OUR_WB_COST_OPENING_DATE:
        return None, False
    cutover = conn.execute(
        """SELECT cutover_at FROM sheet_vitrina_v1_warehouse_functional_cutovers
           WHERE cutover_id='warehouse_functional_cutover_v1' AND status='posted'"""
    ).fetchone()
    if cutover is None:
        return None, False
    cutover_date = business_date_from_timestamp(str(cutover["cutover_at"]))
    row = conn.execute(
        """SELECT * FROM sheet_vitrina_v1_warehouse_wb_daily_cost
           WHERE cutover_id='warehouse_functional_cutover_v1'
             AND as_of_date=? AND nm_id=?""",
        (as_of_date, nm_id),
    ).fetchone()
    if row is not None:
        quantity = max(_decimal(row["quantity"]), ZERO)
        quality = str(row["quality"] or "historical_provisional")
        if quality == "business_approved_archival_estimate" and archival_estimate_for_nm_id(
            conn,
            nm_id=nm_id,
            as_of_date=as_of_date,
        ) is None:
            return None, True
        fallback = quantity if quality == "fallback_average" else ZERO
        estimated = max(quantity - fallback, ZERO)
        return {
            "our_wb_unit_cost_rub": (
                None
                if quality == "zero_quantity_without_cost_basis"
                else row["wac_rub"]
            ),
            "confirmed_qty": "0",
            "estimated_qty": _money_text(estimated),
            "fallback_qty": _money_text(fallback),
            "confirmed_share_pct": "0",
            "source_status": quality,
            "component_status_json": row["provenance_json"],
            "inputs_hash": row["fingerprint"],
        }, True
    estimate = archival_estimate_for_nm_id(
        conn,
        nm_id=nm_id,
        as_of_date=as_of_date,
    )
    if estimate is not None:
        return {
            "our_wb_unit_cost_rub": str(estimate["unit_cost_rub"]),
            "confirmed_qty": "0",
            "estimated_qty": "0",
            "fallback_qty": "0",
            "confirmed_share_pct": "0",
            "source_status": str(estimate.get("quality") or ""),
            "component_status_json": json.dumps(
                estimate,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ),
            "inputs_hash": str(estimate.get("row_fingerprint") or ""),
        }, True
    if as_of_date < cutover_date:
        return None, True
    # A current functional balance is not historical evidence.  Once the
    # functional boundary applies, a missing exact-day row must remain unknown
    # instead of inheriting the last published warehouse version.
    return None, True


def week_bounds(day: date) -> tuple[date, date]:
    start = day - timedelta(days=day.weekday())
    return start, start + timedelta(days=6)


def historical_week_bounds(today: date | None = None) -> list[tuple[date, date]]:
    now = today or datetime.now(MOSCOW).date()
    first_start, _ = week_bounds(FIRST_INCLUDED_DATE)
    current_start, _ = week_bounds(now)
    latest_closed_end = current_start - timedelta(days=1)
    result: list[tuple[date, date]] = []
    cursor = first_start
    while cursor + timedelta(days=6) <= latest_closed_end:
        result.append((cursor, cursor + timedelta(days=6)))
        cursor += timedelta(days=7)
    return result


@dataclass(frozen=True)
class FinanceHttpResult:
    status: int
    rows: list[dict[str, Any]]
    headers: Mapping[str, str]


class WbFinanceApiClient:
    """Official Finance API client with rrdId pagination and rate-limit handling."""

    def __init__(
        self,
        token: str,
        *,
        url: str = FINANCE_URL,
        limit: int = 100_000,
        min_interval_seconds: float = 60.0,
        max_retries: int = 8,
        sleep: Callable[[float], None] = time.sleep,
        request: Callable[[dict[str, Any]], FinanceHttpResult] | None = None,
    ) -> None:
        if not token:
            raise ValueError("WB_API_TOKEN is required for Finance API")
        self._token = token
        self.url = url
        self.limit = min(max(1, int(limit)), 100_000)
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self.max_retries = max(0, int(max_retries))
        self.sleep = sleep
        self._request_override = request
        self._last_request_at = 0.0

    def fetch_week(self, date_from: date, date_to: date) -> list[dict[str, Any]]:
        all_rows: list[dict[str, Any]] = []
        rrd_id = 0
        seen_cursors: set[int] = set()
        while True:
            payload = {
                "dateFrom": date_from.isoformat(),
                "dateTo": date_to.isoformat(),
                "limit": self.limit,
                "rrdId": rrd_id,
                "period": "weekly",
            }
            response = self._request_with_retry(payload)
            if response.status == 204:
                break
            if response.status != 200:
                raise RuntimeError(f"Finance API unexpected HTTP {response.status}")
            rows = response.rows
            if not rows:
                break
            all_rows.extend(rows)
            next_cursor = int(str(rows[-1].get("rrdId") or "0"))
            if next_cursor <= 0 or next_cursor == rrd_id or next_cursor in seen_cursors:
                raise RuntimeError("Finance API pagination cursor did not advance")
            seen_cursors.add(next_cursor)
            rrd_id = next_cursor
        return all_rows

    def _request_with_retry(self, payload: dict[str, Any]) -> FinanceHttpResult:
        attempt = 0
        while True:
            elapsed = time.monotonic() - self._last_request_at
            if self._last_request_at and elapsed < self.min_interval_seconds:
                self.sleep(self.min_interval_seconds - elapsed)
            self._last_request_at = time.monotonic()
            response = self._request(payload)
            if response.status != 429:
                return response
            if attempt >= self.max_retries:
                raise RuntimeError("Finance API rate limit retry budget exhausted")
            attempt += 1
            raw_retry = str(
                response.headers.get("X-Ratelimit-Retry")
                or response.headers.get("Retry-After")
                or "60"
            )
            try:
                retry_seconds = float(raw_retry)
            except ValueError:
                retry_seconds = 60.0
            if retry_seconds > 10_000:
                retry_seconds /= 1_000.0
            self.sleep(max(self.min_interval_seconds, retry_seconds, 1.0))

    def _request(self, payload: dict[str, Any]) -> FinanceHttpResult:
        if self._request_override is not None:
            return self._request_override(payload)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={
                "Authorization": self._token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                raw = response.read()
                rows = json.loads(raw) if raw else []
                return FinanceHttpResult(
                    int(response.status), list(rows), dict(response.headers.items())
                )
        except urllib.error.HTTPError as exc:
            exc.read()
            return FinanceHttpResult(
                int(exc.code), [], dict(exc.headers.items()) if exc.headers else {}
            )

    def __repr__(self) -> str:
        return f"WbFinanceApiClient(url={self.url!r}, token=<redacted>)"


def classify_deduction(row: Mapping[str, Any]) -> str:
    """Versioned, single-bucket classifier for deduction rows."""
    name = " ".join(
        str(row.get(key) or "")
        for key in ("bonusTypeName", "sellerOperName", "paymentProcessing")
    ).casefold()
    if any(
        token in name for token in ("wb продвиж", "продвижен", "реклам", "маркетинг")
    ):
        return "marketing"
    if any(token in name for token in ("баллы за отзывы", "списание за отзыв")):
        return "review_points"
    if "транзит" in name and any(token in name for token in ("логист", "достав")):
        return "transit_logistics"
    if any(token in name for token in ("подписк", "джем", "jamm")):
        return "subscriptions"
    if any(token in name for token in ("платн", "сервис")):
        return "paid_services"
    return "other_deductions"


class WbFinanceWeeklyBlock:
    def __init__(
        self,
        runtime_dir: Path,
        *,
        seller_id: str = "canonical",
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.store_registry = StoreRegistry(self.runtime_dir)
        self.db_path = self.store_registry.resolve("operational")
        self.seller_id = seller_id or "canonical"
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self._capitalization_cache_key = ""
        self._capitalization_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._capitalization_cache_connection: sqlite3.Connection | None = None
        self._canonical_cost_snapshot_connection: sqlite3.Connection | None = None
        self._canonical_cost_snapshot: CanonicalChannelCostSnapshot | None = None
        self._canonical_cost_resolution_cache: dict[
            tuple[str, str, str], dict[str, Any]
        ] = {}
        self._nomenclature_cache_connection: sqlite3.Connection | None = None
        self._nomenclature_cache: tuple[
            dict[str, str], set[str], dict[str, str], dict[str, dict[str, Any]]
        ] = ({}, set(), {}, {})

    def ensure_schema(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        manifest = self.store_registry.load()
        split_storage = (
            manifest.state == "cutover"
            and manifest.canonical_source == "split"
        )
        with self._connect() as conn:
            if not split_storage:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS wb_finance_weekly_raw_rows (
                        seller_id TEXT NOT NULL, report_id TEXT NOT NULL,
                        rrd_id TEXT NOT NULL, report_type INTEGER,
                        week_start TEXT NOT NULL, week_end TEXT NOT NULL,
                        nm_id TEXT, vendor_code TEXT, barcode TEXT,
                        doc_type_name TEXT, seller_oper_name TEXT,
                        row_hash TEXT NOT NULL, raw_json TEXT NOT NULL,
                        first_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                        PRIMARY KEY (seller_id, report_id, rrd_id)
                    );
                    CREATE INDEX IF NOT EXISTS wb_finance_raw_by_week
                    ON wb_finance_weekly_raw_rows(
                        seller_id,week_start,week_end
                    );
                    CREATE INDEX IF NOT EXISTS wb_finance_raw_by_sku_week
                    ON wb_finance_weekly_raw_rows(
                        seller_id,nm_id,week_start,week_end
                    );
                    """
                )
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS wb_finance_weekly_reports (
                    seller_id TEXT NOT NULL, report_id TEXT NOT NULL, report_type INTEGER,
                    week_start TEXT NOT NULL, week_end TEXT NOT NULL, create_date TEXT,
                    currency TEXT, row_count INTEGER NOT NULL, content_hash TEXT NOT NULL,
                    first_loaded_at TEXT NOT NULL, last_synced_at TEXT NOT NULL,
                    PRIMARY KEY (seller_id, report_id)
                );
                CREATE TABLE IF NOT EXISTS wb_finance_weekly_sync (
                    seller_id TEXT NOT NULL, week_start TEXT NOT NULL, week_end TEXT NOT NULL,
                    status TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
                    first_loaded_at TEXT, last_synced_at TEXT, next_retry_at TEXT,
                    report_count INTEGER NOT NULL DEFAULT 0, raw_row_count INTEGER NOT NULL DEFAULT 0,
                    content_hash TEXT, unchanged_sync_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT, PRIMARY KEY (seller_id, week_start, week_end)
                );
                CREATE TABLE IF NOT EXISTS wb_finance_weekly_aggregates (
                    seller_id TEXT NOT NULL, week_start TEXT NOT NULL, week_end TEXT NOT NULL,
                    classifier_version TEXT NOT NULL, metrics_json TEXT NOT NULL,
                    report_ids_json TEXT NOT NULL, report_types_json TEXT NOT NULL,
                    unknown_reasons_json TEXT NOT NULL, calculated_at TEXT NOT NULL,
                    PRIMARY KEY (seller_id, week_start, week_end)
                );
                CREATE TABLE IF NOT EXISTS wb_finance_weekly_reconciliation (
                    seller_id TEXT NOT NULL, week_start TEXT NOT NULL, week_end TEXT NOT NULL,
                    status TEXT NOT NULL, difference_rub TEXT, detail_json TEXT NOT NULL,
                    checked_at TEXT NOT NULL, PRIMARY KEY (seller_id, week_start, week_end)
                );
                CREATE TABLE IF NOT EXISTS wb_finance_weekly_cost_coverage (
                    seller_id TEXT NOT NULL, week_start TEXT NOT NULL, week_end TEXT NOT NULL,
                    matched_units INTEGER NOT NULL, unmatched_units INTEGER NOT NULL,
                    coverage_pct TEXT, cogs_rub TEXT, problem_skus_json TEXT NOT NULL,
                    quality_json TEXT NOT NULL DEFAULT '{}',
                    coverage_json TEXT NOT NULL DEFAULT '{}',
                    cost_state_hash TEXT NOT NULL DEFAULT '',
                    calculated_at TEXT NOT NULL, PRIMARY KEY (seller_id, week_start, week_end)
                );
                CREATE TABLE IF NOT EXISTS wb_finance_weekly_sku_aggregates (
                    seller_id TEXT NOT NULL,
                    week_start TEXT NOT NULL,
                    week_end TEXT NOT NULL,
                    nm_id TEXT NOT NULL,
                    formula_version TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    coverage_json TEXT NOT NULL,
                    raw_source_digest TEXT NOT NULL,
                    week_content_hash TEXT NOT NULL,
                    cost_state_hash TEXT NOT NULL,
                    raw_row_count INTEGER NOT NULL,
                    calculated_at TEXT NOT NULL,
                    PRIMARY KEY (seller_id,week_start,week_end,nm_id)
                );
                CREATE INDEX IF NOT EXISTS wb_finance_sku_aggregate_lookup
                ON wb_finance_weekly_sku_aggregates(seller_id,nm_id,week_start,week_end);
                CREATE TABLE IF NOT EXISTS wb_finance_projection_audit (
                    audit_id TEXT PRIMARY KEY,
                    seller_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    scope_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            coverage_columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(wb_finance_weekly_cost_coverage)"
                ).fetchall()
            }
            if "quality_json" not in coverage_columns:
                conn.execute(
                    "ALTER TABLE wb_finance_weekly_cost_coverage ADD COLUMN quality_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "cost_state_hash" not in coverage_columns:
                conn.execute(
                    "ALTER TABLE wb_finance_weekly_cost_coverage ADD COLUMN cost_state_hash TEXT NOT NULL DEFAULT ''"
                )
            if "coverage_json" not in coverage_columns:
                conn.execute(
                    "ALTER TABLE wb_finance_weekly_cost_coverage ADD COLUMN coverage_json TEXT NOT NULL DEFAULT '{}'"
                )
            conn.commit()

    def sync_week(
        self, week_start: date, week_end: date, client: WbFinanceApiClient
    ) -> dict[str, Any]:
        self.ensure_schema()
        now = (
            self.now_factory()
            .astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        self._mark_loading(week_start, week_end, now)
        try:
            rows = client.fetch_week(week_start, week_end)
            if not rows:
                with self._connect() as conn:
                    conn.execute(
                        """UPDATE wb_finance_weekly_sync SET status='waiting',last_synced_at=?,next_retry_at=?,
                        report_count=0,raw_row_count=0,last_error=NULL WHERE seller_id=? AND week_start=? AND week_end=?""",
                        (
                            now,
                            (
                                self.now_factory().astimezone(timezone.utc)
                                + timedelta(hours=1)
                            )
                            .isoformat()
                            .replace("+00:00", "Z"),
                            self.seller_id,
                            week_start.isoformat(),
                            week_end.isoformat(),
                        ),
                    )
                    conn.commit()
                return {
                    "status": "waiting",
                    "week_start": week_start.isoformat(),
                    "week_end": week_end.isoformat(),
                    "report_count": 0,
                    "raw_row_count": 0,
                }
            result = self.ingest_week(week_start, week_end, rows, synced_at=now)
            return result
        except Exception as exc:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO wb_finance_weekly_sync
                    (seller_id,week_start,week_end,status,attempt_count,last_synced_at,last_error)
                    VALUES (?,?,?,'error_loading',1,?,?)
                    ON CONFLICT(seller_id,week_start,week_end) DO UPDATE SET
                    status='error_loading', attempt_count=attempt_count+1,
                    last_synced_at=excluded.last_synced_at,last_error=excluded.last_error""",
                    (
                        self.seller_id,
                        week_start.isoformat(),
                        week_end.isoformat(),
                        now,
                        str(exc)[:2000],
                    ),
                )
                conn.commit()
            raise

    def ingest_week(
        self,
        week_start: date,
        week_end: date,
        rows: Iterable[Mapping[str, Any]],
        *,
        synced_at: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_schema()
        synced = synced_at or self.now_factory().astimezone(
            timezone.utc
        ).isoformat().replace("+00:00", "Z")
        normalized_rows = [dict(row) for row in rows]
        by_report: dict[str, list[dict[str, Any]]] = {}
        for row in normalized_rows:
            report_id = str(row.get("reportId") or "")
            rrd_id = str(row.get("rrdId") or "")
            if not report_id or not rrd_id:
                raise ValueError("Finance row must contain reportId and rrdId")
            by_report.setdefault(report_id, []).append(row)
        full_hash = hashlib.sha256(
            "\n".join(sorted(self._row_hash(row) for row in normalized_rows)).encode(
                "utf-8"
            )
        ).hexdigest()
        manifest = self.store_registry.load()
        split_ingest = (
            manifest.state == "cutover"
            and manifest.canonical_source == "split"
        )
        shadow_ingest_enabled = self._shadow_ingest_enabled()
        outbox_result = None
        if split_ingest:
            outbox_result = FinanceRawIngestor(
                self.store_registry,
                seller_id=self.seller_id,
                now_factory=lambda: synced,
            ).ingest_batch(
                normalized_rows,
                source_identity=(
                    "wb-finance-week:"
                    + week_start.isoformat()
                    + "/"
                    + week_end.isoformat()
                ),
                source_sha256="sha256:" + full_hash,
                week_start=week_start,
                week_end=week_end,
            )
        with self._connect() as conn:
            if shadow_ingest_enabled:
                if (
                    split_ingest
                    or manifest.state != "monolith"
                    or self.store_registry.resolve("finance_raw", manifest=manifest)
                    != self.store_registry.resolve("operational", manifest=manifest)
                ):
                    raise ValueError(
                        "Finance shadow ingest may only share the canonical monolith transaction"
                    )
                ensure_raw_schema(conn)
            previous = conn.execute(
                "SELECT content_hash,unchanged_sync_count,first_loaded_at FROM wb_finance_weekly_sync WHERE seller_id=? AND week_start=? AND week_end=?",
                (self.seller_id, week_start.isoformat(), week_end.isoformat()),
            ).fetchone()
            for report_id, report_rows in by_report.items():
                first = report_rows[0]
                report_hash = hashlib.sha256(
                    "\n".join(
                        sorted(self._row_hash(row) for row in report_rows)
                    ).encode("utf-8")
                ).hexdigest()
                conn.execute(
                    """INSERT INTO wb_finance_weekly_reports
                    (seller_id,report_id,report_type,week_start,week_end,create_date,currency,row_count,content_hash,first_loaded_at,last_synced_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(seller_id,report_id) DO UPDATE SET
                    report_type=excluded.report_type,week_start=excluded.week_start,week_end=excluded.week_end,
                    create_date=excluded.create_date,currency=excluded.currency,row_count=excluded.row_count,
                    content_hash=excluded.content_hash,last_synced_at=excluded.last_synced_at""",
                    (
                        self.seller_id,
                        report_id,
                        int(first.get("reportType") or 0),
                        week_start.isoformat(),
                        week_end.isoformat(),
                        str(first.get("createDate") or ""),
                        str(first.get("currency") or "RUB"),
                        len(report_rows),
                        report_hash,
                        synced,
                        synced,
                    ),
                )
                for row in report_rows:
                    if split_ingest:
                        continue
                    raw_json = json.dumps(
                        row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    )
                    conn.execute(
                        """INSERT INTO wb_finance_weekly_raw_rows
                        (seller_id,report_id,rrd_id,report_type,week_start,week_end,nm_id,vendor_code,barcode,doc_type_name,seller_oper_name,row_hash,raw_json,first_seen_at,updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(seller_id,report_id,rrd_id) DO UPDATE SET
                        report_type=excluded.report_type,week_start=excluded.week_start,week_end=excluded.week_end,
                        nm_id=excluded.nm_id,vendor_code=excluded.vendor_code,barcode=excluded.barcode,
                        doc_type_name=excluded.doc_type_name,seller_oper_name=excluded.seller_oper_name,
                        row_hash=excluded.row_hash,raw_json=excluded.raw_json,updated_at=excluded.updated_at""",
                        (
                            self.seller_id,
                            report_id,
                            str(row.get("rrdId")),
                            int(row.get("reportType") or 0),
                            week_start.isoformat(),
                            week_end.isoformat(),
                            str(row.get("nmId") or ""),
                            str(row.get("vendorCode") or ""),
                            str(row.get("sku") or ""),
                            str(row.get("docTypeName") or ""),
                            str(row.get("sellerOperName") or ""),
                            self._row_hash(row),
                            raw_json,
                            synced,
                            synced,
                        ),
                    )
            if by_report:
                placeholders = ",".join("?" for _ in by_report)
                if not split_ingest:
                    conn.execute(
                        f"DELETE FROM wb_finance_weekly_raw_rows WHERE seller_id=? AND week_start=? AND week_end=? AND report_id NOT IN ({placeholders})",
                        (
                            self.seller_id,
                            week_start.isoformat(),
                            week_end.isoformat(),
                            *by_report.keys(),
                        ),
                    )
                conn.execute(
                    f"DELETE FROM wb_finance_weekly_reports WHERE seller_id=? AND week_start=? AND week_end=? AND report_id NOT IN ({placeholders})",
                    (
                        self.seller_id,
                        week_start.isoformat(),
                        week_end.isoformat(),
                        *by_report.keys(),
                    ),
                )
            unchanged = (
                int(previous["unchanged_sync_count"] or 0) + 1
                if previous and previous["content_hash"] == full_hash
                else 0
            )
            status = "completed" if unchanged >= 1 else "loaded_preliminary"
            first_loaded = (
                previous["first_loaded_at"]
                if previous and previous["first_loaded_at"]
                else synced
            )
            conn.execute(
                """INSERT INTO wb_finance_weekly_sync
                (seller_id,week_start,week_end,status,attempt_count,first_loaded_at,last_synced_at,next_retry_at,
                 report_count,raw_row_count,content_hash,unchanged_sync_count,last_error)
                VALUES (?,?,?,?,1,?,?,?, ?,?,?,?,NULL)
                ON CONFLICT(seller_id,week_start,week_end) DO UPDATE SET
                status=excluded.status,attempt_count=attempt_count+1,first_loaded_at=COALESCE(first_loaded_at,excluded.first_loaded_at),
                last_synced_at=excluded.last_synced_at,next_retry_at=excluded.next_retry_at,
                report_count=excluded.report_count,raw_row_count=excluded.raw_row_count,
                content_hash=excluded.content_hash,unchanged_sync_count=excluded.unchanged_sync_count,last_error=NULL""",
                (
                    self.seller_id,
                    week_start.isoformat(),
                    week_end.isoformat(),
                    status,
                    first_loaded,
                    synced,
                    (self.now_factory().astimezone(timezone.utc) + timedelta(hours=1))
                    .isoformat()
                    .replace("+00:00", "Z")
                    if status != "completed"
                    else None,
                    len(by_report),
                    len(normalized_rows),
                    full_hash,
                    unchanged,
                ),
            )
            if shadow_ingest_enabled and not split_ingest:
                outbox_result = FinanceRawIngestor(
                    self.store_registry,
                    seller_id=self.seller_id,
                    now_factory=lambda: synced,
                ).ingest_batch(
                    normalized_rows,
                    source_identity=(
                        "wb-finance-week:"
                        + week_start.isoformat()
                        + "/"
                        + week_end.isoformat()
                    ),
                    source_sha256="sha256:" + full_hash,
                    week_start=week_start,
                    week_end=week_end,
                    connection=conn,
                )
            conn.commit()
        aggregate = self.recalculate_week(week_start, week_end)
        outbox_acknowledgement = None
        if split_ingest and outbox_result is not None:
            outbox_acknowledgement = self._acknowledge_split_outbox(
                expected_sequence=int(outbox_result.sequence_no),
            )
        return {
            "status": status,
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "report_count": len(by_report),
            "raw_row_count": len(normalized_rows),
            "aggregate": aggregate,
            "storage_outbox": (
                {
                    "status": outbox_result.status,
                    "batch_id": outbox_result.batch_id,
                    "event_id": outbox_result.event_id,
                    "sequence_no": outbox_result.sequence_no,
                    "acknowledgement": outbox_acknowledgement,
                }
                if outbox_result is not None
                else {
                    "status": "disabled",
                    "reason": "WB_CORE_FINANCE_STORAGE_SHADOW_INGEST_ENABLED is not enabled",
                }
            ),
        }

    def _acknowledge_split_outbox(
        self,
        *,
        expected_sequence: int,
    ) -> dict[str, Any]:
        """Acknowledge only events whose complete operational projection exists."""

        consumer = FinanceOutboxConsumer(
            self.store_registry,
            apply_event=self._verify_outbox_projection,
            now_factory=lambda: self.now_factory()
            .astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        )
        acknowledged: list[dict[str, Any]] = []
        while True:
            result = consumer.consume_next()
            if result is None:
                with self.store_registry.session(
                    "finance_raw",
                    mode="ro",
                    operation="wb_finance_outbox_ack_readback",
                ) as raw:
                    cursor = raw.execute(
                        """SELECT last_sequence_no
                           FROM finance_raw_consumer_cursors
                           WHERE consumer_id='finance_operational_projection_v1'"""
                    ).fetchone()
                last_sequence = int(cursor["last_sequence_no"]) if cursor else 0
                if last_sequence < expected_sequence:
                    raise ValueError(
                        "Finance operational outbox acknowledgement is incomplete"
                    )
                return {
                    "status": "already_acknowledged",
                    "expected_sequence": expected_sequence,
                    "last_sequence_no": last_sequence,
                    "events": acknowledged,
                }
            acknowledged.append(
                {
                    "event_id": result.event_id,
                    "sequence_no": result.sequence_no,
                    "status": result.status,
                    "duplicate": result.duplicate,
                }
            )
            if result.sequence_no > expected_sequence:
                raise ValueError(
                    "Finance outbox cursor advanced beyond the expected event"
                )
            if result.sequence_no == expected_sequence:
                return {
                    "status": "acknowledged",
                    "expected_sequence": expected_sequence,
                    "last_sequence_no": result.sequence_no,
                    "events": acknowledged,
                }

    def recover_receipted_split_outbox(
        self,
        *,
        max_events: int = 64,
    ) -> dict[str, Any]:
        """Acknowledge only pending events with an exact operational receipt."""

        if max_events < 1 or max_events > 64:
            raise ValueError("receipted outbox recovery max_events must be within 1..64")
        manifest = self.store_registry.load()
        if not (
            manifest.state == "cutover"
            and manifest.canonical_source == "split"
        ):
            return {
                "status": "disabled",
                "reason": "canonical Finance storage is not selected split",
                "events": [],
            }
        consumer = FinanceOutboxConsumer(
            self.store_registry,
            apply_event=self._verify_outbox_projection,
            now_factory=lambda: self.now_factory()
            .astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        )
        recovered: list[dict[str, Any]] = []
        while True:
            with self.store_registry.session(
                "finance_raw",
                mode="ro",
                operation="wb_finance_receipted_outbox_recovery_read",
            ) as raw:
                cursor = raw.execute(
                    """SELECT last_sequence_no
                       FROM finance_raw_consumer_cursors
                       WHERE consumer_id='finance_operational_projection_v1'"""
                ).fetchone()
                last_sequence = int(cursor["last_sequence_no"]) if cursor else 0
                event = raw.execute(
                    """SELECT event_id,sequence_no,payload_json
                       FROM finance_raw_outbox
                       WHERE sequence_no>? ORDER BY sequence_no LIMIT 1""",
                    (last_sequence,),
                ).fetchone()
            if event is None:
                return {
                    "status": "acknowledged" if recovered else "clean",
                    "last_sequence_no": last_sequence,
                    "events": recovered,
                }
            if len(recovered) >= max_events:
                raise ValueError(
                    "receipted outbox recovery reached its bounded event limit"
                )
            sequence_no = int(event["sequence_no"])
            if sequence_no != last_sequence + 1:
                raise ValueError(
                    "receipted outbox recovery found a sequence gap or reorder"
                )
            payload = json.loads(str(event["payload_json"]))
            rows_digest = str(payload.get("rows_digest") or "")
            row_count = int(payload.get("row_count") or 0)
            with self.store_registry.session(
                "operational",
                mode="ro",
                operation="wb_finance_receipted_outbox_recovery_receipt",
            ) as operational:
                receipt = operational.execute(
                    """SELECT sequence_no,source_revision,result_row_count,
                              result_digest
                       FROM finance_operational_receipts
                       WHERE consumer_id='finance_operational_projection_v1'
                         AND event_id=?""",
                    (str(event["event_id"]),),
                ).fetchone()
                operational_cursor = operational.execute(
                    """SELECT last_sequence_no
                       FROM finance_operational_consumer_cursors
                       WHERE consumer_id='finance_operational_projection_v1'"""
                ).fetchone()
            if receipt is None:
                return {
                    "status": "waiting_for_projection",
                    "last_sequence_no": last_sequence,
                    "pending_event_id": str(event["event_id"]),
                    "pending_sequence_no": sequence_no,
                    "events": recovered,
                }
            if (
                int(receipt["sequence_no"]) != sequence_no
                or str(receipt["source_revision"]) != rows_digest
                or int(receipt["result_row_count"]) != row_count
                or not str(receipt["result_digest"])
                or operational_cursor is None
                or int(operational_cursor["last_sequence_no"]) < sequence_no
            ):
                raise ValueError(
                    "receipted outbox recovery evidence does not match the pending event"
                )
            result = consumer.consume_next()
            if (
                result is None
                or result.sequence_no != sequence_no
                or result.event_id != str(event["event_id"])
                or not result.duplicate
            ):
                raise ValueError(
                    "receipted outbox recovery did not produce an exact duplicate acknowledgement"
                )
            recovered.append(
                {
                    "event_id": result.event_id,
                    "sequence_no": result.sequence_no,
                    "status": result.status,
                    "duplicate": result.duplicate,
                }
            )

    def _verify_outbox_projection(
        self,
        conn: sqlite3.Connection,
        payload: Mapping[str, Any],
    ) -> tuple[int, str]:
        """Prove an outbox batch is fully represented in operational storage."""

        if str(payload.get("seller_id") or "") != self.seller_id:
            raise ValueError("Finance outbox seller identity mismatch")
        period = str(payload.get("report_period") or "").split("/", 1)
        if len(period) != 2:
            raise ValueError("Finance outbox report period is invalid")
        week_start, week_end = period
        source_sha = str(payload.get("source_sha256") or "")
        if not source_sha.startswith("sha256:"):
            raise ValueError("Finance outbox source digest is invalid")
        content_hash = source_sha.removeprefix("sha256:")
        expected_rows = int(payload.get("row_count") or 0)
        sync = conn.execute(
            """SELECT raw_row_count,content_hash
               FROM wb_finance_weekly_sync
               WHERE seller_id=? AND week_start=? AND week_end=?""",
            (self.seller_id, week_start, week_end),
        ).fetchone()
        report_rows = conn.execute(
            """SELECT COALESCE(SUM(row_count),0)
               FROM wb_finance_weekly_reports
               WHERE seller_id=? AND week_start=? AND week_end=?""",
            (self.seller_id, week_start, week_end),
        ).fetchone()
        projection_counts = {
            table: int(
                conn.execute(
                    f"""SELECT COUNT(*) FROM {table}
                        WHERE seller_id=? AND week_start=? AND week_end=?""",
                    (self.seller_id, week_start, week_end),
                ).fetchone()[0]
            )
            for table in (
                "wb_finance_weekly_aggregates",
                "wb_finance_weekly_cost_coverage",
                "wb_finance_weekly_reconciliation",
            )
        }
        sku = conn.execute(
            """SELECT COUNT(*) AS row_count,
                      SUM(CASE WHEN week_content_hash<>? THEN 1 ELSE 0 END)
                         AS mismatched_hashes
               FROM wb_finance_weekly_sku_aggregates
               WHERE seller_id=? AND week_start=? AND week_end=?""",
            (content_hash, self.seller_id, week_start, week_end),
        ).fetchone()
        evidence = {
            "seller_id": self.seller_id,
            "week_start": week_start,
            "week_end": week_end,
            "expected_rows": expected_rows,
            "sync_raw_row_count": int(sync["raw_row_count"]) if sync else -1,
            "sync_content_hash": str(sync["content_hash"]) if sync else "",
            "report_raw_row_count": int(report_rows[0]) if report_rows else -1,
            "projection_counts": projection_counts,
            "sku_projection_count": int(sku["row_count"]) if sku else 0,
            "sku_hash_mismatches": int(sku["mismatched_hashes"] or 0)
            if sku
            else -1,
        }
        if (
            sync is None
            or evidence["sync_raw_row_count"] != expected_rows
            or evidence["report_raw_row_count"] != expected_rows
            or evidence["sync_content_hash"] != content_hash
            or any(value != 1 for value in projection_counts.values())
            or evidence["sku_projection_count"] < 1
            or evidence["sku_hash_mismatches"] != 0
        ):
            raise ValueError(
                "Finance operational projection does not match the outbox event"
            )
        return expected_rows, "sha256:" + hashlib.sha256(
            json.dumps(
                evidence,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def _shadow_ingest_enabled(self) -> bool:
        if (
            os.environ.get("WB_CORE_FINANCE_STORAGE_SHADOW_INGEST_ENABLED", "")
            .strip()
            .lower()
            in {"1", "true", "yes"}
        ):
            return True
        state_path = (
            self.runtime_dir / FINANCE_SHADOW_INGEST_STATE_FILENAME
        )
        if not state_path.exists():
            return False
        if not state_path.is_file() or state_path.stat().st_mode & 0o077:
            raise ValueError(
                "Finance shadow ingest state must be a private regular file"
            )
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                "Finance shadow ingest state is unreadable"
            ) from exc
        if (
            not isinstance(state, Mapping)
            or str(state.get("contract_version") or "")
            != FINANCE_SHADOW_INGEST_STATE_CONTRACT
            or not isinstance(state.get("enabled"), bool)
        ):
            raise ValueError("Finance shadow ingest state is invalid")
        return bool(state["enabled"])

    def recalculate_week(self, week_start: date, week_end: date) -> dict[str, Any]:
        self.ensure_schema()
        with self._connect() as conn:
            aggregate = self._recalculate_week_in_connection(conn, week_start, week_end)
            conn.commit()
            return aggregate

    def _recalculate_week_in_connection(
        self,
        conn: sqlite3.Connection,
        week_start: date,
        week_end: date,
    ) -> dict[str, Any]:
        projection = self._build_week_target_projection(
            conn,
            week_start=week_start,
            week_end=week_end,
        )
        self._replace_finance_target_images(
            conn,
            target_keys={(self.seller_id, week_start.isoformat(), week_end.isoformat())},
            images=projection["images"],
        )
        return dict(projection["metrics"])

    def _build_week_target_projection(
        self,
        conn: sqlite3.Connection,
        *,
        week_start: date,
        week_end: date,
    ) -> dict[str, Any]:
        """Build exact Finance target after-images without mutating SQLite."""

        db_rows = conn.execute(
            "SELECT report_id,rrd_id,row_hash,raw_json FROM wb_finance_weekly_raw_rows WHERE seller_id=? AND week_start=? AND week_end=? ORDER BY report_id,rrd_id",
            (self.seller_id, week_start.isoformat(), week_end.isoformat()),
        ).fetchall()
        rows = [json.loads(row["raw_json"]) for row in db_rows]
        aggregate, coverage, unknown = self._aggregate_rows(conn, rows, week_start)
        reports = conn.execute(
            "SELECT report_id,report_type FROM wb_finance_weekly_reports WHERE seller_id=? AND week_start=? AND week_end=? ORDER BY report_id",
            (self.seller_id, week_start.isoformat(), week_end.isoformat()),
        ).fetchall()
        now = (
            self.now_factory()
            .astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        sku_projections = self._rebuild_sku_week_aggregates(
            conn,
            week_start=week_start,
            week_end=week_end,
            raw_rows=db_rows,
            global_metrics=aggregate,
            calculated_at=now,
            parsed_rows=rows,
            persist=False,
        )
        expected_for_pay = sum(
            (
                _decimal(row.get("forPay"))
                if str(row.get("docTypeName") or "").casefold() == "продажа"
                else -_decimal(row.get("forPay"))
                if str(row.get("docTypeName") or "").casefold() == "возврат"
                else ZERO
                for row in rows
            ),
            ZERO,
        )
        actual_for_pay = _decimal(aggregate["to_seller"])
        diff = actual_for_pay - expected_for_pay
        reconcile_status = "ok" if abs(diff) <= Decimal("0.01") else "error"
        sync = conn.execute(
            "SELECT * FROM wb_finance_weekly_sync WHERE seller_id=? "
            "AND week_start=? AND week_end=?",
            (self.seller_id, week_start.isoformat(), week_end.isoformat()),
        ).fetchone()
        if sync is None:
            raise ValueError("Finance sync identity is missing for loaded week")
        sync_row = dict(sync)
        if coverage["unmatched_units"] != 0:
            if str(sync_row.get("status") or "") != "error_loading":
                sync_row["status"] = "incomplete_cost"
        elif str(sync_row.get("status") or "") == "incomplete_cost":
            sync_row["status"] = "completed"
        rows_by_table: dict[str, list[Mapping[str, Any]]] = {
            "wb_finance_weekly_aggregates": [
                {
                    "seller_id": self.seller_id,
                    "week_start": week_start.isoformat(),
                    "week_end": week_end.isoformat(),
                    "classifier_version": CLASSIFIER_VERSION,
                    "metrics_json": json.dumps(aggregate, ensure_ascii=False),
                    "report_ids_json": json.dumps([r["report_id"] for r in reports]),
                    "report_types_json": json.dumps(
                        [int(r["report_type"] or 0) for r in reports]
                    ),
                    "unknown_reasons_json": json.dumps(unknown, ensure_ascii=False),
                    "calculated_at": now,
                }
            ],
            "wb_finance_weekly_cost_coverage": [
                {
                    "seller_id": self.seller_id,
                    "week_start": week_start.isoformat(),
                    "week_end": week_end.isoformat(),
                    "matched_units": coverage["matched_units"],
                    "unmatched_units": coverage["unmatched_units"],
                    "coverage_pct": coverage["coverage_pct"],
                    "cogs_rub": coverage["cogs_rub"],
                    "problem_skus_json": json.dumps(
                        coverage["problem_skus"], ensure_ascii=False
                    ),
                    "quality_json": json.dumps(
                        coverage["quality"], ensure_ascii=False
                    ),
                    "coverage_json": json.dumps(coverage, ensure_ascii=False),
                    "cost_state_hash": coverage["cost_state_hash"],
                    "calculated_at": now,
                }
            ],
            "wb_finance_weekly_reconciliation": [
                {
                    "seller_id": self.seller_id,
                    "week_start": week_start.isoformat(),
                    "week_end": week_end.isoformat(),
                    "status": reconcile_status,
                    "difference_rub": _money_text(diff),
                    "detail_json": json.dumps(
                        {
                            "raw_for_pay_sum": _money_text(expected_for_pay),
                            "aggregate_to_seller": aggregate["to_seller"],
                        }
                    ),
                    "checked_at": now,
                }
            ],
            "wb_finance_weekly_sku_aggregates": [
                {**item, "calculated_at": now} for item in sku_projections
            ],
            "wb_finance_weekly_sync": [sync_row],
        }
        images: dict[str, Any] = {}
        for table, projected_rows in rows_by_table.items():
            columns = [
                str(row["name"])
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            ]
            images[table] = {
                "columns": columns,
                "rows": [
                    [row.get(column) for column in columns]
                    for row in projected_rows
                ],
            }
        return {"metrics": aggregate, "coverage": coverage, "images": images}

    def _rebuild_sku_week_aggregates(
        self,
        conn: sqlite3.Connection,
        *,
        week_start: date,
        week_end: date,
        raw_rows: Iterable[sqlite3.Row],
        global_metrics: Mapping[str, Any],
        calculated_at: str,
        persist: bool = True,
        parsed_rows: Iterable[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Materialize an indexed, reproducible per-SKU Finance projection."""

        records = list(raw_rows)
        parsed = (
            list(parsed_rows)
            if parsed_rows is not None
            else [json.loads(row["raw_json"]) for row in records]
        )
        if len(parsed) != len(records):
            raise ValueError("parsed Finance row count differs from stored row count")
        alias_to_nm, ambiguous_aliases, _groups, nomenclature = (
            self._nomenclature_identity_index(conn)
        )
        by_nm: dict[str, list[tuple[dict[str, Any], sqlite3.Row]]] = {
            nm_id: [] for nm_id in nomenclature
        }
        account_rows: list[tuple[dict[str, Any], sqlite3.Row]] = []
        identity_blockers: list[dict[str, Any]] = []
        for raw, stored in zip(parsed, records, strict=True):
            nm_id, method, problem = _resolve_finance_nm_id(
                raw,
                alias_to_nm=alias_to_nm,
                ambiguous_aliases=ambiguous_aliases,
            )
            if nm_id:
                by_nm.setdefault(nm_id, []).append((raw, stored))
            elif method == "unresolved" and str(raw.get("nmId") or "").strip() in {"", "0"}:
                account_rows.append((raw, stored))
            else:
                identity_blockers.append(
                    {
                        "report_id": str(raw.get("reportId") or ""),
                        "rrd_id": str(raw.get("rrdId") or ""),
                        "identity_method": method,
                        "reason": problem,
                    }
                )
        week_content_hash = hashlib.sha256(
            "\n".join(sorted(str(row["row_hash"]) for row in records)).encode("utf-8")
        ).hexdigest()
        if persist:
            conn.execute(
                "DELETE FROM wb_finance_weekly_sku_aggregates WHERE seller_id=? AND week_start=? AND week_end=?",
                (self.seller_id, week_start.isoformat(), week_end.isoformat()),
            )
        projections: list[dict[str, Any]] = []

        def store_projection(
            nm_id: str,
            selected: list[tuple[dict[str, Any], sqlite3.Row]],
            *,
            row_kind: str,
        ) -> None:
            source_rows = [item[0] for item in selected]
            coverage = self._calculate_cogs(
                conn,
                source_rows,
                week_start,
                include_details=True,
            )
            metrics, _metrics_coverage, unknown = self._aggregate_rows(
                conn,
                source_rows,
                week_start,
                coverage_override=coverage,
            )
            unique_cost_dependencies: dict[tuple[str, str], dict[str, Any]] = {}
            for detail in coverage["detail_rows"]:
                key = (str(detail["operation_date"]), str(detail["source_digest"]))
                unique_cost_dependencies.setdefault(
                    key,
                    {
                        field: detail.get(field)
                        for field in (
                            "nm_id",
                            "operation_date",
                            "channel",
                            "facility_id",
                            "pool",
                            "fbs_order_id",
                            "source_date",
                            "source_identity",
                            "source_digest",
                            "source_quality",
                            "projection_quality",
                            "selection_method",
                            "formula_version",
                        )
                    },
                )
            coverage["detail_rows"] = list(unique_cost_dependencies.values())
            source_manifest = [
                [str(item[1]["report_id"]), str(item[1]["rrd_id"]), str(item[1]["row_hash"])]
                for item in selected
            ]
            raw_source_digest = "sha256:" + hashlib.sha256(
                json.dumps(source_manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            coverage_payload = {
                **coverage,
                "row_kind": row_kind,
                "unknown_reasons": unknown,
                "identity_blockers": identity_blockers if row_kind == "account" else [],
                "global_net_revenue": global_metrics.get("net_revenue"),
                "global_week_content_hash": week_content_hash,
            }
            projection = {
                "seller_id": self.seller_id,
                "week_start": week_start.isoformat(),
                "week_end": week_end.isoformat(),
                "nm_id": nm_id,
                "formula_version": SKU_AGGREGATE_FORMULA_VERSION,
                "metrics_json": json.dumps(
                    metrics, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
                "coverage_json": json.dumps(
                    coverage_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "raw_source_digest": raw_source_digest,
                "week_content_hash": week_content_hash,
                "cost_state_hash": str(coverage["cost_state_hash"]),
                "raw_row_count": len(selected),
            }
            projections.append(projection)
            if persist:
                conn.execute(
                    """INSERT INTO wb_finance_weekly_sku_aggregates(
                       seller_id,week_start,week_end,nm_id,formula_version,metrics_json,
                       coverage_json,raw_source_digest,week_content_hash,cost_state_hash,
                       raw_row_count,calculated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        projection["seller_id"],
                        projection["week_start"],
                        projection["week_end"],
                        projection["nm_id"],
                        projection["formula_version"],
                        projection["metrics_json"],
                        projection["coverage_json"],
                        projection["raw_source_digest"],
                        projection["week_content_hash"],
                        projection["cost_state_hash"],
                        projection["raw_row_count"],
                        calculated_at,
                    ),
                )

        for nm_id in sorted(by_nm, key=_finance_nm_id_sort_key):
            store_projection(nm_id, by_nm[nm_id], row_kind="sku")
        store_projection("__account__", account_rows, row_kind="account")
        return projections

    def _aggregate_rows(
        self,
        conn: sqlite3.Connection,
        rows: list[dict[str, Any]],
        week_start: date,
        *,
        coverage_override: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
        values: dict[str, Decimal] = {
            key: ZERO
            for key in (
                "sales_qty",
                "returns_qty",
                "revenue_before_returns",
                "returns_amount",
                "combined_commission_control",
                "agent_remuneration",
                "acquiring",
                "wb_remuneration_adjustment",
                "logistics",
                "storage",
                "acceptance",
                "marketing",
                "transit_logistics",
                "penalties",
                "subscriptions",
                "paid_services",
                "review_points",
                "other_deductions",
                "positive_adjustments",
                "corrections",
                "to_seller",
            )
        }
        unknown: set[str] = set()
        for row in rows:
            doc = str(row.get("docTypeName") or "").casefold()
            quantity = _decimal(row.get("quantity"))
            revenue = _decimal(row.get("retailPriceWithDisc"))
            combined = revenue - _decimal(row.get("forPay"))
            acquiring = _decimal(row.get("acquiringFee"))
            adjustment = _decimal(row.get("additionalPayment"))
            if doc == "продажа":
                values["sales_qty"] += quantity
                values["revenue_before_returns"] += revenue
                values["combined_commission_control"] += combined
                values["acquiring"] += acquiring
                values["agent_remuneration"] += combined - acquiring
                values["wb_remuneration_adjustment"] += adjustment
                values["to_seller"] += _decimal(row.get("forPay"))
            elif doc == "возврат":
                values["returns_qty"] += quantity
                values["returns_amount"] += revenue
                values["combined_commission_control"] -= combined
                values["acquiring"] -= acquiring
                values["agent_remuneration"] -= combined - acquiring
                values["wb_remuneration_adjustment"] -= adjustment
                values["to_seller"] -= _decimal(row.get("forPay"))
            values["logistics"] += _decimal(row.get("deliveryService"))
            values["storage"] += _decimal(row.get("paidStorage"))
            acceptance = _decimal(row.get("paidAcceptance"))
            values["acceptance"] += acceptance
            values["penalties"] += _decimal(row.get("penalty"))
            deduction = _decimal(row.get("deduction"))
            if deduction:
                bucket = classify_deduction(row)
                # WB represents an expense reversal/refund as a negative
                # deduction. Preserve that sign so a storno cannot become a
                # second positive expense.
                values[bucket] += deduction
                if bucket == "other_deductions":
                    unknown.add(
                        str(
                            row.get("bonusTypeName")
                            or row.get("sellerOperName")
                            or "Неизвестное удержание"
                        )
                    )
            # ``additionalPayment`` is the official XLSX column
            # "Корректировка Вознаграждения Вайлдберриз (ВВ)".  Sale/return
            # values are already reflected in ``forPay`` and therefore in the
            # combined commission control.  Only a standalone adjustment row
            # (without sale/return sign) is applied separately.
            if doc not in {"продажа", "возврат"} and adjustment:
                values["wb_remuneration_adjustment"] += adjustment
                if adjustment >= ZERO:
                    values["positive_adjustments"] += adjustment
                else:
                    values["corrections"] += abs(adjustment)
        net_revenue = values["revenue_before_returns"] - values["returns_amount"]
        capitalization = self._capitalization_reconciliation(conn, rows)
        capitalized_acceptance = _decimal(capitalization["matched_acceptance_rub"])
        capitalized_transit = _decimal(capitalization["matched_transit_rub"])
        if capitalization["unmatched_expense_count"]:
            unknown.add("Неподтверждённая капитализация приёмки/транзита оставлена в расходах периода")
        total_expenses = sum(
            (
                values[key]
                for key in (
                    "agent_remuneration",
                    "acquiring",
                    "logistics",
                    "storage",
                    "acceptance",
                    "marketing",
                    "transit_logistics",
                    "penalties",
                    "subscriptions",
                    "paid_services",
                    "review_points",
                    "other_deductions",
                    "corrections",
                )
            ),
            ZERO,
        )
        profit_period_expenses = (
            total_expenses - capitalized_acceptance - capitalized_transit
        )
        coverage = (
            dict(coverage_override)
            if coverage_override is not None
            else self._calculate_cogs(
                conn,
                rows,
                week_start,
            )
        )
        covered_net_revenue = _decimal(coverage["covered_net_revenue_rub"])
        uncovered_net_revenue = _decimal(coverage["uncovered_net_revenue_rub"])
        uncovered_sales_revenue = _decimal(
            coverage["uncovered_sales_revenue_rub"]
        )
        covered_sales_revenue = _decimal(coverage["covered_sales_revenue_rub"])
        profit_eligible = not (
            int(coverage["unmatched_units"]) > 0
            and int(coverage["matched_units"]) <= 0
        )
        before_cogs = (
            covered_net_revenue
            - profit_period_expenses
            + values["positive_adjustments"]
            if profit_eligible
            else None
        )
        # A partial canonical COGS is truthful when its revenue denominator is
        # restricted to the same covered sales.  Missing cost never becomes
        # zero and uncovered revenue never leaks into the profit numerator.
        cogs = (
            _decimal(coverage["partial_cogs_rub"])
            if profit_eligible
            else None
        )
        profit = (
            before_cogs - cogs
            if before_cogs is not None and cogs is not None
            else None
        )
        metrics: dict[str, Any] = {
            "sales_qty": int(values["sales_qty"]),
            "returns_qty": int(values["returns_qty"]),
            "net_sales_qty": int(values["sales_qty"] - values["returns_qty"]),
            "revenue_before_returns": _money_text(values["revenue_before_returns"]),
            "returns_amount": _money_text(values["returns_amount"]),
            "net_revenue": _money_text(net_revenue),
            "profit_revenue_covered": _money_text(covered_net_revenue),
            "profit_revenue_uncovered": _money_text(uncovered_net_revenue),
            "sales_without_cost_rub": _money_text(uncovered_sales_revenue),
            "orders_without_cost": int(coverage["uncovered_sales_order_count"]),
            "units_without_cost": int(coverage["uncovered_sales_units"]),
            "sales_cost_coverage_pct": coverage["sales_revenue_coverage_pct"],
            "profit_coverage_status": coverage["profit_coverage_status"],
            "agent_remuneration": _money_text(values["agent_remuneration"]),
            "commission": _money_text(values["agent_remuneration"]),
            "combined_commission_control": _money_text(values["combined_commission_control"]),
            "acquiring": _money_text(values["acquiring"]),
            "wb_remuneration_adjustment": _money_text(values["wb_remuneration_adjustment"]),
            "logistics": _money_text(values["logistics"]),
            "storage": _money_text(values["storage"]),
            "acceptance": _money_text(values["acceptance"]),
            "marketing": _money_text(values["marketing"]),
            "transit_logistics": _money_text(values["transit_logistics"]),
            "penalties": _money_text(values["penalties"]),
            "subscriptions": _money_text(values["subscriptions"]),
            "paid_services": _money_text(values["paid_services"]),
            "review_points": _money_text(values["review_points"]),
            "other_deductions": _money_text(values["other_deductions"]),
            "positive_adjustments": _money_text(values["positive_adjustments"]),
            "corrections": _money_text(values["corrections"]),
            "total_wb_expenses": _money_text(total_expenses),
            "wb_expenses_without_marketing": _money_text(
                total_expenses - values["marketing"]
            ),
            "wb_expenses_without_marketing_pct": _money_text(
                _ratio(total_expenses - values["marketing"], net_revenue)
            ),
            "profit_period_expenses": _money_text(profit_period_expenses),
            "capitalized_acceptance": _money_text(capitalized_acceptance),
            "capitalized_transit_logistics": _money_text(capitalized_transit),
            "to_seller": _money_text(values["to_seller"]),
            "before_cogs_profit": _money_text(before_cogs),
            "before_cogs_margin_pct": _money_text(
                _ratio(before_cogs, covered_net_revenue)
            ),
            "cogs": _money_text(cogs),
            "cogs_complete": coverage["unmatched_units"] == 0,
            "profit_after_cogs": _money_text(profit),
            "final_margin_pct": _money_text(
                _ratio(profit, covered_net_revenue)
            ),
            "commission_control_reconciliation_rub": _money_text(
                values["combined_commission_control"]
                - values["agent_remuneration"]
                - values["acquiring"]
            ),
            "acquiring_accounting_note": "separate_from_agent; agent_plus_acquiring_equals_combined_control",
            "acceptance_accounting_note": "addback_only_when_supply_cost_layer_lineage_is_matched_and_capped",
            "transit_accounting_note": "addback_only_when_supply_cost_layer_lineage_is_matched_and_capped",
            "capitalization_reconciliation": capitalization,
            "profit_method_version": PROFIT_METHOD_VERSION,
            "profit_semantics_complete": coverage["unmatched_units"] == 0,
        }
        return metrics, coverage, sorted(unknown)

    def _capitalization_reconciliation_legacy_deprecated(
        self,
        conn: sqlite3.Connection,
        rows: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Match Finance acceptance/transit only to proven canonical cost layers."""

        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sheet_vitrina_v1_wb_supply_cost_layers'"
        ).fetchone()
        alias_to_nm, ambiguous_aliases, _groups, _items = (
            self._nomenclature_identity_index(conn)
        )
        candidates: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in rows:
            nm_id, identity_method, identity_problem = _resolve_finance_nm_id(
                row,
                alias_to_nm=alias_to_nm,
                ambiguous_aliases=ambiguous_aliases,
            )
            supply_id = str(
                row.get("giId")
                or row.get("supplyId")
                or row.get("supplyID")
                or ""
            ).strip()
            amounts = (
                ("acceptance", max(_decimal(row.get("paidAcceptance")), ZERO)),
                (
                    "transit",
                    max(_decimal(row.get("deduction")), ZERO)
                    if classify_deduction(row) == "transit_logistics"
                    else ZERO,
                ),
            )
            for component, amount in amounts:
                if amount <= ZERO:
                    continue
                key = (component, supply_id, nm_id)
                item = candidates.setdefault(
                    key,
                    {
                        "component": component,
                        "wb_supply_id": supply_id,
                        "nm_id": nm_id,
                        "finance_amount_rub": ZERO,
                        "finance_row_count": 0,
                        "identity_method": identity_method,
                        "identity_problem": identity_problem,
                    },
                )
                item["finance_amount_rub"] += amount
                item["finance_row_count"] += 1
        lineage: list[dict[str, Any]] = []
        matched = {"acceptance": ZERO, "transit": ZERO}
        unmatched_count = 0
        for key in sorted(candidates):
            item = candidates[key]
            component, supply_id, nm_id = key
            layer = None
            if table is not None and supply_id and nm_id:
                layer = conn.execute(
                    """SELECT wb_supply_cost_layer_id,wb_supply_id,nm_id,accepted_qty,
                              transit_cost_status,transit_amount_total,
                              wb_acceptance_amount_total,source_status,component_status_json,
                              inputs_hash,version
                       FROM sheet_vitrina_v1_wb_supply_cost_layers
                       WHERE is_current=1 AND wb_supply_id=? AND nm_id=?""",
                    (supply_id, nm_id),
                ).fetchone()
            canonical_amount = ZERO
            reason = ""
            if layer is None:
                reason = (
                    "finance_supply_id_missing"
                    if not supply_id
                    else "finance_nm_id_unresolved"
                    if not nm_id
                    else "canonical_supply_cost_layer_missing"
                )
            else:
                raw_capitalized = (
                    layer["wb_acceptance_amount_total"]
                    if component == "acceptance"
                    else layer["transit_amount_total"]
                )
                canonical_amount = max(_decimal(raw_capitalized), ZERO)
                if not str(layer["inputs_hash"] or ""):
                    reason = "canonical_supply_cost_layer_missing_fingerprint"
                elif component == "transit" and str(layer["transit_cost_status"] or "") not in {
                    "confirmed",
                    "seller_portal_confirmed",
                    "official_confirmed",
                }:
                    reason = "canonical_transit_not_confirmed"
                elif canonical_amount <= ZERO:
                    reason = "canonical_capitalized_amount_not_positive"
            finance_amount = item["finance_amount_rub"]
            addback = min(finance_amount, canonical_amount) if not reason else ZERO
            matched[component] += addback
            residual = finance_amount - addback
            if residual > ZERO:
                unmatched_count += int(item["finance_row_count"])
            lineage.append(
                {
                    "component": component,
                    "wb_supply_id": supply_id,
                    "nm_id": nm_id,
                    "finance_amount_rub": _money_text(finance_amount),
                    "canonical_capitalized_amount_rub": _money_text(canonical_amount),
                    "addback_rub": _money_text(addback),
                    "unmatched_period_expense_rub": _money_text(residual),
                    "reason": reason or ("matched_and_capped" if residual == ZERO else "matched_but_capped"),
                    "canonical_layer_id": str(layer["wb_supply_cost_layer_id"] or "") if layer else "",
                    "canonical_layer_version": int(layer["version"] or 0) if layer else 0,
                    "canonical_inputs_hash": str(layer["inputs_hash"] or "") if layer else "",
                    "identity_method": item["identity_method"],
                    "identity_problem": item["identity_problem"],
                }
            )
        return {
            "status": "warning" if unmatched_count else "ok",
            "matched_acceptance_rub": _money_text(matched["acceptance"]),
            "matched_transit_rub": _money_text(matched["transit"]),
            "unmatched_expense_count": unmatched_count,
            "lineage": lineage,
            "policy": "exact Finance giId + canonical nmId; addback capped by current canonical cost layer",
        }

    def _capitalization_reconciliation(
        self,
        conn: sqlite3.Connection,
        rows: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Allocate each canonical supply-layer amount at most once globally."""

        allocations = self._global_capitalization_allocations(conn)
        selected: list[dict[str, Any]] = []
        for row in rows:
            report_id = str(row.get("reportId") or "")
            rrd_id = str(row.get("rrdId") or "")
            for component in ("acceptance", "transit"):
                item = allocations.get((report_id, rrd_id, component))
                if item is not None:
                    selected.append(item)
        grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        matched = {"acceptance": ZERO, "transit": ZERO}
        unmatched_count = 0
        for item in selected:
            component = str(item["component"])
            finance_amount = _decimal(item["finance_amount_rub"])
            addback = _decimal(item["addback_rub"])
            residual = finance_amount - addback
            matched[component] += addback
            if residual > ZERO:
                unmatched_count += 1
            key = (
                component,
                str(item["wb_supply_id"]),
                str(item["nm_id"]),
                str(item["canonical_layer_id"]),
            )
            group = grouped.setdefault(
                key,
                {
                    "component": component,
                    "wb_supply_id": str(item["wb_supply_id"]),
                    "nm_id": str(item["nm_id"]),
                    "finance_amount_rub": ZERO,
                    "canonical_capitalized_amount_rub": str(
                        item["canonical_capitalized_amount_rub"]
                    ),
                    "global_finance_amount_rub": str(item["global_finance_amount_rub"]),
                    "addback_rub": ZERO,
                    "unmatched_period_expense_rub": ZERO,
                    "reasons": set(),
                    "canonical_layer_id": str(item["canonical_layer_id"]),
                    "canonical_layer_version": int(item["canonical_layer_version"]),
                    "canonical_inputs_hash": str(item["canonical_inputs_hash"]),
                    "finance_row_count": 0,
                },
            )
            group["finance_amount_rub"] += finance_amount
            group["addback_rub"] += addback
            group["unmatched_period_expense_rub"] += residual
            group["reasons"].add(str(item["reason"]))
            group["finance_row_count"] += 1
        lineage = []
        for key in sorted(grouped):
            item = grouped[key]
            lineage.append(
                {
                    **{
                        field: item[field]
                        for field in (
                            "component",
                            "wb_supply_id",
                            "nm_id",
                            "canonical_capitalized_amount_rub",
                            "global_finance_amount_rub",
                            "canonical_layer_id",
                            "canonical_layer_version",
                            "canonical_inputs_hash",
                            "finance_row_count",
                        )
                    },
                    "finance_amount_rub": _money_text(item["finance_amount_rub"]),
                    "addback_rub": _money_text(item["addback_rub"]),
                    "unmatched_period_expense_rub": _money_text(
                        item["unmatched_period_expense_rub"]
                    ),
                    "reason": ",".join(sorted(item["reasons"])),
                }
            )
        return {
            "status": "warning" if unmatched_count else "ok",
            "matched_acceptance_rub": _money_text(matched["acceptance"]),
            "matched_transit_rub": _money_text(matched["transit"]),
            "unmatched_expense_count": unmatched_count,
            "lineage": lineage,
            "policy": (
                "exact Finance giId + canonical nmId; each current cost-layer amount "
                "is allocated chronologically across all Finance rows and capped globally"
            ),
        }

    def _global_capitalization_allocations(
        self,
        conn: sqlite3.Connection,
    ) -> dict[tuple[str, str, str], dict[str, Any]]:
        # A plan/apply/readback uses one coherent connection while calculating
        # the global week and every per-SKU projection. Raw Finance rows and
        # canonical supply layers are source tables and are not mutated inside
        # that connection. Re-reading and re-hashing the complete layer manifest
        # for every aggregate was an accidental O(weeks × SKUs × layers) path.
        # A new connection always rebuilds the source-bound cache, so ordinary
        # ingestion or a later canonical-layer correction cannot reuse it.
        if self._capitalization_cache_connection is conn:
            return self._capitalization_cache
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        sync_manifest = (
            [
                list(row)
                for row in conn.execute(
                    """SELECT week_start,week_end,content_hash FROM wb_finance_weekly_sync
                       WHERE seller_id=? ORDER BY week_start,week_end""",
                    (self.seller_id,),
                ).fetchall()
            ]
            if "wb_finance_weekly_sync" in tables
            else []
        )
        layer_manifest: list[list[Any]] = []
        if "sheet_vitrina_v1_wb_supply_cost_layers" in tables:
            layer_manifest = [
                list(row)
                for row in conn.execute(
                    """SELECT wb_supply_cost_layer_id,wb_supply_id,nm_id,
                              transit_cost_status,transit_amount_total,
                              wb_acceptance_amount_total,inputs_hash,version,is_current
                       FROM sheet_vitrina_v1_wb_supply_cost_layers
                       ORDER BY wb_supply_cost_layer_id,version"""
                ).fetchall()
            ]
        cache_key = self._json_digest(
            {"sync": sync_manifest, "cost_layers": layer_manifest}
        )
        if cache_key == self._capitalization_cache_key:
            return self._capitalization_cache

        alias_to_nm, ambiguous_aliases, _groups, _items = (
            self._nomenclature_identity_index(conn)
        )
        candidates: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        raw_rows = conn.execute(
            """SELECT report_id,rrd_id,week_start,raw_json
               FROM wb_finance_weekly_raw_rows WHERE seller_id=?
               ORDER BY week_start,report_id,rrd_id""",
            (self.seller_id,),
        )
        for stored in raw_rows:
            raw = json.loads(str(stored["raw_json"] or "{}"))
            nm_id, identity_method, identity_problem = _resolve_finance_nm_id(
                raw,
                alias_to_nm=alias_to_nm,
                ambiguous_aliases=ambiguous_aliases,
            )
            supply_id = str(
                raw.get("giId") or raw.get("supplyId") or raw.get("supplyID") or ""
            ).strip()
            operation_day, _operation_source = _operation_date(
                raw, date.fromisoformat(str(stored["week_start"]))
            )
            amounts = (
                ("acceptance", max(_decimal(raw.get("paidAcceptance")), ZERO)),
                (
                    "transit",
                    max(_decimal(raw.get("deduction")), ZERO)
                    if classify_deduction(raw) == "transit_logistics"
                    else ZERO,
                ),
            )
            for component, amount in amounts:
                if amount <= ZERO:
                    continue
                candidates.setdefault((component, supply_id, nm_id), []).append(
                    {
                        "component": component,
                        "report_id": str(stored["report_id"]),
                        "rrd_id": str(stored["rrd_id"]),
                        "week_start": str(stored["week_start"]),
                        "operation_date": operation_day.isoformat(),
                        "wb_supply_id": supply_id,
                        "nm_id": nm_id,
                        "finance_amount_rub": amount,
                        "identity_method": identity_method,
                        "identity_problem": identity_problem,
                    }
                )

        allocations: dict[tuple[str, str, str], dict[str, Any]] = {}
        for key in sorted(candidates):
            component, supply_id, nm_id = key
            items = sorted(
                candidates[key],
                key=lambda item: (
                    item["operation_date"],
                    item["week_start"],
                    item["report_id"],
                    item["rrd_id"],
                ),
            )
            layer = None
            if "sheet_vitrina_v1_wb_supply_cost_layers" in tables and supply_id and nm_id:
                layer = conn.execute(
                    """SELECT wb_supply_cost_layer_id,transit_cost_status,
                              transit_amount_total,wb_acceptance_amount_total,
                              inputs_hash,version
                       FROM sheet_vitrina_v1_wb_supply_cost_layers
                       WHERE is_current=1 AND wb_supply_id=? AND nm_id=?""",
                    (supply_id, nm_id),
                ).fetchone()
            reason = ""
            canonical_amount = ZERO
            if layer is None:
                reason = (
                    "finance_supply_id_missing"
                    if not supply_id
                    else "finance_nm_id_unresolved"
                    if not nm_id
                    else "canonical_supply_cost_layer_missing"
                )
            else:
                canonical_amount = max(
                    _decimal(
                        layer["wb_acceptance_amount_total"]
                        if component == "acceptance"
                        else layer["transit_amount_total"]
                    ),
                    ZERO,
                )
                if not str(layer["inputs_hash"] or ""):
                    reason = "canonical_supply_cost_layer_missing_fingerprint"
                elif component == "transit" and str(layer["transit_cost_status"] or "") not in {
                    "confirmed",
                    "seller_portal_confirmed",
                    "official_confirmed",
                }:
                    reason = "canonical_transit_not_confirmed"
                elif canonical_amount <= ZERO:
                    reason = "canonical_capitalized_amount_not_positive"
            remaining = canonical_amount if not reason else ZERO
            global_finance_amount = sum(
                (item["finance_amount_rub"] for item in items), ZERO
            )
            for item in items:
                amount = item["finance_amount_rub"]
                addback = min(amount, remaining)
                remaining -= addback
                residual = amount - addback
                allocations[(item["report_id"], item["rrd_id"], component)] = {
                    **item,
                    "finance_amount_rub": _money_text(amount),
                    "global_finance_amount_rub": _money_text(global_finance_amount),
                    "canonical_capitalized_amount_rub": _money_text(canonical_amount),
                    "addback_rub": _money_text(addback),
                    "reason": reason
                    or (
                        "matched_with_global_cap"
                        if residual == ZERO
                        else "matched_but_global_cap_exhausted_or_capped"
                    ),
                    "canonical_layer_id": str(layer["wb_supply_cost_layer_id"] or "") if layer else "",
                    "canonical_layer_version": int(layer["version"] or 0) if layer else 0,
                    "canonical_inputs_hash": str(layer["inputs_hash"] or "") if layer else "",
                }
        self._capitalization_cache_key = cache_key
        self._capitalization_cache = allocations
        self._capitalization_cache_connection = conn
        return allocations

    def _load_retro_cost_map(
        self, conn: sqlite3.Connection
    ) -> dict[str, dict[str, Any]]:
        rows = conn.execute(
            """SELECT * FROM wb_finance_retro_cost_map
               WHERE seller_id=? AND formula_version=? AND status='business_approved_retro'
               ORDER BY nm_id""",
            (self.seller_id, RETRO_COST_FORMULA_VERSION),
        ).fetchall()
        return {str(row["nm_id"]): dict(row) for row in rows}

    def _calculate_cogs_legacy_deprecated(
        self,
        conn: sqlite3.Connection,
        rows: list[dict[str, Any]],
        week_start: date,
        *,
        retro_cost_map: Mapping[str, Mapping[str, Any]] | None = None,
        include_details: bool = False,
    ) -> dict[str, Any]:
        group_by_nm = {
            str(row[0]): str(row[1])
            for row in conn.execute(
                """SELECT nm_id,group_name FROM registry_upload_config_v2
            WHERE bundle_version=(SELECT bundle_version FROM registry_upload_current_state WHERE slot=1)"""
            )
        }
        (
            alias_to_nm,
            ambiguous_aliases,
            nomenclature_group_by_nm,
            _nomenclature_items,
        ) = _nomenclature_identity_index(conn)
        cost_rows = conn.execute(
            """SELECT group_name,cost_price_rub,effective_from FROM cost_price_upload_rows
            WHERE dataset_version=(SELECT dataset_version FROM cost_price_current_state WHERE slot=1)
            ORDER BY group_name,effective_from"""
        ).fetchall()
        costs: dict[str, list[tuple[date, Decimal]]] = {}
        for row in cost_rows:
            costs.setdefault(str(row["group_name"]), []).append(
                (
                    date.fromisoformat(row["effective_from"]),
                    _decimal(row["cost_price_rub"]),
                )
            )
        daily_state_available = (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sheet_vitrina_v1_wb_cost_daily_state'"
            ).fetchone()
            is not None
        )
        daily_state_cache: dict[tuple[str, str], Mapping[str, Any] | None] = {}
        if retro_cost_map is None:
            retro_cost_map = self._load_retro_cost_map(conn)
        cogs = ZERO
        matched_movements: dict[str, dict[str, Any]] = {}
        problems: dict[str, int] = {}
        problem_gross_units: dict[str, int] = {}
        problem_meta: dict[str, dict[str, Any]] = {}
        dependency_evidence: set[str] = set()
        detail_rows: list[dict[str, Any]] = []
        operation_date_fallback_rows = 0
        operation_date_fallback_units = 0
        for row in rows:
            doc = str(row.get("docTypeName") or "").casefold()
            if doc not in {"продажа", "возврат"}:
                continue
            sign = 1 if doc == "продажа" else -1
            raw_qty = int(_decimal(row.get("quantity")))
            if raw_qty == 0:
                continue
            qty = raw_qty * sign
            raw_keys = [
                str(row.get("nmId") or ""),
                str(row.get("vendorCode") or ""),
                str(row.get("sku") or ""),
            ]
            internal_nm, identity_method, identity_problem = _resolve_finance_nm_id(
                row,
                alias_to_nm=alias_to_nm,
                ambiguous_aliases=ambiguous_aliases,
            )
            group = group_by_nm.get(internal_nm) or nomenclature_group_by_nm.get(
                internal_nm, ""
            )
            operation_date, operation_date_source = _operation_date(row, week_start)
            if operation_date_source == "week_start_fallback":
                operation_date_fallback_rows += 1
                operation_date_fallback_units += abs(qty)
            identity_key = (
                internal_nm
                or raw_keys[1]
                or raw_keys[2]
                or str(row.get("srid") or "")
                or str(row.get("orderUid") or "")
                or str(row.get("shkId") or "")
                or "unknown"
            )
            if (
                operation_date_source == "week_start_fallback"
                and week_start + timedelta(days=6) >= RETRO_COST_PERIOD_START
            ):
                source = "operation_date_missing"
                movement_key = f"{source}|{identity_key}"
                missing_reason = "operation_date_missing"
                dependency = {
                    "source": source,
                    "operation_date": "",
                    "operation_date_source": operation_date_source,
                    "raw_keys": raw_keys,
                    "internal_nm": internal_nm,
                    "identity_method": identity_method,
                    "identity_problem": identity_problem,
                    "group": group,
                    "missing": missing_reason,
                }
                dependency_evidence.add(
                    json.dumps(
                        dependency,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                problems[movement_key] = problems.get(movement_key, 0) + qty
                problem_gross_units[movement_key] = (
                    problem_gross_units.get(movement_key, 0) + abs(qty)
                )
                problem_meta[movement_key] = {
                    "sku": identity_key,
                    "operation_date": "",
                    "source": source,
                    "reason": missing_reason,
                    "operation_date_source": operation_date_source,
                }
                continue
            source = (
                "cost_price"
                if operation_date < RETRO_COST_PERIOD_START
                else "business_approved_retro"
                if operation_date <= RETRO_COST_PERIOD_END
                else "our_wb_cost_daily_state"
            )
            movement_key = (
                f"{source}|{identity_key}"
                if source == "cost_price"
                else f"{source}|{identity_key}|{operation_date.isoformat()}"
            )
            selected_cost: Decimal | None = None
            selected_state: Mapping[str, Any] | None = None
            quality_shares = (ZERO, ZERO, ZERO)
            missing_reason = ""
            dependency: dict[str, Any] = {
                "source": source,
                "operation_date": operation_date.isoformat(),
                "operation_date_source": operation_date_source,
                "raw_keys": raw_keys,
                "internal_nm": internal_nm,
                "identity_method": identity_method,
                "identity_problem": identity_problem,
                "group": group,
            }
            if source == "cost_price":
                candidates = [
                    (effective, cost)
                    for effective, cost in costs.get(group, [])
                    if effective <= operation_date
                ]
                if candidates:
                    effective, selected_cost = candidates[-1]
                    dependency.update(
                        {
                            "effective_from": effective.isoformat(),
                            "unit_cost_rub": _money_text(selected_cost),
                        }
                    )
                else:
                    missing_reason = "cost_price_missing"
                    dependency["missing"] = missing_reason
            elif source == "business_approved_retro":
                selected_retro = retro_cost_map.get(internal_nm) if internal_nm else None
                if selected_retro is not None:
                    selected_cost = _decimal(selected_retro.get("unit_cost_rub"))
                    dependency.update(
                        {
                            "unit_cost_rub": _money_text(selected_cost),
                            "source_date": str(selected_retro.get("source_date") or ""),
                            "source_table": str(selected_retro.get("source_table") or ""),
                            "source_row_sha256": str(selected_retro.get("source_row_sha256") or ""),
                            "source_calculation_fingerprint": str(
                                selected_retro.get("source_calculation_fingerprint") or ""
                            ),
                            "selection_method": str(selected_retro.get("selection_method") or ""),
                            "formula_version": str(selected_retro.get("formula_version") or ""),
                            "status": str(selected_retro.get("status") or ""),
                        }
                    )
                    if selected_cost <= ZERO:
                        selected_cost = None
                        missing_reason = "retro_cost_non_positive"
                        dependency["missing"] = missing_reason
                else:
                    missing_reason = (
                        "finance_identity_ambiguous"
                        if identity_method == "ambiguous_alias"
                        else "finance_identity_unresolved"
                        if not internal_nm
                        else "business_approved_retro_cost_missing"
                    )
                    dependency["missing"] = missing_reason
            else:
                cache_key = (operation_date.isoformat(), internal_nm)
                if cache_key not in daily_state_cache:
                    functional_state, functional_applies = _functional_wb_cost_state(
                        conn,
                        as_of_date=cache_key[0],
                        nm_id=cache_key[1],
                    )
                    if functional_applies:
                        daily_state_cache[cache_key] = functional_state
                    else:
                        daily_state_cache[cache_key] = (
                            conn.execute(
                                """SELECT * FROM sheet_vitrina_v1_wb_cost_daily_state
                                WHERE as_of_date=? AND nm_id=?""",
                                cache_key,
                            ).fetchone()
                            if daily_state_available and internal_nm
                            else None
                        )
                selected_state = daily_state_cache[cache_key]
                if selected_state is not None:
                    raw_unit_cost = selected_state["our_wb_unit_cost_rub"]
                    selected_cost = (
                        _decimal(raw_unit_cost) if raw_unit_cost is not None else None
                    )
                    confirmed_qty = max(_decimal(selected_state["confirmed_qty"]), ZERO)
                    estimated_qty = max(_decimal(selected_state["estimated_qty"]), ZERO)
                    fallback_qty = max(_decimal(selected_state["fallback_qty"]), ZERO)
                    bucket_total = confirmed_qty + estimated_qty + fallback_qty
                    if bucket_total > ZERO:
                        quality_shares = (
                            confirmed_qty / bucket_total,
                            estimated_qty / bucket_total,
                            fallback_qty / bucket_total,
                        )
                    else:
                        state_status = str(selected_state["source_status"] or "")
                        quality_shares = (
                            (Decimal("1"), ZERO, ZERO)
                            if state_status == "confirmed"
                            else (ZERO, ZERO, Decimal("1"))
                            if state_status == "fallback"
                            else (ZERO, Decimal("1"), ZERO)
                        )
                    dependency.update(
                        {
                            "unit_cost_rub": _money_text(selected_cost),
                            "confirmed_qty": _money_text(confirmed_qty),
                            "estimated_qty": _money_text(estimated_qty),
                            "fallback_qty": _money_text(fallback_qty),
                            "confirmed_share_pct": str(
                                selected_state["confirmed_share_pct"]
                            ),
                            "source_status": str(selected_state["source_status"] or ""),
                            "component_status_json": str(
                                selected_state["component_status_json"] or "{}"
                            ),
                            "inputs_hash": str(selected_state["inputs_hash"] or ""),
                        }
                    )
                    if selected_cost is None:
                        missing_reason = "our_wb_unit_cost_missing"
                        dependency["missing"] = missing_reason
                else:
                    missing_reason = "our_wb_daily_state_missing"
                    dependency["missing"] = missing_reason
            dependency_evidence.add(
                json.dumps(
                    dependency,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            if selected_cost is None:
                problems[movement_key] = problems.get(movement_key, 0) + qty
                problem_gross_units[movement_key] = (
                    problem_gross_units.get(movement_key, 0) + abs(qty)
                )
                problem_meta[movement_key] = {
                    "sku": identity_key,
                    "operation_date": operation_date.isoformat(),
                    "source": source,
                    "reason": missing_reason,
                    "operation_date_source": operation_date_source,
                }
                continue
            movement = matched_movements.setdefault(
                movement_key,
                {
                    "net_units": 0,
                    "gross_units": 0,
                    "source": source,
                    "quality_shares": quality_shares,
                },
            )
            movement["net_units"] = int(movement["net_units"]) + qty
            movement["gross_units"] = int(movement["gross_units"]) + abs(qty)
            cogs += Decimal(qty) * selected_cost
            if include_details:
                detail_rows.append(
                    {
                        "report_id": str(row.get("reportId") or ""),
                        "rrd_id": str(row.get("rrdId") or ""),
                        "order_identity_digest": "sha256:"
                        + hashlib.sha256(order_identity.encode("utf-8")).hexdigest(),
                        "nm_id": internal_nm,
                        "operation_date": operation_date.isoformat(),
                        "operation_date_source": operation_date_source,
                        "movement": "sale" if sign > 0 else "return",
                        "quantity": abs(qty),
                        "signed_quantity": qty,
                        "unit_cost_rub": _money_text(selected_cost),
                        "cost_source": source,
                        "source_date": str(dependency.get("source_date") or dependency.get("effective_from") or operation_date.isoformat()),
                        "formula_version": COST_METHOD_VERSION,
                        "signed_cogs_rub": _money_text(Decimal(qty) * selected_cost),
                    }
                )
        matched = sum(
            int(movement["gross_units"]) for movement in matched_movements.values()
        )
        unmatched = sum(problem_gross_units.values())
        denominator = matched + unmatched
        coverage_pct = (
            Decimal(matched) / Decimal(denominator) * Decimal("100")
            if denominator
            else None
        )
        source_units = {
            "cost_price": 0,
            "business_approved_retro": 0,
            "our_wb_cost_daily_state": 0,
        }
        confirmed_units = ZERO
        estimated_units = ZERO
        fallback_units = ZERO
        for movement in matched_movements.values():
            units = int(movement["gross_units"])
            source_name = str(movement["source"])
            source_units[source_name] += units
            if source_name != "our_wb_cost_daily_state":
                continue
            confirmed_share, estimated_share, fallback_share = movement[
                "quality_shares"
            ]
            decimal_units = Decimal(units)
            confirmed_units += decimal_units * confirmed_share
            estimated_units += decimal_units * estimated_share
            fallback_units += decimal_units * fallback_share
        our_wb_units = source_units["our_wb_cost_daily_state"]
        confirmed_share_pct = (
            confirmed_units / Decimal(our_wb_units) * Decimal("100")
            if our_wb_units
            else None
        )
        quality = {
            "cost_method_version": COST_METHOD_VERSION,
            "cutover_date": OUR_WB_COST_OPENING_DATE,
            "source_units": source_units,
            "confirmed_units": _money_text(confirmed_units),
            "estimated_units": _money_text(estimated_units),
            "fallback_units": _money_text(fallback_units),
            "estimated_fallback_units": _money_text(estimated_units + fallback_units),
            "business_approved_retro_units": source_units["business_approved_retro"],
            "confirmed_share_pct": _money_text(confirmed_share_pct),
            "operation_date_fallback_rows": operation_date_fallback_rows,
            "operation_date_fallback_units": operation_date_fallback_units,
        }
        cost_state_hash = hashlib.sha256(
            json.dumps(
                {
                    "cost_method_version": COST_METHOD_VERSION,
                    "dependencies": sorted(dependency_evidence),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {
            "matched_units": matched,
            "unmatched_units": unmatched,
            "coverage_pct": _money_text(coverage_pct),
            "cogs_rub": _money_text(cogs) if unmatched == 0 else None,
            "partial_cogs_rub": _money_text(cogs),
            "problem_skus": [
                {
                    **problem_meta[key],
                    "net_units": problems[key],
                    "unmatched_units": gross_units,
                }
                for key, gross_units in sorted(problem_gross_units.items())
            ],
            "quality": quality,
            "cost_state_hash": cost_state_hash,
            "detail_rows": detail_rows if include_details else [],
        }

    def _nomenclature_identity_index(
        self, conn: sqlite3.Connection
    ) -> tuple[dict[str, str], set[str], dict[str, str], dict[str, dict[str, Any]]]:
        if self._nomenclature_cache_connection is not conn:
            self._nomenclature_cache = _nomenclature_identity_index(conn)
            self._nomenclature_cache_connection = conn
        return self._nomenclature_cache

    def _resolve_canonical_cost(
        self,
        conn: sqlite3.Connection,
        *,
        nm_id: str,
        operation_date: date,
        operation: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot = self._canonical_channel_snapshot(conn)
        raw = dict(operation or {})
        channel_classification = classify_finance_channel(
            snapshot,
            operation=raw,
        )
        cache_key = (
            str(nm_id),
            operation_date.isoformat(),
            channel_classification,
        )
        cached = self._canonical_cost_resolution_cache.get(cache_key)
        if cached is None:
            cached = resolve_channel_location_cost(
                conn,
                nm_id=nm_id,
                operation_date=operation_date,
                operation=raw,
                snapshot=snapshot,
            )
            self._canonical_cost_resolution_cache[cache_key] = cached
        return cached

    def _canonical_channel_snapshot(
        self, conn: sqlite3.Connection
    ) -> CanonicalChannelCostSnapshot:
        if self._canonical_cost_snapshot_connection is not conn:
            self._canonical_cost_snapshot = CanonicalChannelCostSnapshot.from_connection(conn)
            self._canonical_cost_snapshot_connection = conn
            self._canonical_cost_resolution_cache = {}
        if self._canonical_cost_snapshot is None:
            raise RuntimeError("canonical channel cost snapshot did not initialize")
        return self._canonical_cost_snapshot

    def _calculate_cogs(
        self,
        conn: sqlite3.Connection,
        rows: list[dict[str, Any]],
        week_start: date,
        *,
        include_details: bool = False,
    ) -> dict[str, Any]:
        """Calculate signed COGS only through the shared canonical resolver."""

        alias_to_nm, ambiguous_aliases, _groups, _items = (
            self._nomenclature_identity_index(conn)
        )
        cogs = ZERO
        matched_units = 0
        unmatched_units = 0
        problem_rows: dict[tuple[str, ...], dict[str, Any]] = {}
        detail_rows: list[dict[str, Any]] = []
        dependency_digest = _StreamingCostDependencyDigest()
        source_units = {
            "projected_from_2026_07_01": 0,
            "canonical_exact_date": 0,
            "fbs_pooled_physical": 0,
            "fbs_same_day_common_inventory_fallback": 0,
        }
        covered_sales_rub = ZERO
        uncovered_sales_rub = ZERO
        covered_returns_rub = ZERO
        uncovered_returns_rub = ZERO
        covered_sales_orders: set[str] = set()
        uncovered_sales_orders: set[str] = set()
        uncovered_fbs_sales_rub = ZERO
        uncovered_fbs_sales_orders: set[str] = set()
        uncovered_fbs_sales_units = 0
        covered_sales_units = 0
        uncovered_sales_units = 0
        covered_sales_cogs_rub = ZERO
        covered_returns_cogs_rub = ZERO
        daily_coverage: dict[str, dict[str, Any]] = {}
        operation_date_fallback_rows = 0
        operation_date_fallback_units = 0
        for row in rows:
            doc = str(row.get("docTypeName") or "").casefold()
            if doc not in {"продажа", "возврат"}:
                continue
            raw_qty = int(_decimal(row.get("quantity")))
            if raw_qty == 0:
                continue
            sign = 1 if doc == "продажа" else -1
            signed_qty = raw_qty * sign
            gross_qty = abs(signed_qty)
            nm_id, identity_method, identity_problem = _resolve_finance_nm_id(
                row,
                alias_to_nm=alias_to_nm,
                ambiguous_aliases=ambiguous_aliases,
            )
            operation_date, operation_date_source = _operation_date(row, week_start)
            operation_day = (
                operation_date.isoformat()
                if operation_date_source != "week_start_fallback"
                else ""
            )
            revenue = _decimal(row.get("retailPriceWithDisc"))
            order_identity = _finance_sale_identity(row)
            channel_classification = classify_finance_channel(
                self._canonical_channel_snapshot(conn),
                operation=row,
            )
            classified_channel = (
                "FBS" if channel_classification.startswith("fbs_") else "WB"
            )
            daily = daily_coverage.setdefault(
                operation_day,
                {
                    "operation_date": operation_day,
                    "sales_revenue_rub": ZERO,
                    "returns_revenue_rub": ZERO,
                    "covered_sales_revenue_rub": ZERO,
                    "uncovered_sales_revenue_rub": ZERO,
                    "covered_returns_revenue_rub": ZERO,
                    "uncovered_returns_revenue_rub": ZERO,
                    "covered_sales_cogs_rub": ZERO,
                    "covered_returns_cogs_rub": ZERO,
                    "sales_units": 0,
                    "covered_sales_units": 0,
                    "uncovered_sales_units": 0,
                    "sales_order_ids": set(),
                    "covered_sales_order_ids": set(),
                    "uncovered_sales_order_ids": set(),
                    "reason_counts": {},
                },
            )
            if sign > 0:
                daily["sales_revenue_rub"] += revenue
                daily["sales_units"] += gross_qty
                daily["sales_order_ids"].add(order_identity)
            else:
                daily["returns_revenue_rub"] += revenue
            if operation_date_source == "week_start_fallback":
                operation_date_fallback_rows += 1
                operation_date_fallback_units += gross_qty
                resolution = {
                    "status": "missing",
                    "reason": "operation_date_missing",
                    "nm_id": nm_id,
                    "operation_date": "",
                    "canonical_source_date": "",
                    "selection_method": "",
                    "formula_version": COST_METHOD_VERSION,
                    "channel": classified_channel,
                    "pool": "FBS" if classified_channel == "FBS" else "FBO",
                    "facility_id": "",
                    "channel_classification": channel_classification,
                }
            elif not nm_id:
                resolution = {
                    "status": "missing",
                    "reason": (
                        "finance_identity_ambiguous"
                        if identity_method == "ambiguous_alias"
                        else "finance_identity_unresolved"
                    ),
                    "nm_id": "",
                    "operation_date": operation_date.isoformat(),
                    "canonical_source_date": "",
                    "selection_method": "",
                    "formula_version": COST_METHOD_VERSION,
                    "channel": classified_channel,
                    "pool": "FBS" if classified_channel == "FBS" else "FBO",
                    "facility_id": "",
                    "channel_classification": channel_classification,
                }
            else:
                resolution = self._resolve_canonical_cost(
                    conn,
                    nm_id=nm_id,
                    operation_date=operation_date,
                    operation=row,
                )
            dependency = {
                **resolution,
                "report_id": str(row.get("reportId") or ""),
                "rrd_id": str(row.get("rrdId") or ""),
                "identity_method": identity_method,
                "identity_problem": identity_problem,
                "operation_date_source": operation_date_source,
            }
            dependency_digest.add(
                {
                    key: dependency.get(key)
                    for key in (
                        "report_id",
                        "rrd_id",
                        "nm_id",
                        "operation_date",
                        "canonical_source_date",
                        "canonical_source_identity",
                        "source_digest",
                        "quality",
                        "selection_method",
                        "channel",
                        "facility_id",
                        "pool",
                        "fbs_order_id",
                        "status",
                        "reason",
                    )
                }
            )
            if resolution.get("status") != "resolved":
                unmatched_units += gross_qty
                sku = nm_id or str(
                    row.get("nmId")
                    or row.get("vendorCode")
                    or row.get("sku")
                    or "unknown"
                )
                operation_day = (
                    operation_date.isoformat()
                    if operation_date_source != "week_start_fallback"
                    else ""
                )
                canonical_source_date = str(
                    resolution.get("canonical_source_date") or ""
                )
                reason = str(resolution.get("reason") or "canonical_cost_missing")
                reason_counts = daily["reason_counts"]
                reason_counts[reason] = int(reason_counts.get(reason) or 0) + 1
                if sign > 0:
                    uncovered_sales_rub += revenue
                    uncovered_sales_units += gross_qty
                    uncovered_sales_orders.add(order_identity)
                    if str(resolution.get("channel") or "") == "FBS":
                        uncovered_fbs_sales_rub += revenue
                        uncovered_fbs_sales_units += gross_qty
                        uncovered_fbs_sales_orders.add(order_identity)
                    daily["uncovered_sales_revenue_rub"] += revenue
                    daily["uncovered_sales_units"] += gross_qty
                    daily["uncovered_sales_order_ids"].add(order_identity)
                else:
                    uncovered_returns_rub += revenue
                    daily["uncovered_returns_revenue_rub"] += revenue
                problem_key = (
                    sku,
                    nm_id,
                    operation_day,
                    canonical_source_date,
                    reason,
                    operation_date_source,
                    str(resolution.get("channel") or ""),
                    str(resolution.get("facility_id") or ""),
                )
                problem = problem_rows.setdefault(
                    problem_key,
                    {
                        "sku": sku,
                        "nm_id": nm_id,
                        "operation_date": operation_day,
                        "source": "canonical_our_wb_cost",
                        "canonical_source_date": canonical_source_date,
                        "reason": reason,
                        "operation_date_source": operation_date_source,
                        "channel": str(resolution.get("channel") or ""),
                        "facility_id": str(resolution.get("facility_id") or ""),
                        "pool": str(resolution.get("pool") or ""),
                        "operation_count": 0,
                        "sales_qty": 0,
                        "returns_qty": 0,
                        "unmatched_units": 0,
                        "net_units": 0,
                    },
                )
                problem["operation_count"] += 1
                problem["sales_qty" if sign > 0 else "returns_qty"] += gross_qty
                problem["unmatched_units"] += gross_qty
                problem["net_units"] += signed_qty
                continue
            unit_cost = _decimal(resolution["unit_cost_rub"])
            signed_cogs = Decimal(signed_qty) * unit_cost
            cogs += signed_cogs
            matched_units += gross_qty
            if sign > 0:
                covered_sales_rub += revenue
                covered_sales_cogs_rub += signed_cogs
                covered_sales_units += gross_qty
                covered_sales_orders.add(order_identity)
                daily["covered_sales_revenue_rub"] += revenue
                daily["covered_sales_cogs_rub"] += signed_cogs
                daily["covered_sales_units"] += gross_qty
                daily["covered_sales_order_ids"].add(order_identity)
            else:
                covered_returns_rub += revenue
                covered_returns_cogs_rub += abs(signed_cogs)
                daily["covered_returns_revenue_rub"] += revenue
                daily["covered_returns_cogs_rub"] += abs(signed_cogs)
            source_key = (
                (
                    "fbs_same_day_common_inventory_fallback"
                    if str(resolution.get("quality") or "")
                    == "same_day_common_inventory_fallback"
                    else "fbs_pooled_physical"
                )
                if str(resolution.get("channel") or "") == "FBS"
                else "projected_from_2026_07_01"
                if operation_date < CANONICAL_COST_POLICY_DATE
                else "canonical_exact_date"
            )
            source_units[source_key] += gross_qty
            if include_details:
                detail_rows.append(
                    {
                        "report_id": str(row.get("reportId") or ""),
                        "rrd_id": str(row.get("rrdId") or ""),
                        "order_identity_digest": "sha256:"
                        + hashlib.sha256(order_identity.encode("utf-8")).hexdigest(),
                        "nm_id": nm_id,
                        "operation_date": operation_date.isoformat(),
                        "operation_date_source": operation_date_source,
                        "movement": "sale" if sign > 0 else "return",
                        "quantity": gross_qty,
                        "signed_quantity": signed_qty,
                        "unit_cost_rub": _money_text(unit_cost),
                        "cost_source": "canonical_our_wb_cost",
                        "channel": str(resolution.get("channel") or ""),
                        "facility_id": str(resolution.get("facility_id") or ""),
                        "pool": str(resolution.get("pool") or ""),
                        "fbs_order_id": int(resolution.get("fbs_order_id") or 0),
                        "source_date": str(resolution["canonical_source_date"]),
                        "source_identity": str(resolution["canonical_source_identity"]),
                        "source_digest": str(resolution["source_digest"]),
                        "source_quality": str(resolution["quality"]),
                        "projection_quality": str(resolution["projection_quality"]),
                        "selection_method": str(resolution["selection_method"]),
                        "formula_version": COST_METHOD_VERSION,
                        "signed_cogs_rub": _money_text(signed_cogs),
                        "sales_revenue_rub": (
                            _money_text(revenue) if sign > 0 else "0.0000"
                        ),
                    }
                )
        total_units = matched_units + unmatched_units
        coverage_pct = (
            Decimal(matched_units) / Decimal(total_units) * Decimal("100")
            if total_units
            else None
        )
        cost_state_hash = dependency_digest.finish()
        total_sales_rub = covered_sales_rub + uncovered_sales_rub
        sales_revenue_coverage_pct = (
            covered_sales_rub / total_sales_rub * Decimal("100")
            if total_sales_rub
            else None
        )
        daily_rows = []
        for operation_day in sorted(daily_coverage):
            item = daily_coverage[operation_day]
            sales_revenue = _decimal(item["sales_revenue_rub"])
            covered_revenue = _decimal(item["covered_sales_revenue_rub"])
            daily_rows.append(
                {
                    "operation_date": operation_day,
                    "sales_revenue_rub": _money_text(sales_revenue),
                    "returns_revenue_rub": _money_text(item["returns_revenue_rub"]),
                    "net_revenue_rub": _money_text(
                        sales_revenue - _decimal(item["returns_revenue_rub"])
                    ),
                    "covered_sales_revenue_rub": _money_text(covered_revenue),
                    "uncovered_sales_revenue_rub": _money_text(
                        item["uncovered_sales_revenue_rub"]
                    ),
                    "covered_returns_revenue_rub": _money_text(
                        item["covered_returns_revenue_rub"]
                    ),
                    "uncovered_returns_revenue_rub": _money_text(
                        item["uncovered_returns_revenue_rub"]
                    ),
                    "covered_sales_cogs_rub": _money_text(
                        item["covered_sales_cogs_rub"]
                    ),
                    "covered_returns_cogs_rub": _money_text(
                        item["covered_returns_cogs_rub"]
                    ),
                    "covered_net_revenue_rub": _money_text(
                        covered_revenue
                        - _decimal(item["covered_returns_revenue_rub"])
                    ),
                    "sales_units": int(item["sales_units"]),
                    "covered_sales_units": int(item["covered_sales_units"]),
                    "uncovered_sales_units": int(item["uncovered_sales_units"]),
                    "sales_order_count": len(item["sales_order_ids"]),
                    "covered_sales_order_count": len(
                        item["covered_sales_order_ids"]
                    ),
                    "uncovered_sales_order_count": len(
                        item["uncovered_sales_order_ids"]
                    ),
                    "sales_revenue_coverage_pct": _money_text(
                        covered_revenue / sales_revenue * Decimal("100")
                        if sales_revenue
                        else None
                    ),
                    "status": (
                        "partial"
                        if _decimal(item["uncovered_sales_revenue_rub"]) > ZERO
                        else "covered"
                    ),
                    "reason_counts": dict(sorted(item["reason_counts"].items())),
                }
            )
        quality = {
            "cost_method_version": COST_METHOD_VERSION,
            "policy_date": CANONICAL_COST_POLICY_DATE.isoformat(),
            "source_units": source_units,
            "projected_units": source_units["projected_from_2026_07_01"],
            "exact_units": source_units["canonical_exact_date"],
            "fbs_pooled_physical_units": source_units["fbs_pooled_physical"],
            "fbs_same_day_common_inventory_fallback_units": source_units[
                "fbs_same_day_common_inventory_fallback"
            ],
            "fallback_units": source_units[
                "fbs_same_day_common_inventory_fallback"
            ],
            "fallback_average_created": False,
            "silent_zero_created": False,
            "operation_date_fallback_rows": operation_date_fallback_rows,
            "operation_date_fallback_units": operation_date_fallback_units,
            "historical_projection_note": (
                "Operations before 2026-07-01 are a business-approved retrospective "
                "projection from the same SKU canonical cost on 2026-07-01."
            ),
        }
        return {
            "matched_units": matched_units,
            "unmatched_units": unmatched_units,
            "coverage_pct": _money_text(coverage_pct),
            "cogs_rub": _money_text(cogs) if unmatched_units == 0 else None,
            "partial_cogs_rub": _money_text(cogs),
            "covered_sales_revenue_rub": _money_text(covered_sales_rub),
            "uncovered_sales_revenue_rub": _money_text(uncovered_sales_rub),
            "covered_returns_revenue_rub": _money_text(covered_returns_rub),
            "uncovered_returns_revenue_rub": _money_text(uncovered_returns_rub),
            "covered_sales_cogs_rub": _money_text(covered_sales_cogs_rub),
            "covered_returns_cogs_rub": _money_text(covered_returns_cogs_rub),
            "covered_net_revenue_rub": _money_text(
                covered_sales_rub - covered_returns_rub
            ),
            "uncovered_net_revenue_rub": _money_text(
                uncovered_sales_rub - uncovered_returns_rub
            ),
            "sales_revenue_coverage_pct": _money_text(
                sales_revenue_coverage_pct
            ),
            "covered_sales_order_count": len(covered_sales_orders),
            "uncovered_sales_order_count": len(uncovered_sales_orders),
            "uncovered_fbs_sales_revenue_rub": _money_text(
                uncovered_fbs_sales_rub
            ),
            "uncovered_fbs_sales_order_count": len(
                uncovered_fbs_sales_orders
            ),
            "uncovered_fbs_sales_units": uncovered_fbs_sales_units,
            "covered_sales_units": covered_sales_units,
            "uncovered_sales_units": uncovered_sales_units,
            "profit_coverage_status": (
                "partial" if uncovered_sales_rub > ZERO else "complete"
            ),
            "daily_rows": daily_rows,
            "problem_skus": [problem_rows[key] for key in sorted(problem_rows)],
            "quality": quality,
            "cost_state_hash": cost_state_hash,
            "detail_rows": detail_rows if include_details else [],
        }

    def build_payload(self) -> dict[str, Any]:
        self.ensure_schema()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT s.week_start,s.week_end,a.metrics_json,a.report_ids_json,a.report_types_json,
                a.unknown_reasons_json,a.classifier_version,s.status,s.first_loaded_at,s.last_synced_at,
                s.report_count,s.raw_row_count,s.last_error,c.matched_units,c.unmatched_units,c.coverage_pct,
                c.problem_skus_json,c.quality_json,c.coverage_json,c.cost_state_hash,r.status reconciliation_status
                FROM wb_finance_weekly_sync s
                LEFT JOIN wb_finance_weekly_aggregates a USING(seller_id,week_start,week_end)
                LEFT JOIN wb_finance_weekly_cost_coverage c USING(seller_id,week_start,week_end)
                LEFT JOIN wb_finance_weekly_reconciliation r USING(seller_id,week_start,week_end)
                WHERE s.seller_id=? ORDER BY s.week_start""",
                (self.seller_id,),
            ).fetchall()
        weeks = []
        for row in rows:
            weeks.append(
                {
                    "week_start": row["week_start"],
                    "week_end": row["week_end"],
                    "status": row["status"],
                    "first_loaded_at": row["first_loaded_at"],
                    "last_synced_at": row["last_synced_at"],
                    "report_count": row["report_count"],
                    "raw_row_count": row["raw_row_count"],
                    "report_ids": json.loads(row["report_ids_json"] or "[]"),
                    "report_types": json.loads(row["report_types_json"] or "[]"),
                    "metrics": json.loads(row["metrics_json"] or "{}"),
                    "classifier_version": row["classifier_version"]
                    or CLASSIFIER_VERSION,
                    "unknown_reasons": json.loads(row["unknown_reasons_json"] or "[]"),
                    "last_error": row["last_error"],
                    "cost_coverage": (
                        json.loads(row["coverage_json"] or "{}")
                        or {
                            "matched_units": row["matched_units"],
                            "unmatched_units": row["unmatched_units"],
                            "coverage_pct": row["coverage_pct"],
                            "problem_skus": json.loads(row["problem_skus_json"] or "[]"),
                            "quality": json.loads(row["quality_json"] or "{}"),
                            "cost_state_hash": row["cost_state_hash"] or "",
                        }
                    ),
                    "reconciliation_status": row["reconciliation_status"] or "pending",
                }
            )
        return {
            "status": "ok",
            "contract_version": "wb_finance_weekly_v1",
            "weeks": weeks,
            "week_count": len(weeks),
            "classifier_version": CLASSIFIER_VERSION,
            "storage_health": storage_health(self.store_registry),
            "generated_at": self.now_factory()
            .astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }

    def run_backfill(
        self,
        client: WbFinanceApiClient,
        *,
        today: date | None = None,
        continue_on_error: bool = True,
    ) -> dict[str, Any]:
        results = []
        bounds = historical_week_bounds(today)
        for index, (start, end) in enumerate(bounds):
            try:
                item = self.sync_week(start, end, client)
                if index < max(0, len(bounds) - 2):
                    with self._connect() as conn:
                        conn.execute(
                            "UPDATE wb_finance_weekly_sync SET status='completed',unchanged_sync_count=1,next_retry_at=NULL WHERE seller_id=? AND week_start=? AND week_end=? AND status='loaded_preliminary'",
                            (self.seller_id, start.isoformat(), end.isoformat()),
                        )
                        conn.commit()
                    item["status"] = "completed"
                results.append(item)
            except Exception as exc:
                results.append(
                    {
                        "status": "error",
                        "week_start": start.isoformat(),
                        "week_end": end.isoformat(),
                        "error": str(exc),
                    }
                )
                if not continue_on_error:
                    raise
        for start, end in bounds[-2:]:
            try:
                stabilized = self.sync_week(start, end, client)
                results.append({**stabilized, "stabilization_resync": True})
            except Exception as exc:
                results.append(
                    {
                        "status": "error",
                        "week_start": start.isoformat(),
                        "week_end": end.isoformat(),
                        "error": str(exc),
                        "stabilization_resync": True,
                    }
                )
        return {
            "status": "completed_with_errors"
            if any(r["status"] in {"error", "waiting"} for r in results)
            else "completed",
            "weeks": results,
            "week_count": len(bounds),
        }

    def recalculate_all_weeks(self) -> dict[str, Any]:
        """Rebuild every stored week for the configured seller from raw rows."""
        self.ensure_schema()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT DISTINCT week_start,week_end
                FROM wb_finance_weekly_raw_rows
                WHERE seller_id=?
                ORDER BY week_start""",
                (self.seller_id,),
            ).fetchall()
        results = []
        for row in rows:
            start = date.fromisoformat(row["week_start"])
            end = date.fromisoformat(row["week_end"])
            results.append(
                {
                    "week_start": start.isoformat(),
                    "week_end": end.isoformat(),
                    "aggregate": self.recalculate_week(start, end),
                }
            )
        return {"status": "completed", "week_count": len(results), "weeks": results}

    def plan_business_approved_backfill(
        self,
        *,
        date_from: date = RETRO_COST_FIRST_WEEK_START,
        date_to: date | None = None,
    ) -> dict[str, Any]:
        """Read-only production preflight for the Finance retro projection."""

        raise ValueError(
            "legacy business-approved Finance plan is permanently revoked; "
            "use plan_canonical_finance_backfill"
        )

        if not self.db_path.is_file():
            raise ValueError(f"Finance runtime SQLite does not exist: {self.db_path}")
        latest_closed = week_bounds(self.now_factory().astimezone(MOSCOW).date())[0] - timedelta(days=1)
        scope_end = date_to or latest_closed
        if scope_end < date_from:
            raise ValueError("date_to must not be earlier than date_from")
        if scope_end > latest_closed:
            raise ValueError(
                f"date_to must not exceed latest fully closed week end {latest_closed.isoformat()}"
            )
        with self._connect() as conn:
            conn.execute("BEGIN")
            try:
                return self._plan_business_approved_backfill_in_connection(
                    conn,
                    date_from=date_from,
                    date_to=scope_end,
                )
            finally:
                conn.rollback()

    def _plan_business_approved_backfill_in_connection(
        self,
        conn: sqlite3.Connection,
        *,
        date_from: date,
        date_to: date,
    ) -> dict[str, Any]:
        candidate_rows = conn.execute(
            """SELECT DISTINCT week_start,week_end
               FROM wb_finance_weekly_raw_rows
               WHERE seller_id=? AND week_end>=? AND week_end<=?
               ORDER BY week_start""",
            (self.seller_id, date_from.isoformat(), date_to.isoformat()),
        ).fetchall()
        candidates = [
            (date.fromisoformat(row["week_start"]), date.fromisoformat(row["week_end"]))
            for row in candidate_rows
        ]
        finance_weeks: list[dict[str, Any]] = []
        union_nm_ids: set[str] = set()
        identity_issues: list[dict[str, str]] = []
        finance_source_blockers: list[dict[str, str]] = []
        rows_without_nm_id = 0
        alias_to_nm, ambiguous_aliases, _groups, _items = _nomenclature_identity_index(conn)
        for start, end in candidates:
            key = (start.isoformat(), end.isoformat())
            raw_rows = conn.execute(
                """SELECT report_id,rrd_id,nm_id,row_hash,raw_json
                   FROM wb_finance_weekly_raw_rows
                   WHERE seller_id=? AND week_start=? AND week_end=?
                   ORDER BY report_id,rrd_id""",
                (self.seller_id, *key),
            ).fetchall()
            parsed = [json.loads(row["raw_json"]) for row in raw_rows]
            raw_week_nm_ids = {
                str(row.get("nmId") or "").strip()
                for row in parsed
                if str(row.get("docTypeName") or "").casefold()
                in {"продажа", "возврат"}
                and str(row.get("nmId") or "").strip() not in {"", "0"}
            }
            week_nm_ids: set[str] = set()
            week_identity_issues: list[dict[str, str]] = []
            zero_unit_identity_evidence: list[list[str]] = []
            for row in parsed:
                if str(row.get("docTypeName") or "").casefold() not in {
                    "продажа",
                    "возврат",
                }:
                    continue
                resolved_nm, method, reason = _resolve_finance_nm_id(
                    row,
                    alias_to_nm=alias_to_nm,
                    ambiguous_aliases=ambiguous_aliases,
                )
                if resolved_nm:
                    week_nm_ids.add(resolved_nm)
                else:
                    if int(_decimal(row.get("quantity"))) == 0:
                        zero_unit_identity_evidence.append(
                            [
                                str(row.get("reportId") or ""),
                                str(row.get("rrdId") or ""),
                                method,
                                reason,
                            ]
                        )
                        continue
                    issue = {
                        "week_start": key[0],
                        "report_id": str(row.get("reportId") or ""),
                        "rrd_id": str(row.get("rrdId") or ""),
                        "identity_method": method,
                        "reason": reason,
                    }
                    week_identity_issues.append(issue)
                    identity_issues.append(issue)
            union_nm_ids.update(week_nm_ids)
            no_nm_count = sum(
                1 for row in parsed if str(row.get("nmId") or "").strip() in {"", "0"}
            )
            rows_without_nm_id += no_nm_count
            report_rows = conn.execute(
                """SELECT report_id,report_type,row_count,content_hash
                   FROM wb_finance_weekly_reports
                   WHERE seller_id=? AND week_start=? AND week_end=?
                   ORDER BY report_id""",
                (self.seller_id, *key),
            ).fetchall()
            sync_row = conn.execute(
                """SELECT status,last_error FROM wb_finance_weekly_sync
                   WHERE seller_id=? AND week_start=? AND week_end=?""",
                (self.seller_id, *key),
            ).fetchone()
            sync_status = str(sync_row["status"] or "") if sync_row else "missing"
            if sync_status not in {"completed", "incomplete_cost"}:
                finance_source_blockers.append(
                    {
                        "code": "finance_source_week_not_completed",
                        "week_start": key[0],
                        "week_end": key[1],
                        "status": sync_status,
                        "reason": str(sync_row["last_error"] or "") if sync_row else "sync row missing",
                    }
                )
            raw_digest = hashlib.sha256(
                json.dumps(
                    [
                        [
                            row["report_id"],
                            row["rrd_id"],
                            row["nm_id"],
                            row["row_hash"],
                            hashlib.sha256(str(row["raw_json"]).encode("utf-8")).hexdigest(),
                        ]
                        for row in raw_rows
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            report_digest = hashlib.sha256(
                json.dumps(
                    [list(row) for row in report_rows],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            finance_weeks.append(
                {
                    "week_start": key[0],
                    "week_end": key[1],
                    "raw_row_count": len(raw_rows),
                    "report_count": len(report_rows),
                    "sale_return_nm_ids": sorted(week_nm_ids),
                    "raw_sale_return_nm_ids": sorted(raw_week_nm_ids),
                    "sale_return_identity_issues": week_identity_issues,
                    "zero_unit_unresolved_identity_count": len(
                        zero_unit_identity_evidence
                    ),
                    "zero_unit_unresolved_identity_digest": "sha256:"
                    + hashlib.sha256(
                        json.dumps(
                            sorted(zero_unit_identity_evidence),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                    "rows_without_nm_id": no_nm_count,
                    "sync_status": sync_status,
                    "sync_last_error": str(sync_row["last_error"] or "") if sync_row else "",
                    "raw_digest": f"sha256:{raw_digest}",
                    "report_digest": f"sha256:{report_digest}",
                }
            )
            del parsed, raw_rows

        proposed_rows, retro_blockers = self._build_retro_cost_rows(
            conn,
            rows=self._iter_retro_cost_movement_rows(conn, candidates=candidates),
        )
        persisted_map = self._load_retro_cost_map(conn)
        projected_map = {**persisted_map, **{row["nm_id"]: row for row in proposed_rows}}
        changed_weeks: list[dict[str, Any]] = []
        coverage_weeks: list[dict[str, Any]] = []
        blockers = [*retro_blockers, *finance_source_blockers]
        for start, end in candidates:
            key = (start.isoformat(), end.isoformat())
            parsed = [
                json.loads(row["raw_json"])
                for row in conn.execute(
                    """SELECT raw_json FROM wb_finance_weekly_raw_rows
                       WHERE seller_id=? AND week_start=? AND week_end=?
                       ORDER BY report_id,rrd_id""",
                    (self.seller_id, *key),
                ).fetchall()
            ]
            aggregate, coverage, unknown = self._aggregate_rows(
                conn,
                parsed,
                start,
                retro_cost_map=projected_map,
            )
            if int(coverage["unmatched_units"]):
                blockers.append(
                    {
                        "code": "finance_cost_coverage_incomplete",
                        "week_start": key[0],
                        "week_end": key[1],
                        "problem_skus": coverage["problem_skus"],
                    }
                )
            stored_aggregate_row = conn.execute(
                """SELECT metrics_json FROM wb_finance_weekly_aggregates
                   WHERE seller_id=? AND week_start=? AND week_end=?""",
                (self.seller_id, *key),
            ).fetchone()
            stored_coverage_row = conn.execute(
                """SELECT matched_units,unmatched_units,coverage_pct,cogs_rub,
                          problem_skus_json,quality_json,cost_state_hash
                   FROM wb_finance_weekly_cost_coverage
                   WHERE seller_id=? AND week_start=? AND week_end=?""",
                (self.seller_id, *key),
            ).fetchone()
            stored_metrics = (
                json.loads(stored_aggregate_row["metrics_json"])
                if stored_aggregate_row is not None
                else {}
            )
            stored_coverage = (
                {
                    "matched_units": stored_coverage_row["matched_units"],
                    "unmatched_units": stored_coverage_row["unmatched_units"],
                    "coverage_pct": stored_coverage_row["coverage_pct"],
                    "cogs_rub": stored_coverage_row["cogs_rub"],
                    "problem_skus": json.loads(stored_coverage_row["problem_skus_json"] or "[]"),
                    "quality": json.loads(stored_coverage_row["quality_json"] or "{}"),
                    "cost_state_hash": stored_coverage_row["cost_state_hash"] or "",
                }
                if stored_coverage_row is not None
                else {}
            )
            expected_coverage = {
                key_name: coverage[key_name]
                for key_name in (
                    "matched_units",
                    "unmatched_units",
                    "coverage_pct",
                    "cogs_rub",
                    "problem_skus",
                    "quality",
                    "cost_state_hash",
                )
            }
            quality = coverage["quality"]
            confirmed_share = quality.get("confirmed_share_pct")
            coverage_weeks.append(
                {
                    "week_start": key[0],
                    "week_end": key[1],
                    "before": stored_coverage,
                    "expected": expected_coverage,
                    "complete": int(coverage["unmatched_units"]) == 0,
                    "confirmed_share_interpretation": (
                        "complete_historical_frozen_projection"
                        if int(coverage["unmatched_units"]) == 0
                        and confirmed_share == "0.0000"
                        else "not_applicable"
                        if confirmed_share is None
                        else "canonical_quality_share"
                    ),
                    "unknown_reasons": unknown,
                }
            )
            changed = (
                stored_metrics != aggregate
                or stored_coverage != expected_coverage
            )
            if changed:
                changed_weeks.append(
                    {
                        "week_start": key[0],
                        "week_end": key[1],
                        "before": {
                            "cogs": stored_metrics.get("cogs"),
                            "profit_after_cogs": stored_metrics.get("profit_after_cogs"),
                            "final_margin_pct": stored_metrics.get("final_margin_pct"),
                            "matched_units": stored_coverage.get("matched_units"),
                            "unmatched_units": stored_coverage.get("unmatched_units"),
                            "cost_state_hash": stored_coverage.get("cost_state_hash"),
                        },
                        "expected": {
                            "cogs": aggregate.get("cogs"),
                            "profit_after_cogs": aggregate.get("profit_after_cogs"),
                            "final_margin_pct": aggregate.get("final_margin_pct"),
                            "total_wb_expenses": aggregate.get("total_wb_expenses"),
                            "profit_period_expenses": aggregate.get("profit_period_expenses"),
                            "wb_expenses_without_marketing_pct": aggregate.get(
                                "wb_expenses_without_marketing_pct"
                            ),
                            "matched_units": coverage["matched_units"],
                            "unmatched_units": coverage["unmatched_units"],
                            "coverage_pct": coverage["coverage_pct"],
                            "cost_state_hash": coverage["cost_state_hash"],
                            "problem_skus": coverage["problem_skus"],
                            "unknown_reasons": unknown,
                        },
                    }
                )
            del parsed

        target_keys = {
            (self.seller_id, item["week_start"], item["week_end"])
            for item in changed_weeks
        }
        ads_manifest = self._ads_coverage_manifest(
            conn,
            nm_ids=sorted(union_nm_ids),
            date_from=max(date_from, RETRO_COST_PERIOD_START),
            date_to=date_to,
        )
        finance_manifest = {
            "week_count": len(finance_weeks),
            "raw_row_count": sum(item["raw_row_count"] for item in finance_weeks),
            "report_count": sum(item["report_count"] for item in finance_weeks),
            "sale_return_nm_ids": sorted(union_nm_ids),
            "sale_return_nm_id_count": len(union_nm_ids),
            "sale_return_identity_issue_count": len(identity_issues),
            "sale_return_identity_issues": identity_issues,
            "zero_unit_unresolved_identity_count": sum(
                int(item["zero_unit_unresolved_identity_count"])
                for item in finance_weeks
            ),
            "zero_unit_unresolved_identity_digest": "sha256:"
            + hashlib.sha256(
                json.dumps(
                    [
                        [
                            item["week_start"],
                            item["zero_unit_unresolved_identity_count"],
                            item["zero_unit_unresolved_identity_digest"],
                        ]
                        for item in finance_weeks
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "rows_without_nm_id": rows_without_nm_id,
            "weeks": finance_weeks,
        }
        coverage_manifest = {
            "week_count": len(coverage_weeks),
            "complete_week_count": sum(1 for item in coverage_weeks if item["complete"]),
            "incomplete_week_count": sum(1 for item in coverage_weeks if not item["complete"]),
            "weeks": coverage_weeks,
        }
        coverage_manifest["digest"] = "sha256:" + hashlib.sha256(
            json.dumps(
                coverage_manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        cost_manifest = {
            "formula_version": RETRO_COST_FORMULA_VERSION,
            "period_start": RETRO_COST_PERIOD_START.isoformat(),
            "period_end": RETRO_COST_PERIOD_END.isoformat(),
            "reference_date": RETRO_COST_REFERENCE_DATE.isoformat(),
            "persisted_row_count": len(persisted_map),
            "proposed_row_count": len(proposed_rows),
            "rows": proposed_rows,
        }
        source_manifest = {
            "finance": finance_manifest,
            "cost": cost_manifest,
            "ads": ads_manifest,
            "coverage": coverage_manifest,
        }
        source_digest = "sha256:" + hashlib.sha256(
            json.dumps(
                source_manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        plan: dict[str, Any] = {
            "schema_version": "wb_finance_business_approved_backfill_v1",
            "status": "dry_run",
            "runtime_mutation": False,
            "apply_allowed": not blockers,
            "seller_id": self.seller_id,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "formula_versions": {
                "cost": COST_METHOD_VERSION,
                "retro_cost": RETRO_COST_FORMULA_VERSION,
                "profit": PROFIT_METHOD_VERSION,
            },
            "source_manifests": source_manifest,
            "source_digest": source_digest,
            "checked_week_count": len(candidates),
            "target_week_count": len(changed_weeks),
            "weeks": changed_weeks,
            "blockers": blockers,
            "target_before_digest": self._finance_state_digest(
                conn,
                target_keys=target_keys,
                target_only=True,
            ),
            "non_target_digest": self._finance_state_digest(
                conn,
                target_keys=target_keys,
                target_only=False,
            ),
            "backup_plan": {
                "method": "sqlite_online_backup",
                "integrity_check": "required_ok",
                "sha256": "required",
                "mode": "0600",
                "free_space_check": "required_before_backup",
            },
            "apply_plan": {
                "requires_exact_fingerprint": True,
                "single_immediate_transaction": True,
                "rollback_on_drift_or_error": True,
                "partial_or_force_modes": False,
            },
            "reconciliation_plan": {
                "target_readback": True,
                "full_cost_coverage": True,
                "non_target_invariants": True,
                "repeat_apply_noop": True,
                "audit_record": True,
            },
        }
        plan["fingerprint"] = "sha256:" + hashlib.sha256(
            json.dumps(
                plan,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return plan

    def _iter_retro_cost_movement_rows(
        self,
        conn: sqlite3.Connection,
        *,
        candidates: Iterable[tuple[date, date]],
    ) -> Iterable[dict[str, Any]]:
        """Stream only cost-bearing Finance movements one row at a time."""

        for start, end in candidates:
            for raw_row in conn.execute(
                """SELECT raw_json FROM wb_finance_weekly_raw_rows
                   WHERE seller_id=? AND week_start=? AND week_end=?
                   ORDER BY report_id,rrd_id""",
                (self.seller_id, start.isoformat(), end.isoformat()),
            ):
                row = json.loads(raw_row["raw_json"])
                if (
                    str(row.get("docTypeName") or "").casefold()
                    in {"продажа", "возврат"}
                    and int(_decimal(row.get("quantity"))) != 0
                ):
                    yield row

    def _build_retro_cost_rows(
        self,
        conn: sqlite3.Connection,
        *,
        rows: Iterable[Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        alias_to_nm, ambiguous_aliases, _groups, _items = _nomenclature_identity_index(conn)
        required: set[str] = set()
        blockers: list[dict[str, Any]] = []
        for row in rows:
            if str(row.get("docTypeName") or "").casefold() not in {"продажа", "возврат"}:
                continue
            if int(_decimal(row.get("quantity"))) == 0:
                continue
            operation_date, operation_source = _operation_date(row, RETRO_COST_PERIOD_START)
            if not (RETRO_COST_PERIOD_START <= operation_date <= RETRO_COST_PERIOD_END):
                continue
            nm_id, identity_method, identity_problem = _resolve_finance_nm_id(
                row,
                alias_to_nm=alias_to_nm,
                ambiguous_aliases=ambiguous_aliases,
            )
            if operation_source == "week_start_fallback":
                blockers.append(
                    {
                        "code": "retro_operation_date_missing",
                        "report_id": str(row.get("reportId") or ""),
                        "rrd_id": str(row.get("rrdId") or ""),
                    }
                )
            if not nm_id:
                blockers.append(
                    {
                        "code": "retro_finance_identity_unresolved",
                        "report_id": str(row.get("reportId") or ""),
                        "rrd_id": str(row.get("rrdId") or ""),
                        "identity_method": identity_method,
                        "reason": identity_problem,
                    }
                )
                continue
            required.add(nm_id)

        existing = self._load_retro_cost_map(conn)
        proposals: list[dict[str, Any]] = []
        for nm_id in sorted(required):
            candidate = self._select_retro_cost_source(conn, nm_id=nm_id)
            if candidate is None:
                blockers.append(
                    {
                        "code": "retro_canonical_cost_missing",
                        "nm_id": nm_id,
                        "reference_date": RETRO_COST_REFERENCE_DATE.isoformat(),
                    }
                )
                continue
            row_payload = {
                "seller_id": self.seller_id,
                "nm_id": nm_id,
                **candidate,
                "formula_version": RETRO_COST_FORMULA_VERSION,
                "status": "business_approved_retro",
            }
            fingerprint_payload = {
                key: row_payload[key]
                for key in (
                    "seller_id",
                    "nm_id",
                    "unit_cost_rub",
                    "source_date",
                    "source_table",
                    "source_row_sha256",
                    "selection_method",
                    "formula_version",
                    "status",
                )
            }
            row_payload["source_calculation_fingerprint"] = "sha256:" + hashlib.sha256(
                json.dumps(
                    fingerprint_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            persisted = existing.get(nm_id)
            if persisted is not None:
                if str(persisted.get("source_calculation_fingerprint") or "") != str(
                    row_payload["source_calculation_fingerprint"]
                ):
                    blockers.append(
                        {
                            "code": "retro_cost_immutable_conflict",
                            "nm_id": nm_id,
                            "persisted_fingerprint": str(
                                persisted.get("source_calculation_fingerprint") or ""
                            ),
                            "expected_fingerprint": row_payload[
                                "source_calculation_fingerprint"
                            ],
                        }
                    )
                continue
            proposals.append(row_payload)
        deduplicated = {
            json.dumps(item, ensure_ascii=False, sort_keys=True): item for item in blockers
        }
        return proposals, [deduplicated[key] for key in sorted(deduplicated)]

    def _select_retro_cost_source(
        self,
        conn: sqlite3.Connection,
        *,
        nm_id: str,
    ) -> dict[str, Any] | None:
        source_table = ""
        source_row: sqlite3.Row | None = None
        source_cost_column = ""
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        functional_active = False
        if {
            "sheet_vitrina_v1_warehouse_functional_cutovers",
            "sheet_vitrina_v1_warehouse_wb_daily_cost",
        }.issubset(tables):
            functional_active = (
                conn.execute(
                    """SELECT 1 FROM sheet_vitrina_v1_warehouse_functional_cutovers
                       WHERE cutover_id='warehouse_functional_cutover_v1' AND status='posted'"""
                ).fetchone()
                is not None
            )
        if functional_active:
            source_row = conn.execute(
                """SELECT * FROM sheet_vitrina_v1_warehouse_wb_daily_cost
                   WHERE cutover_id='warehouse_functional_cutover_v1'
                     AND nm_id=? AND as_of_date>=? AND wac_rub IS NOT NULL
                   ORDER BY CASE WHEN as_of_date=? THEN 0 ELSE 1 END, as_of_date
                   LIMIT 1""",
                (
                    nm_id,
                    RETRO_COST_REFERENCE_DATE.isoformat(),
                    RETRO_COST_REFERENCE_DATE.isoformat(),
                ),
            ).fetchone()
            if source_row is not None:
                source_table = "sheet_vitrina_v1_warehouse_wb_daily_cost"
                source_cost_column = "wac_rub"
        if (
            source_row is None
            and not functional_active
            and "sheet_vitrina_v1_wb_cost_daily_state" in tables
        ):
            source_row = conn.execute(
                """SELECT * FROM sheet_vitrina_v1_wb_cost_daily_state
                   WHERE nm_id=? AND as_of_date>=? AND our_wb_unit_cost_rub IS NOT NULL
                   ORDER BY CASE WHEN as_of_date=? THEN 0 ELSE 1 END, as_of_date
                   LIMIT 1""",
                (
                    nm_id,
                    RETRO_COST_REFERENCE_DATE.isoformat(),
                    RETRO_COST_REFERENCE_DATE.isoformat(),
                ),
            ).fetchone()
            if source_row is not None:
                source_table = "sheet_vitrina_v1_wb_cost_daily_state"
                source_cost_column = "our_wb_unit_cost_rub"
        if source_row is None:
            return None
        unit_cost = _decimal(source_row[source_cost_column])
        if unit_cost <= ZERO:
            return None
        source = {key: source_row[key] for key in source_row.keys()}
        source_row_json = json.dumps(
            source,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        source_date = str(source_row["as_of_date"])
        return {
            "unit_cost_rub": _money_text(unit_cost),
            "source_date": source_date,
            "source_table": source_table,
            "source_row_json": source_row_json,
            "source_row_sha256": "sha256:"
            + hashlib.sha256(source_row_json.encode("utf-8")).hexdigest(),
            "selection_method": (
                "exact_2026_07_01"
                if source_date == RETRO_COST_REFERENCE_DATE.isoformat()
                else "first_available_after_2026_07_01"
            ),
        }

    def _ads_coverage_manifest(
        self,
        conn: sqlite3.Connection,
        *,
        nm_ids: list[str],
        date_from: date,
        date_to: date,
    ) -> dict[str, Any]:
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='temporal_source_slot_snapshots'"
        ).fetchone()
        wanted = {str(nm_id) for nm_id in nm_ids}
        cursor = date_from
        dates: list[dict[str, Any]] = []
        missing_pairs: list[dict[str, str]] = []
        invalid_pairs: list[dict[str, str]] = []
        evidence: list[Any] = []
        while cursor <= date_to:
            day = cursor.isoformat()
            row = (
                conn.execute(
                    """SELECT captured_at,payload_json
                       FROM temporal_source_slot_snapshots
                       WHERE source_key='ads_compact' AND snapshot_date=?
                         AND snapshot_role='accepted_closed_day_snapshot'""",
                    (day,),
                ).fetchone()
                if table_exists is not None
                else None
            )
            covered: set[str] = set()
            source_kind = "missing"
            envelope_origin = "missing"
            payload_digest = ""
            if row is not None:
                payload_json = str(row["payload_json"] or "")
                payload_digest = "sha256:" + hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
                try:
                    payload = json.loads(payload_json)
                except json.JSONDecodeError:
                    payload = {}
                result, envelope_origin = resolve_ads_snapshot_payload(payload)
                source_kind = str((result or {}).get("kind") or "invalid")
                items = (result or {}).get("items") or []
                for item in items if isinstance(items, list) else []:
                    if isinstance(item, dict):
                        value = str(item.get("nm_id", item.get("nmId", "")) or "")
                        if value in wanted:
                            raw_ads_sum = item.get("ads_sum")
                            try:
                                valid_ads_sum = (
                                    raw_ads_sum not in (None, "")
                                    and Decimal(str(raw_ads_sum)).is_finite()
                                )
                            except (InvalidOperation, ValueError, TypeError):
                                valid_ads_sum = False
                            if valid_ads_sum:
                                covered.add(value)
                            else:
                                invalid_pairs.append(
                                    {"date": day, "nm_id": value, "reason": "ads_sum_invalid"}
                                )
                if source_kind == "empty":
                    covered = set(wanted)
                if result is None:
                    invalid_pairs.extend(
                        {"date": day, "nm_id": nm_id, "reason": "ads_envelope_invalid"}
                        for nm_id in sorted(wanted)
                    )
            for nm_id in sorted(wanted - covered):
                missing_pairs.append({"date": day, "nm_id": nm_id})
            date_item = {
                "date": day,
                "source_kind": source_kind,
                "envelope_origin": envelope_origin,
                "covered_nm_id_count": len(covered),
                "missing_nm_id_count": len(wanted - covered),
                "captured_at": str(row["captured_at"] or "") if row is not None else "",
                "payload_digest": payload_digest,
            }
            dates.append(date_item)
            evidence.append(date_item)
            cursor += timedelta(days=1)
        return {
            "source": "ads_compact/fullstats accepted closed-day snapshots",
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "nm_id_count": len(wanted),
            "date_count": len(dates),
            "complete": not missing_pairs and not invalid_pairs,
            "missing_date_nm_id_count": len(missing_pairs),
            "missing_date_nm_ids": missing_pairs,
            "invalid_date_nm_id_count": len(invalid_pairs),
            "invalid_date_nm_ids": invalid_pairs,
            "dates": dates,
            "digest": "sha256:"
            + hashlib.sha256(
                json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        }

    def plan_canonical_finance_backfill(
        self,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> dict[str, Any]:
        """Read-only fixed-cutoff Finance preflight bound to canonical cost truth."""

        if date_to is None:
            date_to = self.canonical_finance_historical_cutoff()

        conn = self._connect_canonical_plan()
        try:
            return self._plan_canonical_finance_backfill_in_connection(
                conn,
                date_from=date_from,
                date_to=date_to,
            )
        finally:
            conn.close()

    def canonical_finance_historical_cutoff(self) -> date:
        """Return the task-frozen historical cutoff after source eligibility proof."""

        today = self.now_factory().date()
        with self._connect_canonical_plan() as conn:
            row = conn.execute(
                """SELECT MAX(week_end) FROM wb_finance_weekly_raw_rows
                   WHERE seller_id=? AND week_end<?""",
                (self.seller_id, today.isoformat()),
            ).fetchone()
        if row is None or not row[0]:
            raise ValueError("Finance has no fully closed week for a historical cutoff")
        latest_closed = date.fromisoformat(str(row[0]))
        if latest_closed < FBS_FINANCE_HISTORICAL_CUTOFF:
            raise ValueError(
                "Finance raw history has not reached the fixed FBS historical cutoff"
            )
        return FBS_FINANCE_HISTORICAL_CUTOFF

    def _plan_canonical_finance_backfill_in_connection(
        self,
        conn: sqlite3.Connection,
        *,
        date_from: date | None,
        date_to: date | None,
    ) -> dict[str, Any]:
        required_tables = {
            "wb_finance_weekly_raw_rows",
            "wb_finance_weekly_reports",
            "wb_finance_weekly_sync",
            "wb_finance_weekly_aggregates",
            "wb_finance_weekly_cost_coverage",
            "wb_finance_weekly_reconciliation",
            "wb_finance_weekly_sku_aggregates",
            "sheet_vitrina_v1_nomenclature_items",
            "sheet_vitrina_v1_warehouse_wb_daily_cost",
            "sheet_vitrina_v1_warehouse_functional_cutovers",
        }
        existing_tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        existing_tables.update(
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_temp_master WHERE type='view'"
            ).fetchall()
        )
        missing_schema = sorted(required_tables - existing_tables)
        if "wb_finance_weekly_raw_rows" not in existing_tables:
            raise ValueError("Finance raw schema is not deployed")
        bounds = conn.execute(
            """SELECT MIN(week_start) first_week,MAX(week_end) last_week,
                      COUNT(*) raw_row_count,COUNT(DISTINCT NULLIF(nm_id,'')) nm_id_count
               FROM wb_finance_weekly_raw_rows WHERE seller_id=?""",
            (self.seller_id,),
        ).fetchone()
        if bounds is None or not bounds["first_week"]:
            raise ValueError("Finance raw history is empty")
        scope_from = date_from or date.fromisoformat(str(bounds["first_week"]))
        scope_to = date_to or date.fromisoformat(str(bounds["last_week"]))
        week_rows = conn.execute(
            """SELECT DISTINCT week_start,week_end FROM wb_finance_weekly_raw_rows
               WHERE seller_id=? AND week_end>=? AND week_end<=?
               ORDER BY week_start""",
            (self.seller_id, scope_from.isoformat(), scope_to.isoformat()),
        ).fetchall()
        target_keys = {
            (self.seller_id, str(item["week_start"]), str(item["week_end"]))
            for item in week_rows
        }
        initial_target_before_image = self._finance_target_images(conn, target_keys)
        initial_target_before_image_digest = self._json_digest(
            initial_target_before_image
        )
        initial_source_dependency = (
            None
            if missing_schema
            else self._finance_source_dependency_fingerprint(
                conn,
                target_keys=target_keys,
                force_reload=True,
            )
        )
        weeks: list[dict[str, Any]] = []
        matrix: list[dict[str, Any]] = []
        blockers: list[dict[str, Any]] = [
            {"code": "required_schema_missing", "tables": missing_schema}
        ] if missing_schema else []
        finance_manifest_digest = _StreamingJsonArrayDigest()
        cost_manifest: dict[str, dict[str, Any]] = {}
        union_nm_ids: set[str] = set()
        target_before_digest = _StreamingJsonArrayDigest()
        target_after_digest = _StreamingJsonArrayDigest()
        expected_sku_projection_row_count = 0
        fbs_primary_order_digests: set[str] = set()
        fbs_fallback_order_digests: set[str] = set()
        fbs_primary_units = 0
        fbs_fallback_units = 0
        fbs_primary_revenue = ZERO
        fbs_fallback_revenue = ZERO
        fbs_remaining_orders = 0
        fbs_remaining_units = 0
        fbs_remaining_revenue = ZERO
        fbs_remaining_reasons: dict[str, int] = {}
        for week in week_rows:
            start = date.fromisoformat(str(week["week_start"]))
            end = date.fromisoformat(str(week["week_end"]))
            stored_rows = conn.execute(
                """SELECT report_id,rrd_id,row_hash,raw_json
                   FROM wb_finance_weekly_raw_rows
                   WHERE seller_id=? AND week_start=? AND week_end=?
                   ORDER BY report_id,rrd_id""",
                (self.seller_id, start.isoformat(), end.isoformat()),
            ).fetchall()
            parsed = [json.loads(str(row["raw_json"])) for row in stored_rows]
            for row in stored_rows:
                finance_manifest_digest.add(
                    [
                        start.isoformat(),
                        str(row["report_id"]),
                        str(row["rrd_id"]),
                        str(row["row_hash"]),
                    ]
                )
            detailed = self._calculate_cogs(conn, parsed, start, include_details=True)
            for detail in detailed["detail_rows"]:
                if detail.get("movement") != "sale" or detail.get("channel") != "FBS":
                    continue
                order_digest = str(detail.get("order_identity_digest") or "")
                units = int(detail.get("quantity") or 0)
                revenue = _decimal(detail.get("sales_revenue_rub"))
                if detail.get("source_quality") == "pooled_fbs_physical_exact":
                    fbs_primary_order_digests.add(order_digest)
                    fbs_primary_units += units
                    fbs_primary_revenue += revenue
                elif detail.get("source_quality") == "same_day_common_inventory_fallback":
                    fbs_fallback_order_digests.add(order_digest)
                    fbs_fallback_units += units
                    fbs_fallback_revenue += revenue
            fbs_remaining_orders += int(
                detailed.get("uncovered_fbs_sales_order_count") or 0
            )
            fbs_remaining_units += int(
                detailed.get("uncovered_fbs_sales_units") or 0
            )
            fbs_remaining_revenue += _decimal(
                detailed.get("uncovered_fbs_sales_revenue_rub")
            )
            for problem in detailed.get("problem_skus") or []:
                if str(problem.get("channel") or "") != "FBS":
                    continue
                reason = str(problem.get("reason") or "canonical_cost_missing")
                fbs_remaining_reasons[reason] = (
                    fbs_remaining_reasons.get(reason, 0)
                    + int(problem.get("operation_count") or 0)
                )
            new_metrics, new_coverage, unknown = self._aggregate_rows(
                conn,
                parsed,
                start,
                coverage_override={**detailed, "detail_rows": []},
            )
            stored_aggregate = (
                conn.execute(
                    """SELECT classifier_version,metrics_json FROM wb_finance_weekly_aggregates
                       WHERE seller_id=? AND week_start=? AND week_end=?""",
                    (self.seller_id, start.isoformat(), end.isoformat()),
                ).fetchone()
                if "wb_finance_weekly_aggregates" in existing_tables
                else None
            )
            stored_coverage = (
                conn.execute(
                    """SELECT matched_units,unmatched_units,coverage_pct,cogs_rub,
                              problem_skus_json,quality_json,cost_state_hash
                       FROM wb_finance_weekly_cost_coverage
                       WHERE seller_id=? AND week_start=? AND week_end=?""",
                    (self.seller_id, start.isoformat(), end.isoformat()),
                ).fetchone()
                if "wb_finance_weekly_cost_coverage" in existing_tables
                else None
            )
            stored_sku_aggregates = (
                [
                    dict(row)
                    for row in conn.execute(
                        """SELECT seller_id,week_start,week_end,nm_id,formula_version,
                                  metrics_json,coverage_json,raw_source_digest,
                                  week_content_hash,cost_state_hash,raw_row_count
                           FROM wb_finance_weekly_sku_aggregates
                           WHERE seller_id=? AND week_start=? AND week_end=?
                           ORDER BY nm_id""",
                        (self.seller_id, start.isoformat(), end.isoformat()),
                    ).fetchall()
                ]
                if "wb_finance_weekly_sku_aggregates" in existing_tables
                else []
            )
            old_metrics = (
                json.loads(str(stored_aggregate["metrics_json"] or "{}"))
                if stored_aggregate is not None
                else {}
            )
            old_cogs = self._nullable_decimal(old_metrics.get("cogs"))
            new_cogs = self._nullable_decimal(new_metrics.get("cogs"))
            old_profit = self._nullable_decimal(old_metrics.get("profit_after_cogs"))
            new_profit = self._nullable_decimal(new_metrics.get("profit_after_cogs"))
            old_margin = self._nullable_decimal(old_metrics.get("final_margin_pct"))
            new_margin = self._nullable_decimal(new_metrics.get("final_margin_pct"))
            old_before_cogs = self._nullable_decimal(old_metrics.get("before_cogs_profit"))
            new_before_cogs = self._nullable_decimal(new_metrics.get("before_cogs_profit"))
            cogs_delta = self._delta_text(new_cogs, old_cogs)
            profit_delta = self._delta_text(new_profit, old_profit)
            margin_delta = self._delta_text(new_margin, old_margin)
            before_cogs_delta = self._delta_text(new_before_cogs, old_before_cogs)
            cogs_profit_effect = (
                _money_text(-(new_cogs - old_cogs))
                if new_cogs is not None and old_cogs is not None
                else None
            )
            profit_component_keys = (
                "net_revenue",
                "agent_remuneration",
                "acquiring",
                "logistics",
                "storage",
                "acceptance",
                "marketing",
                "transit_logistics",
                "penalties",
                "subscriptions",
                "paid_services",
                "review_points",
                "other_deductions",
                "corrections",
                "capitalized_acceptance",
                "capitalized_transit_logistics",
                "positive_adjustments",
                "profit_period_expenses",
                "before_cogs_profit",
                "cogs",
                "profit_after_cogs",
                "final_margin_pct",
            )
            component_reconciliation: dict[str, dict[str, str | None]] = {}
            changed_components: list[str] = []
            for key in profit_component_keys:
                old_value = self._nullable_decimal(old_metrics.get(key))
                new_value = self._nullable_decimal(new_metrics.get(key))
                delta_value = self._delta_text(new_value, old_value)
                component_reconciliation[key] = {
                    "before": _money_text(old_value),
                    "after": _money_text(new_value),
                    "delta": delta_value,
                }
                if delta_value is not None and _decimal(delta_value) != ZERO:
                    changed_components.append(key)
            profit_difference_explanation = (
                "profit delta equals the negative COGS delta; before-COGS profit is unchanged"
                if profit_delta is not None
                and cogs_profit_effect is not None
                and _decimal(profit_delta) == _decimal(cogs_profit_effect)
                and before_cogs_delta is not None
                and _decimal(before_cogs_delta) == ZERO
                else (
                    "profit delta = before-COGS profit delta "
                    f"{before_cogs_delta or 'NULL'} + COGS effect {cogs_profit_effect or 'NULL'}; "
                    "changed measured inputs: "
                    + (", ".join(changed_components) if changed_components else "legacy inputs unavailable")
                )
            )
            week_item = {
                "week_start": start.isoformat(),
                "week_end": end.isoformat(),
                "raw_row_count": len(stored_rows),
                "before": {
                    "cogs_rub": _money_text(old_cogs),
                    "profit_after_cogs_rub": _money_text(old_profit),
                    "margin_pct": _money_text(old_margin),
                    "before_cogs_profit_rub": _money_text(old_before_cogs),
                },
                "after": {
                    "cogs_rub": _money_text(new_cogs),
                    "profit_after_cogs_rub": _money_text(new_profit),
                    "margin_pct": _money_text(new_margin),
                    "before_cogs_profit_rub": _money_text(new_before_cogs),
                    "agent_remuneration_rub": new_metrics.get("agent_remuneration"),
                    "acquiring_rub": new_metrics.get("acquiring"),
                    "combined_commission_control_rub": new_metrics.get("combined_commission_control"),
                    "commission_control_reconciliation_rub": new_metrics.get("commission_control_reconciliation_rub"),
                    "capitalized_acceptance_rub": new_metrics.get("capitalized_acceptance"),
                    "capitalized_transit_rub": new_metrics.get("capitalized_transit_logistics"),
                },
                "delta": {
                    "cogs_rub": cogs_delta,
                    "profit_after_cogs_rub": profit_delta,
                    "margin_pct_points": margin_delta,
                    "before_cogs_profit_rub": before_cogs_delta,
                    "cogs_profit_effect_rub": cogs_profit_effect,
                },
                "profit_delta_inputs": {
                    "raw_fields": [
                        "docTypeName",
                        "retailPriceWithDisc",
                        "forPay",
                        "acquiringFee",
                        "additionalPayment",
                        "deliveryService",
                        "paidStorage",
                        "paidAcceptance",
                        "penalty",
                        "deduction",
                        "bonusTypeName",
                        "quantity",
                        "nmId/vendorCode/sku",
                        "giId",
                        "rrDate/saleDt/orderDt",
                    ],
                    "derived_fields": [
                        "agent_remuneration",
                        "acquiring",
                        "profit_period_expenses",
                        "capitalized_acceptance",
                        "capitalized_transit_logistics",
                        "positive_adjustments",
                        "cogs",
                    ],
                    "source_contracts": [
                        CLASSIFIER_VERSION,
                        PROFIT_METHOD_VERSION,
                        COST_METHOD_VERSION,
                        "sheet_vitrina_v1_wb_supply_cost_layers",
                        "sheet_vitrina_v1_warehouse_wb_daily_cost",
                    ],
                    "component_reconciliation": component_reconciliation,
                    "profit_identity": (
                        "profit_after_cogs_delta = before_cogs_profit_delta - cogs_delta"
                    ),
                },
                "profit_delta_explanation": profit_difference_explanation,
                "coverage": {
                    key: detailed.get(key)
                    for key in (
                        "matched_units",
                        "unmatched_units",
                        "coverage_pct",
                        "problem_skus",
                        "cost_state_hash",
                    )
                },
                "capitalization_reconciliation": new_metrics.get("capitalization_reconciliation"),
                "unknown_reasons": unknown,
            }
            weeks.append(week_item)
            if int(detailed["unmatched_units"]):
                blockers.append(
                    {
                        "code": "canonical_cost_coverage_incomplete",
                        "week_start": start.isoformat(),
                        "problem_skus": detailed["problem_skus"],
                    }
                )
            grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
            for detail in detailed["detail_rows"]:
                nm_id = str(detail["nm_id"])
                union_nm_ids.add(nm_id)
                key = (
                    nm_id,
                    str(detail["operation_date"]),
                    str(detail["source_date"]),
                    str(detail["unit_cost_rub"]),
                )
                item = grouped.setdefault(
                    key,
                    {
                        "week_start": start.isoformat(),
                        "week_end": end.isoformat(),
                        "nm_id": nm_id,
                        "operation_date": detail["operation_date"],
                        "canonical_source_date": detail["source_date"],
                        "unit_cost_rub": detail["unit_cost_rub"],
                        "source": detail["cost_source"],
                        "source_quality": detail["source_quality"],
                        "projection_quality": detail["projection_quality"],
                        "selection_method": detail["selection_method"],
                        "source_identity": detail["source_identity"],
                        "source_digest": detail["source_digest"],
                        "sales_qty": 0,
                        "returns_qty": 0,
                        "signed_cogs_rub": ZERO,
                        "reason": "canonical_exact_match",
                    },
                )
                if detail["movement"] == "sale":
                    item["sales_qty"] += int(detail["quantity"])
                else:
                    item["returns_qty"] += int(detail["quantity"])
                item["signed_cogs_rub"] += _decimal(detail["signed_cogs_rub"])
                cost_manifest[str(detail["source_identity"])] = {
                    "source_identity": detail["source_identity"],
                    "source_date": detail["source_date"],
                    "nm_id": nm_id,
                    "quality": detail["source_quality"],
                    "unit_cost_rub": detail["unit_cost_rub"],
                    "source_digest": detail["source_digest"],
                }
            for item in grouped.values():
                item["signed_cogs_rub"] = _money_text(item["signed_cogs_rub"])
                matrix.append(item)
            for problem in detailed["problem_skus"]:
                if problem.get("nm_id"):
                    union_nm_ids.add(str(problem["nm_id"]))
                matrix.append(
                    {
                        "week_start": start.isoformat(),
                        "week_end": end.isoformat(),
                        "nm_id": str(problem.get("nm_id") or problem.get("sku") or ""),
                        "operation_date": str(problem.get("operation_date") or ""),
                        "canonical_source_date": str(problem.get("canonical_source_date") or ""),
                        "sales_qty": int(problem.get("sales_qty") or 0),
                        "returns_qty": int(problem.get("returns_qty") or 0),
                        "unit_cost_rub": None,
                        "source": "canonical_our_wb_cost",
                        "source_quality": "missing",
                        "projection_quality": "missing",
                        "selection_method": "",
                        "signed_cogs_rub": None,
                        "reason": str(problem.get("reason") or "canonical_cost_missing"),
                    }
                )
            target_before_digest.add(
                {
                    "week_start": start.isoformat(),
                    "aggregate": dict(stored_aggregate) if stored_aggregate is not None else None,
                    "coverage": dict(stored_coverage) if stored_coverage is not None else None,
                    "sku_aggregates": stored_sku_aggregates,
                }
            )
            # The matrix and cost manifest above are the durable operation-level
            # evidence. Release detail objects, then reuse the already parsed
            # source rows for the independently rebuilt per-SKU projection.
            del detailed
            expected_sku_projections = self._rebuild_sku_week_aggregates(
                conn,
                week_start=start,
                week_end=end,
                raw_rows=stored_rows,
                global_metrics=new_metrics,
                calculated_at="",
                persist=False,
                parsed_rows=parsed,
            )
            del parsed
            expected_sku_projection_row_count += len(expected_sku_projections)
            target_after_digest.add(
                {
                    "week_start": start.isoformat(),
                    "metrics": new_metrics,
                    "coverage": new_coverage,
                    "unknown": unknown,
                    "sku_aggregates": expected_sku_projections,
                }
            )
        finance_digest = finance_manifest_digest.finish()
        cost_rows = [cost_manifest[key] for key in sorted(cost_manifest)]
        canonical_july_first_manifest: list[dict[str, Any]] = []
        for nm_id in sorted(
            union_nm_ids,
            key=_finance_nm_id_sort_key,
        ):
            resolution = self._resolve_canonical_cost(
                conn,
                nm_id=nm_id,
                operation_date=CANONICAL_COST_POLICY_DATE,
            )
            canonical_july_first_manifest.append(
                {
                    "nm_id": nm_id,
                    "status": str(resolution.get("status") or "missing"),
                    "reason": str(resolution.get("reason") or ""),
                    "source_date": str(resolution.get("canonical_source_date") or ""),
                    "unit_cost_rub": resolution.get("unit_cost_rub"),
                    "quality": str(resolution.get("quality") or "missing"),
                    "source_identity": str(
                        resolution.get("canonical_source_identity") or ""
                    ),
                    "source_digest": str(resolution.get("source_digest") or ""),
                }
            )
        cost_digest = self._json_digest(
            {
                "operation_sources": cost_rows,
                "canonical_2026_07_01": canonical_july_first_manifest,
            }
        )
        missing_canonical_cost_nm_ids = sorted(
            {
                str(item.get("nm_id") or "")
                for item in matrix
                if item.get("source_quality") == "missing" and item.get("nm_id")
            },
            key=_finance_nm_id_sort_key,
        )
        ads_manifest = self._ads_coverage_manifest(
            conn,
            nm_ids=sorted(union_nm_ids),
            date_from=scope_from,
            date_to=scope_to,
        )
        non_target_manifest = self._canonical_non_target_manifest(
            conn,
            date_from=scope_from,
            date_to=scope_to,
        )
        target_before_image = self._finance_target_images(conn, target_keys)
        if self._json_digest(target_before_image) != initial_target_before_image_digest:
            raise ValueError(
                "canonical Finance target changed during query-only planning; "
                "rerun the same fixed-cutoff plan"
            )
        final_source_dependency = (
            None
            if initial_source_dependency is None
            else self._finance_source_dependency_fingerprint(
                conn,
                target_keys=target_keys,
                force_reload=True,
            )
        )
        if (
            initial_source_dependency is not None
            and str(initial_source_dependency["digest"])
            != str(final_source_dependency["digest"])
        ):
            raise ValueError(
                "canonical Finance source dependency changed during query-only "
                "planning; rerun the same fixed-cutoff plan"
            )
        fbs_historical_correction = {
            "cutoff_date": scope_to.isoformat(),
            "primary": {
                "reason": "pooled_fbs_physical_wac",
                "order_count": len(fbs_primary_order_digests),
                "unit_count": fbs_primary_units,
                "sales_revenue_rub": _money_text(fbs_primary_revenue),
            },
            "fallback": {
                "reason": "same_nm_same_day_common_inventory_cost",
                "order_count": len(fbs_fallback_order_digests),
                "unit_count": fbs_fallback_units,
                "sales_revenue_rub": _money_text(fbs_fallback_revenue),
            },
            "remaining": {
                "order_count": fbs_remaining_orders,
                "unit_count": fbs_remaining_units,
                "sales_revenue_rub": _money_text(fbs_remaining_revenue),
                "reason_counts": dict(sorted(fbs_remaining_reasons.items())),
            },
        }
        plan: dict[str, Any] = {
            "status": "blocked" if blockers else "ready",
            "schema_version": "wb_finance_canonical_cost_backfill_v3",
            "dry_run": True,
            "seller_id": self.seller_id,
            "date_from": scope_from.isoformat(),
            "date_to": scope_to.isoformat(),
            "historical_cutoff": {
                "cutoff_date": scope_to.isoformat(),
                "forward_ingress_from": (scope_to + timedelta(days=1)).isoformat(),
                "frozen": True,
                "source_rows_after_cutoff_excluded": True,
            },
            "week_count": len(weeks),
            "finance_row_count": finance_manifest_digest.count,
            "finance_nm_id_count": len(union_nm_ids),
            "weeks": weeks,
            "week_nm_operation_date_matrix": matrix,
            "fbs_historical_correction": fbs_historical_correction,
            "source_manifests": {
                "finance": {"row_count": finance_manifest_digest.count, "digest": finance_digest},
                "cost": {
                    "source": "canonical Our WB Cost",
                    "row_count": len(cost_rows),
                    "digest": cost_digest,
                    "rows": cost_rows,
                    "canonical_2026_07_01_rows": [
                        row for row in cost_rows if row["source_date"] == "2026-07-01"
                    ],
                    "canonical_2026_07_01_manifest": {
                        "row_count": len(canonical_july_first_manifest),
                        "resolved_count": sum(
                            item["status"] == "resolved"
                            for item in canonical_july_first_manifest
                        ),
                        "missing_nm_ids": [
                            item["nm_id"]
                            for item in canonical_july_first_manifest
                            if item["status"] != "resolved"
                        ],
                        "digest": self._json_digest(canonical_july_first_manifest),
                        "rows": canonical_july_first_manifest,
                    },
                    "exact_date_rows_after_2026_07_01": [
                        row for row in cost_rows if row["source_date"] > "2026-07-01"
                    ],
                    "missing_nm_id_count": len(missing_canonical_cost_nm_ids),
                    "missing_nm_ids": missing_canonical_cost_nm_ids,
                },
                "ads": ads_manifest,
            },
            "source_dependency": final_source_dependency,
            "target_before_digest": target_before_digest.finish(),
            "target_before_image": target_before_image,
            "expected_target_after_digest": target_after_digest.finish(),
            "non_target_manifest": non_target_manifest,
            "non_target_digest": self._json_digest(non_target_manifest),
            "write_set": {
                "tables": [
                    "wb_finance_weekly_aggregates",
                    "wb_finance_weekly_cost_coverage",
                    "wb_finance_weekly_reconciliation",
                    "wb_finance_weekly_sku_aggregates",
                    "wb_finance_weekly_sync.status",
                    "wb_finance_projection_audit",
                ],
                "week_count": len(weeks),
                "retro_cost_map_rows": 0,
                "weeks": [
                    {"week_start": item["week_start"], "week_end": item["week_end"]}
                    for item in weeks
                ],
                "expected_sku_projection_row_count": expected_sku_projection_row_count,
            },
            "invariants": {
                "raw_finance_rows_immutable": True,
                "ads_rows_immutable": True,
                "ads_missing_never_written_as_zero": True,
                "canonical_cost_rows_immutable": True,
                "exact_date_cost_values_from_2026_07_01_unchanged": True,
                "fallback_average_created": False,
                "silent_zero_created": False,
                "legacy_cost_price_used": False,
                "retro_cost_map_read_or_written": False,
                "sku_aggregate_bound_to_target_readback": True,
            },
            "backup_recovery_plan": {
                "required_before_apply": True,
                "kind": "target_scoped_exact_before_image",
                "whole_store_copy": False,
                "bounded_by_fixed_cutoff": True,
                "before_image_digest": self._json_digest(target_before_image),
                "transaction": "short target CAS in BEGIN IMMEDIATE",
            },
            "blockers": blockers,
            "apply_allowed": not blockers,
            "human_approval_required": True,
            "revoked_fingerprints": [
                "sha256:621323d6f03759cb8685dfffe20639fa18a16c7b5f6a5b1685205a579c6bbf2d"
            ],
        }
        plan["fingerprint"] = self._json_digest(plan)
        return plan

    @staticmethod
    def _nullable_decimal(value: Any) -> Decimal | None:
        if value in (None, ""):
            return None
        try:
            result = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None
        return result if result.is_finite() else None

    @staticmethod
    def _delta_text(after: Decimal | None, before: Decimal | None) -> str | None:
        return _money_text(after - before) if after is not None and before is not None else None

    @staticmethod
    def _json_digest(value: Any) -> str:
        return "sha256:" + hashlib.sha256(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()

    def _canonical_non_target_manifest(
        self,
        conn: sqlite3.Connection,
        *,
        date_from: date,
        date_to: date,
    ) -> dict[str, Any]:
        manifests: dict[str, Any] = {}
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        queries = {
            "finance_raw": (
                "SELECT report_id,rrd_id,row_hash FROM wb_finance_weekly_raw_rows WHERE seller_id=? AND week_end>=? AND week_end<=? ORDER BY report_id,rrd_id",
                (self.seller_id, date_from.isoformat(), date_to.isoformat()),
            ),
        }
        if "sheet_vitrina_v1_warehouse_wb_daily_cost" in tables:
            queries["canonical_cost"] = (
                "SELECT cutover_id,as_of_date,nm_id,fingerprint FROM sheet_vitrina_v1_warehouse_wb_daily_cost WHERE as_of_date>=? AND as_of_date<=? ORDER BY as_of_date,nm_id",
                (CANONICAL_COST_POLICY_DATE.isoformat(), date_to.isoformat()),
            )
        else:
            manifests["canonical_cost"] = {
                "row_count": 0,
                "digest": self._json_digest([]),
                "missing_table": True,
            }
        if "temporal_source_slot_snapshots" in tables:
            queries["ads_snapshots"] = (
                "SELECT snapshot_date,captured_at,payload_json FROM temporal_source_slot_snapshots WHERE source_key='ads_compact' AND snapshot_date>=? AND snapshot_date<=? ORDER BY snapshot_date",
                (date_from.isoformat(), date_to.isoformat()),
            )
        if "sheet_vitrina_v1_wb_supply_cost_layers" in tables:
            queries["supply_cost_layers"] = (
                "SELECT wb_supply_cost_layer_id,wb_supply_id,nm_id,inputs_hash,version,is_current FROM sheet_vitrina_v1_wb_supply_cost_layers ORDER BY wb_supply_cost_layer_id",
                (),
            )
        for name, (query, params) in queries.items():
            digest = _StreamingJsonArrayDigest()
            for row in conn.execute(query, params):
                digest.add(dict(row))
            manifests[name] = {"row_count": digest.count, "digest": digest.finish()}
        return manifests

    def apply_canonical_finance_backfill(
        self,
        *,
        expected_fingerprint: str,
        approval_reference: str,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> dict[str, Any]:
        revoked = "sha256:621323d6f03759cb8685dfffe20639fa18a16c7b5f6a5b1685205a579c6bbf2d"
        if date_to is None:
            date_to = self.canonical_finance_historical_cutoff()
        if expected_fingerprint == revoked:
            raise ValueError("the former Finance plan fingerprint is permanently revoked")
        if not str(approval_reference or "").strip():
            raise ValueError("canonical Finance apply requires approval_reference")
        phase_started = datetime.now(timezone.utc)
        with self._connect_canonical_plan() as plan_conn:
            self._assert_readonly_plan_connection(plan_conn)
            existing = plan_conn.execute(
                """SELECT scope_json,result_json FROM wb_finance_projection_audit
                   WHERE seller_id=? AND action='apply_canonical_finance_backfill'
                     AND fingerprint=?
                   ORDER BY CASE
                              WHEN instr(result_json,'\"post_apply_fingerprint\"')>0
                              THEN 1 ELSE 0
                            END DESC,
                            rowid DESC
                   LIMIT 1""",
                (self.seller_id, expected_fingerprint),
            ).fetchone()
            if existing is not None:
                result = json.loads(str(existing["result_json"] or "{}"))
                prior_scope = json.loads(str(existing["scope_json"] or "{}"))
                current = self._plan_canonical_finance_backfill_in_connection(
                    plan_conn,
                    date_from=date_from,
                    date_to=date_to,
                )
                current_scope = {
                    "date_from": current["date_from"],
                    "date_to": current["date_to"],
                }
                if current_scope != prior_scope:
                    raise ValueError(
                        "previously applied canonical Finance fingerprint belongs to a different scope"
                    )
                if (
                    str(result.get("status") or "")
                    == "applied_pending_query_only_reconciliation"
                    and not result.get("post_apply_fingerprint")
                ):
                    target_keys = {
                        (
                            self.seller_id,
                            str(item["week_start"]),
                            str(item["week_end"]),
                        )
                        for item in current["weeks"]
                    }
                    if self._json_digest(
                        self._finance_target_images(plan_conn, target_keys)
                    ) != str(result.get("target_digest") or ""):
                        raise ValueError(
                            "committed canonical Finance target no longer matches its "
                            "atomic apply audit"
                        )
                    non_target_after = self._json_digest(
                        self._canonical_non_target_manifest(
                            plan_conn,
                            date_from=date.fromisoformat(current_scope["date_from"]),
                            date_to=date.fromisoformat(current_scope["date_to"]),
                        )
                    )
                    if non_target_after != str(
                        result.get("non_target_digest_before") or ""
                    ):
                        raise ValueError(
                            "non-target Finance/ads/cost state changed after the "
                            "atomically recorded apply"
                        )
                    if current["blockers"] or any(
                        any(
                            value not in {None, "0.0000"}
                            for value in week["delta"].values()
                        )
                        for week in current["weeks"]
                    ):
                        raise ValueError(
                            "atomically recorded canonical Finance apply does not "
                            "reconcile to zero"
                        )
                    reconciled_at = (
                        self.now_factory()
                        .astimezone(timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z")
                    )
                    result = {
                        **result,
                        "status": "applied_reconciled_from_atomic_audit",
                        "non_target_digest_after": non_target_after,
                        "post_apply_fingerprint": current["fingerprint"],
                        "reconciled_at": reconciled_at,
                        "idempotent": True,
                    }
                    audit_id = hashlib.sha256(
                        f"{self.seller_id}|canonical-reconciliation|{expected_fingerprint}".encode(
                            "utf-8"
                        )
                    ).hexdigest()
                    with self._connect() as audit_conn:
                        audit_conn.execute("BEGIN IMMEDIATE")
                        try:
                            audit_conn.execute(
                                """INSERT OR IGNORE INTO wb_finance_projection_audit(
                                   audit_id,seller_id,action,fingerprint,scope_json,
                                   result_json,created_at
                                   ) VALUES(?,?,?,?,?,?,?)""",
                                (
                                    audit_id,
                                    self.seller_id,
                                    "apply_canonical_finance_backfill",
                                    expected_fingerprint,
                                    json.dumps(
                                        current_scope, separators=(",", ":")
                                    ),
                                    json.dumps(
                                        result,
                                        ensure_ascii=False,
                                        sort_keys=True,
                                        separators=(",", ":"),
                                    ),
                                    reconciled_at,
                                ),
                            )
                            audit_conn.commit()
                        except Exception:
                            audit_conn.rollback()
                            raise
                    return result
                if (
                    not result.get("post_apply_fingerprint")
                    or str(current["fingerprint"]) != str(result["post_apply_fingerprint"])
                ):
                    raise ValueError(
                        "previously applied canonical Finance state has drifted; a new dry-run and approval are required"
                    )
                return {**result, "status": "no_op_already_applied", "idempotent": True}
            plan = self._plan_canonical_finance_backfill_in_connection(
                plan_conn,
                date_from=date_from,
                date_to=date_to,
            )
            if str(plan["fingerprint"]) != expected_fingerprint:
                raise ValueError("canonical Finance plan fingerprint drifted before apply")
            if not bool(plan["apply_allowed"]):
                raise ValueError("canonical Finance plan contains blockers")
            target_keys = {
                (self.seller_id, str(item["week_start"]), str(item["week_end"]))
                for item in plan["weeks"]
            }
            if not target_keys:
                raise ValueError("canonical Finance reviewed plan has no target weeks")
            after_images: dict[str, Any] = {}
            for item in plan["weeks"]:
                projection = self._build_week_target_projection(
                    plan_conn,
                    week_start=date.fromisoformat(str(item["week_start"])),
                    week_end=date.fromisoformat(str(item["week_end"])),
                )
                for table, image in dict(projection["images"]).items():
                    selected = after_images.setdefault(
                        table,
                        {"columns": list(image["columns"]), "rows": []},
                    )
                    if list(selected["columns"]) != list(image["columns"]):
                        raise ValueError("Finance query projection schema drifted")
                    selected["rows"].extend(list(image["rows"]))
            after_images = self._canonicalize_finance_target_images(
                plan_conn, after_images
            )
            current_before = self._finance_target_images(plan_conn, target_keys)
            before_digest = self._json_digest(current_before)
            if before_digest != str(
                plan["backup_recovery_plan"]["before_image_digest"]
            ):
                raise ValueError("canonical Finance target before-image drifted")
            non_target_before = str(plan["non_target_digest"])
            handoff_data_version = self._sqlite_data_version_token(plan_conn)
            plan_finished = datetime.now(timezone.utc)
            committed_at = (
                self.now_factory()
                .astimezone(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
            committed_target_digest = self._json_digest(after_images)
            commit_audit_id = hashlib.sha256(
                f"{self.seller_id}|canonical-commit|{expected_fingerprint}".encode(
                    "utf-8"
                )
            ).hexdigest()
            committed_result = {
                "status": "applied_pending_query_only_reconciliation",
                "runtime_mutation": True,
                "fingerprint": expected_fingerprint,
                "approval_reference": str(approval_reference),
                "historical_cutoff": dict(plan["historical_cutoff"]),
                "fbs_historical_correction": dict(
                    plan["fbs_historical_correction"]
                ),
                "week_count": int(plan["week_count"]),
                "non_target_digest_before": non_target_before,
                "target_digest": committed_target_digest,
                "idempotent": True,
                "retro_cost_map_rows_written": 0,
                "applied_at": committed_at,
            }

            with self._connect() as writer_conn:
                writer_started = datetime.now(timezone.utc)
                writer_conn.execute("BEGIN IMMEDIATE")
                try:
                    if self._sqlite_data_version_token(plan_conn) != handoff_data_version:
                        raise ValueError(
                            "Finance SQLite changed during the pre-commit handoff; "
                            "the same fixed-cutoff plan must be query-only revalidated"
                        )
                    if self._json_digest(
                        self._finance_target_images(writer_conn, target_keys)
                    ) != before_digest:
                        raise ValueError("canonical Finance target changed before CAS")
                    self._replace_finance_target_images(
                        writer_conn,
                        target_keys=target_keys,
                        images=after_images,
                    )
                    applied_images = self._finance_target_images(
                        writer_conn, target_keys
                    )
                    if self._json_digest(applied_images) != self._json_digest(after_images):
                        raise ValueError("canonical Finance target CAS readback differs")
                    # Commit the exact-target image and its immutable apply
                    # identity atomically.  If transport or post-commit
                    # reconciliation later fails, query-only readback can
                    # prove this one apply and callers must never resubmit it.
                    writer_conn.execute(
                        """INSERT INTO wb_finance_projection_audit(
                           audit_id,seller_id,action,fingerprint,scope_json,
                           result_json,created_at
                           ) VALUES(?,?,?,?,?,?,?)""",
                        (
                            commit_audit_id,
                            self.seller_id,
                            "apply_canonical_finance_backfill",
                            expected_fingerprint,
                            json.dumps(
                                {
                                    "date_from": plan["date_from"],
                                    "date_to": plan["date_to"],
                                },
                                separators=(",", ":"),
                            ),
                            json.dumps(
                                committed_result,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            committed_at,
                        ),
                    )
                    writer_conn.commit()
                except Exception:
                    writer_conn.rollback()
                    raise
                writer_finished = datetime.now(timezone.utc)

        readback_started = datetime.now(timezone.utc)
        with self._connect_canonical_plan() as readback_conn:
            self._assert_readonly_plan_connection(readback_conn)
            non_target_after = self._json_digest(
                self._canonical_non_target_manifest(
                    readback_conn,
                    date_from=date.fromisoformat(str(plan["date_from"])),
                    date_to=date.fromisoformat(str(plan["date_to"])),
                )
            )
            if non_target_after != non_target_before:
                raise ValueError("non-target Finance/ads/cost invariants changed during apply")
            post_apply_plan = self._plan_canonical_finance_backfill_in_connection(
                readback_conn,
                date_from=date.fromisoformat(str(plan["date_from"])),
                date_to=date.fromisoformat(str(plan["date_to"])),
            )
            if post_apply_plan["blockers"] or any(
                any(value not in {None, "0.0000"} for value in week["delta"].values())
                for week in post_apply_plan["weeks"]
            ):
                raise ValueError("post-apply canonical Finance plan is not reconciled to zero")
        readback_finished = datetime.now(timezone.utc)
        now = self.now_factory().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        result = {
            "status": "applied",
            "runtime_mutation": True,
            "fingerprint": expected_fingerprint,
            "approval_reference": str(approval_reference),
            "historical_cutoff": dict(plan["historical_cutoff"]),
            "fbs_historical_correction": dict(plan["fbs_historical_correction"]),
            "week_count": int(plan["week_count"]),
            "non_target_digest_before": non_target_before,
            "non_target_digest_after": non_target_after,
            "target_digest": committed_target_digest,
            "post_apply_fingerprint": post_apply_plan["fingerprint"],
            "idempotent": True,
            "retro_cost_map_rows_written": 0,
            "applied_at": now,
            "phase_timings_ms": {
                "query_plan_and_projection": max(
                    0.0, (plan_finished - phase_started).total_seconds() * 1000
                ),
                "writer_lock_hold": max(
                    0.0, (writer_finished - writer_started).total_seconds() * 1000
                ),
                "post_commit_readback": max(
                    0.0, (readback_finished - readback_started).total_seconds() * 1000
                ),
            },
        }
        audit_id = hashlib.sha256(
            f"{self.seller_id}|canonical-reconciliation|{expected_fingerprint}".encode(
                "utf-8"
            )
        ).hexdigest()
        with self._connect() as audit_conn:
            audit_conn.execute("BEGIN IMMEDIATE")
            try:
                audit_conn.execute(
                    """INSERT INTO wb_finance_projection_audit(
                       audit_id,seller_id,action,fingerprint,scope_json,result_json,created_at
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        audit_id,
                        self.seller_id,
                        "apply_canonical_finance_backfill",
                        expected_fingerprint,
                        json.dumps(
                            {"date_from": plan["date_from"], "date_to": plan["date_to"]},
                            separators=(",", ":"),
                        ),
                        json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                        now,
                    ),
                )
                audit_conn.commit()
            except Exception:
                audit_conn.rollback()
                raise
        return result

    def canonical_finance_fingerprint_applied(self, *, fingerprint: str) -> bool:
        """Return whether this exact reviewed canonical plan already committed."""

        self.ensure_schema()
        with self._connect() as conn:
            return (
                conn.execute(
                    """SELECT 1 FROM wb_finance_projection_audit
                       WHERE seller_id=? AND action='apply_canonical_finance_backfill'
                         AND fingerprint=? LIMIT 1""",
                    (self.seller_id, str(fingerprint)),
                ).fetchone()
                is not None
            )

    def apply_business_approved_backfill(
        self,
        *,
        expected_fingerprint: str,
        approval_reference: str,
        date_from: date = RETRO_COST_FIRST_WEEK_START,
        date_to: date | None = None,
    ) -> dict[str, Any]:
        raise ValueError(
            "legacy business-approved Finance apply and all former fingerprints are permanently revoked"
        )
        approval_reference = str(approval_reference or "").strip()
        if not approval_reference:
            raise ValueError("business-approved Finance apply requires approval_reference")
        latest_closed = week_bounds(self.now_factory().astimezone(MOSCOW).date())[0] - timedelta(days=1)
        scope_end = date_to or latest_closed
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                prior_apply = conn.execute(
                    """SELECT audit_id,scope_json,result_json
                       FROM wb_finance_projection_audit
                       WHERE seller_id=? AND action='apply_business_approved_backfill'
                         AND fingerprint=? ORDER BY created_at DESC LIMIT 1""",
                    (self.seller_id, expected_fingerprint),
                ).fetchone()
                plan = self._plan_business_approved_backfill_in_connection(
                    conn,
                    date_from=date_from,
                    date_to=scope_end,
                )
                if prior_apply is not None:
                    prior_scope = json.loads(str(prior_apply["scope_json"] or "{}"))
                    expected_scope = {
                        "date_from": date_from.isoformat(),
                        "date_to": scope_end.isoformat(),
                    }
                    if prior_scope != expected_scope:
                        raise ValueError("reviewed fingerprint was applied for a different scope")
                    if plan["blockers"] or int(plan["target_week_count"]) != 0:
                        raise ValueError(
                            "previously applied Finance projection no longer reconciles; new dry-run is required"
                        )
                    prior_result = json.loads(str(prior_apply["result_json"] or "{}"))
                    conn.rollback()
                    return {
                        "status": "already_current",
                        "runtime_mutation": False,
                        "fingerprint": expected_fingerprint,
                        "retro_cost_rows_inserted": 0,
                        "recalculated_week_count": 0,
                        "weeks": [],
                        "target_before_digest": prior_result.get("target_after_digest", ""),
                        "target_after_digest": prior_result.get("target_after_digest", ""),
                        "non_target_digest_before": plan["non_target_digest"],
                        "non_target_digest_after": plan["non_target_digest"],
                        "non_target_preserved": True,
                        "post_apply_target_week_count": 0,
                        "audit_id": str(prior_apply["audit_id"]),
                        "approval_reference": str(
                            prior_result.get("approval_reference") or approval_reference
                        ),
                    }
                if str(plan["fingerprint"]) != expected_fingerprint:
                    raise ValueError(
                        "business-approved Finance plan fingerprint changed before apply"
                    )
                if not bool(plan["apply_allowed"]):
                    raise ValueError("business-approved Finance plan contains blockers")
                now = self.now_factory().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                for row in plan["source_manifests"]["cost"]["rows"]:
                    conn.execute(
                        """INSERT INTO wb_finance_retro_cost_map(
                           seller_id,nm_id,unit_cost_rub,source_date,source_table,
                           source_row_json,source_row_sha256,source_calculation_fingerprint,
                           selection_method,formula_version,status,created_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            row["seller_id"],
                            row["nm_id"],
                            row["unit_cost_rub"],
                            row["source_date"],
                            row["source_table"],
                            row["source_row_json"],
                            row["source_row_sha256"],
                            row["source_calculation_fingerprint"],
                            row["selection_method"],
                            row["formula_version"],
                            row["status"],
                            now,
                        ),
                    )
                target_keys = {
                    (self.seller_id, item["week_start"], item["week_end"])
                    for item in plan["weeks"]
                }
                recalculated: list[dict[str, Any]] = []
                for item in plan["weeks"]:
                    start = date.fromisoformat(item["week_start"])
                    end = date.fromisoformat(item["week_end"])
                    metrics = self._recalculate_week_in_connection(conn, start, end)
                    recalculated.append(
                        {
                            "week_start": item["week_start"],
                            "week_end": item["week_end"],
                            "cogs": metrics["cogs"],
                            "profit_after_cogs": metrics["profit_after_cogs"],
                            "final_margin_pct": metrics["final_margin_pct"],
                        }
                    )
                non_target_after = self._finance_state_digest(
                    conn,
                    target_keys=target_keys,
                    target_only=False,
                )
                if non_target_after != plan["non_target_digest"]:
                    raise ValueError("non-target Finance state changed during backfill")
                readback = self._plan_business_approved_backfill_in_connection(
                    conn,
                    date_from=date_from,
                    date_to=scope_end,
                )
                if int(readback["target_week_count"]) != 0:
                    raise ValueError("post-apply Finance readback still contains changed weeks")
                if readback["blockers"]:
                    raise ValueError("post-apply Finance readback contains blockers")
                target_after_digest = self._finance_state_digest(
                    conn,
                    target_keys=target_keys,
                    target_only=True,
                )
                audit_result = {
                    "recalculated": recalculated,
                    "target_before_digest": plan["target_before_digest"],
                    "target_after_digest": target_after_digest,
                    "non_target_digest": non_target_after,
                    "post_apply_target_week_count": 0,
                    "approval_reference": approval_reference,
                }
                audit_id = hashlib.sha256(
                    f"{self.seller_id}|{expected_fingerprint}|{now}".encode("utf-8")
                ).hexdigest()
                conn.execute(
                    """INSERT INTO wb_finance_projection_audit(
                       audit_id,seller_id,action,fingerprint,scope_json,result_json,created_at
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        audit_id,
                        self.seller_id,
                        "apply_business_approved_backfill",
                        expected_fingerprint,
                        json.dumps(
                            {"date_from": date_from.isoformat(), "date_to": scope_end.isoformat()},
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        json.dumps(audit_result, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                        now,
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {
            "status": "already_current" if not recalculated else "applied",
            "runtime_mutation": bool(recalculated),
            "fingerprint": expected_fingerprint,
            "retro_cost_rows_inserted": len(plan["source_manifests"]["cost"]["rows"]),
            "recalculated_week_count": len(recalculated),
            "weeks": recalculated,
            "target_before_digest": plan["target_before_digest"],
            "target_after_digest": target_after_digest,
            "non_target_digest_before": non_target_before_apply,
            "non_target_digest_after": non_target_after,
            "non_target_preserved": True,
            "post_apply_target_week_count": 0,
            "audit_id": audit_id,
            "approval_reference": approval_reference,
        }

    def business_approved_fingerprint_applied(
        self,
        *,
        fingerprint: str,
        date_from: date,
        date_to: date,
    ) -> bool:
        """Return whether the exact reviewed fingerprint already passed this scope."""

        self.ensure_schema()
        expected_scope = {
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
        }
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT scope_json FROM wb_finance_projection_audit
                   WHERE seller_id=? AND action='apply_business_approved_backfill'
                     AND fingerprint=? ORDER BY created_at DESC""",
                (self.seller_id, fingerprint),
            ).fetchall()
        return any(
            json.loads(str(row["scope_json"] or "{}")) == expected_scope for row in rows
        )

    def recalculate_stale_cost_weeks(self) -> dict[str, Any]:
        """Rebuild forward-ingress weeks whose canonical derived state changed."""
        self.ensure_schema()
        plan = self.plan_stale_cost_weeks(
            date_from=FBS_FINANCE_FORWARD_INGRESS_DATE
        )
        return self.apply_stale_cost_weeks(
            expected_fingerprint=str(plan["fingerprint"]),
            date_from=FBS_FINANCE_FORWARD_INGRESS_DATE,
        )

    def plan_stale_cost_weeks(
        self,
        *,
        date_from: date = FBS_FINANCE_FORWARD_INGRESS_DATE,
        date_to: date | None = None,
    ) -> dict[str, Any]:
        """Build a read-only, fingerprinted plan for stale derived Finance weeks."""
        if not self.db_path.is_file():
            raise ValueError(f"Finance runtime SQLite does not exist: {self.db_path}")
        if date_to is not None and date_to < date_from:
            raise ValueError("date_to must not be earlier than date_from")
        with self._connect_stale_cost_plan() as conn:
            self._assert_readonly_plan_connection(conn)
            return self._plan_stale_cost_weeks_in_connection(
                conn, date_from=date_from, date_to=date_to
            )

    def _plan_stale_cost_weeks_in_connection(
        self,
        conn: sqlite3.Connection,
        *,
        date_from: date,
        date_to: date | None,
    ) -> dict[str, Any]:
        candidates = conn.execute(
            """SELECT DISTINCT raw.week_start,raw.week_end,
                       COALESCE(coverage.cost_state_hash,'') AS stored_cost_state_hash,
                       COALESCE(aggregate.classifier_version,'') AS stored_classifier_version,
                       COALESCE(aggregate.metrics_json,'') AS stored_metrics_json
                FROM wb_finance_weekly_raw_rows AS raw
                LEFT JOIN wb_finance_weekly_cost_coverage AS coverage
                 ON coverage.seller_id=raw.seller_id
                 AND coverage.week_start=raw.week_start
                 AND coverage.week_end=raw.week_end
                LEFT JOIN wb_finance_weekly_aggregates AS aggregate
                  ON aggregate.seller_id=raw.seller_id
                 AND aggregate.week_start=raw.week_start
                 AND aggregate.week_end=raw.week_end
                WHERE raw.seller_id=? AND raw.week_end>=?
                  AND (? IS NULL OR raw.week_start<=?)
                ORDER BY raw.week_start""",
            (
                self.seller_id,
                date_from.isoformat(),
                date_to.isoformat() if date_to is not None else None,
                date_to.isoformat() if date_to is not None else None,
            ),
        ).fetchall()
        stale: list[dict[str, Any]] = []
        for candidate in candidates:
            start = date.fromisoformat(candidate["week_start"])
            end = date.fromisoformat(candidate["week_end"])
            raw_rows = conn.execute(
                """SELECT report_id,rrd_id,row_hash,raw_json
                    FROM wb_finance_weekly_raw_rows
                    WHERE seller_id=? AND week_start=? AND week_end=?
                    ORDER BY report_id,rrd_id""",
                (self.seller_id, start.isoformat(), end.isoformat()),
            ).fetchall()
            rows = [json.loads(row["raw_json"]) for row in raw_rows]
            aggregate, current, unknown = self._aggregate_rows(conn, rows, start)
            try:
                stored_metrics = json.loads(str(candidate["stored_metrics_json"] or "{}"))
            except json.JSONDecodeError:
                stored_metrics = {}
            stored_metrics_digest = self._json_digest(stored_metrics)
            expected_metrics_digest = self._json_digest(aggregate)
            if (
                current["cost_state_hash"] == candidate["stored_cost_state_hash"]
                and str(candidate["stored_classifier_version"]) == CLASSIFIER_VERSION
                and stored_metrics_digest == expected_metrics_digest
            ):
                continue
            raw_digest = hashlib.sha256(
                json.dumps(
                    [
                        [
                            row["report_id"],
                            row["rrd_id"],
                            row["row_hash"],
                            hashlib.sha256(
                                str(row["raw_json"]).encode("utf-8")
                            ).hexdigest(),
                        ]
                        for row in raw_rows
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            report_rows = conn.execute(
                """SELECT report_id,report_type,content_hash,row_count
                FROM wb_finance_weekly_reports
                WHERE seller_id=? AND week_start=? AND week_end=?
                ORDER BY report_id""",
                (self.seller_id, start.isoformat(), end.isoformat()),
            ).fetchall()
            report_digest = hashlib.sha256(
                json.dumps(
                    [list(row) for row in report_rows],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            stale.append(
                {
                    "week_start": start.isoformat(),
                    "week_end": end.isoformat(),
                    "stored_cost_state_hash": candidate["stored_cost_state_hash"],
                    "expected_cost_state_hash": current["cost_state_hash"],
                    "stored_classifier_version": str(candidate["stored_classifier_version"]),
                    "expected_classifier_version": CLASSIFIER_VERSION,
                    "stored_metrics_digest": stored_metrics_digest,
                    "expected_metrics_digest": expected_metrics_digest,
                    "raw_digest": f"sha256:{raw_digest}",
                    "raw_row_count": len(raw_rows),
                    "report_digest": f"sha256:{report_digest}",
                    "report_count": len(report_rows),
                    "expected": {
                        "cogs": aggregate["cogs"],
                        "profit_after_cogs": aggregate["profit_after_cogs"],
                        "final_margin_pct": aggregate["final_margin_pct"],
                        "matched_units": current["matched_units"],
                        "unmatched_units": current["unmatched_units"],
                        "problem_skus": current["problem_skus"],
                        "quality": current["quality"],
                        "unknown_reasons": unknown,
                    },
                }
            )
        target_keys = {
            (self.seller_id, str(item["week_start"]), str(item["week_end"]))
            for item in stale
        }
        source_dependency = self._finance_source_dependency_fingerprint(
            conn,
            target_keys=target_keys,
        )
        plan: dict[str, Any] = {
            "schema_version": "wb_finance_stale_cost_recalculation_v1",
            "status": "dry_run",
            "runtime_mutation": False,
            "apply_allowed": True,
            "blockers": [],
            "seller_id": self.seller_id,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat() if date_to is not None else None,
            "checked_week_count": len(candidates),
            "stale_week_count": len(stale),
            "weeks": stale,
            "source_dependency": source_dependency,
            "target_before_digest": self._finance_state_digest(
                conn, target_keys=target_keys, target_only=True
            ),
            "target_before_image_digest": self._json_digest(
                self._finance_target_images(conn, target_keys)
            ),
            "non_target_digest": self._finance_state_digest(
                conn, target_keys=target_keys, target_only=False
            ),
        }
        plan["fingerprint"] = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    plan,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        )
        return plan

    def apply_stale_cost_weeks(
        self,
        *,
        expected_fingerprint: str,
        date_from: date = FBS_FINANCE_FORWARD_INGRESS_DATE,
        date_to: date | None = None,
    ) -> dict[str, Any]:
        """Build and verify a query-only projection, then run one short CAS."""

        phase_started = datetime.now(timezone.utc)
        with self._connect_stale_cost_plan() as plan_conn:
            self._assert_readonly_plan_connection(plan_conn)
            plan = self._plan_stale_cost_weeks_in_connection(
                plan_conn, date_from=date_from, date_to=date_to
            )
            if str(plan["fingerprint"]) != expected_fingerprint:
                raise ValueError("stale Finance cost plan fingerprint changed before apply")
            target_keys = {
                (self.seller_id, str(item["week_start"]), str(item["week_end"]))
                for item in plan["weeks"]
            }
            if not target_keys:
                query_plan_ms = max(
                    0.0,
                    (
                        datetime.now(timezone.utc) - phase_started
                    ).total_seconds()
                    * 1000,
                )
                return {
                    "status": "already_current",
                    "runtime_mutation": False,
                    "fingerprint": expected_fingerprint,
                    "checked_week_count": plan["checked_week_count"],
                    "recalculated_week_count": 0,
                    "weeks": [],
                    "non_target_digest_before": plan["non_target_digest"],
                    "non_target_digest_after": plan["non_target_digest"],
                    "non_target_preserved": True,
                    "post_verify_stale_week_count": 0,
                    "phase_timings_ms": {
                        "query_plan": query_plan_ms,
                        "query_projection": 0,
                        "dependency_verify": 0,
                        "writer_lock_hold": 0,
                        "post_commit_readback": 0,
                    },
                }
            snapshot_started = datetime.now(timezone.utc)
            recalculated: list[dict[str, Any]] = []
            after_images: dict[str, Any] = {}
            for item in plan["weeks"]:
                start = date.fromisoformat(str(item["week_start"]))
                end = date.fromisoformat(str(item["week_end"]))
                projection = self._build_week_target_projection(
                    plan_conn,
                    week_start=start,
                    week_end=end,
                )
                metrics = dict(projection["metrics"])
                coverage = dict(projection["coverage"])
                if (
                    str(coverage["cost_state_hash"])
                    != str(item["expected_cost_state_hash"])
                    or self._json_digest(metrics)
                    != str(item["expected_metrics_digest"])
                ):
                    raise ValueError(
                        "query projection differs from the reviewed stale Finance plan"
                    )
                for table, image in dict(projection["images"]).items():
                    selected = after_images.setdefault(
                        table,
                        {"columns": list(image["columns"]), "rows": []},
                    )
                    if list(selected["columns"]) != list(image["columns"]):
                        raise ValueError("Finance query projection schema drifted")
                    selected["rows"].extend(list(image["rows"]))
                recalculated.append(
                    {
                        "week_start": start.isoformat(),
                        "week_end": end.isoformat(),
                        "cost_state_hash": item["expected_cost_state_hash"],
                        "cogs": metrics["cogs"],
                    }
                )
            after_images = self._canonicalize_finance_target_images(
                plan_conn, after_images
            )
            snapshot_finished = datetime.now(timezone.utc)

            dependency_started = datetime.now(timezone.utc)
            fresh_source_dependency = self._finance_source_dependency_fingerprint(
                plan_conn,
                target_keys=target_keys,
                force_reload=True,
            )
            if (
                str(fresh_source_dependency["digest"])
                != str(plan["source_dependency"]["digest"])
            ):
                raise ValueError(
                    "Finance exact dependency changed after snapshot planning; rebuild the plan"
                )
            if self._json_digest(
                self._finance_target_images(plan_conn, target_keys)
            ) != str(plan["target_before_image_digest"]):
                raise ValueError(
                    "Finance target changed after snapshot planning; rebuild the plan"
                )
            non_target_before_apply = self._finance_state_digest(
                plan_conn,
                target_keys=target_keys,
                target_only=False,
            )
            handoff_data_version = self._sqlite_data_version_token(plan_conn)
            dependency_finished = datetime.now(timezone.utc)

            with self._connect() as writer_conn:
                writer_started = datetime.now(timezone.utc)
                writer_conn.execute("BEGIN IMMEDIATE")
                try:
                    # The exact dependency digest above is deliberately built
                    # on the query-only connection.  Once the writer lock is
                    # held, a data-version handshake on that same observer
                    # closes the small handoff race without repeating any
                    # source scan inside the blocking transaction.
                    if self._sqlite_data_version_token(plan_conn) != handoff_data_version:
                        raise ValueError(
                            "Finance SQLite source changed during snapshot-to-writer handoff; rebuild the plan"
                        )
                    if self._json_digest(
                        self._finance_target_images(writer_conn, target_keys)
                    ) != str(plan["target_before_image_digest"]):
                        raise ValueError(
                            "Finance target changed after snapshot planning; rebuild the plan"
                        )
                    self._replace_finance_target_images(
                        writer_conn,
                        target_keys=target_keys,
                        images=after_images,
                    )
                    applied_images = self._finance_target_images(
                        writer_conn, target_keys
                    )
                    target_image_digest = self._json_digest(after_images)
                    if self._json_digest(applied_images) != target_image_digest:
                        raise ValueError(
                            "Finance target CAS readback differs from snapshot"
                        )
                    writer_conn.commit()
                except Exception:
                    writer_conn.rollback()
                    raise
                writer_finished = datetime.now(timezone.utc)

        with self._connect_stale_cost_plan() as conn:
            self._assert_readonly_plan_connection(conn)
            non_target_after = self._finance_state_digest(
                conn, target_keys=target_keys, target_only=False
            )
            if non_target_after != non_target_before_apply:
                raise ValueError("non-target Finance state changed during scoped apply")
            post_source_dependency = self._finance_source_dependency_fingerprint(
                conn,
                target_keys=target_keys,
                force_reload=True,
            )
            post_verify = self._plan_stale_cost_weeks_in_connection(
                conn, date_from=date_from, date_to=date_to
            )
            source_advanced = (
                str(post_source_dependency["digest"])
                != str(plan["source_dependency"]["digest"])
            )
            if int(post_verify["stale_week_count"]) != 0 and not source_advanced:
                raise ValueError("post-recalculation verification still contains stale weeks")
        phase_finished = datetime.now(timezone.utc)
        milliseconds = lambda start, end: max(
            0.0, (end - start).total_seconds() * 1000
        )
        return {
            "status": "already_current" if not recalculated else "applied",
            "runtime_mutation": bool(recalculated),
            "fingerprint": expected_fingerprint,
            "checked_week_count": plan["checked_week_count"],
            "recalculated_week_count": len(recalculated),
            "weeks": recalculated,
            "planned_non_target_digest": plan["non_target_digest"],
            "non_target_digest_before": non_target_before_apply,
            "non_target_digest_after": non_target_after,
            "non_target_preserved": True,
            "post_verify_stale_week_count": int(post_verify["stale_week_count"]),
            "source_dependency": plan["source_dependency"],
            "post_source_dependency": post_source_dependency,
            "source_advanced_after_apply": source_advanced,
            "target_image_digest": target_image_digest,
            "phase_timings_ms": {
                "query_plan": milliseconds(
                    phase_started, snapshot_started
                ),
                "query_projection": milliseconds(
                    snapshot_started, snapshot_finished
                ),
                "dependency_verify": milliseconds(
                    dependency_started, dependency_finished
                ),
                "writer_lock_hold": milliseconds(writer_started, writer_finished),
                "post_commit_readback": milliseconds(writer_finished, phase_finished),
            },
        }

    def _finance_source_dependency_fingerprint(
        self,
        conn: sqlite3.Connection,
        *,
        target_keys: set[tuple[str, str, str]],
        force_reload: bool = False,
    ) -> dict[str, Any]:
        """Fingerprint only inputs that can alter the reviewed target images.

        A global ``PRAGMA data_version`` also changes for unrelated UI/status
        writers and made every multi-minute query projection lose its CAS. The
        exact dependency fingerprint covers only canonical WB/FBS rows
        reachable from the target operations, nomenclature routing, and the
        raw/report rows of the target weeks. It is built and rechecked on the
        query-only connection before the short writer boundary. A new cost for
        another date/SKU/order is not a target dependency and therefore cannot
        starve this CAS.
        """

        if force_reload or self._canonical_cost_snapshot_connection is not conn:
            snapshot = CanonicalChannelCostSnapshot.from_connection(conn)
        else:
            snapshot = self._canonical_cost_snapshot
            if snapshot is None:
                snapshot = CanonicalChannelCostSnapshot.from_connection(conn)
        digest = hashlib.sha256()
        counts: dict[str, int] = {}

        def add(kind: str, identity: Any, payload: Any) -> None:
            counts[kind] = counts.get(kind, 0) + 1
            digest.update(
                json.dumps(
                    [kind, identity, payload],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            )
            digest.update(b"\n")

        alias_to_nm, ambiguous_aliases, _groups, _items = (
            _nomenclature_identity_index(conn)
        )
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='sheet_vitrina_v1_nomenclature_items'"
        ).fetchone()
        if table_exists is not None:
            for row in conn.execute(
                "SELECT * FROM sheet_vitrina_v1_nomenclature_items "
                "ORDER BY nm_id,rowid"
            ).fetchall():
                add("nomenclature", str(row["nm_id"] or ""), dict(row))

        relevant_wb_keys: set[tuple[str, str]] = set()
        relevant_wb_nm_ids: set[int] = set()
        relevant_fbs_cost_keys: set[tuple[str, str]] = set()
        for seller_id, week_start, week_end in sorted(target_keys):
            raw_rows = conn.execute(
                "SELECT report_id,rrd_id,row_hash,raw_json "
                "FROM wb_finance_weekly_raw_rows WHERE seller_id=? "
                "AND week_start=? AND week_end=? ORDER BY report_id,rrd_id",
                (seller_id, week_start, week_end),
            ).fetchall()
            for row in raw_rows:
                raw_json = str(row["raw_json"] or "{}")
                try:
                    operation = json.loads(raw_json)
                except (TypeError, ValueError, json.JSONDecodeError):
                    operation = None
                identity_hashes = (
                    {
                        "sha256:"
                        + hashlib.sha256(token.encode("utf-8")).hexdigest()
                        for token in (
                            str(operation.get(key) or "").strip()
                            for key in ("srid", "rid", "orderUid", "order_uid")
                        )
                        if token
                    }
                    if isinstance(operation, Mapping)
                    else set()
                )
                add(
                    "finance_raw",
                    [seller_id, week_start, week_end, row["report_id"], row["rrd_id"]],
                    [
                        str(row["row_hash"] or ""),
                        hashlib.sha256(raw_json.encode("utf-8")).hexdigest(),
                        [
                            [
                                identity_hash,
                                list(
                                    snapshot.fbs_order_ids_by_identity_hash.get(
                                        identity_hash, ()
                                    )
                                ),
                            ]
                            for identity_hash in sorted(identity_hashes)
                        ],
                    ],
                )
                if not isinstance(operation, Mapping):
                    continue
                if str(operation.get("docTypeName") or "").casefold() not in {
                    "продажа",
                    "возврат",
                }:
                    continue
                if int(_decimal(operation.get("quantity"))) == 0:
                    continue
                nm_id, _identity_method, _identity_problem = _resolve_finance_nm_id(
                    operation,
                    alias_to_nm=alias_to_nm,
                    ambiguous_aliases=ambiguous_aliases,
                )
                operation_date, operation_date_source = _operation_date(
                    operation, date.fromisoformat(week_start)
                )
                if operation_date_source == "week_start_fallback" or not nm_id:
                    continue
                matched_order_ids: set[int] = set()
                for identity_hash in identity_hashes:
                    matched_order_ids.update(
                        snapshot.fbs_order_ids_by_identity_hash.get(identity_hash, ())
                    )
                channel_tokens = {
                    str(operation.get(key) or "").strip().casefold()
                    for key in (
                        "deliveryType",
                        "delivery_type",
                        "orderType",
                        "order_type",
                    )
                    if str(operation.get(key) or "").strip()
                }
                explicit_fbs = "fbs" in channel_tokens
                if matched_order_ids or explicit_fbs:
                    relevant_fbs_cost_keys.add(
                        (operation_date.isoformat(), nm_id)
                    )
                    continue
                source_date = canonical_cost_source_date(operation_date).isoformat()
                relevant_wb_keys.add((source_date, nm_id))
                if nm_id.isdigit() and int(nm_id) > 0:
                    relevant_wb_nm_ids.add(int(nm_id))

            for row in conn.execute(
                "SELECT report_id,report_type,content_hash,row_count "
                "FROM wb_finance_weekly_reports WHERE seller_id=? "
                "AND week_start=? AND week_end=? ORDER BY report_id",
                (seller_id, week_start, week_end),
            ).fetchall():
                add(
                    "finance_report",
                    [seller_id, week_start, week_end, row["report_id"]],
                    [row["report_type"], row["content_hash"], row["row_count"]],
                )

        add(
            "canonical_table_presence",
            "channel_location_cost",
            sorted(
                name
                for name in snapshot.wb.table_names
                if name
                in {
                    "sheet_vitrina_v1_warehouse_functional_cutovers",
                    FUNCTIONAL_DAILY_TABLE,
                    "sheet_vitrina_v1_warehouse_archival_estimate_rows",
                    "sheet_vitrina_v1_warehouse_functional_events",
                    FBS_OBSERVATIONS_TABLE,
                    "sheet_vitrina_v1_ff_facilities",
                    "sheet_vitrina_v1_warehouse_business_operations",
                    "sheet_vitrina_v1_ff_pool_movement_lines",
                    "sheet_vitrina_v1_ready_snapshots",
                }
            ),
        )
        add("wb_cutover", FUNCTIONAL_CUTOVER_ID, dict(snapshot.wb.cutover or {}))
        for key in sorted(relevant_wb_keys):
            row = snapshot.wb.daily_rows.get(key)
            add("wb_daily_cost", list(key), dict(row) if row is not None else None)
        for nm_id in sorted(relevant_wb_nm_ids):
            row = snapshot.wb.archival_rows.get(nm_id)
            add(
                "wb_archival_cost",
                nm_id,
                dict(row) if row is not None else None,
            )
            add(
                "wb_first_factual_date",
                nm_id,
                snapshot.wb.archival_first_factual_dates.get(nm_id),
            )
        for key in sorted(relevant_fbs_cost_keys):
            pooled = pooled_fbs_state_as_of(
                snapshot,
                business_date=key[0],
                nm_id=key[1],
            )
            add(
                "fbs_pooled_cost",
                list(key),
                dict(pooled) if pooled is not None else None,
            )
            fallback = snapshot.common_inventory_cost_by_date_nm.get(key)
            add(
                "fbs_common_inventory_fallback",
                list(key),
                dict(fallback) if fallback is not None else None,
            )
        return {
            "contract": "wb_finance_exact_target_dependency_v2",
            "digest": "sha256:" + digest.hexdigest(),
            "counts": dict(sorted(counts.items())),
        }

    @staticmethod
    def _finance_target_images(
        conn: sqlite3.Connection,
        target_keys: set[tuple[str, str, str]],
    ) -> dict[str, Any]:
        images: dict[str, Any] = {}
        missing_images: dict[str, Any] = {}
        for table in (
            "wb_finance_weekly_aggregates",
            "wb_finance_weekly_cost_coverage",
            "wb_finance_weekly_reconciliation",
            "wb_finance_weekly_sku_aggregates",
            "wb_finance_weekly_sync",
        ):
            columns = [
                str(row["name"])
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            ]
            if not columns:
                missing_images[table] = {
                    "columns": [],
                    "rows": [],
                    "missing_table": True,
                }
                continue
            rows: list[list[Any]] = []
            for seller_id, week_start, week_end in sorted(target_keys):
                selected = conn.execute(
                    f"SELECT * FROM {table} WHERE seller_id=? AND week_start=? "
                    "AND week_end=? ORDER BY "
                    + ",".join(
                        name
                        for name in ("seller_id", "week_start", "week_end", "nm_id")
                        if name in columns
                    ),
                    (seller_id, week_start, week_end),
                ).fetchall()
                rows.extend([[row[column] for column in columns] for row in selected])
            images[table] = {"columns": columns, "rows": rows}
        canonical = WbFinanceWeeklyBlock._canonicalize_finance_target_images(
            conn, images
        )
        canonical.update(missing_images)
        return canonical

    @staticmethod
    def _canonicalize_finance_target_images(
        conn: sqlite3.Connection,
        images: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Return the exact SQLite storage image in deterministic PK order.

        Query projection order follows the nomenclature source, while the
        target readback is ordered by the persisted primary key.  Comparing
        those two incidental orders made a correct multi-SKU production image
        fail its CAS.  Canonicalization also applies the declared SQLite
        affinity before hashing so textual numeric scale cannot create a
        false target mismatch after an insert/readback round trip.
        """

        canonical: dict[str, Any] = {}
        for table, image in images.items():
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", str(table)) is None:
                raise ValueError("Finance target image table identity is invalid")
            table_info = conn.execute(f"PRAGMA table_info({table})").fetchall()
            expected_columns = [str(row["name"]) for row in table_info]
            columns = [str(item) for item in image["columns"]]
            if columns != expected_columns:
                raise ValueError("Finance target image schema drifted")
            declared_types = {
                str(row["name"]): str(row["type"] or "") for row in table_info
            }
            primary_key = [
                str(row["name"])
                for row in sorted(
                    (row for row in table_info if int(row["pk"] or 0) > 0),
                    key=lambda row: int(row["pk"]),
                )
            ]
            if not primary_key:
                raise ValueError("Finance target image table has no primary key")
            column_index = {name: index for index, name in enumerate(columns)}
            normalized_rows: list[list[Any]] = []
            identities: set[tuple[Any, ...]] = set()
            for source_row in image["rows"]:
                if len(source_row) != len(columns):
                    raise ValueError("Finance target image row width drifted")
                row = [
                    WbFinanceWeeklyBlock._sqlite_affinity_value(
                        value,
                        declared_type=declared_types[column],
                    )
                    for column, value in zip(columns, source_row, strict=True)
                ]
                identity = tuple(row[column_index[name]] for name in primary_key)
                if identity in identities:
                    raise ValueError("Finance target image contains duplicate identity")
                identities.add(identity)
                normalized_rows.append(row)
            normalized_rows.sort(
                key=lambda row: tuple(
                    WbFinanceWeeklyBlock._sqlite_sort_token(
                        row[column_index[name]]
                    )
                    for name in primary_key
                )
            )
            canonical[str(table)] = {
                "columns": columns,
                "rows": normalized_rows,
            }
        return canonical

    @staticmethod
    def _sqlite_affinity_value(value: Any, *, declared_type: str) -> Any:
        if value is None:
            return None
        normalized_type = declared_type.upper()
        if "INT" in normalized_type:
            try:
                number = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValueError(
                    "Finance INTEGER target value is not numeric"
                ) from exc
            if not number.is_finite() or number != number.to_integral_value():
                raise ValueError("Finance INTEGER target value is not integral")
            return int(number)
        if any(token in normalized_type for token in ("CHAR", "CLOB", "TEXT")):
            return str(value)
        if any(token in normalized_type for token in ("REAL", "FLOA", "DOUB")):
            return float(value)
        return value

    @staticmethod
    def _sqlite_sort_token(value: Any) -> tuple[int, str]:
        return (0, "") if value is None else (1, str(value))

    @staticmethod
    def _replace_finance_target_images(
        conn: sqlite3.Connection,
        *,
        target_keys: set[tuple[str, str, str]],
        images: Mapping[str, Any],
    ) -> None:
        for table, image in images.items():
            columns = [str(item) for item in image["columns"]]
            for seller_id, week_start, week_end in sorted(target_keys):
                conn.execute(
                    f"DELETE FROM {table} WHERE seller_id=? AND week_start=? AND week_end=?",
                    (seller_id, week_start, week_end),
                )
            rows = list(image["rows"])
            if rows:
                conn.executemany(
                    f"INSERT INTO {table}({','.join(columns)}) VALUES("
                    + ",".join("?" for _ in columns)
                    + ")",
                    rows,
                )

    def _finance_state_digest(
        self,
        conn: sqlite3.Connection,
        *,
        target_keys: set[tuple[str, str, str]],
        target_only: bool,
    ) -> str:
        evidence: list[list[Any]] = []
        for table in (
            "wb_finance_weekly_aggregates",
            "wb_finance_weekly_cost_coverage",
            "wb_finance_weekly_reconciliation",
            "wb_finance_weekly_sku_aggregates",
            "wb_finance_weekly_sync",
        ):
            columns = [
                str(row["name"])
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            ]
            order_columns = ["seller_id", "week_start", "week_end"]
            order_columns.extend(
                name for name in ("nm_id", "formula_version") if name in columns
            )
            rows = conn.execute(
                f"SELECT * FROM {table} ORDER BY {','.join(order_columns)}"
            ).fetchall()
            for row in rows:
                key = (
                    str(row["seller_id"]),
                    str(row["week_start"]),
                    str(row["week_end"]),
                )
                if (key in target_keys) != target_only:
                    continue
                evidence.append([table, *[row[column] for column in columns]])
        return (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    evidence,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        )

    def repair_orphan_derived_rows(self) -> dict[str, Any]:
        """Remove derived rows that have no matching seller/week sync boundary."""
        self.ensure_schema()
        tables = (
            "wb_finance_weekly_aggregates",
            "wb_finance_weekly_cost_coverage",
            "wb_finance_weekly_reconciliation",
            "wb_finance_weekly_sku_aggregates",
        )
        deleted: dict[str, int] = {}
        with self._connect() as conn:
            for table in tables:
                cursor = conn.execute(
                    f"""DELETE FROM {table} AS derived
                    WHERE NOT EXISTS (
                        SELECT 1 FROM wb_finance_weekly_sync AS sync
                        WHERE sync.seller_id=derived.seller_id
                          AND sync.week_start=derived.week_start
                          AND sync.week_end=derived.week_end
                    )"""
                )
                deleted[table] = cursor.rowcount
            conn.commit()
        return {
            "status": "completed",
            "deleted": deleted,
            "deleted_total": sum(deleted.values()),
        }

    def due_tick_week(self, now: datetime | None = None) -> tuple[date, date] | None:
        moment = (now or self.now_factory()).astimezone(MOSCOW)
        closed = historical_week_bounds(moment.date())
        if not closed:
            return None
        latest = closed[-1]
        monday_after = latest[1] + timedelta(days=1)
        if moment.date() == monday_after and moment.hour < 5:
            return None
        self.ensure_schema()
        with self._connect() as conn:
            candidates = conn.execute(
                """SELECT week_start,week_end,status,last_synced_at FROM wb_finance_weekly_sync
                WHERE seller_id=? ORDER BY week_start DESC LIMIT 2""",
                (self.seller_id,),
            ).fetchall()
        by_start = {row["week_start"]: row for row in candidates}
        latest_row = by_start.get(latest[0].isoformat())
        if latest_row is None or latest_row["status"] in {
            "waiting",
            "loading",
            "loaded_preliminary",
            "error_loading",
            "resync_required",
        }:
            return latest
        for bounds in reversed(closed[-2:]):
            row = by_start.get(bounds[0].isoformat())
            last = (
                datetime.fromisoformat(
                    str(row["last_synced_at"]).replace("Z", "+00:00")
                )
                if row and row["last_synced_at"]
                else None
            )
            if last is None or (
                moment.astimezone(timezone.utc) - last.astimezone(timezone.utc)
            ) >= timedelta(hours=24):
                return bounds
        return None

    def _mark_loading(self, start: date, end: date, now: str) -> None:
        self.ensure_schema()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO wb_finance_weekly_sync(seller_id,week_start,week_end,status,attempt_count,last_synced_at)
                VALUES (?,?,?,'loading',1,?) ON CONFLICT(seller_id,week_start,week_end) DO UPDATE SET
                status='loading',attempt_count=attempt_count+1,last_synced_at=excluded.last_synced_at,last_error=NULL""",
                (self.seller_id, start.isoformat(), end.isoformat(), now),
            )
            conn.commit()

    @staticmethod
    def _row_hash(row: Mapping[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(
                dict(row), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()

    def _attach_split_raw_read_view(
        self,
        conn: sqlite3.Connection,
        *,
        manifest: GenerationManifest,
        query_only_primary: bool,
    ) -> None:
        if not (
            manifest.state == "cutover"
            and manifest.canonical_source == "split"
        ):
            return
        self.store_registry.attach_readonly(
            conn,
            "finance_raw",
            schema_name="finance_raw_store",
            operation="wb_finance_weekly_raw_read",
            manifest=manifest,
        )
        if query_only_primary:
            if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
                raise ValueError("canonical Finance plan requires query_only")
            # Both persistent databases are already opened with SQLite
            # ``mode=ro``. Relax query_only only for this connection-local
            # view, then restore it before any data query.
            conn.execute("PRAGMA query_only=OFF")
        try:
            conn.execute(
                """CREATE TEMP VIEW wb_finance_weekly_raw_rows AS
                   SELECT seller_id,report_id,rrd_id,report_type,week_start,
                          week_end,nm_id,vendor_code,barcode,doc_type_name,
                          seller_oper_name,row_hash,raw_json,first_seen_at,
                          updated_at
                     FROM finance_raw_store.finance_raw_current_rows"""
            )
        finally:
            if query_only_primary:
                conn.execute("PRAGMA query_only=ON")
        if query_only_primary and int(
            conn.execute("PRAGMA query_only").fetchone()[0]
        ) != 1:
            raise ValueError("canonical Finance plan lost query_only")

    def _connect_canonical_plan(self) -> sqlite3.Connection:
        manifest = self.store_registry.load()
        conn = self.store_registry.connect(
            "operational",
            mode="ro",
            operation="finance_canonical_backfill_plan",
            manifest=manifest,
            timeout_ms=60_000,
        )
        try:
            self._attach_split_raw_read_view(
                conn,
                manifest=manifest,
                query_only_primary=True,
            )
        except Exception:
            conn.close()
            raise
        return conn

    def _connect_stale_cost_plan(self) -> sqlite3.Connection:
        manifest = self.store_registry.load()
        conn = self.store_registry.connect(
            "operational",
            mode="ro",
            operation="finance_stale_cost_query_plan",
            manifest=manifest,
            timeout_ms=60_000,
        )
        try:
            self._attach_split_raw_read_view(
                conn,
                manifest=manifest,
                query_only_primary=True,
            )
            self._assert_readonly_plan_connection(conn)
        except Exception:
            conn.close()
            raise
        return conn

    @staticmethod
    def _assert_readonly_plan_connection(conn: sqlite3.Connection) -> None:
        if conn.in_transaction:
            raise ValueError("Finance query plan opened an implicit transaction")
        if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
            raise ValueError("Finance query plan requires query_only")

    @staticmethod
    def _sqlite_data_version_token(conn: sqlite3.Connection) -> dict[str, int]:
        """Observe commits to every persistent database used by the plan."""

        versions: dict[str, int] = {}
        for row in conn.execute("PRAGMA database_list").fetchall():
            schema = str(row[1])
            if schema == "temp":
                continue
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", schema) is None:
                raise ValueError("Finance SQLite schema identity is invalid")
            versions[schema] = int(
                conn.execute(f"PRAGMA {schema}.data_version").fetchone()[0]
            )
        if "main" not in versions:
            raise ValueError("Finance SQLite main data version is unavailable")
        return versions

    def _connect(self) -> sqlite3.Connection:
        manifest = self.store_registry.load()
        conn = self.store_registry.connect(
            "operational",
            mode="rw",
            operation="wb_finance_weekly",
            manifest=manifest,
        )
        self._attach_split_raw_read_view(
            conn,
            manifest=manifest,
            query_only_primary=False,
        )
        return conn


def block_from_env(runtime_dir: Path) -> WbFinanceWeeklyBlock:
    seller_id = os.environ.get("SELLER_PORTAL_CANONICAL_SUPPLIER_ID") or "canonical"
    return WbFinanceWeeklyBlock(runtime_dir, seller_id=seller_id)
