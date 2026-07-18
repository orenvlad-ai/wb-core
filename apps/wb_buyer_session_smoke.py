"""Fake-browser/security smoke for the isolated WB buyer-session contour."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from typing import Any, Mapping
import zipfile
from io import BytesIO


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.wb_buyer_session_recovery import (  # noqa: E402
    BuyerRecoveryConfig,
    _accept_recovery_candidate,
    _capture_settled_candidate,
    _click_saved_account,
    _inspect_login_surface,
    _open_secure_log,
    _spawn,
    _terminate,
    _terminate_process_group,
    _write_supervisor_identity,
    _write_status,
    build_macos_launcher_archive,
    stop_recovery,
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
    _run_recovery_lifecycle_smoke()
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

        state_payload = json.loads(config.storage_state_path.read_text(encoding="utf-8"))
        state_payload["cookies"][0]["value"] = "rotated-auth-token"
        config.storage_state_path.write_text(json.dumps(state_payload), encoding="utf-8")
        rotated = WbBuyerSessionAdapter(
            config=config,
            browser_probe=lambda _path: {"status": "valid", "identity_material": {"userId": "raw-account-id", "session": "rotated"}},
        ).check_session()
        if rotated["status"] != "valid" or rotated["session_fingerprint"] != valid["session_fingerprint"]:
            raise AssertionError("auth/session token rotation for the same stable user must not become wrong_account")

        bound_fingerprint = valid_adapter.stored_fingerprint()
        wrong = WbBuyerSessionAdapter(
            config=config,
            browser_probe=lambda _path: {"status": "valid", "identity_material": {"user_id": "other-account"}},
        ).check_session()
        if wrong["status"] != "wrong_account" or valid_adapter.stored_fingerprint() != bound_fingerprint:
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

    _run_fingerprint_migration_smoke()


def _run_fingerprint_migration_smoke() -> None:
    with TemporaryDirectory(prefix="wb-buyer-fingerprint-migration-") as tmp:
        state_dir = Path(tmp)
        config = WbBuyerSessionConfig(state_dir=state_dir, storage_state_path=state_dir / "storage_state.json")
        state_dir.mkdir(parents=True, exist_ok=True)
        config.storage_state_path.write_text(
            json.dumps({"cookies": [], "origins": [{"origin": "https://www.wildberries.ru", "localStorage": [{"name": "profile", "value": json.dumps({"phone": "private-phone", "user_id": "stable-user"})}]}]}),
            encoding="utf-8",
        )
        adapter = WbBuyerSessionAdapter(config=config)
        legacy = adapter._fingerprint({"user_id": "stable-user"})
        config.fingerprint_record_path.write_text(
            json.dumps({"version": "wb-buyer-account-hmac-sha256-v1", "fingerprint": legacy, "created_at": "2026-07-01T00:00:00+00:00"}),
            encoding="utf-8",
        )
        migrated = adapter.prepare_fingerprint_migration()
        record = json.loads(config.fingerprint_record_path.read_text(encoding="utf-8"))
        if migrated.get("status") != "ready" or not migrated.get("migrated") or "v2-stable-identity" not in record.get("version", ""):
            raise AssertionError(f"provable legacy stable identity must migrate safely: {migrated} {record}")
        if "private-phone" in json.dumps(record):
            raise AssertionError("fingerprint migration record must not persist raw identity")

        record_before = dict(record)
        record_before["version"] = "wb-buyer-account-hmac-sha256-v1"
        record_before["fingerprint"] = adapter._fingerprint({"cookies": [["wildberries.ru", "auth", "rotating-secret"]]})
        config.fingerprint_record_path.write_text(json.dumps(record_before), encoding="utf-8")
        unproven = adapter.prepare_fingerprint_migration()
        after = json.loads(config.fingerprint_record_path.read_text(encoding="utf-8"))
        if unproven.get("status") != "migration_required" or after.get("fingerprint") != record_before["fingerprint"]:
            raise AssertionError("unprovable token-derived fingerprint must fail closed without rebinding")


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
        supervisor = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)", "wb_buyer_session_recovery.py", "supervise"],
            start_new_session=True,
        )
        _write_status(
            config,
            {
                "run_id": "buyer-recovery-test",
                "status": "awaiting_human",
                "reason": "buyer_sms_required",
                "started_at": "2026-07-14T00:00:00+00:00",
                "session": {"status": "missing", "valid": False},
            },
        )
        _write_supervisor_identity(config, pid=supervisor.pid, run_id="buyer-recovery-test")
        secure_log_path = state_dir / "recovery-test.log"
        with _open_secure_log(secure_log_path) as log_handle:
            log_handle.write(b"safe-status-only\n")
        if stat.S_IMODE(secure_log_path.stat().st_mode) != 0o600:
            raise AssertionError("buyer recovery log files must be forced to 0600")
        try:
            archive, filename = build_macos_launcher_archive(
                config,
                public_status_url="https://api.selleros.pro/v1/sheet-vitrina-v1/prices/spp-test/buyer-session/recovery/status",
                public_operator_url="https://api.selleros.pro/sheet-vitrina-v1/vitrina",
            )
        finally:
            _terminate_process_group(supervisor.pid)
        if not filename.endswith(".zip"):
            raise AssertionError("buyer launcher must be a zip")
        with zipfile.ZipFile(BytesIO(archive)) as zip_file:
            body = zip_file.read(zip_file.namelist()[0]).decode("utf-8")
        lowered = body.lower()
        if "127.0.0.1:46090" not in body or "ssh -o exitonforwardfailure" not in lowered:
            raise AssertionError("buyer launcher must use the localhost SSH tunnel pattern")
        if "MAX_POLLS=" not in body or "WB_BUYER_RECOVERY_FINAL=" not in body or "trap cleanup EXIT" not in body:
            raise AssertionError("buyer launcher must have bounded terminal-status lifecycle and tunnel cleanup")
        if "status --run-id" not in body or "close_novnc" not in body or "MISSING_POLLS" in body:
            raise AssertionError("buyer launcher must close by exact recovery status, not only by disappearing port")
        if "storage_state" in lowered or "raw-cookie" in lowered or "authorization" in lowered or "otp" in lowered:
            raise AssertionError("buyer launcher must not contain persistent credentials or storage state")


def _run_recovery_lifecycle_smoke() -> None:
    class FakePage:
        url = "https://www.wildberries.ru/lk"

        def __init__(self) -> None:
            self.events: list[str] = []
            self.wb_recovery_surface = {"state": "authenticated", "reason": "buyer_visible_account_opened"}

        def wait_for_timeout(self, milliseconds: int) -> None:
            self.events.append(f"wait:{milliseconds}")

    class FakeContext:
        def __init__(self, events: list[str]) -> None:
            self.events = events

        def storage_state(self, *, path: str) -> None:
            self.events.append("snapshot")
            Path(path).write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")

    class FakeAdapter:
        def __init__(self, result: Mapping[str, Any]) -> None:
            self.result = dict(result)

        def check_session(self, **_kwargs: Any) -> dict[str, Any]:
            page.events.append("probe")
            return dict(self.result)

    with TemporaryDirectory(prefix="wb-buyer-recovery-lifecycle-") as tmp:
        state_dir = Path(tmp)
        session = WbBuyerSessionConfig(state_dir=state_dir, storage_state_path=state_dir / "storage_state.json")
        config = BuyerRecoveryConfig(session=session, poll_sec=0.75)
        state_dir.mkdir(parents=True, exist_ok=True)
        page = FakePage()
        fresh = FakeAdapter({"status": "valid", "valid": True, "account_confirmed": True})
        recovered = _capture_settled_candidate(
            config,
            FakeAdapter({"status": "login_redirect", "valid": False}),
            FakeContext(page.events),
            page,
            fresh_adapter_factory=lambda **_kwargs: fresh,
        )
        if recovered.get("status") != "valid" or page.events != ["wait:8000", "snapshot", "probe"]:
            raise AssertionError(f"candidate must be captured after settle and then independently probed: {page.events}")

        if _inspect_login_surface(page).get("state") != "authenticated":
            raise AssertionError("visible authenticated /lk surface must be classified")
        page.wb_recovery_surface = {"state": "human", "reason": "buyer_sms_required"}
        if _inspect_login_surface(page).get("reason") != "buyer_sms_required":
            raise AssertionError("SMS surface must stay awaiting_human")
        clicked: list[bool] = []
        class FakeButton:
            def click(self, **_kwargs: Any) -> None:
                clicked.append(True)
        if not _click_saved_account({"candidate": FakeButton()}) or clicked != [True]:
            raise AssertionError("one saved account login action must be clicked automatically")

        class FakeLoginButton:
            def __init__(self, text: str) -> None:
                self.text = text

            def inner_text(self, **_kwargs: Any) -> str:
                return self.text

            def is_visible(self) -> bool:
                return True

            def is_enabled(self) -> bool:
                return True

        class FakeLoginLocator:
            def __init__(self, items: list[FakeLoginButton]) -> None:
                self.items = items

            def count(self) -> int:
                return len(self.items)

            def nth(self, index: int) -> FakeLoginButton:
                return self.items[index]

        class FakeLoginPage:
            def __init__(self, items: list[FakeLoginButton]) -> None:
                self.items = items

            def locator(self, _selector: str) -> FakeLoginLocator:
                return FakeLoginLocator(self.items)

        saved_page = FakeLoginPage([FakeLoginButton("Войти под этим аккаунтом")])
        saved_surface = _inspect_login_surface(saved_page)  # type: ignore[arg-type]
        if saved_surface.get("state") != "automatic_login":
            raise AssertionError("a single saved-account Войти control must be classified for auto-login")

        config.session.storage_state_path.write_text("old-canonical-state", encoding="utf-8")
        config.candidate_path.write_text("new-candidate-state", encoding="utf-8")
        _write_status(config, {"run_id": "rollback", "status": "saving_session", "reason": "buyer_session_saving"})
        class FailedFinalProbeAdapter:
            def persist_storage_state_atomically(self, candidate_path: Path) -> None:
                config.session.storage_state_path.write_text(candidate_path.read_text(encoding="utf-8"), encoding="utf-8")

            def check_session(self, **_kwargs: Any) -> Mapping[str, Any]:
                return {"status": "expired", "valid": False, "reason": "buyer_login_required"}
        failed = _accept_recovery_candidate(
            config,
            FailedFinalProbeAdapter(),  # type: ignore[arg-type]
            {"status": "valid", "valid": True},
        )
        if failed != 1 or config.session.storage_state_path.read_text(encoding="utf-8") != "old-canonical-state":
            raise AssertionError("failed final probe must atomically restore the prior canonical storage state")

        config.session.storage_state_path.unlink()
        config.candidate_path.write_text("first-candidate-state", encoding="utf-8")
        failed_without_prior = _accept_recovery_candidate(
            config,
            FailedFinalProbeAdapter(),  # type: ignore[arg-type]
            {"status": "valid", "valid": True},
        )
        if failed_without_prior != 1 or config.session.storage_state_path.exists():
            raise AssertionError("failed first binding must not leave an unvalidated canonical storage state")

        child_log = state_dir / "spawn-child.log"
        child = _spawn([sys.executable, "-c", "import time; time.sleep(30)"], child_log)
        try:
            if os.getpgid(child.pid) != os.getpgrp():
                raise AssertionError("recovery child must stay inside the supervisor process group")
        finally:
            _terminate(child)
        if child.poll() is None:
            raise AssertionError("normal recovery cleanup must terminate a child process")

        child_pid_path = state_dir / "child.pid"
        parent_code = (
            "import subprocess,sys,time;"
            f"p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
            f"open({str(child_pid_path)!r},'w').write(str(p.pid));"
            "time.sleep(60)"
        )
        parent = subprocess.Popen(
            [sys.executable, "-c", parent_code, "wb_buyer_session_recovery.py", "supervise"],
            start_new_session=True,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not child_pid_path.exists():
            time.sleep(0.05)
        if not child_pid_path.exists():
            parent.kill()
            raise AssertionError("stop lifecycle fixture did not start")
        nested_pid = int(child_pid_path.read_text(encoding="utf-8"))
        _write_supervisor_identity(config, pid=parent.pid, run_id="lifecycle")
        config.candidate_path.write_text("candidate-secret", encoding="utf-8")
        _write_status(
            config,
            {"run_id": "lifecycle", "status": "awaiting_human", "reason": "buyer_sms_required"},
        )
        stop_recovery(config, requested_run_id="lifecycle")
        parent.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and Path(f"/proc/{nested_pid}").exists():
            time.sleep(0.05)
        if Path(f"/proc/{nested_pid}").exists():
            raise AssertionError("stop must terminate the complete isolated recovery process group")
        if config.candidate_path.exists() or config.pid_path.exists():
            raise AssertionError("stop must remove temporary candidate state and supervisor pid")


if __name__ == "__main__":
    main()
