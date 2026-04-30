"""Targeted smoke-check for seller portal recovery HTTP/operator contour."""

from __future__ import annotations

import json
import io
from pathlib import Path
import re
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
from urllib import error as urllib_error
from urllib import request as urllib_request
import zipfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SELLER_PORTAL_SESSION_CHECK_PATH,
    DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH,
    DEFAULT_SELLER_PORTAL_RECOVERY_START_PATH,
    DEFAULT_SELLER_PORTAL_RECOVERY_STATUS_PATH,
    DEFAULT_SELLER_PORTAL_RECOVERY_STOP_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_http_entrypoint import (  # noqa: E402
    RegistryUploadHttpEntrypoint,
    SellerPortalRecoveryController,
)
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402
from apps.seller_portal_relogin_session import probe_storage_state  # noqa: E402


class _FakeSellerRecoveryController:
    def __init__(self) -> None:
        self.running = False
        self.visual_ready = False
        self.current_run_id = ""
        self.run_counter = 0
        self.not_needed_next_start = False
        self.calls: list[str] = []

    def _build_run_payload(
        self,
        *,
        status: str,
        launcher_download_path: str,
        summary: str,
        instruction: str,
        running: bool,
        launcher_enabled: bool,
    ) -> dict[str, object]:
        final_status = status if status in {"completed", "not_needed", "stopped", "timeout", "error"} else ""
        final_label = {
            "completed": "Восстановление завершено",
            "not_needed": "Повторный вход не потребовался",
            "stopped": "Восстановление остановлено",
            "timeout": "Восстановление завершено по таймауту",
            "error": "Восстановление завершено с ошибкой",
        }.get(final_status, "")
        return {
            "status": status,
            "status_label": {
                "idle": "Не запущено",
                "starting": "Запускаем",
                "awaiting_login": "Нужно войти",
                "not_needed": "Не потребовалось",
                "stopped": "Остановлено",
            }.get(status, "Ошибка"),
            "status_tone": {
                "idle": "idle",
                "starting": "loading",
                "awaiting_login": "warning",
                "not_needed": "success",
                "stopped": "idle",
            }.get(status, "error"),
            "run_status": status,
            "run_status_label": {
                "idle": "Не запущено",
                "starting": "Запускаем",
                "awaiting_login": "Нужно войти",
                "not_needed": "Не потребовалось",
                "stopped": "Остановлено",
            }.get(status, "Ошибка"),
            "run_status_tone": {
                "idle": "idle",
                "starting": "loading",
                "awaiting_login": "warning",
                "not_needed": "success",
                "stopped": "idle",
            }.get(status, "error"),
            "summary": summary,
            "instruction": instruction,
            "technical_line": "Нужный кабинет: ИП Сагитов В. Р. · supplier canonical-supplier-id",
            "running": running,
            "can_start": not running,
            "can_stop": running,
            "launcher_enabled": launcher_enabled,
            "launcher_download_path": launcher_download_path,
            "run_id": self.current_run_id,
            "current_run_id": self.current_run_id,
            "run_is_final": bool(final_status),
            "run_final_status": final_status,
            "run_final_label": final_label,
            "started_at": "2026-04-23T10:00:00+05:00" if self.current_run_id else "",
            "finished_at": "2026-04-23T10:01:00+05:00" if final_status else "",
            "session_status": "session_invalid" if status in {"idle", "starting", "awaiting_login", "stopped"} else "session_valid_canonical",
            "session_status_label": "Нужен вход" if status in {"idle", "starting", "awaiting_login", "stopped"} else "Сессия активна",
            "session_status_tone": "error" if status in {"idle", "starting", "awaiting_login", "stopped"} else "success",
        }

    def check_session(self, *, launcher_download_path: str) -> dict[str, object]:
        self.calls.append("check")
        return {
            "status": "session_invalid",
            "status_label": "Нужен вход",
            "status_tone": "error",
            "summary": "Сохранённая seller-сессия больше не действует.",
            "instruction": "Нажмите «Восстановить сессию» и войдите через launcher для Mac.",
            "technical_line": "Нужный кабинет: ИП Сагитов В. Р. · supplier canonical-supplier-id",
            "running": self.running,
            "can_start": True,
            "can_stop": False,
            "launcher_enabled": False,
            "launcher_download_path": launcher_download_path,
        }

    def read_status(self, *, launcher_download_path: str, run_id: str | None = None) -> dict[str, object]:
        self.calls.append(f"status:{run_id or ''}")
        if run_id and run_id != self.current_run_id:
            return self._build_run_payload(
                status="error",
                launcher_download_path=launcher_download_path,
                summary="Текущий launcher больше не смотрит на свой запуск: этот recovery run уже не является текущим.",
                instruction="Откройте operator page заново и скачайте launcher для нового запуска.",
                running=False,
                launcher_enabled=False,
            )
        if self.running:
            if not self.visual_ready:
                self.visual_ready = True
                return self._build_run_payload(
                    status="starting",
                    launcher_download_path=launcher_download_path,
                    summary="Запускаем текущее временное окно входа на host.",
                    instruction="Дождитесь статуса «Нужно войти».",
                    running=True,
                    launcher_enabled=False,
                )
            return self._build_run_payload(
                status="awaiting_login",
                launcher_download_path=launcher_download_path,
                summary="Временное окно входа готово. Откройте launcher и войдите в seller portal.",
                instruction="После входа система сама сохранит storage_state.json и завершит текущий запуск.",
                running=True,
                launcher_enabled=True,
            )
        if self.not_needed_next_start:
            return self._build_run_payload(
                status="not_needed",
                launcher_download_path=launcher_download_path,
                summary="Повторный вход не потребовался: на момент старта seller-сессия уже была активна и нужный кабинет был подтверждён.",
                instruction="Текущий запуск завершён сразу, без noVNC и launcher.",
                running=False,
                launcher_enabled=False,
            )
        return self._build_run_payload(
            status="idle",
            launcher_download_path=launcher_download_path,
            summary="Новый запуск восстановления сейчас не выполняется. Сохранённая seller-сессия больше не действует.",
            instruction="Нажмите «Восстановить сессию», затем скачайте launcher и выполните вход.",
            running=False,
            launcher_enabled=False,
        )

    def start(self, *, replace: bool, launcher_download_path: str) -> dict[str, object]:
        self.calls.append(f"start:{replace}")
        self.run_counter += 1
        self.current_run_id = f"seller-recovery-run-{self.run_counter}"
        if self.not_needed_next_start:
            self.running = False
            self.visual_ready = False
            self.not_needed_next_start = False
            return self._build_run_payload(
                status="not_needed",
                launcher_download_path=launcher_download_path,
                summary="Повторный вход не потребовался: на момент старта seller-сессия уже была активна и нужный кабинет был подтверждён.",
                instruction="Текущий запуск завершён сразу, без noVNC и launcher.",
                running=False,
                launcher_enabled=False,
            )
        self.running = True
        self.visual_ready = False
        return self._build_run_payload(
            status="starting",
            launcher_download_path=launcher_download_path,
            summary="Запускаем текущее временное окно входа на host.",
            instruction="Дождитесь статуса «Нужно войти».",
            running=True,
            launcher_enabled=False,
        )

    def stop(self, *, launcher_download_path: str) -> dict[str, object]:
        self.calls.append("stop")
        self.running = False
        self.visual_ready = False
        return self._build_run_payload(
            status="stopped",
            launcher_download_path=launcher_download_path,
            summary="Восстановление остановлено: временное окно входа закрыто. Сохранённая seller-сессия и бот не изменены.",
            instruction="Кнопка «Остановить восстановление» закрывает только временное окно входа.",
            running=False,
            launcher_enabled=False,
        )

    def build_launcher_archive(self, *, public_status_url: str, public_operator_url: str) -> tuple[bytes, str]:
        self.calls.append("launcher")
        if not (self.running and self.visual_ready):
            raise RuntimeError(
                "seller recovery launcher is only available while the current recovery run is awaiting login"
            )
        archive_buffer = io.BytesIO()
        with zipfile.ZipFile(archive_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            info = zipfile.ZipInfo("seller-portal-relogin.command")
            info.external_attr = 0o755 << 16
            archive.writestr(
                info,
                "\n".join(
                    [
                        "#!/bin/bash",
                        "set -euo pipefail",
                        f'RUN_ID="{self.current_run_id}"',
                        f'STATUS_URL="{public_status_url}?run_id={self.current_run_id}"',
                        f'OPERATOR_URL="{public_operator_url}"',
                        'STATUS_JSON="$(curl -fsS "${STATUS_URL}" 2>/dev/null || true)"',
                        "STATUS=\"$(printf '%s' \"${STATUS_JSON}\" | python3 -c 'import json, sys; raw = sys.stdin.read().strip(); print((json.loads(raw).get(\"status\", \"\") if raw else \"\"), end=\"\")' 2>/dev/null || true)\"",
                        'SUMMARY="$(printf \'%s\' "${STATUS_JSON}" | python3 -c \'import json, sys; raw = sys.stdin.read().strip(); print((json.loads(raw).get(\"summary\", \"\") if raw else \"\"), end=\"\")\' 2>/dev/null || true)"',
                        'echo "Восстановление завершено: ${STATUS:-unknown}"',
                        'echo "${SUMMARY}"',
                    ]
                ),
            )
        return archive_buffer.getvalue(), "seller-portal-relogin-macos.zip"


def main() -> None:
    with TemporaryDirectory(prefix="sheet-vitrina-recovery-probe-") as probe_tmp:
        storage_state = Path(probe_tmp) / "storage_state.json"
        storage_state.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
        missing_python_payload = probe_storage_state(
            storage_state,
            wb_bot_python=Path(probe_tmp) / "missing-venv" / "bin" / "python",
        )
        if (
            missing_python_payload.get("ok") is not False
            or missing_python_payload.get("status") != "seller_portal_session_probe_unavailable"
        ):
            raise AssertionError(
                "missing wb-web-bot python must degrade into a truthful probe payload, "
                f"got {missing_python_payload}"
            )

    probe_error_controller = SellerPortalRecoveryController(
        config_factory=lambda: SimpleNamespace(
            canonical_supplier_id="canonical-supplier-id",
            canonical_supplier_label="ИП Сагитов В. Р.",
        ),
        status_reader=lambda *args, **kwargs: (_ for _ in ()).throw(
            FileNotFoundError("/opt/wb-web-bot/venv/bin/python")
        ),
    )
    probe_error_payload = probe_error_controller.check_session(
        launcher_download_path=DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH,
    )
    if probe_error_payload.get("status") != "session_probe_error":
        raise AssertionError(f"session-check must degrade probe exceptions into truthful payload, got {probe_error_payload}")

    with TemporaryDirectory(prefix="sheet-vitrina-recovery-http-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        port = _reserve_free_port()
        controller = _FakeSellerRecoveryController()
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            seller_portal_recovery_controller=controller,
        )
        config = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1",
            port=port,
            upload_path="/v1/registry-upload/bundle",
            sheet_plan_path="/v1/sheet-vitrina-v1/plan",
            sheet_refresh_path="/v1/sheet-vitrina-v1/refresh",
            sheet_status_path="/v1/sheet-vitrina-v1/status",
            sheet_operator_ui_path=DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=runtime_dir,
        )
        server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{port}"
            operator_status, operator_html = _get_text(base_url + DEFAULT_SHEET_OPERATOR_UI_PATH + "?embedded_tab=vitrina")
            if operator_status != 200:
                raise AssertionError(f"operator UI must return 200, got {operator_status}")
            if "Проверка и восстановление Seller-сессии" not in operator_html or "Проверить сессию" not in operator_html:
                raise AssertionError("operator UI must render the seller recovery block")
            config_payload = _extract_operator_ui_config(operator_html)
            for key, expected in {
                "seller_session_check_path": DEFAULT_SELLER_PORTAL_SESSION_CHECK_PATH,
                "seller_recovery_status_path": DEFAULT_SELLER_PORTAL_RECOVERY_STATUS_PATH,
                "seller_recovery_start_path": DEFAULT_SELLER_PORTAL_RECOVERY_START_PATH,
                "seller_recovery_stop_path": DEFAULT_SELLER_PORTAL_RECOVERY_STOP_PATH,
                "seller_recovery_launcher_path": DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH,
            }.items():
                if config_payload.get(key) != expected:
                    raise AssertionError(f"operator UI config must expose {key}, got {config_payload.get(key)!r}")

            check_code, check_payload = _get_json(base_url + DEFAULT_SELLER_PORTAL_SESSION_CHECK_PATH)
            if check_code != 200 or check_payload.get("status") != "session_invalid":
                raise AssertionError(f"session-check must surface invalid session, got {check_code} / {check_payload}")

            status_code, status_payload = _get_json(base_url + DEFAULT_SELLER_PORTAL_RECOVERY_STATUS_PATH)
            if status_code != 200 or status_payload.get("status") != "idle" or status_payload.get("session_status") != "session_invalid":
                raise AssertionError(f"initial recovery status must separate idle run-state from invalid session-state, got {status_code} / {status_payload}")

            unavailable_code, unavailable_payload = _get_json_allow_error(
                base_url + DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH
            )
            if unavailable_code != 409 or "launcher unavailable" not in str(unavailable_payload.get("error") or ""):
                raise AssertionError(
                    "launcher route must return truthful unavailable JSON before awaiting_login, "
                    f"got {unavailable_code} / {unavailable_payload}"
                )

            start_code, start_payload = _post_json(base_url + DEFAULT_SELLER_PORTAL_RECOVERY_START_PATH, {"replace": True})
            if start_code != 200 or start_payload.get("status") != "starting" or not start_payload.get("run_id"):
                raise AssertionError(f"start must surface current run startup with run_id, got {start_code} / {start_payload}")
            if start_payload.get("launcher_enabled") is not False:
                raise AssertionError("starting status must keep launcher disabled until browser window is ready")

            status_code, status_payload = _get_json(base_url + DEFAULT_SELLER_PORTAL_RECOVERY_STATUS_PATH)
            if status_code != 200 or status_payload.get("status") != "starting":
                raise AssertionError(f"first status after start must still surface startup, got {status_code} / {status_payload}")

            status_code, status_payload = _get_json(base_url + DEFAULT_SELLER_PORTAL_RECOVERY_STATUS_PATH)
            if status_code != 200 or status_payload.get("status") != "awaiting_login":
                raise AssertionError(f"status after startup must switch to awaiting_login, got {status_code} / {status_payload}")
            if status_payload.get("launcher_enabled") is not True:
                raise AssertionError("awaiting_login payload must enable launcher download once the browser window is visible")
            run_id = str(status_payload.get("run_id") or "").strip()
            scoped_status_code, scoped_status_payload = _get_json(
                base_url + DEFAULT_SELLER_PORTAL_RECOVERY_STATUS_PATH + "?run_id=" + run_id
            )
            if scoped_status_code != 200 or scoped_status_payload.get("run_id") != run_id or scoped_status_payload.get("status") != "awaiting_login":
                raise AssertionError(f"run-aware status surface must resolve the active recovery run, got {scoped_status_code} / {scoped_status_payload}")

            launcher_request = urllib_request.Request(base_url + DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH, method="GET")
            with urllib_request.urlopen(launcher_request, timeout=15) as response:
                launcher_bytes = response.read()
                if response.status != 200:
                    raise AssertionError(f"launcher download must return 200, got {response.status}")
                if response.getheader("Content-Type") != "application/zip":
                    raise AssertionError(f"launcher download must be zip, got {response.getheader('Content-Type')!r}")
            with zipfile.ZipFile(io.BytesIO(launcher_bytes), "r") as archive:
                names = archive.namelist()
                if names != ["seller-portal-relogin.command"]:
                    raise AssertionError(f"launcher zip must contain executable .command, got {names}")
                launcher_text = archive.read("seller-portal-relogin.command").decode("utf-8")
                if DEFAULT_SELLER_PORTAL_RECOVERY_STATUS_PATH not in launcher_text or DEFAULT_SHEET_OPERATOR_UI_PATH not in launcher_text:
                    raise AssertionError("launcher script must poll recovery status and reopen operator UI")
                if "python3 -c" not in launcher_text or 'json.loads(raw).get("status", "")' not in launcher_text:
                    raise AssertionError("launcher script must parse the root recovery status via JSON, not regex")
                if "run_id=" + run_id not in launcher_text or "Восстановление завершено: ${STATUS:-unknown}" not in launcher_text:
                    raise AssertionError("launcher script must bind to the current run_id and print a final completion marker")

            stop_code, stop_payload = _post_json(base_url + DEFAULT_SELLER_PORTAL_RECOVERY_STOP_PATH, {})
            if stop_code != 200 or stop_payload.get("status") != "stopped":
                raise AssertionError(f"stop must cleanup recovery contour, got {stop_code} / {stop_payload}")
            if stop_payload.get("run_final_status") != "stopped":
                raise AssertionError(f"stop must surface a final stopped outcome, got {stop_payload}")

            controller.not_needed_next_start = True
            second_start_code, second_start_payload = _post_json(base_url + DEFAULT_SELLER_PORTAL_RECOVERY_START_PATH, {"replace": True})
            if second_start_code != 200 or second_start_payload.get("status") != "not_needed" or second_start_payload.get("run_final_status") != "not_needed":
                raise AssertionError(f"not_needed start must finish immediately with final outcome, got {second_start_code} / {second_start_payload}")
            if controller.calls != ["check", "status:", "launcher", "start:True", "status:", "status:", f"status:{run_id}", "launcher", "stop", "start:True"]:
                raise AssertionError(f"unexpected recovery controller lifecycle, got {controller.calls}")

            print("seller_portal_session_check_http: ok -> lightweight session-check route is wired")
            print("seller_portal_session_check_probe_error: ok -> probe exceptions stay 200-shape")
            print("seller_portal_recovery_http_operator: ok -> operator UI exposes recovery block and config")
            print("seller_portal_recovery_http_lifecycle: ok -> run-aware start/status/stop/not_needed lifecycle is wired")
            print("seller_portal_recovery_launcher_unavailable: ok -> unavailable launcher is 409 JSON, not 500")
            print("seller_portal_recovery_launcher_download: ok -> downloadable Mac launcher is attached")
            print("smoke-check passed")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def _extract_operator_ui_config(html: str) -> dict[str, object]:
    match = re.search(
        r'<script id="sheet-vitrina-v1-operator-config" type="application/json">(.*?)</script>',
        html,
        re.S,
    )
    if not match:
        raise AssertionError("operator UI config script is missing")
    return json.loads(match.group(1))


def _get_text(url: str) -> tuple[int, str]:
    request = urllib_request.Request(url, method="GET")
    with urllib_request.urlopen(request, timeout=15) as response:
        return response.status, response.read().decode("utf-8")


def _get_json(url: str) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(url, method="GET", headers={"Accept": "application/json"})
    with urllib_request.urlopen(request, timeout=15) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _get_json_allow_error(url: str) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with urllib_request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _post_json(url: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
    )
    with urllib_request.urlopen(request, timeout=15) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    main()
