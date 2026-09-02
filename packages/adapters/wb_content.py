"""Read-only adapter boundary for WB Content product cards."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Mapping
from urllib import error, request as urllib_request

from packages.adapters.official_api_runtime import DEFAULT_WB_API_TOKEN_ENV, load_runtime_config


DEFAULT_WB_CONTENT_API_BASE_URL = "https://content-api.wildberries.ru"
DEFAULT_WB_CONTENT_API_BASE_URL_ENV = "WB_CONTENT_API_BASE_URL"


class WbContentHttpStatusError(RuntimeError):
    def __init__(self, status_code: int, body: str, *, content_type: str = "") -> None:
        self.status_code = int(status_code)
        self.body = body
        self.content_type = content_type
        self.body_prefix = _sanitize_body_prefix(body)
        message = f"WB content API returned status {status_code}"
        if content_type:
            message += f"; content-type={content_type}"
        if self.body_prefix:
            message += f"; body_prefix={self.body_prefix}"
        super().__init__(message)


class WbContentTransportError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        content_type: str = "",
        body_prefix: str = "",
    ) -> None:
        self.status_code = status_code
        self.content_type = content_type
        self.body_prefix = body_prefix
        details: list[str] = []
        if status_code is not None:
            details.append(f"status={status_code}")
        if content_type:
            details.append(f"content-type={content_type}")
        if body_prefix:
            details.append(f"body_prefix={body_prefix}")
        super().__init__(message + (": " + "; ".join(details) if details else ""))


@dataclass(frozen=True)
class WbContentBarcodeResolution:
    nm_id: int
    barcodes: list[str]
    cards_found: int
    pages_fetched: int
    endpoint: str = "/content/v2/get/cards/list"


@dataclass(frozen=True)
class WbContentCard:
    nm_id: int | None
    vendor_code: str
    title: str
    subject_name: str
    updated_at: str
    barcodes: list[str]
    chrt_ids: list[int] = field(default_factory=list)
    endpoint: str = "/content/v2/get/cards/list"

    def to_dict(self) -> dict[str, Any]:
        return {
            "nm_id": self.nm_id,
            "vendor_code": self.vendor_code,
            "title": self.title,
            "subject_name": self.subject_name,
            "updated_at": self.updated_at,
            "barcodes": list(self.barcodes),
            "chrt_ids": list(self.chrt_ids),
            "endpoint": self.endpoint,
        }


@dataclass(frozen=True)
class WbContentCatalogSnapshot:
    """One provably terminal traversal of the official non-trash card list."""

    cards: list[WbContentCard]
    pages_fetched: int
    terminal_short_page: bool
    cursor_chain_digest: str
    source_digest: str
    endpoint: str = "/content/v2/get/cards/list"
    scope_policy: str = "official_non_trash_cards_cursor_v1"
    complete: bool = True

    @classmethod
    def from_cards(
        cls,
        cards: list[WbContentCard],
        *,
        pages_fetched: int,
        terminal_short_page: bool,
        cursor_chain_digest: str,
    ) -> "WbContentCatalogSnapshot":
        normalized = sorted(
            (
                {
                    "nm_id": card.nm_id,
                    "vendor_code": card.vendor_code,
                    "title": card.title,
                    "subject_name": card.subject_name,
                    "updated_at": card.updated_at,
                    "barcodes": sorted(card.barcodes),
                    "chrt_ids": sorted(card.chrt_ids),
                    "endpoint": card.endpoint,
                }
                for card in cards
            ),
            key=lambda item: (
                int(item["nm_id"] or 0),
                str(item["vendor_code"]),
                str(item["updated_at"]),
            ),
        )
        material = {
            "scope_policy": "official_non_trash_cards_cursor_v1",
            "endpoint": "/content/v2/get/cards/list",
            "cards": normalized,
        }
        source_digest = "sha256:" + hashlib.sha256(
            json.dumps(
                material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        return cls(
            cards=list(cards),
            pages_fetched=int(pages_fetched),
            terminal_short_page=bool(terminal_short_page),
            cursor_chain_digest=str(cursor_chain_digest),
            source_digest=source_digest,
            complete=bool(terminal_short_page),
        )


class HttpBackedWbContentSource:
    """Read-only source for WB Content cards through the canonical official API token."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_WB_CONTENT_API_BASE_URL,
        token_env_var: str = DEFAULT_WB_API_TOKEN_ENV,
        base_url_env_var: str = DEFAULT_WB_CONTENT_API_BASE_URL_ENV,
        timeout_seconds: float = 30.0,
        opener: Any | None = None,
        page_limit: int = 100,
        max_pages_per_nm_id: int = 3,
        max_cards_pages: int = 500,
    ) -> None:
        self._default_base_url = base_url.rstrip("/")
        self._token_env_var = token_env_var
        self._base_url_env_var = base_url_env_var
        self._default_timeout_seconds = timeout_seconds
        self._opener = opener or urllib_request.urlopen
        self._page_limit = _bounded_int(page_limit, default=100, minimum=1, maximum=100)
        self._max_pages_per_nm_id = _bounded_int(max_pages_per_nm_id, default=3, minimum=1, maximum=10)
        self._max_cards_pages = _bounded_int(max_cards_pages, default=500, minimum=1, maximum=5000)

    def fetch_barcodes_by_nm_ids(self, nm_ids: list[int]) -> dict[int, WbContentBarcodeResolution]:
        runtime = load_runtime_config(
            token_env_var=self._token_env_var,
            default_base_url=self._default_base_url,
            base_url_env_var=self._base_url_env_var,
            default_timeout_seconds=self._default_timeout_seconds,
        )
        result: dict[int, WbContentBarcodeResolution] = {}
        for nm_id in _normalize_nm_ids(nm_ids):
            result[nm_id] = self._fetch_one_nm_id(
                nm_id,
                base_url=runtime.base_url,
                token=runtime.token,
                timeout_seconds=runtime.timeout_seconds,
            )
        return result

    def fetch_cards(self, *, limit: int | None = None, max_pages: int | None = None) -> list[WbContentCard]:
        return self.fetch_catalog_snapshot(limit=limit, max_pages=max_pages).cards

    def fetch_catalog_snapshot(
        self, *, limit: int | None = None, max_pages: int | None = None
    ) -> WbContentCatalogSnapshot:
        """Traverse the current official card list or fail without partial success."""

        runtime = load_runtime_config(
            token_env_var=self._token_env_var,
            default_base_url=self._default_base_url,
            base_url_env_var=self._base_url_env_var,
            default_timeout_seconds=self._default_timeout_seconds,
        )
        page_limit = _bounded_int(limit, default=self._page_limit, minimum=1, maximum=100)
        max_page_count = _bounded_int(max_pages, default=self._max_cards_pages, minimum=1, maximum=5000)
        cursor: dict[str, Any] = {"limit": page_limit}
        result: list[WbContentCard] = []
        seen_cursors: set[tuple[str, int | None]] = set()
        seen_nm_ids: set[int] = set()
        cursor_chain: list[dict[str, Any]] = []
        terminal_short_page = False
        pages_fetched = 0
        for _ in range(max_page_count):
            requested_cursor = dict(cursor)
            payload = self._request_cards_page(
                base_url=runtime.base_url,
                token=runtime.token,
                timeout_seconds=runtime.timeout_seconds,
                cursor=cursor,
            )
            cards = _extract_cards(payload)
            pages_fetched += 1
            normalized_cards = [_normalize_card(card) for card in cards]
            for card in normalized_cards:
                if card.nm_id is not None:
                    if card.nm_id in seen_nm_ids:
                        raise WbContentTransportError(
                            "WB content catalog traversal returned a duplicate nmID"
                        )
                    seen_nm_ids.add(card.nm_id)
            result.extend(normalized_cards)
            next_cursor = _extract_cursor(payload)
            total = _optional_int(next_cursor.get("total")) or len(cards)
            cursor_chain.append(
                {
                    "request": requested_cursor,
                    "response": {
                        "updatedAt": str(next_cursor.get("updatedAt") or "").strip(),
                        "nmID": _optional_int(
                            next_cursor.get("nmID")
                            or next_cursor.get("nmId")
                            or next_cursor.get("nm_id")
                        ),
                        "total": total,
                    },
                    "card_count": len(cards),
                }
            )
            if total < page_limit:
                terminal_short_page = True
                break
            updated_at = str(next_cursor.get("updatedAt") or "").strip()
            cursor_nm_id = _optional_int(next_cursor.get("nmID") or next_cursor.get("nmId") or next_cursor.get("nm_id"))
            cursor_key = (updated_at, cursor_nm_id)
            if not updated_at or cursor_nm_id is None:
                raise WbContentTransportError(
                    "WB content catalog full page lacks a continuation cursor"
                )
            if cursor_key in seen_cursors:
                raise WbContentTransportError(
                    "WB content catalog continuation cursor repeated"
                )
            seen_cursors.add(cursor_key)
            cursor = {"limit": page_limit, "updatedAt": updated_at, "nmID": cursor_nm_id}
        if not terminal_short_page:
            raise WbContentTransportError(
                "WB content catalog traversal reached the page bound before a terminal page"
            )
        cursor_chain_digest = "sha256:" + hashlib.sha256(
            json.dumps(
                cursor_chain,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return WbContentCatalogSnapshot.from_cards(
            result,
            pages_fetched=pages_fetched,
            terminal_short_page=terminal_short_page,
            cursor_chain_digest=cursor_chain_digest,
        )

    def _fetch_one_nm_id(
        self,
        nm_id: int,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float,
    ) -> WbContentBarcodeResolution:
        barcodes: list[str] = []
        seen: set[str] = set()
        cards_found = 0
        cursor: dict[str, Any] = {"limit": self._page_limit}
        pages_fetched = 0
        for _ in range(self._max_pages_per_nm_id):
            body = {
                "settings": {
                    "cursor": cursor,
                    "filter": {
                        "textSearch": str(nm_id),
                        "withPhoto": -1,
                    },
                }
            }
            payload = self._request_json(
                method="POST",
                url=f"{base_url}/content/v2/get/cards/list",
                token=token,
                timeout_seconds=timeout_seconds,
                body=body,
            )
            pages_fetched += 1
            cards = _extract_cards(payload)
            for card in cards:
                if _optional_int(card.get("nmID") or card.get("nmId") or card.get("nm_id")) != nm_id:
                    continue
                cards_found += 1
                for barcode in _extract_card_barcodes(card):
                    if barcode not in seen:
                        seen.add(barcode)
                        barcodes.append(barcode)
            next_cursor = _extract_cursor(payload)
            total = _optional_int(next_cursor.get("total")) or len(cards)
            if total < self._page_limit:
                break
            updated_at = str(next_cursor.get("updatedAt") or "").strip()
            cursor_nm_id = _optional_int(next_cursor.get("nmID") or next_cursor.get("nmId") or next_cursor.get("nm_id"))
            if not updated_at or cursor_nm_id is None:
                break
            cursor = {"limit": self._page_limit, "updatedAt": updated_at, "nmID": cursor_nm_id}
        return WbContentBarcodeResolution(
            nm_id=nm_id,
            barcodes=barcodes,
            cards_found=cards_found,
            pages_fetched=pages_fetched,
        )

    def _request_cards_page(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float,
        cursor: Mapping[str, Any],
    ) -> Any:
        body = {
            "settings": {
                "cursor": dict(cursor),
                "filter": {
                    "withPhoto": -1,
                },
            }
        }
        return self._request_json(
            method="POST",
            url=f"{base_url}/content/v2/get/cards/list",
            token=token,
            timeout_seconds=timeout_seconds,
            body=body,
        )

    def _request_json(
        self,
        *,
        method: str,
        url: str,
        token: str,
        timeout_seconds: float,
        body: Mapping[str, Any],
    ) -> Any:
        data = json.dumps(dict(body), ensure_ascii=False).encode("utf-8")
        req = urllib_request.Request(
            url=url,
            data=data,
            method=method,
            headers={
                "Authorization": token,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with self._opener(req, timeout=timeout_seconds) as response:
                status_code = _response_status(response)
                content_type = _response_content_type(response)
                raw_body = response.read().decode("utf-8", errors="replace")
        except error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            raise WbContentHttpStatusError(exc.code, body_text, content_type=_headers_content_type(exc.headers)) from exc
        except error.URLError as exc:
            raise WbContentTransportError(f"WB content API transport failed: {exc}") from exc
        except OSError as exc:
            raise WbContentTransportError(f"WB content API transport failed: {exc}") from exc

        body_prefix = _sanitize_body_prefix(raw_body)
        if status_code is not None and (status_code < 200 or status_code >= 300):
            raise WbContentHttpStatusError(status_code, raw_body, content_type=content_type)
        if not raw_body.strip():
            raise WbContentTransportError(
                "WB content API returned empty response",
                status_code=status_code,
                content_type=content_type,
            )
        if content_type and "json" not in content_type.casefold() and raw_body.lstrip()[:1] not in {"{", "["}:
            raise WbContentTransportError(
                "WB content API returned non-JSON response",
                status_code=status_code,
                content_type=content_type,
                body_prefix=body_prefix,
            )
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise WbContentTransportError(
                "WB content API returned non-JSON response",
                status_code=status_code,
                content_type=content_type,
                body_prefix=body_prefix,
            ) from exc
        if isinstance(payload, Mapping) and bool(payload.get("error")):
            detail = str(payload.get("errorText") or payload.get("message") or "unknown WB content API error")
            raise WbContentTransportError(f"WB content API returned error payload: {detail}")
        return payload


def _extract_cards(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, Mapping):
        for key in ("cards", "data", "items", "rows", "result", "response"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, Mapping)]
            if isinstance(value, Mapping):
                nested = _extract_cards(value)
                if nested:
                    return nested
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    return []


def _extract_cursor(payload: Any) -> dict[str, Any]:
    if isinstance(payload, Mapping):
        cursor = payload.get("cursor")
        if isinstance(cursor, Mapping):
            return dict(cursor)
        response = payload.get("response")
        if isinstance(response, Mapping):
            return _extract_cursor(response)
    return {}


def _extract_card_barcodes(card: Mapping[str, Any]) -> list[str]:
    barcodes: list[str] = []
    for size in card.get("sizes") or []:
        if not isinstance(size, Mapping):
            continue
        for raw in size.get("skus") or size.get("barcodes") or []:
            barcode = str(raw or "").strip()
            if barcode:
                barcodes.append(barcode)
    return barcodes


def _normalize_card(card: Mapping[str, Any]) -> WbContentCard:
    return WbContentCard(
        nm_id=_optional_int(card.get("nmID") or card.get("nmId") or card.get("nm_id")),
        vendor_code=_optional_str(card.get("vendorCode") or card.get("vendor_code") or card.get("sellerArticle")),
        title=_optional_str(card.get("title") or card.get("imtName") or card.get("name")),
        subject_name=_optional_str(card.get("subjectName") or card.get("subject_name") or card.get("object")),
        updated_at=_optional_str(card.get("updatedAt") or card.get("updated_at")),
        barcodes=_normalize_string_list(_extract_card_barcodes(card)),
        chrt_ids=_extract_card_chrt_ids(card),
    )


def _extract_card_chrt_ids(card: Mapping[str, Any]) -> list[int]:
    result: set[int] = set()
    for size in card.get("sizes") or []:
        if not isinstance(size, Mapping):
            continue
        parsed = _optional_int(
            size.get("chrtID") or size.get("chrtId") or size.get("chrt_id")
        )
        if parsed is not None and parsed > 0:
            result.add(parsed)
    return sorted(result)


def _normalize_string_list(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _normalize_nm_ids(values: list[int]) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for value in values or []:
        parsed = _optional_int(value)
        if parsed is None or parsed <= 0 or parsed in seen:
            continue
        seen.add(parsed)
        result.append(parsed)
    return result


def _optional_int(value: Any) -> int | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _optional_str(value: Any) -> str:
    return str(value or "").strip()


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = int(default)
    return min(max(normalized, minimum), maximum)


def _response_status(response: Any) -> int | None:
    for attr_name in ("status", "code"):
        value = getattr(response, attr_name, None)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    getcode = getattr(response, "getcode", None)
    if callable(getcode):
        try:
            return int(getcode())
        except (TypeError, ValueError):
            return None
    return None


def _response_content_type(response: Any) -> str:
    headers = getattr(response, "headers", None) or getattr(response, "info", lambda: None)()
    return _headers_content_type(headers)


def _headers_content_type(headers: Any) -> str:
    if headers is None:
        return ""
    get = getattr(headers, "get", None)
    if callable(get):
        return str(get("Content-Type") or get("content-type") or "").strip()
    if isinstance(headers, Mapping):
        return str(headers.get("Content-Type") or headers.get("content-type") or "").strip()
    return ""


def _sanitize_body_prefix(body: str, *, limit: int = 420) -> str:
    text = str(body or "").replace("\x00", "")
    text = re.sub(r"(?i)(authorization|token|cookie|password|secret)([\"'=:\s]+)([^\\s\"'<>;,]+)", r"\1\2<redacted>", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]
