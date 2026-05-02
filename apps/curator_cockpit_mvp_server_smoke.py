"""Smoke-check for the local-only curator cockpit MVP server."""

from __future__ import annotations

import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from tempfile import TemporaryDirectory
from urllib import error as urllib_error, request as urllib_request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SERVER = ROOT / "apps" / "curator_cockpit_mvp_server.py"
EXAMPLE_SPEC = ROOT / "artifacts" / "curator_cockpit_mvp" / "input" / "example_task_spec.json"


def main() -> None:
    port = _free_port()
    with TemporaryDirectory(prefix="curator-cockpit-server-smoke-") as tmp:
        state_dir = Path(tmp) / "state"
        process = subprocess.Popen(
            [
                sys.executable,
                str(SERVER),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--state-dir",
                str(state_dir),
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            base_url = f"http://127.0.0.1:{port}"
            _wait_ready(base_url)

            html = _get_text(base_url + "/")
            if "Curator Cockpit MVP" not in html or "Local-only MVP prototype" not in html:
                raise AssertionError("root route must return cockpit HTML with local-only notice")

            state = _get_json(base_url + "/api/state")
            if state.get("host") != "127.0.0.1" or state.get("local_only") is not True:
                raise AssertionError(f"server must report local-only 127.0.0.1 binding: {state}")
            for route in state.get("exposed_routes", []):
                if "deploy" in route.lower() or "live" in route.lower():
                    raise AssertionError(f"server must not expose live/deploy route: {route}")
            if state.get("live_deploy_enabled") is not False or state.get("public_routes_enabled") is not False:
                raise AssertionError(f"live/public flags must stay false: {state}")

            discussion = _post_json(base_url + "/api/discussions", {"title": "Smoke discussion"})
            discussion_id = discussion["id"]
            discussion = _post_json(
                base_url + f"/api/discussions/{discussion_id}/messages",
                {"role": "operator", "content": "Prepare a local repo-only task."},
            )
            messages = discussion.get("messages", [])
            if len(messages) != 2 or messages[1].get("role") != "curator":
                raise AssertionError(f"operator message must append static curator placeholder: {messages}")

            example_spec = json.loads(EXAMPLE_SPEC.read_text(encoding="utf-8"))
            draft = _post_json(base_url + "/api/task-specs", example_spec)
            task_spec_id = draft["id"]
            _expect_http_error(
                lambda: _post_json(base_url + f"/api/task-specs/{task_spec_id}/generate-prompt", {"step_id": "step-001"}),
                expected_status=400,
            )

            frozen_summary = _post_json(
                base_url + f"/api/task-specs/{task_spec_id}/freeze",
                {"frozen_at": "2026-05-01T00:00:00Z"},
            )
            if frozen_summary.get("status") != "frozen" or not frozen_summary.get("spec_hash"):
                raise AssertionError(f"freeze must return frozen spec hash: {frozen_summary}")

            frozen = _get_json(base_url + f"/api/task-specs/{task_spec_id}")
            if frozen.get("status") != "frozen":
                raise AssertionError(f"stored task spec must be frozen: {frozen}")
            if not frozen.get("sprint_steps"):
                raise AssertionError("stored task spec must keep sprint steps")

            prompt_summary = _post_json(
                base_url + f"/api/task-specs/{task_spec_id}/generate-prompt",
                {"step_id": "step-001"},
            )
            if not prompt_summary.get("mandatory_blocks_present"):
                raise AssertionError(f"prompt summary must confirm mandatory blocks: {prompt_summary}")

            prompt = _get_text(base_url + f"/api/prompts/{prompt_summary['id']}")
            for token in (
                "Класс задачи:",
                "Причина классификации:",
                "Режим выполнения:",
                "=== ДЛЯ КУРАТОРА ===",
                "=== СЖАТАЯ ПРОВЕРКА ===",
            ):
                if token not in prompt:
                    raise AssertionError(f"generated prompt missing token: {token}")

            _expect_http_error(lambda: _get_json(base_url + "/api/live-deploy"), expected_status=404)
            _expect_http_error(lambda: _get_json(base_url + "/deploy"), expected_status=404)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    print("curator-cockpit-mvp-server-smoke passed")


def _wait_ready(base_url: str) -> None:
    deadline = time.time() + 10
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            _get_json(base_url + "/api/state")
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.1)
    raise AssertionError(f"server did not become ready: {last_error}")


def _get_json(url: str) -> dict:
    return json.loads(_get_text(url))


def _get_text(url: str) -> str:
    with urllib_request.urlopen(url, timeout=10) as response:
        return response.read().decode("utf-8")


def _post_json(url: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib_request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib_request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _expect_http_error(callback, expected_status: int) -> None:
    try:
        callback()
    except urllib_error.HTTPError as exc:
        if exc.code != expected_status:
            raise AssertionError(f"expected HTTP {expected_status}, got {exc.code}") from exc
        return
    raise AssertionError(f"expected HTTP {expected_status}")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    main()
