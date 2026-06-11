"""Browser smoke for Seller Portal recovery state machine in web-vitrina UI."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import socket
import sys
import threading
from urllib import parse as urllib_parse
import zipfile

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH,
    DEFAULT_SELLER_PORTAL_RECOVERY_START_PATH,
    DEFAULT_SELLER_PORTAL_RECOVERY_STATUS_PATH,
    DEFAULT_SHEET_JOB_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
    DEFAULT_SHEET_WEB_VITRINA_SELLER_RECOVERY_START_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    _render_sheet_vitrina_web_vitrina_ui,
)


def main() -> None:
    with RecoveryUiFixtureServer() as fixture:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                page.goto(fixture.base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH, wait_until="domcontentloaded")
                page.locator("[data-activity-block] > summary").click()
                page.wait_for_selector("[data-session-install]", timeout=10000)
                controls_payload = page.evaluate(
                    """() => ({
                      check: document.querySelectorAll('[data-session-check]').length,
                      install: document.querySelectorAll('[data-session-install]').length,
                      recovery: document.querySelectorAll('[data-session-recovery-start]').length,
                      launcher: document.querySelectorAll('[data-session-launcher]').length
                    })"""
                )
                if controls_payload != {"check": 1, "install": 1, "recovery": 0, "launcher": 0}:
                    raise AssertionError(f"web-vitrina must expose only check/install session controls, got {controls_payload}")

                with page.expect_response(f"**{DEFAULT_SHEET_WEB_VITRINA_SELLER_RECOVERY_START_PATH}") as start_response_info:
                    page.click("[data-session-install]")
                if start_response_info.value.status != HTTPStatus.ACCEPTED.value:
                    raise AssertionError(f"install start route must return 202 job payload, got {start_response_info.value.status}")
                page.wait_for_function(
                    "() => (document.querySelector('[data-session-recovery-state]') || {}).textContent.includes('Запускаем')",
                    timeout=10000,
                )
                if fixture.launcher_requests != 0:
                    raise AssertionError(
                        f"restore click must not download launcher while run is starting, got {fixture.launcher_requests}"
                    )
                first_start_payload = fixture.start_payloads[-1]
                if first_start_payload != {"replace": True, "async": True}:
                    raise AssertionError(f"web-vitrina recovery start must request replace=true async flow, got {first_start_payload}")

                page.wait_for_function(
                    """() => {
                      const state = document.querySelector('[data-session-recovery-state]');
                      return !!state && state.textContent.includes('Нужно войти');
                    }""",
                    timeout=10000,
                )

                page.wait_for_function(
                    """() => {
                      const log = (document.querySelector('[data-activity-log-body]') || {}).textContent || '';
                      return log.includes('launcher artifact') || log.includes('Launcher route');
                    }""",
                    timeout=10000,
                )
                log_after_409 = page.locator("[data-activity-log-body]").inner_text()
                if "Не удалось скачать launcher" in log_after_409:
                    raise AssertionError(f"409/not-ready launcher response must be non-fatal UI state, got {log_after_409!r}")
                if fixture.launcher_requests < 1:
                    raise AssertionError(f"automatic launcher attempt must hit launcher route, got {fixture.launcher_requests}")

                page.wait_for_function(
                    "() => (document.querySelector('[data-activity-log-body]') || {}).textContent.includes('Launcher для Seller Portal recovery скачан автоматически')",
                    timeout=10000,
                )
                post_download_controls = page.evaluate(
                    """() => ({
                      check: document.querySelectorAll('[data-session-check]').length,
                      install: document.querySelectorAll('[data-session-install]').length,
                      recovery: document.querySelectorAll('[data-session-recovery-start]').length,
                      launcher: document.querySelectorAll('[data-session-launcher]').length
                    })"""
                )
                if post_download_controls != {"check": 1, "install": 1, "recovery": 0, "launcher": 0}:
                    raise AssertionError(f"launcher download must not render extra session controls, got {post_download_controls}")
                page.wait_for_function(
                    """() => {
                      const node = document.querySelector('[data-seller-top-session]');
                      const word = node ? node.querySelector('[data-seller-top-session-label]') : null;
                      const separator = node ? node.querySelector('[data-seller-top-session-separator]') : null;
                      return !!node
                        && !!word
                        && !!separator
                        && word.textContent.trim() === 'сессия'
                        && separator.textContent.trim() === '|'
                        && !node.classList.contains('tone-warning')
                        && !node.classList.contains('is-actionable');
                    }""",
                    timeout=5000,
                )
                if fixture.launcher_requests != 2:
                    raise AssertionError(f"second automatic launcher attempt must download zip, got {fixture.launcher_requests}")

                with page.expect_response(f"**{DEFAULT_SHEET_WEB_VITRINA_SELLER_RECOVERY_START_PATH}") as second_start_response_info:
                    page.click("[data-session-install]")
                if second_start_response_info.value.status != HTTPStatus.ACCEPTED.value:
                    raise AssertionError(f"second install start route must return 202, got {second_start_response_info.value.status}")
                if fixture.start_payloads[-1] != {"replace": True, "async": True}:
                    raise AssertionError(f"repeated install click must remain replace=true/idempotent, got {fixture.start_payloads}")

                print("seller_recovery_ui_controls: ok -> only check/install session controls")
                print("seller_recovery_ui_starting_no_autodownload: ok -> install waits until launcher readiness")
                print("seller_recovery_ui_awaiting_autodownload: ok -> awaiting_login triggers launcher download automatically")
                print("seller_recovery_ui_launcher_409_nonfatal: ok -> not-ready launcher 409 is warning, not fatal copy")
                print("seller_recovery_ui_no_extra_buttons: ok -> launcher fallback controls are not rendered")
                print("seller_recovery_ui_repeated_install_replace: ok -> repeated install clicks keep replace=true")
                print("smoke-check passed")
            finally:
                browser.close()


class RecoveryUiFixtureServer:
    def __init__(self) -> None:
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.base_url = ""
        self.run_counter = 0
        self.started = False
        self.status_reads_after_start = 0
        self.launcher_requests = 0
        self.start_payloads: list[dict[str, object]] = []

    def __enter__(self) -> "RecoveryUiFixtureServer":
        port = _reserve_free_port()
        self.base_url = f"http://127.0.0.1:{port}"
        handler = self._build_handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)

    def _build_handler(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urllib_parse.urlparse(self.path)
                if parsed.path == DEFAULT_SHEET_WEB_VITRINA_UI_PATH:
                    _write_response(
                        self,
                        HTTPStatus.OK,
                        "text/html; charset=utf-8",
                        _render_sheet_vitrina_web_vitrina_ui(
                            read_path=DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
                            operator_path=DEFAULT_SHEET_OPERATOR_UI_PATH,
                            refresh_path="/v1/sheet-vitrina-v1/refresh",
                            job_path=DEFAULT_SHEET_JOB_PATH,
                        ).encode("utf-8"),
                    )
                    return
                if parsed.path == DEFAULT_SHEET_WEB_VITRINA_READ_PATH:
                    _write_json(self, HTTPStatus.OK, _page_composition_payload())
                    return
                if parsed.path == DEFAULT_SELLER_PORTAL_RECOVERY_STATUS_PATH:
                    _write_json(self, HTTPStatus.OK, fixture._next_status_payload())
                    return
                if parsed.path == DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH:
                    fixture.launcher_requests += 1
                    if fixture.launcher_requests == 1:
                        _write_json(
                            self,
                            HTTPStatus.CONFLICT,
                            {
                                "error": "seller portal recovery launcher unavailable: launcher artifact is not ready",
                                "status": "launcher_unavailable",
                                "launcher_status": "launcher_artifact_missing",
                                "run_id": fixture.current_run_id,
                                "run_status": "awaiting_login",
                                "running": True,
                                "launcher_ready": False,
                                "can_download_launcher": False,
                                "summary": "Launcher route controlled 409: launcher artifact is not ready yet.",
                                "reason": "launcher artifact is not ready yet",
                                "retryable": True,
                            },
                        )
                        return
                    _write_response(
                        self,
                        HTTPStatus.OK,
                        "application/zip",
                        _minimal_launcher_zip(),
                        headers={"Content-Disposition": 'attachment; filename="seller-portal-relogin-macos.zip"'},
                    )
                    return
                if parsed.path == DEFAULT_SHEET_JOB_PATH:
                    _write_json(
                        self,
                        HTTPStatus.OK,
                        {
                            "job_id": "seller-recovery-job",
                            "operation": "session_recovery_start",
                            "status": "success",
                            "result": fixture._recovery_payload("starting"),
                            "log_lines": [
                                "event=seller_recovery_finish result=accepted run_status=starting launcher_ready=false"
                            ],
                        },
                    )
                    return
                _write_json(self, HTTPStatus.NOT_FOUND, {"error": f"unsupported path: {parsed.path}"})

            def do_POST(self) -> None:  # noqa: N802
                parsed = urllib_parse.urlparse(self.path)
                if parsed.path in {
                    DEFAULT_SHEET_WEB_VITRINA_SELLER_RECOVERY_START_PATH,
                    DEFAULT_SELLER_PORTAL_RECOVERY_START_PATH,
                }:
                    payload = _read_json(self)
                    fixture.start_payloads.append(payload)
                    fixture.run_counter += 1
                    fixture.started = True
                    fixture.status_reads_after_start = 0
                    self.send_response(HTTPStatus.ACCEPTED.value)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("X-Seller-Recovery-Start-Count", str(len(fixture.start_payloads)))
                    body = json.dumps(
                        {
                            "job_id": "seller-recovery-job",
                            "operation": "session_recovery_start",
                            "status": "running",
                            "job_path": f"{DEFAULT_SHEET_JOB_PATH}?job_id=seller-recovery-job",
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                _write_json(self, HTTPStatus.NOT_FOUND, {"error": f"unsupported path: {parsed.path}"})

            def log_message(self, format: str, *args) -> None:  # noqa: A003
                return

        return Handler

    @property
    def current_run_id(self) -> str:
        return f"seller-recovery-ui-run-{self.run_counter or 1}"

    def _next_status_payload(self) -> dict[str, object]:
        if not self.started:
            return self._recovery_payload("idle")
        if self.status_reads_after_start == 0:
            self.status_reads_after_start += 1
            return self._recovery_payload("starting")
        self.status_reads_after_start += 1
        return self._recovery_payload("awaiting_login")

    def _recovery_payload(self, status: str) -> dict[str, object]:
        run_id = self.current_run_id if status != "idle" else ""
        can_download = bool(run_id) and status == "awaiting_login"
        labels = {
            "idle": "Не запущено",
            "starting": "Запускаем",
            "awaiting_login": "Нужно войти",
        }
        summary = {
            "idle": "Новый запуск восстановления сейчас не выполняется.",
            "starting": "Запускаем текущее временное окно входа на host.",
            "awaiting_login": "Временное окно входа готово. Откройте скачанный launcher и войдите в seller portal.",
        }[status]
        return {
            "status": status,
            "status_label": labels[status],
            "status_tone": "warning" if status == "awaiting_login" else ("loading" if status == "starting" else "idle"),
            "run_status": status,
            "run_status_label": labels[status],
            "run_status_tone": "warning" if status == "awaiting_login" else ("loading" if status == "starting" else "idle"),
            "summary": summary,
            "reason": summary,
            "running": status in {"starting", "awaiting_login"},
            "can_start": True,
            "can_stop": status in {"starting", "awaiting_login"},
            "launcher_enabled": can_download,
            "launcher_ready": can_download,
            "can_download_launcher": can_download,
            "can_open_login_window": False,
            "launcher_url": DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH if can_download else "",
            "launcher_download_path": DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH,
            "run_id": run_id,
            "current_run_id": run_id,
            "run_is_final": False,
            "run_final_status": "",
            "run_final_label": "",
            "final_marker": "",
            "session_status": "session_invalid",
            "session_status_label": "Нужен вход",
            "session_status_tone": "error",
        }


def _page_composition_payload() -> dict[str, object]:
    dates = ["2026-05-05", "2026-05-06"]
    groups = [
        _loading_group("wb_api", "WB API", dates, session_controls=False),
        _loading_group("seller_portal_bot", "Seller Portal / бот", dates, session_controls=True),
        _loading_group("other_sources", "Прочие источники", dates, session_controls=False),
    ]
    return {
        "composition_name": "web_vitrina_page_composition",
        "composition_version": "v1",
        "meta": {
            "current_state": "empty",
            "state_message": "fixture",
            "snapshot_as_of_date": "2026-05-06",
            "as_of_date": "2026-05-06",
            "yesterday_closed_date": "2026-05-05",
            "today_current_date": "2026-05-06",
            "business_timezone": "Asia/Yekaterinburg",
            "state_namespace": "wb-core:sheet-vitrina-v1:recovery-ui-smoke",
            "browser_state_persistence": "localStorage",
            "grid_library_name": "fixture",
        },
        "summary_cards": [],
        "historical_access": {
            "options": [{"value": item, "label": item} for item in dates],
            "selected_as_of_date": "2026-05-06",
            "selected_date_from": "",
            "selected_date_to": "",
            "current_mode": "default",
            "status_text": "fixture",
            "default_as_of_date": "2026-05-06",
            "url_state_mode": "query_string",
            "browser_state_persistence": "localStorage",
            "supported_query_mode": "history_mode_explicit_date_window",
            "preset_options": [],
            "empty_message": "",
        },
        "filter_surface": {
            "controls": [],
            "sort_options": [],
            "default_sort_value": "",
            "empty_result_message": "fixture rows are intentionally empty",
        },
        "table_surface": {
            "table_data_state": "included",
            "columns": [],
            "rows": [],
            "total_row_count": 0,
        },
        "status_summary": {},
        "capabilities": {},
        "activity_surface": {
            "log_block": {
                "title": "Лог",
                "subtitle": "fixture",
                "status_label": "Нет данных",
                "tone": "neutral",
                "detail": "",
                "preview_lines": [],
                "line_count": 0,
                "download_path": "",
                "log_filename": "",
                "empty_message": "Лог пока недоступен.",
            },
            "loading_table": {
                "title": "Загрузка данных",
                "subtitle": "fixture",
                "source_status_state": "loaded",
                "today_date": "2026-05-06",
                "yesterday_date": "2026-05-05",
                "available_dates": dates,
                "default_refresh_date": "2026-05-06",
                "groups": groups,
                "columns": [
                    {"id": "source", "label": "Источник"},
                    {"id": "today_status", "label": "Сегодня: 2026-05-06"},
                    {"id": "today_reason", "label": "Причина сегодня"},
                    {"id": "yesterday_status", "label": "Вчера: 2026-05-05"},
                    {"id": "yesterday_reason", "label": "Причина вчера"},
                    {"id": "metrics", "label": "Метрики"},
                    {"id": "technical_endpoint", "label": "Технический endpoint"},
                ],
                "rows": [
                    {
                        "source_key": "seller_funnel_snapshot",
                        "source_group_id": "seller_portal_bot",
                        "source_label": "Seller Portal source",
                        "today": {"label": "Внимание", "tone": "warning", "ok": False},
                        "today_reason": "требуется seller session",
                        "yesterday": {"label": "Внимание", "tone": "warning", "ok": False},
                        "yesterday_reason": "требуется seller session",
                        "metric_labels": ["Воронка продавца"],
                        "technical_endpoint": "Seller Portal / bot",
                    }
                ],
            },
        },
    }


def _loading_group(group_id: str, label: str, dates: list[str], *, session_controls: bool) -> dict[str, object]:
    return {
        "group_id": group_id,
        "label": label,
        "source_keys": [],
        "last_updated_at": "",
        "refresh_action": {
            "label": "Обновить группу",
            "source_group_id": group_id,
            "default_as_of_date": dates[-1],
            "available_dates": dates,
            "min_date": dates[0],
            "max_date": dates[-1],
        },
        "session_controls": session_controls,
    }


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, object]:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length).decode("utf-8")
    return json.loads(raw) if raw.strip() else {}


def _write_json(handler: BaseHTTPRequestHandler, status: HTTPStatus, payload: dict[str, object]) -> None:
    _write_response(
        handler,
        status,
        "application/json; charset=utf-8",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
    )


def _write_response(
    handler: BaseHTTPRequestHandler,
    status: HTTPStatus,
    content_type: str,
    body: bytes,
    *,
    headers: dict[str, str] | None = None,
) -> None:
    handler.send_response(status.value)
    handler.send_header("Content-Type", content_type)
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _minimal_launcher_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("seller-portal-relogin.command", "#!/bin/bash\necho ready\n")
    return buffer.getvalue()


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    main()
