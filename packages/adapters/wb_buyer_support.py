"""WB buyer support read boundary. No message/decision/write capability."""
from __future__ import annotations

import json
from typing import Any
from urllib import error, parse, request

from packages.adapters.official_api_runtime import load_runtime_config


class BuyerSupportApiError(RuntimeError):
    """Safe upstream error: never includes response body, URL or credentials."""

    def __init__(self, code: str, http_status: int | None = None):
        self.code = code
        self.http_status = http_status
        super().__init__(code)


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BuyerSupportApiError("upstream_redirect_blocked", code)


class WbBuyerSupportReadAdapter:
    def __init__(self, *, token_env_var: str = "WB_API_TOKEN"):
        self.token_env_var = token_env_var

    def _get(self, *, host: str, path: str, params: dict[str, Any]) -> dict:
        runtime = load_runtime_config(token_env_var=self.token_env_var, default_base_url=host,
                                      default_timeout_seconds=30)
        # Hosts are deliberately fixed. Credentials cannot be redirected by a base URL override.
        req = request.Request(host + path + ("?" + parse.urlencode(params) if params else ""),
                              method="GET", headers={"Authorization": runtime.token,
                                                     "Accept": "application/json"})
        try:
            with request.build_opener(_NoRedirect()).open(req, timeout=runtime.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            raise BuyerSupportApiError("upstream_http_error", exc.code) from None
        except (error.URLError, TimeoutError, OSError):
            raise BuyerSupportApiError("upstream_transport_error") from None
        except (ValueError, UnicodeError):
            raise BuyerSupportApiError("upstream_invalid_json") from None
        if not isinstance(payload, dict) or payload.get("error") or payload.get("errors"):
            raise BuyerSupportApiError("upstream_error_payload")
        return payload

    def fetch_chats(self) -> dict:
        return self._get(host="https://buyer-chat-api.wildberries.ru", path="/api/v1/seller/chats", params={})

    def fetch_events(self, cursor: int | None) -> dict:
        return self._get(host="https://buyer-chat-api.wildberries.ru", path="/api/v1/seller/events",
                         params={} if cursor is None else {"next": cursor})

    def fetch_claims(self, *, archive: bool, offset: int, limit: int) -> dict:
        return self._get(host="https://returns-api.wildberries.ru", path="/api/v1/claims",
                         params={"is_archive": "true" if archive else "false", "offset": offset, "limit": limit})
