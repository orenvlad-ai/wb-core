"""End-to-end smoke for the persistent WB buyer-session profile."""

from __future__ import annotations

from io import BytesIO
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


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import wb_buyer_session_recovery as recovery_tool  # noqa: E402
from apps.wb_buyer_session_recovery import (  # noqa: E402
    BuyerRecoveryConfig,
    _click_saved_account,
    _inspect_login_surface,
    _open_secure_log,
    _terminate_process_group,
    _write_status,
    _write_supervisor_identity,
    build_macos_launcher_archive,
    stop_recovery,
)
from packages.adapters.wb_buyer_session import (  # noqa: E402
    FINGERPRINT_VERSION,
    WbBuyerSessionAdapter,
    WbBuyerSessionConfig,
    extract_authenticated_price_from_network_payload,
)
from packages.application.wb_spp_tester import _stable_authenticated_price  # noqa: E402
from packages.application.wb_buyer_session import WbBuyerSessionBlock  # noqa: E402


NM_ID = 210183919


def main() -> None:
    _run_architecture_guard()
    _run_profile_adapter_smoke()
    _run_security_challenge_classification_smoke()
    _run_price_extraction_smoke()
    _run_recovery_persistent_e2e()
    _run_single_flight_start_smoke()
    _run_stop_and_launcher_smoke()
    print("wb_buyer_session_smoke: OK")


def _run_architecture_guard() -> None:
    source = (ROOT / "packages/adapters/wb_buyer_session.py").read_text(encoding="utf-8")
    recovery = (ROOT / "apps/wb_buyer_session_recovery.py").read_text(encoding="utf-8")
    combined = source + recovery
    forbidden = (
        ".new_context(storage_state=",
        "browser.new_context(",
        "context.storage_state(",
        "candidate_storage_state.json",
        "indexed_db=True",
        '"stabilizing_session"',
    )
    present = [token for token in forbidden if token in combined]
    if present:
        raise AssertionError(f"snapshot/import buyer-session architecture returned: {present}")
    if combined.count("launch_persistent_context(") < 2:
        raise AssertionError("all buyer browser paths must be persistent-profile launches")


def _run_profile_adapter_smoke() -> None:
    with TemporaryDirectory(prefix="wb-buyer-profile-") as tmp:
        state_dir = Path(tmp) / "state"
        config = WbBuyerSessionConfig(
            state_dir=state_dir,
            storage_state_path=state_dir / "storage_state.json",
            validation_nm_id=NM_ID,
        )
        calls: list[tuple[Path, int | None, bool]] = []
        session_result: dict[str, Any] = {"status": "expired", "reason": "buyer_login_required"}

        def operation(profile_dir: Path, nm_id: int | None, headless: bool) -> Mapping[str, Any]:
            calls.append((profile_dir, nm_id, headless))
            price = _ok_price(nm_id or NM_ID) if nm_id is not None and session_result.get("status") == "valid" else {}
            return {"session": dict(session_result), "price": price}

        adapter = WbBuyerSessionAdapter(config=config, operation_probe=operation)
        logged_out = adapter.check_session()
        if logged_out.get("status") != "expired":
            raise AssertionError(f"logout must be detected from the persistent profile: {logged_out}")
        if not config.persistent_profile_dir.is_dir() or stat.S_IMODE(config.persistent_profile_dir.stat().st_mode) != 0o700:
            raise AssertionError("persistent Chromium user_data_dir must be created with mode 0700")

        session_result.clear()
        session_result.update({"status": "valid", "reason": "buyer_session_valid"})
        without_identity = adapter.check_session()
        if (
            without_identity.get("status") != "valid"
            or without_identity.get("reason") != "buyer_account_context_missing"
            or without_identity.get("account_confirmed")
        ):
            raise AssertionError(f"missing account fingerprint data must not become logout: {without_identity}")
        price_without_identity = adapter.fetch_authenticated_buyer_price(NM_ID)
        if price_without_identity.get("status") != "ok" or not price_without_identity.get("authenticated_session_proof"):
            raise AssertionError(f"read-only SPP must work without fingerprint heuristics: {price_without_identity}")

        session_result["identity_material"] = {"user_id": "stable-account-id"}
        bound = adapter.check_session()
        record = json.loads(config.fingerprint_record_path.read_text(encoding="utf-8"))
        if bound.get("status") != "valid" or len(str(bound.get("session_fingerprint") or "")) != 64:
            raise AssertionError(f"stable authenticated account id must bind an HMAC: {bound}")
        if record.get("version") != FINGERPRINT_VERSION or "stable-account-id" in json.dumps(record):
            raise AssertionError("fingerprint record must contain only the stable-account HMAC")
        session_result["identity_material"] = {"user_id": "different-account-id"}
        mismatch = adapter.check_session()
        if mismatch.get("status") != "wrong_account":
            raise AssertionError(f"a real stable account-id mismatch must be blocked: {mismatch}")
        session_result["identity_material"] = {"user_id": "stable-account-id"}
        capability_call_count = len(calls)
        capability = WbBuyerSessionBlock(adapter=adapter).check_spp_capability()
        if (
            capability.get("status") != "valid"
            or capability.get("capability") != "authenticated_buyer_price"
            or capability.get("capability_status") != "available"
            or capability.get("capability_valid") is not True
            or capability.get("validation_nm_id") != NM_ID
        ):
            raise AssertionError(
                "buyer health must validate the exact authenticated-price capability: "
                + json.dumps(capability, ensure_ascii=False, sort_keys=True)
            )
        capability_calls = calls[capability_call_count:]
        if len(capability_calls) != 1 or capability_calls[0][1] != NM_ID:
            raise AssertionError(
                "exact buyer capability preflight must use one atomic persistent-price operation: "
                f"{capability_calls}"
            )

        if not calls or any(path != config.persistent_profile_dir for path, _nm_id, _headless in calls):
            raise AssertionError(f"all session/price operations must use one user_data_dir: {calls}")

        legacy_payload = {
            "cookies": [
                {
                    "domain": ".wildberries.ru",
                    "path": "/",
                    "name": "legacy-auth",
                    "value": "private-cookie",
                    "secure": True,
                    "httpOnly": True,
                }
            ],
            "origins": [
                {
                    "origin": "https://www.wildberries.ru",
                    "localStorage": [{"name": "legacy-profile", "value": "private-local-storage"}],
                }
            ],
        }
        config.storage_state_path.write_text(json.dumps(legacy_payload), encoding="utf-8")
        migration_context = _MigrationContext()
        first_migration = adapter.migrate_legacy_storage_state(migration_context)
        second_migration = adapter.migrate_legacy_storage_state(migration_context)
        if first_migration.get("status") != "attempted" or second_migration.get("status") != "already_attempted":
            raise AssertionError("legacy storage_state migration must be best-effort and one-time")
        if not config.storage_state_path.exists() or migration_context.cookies_added != 1 or migration_context.local_storage_sets != 1:
            raise AssertionError("legacy state must remain untouched after one-time profile migration")
        if stat.S_IMODE(config.legacy_migration_path.stat().st_mode) != 0o600:
            raise AssertionError("legacy migration marker must be protected")

        no_fingerprint_reads = [
            {
                "authenticated": {
                    **_ok_price(NM_ID),
                    "session_fingerprint": "",
                    "authenticated_session_proof": True,
                    "persistent_profile": True,
                },
                "anonymous": {"public_buyer_price": 394.0, "destination_context": _destination()},
            },
            {
                "authenticated": {
                    **_ok_price(NM_ID),
                    "session_fingerprint": "",
                    "authenticated_session_proof": True,
                    "persistent_profile": True,
                },
                "anonymous": {"public_buyer_price": 394.0, "destination_context": _destination()},
            },
        ]
        if _stable_authenticated_price(
            [dict(row["authenticated"]) for row in no_fingerprint_reads]
        ) != 386.0:
            raise AssertionError("stable authenticated browser proof must replace missing fingerprint heuristics")


def _run_security_challenge_classification_smoke() -> None:
    class Body:
        def inner_text(self, **_kwargs: Any) -> str:
            return "Что-то не так... Подозрительная активность. captcha-support@rwb.ru"

    class Response:
        status = 498

    class Page:
        url = "https://www.wildberries.ru/lk"

        def on(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def goto(self, *_args: Any, **_kwargs: Any) -> Response:
            return Response()

        def wait_for_timeout(self, _milliseconds: int) -> None:
            return None

        def locator(self, _selector: str) -> Body:
            return Body()

    page = Page()
    session = WbBuyerSessionAdapter()._probe_session_in_context(page)
    surface = _inspect_login_surface(page)
    if session.get("status") != "security_challenge" or session.get("reason") != "buyer_security_challenge":
        raise AssertionError(f"WB 498 must remain a truthful session challenge: {session}")
    if surface != {"state": "human", "reason": "buyer_security_challenge"}:
        raise AssertionError(f"central recovery must keep the challenge window alive: {surface}")


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
    destination = extracted.get("destination_context") if isinstance(extracted.get("destination_context"), Mapping) else {}
    if (
        extracted.get("authenticated_buyer_price") != 386.0
        or destination.get("dest") != "-1257786"
        or destination.get("currency") != "rub"
    ):
        raise AssertionError(f"authenticated response price extraction failed: {extracted}")


def _run_recovery_persistent_e2e() -> None:
    with TemporaryDirectory(prefix="wb-buyer-recovery-e2e-") as tmp:
        state_dir = Path(tmp)
        session = WbBuyerSessionConfig(
            state_dir=state_dir,
            storage_state_path=state_dir / "storage_state.json",
            settle_timeout_ms=1,
            validation_nm_id=NM_ID,
        )
        config = BuyerRecoveryConfig(session=session, timeout_sec=5, poll_sec=0.01, lock_wait_sec=1.0)
        state_dir.mkdir(parents=True, exist_ok=True)
        _write_status(
            config,
            {
                "run_id": "persistent-e2e",
                "status": "starting",
                "reason": "buyer_recovery_starting",
                "started_at": recovery_tool._now_text(),
            },
        )
        events: list[str] = []
        statuses: list[str] = []
        fake_playwright = _FakePlaywright(events)
        delegate = WbBuyerSessionAdapter(config=session)

        class FixtureAdapter:
            def __init__(self, *, config: WbBuyerSessionConfig) -> None:
                if config != session:
                    raise AssertionError("fixture received the wrong buyer profile")

            def session_lock(self, **kwargs: Any) -> Any:
                return delegate.session_lock(**kwargs)

            def migrate_legacy_storage_state(self, _context: Any) -> Mapping[str, Any]:
                events.append("migration_checked")
                return {"status": "legacy_state_absent"}

            def probe_persistent_context(self, context: Any, *, nm_id: int, page: Any) -> Mapping[str, Any]:
                events.append(f"proof:{context.process_number}:{nm_id}")
                if page.url != session.buyer_url:
                    page.goto(session.buyer_url, wait_until="domcontentloaded")
                return {
                    "session": {"status": "valid", "reason": "buyer_session_valid"},
                    "price": _ok_price(nm_id),
                }

            def validate_persistent_proof(self, operation: Mapping[str, Any], *, require_price: bool) -> Mapping[str, Any]:
                price = operation.get("price") if isinstance(operation.get("price"), Mapping) else {}
                valid = operation.get("session", {}).get("status") == "valid" and (not require_price or price.get("status") == "ok")
                return {"valid": valid, "session": operation.get("session", {}), "price": price, "reason": "buyer_persistent_profile_authenticated"}

        class FakeProcess:
            def __init__(self, name: str) -> None:
                self.name = name
                self.returncode: int | None = None

            def poll(self) -> int | None:
                return self.returncode

            def terminate(self) -> None:
                events.append(f"terminate:{self.name}")
                self.returncode = 0

            def wait(self, timeout: float | None = None) -> int:
                del timeout
                self.returncode = 0
                return 0

            def kill(self) -> None:
                events.append(f"kill:{self.name}")
                self.returncode = -9

        original_adapter = recovery_tool.WbBuyerSessionAdapter
        original_playwright = recovery_tool.sync_playwright
        original_spawn = recovery_tool._spawn
        original_wait_display = recovery_tool._wait_display
        original_wait_port = recovery_tool._wait_port
        original_ensure = recovery_tool._ensure_commands
        original_which = recovery_tool.shutil.which
        original_write = recovery_tool._write_status

        def record_status(fixture_config: BuyerRecoveryConfig, payload: Mapping[str, Any]) -> None:
            statuses.append(str(payload.get("status") or ""))
            original_write(fixture_config, payload)

        def fake_spawn(args: list[str], _log_path: Path, **_kwargs: Any) -> FakeProcess:
            name = Path(args[0]).name
            events.append(f"spawn:{name}")
            return FakeProcess(name)

        recovery_tool.WbBuyerSessionAdapter = FixtureAdapter  # type: ignore[assignment]
        recovery_tool.sync_playwright = lambda: fake_playwright
        recovery_tool._spawn = fake_spawn
        recovery_tool._wait_display = lambda _display: None
        recovery_tool._wait_port = lambda _port: None
        recovery_tool._ensure_commands = lambda _config: None
        recovery_tool.shutil.which = lambda _name: None
        recovery_tool._write_status = record_status
        try:
            result = recovery_tool.supervise_recovery(config)
        finally:
            recovery_tool.WbBuyerSessionAdapter = original_adapter  # type: ignore[assignment]
            recovery_tool.sync_playwright = original_playwright
            recovery_tool._spawn = original_spawn
            recovery_tool._wait_display = original_wait_display
            recovery_tool._wait_port = original_wait_port
            recovery_tool._ensure_commands = original_ensure
            recovery_tool.shutil.which = original_which
            recovery_tool._write_status = original_write

        terminal = recovery_tool._read_status(config.status_path)
        if result != 0 or terminal.get("status") != "completed":
            raise AssertionError(f"persistent recovery must complete: {terminal} {events}")
        if fake_playwright.chromium.user_data_dirs != [str(session.persistent_profile_dir)] * 2:
            raise AssertionError(f"both Chromium processes must use one persistent profile: {events}")
        if events.count("saved_account_click") != 1:
            raise AssertionError(f"exactly one saved-account control must be clicked: {events}")
        if "awaiting_human" not in statuses or "spawn:x11vnc" not in events or "spawn:websockify" not in events:
            raise AssertionError(f"SMS challenge must pause automation and start noVNC only then: {statuses} {events}")
        if events.index("close_context:1") > events.index("launch_context:2"):
            raise AssertionError(f"the first persistent context must close before restart validation: {events}")
        if "proof:1:210183919" not in events or "proof:2:210183919" not in events:
            raise AssertionError(f"both processes must prove /lk plus authenticated price read: {events}")
        for process_name in ("Xvfb", "x11vnc", "websockify"):
            if f"terminate:{process_name}" not in events:
                raise AssertionError(f"terminal cleanup did not terminate {process_name}: {events}")
        if session.lock_owner_path.exists():
            raise AssertionError("terminal cleanup must release the supervisor-owned automation lock")
        with delegate.session_lock(blocking=False, owner_operation="post_terminal_check"):
            pass


def _run_single_flight_start_smoke() -> None:
    with TemporaryDirectory(prefix="wb-buyer-single-flight-") as tmp:
        state_dir = Path(tmp)
        session = WbBuyerSessionConfig(state_dir=state_dir, storage_state_path=state_dir / "storage_state.json")
        config = BuyerRecoveryConfig(session=session, timeout_sec=5, poll_sec=0.01, lock_wait_sec=1.0)
        commands: list[list[str]] = []

        def fixture_command(_config: BuyerRecoveryConfig) -> list[str]:
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "_single_flight_fixture",
                str(state_dir),
                "wb_buyer_session_recovery.py",
                "supervise",
            ]
            commands.append(command)
            return command

        original_command = recovery_tool._supervisor_command
        recovery_tool._supervisor_command = fixture_command
        try:
            first = recovery_tool.start_recovery(config, replace=False)
            second = recovery_tool.start_recovery(config, replace=False)
        finally:
            recovery_tool._supervisor_command = original_command
        if first.get("run_id") != second.get("run_id") or len(commands) != 1:
            raise AssertionError(f"double start must join one exact run: {first} {second} {commands}")
        guarded = WbBuyerSessionAdapter(
            config=session,
            operation_probe=lambda *_args: (_ for _ in ()).throw(
                AssertionError("a competing request must not launch another persistent context")
            ),
        )
        joined_check = guarded.check_session()
        joined_price = guarded.fetch_authenticated_buyer_price(NM_ID)
        joined_status = recovery_tool.read_recovery_status(config, with_probe=True)
        if (
            joined_check.get("status") != "recovery_running"
            or joined_price.get("status") != "session_recovery_running"
            or joined_check.get("recovery_run_id") != first.get("run_id")
            or joined_price.get("recovery_run_id") != first.get("run_id")
            or joined_status.get("run_id") != first.get("run_id")
        ):
            raise AssertionError(f"competing UI/status/price calls must join one run: {joined_check} {joined_price} {joined_status}")
        deadline = time.monotonic() + 5
        terminal: Mapping[str, Any] = {}
        while time.monotonic() < deadline:
            terminal = recovery_tool.read_recovery_status(config, with_probe=False)
            if terminal.get("status") == "completed" and not terminal.get("running"):
                break
            time.sleep(0.02)
        if terminal.get("status") != "completed" or config.pid_path.exists() or session.lock_owner_path.exists():
            raise AssertionError(f"single-flight terminal cleanup failed: {terminal}")
        if not session.persistent_profile_dir.is_dir() or stat.S_IMODE(session.persistent_profile_dir.stat().st_mode) != 0o700:
            raise AssertionError("single-flight supervisor must create the protected persistent profile")


def _run_single_flight_fixture(state_dir: Path) -> int:
    session = WbBuyerSessionConfig(state_dir=state_dir, storage_state_path=state_dir / "storage_state.json")
    config = BuyerRecoveryConfig(session=session, timeout_sec=5, poll_sec=0.01, lock_wait_sec=1.0)
    run_id = str(recovery_tool._read_status(config.status_path).get("run_id") or "")
    _write_supervisor_identity(config, pid=os.getpid(), run_id=run_id)
    adapter = WbBuyerSessionAdapter(config=session)
    try:
        with adapter.session_lock(
            blocking=True,
            timeout_seconds=1,
            owner_run_id=run_id,
            owner_operation="single_flight_fixture",
        ):
            _write_status(config, {**recovery_tool._read_status(config.status_path), "status": "checking_session"})
            time.sleep(1.0)
        _write_status(
            config,
            {
                **recovery_tool._read_status(config.status_path),
                "status": "completed",
                "reason": "buyer_persistent_profile_validated",
                "finished_at": recovery_tool._now_text(),
            },
        )
        return 0
    finally:
        config.pid_path.unlink(missing_ok=True)


def _run_stop_and_launcher_smoke() -> None:
    with TemporaryDirectory(prefix="wb-buyer-stop-") as tmp:
        state_dir = Path(tmp)
        session = WbBuyerSessionConfig(state_dir=state_dir, storage_state_path=state_dir / "storage_state.json")
        config = BuyerRecoveryConfig(session=session, ssh_destination="wb-core-eu-root")
        state_dir.mkdir(parents=True, exist_ok=True)
        secure_log = state_dir / "secure.log"
        with _open_secure_log(secure_log) as handle:
            handle.write(b"safe-status-only\n")
        if stat.S_IMODE(secure_log.stat().st_mode) != 0o600:
            raise AssertionError("buyer recovery log files must be 0600")

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
            time.sleep(0.02)
        if not child_pid_path.exists():
            parent.kill()
            raise AssertionError("canonical stop fixture did not start")
        nested_pid = int(child_pid_path.read_text(encoding="utf-8"))
        _write_supervisor_identity(config, pid=parent.pid, run_id="stop-run")
        session.lock_owner_path.write_text(json.dumps({"run_id": "stop-run", "pid": parent.pid}), encoding="utf-8")
        os.chmod(session.lock_owner_path, 0o600)
        _write_status(config, {"run_id": "stop-run", "status": "awaiting_human", "reason": "buyer_sms_required"})
        stopped = stop_recovery(config, requested_run_id="stop-run")
        parent.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and Path(f"/proc/{nested_pid}").exists():
            time.sleep(0.02)
        if (
            stopped.get("status") != "stopped"
            or Path(f"/proc/{nested_pid}").exists()
            or config.pid_path.exists()
            or session.lock_owner_path.exists()
        ):
            raise AssertionError(f"canonical stop must terminate the entire process group: {stopped}")

        supervisor = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)", "wb_buyer_session_recovery.py", "supervise"],
            start_new_session=True,
        )
        _write_supervisor_identity(config, pid=supervisor.pid, run_id="launcher-run")
        _write_status(config, {"run_id": "launcher-run", "status": "awaiting_human", "reason": "buyer_sms_required"})
        try:
            archive, filename = build_macos_launcher_archive(
                config,
                public_status_url="https://api.selleros.pro/v1/sheet-vitrina-v1/prices/spp-test/buyer-session/recovery/status",
                public_operator_url="https://api.selleros.pro/sheet-vitrina-v1/vitrina",
            )
        finally:
            _terminate_process_group(supervisor.pid)
        if not filename.endswith(".zip"):
            raise AssertionError("buyer noVNC launcher must be a zip")
        with zipfile.ZipFile(BytesIO(archive)) as zip_file:
            body = zip_file.read(zip_file.namelist()[0]).decode("utf-8")
        lowered = body.lower()
        if "127.0.0.1:46090" not in body or "ssh -o exitonforwardfailure" not in lowered:
            raise AssertionError("buyer launcher must use a localhost-only SSH tunnel")
        if "storage_state" in lowered or "authorization" in lowered or "private-cookie" in lowered:
            raise AssertionError("buyer launcher must not contain session material")


def _destination() -> dict[str, str]:
    return {"dest": "-1257786", "currency": "rub"}


def _ok_price(nm_id: int) -> dict[str, Any]:
    return {
        "status": "ok",
        "nm_id": nm_id,
        "authenticated_buyer_price": 386.0,
        "normal_price": 386.0,
        "wallet_price": 378.0,
        "card_price": 381.0,
        "club_price": 375.0,
        "payment_context": "account_default_with_wallet_and_card_and_club_option",
        "destination_context": _destination(),
        "source_method": "authenticated_browser_network_json",
        "source_endpoint": "https://card.wb.ru/cards/v4/detail",
        "http_status": 200,
    }


class _MigrationPage:
    def __init__(self, context: "_MigrationContext") -> None:
        self.context = context

    def goto(self, _url: str, **_kwargs: Any) -> None:
        return None

    def evaluate(self, _script: str, items: list[Mapping[str, str]]) -> None:
        self.context.local_storage_sets += len(items)

    def close(self) -> None:
        return None


class _MigrationContext:
    def __init__(self) -> None:
        self.cookies_added = 0
        self.local_storage_sets = 0

    def add_cookies(self, cookies: list[Mapping[str, Any]]) -> None:
        self.cookies_added += len(cookies)

    def new_page(self) -> _MigrationPage:
        return _MigrationPage(self)


class _SavedAccountButton:
    def __init__(self, page: "_FakePage", events: list[str]) -> None:
        self.page = page
        self.events = events

    def click(self, **_kwargs: Any) -> None:
        self.events.append("saved_account_click")
        self.page.surface = {"state": "human", "reason": "buyer_sms_required"}


class _FakePage:
    def __init__(self, process_number: int, events: list[str]) -> None:
        self.process_number = process_number
        self.events = events
        self.url = "https://www.wildberries.ru/lk"
        self.human_waits = 0
        self.surface: dict[str, Any]
        if process_number == 1:
            self.surface = {"state": "automatic_login", "reason": "buyer_saved_account_available"}
            self.surface["candidate"] = _SavedAccountButton(self, events)
        else:
            self.surface = {"state": "authenticated", "reason": "buyer_visible_account_opened"}

    @property
    def wb_recovery_surface(self) -> Mapping[str, Any]:
        return self.surface

    def goto(self, url: str, **_kwargs: Any) -> None:
        self.url = url

    def wait_for_load_state(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def wait_for_timeout(self, _milliseconds: int) -> None:
        if self.surface.get("state") == "human" and "spawn:x11vnc" in self.events:
            self.human_waits += 1
            if self.human_waits >= 1:
                self.surface = {"state": "authenticated", "reason": "buyer_visible_account_opened"}


class _FakeContext:
    def __init__(self, process_number: int, events: list[str]) -> None:
        self.process_number = process_number
        self.events = events
        self.pages = [_FakePage(process_number, events)]
        self.closed = False

    def new_page(self) -> _FakePage:
        page = _FakePage(self.process_number, self.events)
        self.pages.append(page)
        return page

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.events.append(f"close_context:{self.process_number}")


class _FakeChromium:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.user_data_dirs: list[str] = []

    def launch_persistent_context(self, *, user_data_dir: str, **_kwargs: Any) -> _FakeContext:
        self.user_data_dirs.append(user_data_dir)
        process_number = len(self.user_data_dirs)
        self.events.append(f"launch_context:{process_number}")
        return _FakeContext(process_number, self.events)


class _FakePlaywright:
    def __init__(self, events: list[str]) -> None:
        self.chromium = _FakeChromium(events)

    def __enter__(self) -> "_FakePlaywright":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "_single_flight_fixture":
        raise SystemExit(_run_single_flight_fixture(Path(sys.argv[2])))
    main()
