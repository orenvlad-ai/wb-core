"""Targeted smoke-check for seller portal relogin auto-capture flow."""

from __future__ import annotations

from importlib import util
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "apps" / "seller_portal_relogin_session.py"
SPEC = util.spec_from_file_location("seller_portal_relogin_session", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"unable to load module spec from {MODULE_PATH}")
MODULE = util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class _FakePage:
    def goto(self, *_args, **_kwargs) -> None:
        return


class _FakeContext:
    def __init__(self) -> None:
        self.storage_state_calls = 0

    def new_page(self) -> _FakePage:
        return _FakePage()

    def storage_state(self, *, path: str) -> None:
        self.storage_state_calls += 1
        Path(path).write_text('{"cookies": [], "origins": []}', encoding="utf-8")


class _FakeBrowser:
    def __init__(self) -> None:
        self.context = _FakeContext()
        self.closed = False

    def new_context(self, **_kwargs) -> _FakeContext:
        return self.context

    def close(self) -> None:
        self.closed = True


class _FakePlaywright:
    def __init__(self) -> None:
        self.browser = _FakeBrowser()

    def __enter__(self) -> "_FakePlaywright":
        return self

    def __exit__(self, *_args) -> None:
        return

    @property
    def chromium(self) -> "_FakePlaywright":
        return self

    def launch(self, **_kwargs) -> _FakeBrowser:
        return self.browser


def main() -> None:
    with tempfile.TemporaryDirectory() as temp_dir_raw:
        temp_dir = Path(temp_dir_raw)
        config = MODULE.ReloginSessionConfig(
            state_dir=temp_dir,
            storage_state_path=temp_dir / "storage_state.json",
            wb_bot_python=Path(sys.executable),
            timeout_sec=30,
            poll_sec=0.0,
            ssh_destination="selleros-root",
        )
        config.state_dir.mkdir(parents=True, exist_ok=True)
        fake_playwright = _FakePlaywright()
        probe_calls = {"count": 0}

        def fake_probe(_path: Path) -> dict[str, object]:
            probe_calls["count"] += 1
            if probe_calls["count"] == 1:
                return {"ok": False, "status": "seller_portal_session_invalid"}
            return {
                "ok": True,
                "status": "ok",
                "final_url": "https://seller.wildberries.ru/search-analytics/my-search-queries",
            }

        result = MODULE.run_login_capture(
            config,
            probe_fn=fake_probe,
            playwright_factory=lambda: fake_playwright,
            sleep_fn=lambda _seconds: None,
        )

        if result.get("status") != "auth_confirmed":
            raise AssertionError(f"expected auth_confirmed, got {result}")
        if not config.storage_state_path.exists():
            raise AssertionError("storage_state.json must be written after auth is confirmed")
        if probe_calls["count"] < 2:
            raise AssertionError(f"probe must be retried until auth succeeds, got {probe_calls}")
        if not fake_playwright.browser.closed:
            raise AssertionError("browser must be closed after auto-capture")
        status_payload = MODULE.read_session_status(config, with_probe=False)
        if status_payload.get("status") != "awaiting_login":
            raise AssertionError(f"intermediate awaiting_login status must be persisted, got {status_payload}")

        print("seller_portal_relogin_session_capture: ok -> auth_confirmed after browser login")
        print("smoke-check passed")


if __name__ == "__main__":
    main()
