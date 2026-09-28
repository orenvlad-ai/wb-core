"""Bounded, secret-free CDP network outcomes for one WB Buyer login run.

Only fixed vocabulary and numeric HTTP status leave this module. Raw CDP
events, URLs, request IDs, OAuth parameters and error text stay in memory.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
import json
import os
import re
from typing import Any, Mapping
from urllib.parse import urlsplit


MAX_EVENTS = 96
MAX_REQUESTS = 256
KINDS = {"Document", "XHR", "Fetch"}
METHODS = {"GET", "POST", "OPTIONS", "PUT", "PATCH", "DELETE"}
NETWORK_ERROR = re.compile(r"net::ERR_[A-Z0-9_]{1,48}\Z")


def endpoint_category(url: object) -> str:
    """Return a fixed category; never return any part of an untrusted URL."""
    try:
        parsed = urlsplit(str(url))
        host = (parsed.hostname or "").lower()
        path = parsed.path.lower()
    except ValueError:
        return ""
    if parsed.scheme != "https":
        return ""
    if host == "id.wb.ru":
        if path == "/login" or path.startswith("/login/"):
            return "wbid_login"
        if path.startswith("/api/") or "/api/" in path:
            return "wbid_api"
        return "wbid_other"
    if host in {"wildberries.ru", "www.wildberries.ru"}:
        if path == "/wb-id/callback":
            return "marketplace_callback"
        if path == "/lk" or path.startswith("/lk/"):
            return "marketplace_account"
        if path.startswith("/api/") or "/api/" in path:
            return "marketplace_api"
        return "marketplace_other"
    return ""


class NetworkDiagnostic:
    """Receive events from the existing private pipe, never another client."""

    def __init__(self, *, output_fd: int = 1, existing_events: int = 0) -> None:
        self.output_fd = output_fd
        self.events = min(MAX_EVENTS, max(0, existing_events))
        self.requests: OrderedDict[tuple[str, str], tuple[str, str, str]] = OrderedDict()
        self.sessions: set[str] = set()
        self.armed_wbid_targets: set[str] = set()

    def enable_session(self, session: str) -> None:
        if session:
            self.sessions.add(session)

    def mark_wbid_armed(self, target_id: str) -> None:
        """A fixed readback marker for the operator's pre-submit handshake."""
        if target_id in self.armed_wbid_targets or len(self.armed_wbid_targets) >= 8:
            return
        self.armed_wbid_targets.add(target_id)
        record = {"event": "buyer_network_armed", "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "target": "wbid"}
        try:
            os.write(self.output_fd, (json.dumps(record, separators=(",", ":")) + "\n").encode("ascii"))
        except OSError:
            pass

    def _emit(self, *, category: str, kind: str, method: str, outcome: str, code: int | str) -> None:
        if self.events >= MAX_EVENTS:
            return
        self.events += 1
        record = {
            "event": "buyer_network_outcome",
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "stage": "wbid_transition" if category.startswith("wbid_") else (
                "marketplace_callback" if category == "marketplace_callback" else "marketplace_navigation"),
            "endpoint": category,
            "kind": kind,
            "method": method,
            "outcome": outcome,
            "code": code,
        }
        try:
            os.write(self.output_fd, (json.dumps(record, separators=(",", ":")) + "\n").encode("ascii"))
        except OSError:
            pass  # Diagnostics must not affect login or cleanup.

    def consume(self, event: Mapping[str, Any]) -> None:
        if self.events >= MAX_EVENTS or str(event.get("sessionId") or "") not in self.sessions:
            return
        method = event.get("method")
        params = event.get("params")
        if not isinstance(params, Mapping) or method not in {
            "Network.requestWillBeSent", "Network.responseReceived", "Network.loadingFailed"
        }:
            return
        session = str(event.get("sessionId") or "")
        request_id = params.get("requestId")
        if not isinstance(request_id, str) or len(request_id) > 128:
            return
        key = (session, request_id)
        if method == "Network.requestWillBeSent":
            previous = self.requests.pop(key, None)
            redirect = params.get("redirectResponse")
            if previous and isinstance(redirect, Mapping):
                self._record_response(previous, redirect.get("status"))
            request = params.get("request")
            if not isinstance(request, Mapping):
                return
            category = endpoint_category(request.get("url"))
            kind = str(params.get("type") or "")
            if not category or kind not in KINDS:
                return
            verb = str(request.get("method") or "").upper()
            self.requests[key] = (category, kind, verb if verb in METHODS else "OTHER")
            while len(self.requests) > MAX_REQUESTS:
                self.requests.popitem(last=False)
            return
        prior = self.requests.pop(key, None)
        if prior is None:
            return
        if method == "Network.responseReceived":
            response = params.get("response")
            self._record_response(prior, response.get("status") if isinstance(response, Mapping) else None)
        else:
            raw = params.get("errorText")
            code = raw if isinstance(raw, str) and NETWORK_ERROR.fullmatch(raw) else "network_other"
            self._emit(category=prior[0], kind=prior[1], method=prior[2], outcome="network_error", code=code)

    def _record_response(self, prior: tuple[str, str, str], raw_status: object) -> None:
        if isinstance(raw_status, bool):
            return
        try:
            status = int(raw_status)  # CDP HTTP status is a number.
        except (TypeError, ValueError, OverflowError):
            return
        if not 100 <= status <= 599:
            return
        category, kind, method = prior
        # Keep the finite budget for the phone transition: routine GET assets
        # and background successes are deliberately omitted.
        if status >= 400 or method == "POST" or kind == "Document":
            self._emit(category=category, kind=kind, method=method, outcome="http", code=status)
