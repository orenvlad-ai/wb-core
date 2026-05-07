"""Адаптерная граница блока spp."""

from datetime import datetime
import json
from collections import defaultdict
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib import error, parse, request as urllib_request
from zoneinfo import ZoneInfo

from packages.adapters.official_api_runtime import DEFAULT_WB_API_TOKEN_ENV, load_runtime_config
from packages.contracts.spp_block import SppRequest

BUSINESS_TIMEZONE = ZoneInfo("Asia/Yekaterinburg")
SPP_SOURCE_MODE_ENV = "WB_CORE_SPP_SOURCE_MODE"
SPP_SOURCE_MODE_AUTO = "auto"
SPP_SOURCE_MODE_STATISTICS_SALES_AVG = "statistics_sales_avg"
SPP_SOURCE_MODE_SELLER_PORTAL_DISCOUNT_ON_SITE = "seller_portal_discount_on_site"
SPP_SOURCE_MODES = {
    SPP_SOURCE_MODE_AUTO,
    SPP_SOURCE_MODE_STATISTICS_SALES_AVG,
    SPP_SOURCE_MODE_SELLER_PORTAL_DISCOUNT_ON_SITE,
}
SELLER_PORTAL_STORAGE_STATE_ENV = "PROMO_XLSX_COLLECTOR_STORAGE_STATE_PATH"
DEFAULT_SELLER_PORTAL_STORAGE_STATE_PATH = "/opt/wb-web-bot/storage_state.json"
SELLER_PORTAL_DISCOUNTS_PAGE_URL = "https://seller.wildberries.ru/discount-and-prices/main-table"
SELLER_PORTAL_DISCOUNTS_FILTER_URL = (
    "https://discounts-prices.wildberries.ru"
    "/ns/dp-api/discounts-prices/suppliers/api/v1/list/goods/filter"
)


class SppSource(Protocol):
    """Источник snapshot-данных для application-слоя."""

    def fetch(self, request: SppRequest) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")


SellerPortalGoodsFetcher = Callable[[list[int]], list[Mapping[str, Any]]]
BusinessDateFactory = Callable[[], str]


class ArtifactBackedSppSource:
    """Локальный adapter, читающий legacy artifacts вместо сети."""

    def __init__(self, artifacts_root: Path) -> None:
        self._artifacts_root = artifacts_root

    def fetch(self, request: SppRequest) -> Mapping[str, Any]:
        path = self._resolve_legacy_path(request.scenario)
        return json.loads(path.read_text(encoding="utf-8"))

    def _resolve_legacy_path(self, scenario: str) -> Path:
        if scenario == "normal":
            return self._artifacts_root / "legacy" / "normal__template__legacy__fixture.json"
        if scenario == "empty":
            return self._artifacts_root / "legacy" / "empty__template__legacy__fixture.json"
        raise ValueError(f"unsupported scenario: {scenario}")


class SellerPortalDiscountOnSiteSppSource:
    """Current-visible SPP adapter backed by Seller Portal discountOnSite."""

    def __init__(
        self,
        *,
        storage_state_path: str | Path | None = None,
        timeout_seconds: float = 60.0,
        headless: bool = True,
        goods_fetcher: SellerPortalGoodsFetcher | None = None,
        business_date_factory: BusinessDateFactory | None = None,
    ) -> None:
        self._storage_state_path = Path(
            str(
                storage_state_path
                or os.environ.get(SELLER_PORTAL_STORAGE_STATE_ENV, "").strip()
                or DEFAULT_SELLER_PORTAL_STORAGE_STATE_PATH
            )
        )
        self._timeout_seconds = timeout_seconds
        self._headless = headless
        self._goods_fetcher = goods_fetcher
        self._business_date_factory = business_date_factory or _current_business_date

    def fetch(self, request: SppRequest) -> Mapping[str, Any]:
        if request.snapshot_date != self._business_date_factory():
            return {
                "snapshot_date": request.snapshot_date,
                "requested_nm_ids": request.nm_ids,
                "source": {
                    "mode": SPP_SOURCE_MODE_SELLER_PORTAL_DISCOUNT_ON_SITE,
                    "endpoint": "Seller Portal discounts-prices list/goods/filter",
                    "temporal_capability": "current_only",
                    "status": "historical_unavailable",
                },
                "data": {"items": []},
            }

        goods = (
            self._goods_fetcher(request.nm_ids)
            if self._goods_fetcher is not None
            else self._fetch_goods_from_seller_portal(request.nm_ids)
        )
        items = _discount_on_site_goods_to_spp_items(goods, request.nm_ids)
        return {
            "snapshot_date": request.snapshot_date,
            "requested_nm_ids": request.nm_ids,
            "source": {
                "mode": SPP_SOURCE_MODE_SELLER_PORTAL_DISCOUNT_ON_SITE,
                "endpoint": "Seller Portal discounts-prices list/goods/filter",
                "temporal_capability": "current_only",
            },
            "data": {"items": items},
        }

    def _fetch_goods_from_seller_portal(self, nm_ids: list[int]) -> list[Mapping[str, Any]]:
        if not self._storage_state_path.exists():
            raise RuntimeError("seller portal storage_state.json is missing for current SPP source")

        try:
            from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, sync_playwright
        except ImportError as exc:
            raise RuntimeError("playwright is required for seller portal current SPP source") from exc

        timeout_ms = max(1, int(self._timeout_seconds * 1000))
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=self._headless)
            context = browser.new_context(storage_state=str(self._storage_state_path), locale="ru-RU")
            page = context.new_page()
            try:
                try:
                    with page.expect_request(
                        lambda item: "list/goods/filter" in str(item.url),
                        timeout=timeout_ms,
                    ) as request_info:
                        page.goto(
                            SELLER_PORTAL_DISCOUNTS_PAGE_URL,
                            wait_until="domcontentloaded",
                            timeout=timeout_ms,
                        )
                    captured_headers = _safe_seller_portal_request_headers(request_info.value)
                except PlaywrightTimeoutError as exc:
                    raise RuntimeError("seller portal discounts-prices request was not observed") from exc
                except PlaywrightError as exc:
                    raise RuntimeError(f"seller portal discounts-prices request capture failed: {exc}") from exc

                if not captured_headers:
                    raise RuntimeError("seller portal discounts-prices request headers were not captured")

                goods: list[Mapping[str, Any]] = []
                for nm_id in nm_ids:
                    response = context.request.post(
                        SELLER_PORTAL_DISCOUNTS_FILTER_URL,
                        headers=captured_headers,
                        data=_seller_portal_filter_body(nm_id),
                        timeout=timeout_ms,
                    )
                    if response.status != 200:
                        raise RuntimeError(
                            "seller portal discounts-prices request failed "
                            f"with status {response.status} for nmId={nm_id}"
                        )
                    payload = response.json()
                    goods.extend(_seller_portal_goods_list(payload))
                return goods
            finally:
                browser.close()


class HttpBackedSppSource:
    """SPP adapter with current Seller Portal source and legacy sales-average fallback."""

    def __init__(
        self,
        base_url: str = "https://statistics-api.wildberries.ru",
        token_env_var: str = DEFAULT_WB_API_TOKEN_ENV,
        base_url_env_var: str = "WB_STATISTICS_API_BASE_URL",
        timeout_seconds: float = 30.0,
        source_mode: str | None = None,
        seller_portal_source: SellerPortalDiscountOnSiteSppSource | None = None,
    ) -> None:
        self._default_base_url = base_url.rstrip("/")
        self._token_env_var = token_env_var
        self._base_url_env_var = base_url_env_var
        self._default_timeout_seconds = timeout_seconds
        self._source_mode = source_mode
        self._seller_portal_source = seller_portal_source

    def fetch(self, request: SppRequest) -> Mapping[str, Any]:
        source_mode = self._resolve_source_mode()
        if source_mode == SPP_SOURCE_MODE_SELLER_PORTAL_DISCOUNT_ON_SITE:
            source = self._seller_portal_source or SellerPortalDiscountOnSiteSppSource(
                timeout_seconds=self._default_timeout_seconds
            )
            return source.fetch(request)

        runtime = load_runtime_config(
            token_env_var=self._token_env_var,
            default_base_url=self._default_base_url,
            base_url_env_var=self._base_url_env_var,
            default_timeout_seconds=self._default_timeout_seconds,
        )
        sales_rows = self._get_sales(
            base_url=runtime.base_url,
            token=runtime.token,
            snapshot_date=request.snapshot_date,
            timeout_seconds=runtime.timeout_seconds,
        )
        aggregated = self._aggregate_by_nm_id(
            rows=sales_rows,
            snapshot_date=request.snapshot_date,
            nm_ids=request.nm_ids,
        )
        return {
            "snapshot_date": request.snapshot_date,
            "requested_nm_ids": request.nm_ids,
            "source": {
                "mode": SPP_SOURCE_MODE_STATISTICS_SALES_AVG,
                "endpoint": "GET /api/v1/supplier/sales?dateFrom=<YYYY-MM-DD>",
                "temporal_capability": "sales_rows_by_date",
            },
            "data": {
                "items": aggregated,
            },
        }

    def _resolve_source_mode(self) -> str:
        raw_mode = str(self._source_mode or os.environ.get(SPP_SOURCE_MODE_ENV, "")).strip().lower()
        mode = raw_mode or SPP_SOURCE_MODE_AUTO
        if mode not in SPP_SOURCE_MODES:
            raise ValueError(
                f"{SPP_SOURCE_MODE_ENV} must be one of: {', '.join(sorted(SPP_SOURCE_MODES))}"
            )
        if mode != SPP_SOURCE_MODE_AUTO:
            return mode

        storage_state = Path(
            os.environ.get(SELLER_PORTAL_STORAGE_STATE_ENV, "").strip()
            or DEFAULT_SELLER_PORTAL_STORAGE_STATE_PATH
        )
        if storage_state.exists():
            return SPP_SOURCE_MODE_SELLER_PORTAL_DISCOUNT_ON_SITE
        return SPP_SOURCE_MODE_STATISTICS_SALES_AVG

    def _get_sales(
        self,
        *,
        base_url: str,
        token: str,
        snapshot_date: str,
        timeout_seconds: float,
    ) -> list[Mapping[str, Any]]:
        url = f"{base_url}/api/v1/supplier/sales?{parse.urlencode({'dateFrom': snapshot_date})}"
        req = urllib_request.Request(
            url=url,
            headers={"Authorization": token},
            method="GET",
        )
        try:
            with urllib_request.urlopen(req, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            raise RuntimeError(f"official spp request failed with status {exc.code}: {body}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"official spp request transport failed: {exc}") from exc

        if not isinstance(payload, list):
            raise RuntimeError("official spp request returned non-list payload")
        return [row for row in payload if isinstance(row, Mapping)]

    def _aggregate_by_nm_id(
        self,
        *,
        rows: list[Mapping[str, Any]],
        snapshot_date: str,
        nm_ids: list[int],
    ) -> list[Mapping[str, Any]]:
        wanted = set(nm_ids)
        by_nm_id: dict[int, dict[str, float | int]] = defaultdict(lambda: {"sum": 0.0, "count": 0})

        for row in rows:
            if self._extract_sale_date(row) != snapshot_date:
                continue

            nm_id = row.get("nmId")
            if not isinstance(nm_id, int) or nm_id not in wanted:
                continue

            raw_spp = row.get("spp")
            try:
                spp_num = float(raw_spp)
            except (TypeError, ValueError):
                continue

            normalized = spp_num / 100.0 if spp_num > 1 else spp_num
            acc = by_nm_id[nm_id]
            acc["sum"] = float(acc["sum"]) + normalized
            acc["count"] = int(acc["count"]) + 1

        items: list[Mapping[str, Any]] = []
        for nm_id in sorted(by_nm_id.keys()):
            acc = by_nm_id[nm_id]
            count = int(acc["count"])
            if count <= 0:
                continue
            items.append(
                {
                    "nmId": nm_id,
                    "spp_avg": float(acc["sum"]) / count,
                    "spp_count": count,
                }
            )
        return items

    def _extract_sale_date(self, row: Mapping[str, Any]) -> str:
        source = str(row.get("date") or row.get("lastChangeDate") or "").strip()
        if len(source) >= 10 and source[4:5] == "-":
            return source[:10]
        if len(source) >= 10 and source[2:3] == ".":
            return f"{source[6:10]}-{source[3:5]}-{source[:2]}"
        return ""


def _current_business_date() -> str:
    return datetime.now(tz=BUSINESS_TIMEZONE).date().isoformat()


def _safe_seller_portal_headers(raw_headers: Mapping[str, str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in raw_headers.items():
        normalized = str(key).lower()
        if normalized.startswith(":") or normalized in {"content-length", "cookie", "accept-encoding"}:
            continue
        headers[str(key)] = str(value)
    return headers


def _safe_seller_portal_request_headers(request_object: Any) -> dict[str, str]:
    raw_headers = getattr(request_object, "headers", None)
    if isinstance(raw_headers, Mapping):
        headers = _safe_seller_portal_headers(raw_headers)
        if headers:
            return headers

    all_headers = getattr(request_object, "all_headers", None)
    if callable(all_headers):
        try:
            return _safe_seller_portal_headers(all_headers())
        except Exception:
            return {}
    return {}


def _seller_portal_filter_body(nm_id: int) -> dict[str, Any]:
    return {
        "limit": 50,
        "offset": 0,
        "code": str(nm_id),
        "facets": [],
        "filterWithoutPrice": False,
        "filterWithLeftovers": False,
        "filterWithoutCompetitivePrice": False,
        "sort": "price",
        "sortOrder": 0,
    }


def _seller_portal_goods_list(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return []
    goods = data.get("listGoods")
    if not isinstance(goods, list):
        return []
    return [item for item in goods if isinstance(item, Mapping)]


def _discount_on_site_goods_to_spp_items(
    goods: list[Mapping[str, Any]],
    requested_nm_ids: list[int],
) -> list[Mapping[str, Any]]:
    requested = set(requested_nm_ids)
    by_nm_id: dict[int, Mapping[str, Any]] = {}
    for item in goods:
        nm_id = item.get("nmID")
        if not isinstance(nm_id, int) or nm_id not in requested or nm_id in by_nm_id:
            continue
        by_nm_id[nm_id] = item

    items: list[Mapping[str, Any]] = []
    for nm_id in sorted(by_nm_id.keys()):
        raw_discount = by_nm_id[nm_id].get("discountOnSite")
        normalized = _normalize_discount_on_site(raw_discount)
        if normalized is None:
            continue
        items.append(
            {
                "nmId": nm_id,
                "spp_avg": normalized,
                "spp_count": 1,
                "source_field": "discountOnSite",
            }
        )
    return items


def _normalize_discount_on_site(raw_value: Any) -> float | None:
    if not isinstance(raw_value, (int, float)):
        return None
    value = float(raw_value)
    if value < 0:
        return None
    return value / 100.0 if value > 1 else value
