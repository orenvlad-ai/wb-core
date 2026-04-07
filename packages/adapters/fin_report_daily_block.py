"""Адаптерная граница блока fin report daily."""

import json
import time
from pathlib import Path
from typing import Any, List, Mapping, Optional, Protocol, Tuple
from urllib import error, parse, request as urllib_request

from packages.adapters.official_api_runtime import load_runtime_config
from packages.contracts.fin_report_daily_block import FinReportDailyRequest


DEADLINE_MS = 240000
MAX_PAGES = 200


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
        base_url: str = "https://statistics-api.wildberries.ru",
        token_env_var: str = "WB_TOKEN",
        base_url_env_var: str = "WB_STATISTICS_API_BASE_URL",
        timeout_seconds: float = 30.0,
        page_sleep_seconds: float = 0.2,
    ) -> None:
        self._default_base_url = base_url.rstrip("/")
        self._token_env_var = token_env_var
        self._base_url_env_var = base_url_env_var
        self._default_timeout_seconds = timeout_seconds
        self._page_sleep_seconds = page_sleep_seconds

    def fetch(self, request: FinReportDailyRequest) -> Mapping[str, Any]:
        runtime = load_runtime_config(
            token_env_var=self._token_env_var,
            default_base_url=self._default_base_url,
            base_url_env_var=self._base_url_env_var,
            default_timeout_seconds=self._default_timeout_seconds,
        )
        rows, pages, rrdid_end = self._fetch_all_pages(
            base_url=runtime.base_url,
            token=runtime.token,
            snapshot_date=request.snapshot_date,
            timeout_seconds=runtime.timeout_seconds,
            nm_ids=request.nm_ids,
        )
        return {
            "snapshot_date": request.snapshot_date,
            "requested_nm_ids": request.nm_ids,
            "source": {
                "endpoint": "GET /api/v5/supplier/reportDetailByPeriod?period=daily",
                "mode": "official_api_bootstrap_sample",
                "pagination": {"pages": pages, "rrdid_start": 0, "rrdid_end": rrdid_end},
            },
            "data": {"rows": rows},
        }

    def _fetch_all_pages(
        self,
        *,
        base_url: str,
        token: str,
        snapshot_date: str,
        timeout_seconds: float,
        nm_ids: list[int],
    ) -> Tuple[List[Mapping[str, Any]], int, int]:
        started_at = time.monotonic()
        pages = 0
        rrdid = 0
        wanted = set(nm_ids)
        fetched_at = f"{snapshot_date} 21:30:00"
        items: dict[int, dict[str, float]] = {}
        total_storage_fee = 0.0

        while True:
            if (time.monotonic() - started_at) * 1000 > DEADLINE_MS:
                raise RuntimeError("ERR_FIN_PAGINATION_DEADLINE")
            if pages >= MAX_PAGES:
                raise RuntimeError("ERR_FIN_PAGINATION_MAXPAGES")

            page_rows = self._fetch_page(
                base_url=base_url,
                token=token,
                snapshot_date=snapshot_date,
                rrdid=rrdid,
                timeout_seconds=timeout_seconds,
            )
            if page_rows is None:
                break
            if not page_rows:
                raise RuntimeError("reportDetailByPeriod: empty page with status=200")

            pages += 1
            current_rrdid = rrdid
            last_rrdid = current_rrdid
            for row in page_rows:
                if not isinstance(row, Mapping):
                    continue
                row_snapshot = _extract_snapshot_date(row)
                if row_snapshot != snapshot_date:
                    continue

                storage_fee = _to_float(row.get("storage_fee"))
                total_storage_fee += storage_fee

                nm_id = row.get("nm_id")
                if not isinstance(nm_id, int) or nm_id <= 0 or nm_id not in wanted:
                    last_rrdid = _read_rrdid(row, last_rrdid)
                    continue

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
                        "fetched_at": fetched_at,
                    },
                )

                retail_price_withdisc_rub = _to_float(row.get("retail_price_withdisc_rub"))
                commission_by_retail_withdisc = (
                    retail_price_withdisc_rub * _to_float(row.get("commission_percent")) / 100.0
                )
                doc_type_name = str(row.get("doc_type_name") or "")
                supplier_oper_name = str(row.get("supplier_oper_name") or "")
                is_sale = doc_type_name == "Продажа" or supplier_oper_name == "Продажа"
                is_return = "Возврат" in doc_type_name or "Возврат" in supplier_oper_name

                rec["fin_delivery_rub"] += _to_float(row.get("delivery_rub"))
                rec["fin_storage_fee"] += storage_fee
                rec["fin_deduction"] += _to_float(row.get("deduction"))
                rec["fin_commission"] += _to_float(row.get("ppvz_sales_commission"))
                rec["fin_penalty"] += _to_float(row.get("penalty"))
                rec["fin_additional_payment"] += _to_float(row.get("additional_payment"))
                rec["fin_acquiring_fee"] += _to_float(row.get("acquiring_fee"))
                rec["fin_loyalty_rub"] += _to_float(row.get("cashback_amount"))

                if is_sale:
                    rec["fin_buyout_rub"] += retail_price_withdisc_rub
                    rec["fin_commission_wb_portal"] += commission_by_retail_withdisc
                elif is_return:
                    rec["fin_buyout_rub"] -= retail_price_withdisc_rub
                    rec["fin_commission_wb_portal"] -= commission_by_retail_withdisc

                last_rrdid = _read_rrdid(row, last_rrdid)

            if last_rrdid <= current_rrdid:
                raise RuntimeError("ERR_PAGINATION_STUCK")
            rrdid = last_rrdid
            time.sleep(self._page_sleep_seconds)

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
                "fetched_at": fetched_at,
            }
        )
        return rows, pages, rrdid

    def _fetch_page(
        self,
        *,
        base_url: str,
        token: str,
        snapshot_date: str,
        rrdid: int,
        timeout_seconds: float,
    ) -> Optional[List[Mapping[str, Any]]]:
        url = (
            f"{base_url}/api/v5/supplier/reportDetailByPeriod?"
            f"{parse.urlencode({'dateFrom': snapshot_date, 'dateTo': snapshot_date, 'rrdid': str(rrdid), 'period': 'daily'})}"
        )
        req = urllib_request.Request(url=url, headers={"Authorization": token}, method="GET")
        try:
            with urllib_request.urlopen(req, timeout=timeout_seconds) as response:
                if response.status == 204:
                    return None
                payload = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            if exc.code == 204:
                return None
            body = exc.read().decode("utf-8")
            raise RuntimeError(
                f"official fin report daily request failed with status {exc.code}: {body}"
            ) from exc
        except error.URLError as exc:
            raise RuntimeError(
                f"official fin report daily request transport failed: {exc}"
            ) from exc

        if not isinstance(payload, list):
            raise RuntimeError("reportDetailByPeriod: expected array payload")
        return [row for row in payload if isinstance(row, Mapping)]


def _extract_snapshot_date(row: Mapping[str, Any]) -> str:
    from_rr_dt = _extract_ymd(row.get("rr_dt"))
    if from_rr_dt:
        return from_rr_dt
    return _extract_ymd(row.get("sale_dt"))


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


def _to_float(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _read_rrdid(row: Mapping[str, Any], fallback: int) -> int:
    value = row.get("rrd_id")
    return value if isinstance(value, int) else fallback
