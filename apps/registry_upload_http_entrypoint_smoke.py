"""Интеграционный smoke-check для HTTP entrypoint registry upload."""

from dataclasses import asdict
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from urllib import error, request as urllib_request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_UPLOAD_PATH,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig

ARTIFACTS_DIR = ROOT / "artifacts" / "registry_upload_http_entrypoint"
INPUT_BUNDLE_FIXTURE = ARTIFACTS_DIR / "input" / "registry_upload_bundle__fixture.json"
TARGET_DIR = ARTIFACTS_DIR / "target"
ACTIVATED_AT = "2026-04-13T12:00:03Z"


def main() -> None:
    with TemporaryDirectory(prefix="registry-upload-http-entrypoint-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        port = _reserve_free_port()
        config = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1",
            port=port,
            upload_path=DEFAULT_UPLOAD_PATH,
            sheet_plan_path=DEFAULT_SHEET_PLAN_PATH,
            runtime_dir=runtime_dir,
        )
        env = os.environ.copy()
        env.update(
            {
                "REGISTRY_UPLOAD_HTTP_HOST": config.host,
                "REGISTRY_UPLOAD_HTTP_PORT": str(config.port),
                "REGISTRY_UPLOAD_HTTP_PATH": config.upload_path,
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

            accepted_status, accepted_payload = _post_json_when_ready(
                base_url,
                _load_json(INPUT_BUNDLE_FIXTURE),
            )
            if accepted_status != 200:
                raise AssertionError(f"accepted request must return 200, got {accepted_status}")
            accepted_expected = _load_json(TARGET_DIR / "http_result__accepted__fixture.json")
            if accepted_payload != accepted_expected:
                raise AssertionError("accepted HTTP result differs from target fixture")

            runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
            current_state = asdict(runtime.load_current_state())
            current_expected = _load_json(TARGET_DIR / "current_state__fixture.json")
            if current_state != current_expected:
                raise AssertionError("runtime current state differs from HTTP target fixture")

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
            print(f"duplicate status: ok -> {duplicate_payload['status']}")
            print("smoke-check passed")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


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


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
