"""Focused offline checks for the isolated durable Chrome price source."""

from __future__ import annotations

from contextlib import nullcontext
import json
from pathlib import Path
import os
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import wb_buyer_chrome_price as price  # noqa: E402
from apps import wb_buyer_chrome_runtime as runtime  # noqa: E402
from packages.adapters.wb_buyer_chrome_price import WbBuyerChromePriceAdapter  # noqa: E402
from packages.application.wb_buyer_session import WbBuyerSessionBlock  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402


def _labelled_popup_only() -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_content("""<style>.reference { text-decoration: line-through; }</style>
            <button class='current-price' style='margin-left:65%' onclick='document.querySelector("#detail").hidden=false'>139 ₽</button>
            <div id='detail' hidden><h3>Детализация цены</h3><div>Цены с учётом скидки</div>
            <div class='reference'>1500 ₽</div><div>139 ₽</div><div>с WB Кошельком</div>
            <del>1499 ₽</del><div>144 ₽</div><div>без WB Кошелька</div>
            <div>138 ₽ для бизнеса</div></div>""")
        assert page.evaluate(price.PRICE_DETAIL) == {"wallet": None, "nonwallet": None}
        assert page.evaluate(price.OPEN_DETAIL) is True
        observed = page.evaluate(price.PRICE_DETAIL)
        assert observed == {"wallet": 139, "nonwallet": 144}, observed
        browser.close()


def _missing_price_is_not_zero_or_spp() -> None:
    assert price._labelled_prices({"wallet": 0, "nonwallet": "144"}) == (None, None)
    assert price._labelled_prices({"wallet": 139, "nonwallet": 144}) == (139, 144)
    empty = price._unknown(497416931)
    assert empty["wallet_price"] is None and empty["normal_price"] is None
    assert empty["authenticated_buyer_price"] is None
    with patch.object(price, "read_price", return_value={
        **empty, "status": "observed", "reason": "buyer_price_context_not_verified",
        "wallet_price": 139, "normal_price": 144, "session_status": "authenticated_surface",
        "authenticated_session_proof": True, "stable": True,
    }), patch.object(WbBuyerChromePriceAdapter, "check_session", return_value={"status": "authenticated_surface", "account_confirmed": False}):
        source = WbBuyerSessionBlock(adapter=WbBuyerChromePriceAdapter(), sleep=lambda _seconds: None)
        capability = source.check_spp_capability()
        assert capability["price"]["wallet_price"] == 139
        assert capability["price"]["nonwallet_price"] == 144
        assert capability["price"]["authenticated_buyer_price"] is None
        assert capability["status"] == "authenticated_surface"
        assert not capability["account_confirmed"] and not capability["capability_valid"]


def _settings_reader_does_not_replace_spp_source() -> None:
    with TemporaryDirectory() as directory:
        app = RegistryUploadHttpEntrypoint(runtime_dir=Path(directory))
        assert isinstance(app.buyer_price_block.adapter, WbBuyerChromePriceAdapter)
        assert app.spp_tester_block.buyer_source is app.buyer_session_block
        assert app.buyer_price_block is not app.spp_tester_block.buyer_source
        assert type(app.buyer_session_block.adapter).__name__ == "WbBuyerSessionAdapter"


def _reader_blocks_orphan_and_serializes_profile() -> None:
    with TemporaryDirectory() as directory:
        state = Path(directory)
        with patch.object(runtime, "STATE", state), patch.object(runtime, "_ensure_user", return_value=SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())):
            with runtime.profile_operation_lock():
                try:
                    with runtime.profile_operation_lock():
                        raise AssertionError("second profile operation acquired lock")
                except BlockingIOError:
                    pass
        with (
            patch.object(price.os, "geteuid", return_value=0),
            patch.object(runtime, "STATE", state),
            patch.object(runtime, "_ensure_user", return_value=SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())),
            patch.object(price.auth, "_shared_start_lock", side_effect=lambda: nullcontext()),
            patch.object(price.auth, "raw_status", return_value={"running": False}),
            patch.object(price.legacy, "load_recovery_config_from_env", return_value=object()),
            patch.object(price.legacy, "read_recovery_status", return_value={"running": False}),
            patch.object(runtime, "ensure_runner_idle", side_effect=RuntimeError("dedicated Chrome runner still has processes")),
            patch.object(runtime, "ensure_runtime", side_effect=AssertionError("orphan reached Chrome launch")),
        ):
            blocked = price.read_price(497416931)
            assert blocked["status"] == "price_unavailable" and blocked["wallet_price"] is None


def _cleanup_continues_after_chrome_failure() -> None:
    # A Chrome stop error cannot skip the X server or window manager stop, and
    # no observed price may be returned after an incomplete profile shutdown.
    marker: list[str] = []
    with (
        patch.object(price.os, "geteuid", return_value=123),
        patch.object(price.runtime, "profile_operation_lock", side_effect=lambda: nullcontext()),
        patch.object(price.runtime, "STATE", Path("/tmp/fixture")),
        patch.object(price.auth, "_spawn", side_effect=lambda *_args, **_kwargs: SimpleNamespace(poll=lambda: None)),
        patch.object(price.auth, "_wait_display"),
        patch.object(price.auth, "_launch_chrome", side_effect=RuntimeError("fixture launch failure")),
        patch.object(price.auth, "_stop_chrome", side_effect=lambda _process: marker.append("chrome") or (_ for _ in ()).throw(RuntimeError("fixture cleanup failure"))),
        patch.object(price.auth, "_stop_process", side_effect=lambda _process: marker.append("other")),
        patch.object(price.subprocess, "run", return_value=SimpleNamespace(returncode=0)),
        patch.object(Path, "touch"), patch.object(os, "chmod"),
    ):
        try:
            price._read_in_runner(497416931)
        except RuntimeError as error:
            assert "cleanup failed" in str(error)
        else:
            raise AssertionError("cleanup failure was reported as price evidence")
    assert marker == ["chrome", "other", "other"]


def _batch_rechecks_auth_and_streams_partial() -> None:
    partial = (json.dumps({"nm_id": 1, "status": "observed"}) + "\n" +
               json.dumps({"nm_id": 999, "status": "observed"}) + "\n" + "truncated")
    assert price._parse_batch_lines(partial.encode(), [1, 2]) == {1: {"nm_id": 1, "status": "observed"}}
    class Pipe:
        def __init__(self):
            self.navigations = []
        def call(self, method, params=None, *, session=None):
            if method == "Target.attachToTarget":
                return {"sessionId": "fixture-session"}
            if method == "Page.navigate":
                self.navigations.append(params["url"])
            return {}
        def visible_surface(self):
            return "account"
    pipe = Pipe()
    def evaluate(_pipe, _session, expression):
        return "account" if expression == price.auth.SURFACE_EXPRESSION else {"wallet": 139, "nonwallet": 144}
    with (patch.object(price, "_product_target", return_value="fixture-card"),
          patch.object(price, "_evaluate", side_effect=evaluate),
          patch.object(price.time, "sleep")):
        row = price._read_product_in_batch(pipe, 497416931, "fixture-account",
                                           batch_deadline=price.time.monotonic() + 5)
    assert row["status"] == "observed" and row["session_checked_at"]
    assert pipe.navigations == ["https://www.wildberries.ru/lk",
                                "https://www.wildberries.ru/catalog/497416931/detail.aspx"]

    emitted = []
    fake = SimpleNamespace(
        visible_surface=lambda: "account",
        call=lambda method, *_args, **_kwargs: {"targetInfos": [{"type": "page", "targetId": "account",
            "url": "https://www.wildberries.ru/lk"}]} if method == "Target.getTargets" else {},
        close=lambda: None,
    )
    first = {**price._unknown(1), "status": "observed", "session_status": "authenticated_surface",
             "authenticated_session_proof": True, "wallet_price": 139, "normal_price": 144}
    with (patch.object(price.os, "geteuid", return_value=123),
          patch.object(price.runtime, "STATE", Path("/tmp/fixture")),
          patch.object(price.runtime, "PROFILE", ROOT),
          patch.object(price.auth, "_spawn", return_value=SimpleNamespace()),
          patch.object(price.auth, "_wait_display"),
          patch.object(price.auth, "_launch_chrome", return_value=(SimpleNamespace(), fake)),
          patch.object(price.auth, "_stop_chrome"), patch.object(price.auth, "_stop_process"),
          patch.object(price.subprocess, "run", return_value=SimpleNamespace(returncode=0)),
          patch.object(Path, "touch"), patch.object(os, "chmod"),
          patch.object(price, "_read_product_in_batch", side_effect=[first, RuntimeError("fixture"),
                                                              RuntimeError("fixture"), RuntimeError("fixture")])):
        rows = price._read_batch_in_runner([1, 2, 3, 4, 5], on_result=emitted.append)
    assert len(rows) == len(emitted) == 5
    assert rows[0]["status"] == "observed"
    assert rows[-1]["reason"] == "authenticated_price_batch_probe_failures"


def _printed_rows_survive_failed_cleanup_without_healthy_run() -> None:
    measured = "2026-09-28T18:29:02+00:00"
    output = "".join(json.dumps({
        "nm_id": nm_id, "measured_at": measured, "status": "observed",
        "session_status": "authenticated_surface", "authenticated_session_proof": True,
        "wallet_price": 139, "normal_price": 144,
    }) + "\n" for nm_id in (1, 2))

    class Child:
        returncode = 1  # Rows were streamed, then browser cleanup failed.

        def communicate(self, *, timeout):
            return output, ""

    with (
        patch.object(price.os, "geteuid", return_value=0),
        patch.object(price.runtime, "profile_operation_lock", side_effect=lambda: nullcontext(7)),
        patch.object(price.auth, "_shared_start_lock", side_effect=lambda: nullcontext()),
        patch.object(price.auth, "raw_status", return_value={"running": False, "status": "completed", "login_confirmed": True, "run_id": "auth-one"}),
        patch.object(price.legacy, "load_recovery_config_from_env", return_value=object()),
        patch.object(price.legacy, "read_recovery_status", return_value={"running": False}),
        patch.object(price.runtime, "ensure_runner_idle"),
        patch.object(price.runtime, "_available", return_value=100_000_000_000),
        patch.object(price.runtime, "_root_reserve", return_value=25_000_000_000),
        patch.object(price.runtime, "ensure_runtime"),
        patch.object(price.runtime, "PROFILE", ROOT),
        patch.object(price.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())),
        patch.object(price.subprocess, "Popen", return_value=Child()),
    ):
        rows = price.read_prices([1, 2])
    assert len(rows) == 2 and all(row["status"] == "observed" for row in rows)
    assert all(row["reader_lifecycle_status"] == "cleanup_failed" for row in rows)
    assert all(row["auth_run_reference"] == "auth-one" for row in rows)


def _early_reader_failure_carries_current_login_generation() -> None:
    with (
        patch.object(price.os, "geteuid", return_value=0),
        patch.object(price.runtime, "profile_operation_lock", side_effect=BlockingIOError),
        patch.object(price.auth, "raw_status", return_value={"status": "completed", "run_id": "new-login"}),
        patch.object(price.runtime, "PROFILE", ROOT),
    ):
        rows = price.read_prices([1, 2])
    assert all(row["status"] == "price_unavailable" for row in rows)
    assert all(row["auth_run_reference"] == "new-login" and row["profile_reference"] for row in rows)


def main() -> None:
    _labelled_popup_only()
    _missing_price_is_not_zero_or_spp()
    _settings_reader_does_not_replace_spp_source()
    _reader_blocks_orphan_and_serializes_profile()
    _cleanup_continues_after_chrome_failure()
    _batch_rechecks_auth_and_streams_partial()
    _printed_rows_survive_failed_cleanup_without_healthy_run()
    _early_reader_failure_carries_current_login_generation()
    print("wb_buyer_chrome_price_smoke: OK")


if __name__ == "__main__":
    main()
