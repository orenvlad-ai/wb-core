"""Adapter boundary for public WB card buyer-price source used by SPP proxy."""

from __future__ import annotations

from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib import error, parse, request as urllib_request
from zoneinfo import ZoneInfo

from packages.contracts.spp_proxy_block import SppProxyRequest

BUSINESS_TIMEZONE = ZoneInfo("Asia/Yekaterinburg")
PUBLIC_WB_CARD_BASE_URL_ENV = "WB_PUBLIC_CARD_BASE_URL"
PUBLIC_WB_CARD_API_BASE_URL_ENV = "WB_PUBLIC_CARD_API_BASE_URL"
PUBLIC_WB_CARD_DEST_ENV = "WB_PUBLIC_CARD_DEST"
PUBLIC_WB_CARD_TIMEOUT_ENV = "WB_PUBLIC_CARD_TIMEOUT_SECONDS"
DEFAULT_PUBLIC_WB_CARD_BASE_URL = "https://www.wildberries.ru"
DEFAULT_PUBLIC_WB_CARD_API_BASE_URL = "https://card.wb.ru"
DEFAULT_PUBLIC_WB_CARD_DEST = "-1257786"


class SppProxySource(Protocol):
    """Buyer-price source for application layer."""

    def fetch(self, request: SppProxyRequest) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")


BusinessDateFactory = Callable[[], str]
HttpGet = Callable[[str, float], tuple[int, str, Mapping[str, str]]]


@dataclass(frozen=True)
class PublicBuyerPriceExtraction:
    price: float | None
    method: str
    detail: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)


class ArtifactBackedPublicWbCardBuyerPriceSource:
    """Fixture source for local smoke tests."""

    def __init__(self, artifacts_root: Path) -> None:
        self._artifacts_root = artifacts_root

    def fetch(self, request: SppProxyRequest) -> Mapping[str, Any]:
        path = self._resolve_fixture_path(request.scenario)
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {
            "snapshot_date": request.snapshot_date,
            "requested_nm_ids": request.nm_ids,
            **payload,
        }

    def _resolve_fixture_path(self, scenario: str) -> Path:
        if scenario == "normal":
            return self._artifacts_root / "public_card" / "normal__template__public-card__fixture.json"
        if scenario == "empty":
            return self._artifacts_root / "public_card" / "empty__template__public-card__fixture.json"
        raise ValueError(f"unsupported scenario: {scenario}")


class HttpBackedPublicWbCardBuyerPriceSource:
    """Anonymous HTTP source for current public WB buyer prices."""

    def __init__(
        self,
        *,
        card_base_url: str | None = None,
        card_api_base_url: str | None = None,
        dest: str | None = None,
        timeout_seconds: float | None = None,
        http_get: HttpGet | None = None,
        business_date_factory: BusinessDateFactory | None = None,
    ) -> None:
        self._card_base_url = (
            card_base_url
            or os.environ.get(PUBLIC_WB_CARD_BASE_URL_ENV, "").strip()
            or DEFAULT_PUBLIC_WB_CARD_BASE_URL
        ).rstrip("/")
        self._card_api_base_url = (
            card_api_base_url
            or os.environ.get(PUBLIC_WB_CARD_API_BASE_URL_ENV, "").strip()
            or DEFAULT_PUBLIC_WB_CARD_API_BASE_URL
        ).rstrip("/")
        self._dest = str(
            dest
            if dest is not None
            else os.environ.get(PUBLIC_WB_CARD_DEST_ENV, "").strip() or DEFAULT_PUBLIC_WB_CARD_DEST
        )
        self._dest = _normalize_public_dest(self._dest)
        self._timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else _float_env(PUBLIC_WB_CARD_TIMEOUT_ENV, 20.0)
        )
        self._http_get = http_get or _anonymous_http_get
        self._business_date_factory = business_date_factory or _current_business_date

    def for_destination(self, dest: str) -> "HttpBackedPublicWbCardBuyerPriceSource":
        """Return an isolated anonymous source for one explicit WB destination."""

        return HttpBackedPublicWbCardBuyerPriceSource(
            card_base_url=self._card_base_url,
            card_api_base_url=self._card_api_base_url,
            dest=_normalize_public_dest(dest),
            timeout_seconds=self._timeout_seconds,
            http_get=self._http_get,
            business_date_factory=self._business_date_factory,
        )

    def fetch(self, request: SppProxyRequest) -> Mapping[str, Any]:
        if request.snapshot_date != self._business_date_factory():
            return {
                "snapshot_date": request.snapshot_date,
                "requested_nm_ids": request.nm_ids,
                "source": {
                    "mode": "public_wb_card_buyer_price",
                    "endpoint": "GET /catalog/{nmId}/detail.aspx",
                    "temporal_capability": "current_only",
                    "status": "historical_unavailable",
                    "auth_context": "anonymous",
                },
                "data": {"items": []},
                "diagnostics": {
                    "current_only": True,
                    "reason": "historical public card buyer price is unavailable",
                },
            }

        items: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        for nm_id in request.nm_ids:
            item = self._fetch_one(nm_id)
            if item.get("public_buyer_price") is None:
                missing.append(item)
            else:
                items.append(item)
        return {
            "snapshot_date": request.snapshot_date,
            "requested_nm_ids": request.nm_ids,
            "source": {
                "mode": "public_wb_card_buyer_price",
                "endpoint": "GET /catalog/{nmId}/detail.aspx + public card API fallback",
                "temporal_capability": "current_only",
                "auth_context": "anonymous",
                "region_context": f"dest={self._dest}",
            },
            "data": {"items": items},
            "diagnostics": {
                "requested_count": len(request.nm_ids),
                "covered_count": len(items),
                "missing_count": len(missing),
                "missing": missing[:20],
                "region_context": f"dest={self._dest}",
            },
        }

    def _fetch_one(self, nm_id: int) -> dict[str, Any]:
        card_url = public_wb_card_url(nm_id, base_url=self._card_base_url)
        diagnostics: dict[str, Any] = {"card_url": card_url}
        try:
            status, body, headers = self._http_get(card_url, self._timeout_seconds)
        except Exception as exc:  # pragma: no cover - live transport fallback
            status, body, headers = 0, "", {}
            diagnostics["card_http_error"] = str(exc)
        diagnostics["card_http_status"] = status
        if _looks_like_wb_antibot(body, headers):
            diagnostics["card_antibot"] = True

        extracted = extract_public_buyer_price_from_wb_card_html(body, nm_id=nm_id)
        if extracted.price is not None:
            return {
                "nmId": nm_id,
                "public_buyer_price": extracted.price,
                "card_url": card_url,
                "parse_method": extracted.method,
                "diagnostics": {**diagnostics, **extracted.diagnostics},
            }

        diagnostics["card_parse_detail"] = extracted.detail
        for api_url in self._public_api_urls(nm_id):
            try:
                api_status, api_body, api_headers = self._http_get(api_url, self._timeout_seconds)
            except Exception as exc:  # pragma: no cover - live transport fallback
                diagnostics.setdefault("api_errors", []).append({"url": api_url, "error": str(exc)})
                continue
            api_diag = {"url": api_url, "status": api_status}
            if _looks_like_wb_antibot(api_body, api_headers):
                api_diag["antibot"] = True
            try:
                payload = json.loads(api_body)
            except json.JSONDecodeError:
                diagnostics.setdefault("api_attempts", []).append({**api_diag, "json": "invalid"})
                continue
            api_extracted = extract_public_buyer_price_from_public_card_json(payload, nm_id=nm_id)
            diagnostics.setdefault("api_attempts", []).append({**api_diag, **api_extracted.diagnostics})
            if api_extracted.price is not None:
                return {
                    "nmId": nm_id,
                    "public_buyer_price": api_extracted.price,
                    "card_url": card_url,
                    "api_url": api_url,
                    "parse_method": api_extracted.method,
                    "diagnostics": diagnostics,
                }

        return {
            "nmId": nm_id,
            "public_buyer_price": None,
            "card_url": card_url,
            "parse_method": "",
            "diagnostics": diagnostics,
        }

    def _public_api_urls(self, nm_id: int) -> list[str]:
        query = parse.urlencode(
            {
                "appType": "1",
                "curr": "rub",
                "dest": self._dest,
                "spp": "30",
                "ab_testing": "false",
                "nm": str(nm_id),
            }
        )
        legacy_query = parse.urlencode(
            {
                "appType": "1",
                "curr": "rub",
                "dest": self._dest,
                "spp": "30",
                "nm": str(nm_id),
            }
        )
        return [
            f"{self._card_api_base_url}/cards/v4/detail?{legacy_query}",
            f"{self._card_api_base_url}/cards/v2/detail?{query}",
            f"{self._card_api_base_url}/cards/detail?{legacy_query}",
        ]


def public_wb_card_url(nm_id: int, *, base_url: str = DEFAULT_PUBLIC_WB_CARD_BASE_URL) -> str:
    return f"{base_url.rstrip('/')}/catalog/{int(nm_id)}/detail.aspx"


def _normalize_public_dest(value: str) -> str:
    normalized = str(value or "").strip()
    if not re.fullmatch(r"-?\d+(?:,-?\d+)*", normalized):
        raise ValueError("WB public card destination must be a comma-separated integer list")
    return normalized


def extract_public_buyer_price_from_wb_card_html(
    html: str,
    *,
    nm_id: int,
) -> PublicBuyerPriceExtraction:
    if not html:
        return PublicBuyerPriceExtraction(price=None, method="", detail="empty html")

    scripts = _extract_scripts(html)
    for script_type, script_text in scripts:
        normalized_type = script_type.lower()
        if "json" not in normalized_type and not _looks_like_json_script(script_text):
            continue
        for payload_text in _candidate_json_texts(script_text):
            try:
                payload = json.loads(payload_text)
            except json.JSONDecodeError:
                continue
            extracted = extract_public_buyer_price_from_public_card_json(payload, nm_id=nm_id)
            if extracted.price is not None:
                return PublicBuyerPriceExtraction(
                    price=extracted.price,
                    method=f"html_script_json:{extracted.method}",
                    diagnostics=extracted.diagnostics,
                )

    meta_price = _extract_meta_price(html)
    if meta_price is not None:
        return PublicBuyerPriceExtraction(
            price=meta_price,
            method="html_meta_price",
            diagnostics={"fallback": "meta itemprop/product price"},
        )

    dom_price = _extract_dom_price(html)
    if dom_price is not None:
        return PublicBuyerPriceExtraction(
            price=dom_price,
            method="html_dom_price_text",
            diagnostics={"fallback": "bounded price text pattern"},
        )

    detail = "public buyer price not found in hydrated JSON or bounded DOM fallback"
    if _looks_like_wb_antibot(html, {}):
        detail = "WB antibot/challenge page returned instead of public card payload"
    return PublicBuyerPriceExtraction(price=None, method="", detail=detail)


def extract_public_buyer_price_from_public_card_json(
    payload: Any,
    *,
    nm_id: int,
) -> PublicBuyerPriceExtraction:
    product = _find_product_payload(payload, nm_id=nm_id)
    if product is None:
        return PublicBuyerPriceExtraction(
            price=None,
            method="",
            detail="product payload not found",
            diagnostics={"nm_id": nm_id, "product_found": False},
        )

    candidate = _extract_price_from_product_payload(product)
    if candidate is None:
        return PublicBuyerPriceExtraction(
            price=None,
            method="",
            detail="price field not found in product payload",
            diagnostics={"nm_id": nm_id, "product_found": True},
        )
    price, path = candidate
    return PublicBuyerPriceExtraction(
        price=price,
        method=f"json:{path}",
        diagnostics={"nm_id": nm_id, "product_found": True, "price_path": path},
    )


def _extract_price_from_product_payload(product: Any) -> tuple[float, str] | None:
    priority_paths = [
        ("salePriceU",),
        ("sale_price_u",),
        ("salePrice",),
        ("sale_price",),
        ("finalPriceU",),
        ("final_price_u",),
        ("finalPrice",),
        ("final_price",),
        ("clientPriceU",),
        ("client_price_u",),
        ("clientPrice",),
        ("priceWithDiscU",),
        ("price_with_disc_u",),
        ("priceWithDisc",),
        ("discountedPrice",),
        ("sizes", 0, "price", "total"),
        ("sizes", 0, "price", "product"),
        ("sizes", 0, "price", "sale"),
        ("offers", 0, "price"),
    ]
    for path in priority_paths:
        value = _value_at_path(product, path)
        price = _normalize_public_price(value, path=path)
        if price is not None:
            return price, ".".join(str(item) for item in path)

    recursive = _find_price_key_recursive(product)
    if recursive is not None:
        value, path = recursive
        price = _normalize_public_price(value, path=tuple(path))
        if price is not None:
            return price, ".".join(path)
    return None


def _find_product_payload(payload: Any, *, nm_id: int) -> Any | None:
    if isinstance(payload, Mapping):
        payload_id = _int_or_none(
            payload.get("id")
            or payload.get("nmId")
            or payload.get("nmID")
            or payload.get("nm_id")
        )
        if payload_id == int(nm_id):
            return payload
        products = payload.get("products")
        if products is None and isinstance(payload.get("data"), Mapping):
            products = payload["data"].get("products")
        if isinstance(products, list):
            for item in products:
                found = _find_product_payload(item, nm_id=nm_id)
                if found is not None:
                    return found
        for value in payload.values():
            found = _find_product_payload(value, nm_id=nm_id)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _find_product_payload(item, nm_id=nm_id)
            if found is not None:
                return found
    return None


def _find_price_key_recursive(value: Any, path: list[str] | None = None) -> tuple[Any, list[str]] | None:
    path = path or []
    if isinstance(value, Mapping):
        for key in (
            "salePriceU",
            "finalPriceU",
            "clientPriceU",
            "priceWithDiscU",
            "salePrice",
            "finalPrice",
            "clientPrice",
            "discountedPrice",
        ):
            if key in value:
                return value[key], [*path, key]
        for key, item in value.items():
            found = _find_price_key_recursive(item, [*path, str(key)])
            if found is not None:
                return found
    elif isinstance(value, list):
        for index, item in enumerate(value[:10]):
            found = _find_price_key_recursive(item, [*path, str(index)])
            if found is not None:
                return found
    return None


def _value_at_path(value: Any, path: tuple[str | int, ...]) -> Any:
    current = value
    for part in path:
        if isinstance(part, int):
            if not isinstance(current, list) or len(current) <= part:
                return None
            current = current[part]
        else:
            if not isinstance(current, Mapping) or part not in current:
                return None
            current = current[part]
    return current


def _normalize_public_price(value: Any, *, path: tuple[str | int, ...]) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = _parse_price_text(value)
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric <= 0:
        return None
    path_text = ".".join(str(item) for item in path).lower()
    if path_text.endswith("u") or path_text.endswith("priceu") or "priceu" in path_text:
        numeric = numeric / 100.0
    elif (
        ".price." in path_text
        and path_text.rsplit(".", 1)[-1] in {"basic", "product", "logistics", "return", "cashback"}
        and numeric >= 10000
    ):
        numeric = numeric / 100.0
    return round(float(numeric), 2)


class _ScriptExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[tuple[str, str]] = []
        self._active_type = ""
        self._active_chunks: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        attrs_map = {key.lower(): value or "" for key, value in attrs}
        self._active_type = attrs_map.get("type", "")
        self._active_chunks = []

    def handle_data(self, data: str) -> None:
        if self._active_chunks is not None:
            self._active_chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "script" or self._active_chunks is None:
            return
        self.scripts.append((self._active_type, "".join(self._active_chunks).strip()))
        self._active_type = ""
        self._active_chunks = None


def _extract_scripts(html: str) -> list[tuple[str, str]]:
    parser = _ScriptExtractor()
    try:
        parser.feed(html)
    except Exception:
        return []
    return parser.scripts


def _looks_like_json_script(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("{") or stripped.startswith("[") or "products" in stripped or "salePrice" in stripped


def _candidate_json_texts(text: str) -> list[str]:
    stripped = text.strip().rstrip(";")
    candidates = [stripped]
    for pattern in (
        r"window\.__INITIAL_STATE__\s*=\s*({.*})\s*;?",
        r"window\.__NUXT__\s*=\s*({.*})\s*;?",
        r"window\.__NEXT_DATA__\s*=\s*({.*})\s*;?",
    ):
        match = re.search(pattern, stripped, flags=re.DOTALL)
        if match:
            candidates.append(match.group(1))
    return [item for item in candidates if item]


def _extract_meta_price(html: str) -> float | None:
    patterns = [
        r'<meta[^>]+itemprop=["\']price["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+property=["\']product:price:amount["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+itemprop=["\']price["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.IGNORECASE)
        if match:
            price = _parse_price_text(match.group(1))
            if price is not None:
                return price
    return None


def _extract_dom_price(html: str) -> float | None:
    patterns = [
        r'class=["\'][^"\']*(?:price-block__final-price|product-page__price|final-price)[^"\']*["\'][^>]*>([^<]+)',
        r'(?:"finalPrice"|"salePrice")[^0-9]{0,20}([0-9][0-9\s.,]*)',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.IGNORECASE)
        if match:
            price = _parse_price_text(unescape(match.group(1)))
            if price is not None:
                return price
    return None


def _parse_price_text(value: str) -> float | None:
    normalized = (
        str(value)
        .replace("\u00a0", " ")
        .replace("₽", "")
        .replace("руб.", "")
        .replace("руб", "")
        .strip()
    )
    normalized = re.sub(r"[^0-9,.\s]", "", normalized)
    normalized = normalized.replace(" ", "")
    if not normalized:
        return None
    if "," in normalized and "." not in normalized:
        normalized = normalized.replace(",", ".")
    try:
        price = float(normalized)
    except ValueError:
        return None
    if price <= 0:
        return None
    return round(price, 2)


def _looks_like_wb_antibot(body: str, headers: Mapping[str, str]) -> bool:
    header_text = " ".join(f"{key}:{value}" for key, value in headers.items()).lower()
    body_text = body[:5000].lower()
    return (
        "x-pow" in header_text
        or "__wbaas/challenges/antibot" in body_text
        or "почти готово" in body_text
        or "data-site-key" in body_text
    )


def _anonymous_http_get(url: str, timeout_seconds: float) -> tuple[int, str, Mapping[str, str]]:
    req = urllib_request.Request(
        url=url,
        method="GET",
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
            ),
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
        },
    )
    try:
        with urllib_request.urlopen(req, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8", errors="replace")
            return int(response.status), body, dict(response.headers.items())
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return int(exc.code), body, dict(exc.headers.items())
    except error.URLError as exc:
        raise RuntimeError(f"public WB card request transport failed: {exc}") from exc


def _current_business_date() -> str:
    from datetime import datetime

    return datetime.now(BUSINESS_TIMEZONE).date().isoformat()


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
