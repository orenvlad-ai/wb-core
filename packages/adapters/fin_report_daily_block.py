"""Адаптерная граница блока fin report daily."""

import json
import os
from pathlib import Path
from typing import Any, Mapping, Protocol

from packages.adapters.official_api_runtime import DEFAULT_WB_API_TOKEN_ENV, load_runtime_config
from packages.adapters.wb_finance_api import (
    FinanceFetchResult,
    WbFinanceApiClient,
)
from packages.contracts.fin_report_daily_block import FinReportDailyRequest


FINANCE_BASE_URL = "https://finance-api.wildberries.ru"


class FinReportDailySource(Protocol):
    def fetch(self, request: FinReportDailyRequest) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")


class ArtifactBackedFinReportDailySource:
    def __init__(self, artifacts_root: Path) -> None:
        self._artifacts_root = artifacts_root

    def fetch(self, request: FinReportDailyRequest) -> Mapping[str, Any]:
        path = self._resolve_legacy_path(request.scenario)
        return json.loads(path.read_text(encoding="utf-8"))

    def _resolve_legacy_path(self, scenario: str) -> Path:
        if scenario == "normal":
            return self._artifacts_root / "legacy" / "normal__template__legacy__fixture.json"
        if scenario == "storage_total":
            return self._artifacts_root / "legacy" / "storage_total__template__legacy__fixture.json"
        raise ValueError(f"unsupported scenario: {scenario}")


class HttpBackedFinReportDailySource:
    def __init__(
        self,
        base_url: str = FINANCE_BASE_URL,
        token_env_var: str = DEFAULT_WB_API_TOKEN_ENV,
        base_url_env_var: str = "WB_FINANCE_API_BASE_URL",
        timeout_seconds: float = 180.0,
        runtime_dir: Path | None = None,
        client: WbFinanceApiClient | None = None,
    ) -> None:
        self._default_base_url = base_url.rstrip("/")
        self._token_env_var = token_env_var
        self._base_url_env_var = base_url_env_var
        self._default_timeout_seconds = timeout_seconds
        self._runtime_dir = runtime_dir
        self._client = client

    def fetch(self, request: FinReportDailyRequest) -> Mapping[str, Any]:
        client = self._client
        if client is None:
            runtime = load_runtime_config(
                token_env_var=self._token_env_var,
                default_base_url=self._default_base_url,
                base_url_env_var=self._base_url_env_var,
                default_timeout_seconds=self._default_timeout_seconds,
            )
            client = WbFinanceApiClient(
                runtime.token,
                url=_finance_detailed_url(runtime.base_url),
                rate_gate_root=(
                    self._runtime_dir
                    or Path(os.environ.get("REGISTRY_UPLOAD_RUNTIME_DIR", ".runtime/registry_upload"))
                ),
            )
        fetched = client.fetch_report(
            date_from=request.snapshot_date,
            date_to=request.snapshot_date,
            period="daily",
        )
        rows, exact_row_count, target_row_count = self._map_finance_rows(
            fetched=fetched,
            snapshot_date=request.snapshot_date,
            nm_ids=request.nm_ids,
        )
        covered_nm_ids = sorted(
            int(row["nmId"])
            for row in rows
            if isinstance(row.get("nmId"), int) and int(row["nmId"]) > 0
        )
        return {
            "snapshot_date": request.snapshot_date,
            "requested_nm_ids": request.nm_ids,
            "source": {
                "endpoint": "POST /api/finance/v1/sales-reports/detailed",
                "mode": "official_finance_daily",
                "period": "daily",
                "pagination": {
                    "pages": fetched.pages,
                    "rrdid_start": 0,
                    "rrdid_end": fetched.rrd_id_end,
                    "terminal_status": fetched.terminal_status,
                    "complete": fetched.terminal_status == 204,
                },
                "source_digest": fetched.source_digest,
                "source_row_count": len(fetched.rows),
                "exact_date_row_count": exact_row_count,
                "target_row_count": target_row_count,
                "requested_count": len(set(request.nm_ids)),
                "covered_count": len(set(covered_nm_ids)),
            },
            "data": {"rows": rows},
        }

    def _map_finance_rows(
        self,
        *,
        fetched: FinanceFetchResult,
        snapshot_date: str,
        nm_ids: list[int],
    ) -> tuple[list[dict[str, Any]], int, int]:
        wanted = set(nm_ids)
        items: dict[int, dict[str, float]] = {}
        total_storage_fee = 0.0
        exact_row_count = 0
        target_row_count = 0

        for row in fetched.rows:
            row_snapshot = _extract_snapshot_date(row)
            if row_snapshot != snapshot_date:
                continue
            exact_row_count += 1
            storage_fee = _required_money(row, "paidStorage")
            total_storage_fee += storage_fee
            nm_id = _positive_int(row.get("nmId"))
            if nm_id is None or nm_id not in wanted:
                continue
            target_row_count += 1
            rec = items.setdefault(
                nm_id,
                {
                    "snapshot_date": snapshot_date,
                    "nmId": nm_id,
                    "fin_delivery_rub": 0.0,
                    "fin_storage_fee": 0.0,
                    "fin_deduction": 0.0,
                    "fin_commission": 0.0,
                    "fin_penalty": 0.0,
                    "fin_additional_payment": 0.0,
                    "fin_buyout_rub": 0.0,
                    "fin_commission_wb_portal": 0.0,
                    "fin_acquiring_fee": 0.0,
                    "fin_loyalty_rub": 0.0,
                },
            )
            retail = _required_money(row, "retailPriceWithDisc")
            commission = retail * _required_money(row, "commissionPercent") / 100.0
            doc_type = str(row.get("docTypeName") or "").casefold()
            operation = str(row.get("sellerOperName") or "").casefold()
            is_sale = doc_type == "продажа" or operation == "продажа"
            is_return = "возврат" in doc_type or "возврат" in operation

            rec["fin_delivery_rub"] += _required_money(row, "deliveryService")
            rec["fin_storage_fee"] += storage_fee
            rec["fin_deduction"] += _required_money(row, "deduction")
            rec["fin_commission"] += _required_money(row, "ppvzSalesCommission")
            rec["fin_penalty"] += _required_money(row, "penalty")
            rec["fin_additional_payment"] += _required_money(row, "additionalPayment")
            # The daily Vitrina contract is additive: delivered acquiringFee is
            # summed as-is, including rows whose document is a return.
            rec["fin_acquiring_fee"] += _required_money(row, "acquiringFee")
            rec["fin_loyalty_rub"] += _required_money(row, "cashbackAmount")
            if is_sale:
                rec["fin_buyout_rub"] += retail
                rec["fin_commission_wb_portal"] += commission
            elif is_return:
                rec["fin_buyout_rub"] -= retail
                rec["fin_commission_wb_portal"] -= commission

        rows = [items[nm_id] for nm_id in sorted(items)]
        rows.append(
            {
                "snapshot_date": snapshot_date,
                "nmId": 0,
                "fin_delivery_rub": 0.0,
                "fin_storage_fee": total_storage_fee,
                "fin_deduction": 0.0,
                "fin_commission": 0.0,
                "fin_penalty": 0.0,
                "fin_additional_payment": 0.0,
                "fin_buyout_rub": 0.0,
                "fin_commission_wb_portal": 0.0,
                "fin_acquiring_fee": 0.0,
                "fin_loyalty_rub": 0.0,
            }
        )
        return rows, exact_row_count, target_row_count


def _extract_snapshot_date(row: Mapping[str, Any]) -> str:
    from_rr_dt = _extract_ymd(row.get("rrDate"))
    if from_rr_dt:
        return from_rr_dt
    from_sale = _extract_ymd(row.get("saleDt"))
    if from_sale:
        return from_sale
    return _extract_ymd(row.get("dateFrom"))


def _extract_ymd(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    raw = value.strip()
    if not raw:
        return ""
    if len(raw) >= 10 and raw[4:5] == "-" and raw[7:8] == "-":
        return raw[:10]
    if len(raw) >= 10 and raw[2:3] == "." and raw[5:6] == ".":
        return f"{raw[6:10]}-{raw[3:5]}-{raw[0:2]}"
    return ""


def _required_money(row: Mapping[str, Any], field: str) -> float:
    if field not in row or row.get(field) in (None, ""):
        raise RuntimeError(f"Finance daily required field is missing: {field}")
    value = row.get(field)
    if isinstance(value, bool):
        raise RuntimeError(f"Finance daily money field is invalid: {field}")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Finance daily money field is invalid: {field}") from exc


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _finance_detailed_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/api/finance/v1/sales-reports/detailed"):
        return normalized
    return normalized + "/api/finance/v1/sales-reports/detailed"
