"""Fake-browser/security smoke for the isolated WB buyer-session contour."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
from tempfile import TemporaryDirectory
from typing import Any, Mapping
import zipfile
from io import BytesIO


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.wb_buyer_session_recovery import (  # noqa: E402
    BuyerRecoveryConfig,
    _open_secure_log,
    _write_status,
    build_macos_launcher_archive,
)
from packages.adapters.wb_buyer_session import (  # noqa: E402
    WbBuyerSessionAdapter,
    WbBuyerSessionConfig,
    extract_authenticated_price_from_network_payload,
)
from packages.application.wb_buyer_session import WbBuyerSessionBlock  # noqa: E402
from packages.application.wb_spp_tester import _buyer_contexts_compatible, _stable_buyer_pair  # noqa: E402


NM_ID = 123456789


def main() -> None:
    _run_session_and_security_smoke()
    _run_price_extraction_smoke()
    _run_launcher_smoke()
    print("wb_buyer_session_smoke: OK")


def _run_session_and_security_smoke() -> None:
    with TemporaryDirectory(prefix="wb-buyer-session-") as tmp:
        state_dir = Path(tmp) / "state"
        config = WbBuyerSessionConfig(state_dir=state_dir, storage_state_path=state_dir / "storage_state.json")
        missing = WbBuyerSessionAdapter(config=config, browser_probe=lambda _path: {"status": "valid", "identity_material": {"user_id": "never"}}).check_session()
        if missing["status"] != "missing":
            raise AssertionError(f"missing state must be controlled: {missing}")

        state_dir.mkdir(parents=True, exist_ok=True)
        config.storage_state_path.write_text(
            json.dumps(
                {
                    "cookies": [{"domain": ".wildberries.ru", "name": "auth_token", "value": "raw-cookie-secret"}],
                    "origins": [{"origin": "https://www.wildberries.ru", "localStorage": [{"name": "profile", "value": json.dumps({"userId": "raw-account-id"})}]}],
                }
            ),
            encoding="utf-8",
        )
        os.chmod(config.storage_state_path, 0o644)
        valid_adapter = WbBuyerSessionAdapter(
            config=config,
            browser_probe=lambda _path: {"status": "valid", "identity_material": {"user_id": "raw-account-id"}},
        )
        valid = valid_adapter.check_session()
        serialized = json.dumps(valid, ensure_ascii=False)
        if valid["status"] != "valid" or len(valid["session_fingerprint"]) != 64:
            raise AssertionError(f"first login fingerprint failed: {valid}")
        if "raw-account-id" in serialized or "raw-cookie-secret" in serialized:
            raise AssertionError("session API leaked raw buyer identity or cookie")
        if stat.S_IMODE(state_dir.stat().st_mode) != 0o700 or stat.S_IMODE(config.storage_state_path.stat().st_mode) != 0o600:
            raise AssertionError("buyer state permissions must be 0700/0600")
        for protected in (config.fingerprint_key_path, config.fingerprint_record_path, config.probe_metadata_path):
            if not protected.exists() or stat.S_IMODE(protected.stat().st_mode) != 0o600:
                raise AssertionError(f"protected buyer metadata has wrong mode: {protected}")

        restarted = WbBuyerSessionAdapter(
            config=config,
            browser_probe=lambda _path: {"status": "valid", "identity_material": {"user_id": "raw-account-id"}},
        ).check_session()
        if restarted["status"] != "valid" or restarted["session_fingerprint"] != valid["session_fingerprint"]:
            raise AssertionError("fingerprint must survive process restart")

        wrong = WbBuyerSessionAdapter(
            config=config,
            browser_probe=lambda _path: {"status": "valid", "identity_material": {"user_id": "other-account"}},
        ).check_session()
        if wrong["status"] != "wrong_account":
            raise AssertionError(f"wrong account must be detected: {wrong}")
        expired = WbBuyerSessionAdapter(
            config=config,
            browser_probe=lambda _path: {"status": "expired", "reason": "buyer_login_required"},
        ).check_session()
        if expired["status"] != "expired":
            raise AssertionError(f"expired session must be controlled: {expired}")
        for blocked_status in ("login_redirect", "security_challenge"):
            blocked = WbBuyerSessionAdapter(
                config=config,
                browser_probe=lambda _path, status=blocked_status: {
                    "status": status,
                    "reason": f"buyer_{status}",
                },
            ).check_session()
            if blocked["status"] != blocked_status:
                raise AssertionError(f"{blocked_status} must stay distinct: {blocked}")
        unsafe_reason_block = WbBuyerSessionBlock(
            adapter=WbBuyerSessionAdapter(
                config=config,
                browser_probe=lambda _path: {"status": "expired", "reason": "phone=raw-account-id"},
            )
        ).check_session()
        if "raw-account-id" in json.dumps(unsafe_reason_block, ensure_ascii=False):
            raise AssertionError("buyer session public reason must not expose raw personal context")

        candidate = state_dir / "candidate.json"
        candidate.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
        valid_adapter.persist_storage_state_atomically(candidate)
        if stat.S_IMODE(config.storage_state_path.stat().st_mode) != 0o600:
            raise AssertionError("atomic buyer storage save must preserve 0600")


def _run_price_extraction_smoke() -> None:
    payload = {
        "data": {
            "products": [
                {
                    "id": NM_ID,
                    "sizes": [
                        {
                            "price": {
                                "basic": 50000,
                                "product": 38600,
                                "wallet": 37800,
                                "card": 38100,
                                "club": 37500,
                            }
                        }
                    ],
                }
            ]
        }
    }
    extracted = extract_authenticated_price_from_network_payload(
        payload,
        nm_id=NM_ID,
        response_url="https://card.wb.ru/cards/v4/detail?dest=-1257786&curr=rub&locale=ru",
    )
    if (
        extracted["authenticated_buyer_price"] != 386.0
        or extracted["wallet_price"] != 378.0
        or extracted["card_price"] != 381.0
        or extracted["club_price"] != 375.0
        or extracted["payment_context"] != "account_default_with_wallet_and_card_and_club_option"
        or extracted["destination_context"].get("dest") != "-1257786"
    ):
        raise AssertionError(f"authenticated network price extraction failed: {extracted}")

    ambiguous = extract_authenticated_price_from_network_payload(
        {"products": [{"id": NM_ID, "salePriceU": 38600}]},
        nm_id=NM_ID,
        response_url="https://www.wildberries.ru/api/product",
    )
    if ambiguous["authenticated_buyer_price"] != 386.0 or ambiguous["payment_context"] != "account_default":
        raise AssertionError(f"ambiguous payment context must stay explicit: {ambiguous}")

    matching_authenticated = {
        "authenticated_buyer_price": 386.0,
        "session_fingerprint": "a" * 64,
        "destination_context": {"dest": "-1257786", "currency": "rub"},
    }
    matching_anonymous = {
        "public_buyer_price": 394.0,
        "destination_context": {"dest": "-1257786", "currency": "rub"},
    }
    stable_reads = [
        {"authenticated": dict(matching_authenticated), "anonymous": dict(matching_anonymous)},
        {"authenticated": dict(matching_authenticated), "anonymous": dict(matching_anonymous)},
    ]
    if _stable_buyer_pair(stable_reads) != (386.0, 394.0):
        raise AssertionError("two identical authenticated+anonymous reads must form stable proof")
    changed_context_reads = [dict(stable_reads[0]), {
        "authenticated": {**matching_authenticated, "destination_context": {"dest": "-2133462", "currency": "rub"}},
        "anonymous": {**matching_anonymous, "destination_context": {"dest": "-2133462", "currency": "rub"}},
    }]
    if _stable_buyer_pair(changed_context_reads) is not None:
        raise AssertionError("two price reads under different destination contexts must not form stable proof")
    mismatched_anonymous = {
        **matching_anonymous,
        "destination_context": {"dest": "-2133462", "currency": "rub"},
    }
    if _buyer_contexts_compatible(matching_authenticated, mismatched_anonymous):
        raise AssertionError("different buyer destination must block authenticated/anonymous comparison")

    with TemporaryDirectory(prefix="wb-buyer-price-") as tmp:
        state_dir = Path(tmp)
        config = WbBuyerSessionConfig(state_dir=state_dir, storage_state_path=state_dir / "storage_state.json")
        config.storage_state_path.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
        adapter = WbBuyerSessionAdapter(
            config=config,
            browser_probe=lambda _path: {"status": "valid", "identity_material": {"user_id": "safe-source"}},
            price_probe=lambda _path, nm_id: {
                **extracted,
                "nm_id": nm_id,
                "source_endpoint": "https://card.wb.ru/cards/v4/detail?token=must-be-stripped",
                "diagnostics": {"network_primary": True, "authorization": "must-not-pass"},
            },
        )
        public = WbBuyerSessionBlock(adapter=adapter).fetch_authenticated_buyer_price(NM_ID)
        serialized = json.dumps(public, ensure_ascii=False).lower()
        if public["status"] != "ok" or public["source_endpoint"] != "https://card.wb.ru/cards/v4/detail":
            raise AssertionError(f"safe price application contract failed: {public}")
        if "must-be-stripped" in serialized or "authorization" in serialized or "cookie" in serialized:
            raise AssertionError("authenticated price API leaked sensitive request material")


def _run_launcher_smoke() -> None:
    with TemporaryDirectory(prefix="wb-buyer-launcher-") as tmp:
        state_dir = Path(tmp)
        session = WbBuyerSessionConfig(state_dir=state_dir, storage_state_path=state_dir / "storage_state.json")
        config = BuyerRecoveryConfig(session=session, ssh_destination="wb-core-eu-root")
        state_dir.mkdir(parents=True, exist_ok=True)
        _write_status(
            config,
            {
                "run_id": "buyer-recovery-test",
                "status": "awaiting_login",
                "reason": "buyer_login_window_ready",
                "started_at": "2026-07-14T00:00:00+00:00",
                "session": {"status": "missing", "valid": False},
            },
        )
        config.pid_path.write_text(str(os.getpid()), encoding="utf-8")
        secure_log_path = state_dir / "recovery-test.log"
        with _open_secure_log(secure_log_path) as log_handle:
            log_handle.write(b"safe-status-only\n")
        if stat.S_IMODE(secure_log_path.stat().st_mode) != 0o600:
            raise AssertionError("buyer recovery log files must be forced to 0600")
        archive, filename = build_macos_launcher_archive(
            config,
            public_status_url="https://api.selleros.pro/v1/sheet-vitrina-v1/prices/spp-test/buyer-session/recovery/status",
            public_operator_url="https://api.selleros.pro/sheet-vitrina-v1/vitrina",
        )
        if not filename.endswith(".zip"):
            raise AssertionError("buyer launcher must be a zip")
        with zipfile.ZipFile(BytesIO(archive)) as zip_file:
            body = zip_file.read(zip_file.namelist()[0]).decode("utf-8")
        lowered = body.lower()
        if "127.0.0.1:46090" not in body or "ssh -o exitonforwardfailure" not in lowered:
            raise AssertionError("buyer launcher must use the localhost SSH tunnel pattern")
        if "MAX_POLLS=" not in body or "MISSING_POLLS" not in body or "trap cleanup EXIT" not in body:
            raise AssertionError("buyer launcher must have bounded lifecycle and tunnel cleanup")
        if "storage_state" in lowered or "raw-cookie" in lowered or "authorization" in lowered or "otp" in lowered:
            raise AssertionError("buyer launcher must not contain persistent credentials or storage state")


if __name__ == "__main__":
    main()
