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
from urllib import request as urllib_request
import zipfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH,
    DEFAULT_SELLER_PORTAL_RECOVERY_START_PATH,
    DEFAULT_SELLER_PORTAL_RECOVERY_STATUS_PATH,
    DEFAULT_SELLER_PORTAL_RECOVERY_STOP_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


class _FakeSellerRecoveryController:
    def __init__(self) -> None:
        self.running = False
        self.visual_ready = False
        self.calls: list[str] = []

    def read_status(self, *, launcher_download_path: str) -> dict[str, object]:
        self.calls.append("status")
        if self.running:
            if not self.visual_ready:
                self.visual_ready = True
                return {
                    "status": "starting_visual_session",
                    "status_label": "Запускаем браузер",
                    "status_tone": "loading",
                    "summary": "Запускаем удалённый браузер seller portal.",
                    "instruction": "Дождитесь статуса «Ожидается вход».",
                    "technical_line": "Нужный кабинет: ИП Сагитов В. Р. · supplier canonical-supplier-id",
                    "running": True,
                    "can_start": False,
                    "can_stop": True,
                    "launcher_enabled": False,
                    "launcher_download_path": launcher_download_path,
                }
            return {
                "status": "awaiting_login",
                "status_label": "Ожидается вход",
                "status_tone": "warning",
                "summary": "Откройте launcher и войдите в seller portal.",
                "instruction": "После входа система сама завершит recovery.",
                "technical_line": "Нужный кабинет: ИП Сагитов В. Р. · supplier canonical-supplier-id",
                "running": True,
                "can_start": False,
                "can_stop": True,
                "launcher_enabled": True,
                "launcher_download_path": launcher_download_path,
            }
        return {
            "status": "session_invalid",
            "status_label": "Требуется вход",
            "status_tone": "error",
            "summary": "Сессия seller portal больше не действует; запустите восстановление.",
            "instruction": "Нажмите «Восстановить Seller-сессию», затем скачайте launcher.",
            "technical_line": "Нужный кабинет: ИП Сагитов В. Р. · supplier canonical-supplier-id",
            "running": False,
            "can_start": True,
            "can_stop": False,
            "launcher_enabled": False,
            "launcher_download_path": launcher_download_path,
        }

    def start(self, *, replace: bool, launcher_download_path: str) -> dict[str, object]:
        self.calls.append(f"start:{replace}")
        self.running = True
        self.visual_ready = False
        return self.read_status(launcher_download_path=launcher_download_path)

    def stop(self, *, launcher_download_path: str) -> dict[str, object]:
        self.calls.append("stop")
        self.running = False
        self.visual_ready = False
        return {
            "status": "stopped",
            "status_label": "Остановлено",
            "status_tone": "idle",
            "summary": "Временный recovery-сеанс остановлен.",
            "instruction": "При необходимости можно запустить recovery заново.",
            "technical_line": "Нужный кабинет: ИП Сагитов В. Р. · supplier canonical-supplier-id",
            "running": False,
            "can_start": True,
            "can_stop": False,
            "launcher_enabled": False,
            "launcher_download_path": launcher_download_path,
        }

    def build_launcher_archive(self, *, public_status_url: str, public_operator_url: str) -> tuple[bytes, str]:
        self.calls.append("launcher")
        archive_buffer = io.BytesIO()
        with zipfile.ZipFile(archive_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            info = zipfile.ZipInfo("seller-portal-relogin.command")
            info.external_attr = 0o755 << 16
            archive.writestr(
                info,
                "\n".join(
                    [
                        "#!/bin/bash",
                        f'STATUS_URL="{public_status_url}"',
                        f'OPERATOR_URL="{public_operator_url}"',
                    ]
                ),
            )
        return archive_buffer.getvalue(), "seller-portal-relogin-macos.zip"


def main() -> None:
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
            operator_status, operator_html = _get_text(base_url + DEFAULT_SHEET_OPERATOR_UI_PATH)
            if operator_status != 200:
                raise AssertionError(f"operator UI must return 200, got {operator_status}")
            if "Восстановление Seller-сессии" not in operator_html:
                raise AssertionError("operator UI must render the seller recovery block")
            config_payload = _extract_operator_ui_config(operator_html)
            for key, expected in {
                "seller_recovery_status_path": DEFAULT_SELLER_PORTAL_RECOVERY_STATUS_PATH,
                "seller_recovery_start_path": DEFAULT_SELLER_PORTAL_RECOVERY_START_PATH,
                "seller_recovery_stop_path": DEFAULT_SELLER_PORTAL_RECOVERY_STOP_PATH,
                "seller_recovery_launcher_path": DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH,
            }.items():
                if config_payload.get(key) != expected:
                    raise AssertionError(f"operator UI config must expose {key}, got {config_payload.get(key)!r}")

            status_code, status_payload = _get_json(base_url + DEFAULT_SELLER_PORTAL_RECOVERY_STATUS_PATH)
            if status_code != 200 or status_payload.get("status") != "session_invalid":
                raise AssertionError(f"initial recovery status must surface invalid session, got {status_code} / {status_payload}")

            start_code, start_payload = _post_json(base_url + DEFAULT_SELLER_PORTAL_RECOVERY_START_PATH, {"replace": True})
            if start_code != 200 or start_payload.get("status") != "starting_visual_session":
                raise AssertionError(f"start must surface visual-session startup before awaiting_login, got {start_code} / {start_payload}")
            if start_payload.get("launcher_enabled") is not False:
                raise AssertionError("starting_visual_session must keep launcher disabled until browser window is ready")

            status_code, status_payload = _get_json(base_url + DEFAULT_SELLER_PORTAL_RECOVERY_STATUS_PATH)
            if status_code != 200 or status_payload.get("status") != "awaiting_login":
                raise AssertionError(f"status after start must stay awaiting_login, got {status_code} / {status_payload}")
            if status_payload.get("launcher_enabled") is not True:
                raise AssertionError("awaiting_login payload must enable launcher download once the browser window is visible")

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

            stop_code, stop_payload = _post_json(base_url + DEFAULT_SELLER_PORTAL_RECOVERY_STOP_PATH, {})
            if stop_code != 200 or stop_payload.get("status") != "stopped":
                raise AssertionError(f"stop must cleanup recovery contour, got {stop_code} / {stop_payload}")
            if controller.calls != ["status", "start:True", "status", "status", "launcher", "stop"]:
                raise AssertionError(f"unexpected recovery controller lifecycle, got {controller.calls}")

            print("seller_portal_recovery_http_operator: ok -> operator UI exposes recovery block and config")
            print("seller_portal_recovery_http_lifecycle: ok -> start/status/stop lifecycle is wired")
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
