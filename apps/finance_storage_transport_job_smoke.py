#!/usr/bin/env python3
"""Regression checks for durable Finance hold-mutation transport."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.finance_storage_transport_job import (
    REQUEST_CONTRACT,
    _digest,
    _status_payload,
    resume_job,
    submit_job,
)


DEPLOYED_SHA = "a" * 40


def _request(
    *,
    repo_root: Path,
    stdin_text: str,
    action: str = "rollback-apply",
) -> dict[str, object]:
    runner_args = [
        "python3",
        "apps/finance_storage_split.py",
        action,
    ]
    identity_without_job = {
        "contract_name": (
            "wb_core_finance_storage_transport_identity_v1"
        ),
        "action": action,
        "deployed_sha": DEPLOYED_SHA,
        "runner_args": runner_args,
        "stdin_sha256": (
            "sha256:"
            + hashlib.sha256(stdin_text.encode("utf-8")).hexdigest()
        ),
        "timeout_seconds": 30,
    }
    job_id = hashlib.sha256(
        json.dumps(
            identity_without_job,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    identity = {**identity_without_job, "job_id": job_id}
    return {
        "contract_name": REQUEST_CONTRACT,
        "job_id": job_id,
        "request_identity": _digest(identity),
        "identity": identity,
        "action": action,
        "deployed_sha": DEPLOYED_SHA,
        "repo_root": str(repo_root),
        "runner_args": runner_args,
        "stdin_text": stdin_text,
        "timeout_seconds": 30,
    }


def main() -> None:
    with tempfile.TemporaryDirectory(
        prefix="finance-storage-transport-smoke-"
    ) as raw:
        root = Path(raw)
        runtime = root / "runtime"
        repo = root / "repo"
        apps = repo / "apps"
        apps.mkdir(parents=True)
        runtime.mkdir()
        marker = repo / ".wb-core-runtime-sha"
        marker.write_text(DEPLOYED_SHA + "\n", encoding="utf-8")
        runner = apps / "finance_storage_split.py"
        runner.write_text(
            "import json,sys,time\n"
            "time.sleep(0.25)\n"
            "payload=json.load(sys.stdin)\n"
            "print(json.dumps({'status':'ok','echo':payload}))\n",
            encoding="utf-8",
        )
        stdin_text = json.dumps({"fingerprint": "sha256:" + "b" * 64})
        request = _request(
            repo_root=repo,
            stdin_text=stdin_text,
        )
        seed = str(request["job_id"])
        first = submit_job(
            runtime,
            job_id=seed,
            deployed_sha_file=marker,
            request_payload=request,
        )
        second = submit_job(
            runtime,
            job_id=seed,
            deployed_sha_file=marker,
            request_payload=request,
        )
        assert first["worker_classification"] == "active_worker", first
        assert second["worker_classification"] == "active_worker", second
        assert first["pid"] == second["pid"]

        mismatch = dict(request)
        mismatch["request_identity"] = "sha256:" + "c" * 64
        try:
            submit_job(
                runtime,
                job_id=seed,
                deployed_sha_file=marker,
                request_payload=mismatch,
            )
        except ValueError:
            pass
        else:
            raise AssertionError(
                "mismatched durable request was not rejected"
            )
        tampered_stdin = dict(request)
        tampered_stdin["stdin_text"] = '{"tampered":true}'
        try:
            submit_job(
                runtime,
                job_id=seed,
                deployed_sha_file=marker,
                request_payload=tampered_stdin,
            )
        except ValueError:
            pass
        else:
            raise AssertionError(
                "transport request accepted stdin outside its identity"
            )

        deadline = time.monotonic() + 10
        terminal: dict[str, object] = {}
        while time.monotonic() < deadline:
            terminal = _status_payload(
                runtime,
                job_id=seed,
                deployed_sha=DEPLOYED_SHA,
            )
            if terminal.get("terminal"):
                break
            time.sleep(0.05)
        assert terminal["status"] == "succeeded"
        assert terminal["worker_classification"] == "terminal_succeeded"
        assert terminal["result"] == {
            "status": "ok",
            "echo": json.loads(stdin_text),
        }
        repeated = submit_job(
            runtime,
            job_id=seed,
            deployed_sha_file=marker,
            request_payload=request,
        )
        assert repeated["status"] == "succeeded"
        assert repeated["pid"] == terminal["pid"]

        resume_repo = root / "resume-repo"
        resume_apps = resume_repo / "apps"
        resume_apps.mkdir(parents=True)
        resume_runner = resume_apps / "finance_storage_split.py"
        resume_runner.write_text(
            "import json,sys\n"
            "from pathlib import Path\n"
            "payload=json.load(sys.stdin)\n"
            "marker=Path('.resume-attempt')\n"
            "if not marker.exists():\n"
            " marker.write_text('failed',encoding='utf-8')\n"
            " raise SystemExit(75)\n"
            "print(json.dumps({'status':'completed','echo':payload}))\n",
            encoding="utf-8",
        )
        resume_request = _request(
            repo_root=resume_repo,
            stdin_text=stdin_text,
            action="snapshot-retention-apply",
        )
        resume_seed = str(resume_request["job_id"])
        submit_job(
            runtime,
            job_id=resume_seed,
            deployed_sha_file=marker,
            request_payload=resume_request,
        )
        failed: dict[str, object] = {}
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            failed = _status_payload(
                runtime,
                job_id=resume_seed,
                deployed_sha=DEPLOYED_SHA,
            )
            if failed.get("terminal"):
                break
            time.sleep(0.05)
        assert failed["worker_classification"] == "terminal_failed", failed
        resumed = resume_job(
            runtime,
            job_id=resume_seed,
            deployed_sha_file=marker,
            request_payload=resume_request,
        )
        assert resumed["worker_classification"] == "active_worker", resumed
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            resumed = _status_payload(
                runtime,
                job_id=resume_seed,
                deployed_sha=DEPLOYED_SHA,
            )
            if resumed.get("terminal"):
                break
            time.sleep(0.05)
        assert resumed["worker_classification"] == "terminal_succeeded", resumed
        assert resumed["attempt"] == 2, resumed
        assert resumed["previous_failure_digest"]
        assert (
            runtime
            / "finance-storage-transport-jobs"
            / resume_seed
            / "status-attempt-1.json"
        ).is_file()
        repeated_resume = resume_job(
            runtime,
            job_id=resume_seed,
            deployed_sha_file=marker,
            request_payload=resume_request,
        )
        assert repeated_resume["status"] == "succeeded", repeated_resume
    print(
        "finance_storage_transport_job_smoke: ok -> "
        "one exact worker, disconnect-safe observation, request drift blocked, "
        "explicit same-job failed-worker resume"
    )


if __name__ == "__main__":
    main()
