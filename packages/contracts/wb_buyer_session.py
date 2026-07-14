"""Contracts for the isolated Wildberries buyer-session and price source."""

from __future__ import annotations

from typing import Any, Mapping, Protocol


WB_BUYER_SESSION_CONTRACT_PREFIX = "wb_buyer_session"
WB_BUYER_SESSION_VALID_STATUS = "valid"
WB_BUYER_SESSION_BLOCKING_STATUSES = {
    "missing",
    "expired",
    "wrong_account",
    "login_redirect",
    "security_challenge",
    "probe_error",
    "recovery_running",
}


class WbAuthenticatedBuyerPriceSource(Protocol):
    """Safe application boundary consumed by the SPP tester."""

    def check_session(self) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")

    def fetch_authenticated_buyer_price(self, nm_id: int) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")
