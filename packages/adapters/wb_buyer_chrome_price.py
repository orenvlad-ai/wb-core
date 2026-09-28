"""Existing buyer-price application port backed by the durable Chrome profile."""

from __future__ import annotations

import importlib
from typing import Any

from packages.adapters.wb_buyer_session import load_wb_buyer_session_config_from_env


class WbBuyerChromePriceAdapter:
    def __init__(self) -> None:
        self.config = load_wb_buyer_session_config_from_env()

    @staticmethod
    def _auth() -> Any:
        return importlib.import_module("apps.wb_buyer_chrome_auth")

    def check_session(self) -> dict[str, Any]:
        current = self._auth().raw_status()
        confirmed = current.get("status") == "completed" and current.get("login_confirmed") is True
        return {
            "status": "authenticated_surface" if confirmed else "recovery_running" if current.get("running") else "missing",
            "reason": "" if confirmed else "buyer_recovery_in_progress" if current.get("running") else "buyer_login_required",
            "checked_at": str((current.get("session") or {}).get("checked_at") or current.get("finished_at") or ""),
            "account_confirmed": False, "authenticated_session_proof": confirmed,
            "persistent_profile": True, "recovery_run_id": str(current.get("run_id") or ""),
        }

    def fetch_authenticated_buyer_price(self, nm_id: int) -> dict[str, Any]:
        reader = importlib.import_module("apps.wb_buyer_chrome_price")
        return reader.read_price(int(nm_id))

    def fetch_stable_authenticated_buyer_price(self, nm_id: int) -> dict[str, Any]:
        # The one Chrome operation obtains two matching DOM reads. A second
        # browser launch would add latency without stronger identity proof.
        return self.fetch_authenticated_buyer_price(int(nm_id))
