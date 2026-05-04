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
        created_run_id = None
        try:
            base_url = f"http://127.0.0.1:{port}"
            _wait_ready(base_url)

            html = _get_text(base_url + "/")
            if "Curator Cockpit MVP" not in html or "Local-only MVP prototype" not in html:
                raise AssertionError("root route must return cockpit HTML with local-only notice")
            for token in (
                "Draft Task Spec from Discussion",
                "Run Safe Fake Flow",
                "Advanced / Raw JSON",
                "Fake executor only in MVP",
            ):
                if token not in html:
                    raise AssertionError(f"root route must expose simplified guided UI token: {token}")

            state = _get_json(base_url + "/api/state")
            if state.get("host") != "127.0.0.1" or state.get("local_only") is not True:
                raise AssertionError(f"server must report local-only 127.0.0.1 binding: {state}")
            for route in state.get("exposed_routes", []):
                if "deploy" in route.lower() or "live" in route.lower():
                    raise AssertionError(f"server must not expose live/deploy route: {route}")
            if state.get("live_deploy_enabled") is not False or state.get("public_routes_enabled") is not False:
                raise AssertionError(f"live/public flags must stay false: {state}")
            if state.get("fake_executor_enabled") is not True or state.get("real_executor_enabled") is not False:
                raise AssertionError(f"server must expose fake-only executor state: {state}")
            if state.get("ai_curator_enabled") is not True or state.get("openai_curator_optional") is not True:
                raise AssertionError(f"server must expose optional AI curator state: {state}")

            discussion = _post_json(base_url + "/api/discussions", {"title": "Smoke discussion"})
            discussion_id = discussion["id"]
            discussion = _post_json(
                base_url + f"/api/discussions/{discussion_id}/messages",
                {"role": "operator", "content": "Prepare a local repo-only task."},
            )
            messages = discussion.get("messages", [])
            if len(messages) != 2 or messages[1].get("role") != "curator":
                raise AssertionError(f"operator message must append static curator placeholder: {messages}")

            draft_summary = _post_json(
                base_url + f"/api/discussions/{discussion_id}/draft-task-spec",
                {"mode": "fake"},
            )
            if draft_summary.get("status") != "drafted" or draft_summary.get("provider") != "fake":
                raise AssertionError(f"fake curator must draft valid task spec: {draft_summary}")
            task_spec_id = draft_summary["task_spec_id"]
            draft = _get_json(base_url + f"/api/task-specs/{task_spec_id}")
            for path in ("wb_core_docs_master/**", "99_MANIFEST__DOCSET_VERSION.md"):
                if path not in draft.get("forbidden_paths", []):
                    raise AssertionError(f"draft must preserve forbidden path: {path}")
            for action in ("live_deploy", "ssh", "root_shell", "public_route_change"):
                if action not in draft.get("forbidden_actions", []):
                    raise AssertionError(f"draft must preserve forbidden action: {action}")

            _expect_http_error(
                lambda: _post_json(base_url + f"/api/task-specs/{task_spec_id}/generate-prompt", {"step_id": "step-001"}),
                expected_status=400,
            )
            _expect_http_error(
                lambda: _post_json(base_url + f"/api/task-specs/{task_spec_id}/prepare-run", {"step_id": "step-001"}),
                expected_status=400,
            )
            _expect_http_error(
                lambda: _post_json(base_url + f"/api/task-specs/{task_spec_id}/run-fake", {"step_id": "step-001"}),
                expected_status=400,
            )
            _expect_http_error(
                lambda: _post_json(base_url + "/api/guided-safe-fake-run", {"task_spec_id": task_spec_id, "step_id": "step-001"}),
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

            prepared = _post_json(
                base_url + f"/api/task-specs/{task_spec_id}/prepare-run",
                {"step_id": "step-001"},
            )
            if prepared.get("status") != "prepared" or prepared.get("verifier_status") is not None:
                raise AssertionError(f"prepare-run must only prepare local artifacts: {prepared}")
            prepared_run = _get_json(base_url + f"/api/runs/{prepared['run_id']}")
            if "Класс задачи:" not in prepared_run.get("prompt_text", ""):
                raise AssertionError(f"prepared run must expose prompt preview: {prepared_run}")
            if prepared_run.get("handoff_text") is not None:
                raise AssertionError(f"prepared run must not have handoff yet: {prepared_run}")

            fake_run = _post_json(
                base_url + f"/api/task-specs/{task_spec_id}/run-fake",
                {"step_id": "step-001"},
            )
            created_run_id = fake_run["run_id"]
            if fake_run.get("status") != "verifier_passed" or fake_run.get("verifier_status") != "passed":
                raise AssertionError(f"run-fake must pass verifier: {fake_run}")
            if fake_run.get("mandatory_handoff_blocks_present") is not True:
                raise AssertionError(f"run-fake must report mandatory handoff blocks: {fake_run}")
            worktree_path = Path(fake_run["worktree_path"]).resolve()
            if worktree_path == ROOT.resolve() or not _is_relative_to(worktree_path, state_dir.resolve()):
                raise AssertionError(f"run-fake must use isolated smoke worktree: {worktree_path}")

            run = _get_json(base_url + f"/api/runs/{created_run_id}")
            for token in ("=== ДЛЯ КУРАТОРА ===", "=== СЖАТАЯ ПРОВЕРКА ==="):
                if token not in run.get("handoff_text", ""):
                    raise AssertionError(f"run handoff missing token: {token}")
            if "Класс задачи:" not in run.get("prompt_text", ""):
                raise AssertionError("run prompt preview must include classification header")

            verified = _post_json(base_url + f"/api/runs/{created_run_id}/verify", {})
            if verified.get("verifier_status") != "passed":
                raise AssertionError(f"verify-run endpoint must pass: {verified}")

            cleanup = _post_json(base_url + f"/api/runs/{created_run_id}/cleanup", {})
            if cleanup.get("cleanup", {}).get("status") != "cleaned":
                raise AssertionError(f"cleanup endpoint must clean owned worktree: {cleanup}")
            if worktree_path.exists():
                raise AssertionError(f"cleanup must remove owned worktree: {worktree_path}")
            if _branch_exists(str(fake_run["branch_name"])):
                raise AssertionError(f"cleanup must remove owned test branch: {fake_run['branch_name']}")
            created_run_id = None

            guided = _post_json(
                base_url + "/api/guided-safe-fake-run",
                {"task_spec_id": task_spec_id, "step_id": "step-001"},
            )
            created_run_id = guided["run_id"]
            if guided.get("status") != "verifier_passed" or guided.get("verifier_status") != "passed":
                raise AssertionError(f"guided safe fake flow must pass verifier: {guided}")
            guided_run = _get_json(base_url + f"/api/runs/{created_run_id}")
            if not guided_run.get("prompt_text") or not guided_run.get("handoff_text"):
                raise AssertionError(f"guided run must expose prompt and handoff: {guided_run}")
            _post_json(base_url + f"/api/runs/{created_run_id}/cleanup", {})
            if _branch_exists(str(guided_run["branch_name"])):
                raise AssertionError(f"guided cleanup must remove owned test branch: {guided_run['branch_name']}")
            created_run_id = None

            _expect_http_error(lambda: _get_json(base_url + "/api/live-deploy"), expected_status=404)
            _expect_http_error(lambda: _get_json(base_url + "/deploy"), expected_status=404)
        finally:
            if created_run_id:
                try:
                    _post_json(base_url + f"/api/runs/{created_run_id}/cleanup", {})
                except Exception:
                    pass
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


def _branch_exists(branch_name: str) -> bool:
    completed = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", branch_name],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


if __name__ == "__main__":
    main()
