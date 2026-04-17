"""Интеграционный smoke-check для HTTP entrypoint registry upload."""

from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
from urllib import error, request as urllib_request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.registry_upload_smoke_support import (
    LEGACY_CONFIG_CAP,
    LEGACY_FORMULAS_CAP,
    LEGACY_METRICS_CAP,
    build_synthetic_oversized_bundle,
    write_runtime_registry_fixture,
)
from packages.adapters.registry_upload_http_entrypoint import (
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_REFRESH_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_bundle_v1 import RegistryUploadBundleV1Block
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig

ARTIFACTS_DIR = ROOT / "artifacts" / "registry_upload_http_entrypoint"
INPUT_BUNDLE_FIXTURE = ARTIFACTS_DIR / "input" / "registry_upload_bundle__fixture.json"
TARGET_DIR = ARTIFACTS_DIR / "target"
ACTIVATED_AT = "2026-04-13T12:00:03Z"


def main() -> None:
    input_bundle = _load_json(INPUT_BUNDLE_FIXTURE)
    with TemporaryDirectory(prefix="registry-upload-http-entrypoint-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        port = _reserve_free_port()
        config = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1",
            port=port,
            upload_path=DEFAULT_UPLOAD_PATH,
            sheet_plan_path=DEFAULT_SHEET_PLAN_PATH,
            sheet_refresh_path=DEFAULT_SHEET_REFRESH_PATH,
            sheet_status_path=DEFAULT_SHEET_STATUS_PATH,
            sheet_operator_ui_path=DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=runtime_dir,
        )
        env = os.environ.copy()
        env.update(
            {
                "REGISTRY_UPLOAD_HTTP_HOST": config.host,
                "REGISTRY_UPLOAD_HTTP_PORT": str(config.port),
                "REGISTRY_UPLOAD_HTTP_PATH": config.upload_path,
                "SHEET_VITRINA_HTTP_PATH": config.sheet_plan_path,
                "SHEET_VITRINA_REFRESH_HTTP_PATH": config.sheet_refresh_path,
                "SHEET_VITRINA_STATUS_HTTP_PATH": config.sheet_status_path,
                "SHEET_VITRINA_OPERATOR_UI_PATH": config.sheet_operator_ui_path,
                "REGISTRY_UPLOAD_RUNTIME_DIR": str(config.runtime_dir),
            }
        )
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "apps" / "registry_upload_http_entrypoint_live.py")],
            cwd=ROOT,
            env={**env, "REGISTRY_UPLOAD_ACTIVATED_AT_OVERRIDE": ACTIVATED_AT},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            base_url = f"http://127.0.0.1:{config.port}{config.upload_path}"
            plan_url = f"http://127.0.0.1:{config.port}{config.sheet_plan_path}"
            status_url = f"http://127.0.0.1:{config.port}{config.sheet_status_path}"
            operator_ui_url = f"http://127.0.0.1:{config.port}{config.sheet_operator_ui_path}"

            accepted_status, accepted_payload = _post_json_when_ready(
                base_url,
                _load_json(INPUT_BUNDLE_FIXTURE),
            )
            if accepted_status != 200:
                raise AssertionError(f"accepted request must return 200, got {accepted_status}")
            accepted_expected = _load_json(TARGET_DIR / "http_result__accepted__fixture.json")
            if accepted_payload != accepted_expected:
                raise AssertionError("accepted HTTP result differs from target fixture")
            if accepted_payload["accepted_counts"]["config_v2"] != len(input_bundle["config_v2"]):
                raise AssertionError("HTTP entrypoint must persist all config_v2 rows from request body")
            if accepted_payload["accepted_counts"]["metrics_v2"] != len(input_bundle["metrics_v2"]):
                raise AssertionError("HTTP entrypoint must persist all metrics_v2 rows from request body")
            if accepted_payload["accepted_counts"]["formulas_v2"] != len(input_bundle["formulas_v2"]):
                raise AssertionError("HTTP entrypoint must persist all formulas_v2 rows from request body")

            runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
            current_state = asdict(runtime.load_current_state())
            current_expected = _load_json(TARGET_DIR / "current_state__fixture.json")
            if current_state != current_expected:
                raise AssertionError("runtime current state differs from HTTP target fixture")

            operator_ui_status, operator_ui_html = _get_text(operator_ui_url)
            if operator_ui_status != 200:
                raise AssertionError(f"operator UI must return 200, got {operator_ui_status}")
            if "Обновление данных витрины" not in operator_ui_html or "Загрузить данные" not in operator_ui_html:
                raise AssertionError("operator UI must expose the expected minimal page")
            if "Статус" not in operator_ui_html or "Результат" not in operator_ui_html or "ожидание" not in operator_ui_html:
                raise AssertionError("operator UI must keep the compact Russian chrome")
            if "Строки DATA_VITRINA" not in operator_ui_html or "Строки STATUS" not in operator_ui_html:
                raise AssertionError("operator UI must surface row-count fields with Russian labels")
            if "Сервер и расписание" not in operator_ui_html or "Часовой пояс" not in operator_ui_html:
                raise AssertionError("operator UI must expose the compact server context block")
            if "Автообновление" not in operator_ui_html or "Технический триггер" not in operator_ui_html:
                raise AssertionError("operator UI must expose scheduler labels in Russian")
            if "Снимок пока не подготовлен." not in operator_ui_html:
                raise AssertionError("operator UI must keep the Russian empty-state helper text")
            if (
                "UTC yesterday" in operator_ui_html
                or "server-side refresh" in operator_ui_html
                or "Ready snapshot пока не materialized." in operator_ui_html
            ):
                raise AssertionError("operator UI must not keep the stale explanatory date subtitle")
            operator_ui_config = _extract_operator_ui_config(operator_ui_html)
            if operator_ui_config != {
                "page_title": "Обновление данных витрины",
                "refresh_path": config.sheet_refresh_path,
                "status_path": config.sheet_status_path,
            }:
                raise AssertionError("operator UI config must expose existing refresh/status paths")

            missing_plan_status, missing_plan_payload = _get_json(plan_url)
            if missing_plan_status != 422:
                raise AssertionError(f"plan read before refresh must return 422, got {missing_plan_status}")
            if "ready snapshot missing" not in str(missing_plan_payload.get("error", "")):
                raise AssertionError("plan read before refresh must surface ready snapshot miss")

            missing_status_status, missing_status_payload = _get_json(status_url)
            if missing_status_status != 422:
                raise AssertionError(f"status read before refresh must return 422, got {missing_status_status}")
            if "ready snapshot missing" not in str(missing_status_payload.get("error", "")):
                raise AssertionError("status read before refresh must surface ready snapshot miss")
            server_context = missing_status_payload.get("server_context")
            if not isinstance(server_context, dict):
                raise AssertionError("status read before refresh must still expose server_context metadata")
            if server_context.get("business_timezone") != "Asia/Yekaterinburg":
                raise AssertionError("status read before refresh must expose the canonical business timezone")
            if server_context.get("daily_refresh_business_time") != "11:00 Asia/Yekaterinburg":
                raise AssertionError("status read before refresh must expose the daily business refresh time")
            if server_context.get("daily_refresh_systemd_time") != "06:00:00 UTC":
                raise AssertionError("status read before refresh must expose the current host UTC trigger time")
            if server_context.get("daily_refresh_systemd_oncalendar") != "*-*-* 06:00:00 UTC":
                raise AssertionError("status read before refresh must expose the configured OnCalendar trigger")

            duplicate_status, duplicate_payload = _post_json(base_url, _load_json(INPUT_BUNDLE_FIXTURE))
            if duplicate_status != 409:
                raise AssertionError(f"duplicate request must return 409, got {duplicate_status}")
            duplicate_expected = _load_json(TARGET_DIR / "http_result__duplicate_bundle_version__fixture.json")
            if duplicate_payload != duplicate_expected:
                raise AssertionError("duplicate HTTP result differs from target fixture")

            if asdict(runtime.load_current_state()) != current_expected:
                raise AssertionError("runtime current state changed after HTTP duplicate rejection")

            print(f"accepted status: ok -> {accepted_payload['status']}")
            print(f"http path: ok -> {config.upload_path}")
            print(f"current bundle_version: ok -> {current_state['bundle_version']}")
            print(f"operator_page: ok -> {config.sheet_operator_ui_path}")
            print(f"plan_before_refresh: ok -> {missing_plan_payload['error']}")
            print(f"status_before_refresh: ok -> {missing_status_payload['error']}")
            print(f"duplicate status: ok -> {duplicate_payload['status']}")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    synthetic_bundle = build_synthetic_oversized_bundle()
    if len(synthetic_bundle.config_v2) <= LEGACY_CONFIG_CAP:
        raise AssertionError("synthetic config_v2 count must exceed legacy hardcoded cap")
    if len(synthetic_bundle.metrics_v2) <= LEGACY_METRICS_CAP:
        raise AssertionError("synthetic metrics_v2 count must exceed legacy hardcoded cap")
    if len(synthetic_bundle.formulas_v2) <= LEGACY_FORMULAS_CAP:
        raise AssertionError("synthetic formulas_v2 count must exceed legacy hardcoded cap")

    with TemporaryDirectory(prefix="registry-upload-http-entrypoint-uncapped-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime_registry_path = Path(tmp) / "runtime_registry.json"
        write_runtime_registry_fixture(runtime_registry_path, synthetic_bundle)
        port = _reserve_free_port()
        config = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1",
            port=port,
            upload_path=DEFAULT_UPLOAD_PATH,
            sheet_plan_path=DEFAULT_SHEET_PLAN_PATH,
            sheet_refresh_path=DEFAULT_SHEET_REFRESH_PATH,
            sheet_status_path=DEFAULT_SHEET_STATUS_PATH,
            sheet_operator_ui_path=DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=runtime_dir,
        )
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=RegistryUploadDbBackedRuntime(
                runtime_dir=runtime_dir,
                bundle_block=RegistryUploadBundleV1Block(runtime_registry_path=runtime_registry_path),
            ),
            activated_at_factory=lambda: ACTIVATED_AT,
        )
        server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, payload = _post_json(
                f"http://127.0.0.1:{config.port}{config.upload_path}",
                asdict(synthetic_bundle),
            )
            if status != 200:
                raise AssertionError(f"synthetic oversized HTTP request must return 200, got {status}")
            if payload["accepted_counts"]["config_v2"] != len(synthetic_bundle.config_v2):
                raise AssertionError("synthetic HTTP request must persist all config_v2 rows")
            if payload["accepted_counts"]["metrics_v2"] != len(synthetic_bundle.metrics_v2):
                raise AssertionError("synthetic HTTP request must persist all metrics_v2 rows")
            if payload["accepted_counts"]["formulas_v2"] != len(synthetic_bundle.formulas_v2):
                raise AssertionError("synthetic HTTP request must persist all formulas_v2 rows")
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    print(
        "uncapped HTTP bundle: ok -> "
        f"{len(synthetic_bundle.config_v2)}/{len(synthetic_bundle.metrics_v2)}/{len(synthetic_bundle.formulas_v2)}"
    )
    print("smoke-check passed")


def _post_json(url: str, payload: object) -> tuple[int, object]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib_request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib_request.urlopen(req) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _post_json_when_ready(url: str, payload: object) -> tuple[int, object]:
    deadline = time.time() + 10
    while True:
        try:
            return _post_json(url, payload)
        except error.URLError as exc:
            if time.time() >= deadline:
                raise AssertionError(f"HTTP entrypoint did not become reachable: {exc}") from exc
            time.sleep(0.1)


def _get_json(url: str) -> tuple[int, object]:
    req = urllib_request.Request(url, method="GET")
    try:
        with urllib_request.urlopen(req) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get_text(url: str) -> tuple[int, str]:
    req = urllib_request.Request(url, method="GET")
    try:
        with urllib_request.urlopen(req) as response:
            return response.status, response.read().decode("utf-8")
    except error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _extract_operator_ui_config(html: str) -> dict[str, object]:
    match = re.search(
        r'<script id="sheet-vitrina-v1-operator-config" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    if match is None:
        raise AssertionError("operator UI config script is missing")
    return json.loads(match.group(1))


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
