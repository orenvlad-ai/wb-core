"""Targeted smoke-check for seller portal relogin auto-capture flow."""

from __future__ import annotations

import base64
import io
from importlib import util
import json
from pathlib import Path
import sys
import tempfile
import zipfile


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

    def bring_to_front(self) -> None:
        return

    def evaluate(self, *_args, **_kwargs) -> None:
        return


class _FakeContext:
    def __init__(self) -> None:
        self.storage_state_calls = 0
        self.cookies: list[dict[str, object]] = []
        self.pages = [_FakePage()]
        self.closed = False

    def new_page(self) -> _FakePage:
        page = _FakePage()
        self.pages.append(page)
        return page

    def add_cookies(self, cookies: list[dict[str, object]]) -> None:
        self.cookies.extend(cookies)

    def storage_state(self, *, path: str) -> None:
        self.storage_state_calls += 1
        wrong_supplier_id = "wrong-supplier-id"
        payload = {
            "cookies": [
                {
                    "name": "x-supplier-id",
                    "value": wrong_supplier_id,
                    "domain": "seller.wildberries.ru",
                    "path": "/",
                    "secure": True,
                    "httpOnly": False,
                    "sameSite": "Lax",
                },
                {
                    "name": "x-supplier-id-external",
                    "value": wrong_supplier_id,
                    "domain": ".wildberries.ru",
                    "path": "/",
                    "secure": True,
                    "httpOnly": False,
                    "sameSite": "Lax",
                },
            ],
            "origins": [
                {
                    "origin": "https://seller.wildberries.ru",
                    "localStorage": [
                        {
                            "name": "analytics-external-data",
                            "value": base64.b64encode(
                                json.dumps(
                                    {"idSupplier": wrong_supplier_id, "idUser": 51178567},
                                    ensure_ascii=False,
                                ).encode("utf-8")
                            ).decode("utf-8"),
                        }
                    ],
                }
            ],
        }
        Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self) -> None:
        self.context = _FakeContext()
        self.closed = False

    def launch_persistent_context(self, *_args, **_kwargs) -> _FakeContext:
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

    def launch_persistent_context(self, *_args, **_kwargs) -> _FakeContext:
        return self.browser.launch_persistent_context(*_args, **_kwargs)


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
            canonical_supplier_id="canonical-supplier-id",
            canonical_supplier_label="ИП Сагитов В. Р.",
        )
        config.state_dir.mkdir(parents=True, exist_ok=True)
        config.storage_state_path.write_text(
            json.dumps(
                {
                    "cookies": [
                        {
                            "name": "existing-session-cookie",
                            "value": "seed",
                            "domain": "seller.wildberries.ru",
                            "path": "/",
                        }
                    ],
                    "origins": [],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        fake_playwright = _FakePlaywright()
        probe_calls = {"count": 0}

        def fake_probe(_path: Path) -> dict[str, object]:
            probe_calls["count"] += 1
            if probe_calls["count"] == 1:
                return {"ok": False, "status": "seller_portal_session_invalid"}
            supplier_context = MODULE.read_storage_state_supplier_context(_path)
            return {
                "ok": True,
                "status": "ok",
                "final_url": "https://seller.wildberries.ru/search-analytics/my-search-queries",
                "supplier_context": supplier_context,
            }

        result = MODULE.run_login_capture(
            config,
            probe_fn=fake_probe,
            playwright_factory=lambda: fake_playwright,
            sleep_fn=lambda _seconds: None,
            visual_ready_fn=lambda _display: True,
        )

        if result.get("status") != "auth_confirmed":
            raise AssertionError(f"expected auth_confirmed, got {result}")
        if not config.storage_state_path.exists():
            raise AssertionError("storage_state.json must be written after auth is confirmed")
        if probe_calls["count"] < 2:
            raise AssertionError(f"probe must be retried until auth succeeds, got {probe_calls}")
        if not fake_playwright.browser.context.cookies:
            raise AssertionError("persistent context must receive cookies from existing storage state when available")
        if not fake_playwright.browser.context.closed:
            raise AssertionError("persistent context must be closed after auto-capture")
        if result.get("organization_confirmed") is not True:
            raise AssertionError(f"canonical supplier must be confirmed, got {result}")
        if result.get("organization_switch_applied") is not True:
            raise AssertionError(f"run_login_capture must auto-switch to canonical supplier, got {result}")
        final_supplier_context = MODULE.read_storage_state_supplier_context(config.storage_state_path)
        if final_supplier_context.get("current_supplier_id") != "canonical-supplier-id":
            raise AssertionError(f"storage_state.json must be rewritten to canonical supplier, got {final_supplier_context}")
        status_payload = MODULE.read_session_status(config, with_probe=False)
        if status_payload.get("status") != "awaiting_login":
            raise AssertionError(f"intermediate awaiting_login status must be persisted, got {status_payload}")
        archive_bytes, archive_name = MODULE.build_macos_launcher_archive(
            config,
            public_status_url="https://api.selleros.pro/v1/sheet-vitrina-v1/seller-portal-recovery/status",
            public_operator_url="https://api.selleros.pro/sheet-vitrina-v1/operator",
        )
        if archive_name != "seller-portal-relogin-macos.zip":
            raise AssertionError(f"unexpected launcher archive name: {archive_name}")
        with zipfile.ZipFile(io.BytesIO(archive_bytes), "r") as archive:
            names = archive.namelist()
            if names != ["seller-portal-relogin.command"]:
                raise AssertionError(f"unexpected launcher archive entries: {names}")
            launcher_text = archive.read("seller-portal-relogin.command").decode("utf-8")
        required_fragments = [
            "selleros-root",
            "${STATUS}",
            "python3 -c",
            'json.loads(raw).get("status", "")',
            "https://api.selleros.pro/v1/sheet-vitrina-v1/seller-portal-recovery/status",
            "https://api.selleros.pro/sheet-vitrina-v1/operator",
            "/vnc.html?autoconnect=1&resize=remote&path=websockify&reconnect=1",
        ]
        missing_fragments = [item for item in required_fragments if item not in launcher_text]
        if missing_fragments:
            raise AssertionError(f"launcher script is missing required fragments: {missing_fragments}")
        if "sed -n 's/.*\"status\"" in launcher_text or 'sed -n \'s/.*"status"' in launcher_text:
            raise AssertionError("launcher script must not parse nested JSON status fields via greedy sed")

        print("seller_portal_relogin_session_capture: ok -> auth_confirmed after browser login")
        print("seller_portal_relogin_session_supplier_switch: ok -> canonical supplier enforced before final save")
        print("seller_portal_relogin_session_launcher: ok -> archive contains reusable Mac launcher script")
        print("smoke-check passed")


if __name__ == "__main__":
    main()
