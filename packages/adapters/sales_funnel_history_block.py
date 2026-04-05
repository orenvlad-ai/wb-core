"""Адаптерная граница блока sales funnel history."""

import json
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib import error, request as urllib_request

from packages.adapters.official_api_runtime import load_runtime_config
from packages.contracts.sales_funnel_history_block import SalesFunnelHistoryRequest


class SalesFunnelHistorySource(Protocol):
    def fetch(self, request: SalesFunnelHistoryRequest) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")


class ArtifactBackedSalesFunnelHistorySource:
    def __init__(self, artifacts_root: Path) -> None:
        self._artifacts_root = artifacts_root

    def fetch(self, request: SalesFunnelHistoryRequest) -> Mapping[str, Any]:
        path = self._resolve_legacy_path(request.scenario)
        return json.loads(path.read_text(encoding="utf-8"))

    def _resolve_legacy_path(self, scenario: str) -> Path:
        if scenario == "normal":
            return self._artifacts_root / "legacy" / "normal__template__legacy__fixture.json"
        if scenario == "empty":
            return self._artifacts_root / "legacy" / "empty__template__legacy__fixture.json"
        raise ValueError(f"unsupported scenario: {scenario}")


class HttpBackedSalesFunnelHistorySource:
    def __init__(
        self,
        base_url: str = "https://seller-analytics-api.wildberries.ru",
        token_env_var: str = "WB_TOKEN",
        base_url_env_var: str = "WB_SELLER_ANALYTICS_API_BASE_URL",
        timeout_seconds: float = 30.0,
    ) -> None:
        self._default_base_url = base_url.rstrip("/")
        self._token_env_var = token_env_var
        self._base_url_env_var = base_url_env_var
        self._default_timeout_seconds = timeout_seconds

    def fetch(self, request: SalesFunnelHistoryRequest) -> Mapping[str, Any]:
        runtime = load_runtime_config(
            token_env_var=self._token_env_var,
            default_base_url=self._default_base_url,
            base_url_env_var=self._base_url_env_var,
            default_timeout_seconds=self._default_timeout_seconds,
        )
        payload = self._post_history(
            base_url=runtime.base_url,
            token=runtime.token,
            date_from=request.date_from,
            date_to=request.date_to,
            nm_ids=request.nm_ids,
            timeout_seconds=runtime.timeout_seconds,
        )
        rows: list[list[Any]] = []
        fetched_at = f"{request.date_to} 21:30:00"
        if isinstance(payload, list):
            for item in payload:
                if not isinstance(item, Mapping):
                    continue
                product = item.get("product")
                nm_id = product.get("nmId") if isinstance(product, Mapping) else None
                history = item.get("history")
                if not isinstance(nm_id, int) or not isinstance(history, list):
                    continue
                for point in history:
                    if not isinstance(point, Mapping):
                        continue
                    date = point.get("date")
                    if not isinstance(date, str):
                        continue
                    for metric, value in point.items():
                        if metric == "date" or isinstance(value, Mapping) or value is None:
                            continue
                        rows.append([fetched_at, date, nm_id, metric, value])
        return {
            "date_from": request.date_from,
            "date_to": request.date_to,
            "requested_nm_ids": request.nm_ids,
            "data": {"rows": rows},
        }

    def _post_history(
        self,
        *,
        base_url: str,
        token: str,
        date_from: str,
        date_to: str,
        nm_ids: list[int],
        timeout_seconds: float,
    ) -> Any:
        req = urllib_request.Request(
            url=f"{base_url}/api/analytics/v3/sales-funnel/products/history",
            data=json.dumps(
                {
                    "selectedPeriod": {"start": date_from, "end": date_to},
                    "nmIds": nm_ids,
                    "skipDeletedNm": True,
                    "aggregationLevel": "day",
                }
            ).encode(),
            method="POST",
            headers={"Authorization": token, "Content-Type": "application/json"},
        )
        try:
            with urllib_request.urlopen(req, timeout=timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            raise RuntimeError(
                f"official sales funnel history request failed with status {exc.code}: {body}"
            ) from exc
        except error.URLError as exc:
            raise RuntimeError(
                f"official sales funnel history request transport failed: {exc}"
            ) from exc
