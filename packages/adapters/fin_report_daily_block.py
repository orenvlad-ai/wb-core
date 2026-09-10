"""Адаптерная граница блока fin report daily."""

import json
import os
import math
from pathlib import Path
from typing import Any, Mapping, Protocol

from packages.adapters.official_api_runtime import DEFAULT_WB_API_TOKEN_ENV, load_runtime_config
from packages.adapters.wb_finance_api import (
    FinanceApiError,
    FinanceFetchResult,
    WbFinanceApiClient,
)
from packages.contracts.fin_report_daily_block import FinReportDailyRequest
from packages.contracts.source_attempt_diagnostics import (
    SourceAttemptError, new_attempt, unknown_diagnostics,
)


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
        diagnostics = {**unknown_diagnostics("fin_report_daily"),
                       **new_attempt("fin_report_daily", request.snapshot_date),
                       "endpoint": "POST /api/finance/v1/sales-reports/detailed",
                       "mode": "official_finance_daily", "period": "daily",
                       "requested_count": len(set(request.nm_ids))}
        try:
            fetched = client.fetch_report(
                date_from=request.snapshot_date, date_to=request.snapshot_date, period="daily",
            )
        except FinanceApiError as exc:
            observed_rows = getattr(exc, "_observed_rows", None)
            if observed_rows is not None:
                diagnostics.update(_finance_row_evidence(observed_rows, request.snapshot_date, request.nm_ids))
            diagnostics.update(
                attempt_status="rejected", error_code=exc.code,
                source_digest=getattr(exc, "source_digest", None),
                source_observed_at=getattr(exc, "source_observed_at", None),
                pagination={"pages": exc.pages, "rrdid_start": 0, "rrdid_end": exc.cursor,
                            "terminal_status": exc.http_status, "complete": False},
            )
            exc.diagnostics = diagnostics
            raise
        diagnostics.update(_finance_row_evidence(fetched.rows, request.snapshot_date, request.nm_ids))
        diagnostics.update(source_observed_at=fetched.source_observed_at, source_digest=fetched.source_digest,
                           pagination={"pages": fetched.pages, "rrdid_start": 0,
                                       "rrdid_end": fetched.rrd_id_end,
                                       "terminal_status": fetched.terminal_status,
                                       "complete": fetched.terminal_status == 204})
        try:
            rows, _exact_count, _target_count = self._map_finance_rows(
                fetched=fetched, snapshot_date=request.snapshot_date, nm_ids=request.nm_ids,
            )
        except Exception as exc:
            # Field names are allowlisted by the observer; provider text is not.
            raise SourceAttemptError("finance_daily_mapping_failed", diagnostics) from exc
        diagnostics["attempt_status"] = "returned"
        return {
            "snapshot_date": request.snapshot_date,
            "requested_nm_ids": request.nm_ids,
            "source": diagnostics,
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


_FINANCE_REQUIRED_FIELDS = (
    "retailPriceWithDisc", "commissionPercent", "deliveryService", "paidStorage",
    "deduction", "ppvzSalesCommission", "penalty", "additionalPayment",
    "acquiringFee", "cashbackAmount",
)


def _finance_row_evidence(rows: list[dict[str, Any]], snapshot_date: str, nm_ids: list[int]) -> dict[str, Any]:
    """Observed row scope, before mapping can reject; never contains money or PII."""
    wanted = set(nm_ids)
    covered: set[int] = set()
    evidence: dict[str, Any] = {
        "counter_basis": "observed_source_rows",
        "source_row_count": len(rows), "exact_date_row_count": 0,
        "target_row_count": 0, "non_target_row_count": 0,
        "invalid_identity_row_count": 0, "invalid_row_count": 0, "date_fallback_count": 0,
        "date_discard_count": 0, "missing_date_row_count": 0,
        "date_basis_counts": {"rrDate": 0, "saleDt": 0, "dateFrom": 0, "missing": 0},
        "missing_required_fields": {}, "invalid_required_fields": {},
        "anomaly_codes": [],
    }
    for row in rows:
        if not isinstance(row, Mapping):
            evidence["invalid_row_count"] += 1
            continue
        basis = next((key for key in ("rrDate", "saleDt", "dateFrom") if _extract_ymd(row.get(key))), "missing")
        evidence["date_basis_counts"][basis] += 1
        if basis in ("saleDt", "dateFrom"):
            evidence["date_fallback_count"] += 1
        if basis == "missing":
            evidence["missing_date_row_count"] += 1
        if _extract_snapshot_date(row) != snapshot_date:
            evidence["date_discard_count"] += 1
            continue
        evidence["exact_date_row_count"] += 1
        nm_id = _positive_int(row.get("nmId"))
        if nm_id is None:
            evidence["invalid_identity_row_count"] += 1
        elif nm_id not in wanted:
            evidence["non_target_row_count"] += 1
        else:
            evidence["target_row_count"] += 1
            covered.add(nm_id)
        fields = _FINANCE_REQUIRED_FIELDS if nm_id in wanted else ("paidStorage",)
        for key in fields:
            value = row.get(key)
            category = None
            if value is None or value == "":
                category = "missing_required_fields"
            else:
                try:
                    if isinstance(value, bool) or not math.isfinite(float(value)):
                        category = "invalid_required_fields"
                except (ValueError, TypeError, OverflowError):
                    category = "invalid_required_fields"
            if category:
                evidence[category][key] = evidence[category].get(key, 0) + 1
    evidence.update(covered_count=len(covered), covered_nm_ids=sorted(covered),
                    missing_nm_ids=sorted(wanted - covered))
    for code, present in (
        ("empty_unconfirmed", not rows),
        ("date_basis_mismatch_or_unavailable", rows and not evidence["exact_date_row_count"]),
        ("date_fallback_used", evidence["date_fallback_count"]),
        ("date_rows_discarded", evidence["date_discard_count"]),
        ("invalid_identity", evidence["invalid_identity_row_count"]),
        ("invalid_row", evidence["invalid_row_count"]),
        ("missing_required_fields", evidence["missing_required_fields"]),
        ("invalid_required_fields", evidence["invalid_required_fields"]),
    ):
        if present:
            evidence["anomaly_codes"].append(code)
    return evidence
