"""Temporary localhost-only noVNC recovery for the isolated WB buyer session."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from typing import Any, Mapping
from urllib import parse as urllib_parse
from uuid import uuid4
import zipfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from packages.adapters.wb_buyer_session import (  # noqa: E402
    WbBuyerSessionAdapter,
    WbBuyerSessionConfig,
    load_wb_buyer_session_config_from_env,
)


UTC = timezone.utc
DEFAULT_DISPLAY = ":98"
DEFAULT_VNC_PORT = 45911
DEFAULT_WEB_PORT = 46090
DEFAULT_TIMEOUT_SEC = 1800
DEFAULT_POLL_SEC = 3.0
DEFAULT_SSH_DESTINATION = "wb-core-eu-root"
DEFAULT_NOVNC_WEB_DIR = Path("/usr/share/novnc")
ACTIVE_STATUSES = {
    "starting",
    "checking_session",
    "automatic_login",
    "stabilizing_session",
    "awaiting_human",
    "saving_session",
    "validating_session",
}
FINAL_STATUSES = {"completed", "migration_required", "stopped", "timeout", "error"}


@dataclass(frozen=True)
class BuyerRecoveryConfig:
    session: WbBuyerSessionConfig
    display: str = DEFAULT_DISPLAY
    vnc_port: int = DEFAULT_VNC_PORT
    web_port: int = DEFAULT_WEB_PORT
    timeout_sec: int = DEFAULT_TIMEOUT_SEC
    poll_sec: float = DEFAULT_POLL_SEC
    ssh_destination: str = DEFAULT_SSH_DESTINATION
    novnc_web_dir: Path = DEFAULT_NOVNC_WEB_DIR

    @property
    def status_path(self) -> Path:
        return self.session.state_dir / "recovery_status.json"

    @property
    def pid_path(self) -> Path:
        return self.session.state_dir / "recovery_supervisor.pid"

    @property
    def start_lock_path(self) -> Path:
        return self.session.state_dir / "recovery_start.lock"

    @property
    def candidate_path(self) -> Path:
        return self.session.state_dir / "candidate_storage_state.json"

    @property
    def supervisor_log_path(self) -> Path:
        return self.session.state_dir / "recovery_supervisor.log"

    @property
    def xvfb_log_path(self) -> Path:
        return self.session.state_dir / "recovery_xvfb.log"

    @property
    def openbox_log_path(self) -> Path:
        return self.session.state_dir / "recovery_openbox.log"

    @property
    def x11vnc_log_path(self) -> Path:
        return self.session.state_dir / "recovery_x11vnc.log"

    @property
    def websockify_log_path(self) -> Path:
        return self.session.state_dir / "recovery_websockify.log"

    @property
    def novnc_url(self) -> str:
        query = urllib_parse.urlencode({"autoconnect": "1", "resize": "remote", "path": "websockify", "reconnect": "1"})
        return f"http://127.0.0.1:{self.web_port}/vnc.html?{query}"


def load_recovery_config_from_env() -> BuyerRecoveryConfig:
    return BuyerRecoveryConfig(
        session=load_wb_buyer_session_config_from_env(),
        display=str(os.environ.get("WB_BUYER_RECOVERY_DISPLAY") or DEFAULT_DISPLAY),
        vnc_port=_env_int("WB_BUYER_RECOVERY_VNC_PORT", DEFAULT_VNC_PORT),
        web_port=_env_int("WB_BUYER_RECOVERY_WEB_PORT", DEFAULT_WEB_PORT),
        timeout_sec=_env_int("WB_BUYER_RECOVERY_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC),
        poll_sec=_env_float("WB_BUYER_RECOVERY_POLL_SEC", DEFAULT_POLL_SEC),
        ssh_destination=str(os.environ.get("WB_BUYER_RECOVERY_SSH_DESTINATION") or DEFAULT_SSH_DESTINATION),
        novnc_web_dir=Path(str(os.environ.get("WB_BUYER_RECOVERY_NOVNC_WEB_DIR") or DEFAULT_NOVNC_WEB_DIR)),
    )


def main() -> None:
    config = load_recovery_config_from_env()
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "status", "stop", "supervise"):
        sub = subparsers.add_parser(name)
        _add_args(sub, config)
        if name == "start":
            sub.add_argument("--replace", action="store_true")
        if name == "status":
            sub.add_argument("--probe", action="store_true")
            sub.add_argument("--run-id", default="")
    args = parser.parse_args()
    config = _config_from_args(args)
    if args.command == "start":
        payload = start_recovery(config, replace=args.replace)
    elif args.command == "status":
        payload = read_recovery_status(config, with_probe=args.probe, requested_run_id=args.run_id or None)
    elif args.command == "stop":
        payload = stop_recovery(config)
    else:
        raise SystemExit(supervise_recovery(config))
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def start_recovery(config: BuyerRecoveryConfig, *, replace: bool = False) -> dict[str, Any]:
    _ensure_state_dir(config)
    with _recovery_start_lock(config):
        current = read_recovery_status(config, with_probe=False)
        if current.get("running"):
            if not replace:
                return current
            stop_recovery(config, requested_run_id=str(current.get("run_id") or ""), acquire_start_lock=False)
        run_id = f"buyer-recovery-{_now().strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
        adapter = WbBuyerSessionAdapter(config=config.session)
        session: Mapping[str, Any]
        # The UI preflight and recovery start can overlap while the first
        # independent probe is still holding the shared session lease.  Wait
        # for one bounded probe lease (45s), then fail explicitly; never
        # unlink or steal the lock.
        lock_contention = False
        for attempt in range(45):
            try:
                session = adapter.check_session()
                break
            except (RuntimeError, BlockingIOError) as exc:
                if isinstance(exc, RuntimeError) and "lock" not in str(exc).lower():
                    raise
                if attempt == 44:
                    lock_contention = True
                    session = {
                        "status": "recovery_running",
                        "valid": False,
                        "reason": "buyer_session_lock_busy",
                    }
                    break
                time.sleep(0.5)
        if session.get("status") == "valid":
            payload = {
                "run_id": run_id,
                "status": "completed",
                "reason": "buyer_session_already_valid",
                "started_at": _now_text(),
                "finished_at": _now_text(),
                "session": session,
            }
            _write_status(config, payload)
            return read_recovery_status(config, with_probe=False)
        adapter.prepare_fingerprint_migration()
        _write_status(
            config,
            {
                "run_id": run_id,
                "status": "starting",
                "reason": "buyer_recovery_starting",
                "started_at": _now_text(),
                "deadline_at": (_now() + timedelta(seconds=config.timeout_sec)).isoformat(),
                "session": session,
            },
        )
        with _open_secure_log(config.supervisor_log_path) as log_file:
            process = subprocess.Popen(
                _supervisor_command(config),
                cwd=str(ROOT),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        _write_supervisor_identity(config, pid=process.pid, run_id=run_id)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            status = read_recovery_status(config, with_probe=False)
            if status.get("status") in ACTIVE_STATUSES | FINAL_STATUSES:
                return status
            time.sleep(0.25)
        return read_recovery_status(config, with_probe=False)


def stop_recovery(
    config: BuyerRecoveryConfig,
    *,
    requested_run_id: str | None = None,
    acquire_start_lock: bool = True,
) -> dict[str, Any]:
    manager = _recovery_start_lock(config) if acquire_start_lock else _null_context()
    with manager:
        payload = _read_status(config.status_path)
        current_run_id = str(payload.get("run_id") or "")
        requested = str(requested_run_id or "").strip()
        if requested and requested != current_run_id:
            return {
                **payload,
                "status": "error",
                "reason": "buyer_recovery_run_not_current",
                "running": _verified_supervisor_running(config, current_run_id),
            }
        identity = _read_supervisor_identity(config)
        pid = _identity_pid(identity)
        if pid and _pid_running(pid) and not _supervisor_identity_matches(config, identity, current_run_id):
            payload.update({"status": "error", "reason": "buyer_recovery_process_identity_mismatch", "finished_at": _now_text()})
            _write_status(config, payload)
            return read_recovery_status(config, with_probe=False)
        if pid and _supervisor_identity_matches(config, identity, current_run_id):
            _terminate_process_group(pid)
        config.pid_path.unlink(missing_ok=True)
        config.candidate_path.unlink(missing_ok=True)
        if str(payload.get("status") or "") not in FINAL_STATUSES:
            payload.update({"status": "stopped", "reason": "buyer_recovery_stopped", "finished_at": _now_text()})
            _write_status(config, payload)
    return read_recovery_status(config, with_probe=True)


def read_recovery_status(
    config: BuyerRecoveryConfig,
    with_probe: bool,
    *,
    requested_run_id: str | None = None,
) -> dict[str, Any]:
    payload = _read_status(config.status_path)
    run_id = str(payload.get("run_id") or "")
    payload["running"] = _verified_supervisor_running(config, run_id)
    payload.setdefault("status", "idle")
    payload.setdefault("run_id", "")
    launch_grace = payload["status"] == "starting" and _status_age_seconds(payload) < 5.0
    if not payload["running"] and payload["status"] in ACTIVE_STATUSES and not launch_grace:
        payload.update({"status": "error", "reason": "buyer_recovery_unexpected_exit", "finished_at": _now_text()})
        _write_status(config, payload)
    requested = str(requested_run_id or "").strip()
    if requested and requested != str(payload.get("run_id") or ""):
        return {
            "run_id": str(payload.get("run_id") or ""),
            "status": "error",
            "reason": "buyer_recovery_run_not_current",
            "running": payload["running"],
            "session": {},
        }
    if with_probe and not payload["running"]:
        payload["session"] = WbBuyerSessionAdapter(config=config.session).check_session()
    return payload


def _status_age_seconds(payload: Mapping[str, Any]) -> float:
    try:
        started_at = datetime.fromisoformat(str(payload.get("started_at") or ""))
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        return max(0.0, (_now() - started_at.astimezone(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return float("inf")


def supervise_recovery(config: BuyerRecoveryConfig) -> int:
    _ensure_state_dir(config)
    run_id = str(_read_status(config.status_path).get("run_id") or "")
    _write_supervisor_identity(config, pid=os.getpid(), run_id=run_id)
    processes: list[subprocess.Popen[Any]] = []
    adapter = WbBuyerSessionAdapter(config=config.session)
    try:
        with adapter.session_lock(blocking=False):
            _ensure_commands(config)
            _write_status(config, {**_read_status(config.status_path), "status": "starting", "reason": "buyer_visual_session_starting"})
            xvfb = _spawn(["Xvfb", config.display, "-screen", "0", "1600x900x24", "-nolisten", "tcp"], config.xvfb_log_path)
            processes.append(xvfb)
            _wait_display(config.display)
            openbox_path = shutil.which("openbox")
            if openbox_path:
                processes.append(_spawn([openbox_path], config.openbox_log_path, env={"DISPLAY": config.display}))
            return _capture_login(config, adapter, processes)
    except BlockingIOError:
        _write_status(config, {**_read_status(config.status_path), "status": "error", "reason": "buyer_session_lock_busy", "finished_at": _now_text()})
        return 1
    except Exception:
        _write_status(config, {**_read_status(config.status_path), "status": "error", "reason": "buyer_recovery_runtime_error", "finished_at": _now_text()})
        return 1
    finally:
        for process in reversed(processes):
            _terminate(process)
        config.candidate_path.unlink(missing_ok=True)
        if _supervisor_identity_matches(config, _read_supervisor_identity(config), run_id):
            config.pid_path.unlink(missing_ok=True)


def _capture_login(
    config: BuyerRecoveryConfig,
    adapter: WbBuyerSessionAdapter,
    processes: list[subprocess.Popen[Any]],
) -> int:
    deadline = time.monotonic() + config.timeout_sec
    env = os.environ.copy()
    env["DISPLAY"] = config.display
    old_display = os.environ.get("DISPLAY")
    os.environ["DISPLAY"] = config.display
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            try:
                context_args: dict[str, Any] = {"locale": "ru-RU", "viewport": {"width": 1500, "height": 820}}
                if config.session.storage_state_path.exists():
                    context_args["storage_state"] = str(config.session.storage_state_path)
                context = browser.new_context(**context_args)
                page = context.new_page()
                page.goto(config.session.buyer_url, wait_until="domcontentloaded")
                _write_status(
                    config,
                    {
                        **_read_status(config.status_path),
                        "status": "checking_session",
                        "reason": "buyer_recovery_checking_session",
                        "session": {"status": "missing", "valid": False},
                    },
                )
                last_session: Mapping[str, Any] = {}
                automatic_login_attempted = False
                human_window_started = False
                candidate_probe_attempts = 0
                while time.monotonic() < deadline:
                    surface = _inspect_login_surface(page)
                    if surface.get("state") == "authenticated":
                        # Human completion can arrive through an async WB
                        # redirect just like the saved-account click.  Apply
                        # the same bounded DOM/network-idle settle before
                        # taking candidate storage state.
                        _settle_after_login_action(config, page)
                        _write_status(
                            config,
                            {
                                **_read_status(config.status_path),
                                "status": "stabilizing_session",
                                "reason": "buyer_session_stabilizing",
                                "session": last_session,
                            },
                        )
                        candidate = _capture_settled_candidate(config, adapter, context, page)
                        last_session = candidate
                        result = _accept_recovery_candidate(config, adapter, candidate)
                        if result is not None:
                            return result
                        candidate_probe_attempts += 1
                        if candidate.get("status") == "wrong_account":
                            surface = {"state": "human", "reason": "buyer_account_fingerprint_mismatch"}
                        elif candidate.get("status") == "migration_required":
                            _write_status(
                                config,
                                {
                                    **_read_status(config.status_path),
                                    "status": "migration_required",
                                    "reason": "buyer_fingerprint_migration_unproven",
                                    "finished_at": _now_text(),
                                    "session": candidate,
                                },
                            )
                            return 1
                        elif candidate_probe_attempts >= 2:
                            _write_status(
                                config,
                                {
                                    **_read_status(config.status_path),
                                    "status": "error",
                                    "reason": "buyer_session_post_login_probe_failed",
                                    "finished_at": _now_text(),
                                    "session": candidate,
                                },
                            )
                            return 1
                        else:
                            page.wait_for_timeout(max(500, int(config.poll_sec * 1000)))
                            continue

                    if surface.get("state") == "automatic_login" and not automatic_login_attempted:
                        automatic_login_attempted = True
                        _write_status(
                            config,
                            {
                                **_read_status(config.status_path),
                                "status": "automatic_login",
                                "reason": "buyer_saved_account_login_started",
                                "session": last_session,
                            },
                        )
                        if _click_saved_account(surface):
                            _settle_after_login_action(config, page)
                            continue
                        surface = {"state": "human", "reason": "buyer_saved_account_login_unavailable"}

                    if surface.get("state") == "automatic_login":
                        surface = {"state": "human", "reason": "buyer_saved_account_login_not_completed"}
                    if not human_window_started:
                        _start_human_window(config, processes)
                        human_window_started = True
                    _write_status(
                        config,
                        {
                            **_read_status(config.status_path),
                            "status": "awaiting_human",
                            "reason": str(surface.get("reason") or "buyer_human_action_required"),
                            "session": last_session,
                        },
                    )
                    page.wait_for_timeout(max(500, int(config.poll_sec * 1000)))
                _write_status(
                    config,
                    {
                        **_read_status(config.status_path),
                        "status": "timeout",
                        "reason": "buyer_login_timeout",
                        "finished_at": _now_text(),
                        "session": last_session,
                    },
                )
                return 1
            finally:
                browser.close()
    finally:
        if old_display is None:
            os.environ.pop("DISPLAY", None)
        else:
            os.environ["DISPLAY"] = old_display


def _capture_settled_candidate(
    config: BuyerRecoveryConfig,
    adapter: WbBuyerSessionAdapter,
    context: Any,
    page: Any,
    *,
    fresh_adapter_factory: Any = WbBuyerSessionAdapter,
) -> dict[str, Any]:
    del adapter
    page.wait_for_timeout(max(1_000, int(config.session.settle_timeout_ms)))
    try:
        context.storage_state(path=str(config.candidate_path), indexed_db=True)
    except TypeError:
        # Older Playwright runtimes do not expose indexed_db; retain the
        # cookie/localStorage snapshot rather than failing recovery outright.
        context.storage_state(path=str(config.candidate_path))
    os.chmod(config.candidate_path, 0o600)
    return fresh_adapter_factory(config=config.session).check_session(
        storage_state_path=config.candidate_path,
        persist_fingerprint=False,
        acquire_lock=False,
    )


def _accept_recovery_candidate(
    config: BuyerRecoveryConfig,
    adapter: WbBuyerSessionAdapter,
    candidate: Mapping[str, Any],
) -> int | None:
    if candidate.get("status") != "valid":
        return None
    _write_status(
        config,
        {**_read_status(config.status_path), "status": "saving_session", "reason": "buyer_session_saving", "session": candidate},
    )
    rollback_path = config.session.state_dir / f"storage_state.rollback.{uuid4().hex}.json"
    had_canonical_state = config.session.storage_state_path.exists()
    if had_canonical_state:
        shutil.copy2(config.session.storage_state_path, rollback_path)
        os.chmod(rollback_path, 0o600)
    try:
        adapter.persist_storage_state_atomically(config.candidate_path)
    except Exception:
        _restore_canonical_storage(config, rollback_path, had_canonical_state=had_canonical_state)
        raise
    _write_status(
        config,
        {**_read_status(config.status_path), "status": "validating_session", "reason": "buyer_session_validating", "session": candidate},
    )
    try:
        validated = adapter.check_session(persist_fingerprint=True, acquire_lock=False)
    except Exception:
        validated = {
            "status": "probe_error",
            "valid": False,
            "reason": "buyer_session_post_save_validation_failed",
        }
    if validated.get("status") != "valid":
        _restore_canonical_storage(config, rollback_path, had_canonical_state=had_canonical_state)
        _write_status(
            config,
            {
                **_read_status(config.status_path),
                "status": "error",
                "reason": str(validated.get("reason") or "buyer_session_post_save_validation_failed"),
                "finished_at": _now_text(),
                "session": validated,
            },
        )
        return 1
    rollback_path.unlink(missing_ok=True)
    _write_status(
        config,
        {
            **_read_status(config.status_path),
            "status": "completed",
            "reason": "buyer_session_saved_and_validated",
            "finished_at": _now_text(),
            "session": validated,
        },
    )
    return 0


def _restore_canonical_storage(
    config: BuyerRecoveryConfig,
    rollback_path: Path,
    *,
    had_canonical_state: bool,
) -> None:
    if rollback_path.exists():
        rollback_path.replace(config.session.storage_state_path)
        os.chmod(config.session.storage_state_path, 0o600)
        return
    if not had_canonical_state:
        config.session.storage_state_path.unlink(missing_ok=True)


def _inspect_login_surface(page: Any) -> dict[str, Any]:
    injected = getattr(page, "wb_recovery_surface", None)
    if isinstance(injected, Mapping):
        return dict(injected)
    body = _safe_body_text(page).lower()
    if any(marker in body for marker in ("код из смс", "введите код", "код подтверждения", "отправили код")):
        return {"state": "human", "reason": "buyer_sms_required"}
    if any(marker in body for marker in ("введите номер телефона", "номер телефона", "получить код")):
        return {"state": "human", "reason": "buyer_phone_required"}
    if any(marker in body for marker in ("подтвердите, что вы не робот", "капча", "captcha", "data-site-key")):
        return {"state": "human", "reason": "buyer_captcha_required"}
    if any(marker in body for marker in ("подтвердите вход", "подтверждение безопасности", "это вы")):
        return {"state": "human", "reason": "buyer_security_confirmation_required"}
    candidates = _saved_account_login_candidates(page, body=body)
    if len(candidates) == 1:
        return {"state": "automatic_login", "reason": "buyer_saved_account_available", "candidate": candidates[0]}
    if len(candidates) > 1 or any(marker in body for marker in ("выберите аккаунт", "другой аккаунт")):
        return {"state": "human", "reason": "buyer_account_selection_required"}
    if _visible_login_completed(page) and not any(
        marker in body for marker in ("войти или зарегистрироваться", "войдите в аккаунт", "получить код")
    ):
        return {"state": "authenticated", "reason": "buyer_visible_account_opened"}
    return {"state": "human", "reason": "buyer_human_action_required"}


def _saved_account_login_candidates(page: Any, *, body: str = "") -> list[Any]:
    result: list[Any] = []
    try:
        locator = page.locator("button, [role='button'], input[type='submit'], input[type='button'], a[href]")
        count = min(int(locator.count()), 100)
    except Exception:
        return result
    account_markers = (
        "сохранённый аккаунт",
        "сохраненный аккаунт",
        "этим аккаунтом",
        "продолжить как",
        "войти как",
        "аккаунт",
        "профиль",
    )
    body_has_account_marker = any(marker in body.lower() for marker in account_markers)
    for index in range(count):
        item = locator.nth(index)
        try:
            text = " ".join(str(item.inner_text(timeout=500) or "").split()).lower()
            aria_label = " ".join(str(item.get_attribute("aria-label") or "").split()).lower()
            title = " ".join(str(item.get_attribute("title") or "").split()).lower()
            action_text = " ".join(value for value in (text, aria_label, title) if value)
            visible = bool(item.is_visible())
            enabled = bool(item.is_enabled())
        except Exception:
            continue
        is_saved_account_action = action_text in {"войти", "продолжить", "далее"} or (
            body_has_account_marker and any(token in action_text for token in ("войти", "продолж", "далее", "аккаунт"))
        ) or any(
            marker in action_text
            for marker in (
                "войти под этим аккаунтом",
                "продолжить как",
                "войти как",
                "войти через аккаунт",
                "войти в аккаунт",
                "войти в личный кабинет",
                "продолжить вход",
                "выбрать аккаунт",
            )
        )
        if action_text in {"продолжить", "далее"} and not body_has_account_marker:
            is_saved_account_action = False
        if visible and enabled and is_saved_account_action:
            result.append(item)
    # WB has shipped saved-account cards whose continuation control is an
    # icon/link without accessible text.  Only accept this fallback when the
    # login surface advertises an account and there is exactly one visible
    # enabled control; human markers are handled before this function.
    if not result:
        visible_controls: list[Any] = []
        for index in range(count):
            item = locator.nth(index)
            try:
                if item.is_visible() and item.is_enabled():
                    visible_controls.append(item)
            except Exception:
                continue
        if len(visible_controls) == 1:
            result.append(visible_controls[0])
    return result


def _click_saved_account(surface: Mapping[str, Any]) -> bool:
    candidate = surface.get("candidate")
    if candidate is None:
        return False
    try:
        candidate.click(timeout=5_000)
        return True
    except Exception:
        return False


def _settle_after_login_action(config: BuyerRecoveryConfig, page: Any) -> None:
    stable_url = ""
    stable_count = 0
    try:
        page.wait_for_load_state("domcontentloaded", timeout=min(15_000, config.session.navigation_timeout_ms))
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=min(10_000, config.session.navigation_timeout_ms))
    except Exception:
        pass
    deadline = time.monotonic() + max(1.0, config.session.settle_timeout_ms / 1000)
    while time.monotonic() < deadline:
        current_url = str(getattr(page, "url", "") or "")
        if current_url == stable_url and current_url:
            stable_count += 1
        else:
            stable_url, stable_count = current_url, 1
        if stable_count >= 3:
            break
        page.wait_for_timeout(500)


def _safe_body_text(page: Any) -> str:
    try:
        return str(page.locator("body").inner_text(timeout=2_000) or "")[:30_000]
    except Exception:
        return ""


def _visible_login_completed(page: Any) -> bool:
    try:
        parsed = urllib_parse.urlparse(str(page.url or ""))
    except Exception:
        return False
    host = str(parsed.hostname or "").lower()
    path = str(parsed.path or "").rstrip("/").lower()
    return (host == "wildberries.ru" or host.endswith(".wildberries.ru")) and (
        path == "/lk" or path.startswith("/lk/")
    )


def _start_human_window(config: BuyerRecoveryConfig, processes: list[subprocess.Popen[Any]]) -> None:
    x11vnc = _spawn(
        [
            "x11vnc",
            "-display",
            config.display,
            "-localhost",
            "-shared",
            "-forever",
            "-nopw",
            "-noxdamage",
            "-rfbport",
            str(config.vnc_port),
        ],
        config.x11vnc_log_path,
    )
    processes.append(x11vnc)
    _wait_port(config.vnc_port)
    websockify = _spawn(
        [
            "websockify",
            f"127.0.0.1:{config.web_port}",
            f"127.0.0.1:{config.vnc_port}",
            "--web",
            str(config.novnc_web_dir),
        ],
        config.websockify_log_path,
    )
    processes.append(websockify)
    _wait_port(config.web_port)


def build_macos_launcher_archive(
    config: BuyerRecoveryConfig,
    *,
    public_status_url: str,
    public_operator_url: str,
) -> tuple[bytes, str]:
    status = read_recovery_status(config, with_probe=False)
    run_id = str(status.get("run_id") or "")
    if not run_id or not status.get("running") or status.get("status") != "awaiting_human":
        raise RuntimeError("buyer recovery launcher is not ready")
    body = _launcher_script(
        config,
        run_id=run_id,
        public_status_url=public_status_url,
        public_operator_url=public_operator_url,
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo("Открыть вход Wildberries.command")
        info.external_attr = 0o755 << 16
        archive.writestr(info, body)
    return buffer.getvalue(), f"wb-buyer-session-{run_id}.zip"


def _launcher_script(
    config: BuyerRecoveryConfig,
    *,
    run_id: str,
    public_status_url: str,
    public_operator_url: str,
) -> str:
    return "\n".join(
        [
            "#!/bin/bash",
            "set -euo pipefail",
            f"WEB_PORT={config.web_port}",
            f"SSH_DESTINATION={shlex.quote(config.ssh_destination)}",
            f"NOVNC_URL={shlex.quote(config.novnc_url)}",
            f"RUN_ID={shlex.quote(run_id)}",
            f"OPERATOR_URL={shlex.quote(public_operator_url)}",
            "REMOTE_APP=/opt/wb-core-runtime/app/apps/wb_buyer_session_recovery.py",
            'SSH_LOG="${TMPDIR:-/tmp}/wb-buyer-recovery-ssh.log"',
            f"MAX_POLLS={max(1, int(config.timeout_sec / 3) + 20)}",
            'cleanup() { if [[ -n "${SSH_PID:-}" ]]; then kill "${SSH_PID}" >/dev/null 2>&1 || true; fi; }',
            'close_novnc() {',
            '  osascript -e "tell application \\"Safari\\" to close (every tab of every window whose URL starts with \\"http://127.0.0.1:${WEB_PORT}/\\")" >/dev/null 2>&1 || true',
            '  osascript -e "tell application \\"Google Chrome\\" to close (every tab of every window whose URL starts with \\"http://127.0.0.1:${WEB_PORT}/\\")" >/dev/null 2>&1 || true',
            '}',
            "trap cleanup EXIT",
            'ssh -o ExitOnForwardFailure=yes -L "${WEB_PORT}:127.0.0.1:${WEB_PORT}" "${SSH_DESTINATION}" -N >"${SSH_LOG}" 2>&1 &',
            "SSH_PID=$!",
            'for attempt in $(seq 1 20); do if curl -fsS --max-time 2 "${NOVNC_URL}" >/dev/null 2>&1; then break; fi; sleep 1; done',
            'open "${NOVNC_URL}"',
            'echo "Окно входа Wildberries открыто для запуска ${RUN_ID}."',
            'FINAL_STATUS=""',
            'for attempt in $(seq 1 "${MAX_POLLS}"); do',
            '  if ! kill -0 "${SSH_PID}" >/dev/null 2>&1; then FINAL_STATUS="tunnel_error"; break; fi',
            '  STATUS_JSON=$(ssh -o BatchMode=yes -o ConnectTimeout=5 "${SSH_DESTINATION}" python3 "${REMOTE_APP}" status --run-id "${RUN_ID}" 2>/dev/null || true)',
            '  RUN_STATUS=$(printf "%s" "${STATUS_JSON}" | /usr/bin/python3 -c \'import json,sys; data=json.load(sys.stdin); print(data.get("status", ""))\' 2>/dev/null || true)',
            '  case "${RUN_STATUS}" in completed|migration_required|stopped|timeout|error) FINAL_STATUS="${RUN_STATUS}"; break ;; esac',
            '  sleep 3',
            'done',
            'if [[ -z "${FINAL_STATUS}" ]]; then FINAL_STATUS="timeout"; fi',
            'close_novnc',
            'echo "WB_BUYER_RECOVERY_FINAL=${FINAL_STATUS}"',
            'open "${OPERATOR_URL}" >/dev/null 2>&1 || true',
            "",
        ]
    )


def _write_status(config: BuyerRecoveryConfig, payload: Mapping[str, Any]) -> None:
    safe = {
        key: value
        for key, value in payload.items()
        if key in {"run_id", "status", "reason", "started_at", "finished_at", "deadline_at", "session"}
    }
    staged = config.session.state_dir / f".recovery-status-{uuid4().hex}.tmp"
    staged.write_text(json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    os.chmod(staged, 0o600)
    staged.replace(config.status_path)
    os.chmod(config.status_path, 0o600)


def _read_status(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "idle", "run_id": "", "reason": ""}
    return dict(value) if isinstance(value, Mapping) else {"status": "idle", "run_id": "", "reason": ""}


def _ensure_state_dir(config: BuyerRecoveryConfig) -> None:
    config.session.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(config.session.state_dir, 0o700)


def _ensure_commands(config: BuyerRecoveryConfig) -> None:
    missing = [name for name in ("Xvfb", "x11vnc", "websockify") if shutil.which(name) is None]
    if missing:
        raise RuntimeError("buyer recovery dependencies missing")
    if not config.novnc_web_dir.exists():
        raise RuntimeError("buyer recovery noVNC assets missing")


def _spawn(args: list[str], log_path: Path, *, env: Mapping[str, str] | None = None) -> subprocess.Popen[Any]:
    merged = os.environ.copy()
    merged.update(dict(env or {}))
    log = _open_secure_log(log_path)
    return subprocess.Popen(args, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, env=merged)


def _open_secure_log(path: Path) -> Any:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    handle = path.open("ab", buffering=0)
    os.chmod(path, 0o600)
    return handle


def _wait_display(display: str) -> None:
    number = display.lstrip(":").split(".", 1)[0]
    path = Path(f"/tmp/.X11-unix/X{number}")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.2)
    raise RuntimeError("buyer recovery display did not start")


def _wait_port(port: int) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("buyer recovery localhost port did not start")


def _terminate(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass


@contextmanager
def _recovery_start_lock(config: BuyerRecoveryConfig) -> Any:
    handle = config.start_lock_path.open("a+", encoding="utf-8")
    os.chmod(config.start_lock_path, 0o600)
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@contextmanager
def _null_context() -> Any:
    yield


def _write_supervisor_identity(config: BuyerRecoveryConfig, *, pid: int, run_id: str) -> None:
    payload = {
        "pid": int(pid),
        "pgid": _safe_process_group(int(pid)),
        "run_id": str(run_id or ""),
        "proc_start_ticks": _process_start_ticks(int(pid)),
    }
    staged = config.session.state_dir / f".recovery-supervisor-{uuid4().hex}.tmp"
    staged.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    os.chmod(staged, 0o600)
    staged.replace(config.pid_path)
    os.chmod(config.pid_path, 0o600)


def _read_supervisor_identity(config: BuyerRecoveryConfig) -> dict[str, Any]:
    try:
        raw = config.pid_path.read_text(encoding="utf-8").strip()
        payload = json.loads(raw)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _identity_pid(identity: Mapping[str, Any]) -> int | None:
    try:
        pid = int(identity.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _verified_supervisor_running(config: BuyerRecoveryConfig, run_id: str) -> bool:
    return _supervisor_identity_matches(config, _read_supervisor_identity(config), run_id)


def _supervisor_identity_matches(
    config: BuyerRecoveryConfig,
    identity: Mapping[str, Any],
    run_id: str,
) -> bool:
    del config
    pid = _identity_pid(identity)
    if not pid or not _pid_running(pid):
        return False
    if str(identity.get("run_id") or "") != str(run_id or ""):
        return False
    if int(identity.get("pgid") or 0) != pid or _safe_process_group(pid) != pid:
        return False
    recorded_start = str(identity.get("proc_start_ticks") or "")
    if not recorded_start or recorded_start != _process_start_ticks(pid):
        return False
    cmdline = _process_command_line(pid)
    if not cmdline:
        return False
    return Path(__file__).name in cmdline and " supervise " in f" {cmdline} "


def _process_start_ticks(pid: int) -> str:
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
        _prefix, separator, suffix = raw.rpartition(") ")
        fields = suffix.split() if separator else raw.split()
        return str(fields[19] if separator else fields[21])
    except (OSError, IndexError, ValueError):
        try:
            return subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(int(pid))],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            ).stdout.strip()
        except Exception:
            return ""


def _process_command_line(pid: int) -> str:
    try:
        return Path(f"/proc/{int(pid)}/cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        try:
            return subprocess.run(
                ["ps", "-o", "command=", "-p", str(int(pid))],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            ).stdout.strip()
        except Exception:
            return ""


def _safe_process_group(pid: int) -> int:
    try:
        return int(os.getpgid(int(pid)))
    except (OSError, ValueError):
        return 0


def _terminate_process_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and _pid_running(pid):
        time.sleep(0.2)
    if _pid_running(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _pid_running(pid: int) -> bool:
    try:
        waited_pid, _status = os.waitpid(pid, os.WNOHANG)
        if waited_pid == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _supervisor_command(config: BuyerRecoveryConfig) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "supervise",
        "--state-dir",
        str(config.session.state_dir),
        "--storage-state-path",
        str(config.session.storage_state_path),
        "--display",
        config.display,
        "--vnc-port",
        str(config.vnc_port),
        "--web-port",
        str(config.web_port),
        "--timeout-sec",
        str(config.timeout_sec),
        "--poll-sec",
        str(config.poll_sec),
        "--ssh-destination",
        config.ssh_destination,
        "--novnc-web-dir",
        str(config.novnc_web_dir),
    ]


def _add_args(parser: argparse.ArgumentParser, config: BuyerRecoveryConfig) -> None:
    parser.add_argument("--state-dir", default=str(config.session.state_dir))
    parser.add_argument("--storage-state-path", default=str(config.session.storage_state_path))
    parser.add_argument("--display", default=config.display)
    parser.add_argument("--vnc-port", type=int, default=config.vnc_port)
    parser.add_argument("--web-port", type=int, default=config.web_port)
    parser.add_argument("--timeout-sec", type=int, default=config.timeout_sec)
    parser.add_argument("--poll-sec", type=float, default=config.poll_sec)
    parser.add_argument("--ssh-destination", default=config.ssh_destination)
    parser.add_argument("--novnc-web-dir", default=str(config.novnc_web_dir))


def _config_from_args(args: argparse.Namespace) -> BuyerRecoveryConfig:
    session = load_wb_buyer_session_config_from_env()
    session = WbBuyerSessionConfig(
        state_dir=Path(args.state_dir).expanduser(),
        storage_state_path=Path(args.storage_state_path).expanduser(),
        buyer_url=session.buyer_url,
        product_url_template=session.product_url_template,
        navigation_timeout_ms=session.navigation_timeout_ms,
        settle_timeout_ms=session.settle_timeout_ms,
    )
    return BuyerRecoveryConfig(
        session=session,
        display=args.display,
        vnc_port=args.vnc_port,
        web_port=args.web_port,
        timeout_sec=args.timeout_sec,
        poll_sec=args.poll_sec,
        ssh_destination=args.ssh_destination,
        novnc_web_dir=Path(args.novnc_web_dir),
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _now_text() -> str:
    return _now().isoformat()


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name) or default))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.environ.get(name) or default))
    except ValueError:
        return default


if __name__ == "__main__":
    main()
