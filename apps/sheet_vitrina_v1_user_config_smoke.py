"""Targeted smoke for server-side web-vitrina user presentation config."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402


CONFIG_A = {
    "version": 2,
    "scopes": {
        "total": {
            "order": ["avg_ctr_current", "total_orderSum"],
            "display": {"total_orderSum": "collapsed"},
            "manual": True,
        },
        "sku": {
            "order": ["ctr_current", "views_current"],
            "display": {"views_current": "hidden"},
            "manual": True,
        },
    },
    "expanded_anchors": ["total::avg_ctr_current"],
}


def main() -> None:
    with TemporaryDirectory(prefix="sheet-vitrina-user-config-") as tmp:
        entrypoint = RegistryUploadHttpEntrypoint(runtime_dir=Path(tmp))

        missing = entrypoint.handle_sheet_web_vitrina_user_config_request(user_key="operator-a")
        if missing["status"] != "missing" or missing["revision"] != 0:
            raise AssertionError(f"new account must start with missing/default config, got {missing}")

        saved = entrypoint.handle_sheet_web_vitrina_user_config_save_request(
            user_key="operator-a",
            payload={"base_revision": 0, "config": CONFIG_A},
        )
        if saved["status"] != "ok" or saved["revision"] != 1:
            raise AssertionError(f"first save must create revision 1, got {saved}")
        if saved["config"]["scopes"]["sku"]["display"] != {"views_current": "hidden"}:
            raise AssertionError(f"presentation display state was not preserved, got {saved}")

        loaded = entrypoint.handle_sheet_web_vitrina_user_config_request(user_key="operator-a")
        if loaded["config"] != saved["config"]:
            raise AssertionError(f"reload/new browser must read server config, got {loaded}")

        user_b = entrypoint.handle_sheet_web_vitrina_user_config_request(user_key="operator-b")
        if user_b["status"] != "missing":
            raise AssertionError(f"second account must not inherit operator-a config, got {user_b}")

        conflict = entrypoint.handle_sheet_web_vitrina_user_config_save_request(
            user_key="operator-a",
            payload={"base_revision": 0, "config": {"version": 2, "scopes": {}, "expanded_anchors": []}},
        )
        if conflict["status"] != "conflict" or conflict["current"]["revision"] != 1:
            raise AssertionError(f"stale browser revision must conflict, got {conflict}")

        latest = {
            "version": 2,
            "scopes": {"total": {"order": ["total_orderSum", "avg_ctr_current"], "display": {}, "manual": True}},
            "expanded_anchors": [],
        }
        second_save = entrypoint.handle_sheet_web_vitrina_user_config_save_request(
            user_key="operator-a",
            payload={"base_revision": 1, "config": latest},
        )
        if second_save["revision"] != 2 or second_save["config"]["scopes"]["total"]["order"][0] != "total_orderSum":
            raise AssertionError(f"latest rapid-change save must win with next revision, got {second_save}")

        sanitized = entrypoint.handle_sheet_web_vitrina_user_config_save_request(
            user_key="operator-b",
            payload={
                "base_revision": 0,
                "config": {
                    "version": 2,
                    "date_from": "2026-04-20",
                    "date_to": "2026-04-24",
                    "preset": "legacy",
                    "scopes": {
                        "sku": {
                            "order": ["ctr_current", "ctr_current", ""],
                            "display": {"ctr_current": "unsupported", "views_current": "shown", "orders_current": "hidden"},
                            "manual": "yes",
                            "date_from": "2026-04-20",
                            "date_to": "2026-04-24",
                        }
                    },
                    "expanded_anchors": ["sku::ctr_current", "sku::ctr_current"],
                },
            },
        )
        if sanitized["config"]["scopes"]["sku"]["order"] != ["ctr_current"]:
            raise AssertionError(f"duplicate/empty metric keys must be sanitized, got {sanitized}")
        if sanitized["config"]["scopes"]["sku"]["display"] != {"orders_current": "hidden"}:
            raise AssertionError(f"unsupported/shown display values must be sanitized, got {sanitized}")
        if sanitized["config"]["expanded_anchors"] != ["sku::ctr_current"]:
            raise AssertionError(f"expanded anchors must be deduplicated, got {sanitized}")
        sanitized_raw = str(sanitized["config"])
        if "2026-04-20" in sanitized_raw or "2026-04-24" in sanitized_raw or "legacy" in sanitized_raw:
            raise AssertionError(f"legacy period fields must not survive server user-config sanitization, got {sanitized}")

    print({"status": "ok", "checks": ["missing", "save", "reload", "multi_user", "conflict", "sanitize", "legacy_period_drop"]})


if __name__ == "__main__":
    main()
