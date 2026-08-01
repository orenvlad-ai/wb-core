"""Static contract smoke for centralized source/session settings."""

from __future__ import annotations

from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SETTINGS_UI_PATH,
    DEFAULT_SOURCES_SESSIONS_PATH,
    DEFAULT_SPP_PROXY_SOURCE_CHECK_PATH,
    DEFAULT_WB_SUPPLIES_TRANSIT_COST_CHECK_PATH,
    _render_sheet_vitrina_settings_ui,
)
from packages.application.registry_upload_http_entrypoint import (  # noqa: E402
    RegistryUploadHttpEntrypoint,
)


def main() -> None:
    html = _render_sheet_vitrina_settings_ui()
    required = (
        'data-settings-group-button="sources-sessions"',
        'data-settings-group-panel="sources-sessions"',
        'data-source-card="seller"',
        'data-source-card="buyer"',
        'data-source-card="public"',
        "Seller Portal",
        "WB Buyer",
        "WB Card / SPP Proxy",
        "Авторизация и login UI не используются",
        "Проверка",
        "Автовход",
        "Человек",
        "Валидация",
        "Готово",
        "sourceCheckPromises",
        'String(window.location.hash || "").replace(/^#/, "")',
        "refreshStale: true",
        "Math.max(Number(ttlSeconds || 180), 60)",
        "window.setTimeout(poll, 2000)",
        DEFAULT_SOURCES_SESSIONS_PATH,
        DEFAULT_SPP_PROXY_SOURCE_CHECK_PATH,
        DEFAULT_WB_SUPPLIES_TRANSIT_COST_CHECK_PATH,
    )
    missing = [item for item in required if item not in html]
    if missing:
        raise AssertionError(f"centralized sources/session UI contract is incomplete: {missing}")
    public_match = re.search(
        r'<article[^>]+data-source-card="public"[\s\S]*?</article>',
        html,
    )
    public_card = public_match.group(0) if public_match else ""
    if not public_card or "data-source-recover" in public_card or "Launcher" in public_card:
        raise AssertionError("public WB Card/SPP Proxy must not expose login or recovery UI")
    for source in ("seller", "buyer"):
        if f'data-source-check="{source}"' not in html or f'data-source-recover="{source}"' not in html:
            raise AssertionError(f"{source} must expose centralized exact check and recovery actions")
    if DEFAULT_SETTINGS_UI_PATH != "/sheet-vitrina-v1/settings":
        raise AssertionError("settings route changed unexpectedly")
    _check_cached_status_composition()
    print("sheet_vitrina_v1_settings_sources_sessions_smoke: OK")


def _check_cached_status_composition() -> None:
    class Runtime:
        def load_source_health_status(self, source_key: str):
            return {"source_key": source_key, "status": "available"}

        def load_sheet_vitrina_refresh_status(self):
            raise ValueError("fixture has no Vitrina status")

    class Supplies:
        def get_transit_cost_enrichment_status(self, _params):
            return {
                "coverage": {
                    "auth_status": "valid",
                    "route_status": "available",
                    "collector_status": "healthy",
                    "freshness_status": "fresh",
                    "confirmed": 3,
                    "eligible": 3,
                }
            }

    entrypoint = object.__new__(RegistryUploadHttpEntrypoint)
    entrypoint.runtime = Runtime()
    entrypoint.wb_supplies_block = Supplies()
    entrypoint.activated_at_factory = lambda: "2026-08-02T00:00:00Z"
    seller_probes: list[bool] = []
    buyer_probes: list[bool] = []
    entrypoint.handle_seller_portal_recovery_status_request = lambda **kwargs: (
        seller_probes.append(bool(kwargs.get("with_probe")))
        or {"session_status": "session_valid_canonical", "organization_confirmed": True}
    )
    entrypoint.handle_wb_buyer_session_recovery_status_request = lambda **kwargs: (
        buyer_probes.append(bool(kwargs.get("with_probe")))
        or {"status": "valid", "account_confirmed": True}
    )
    payload = entrypoint.handle_sources_sessions_status_request(
        seller_launcher_download_path="/seller.zip",
        buyer_launcher_download_path="/buyer.zip",
    )
    if seller_probes != [False] or buyer_probes != [False]:
        raise AssertionError("initial centralized read must use cached statuses without live probes")
    if (
        payload.get("refresh_ttl_seconds") != 180
        or payload.get("spp_proxy", {}).get("authorization_required") is not False
        or payload.get("wb_buyer", {}).get("capability", {}).get("source_key") != "wb_buyer_spp_capability"
        or payload.get("seller_portal", {}).get("transit_cost", {}).get("coverage", {}).get("confirmed") != 3
    ):
        raise AssertionError(f"centralized source/session payload lost layered status truth: {payload}")


if __name__ == "__main__":
    main()
